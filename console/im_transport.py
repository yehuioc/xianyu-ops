"""Owned transport over the official IM page's authenticated WebSocket.

The browser page remains the authentication/session owner.  Preparing the
bridge requires an explicit page reload and is disabled by default.  Protocol
facts were informed by the archived AGPL reference.  This project-owned module
has no vendor runtime imports.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import suppress
from typing import Any
from urllib.parse import urlencode

from .im_codec import (
    HistoryMessage,
    build_history_request,
    build_text_send_frame,
    decode_history,
    frame_json,
)
from .order_detail import (
    OrderDetailCache,
    OrderDetailEvidence,
    extract_order_detail_evidence,
    is_order_detail_url,
)


class TransportError(RuntimeError):
    def __init__(self, code: str, message: str, *, attempted: bool = False):
        self.code = code
        self.attempted = attempted
        super().__init__(message)


BRIDGE_SCRIPT = r"""
(() => {
  if (window.__xianyuOwnedBridgeVersion === 2) return;
  const NativeWebSocket = window.WebSocket;
  const receiveQueue = [];
  const sentFrames = [];
  const remember = (list, value) => {
    list.push(String(value));
    if (list.length > 1000) list.splice(0, list.length - 1000);
  };
  class OwnedWebSocket extends NativeWebSocket {
    constructor(...args) {
      super(...args);
      const url = String(args[0] || "");
      if (url.includes("wss-goofish.dingtalk.com")) {
        window.__xianyuOwnedSocket = this;
        window.__xianyuOwnedSocketGeneration = (window.__xianyuOwnedSocketGeneration || 0) + 1;
        window.__xianyuOwnedSubscriptionGeneration = 0;
        this.addEventListener("message", event => {
          if (typeof event.data === "string") remember(receiveQueue, event.data);
          else if (event.data instanceof Blob) event.data.text().then(value => remember(receiveQueue, value));
          else if (event.data instanceof ArrayBuffer) remember(receiveQueue, new TextDecoder().decode(event.data));
        });
      }
    }
    send(value) {
      if (this === window.__xianyuOwnedSocket) {
        remember(sentFrames, value);
        if (String(value).includes('/r/Conversation/listNewestPagination')) {
          window.__xianyuOwnedSubscriptionGeneration = window.__xianyuOwnedSocketGeneration;
        }
      }
      return super.send(value);
    }
  }
  Object.defineProperties(OwnedWebSocket, {
    CONNECTING: {value: NativeWebSocket.CONNECTING},
    OPEN: {value: NativeWebSocket.OPEN},
    CLOSING: {value: NativeWebSocket.CLOSING},
    CLOSED: {value: NativeWebSocket.CLOSED}
  });
  window.WebSocket = OwnedWebSocket;
  window.__xianyuOwnedReceiveQueue = receiveQueue;
  window.__xianyuOwnedSentFrames = sentFrames;
  window.__xianyuOwnedBridgeInstalled = true;
  window.__xianyuOwnedBridgeVersion = 2;
})();
"""


class EdgeImTransport:
    """Bounded Playwright/CDP adapter for one existing official IM tab."""

    _process_owner = threading.Lock()

    def __init__(
        self,
        cdp_url: str = "http://127.0.0.1:9223",
        *,
        prepare_page: bool = False,
        connect_timeout: float = 15.0,
        operation_timeout: float = 10.0,
        poll_interval: float = 0.1,
        reconnect_limit: int = 3,
        reconnect_backoff: float = 1.0,
    ):
        self.cdp_url = cdp_url.rstrip("/")
        self.prepare_page = bool(prepare_page)
        self.connect_timeout = min(max(float(connect_timeout), 2.0), 30.0)
        self.operation_timeout = min(max(float(operation_timeout), 1.0), 30.0)
        self.poll_interval = min(max(float(poll_interval), 0.05), 1.0)
        self.reconnect_limit = min(max(int(reconnect_limit), 0), 10)
        self.reconnect_backoff = min(max(float(reconnect_backoff), 0.1), 10.0)
        self.order_details = OrderDetailCache()
        self._playwright = None
        self._browser = None
        self._page = None
        self._pump_task: asyncio.Task | None = None
        self._inbound: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._waiters: dict[str, asyncio.Future] = {}
        self._owned = False
        self._ready = False
        self._closing = False
        self._connection_state = "inactive"
        self._reconnect_attempts = 0
        self._last_connected_at: float | None = None
        self._last_disconnected_at: float | None = None
        self._fatal_disconnect = False
        self._last_error: dict[str, str] | None = None

    async def connect(self) -> None:
        if self._ready:
            return
        self._closing = False
        self._fatal_disconnect = False
        self._connection_state = "connecting"
        if not self._process_owner.acquire(blocking=False):
            raise TransportError("DUPLICATE_OWNER", "当前进程已有一个闲鱼消息传输实例。")
        self._owned = True
        try:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._browser = await asyncio.wait_for(
                self._playwright.chromium.connect_over_cdp(self.cdp_url, timeout=int(self.connect_timeout * 1000)),
                timeout=self.connect_timeout + 1,
            )
            pages = [page for context in self._browser.contexts for page in context.pages]
            candidates = [page for page in pages if "goofish.com" in page.url and "/im" in page.url]
            if len(candidates) != 1:
                raise TransportError(
                    "IM_TAB_REQUIRED",
                    "需要且只能有一个已登录的闲鱼官方 IM 页面；本次未修改浏览器页面。",
                )
            self._page = candidates[0]
            # Observe any user-opened order-detail tab in the same authenticated
            # browser context.  This is passive: no tab is opened or navigated.
            for context in self._browser.contexts:
                context.on("response", self._on_response)
            bridge_version = await asyncio.wait_for(
                self._page.evaluate("Number(window.__xianyuOwnedBridgeVersion || 0)"),
                timeout=self.operation_timeout,
            )
            if self.prepare_page:
                # Init scripts belong to the attached CDP session.  A version
                # marker in the current document does not prove that a future
                # reload will install the bridge after this process restarts.
                await self._page.add_init_script(BRIDGE_SCRIPT)
            elif bridge_version != 2:
                raise TransportError(
                    "BRIDGE_NOT_PREPARED",
                    "官方 IM 页尚未安装当前版本桥接器；需要在明确启用后以 prepare_page=True 重载一次该页。",
                )
            if self.prepare_page and (bridge_version != 2 or not await self._bridge_ready()):
                await self._page.reload(wait_until="domcontentloaded", timeout=int(self.connect_timeout * 1000))
            if await self._wait_until_ready():
                self._mark_connected()
                self._pump_task = asyncio.create_task(self._pump(), name="xianyu-owned-im-pump")
                return
            raise TransportError("IM_SOCKET_TIMEOUT", "官方 IM 页面未在限定时间内建立可用连接。")
        except Exception:
            await self.close()
            raise

    async def _bridge_ready(self) -> bool:
        if not self._page:
            return False
        try:
            return bool(await asyncio.wait_for(self._page.evaluate(
                """Boolean(
                  window.__xianyuOwnedBridgeInstalled &&
                  window.__xianyuOwnedBridgeVersion === 2 &&
                  window.__xianyuOwnedSocket &&
                  window.__xianyuOwnedSocket.readyState === WebSocket.OPEN &&
                  window.__xianyuOwnedSubscriptionGeneration === window.__xianyuOwnedSocketGeneration
                )"""
            ), timeout=self.operation_timeout))
        except Exception:
            return False

    async def _wait_until_ready(self) -> bool:
        deadline = time.monotonic() + self.connect_timeout
        while not self._closing and time.monotonic() < deadline:
            if await self._bridge_ready():
                return True
            await asyncio.sleep(0.2)
        return False

    def _mark_connected(self) -> None:
        self._ready = True
        self._connection_state = "connected"
        self._reconnect_attempts = 0
        self._last_connected_at = time.time()
        self._last_error = None

    def _on_response(self, response: Any) -> None:
        if is_order_detail_url(getattr(response, "url", "")):
            asyncio.create_task(self._capture_order_detail(response))

    async def _capture_order_detail(self, response: Any) -> None:
        try:
            payload = await asyncio.wait_for(response.json(), timeout=self.operation_timeout)
            if isinstance(payload, dict):
                self.order_details.observe(response.url, payload)
        except Exception as exc:
            self._last_error = {"code": "ORDER_DETAIL_CAPTURE_FAILED", "message": type(exc).__name__}

    def _fail_waiters_for_disconnect(self) -> None:
        for future in self._waiters.values():
            if not future.done():
                future.set_exception(TransportError(
                    "TRANSPORT_DISCONNECTED", "官方 IM 连接已断开；查询结果不确定。", attempted=True,
                ))
        self._waiters.clear()

    async def _recover_socket(self) -> bool:
        self._ready = False
        self._last_disconnected_at = time.time()
        self._fail_waiters_for_disconnect()
        if self.reconnect_limit <= 0:
            self._connection_state = "blocked"
            self._fatal_disconnect = True
            return False
        self._connection_state = "reconnecting"
        for attempt in range(1, self.reconnect_limit + 1):
            if self._closing or not self._page:
                return False
            self._reconnect_attempts = attempt
            await asyncio.sleep(min(self.reconnect_backoff * attempt, 10.0))
            if await self._bridge_ready():
                self._mark_connected()
                return True
            # A prepared page may be reloaded to recreate the bridge and the
            # official page's own subscription.  No application frame is sent.
            if self.prepare_page:
                try:
                    await self._page.reload(
                        wait_until="domcontentloaded", timeout=int(self.connect_timeout * 1000),
                    )
                except Exception as exc:
                    self._last_error = {"code": "IM_RELOAD_FAILED", "message": type(exc).__name__}
            # The page needs a full bounded connection window after reloading.
            # Repeated reloads a second apart can prevent its handshake finishing.
            if await self._wait_until_ready():
                self._mark_connected()
                return True
        self._connection_state = "blocked"
        self._fatal_disconnect = True
        self._last_error = {
            "code": "IM_RECONNECT_EXHAUSTED",
            "message": f"官方 IM 连接在 {self.reconnect_limit} 次有限恢复后仍不可用。",
        }
        return False

    async def _pump(self) -> None:
        while not self._closing and self._page:
            try:
                if not await self._bridge_ready():
                    if not await self._recover_socket():
                        return
                    continue
                frames = await asyncio.wait_for(self._page.evaluate(
                    """(() => {
                      const queue = window.__xianyuOwnedReceiveQueue || [];
                      return queue.splice(0, Math.min(queue.length, 100));
                    })()"""
                ), timeout=self.operation_timeout)
                for raw in frames or []:
                    try:
                        value = json.loads(raw)
                    except (TypeError, ValueError):
                        value = None
                    mid = str(((value or {}).get("headers") or {}).get("mid") or "") if isinstance(value, dict) else ""
                    waiter = self._waiters.pop(mid, None) if mid else None
                    if waiter and not waiter.done():
                        waiter.set_result(value)
                    else:
                        if self._inbound.full():
                            with suppress(asyncio.QueueEmpty):
                                self._inbound.get_nowait()
                        self._inbound.put_nowait(str(raw))
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = {"code": "BRIDGE_READ_FAILED", "message": type(exc).__name__}
                await asyncio.sleep(min(1.0, self.poll_interval * 5))

    async def send_raw(self, payload: dict[str, Any]) -> None:
        if not self._ready or not self._page or not await self._bridge_ready():
            raise TransportError("TRANSPORT_NOT_READY", "官方 IM 连接当前不可发送。")
        encoded = frame_json(payload)
        try:
            sent = await asyncio.wait_for(self._page.evaluate(
                """payload => {
                  const ws = window.__xianyuOwnedSocket;
                  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
                  ws.send(payload);
                  return true;
                }""",
                encoded,
            ), timeout=self.operation_timeout)
        except Exception as exc:
            raise TransportError("SEND_AMBIGUOUS", "浏览器发送调用结果不确定。", attempted=True) from exc
        if not sent:
            raise TransportError("SEND_AMBIGUOUS", "官方 IM 连接在发送时已不可用。", attempted=True)

    async def send_text(self, *, cid: str, recipient_id: str, self_user_id: str, text: str) -> None:
        await self.send_raw(build_text_send_frame(
            cid=cid, recipient_id=recipient_id, self_user_id=self_user_id, text=text,
        ))

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._ready:
            raise TransportError("TRANSPORT_NOT_READY", "官方 IM 连接当前不可查询。")
        mid = str((payload.get("headers") or {}).get("mid") or "")
        if not mid:
            raise ValueError("request payload requires headers.mid")
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._waiters[mid] = future
        try:
            await self.send_raw(payload)
            return await asyncio.wait_for(future, timeout=self.operation_timeout)
        except asyncio.TimeoutError as exc:
            raise TransportError("REQUEST_TIMEOUT", "官方 IM 查询在限定时间内没有返回。", attempted=True) from exc
        finally:
            self._waiters.pop(mid, None)

    async def history(self, cid: str, *, limit: int = 50) -> list[HistoryMessage]:
        response = await self.request(build_history_request(cid, limit=limit))
        body = response.get("body") if isinstance(response.get("body"), dict) else {}
        return decode_history(body, cid=cid)

    async def fetch_order_detail(
        self, order_id: str, item_id: str, buyer_id: str | None = None,
    ) -> OrderDetailEvidence:
        """Fetch exact structured evidence in one bounded temporary authenticated tab."""
        order_id, item_id = str(order_id).strip(), str(item_id).strip()
        if not self._ready or not self._page or not order_id or not item_id:
            raise TransportError("ORDER_DETAIL_NOT_READY", "订单详情获取缺少可用浏览器上下文或目标标识。")
        cached = self.order_details.get(order_id)
        if cached:
            cached.validate(order_id=order_id, item_id=item_id, buyer_id=buyer_id)
            return cached

        context = self._page.context
        detail_page = await asyncio.wait_for(context.new_page(), timeout=self.operation_timeout)
        loop = asyncio.get_running_loop()
        result: asyncio.Future[OrderDetailEvidence] = loop.create_future()
        capture_tasks: set[asyncio.Task] = set()

        async def capture(response: Any) -> None:
            if result.done() or not is_order_detail_url(getattr(response, "url", "")):
                return
            try:
                payload = await asyncio.wait_for(response.json(), timeout=self.operation_timeout)
                evidence = extract_order_detail_evidence(
                    response.url, payload,
                    expected_order_id=order_id, expected_item_id=item_id,
                )
                evidence.validate(order_id=order_id, item_id=item_id, buyer_id=buyer_id)
                self.order_details.observe(response.url, payload)
                if not result.done():
                    result.set_result(evidence)
            except Exception as exc:
                self._last_error = {"code": getattr(exc, "code", "ORDER_DETAIL_REJECTED"), "message": str(exc)}

        def schedule(response: Any) -> None:
            task = asyncio.create_task(capture(response))
            capture_tasks.add(task)
            task.add_done_callback(capture_tasks.discard)

        detail_page.on("response", schedule)
        target = "https://www.goofish.com/order-detail?" + urlencode({"orderId": order_id, "role": "seller"})
        try:
            await asyncio.wait_for(
                detail_page.goto(target, wait_until="domcontentloaded", timeout=int(self.operation_timeout * 1000)),
                timeout=self.operation_timeout + 1,
            )
            return await asyncio.wait_for(result, timeout=self.operation_timeout)
        except asyncio.TimeoutError as exc:
            raise TransportError("ORDER_DETAIL_TIMEOUT", "订单详情未在限定时间内返回匹配的结构化响应。") from exc
        finally:
            with suppress(Exception):
                detail_page.remove_listener("response", schedule)
            for task in tuple(capture_tasks):
                task.cancel()
            if capture_tasks:
                await asyncio.gather(*capture_tasks, return_exceptions=True)
            with suppress(Exception):
                await detail_page.close()

    async def recv(self, *, timeout: float = 1.0) -> str | None:
        if self._fatal_disconnect:
            raise TransportError("TRANSPORT_DISCONNECTED", "官方 IM 连接已断开且有限恢复已经用尽。")
        try:
            return await asyncio.wait_for(self._inbound.get(), timeout=min(max(timeout, 0.05), 30.0))
        except asyncio.TimeoutError:
            return None

    async def close(self) -> None:
        self._closing = True
        self._ready = False
        if self._connection_state != "blocked":
            self._connection_state = "stopped"
        if self._pump_task:
            self._pump_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._pump_task
            self._pump_task = None
        for future in self._waiters.values():
            if not future.done():
                future.cancel()
        self._waiters.clear()
        # Do not browser.close(): this is the user's persistent Edge instance.
        if self._playwright:
            with suppress(Exception):
                await self._playwright.stop()
        self._playwright = self._browser = self._page = None
        if self._owned:
            self._owned = False
            self._process_owner.release()

    def status(self) -> dict[str, Any]:
        return {
            "ready": self._ready,
            "connection_state": self._connection_state,
            "reconnect_attempts": self._reconnect_attempts,
            "last_connected_at": self._last_connected_at,
            "last_disconnected_at": self._last_disconnected_at,
            "cdp_url": self.cdp_url,
            "prepare_page": self.prepare_page,
            "last_error": self._last_error,
            "order_detail_error": self.order_details.last_error,
        }
