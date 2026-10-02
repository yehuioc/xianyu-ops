"""Durable, fail-closed owned keyword reply and text delivery engine.

Only buyer text can trigger keyword replies.  Only an explicit paid/pending-ship
system event can trigger delivery.  Every send is persisted before the browser
call; an attempted send is never retried automatically unless official history
later proves the exact message was present.

The protocol behavior was informed by the archived AGPL reference.  This
project-owned module has no vendor runtime imports.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from inspect import isawaitable
from typing import Any, Awaitable, Callable, Iterable

from .im_codec import HistoryMessage, MessageEvent, decode_events
from .order_detail import OrderDetailEvidence, OrderDetailError
from .paths import PROJECT
from .store import CHINA, Store, now, product_key


LEGACY_DB = PROJECT / "vendor" / "xianyu-auto-reply-fix" / "data" / "xianyu_data.db"
DEFAULT_MANAGED_ITEMS = frozenset({"2534367850985", "2534016871941", "2534001733981"})
PAID_STATUSES = frozenset({
    "paid", "pending_ship", "wait_seller_send_goods", "wait_seller_send",
    "waitseller_send_goods", "待发货", "买家已付款", "等待卖家发货",
})
FINAL_STATES = frozenset({"confirmed", "message_confirmed", "platform_confirming", "platform_unconfirmed", "finalized"})
DO_NOT_RESEND_STATES = frozenset({"sending", "sent_unconfirmed", "ambiguous", "confirmed"})
MAX_DELIVERY_UNITS = 10
BUYER_FUTURE_SKEW_MS = 300_000


class MessagingError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    cid: str | None = None
    recipient_id: str | None = None
    text: str | None = None
    purpose: str | None = None
    item_id: str | None = None
    order_id: str | None = None
    unit_index: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _normalize_cid(value: Any) -> str:
    text = str(value or "").strip()
    return text[:-8] if text.endswith("@goofish") else text


def _normalize_spec(value: Any) -> str:
    return str(value or "").strip().replace(" ", "").replace("\u3000", "").casefold()


def _safe_format(template: str, event: MessageEvent) -> str:
    replacements = {
        "{send_user_name}": event.sender_name or "买家",
        "{send_user_id}": event.sender_id or "",
        "{send_message}": event.text or "",
        "{item_id}": event.item_id or "",
    }
    result = str(template)
    for marker, value in replacements.items():
        result = result.replace(marker, value)
    return result


def _schema(store: Store) -> None:
    with store.connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS message_inbox (
                account TEXT NOT NULL,
                message_id TEXT NOT NULL,
                cid TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                state TEXT NOT NULL,
                result TEXT,
                received_at TEXT NOT NULL,
                processed_at TEXT,
                PRIMARY KEY(account,message_id)
            );
            CREATE INDEX IF NOT EXISTS message_inbox_state ON message_inbox(account,state,received_at);
            CREATE TABLE IF NOT EXISTS message_outbox (
                id TEXT PRIMARY KEY,
                account TEXT NOT NULL,
                inbox_message_id TEXT,
                purpose TEXT NOT NULL,
                cid TEXT NOT NULL,
                recipient_id TEXT NOT NULL,
                item_id TEXT,
                order_id TEXT,
                unit_index INTEGER NOT NULL DEFAULT 1,
                total_units INTEGER NOT NULL DEFAULT 1,
                step_index INTEGER NOT NULL DEFAULT 1,
                payload_hash TEXT NOT NULL,
                text_content TEXT NOT NULL,
                state TEXT NOT NULL,
                prepared_at TEXT NOT NULL,
                attempted_at TEXT,
                confirmed_at TEXT,
                history_message_id TEXT,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS message_outbox_state ON message_outbox(account,state,prepared_at);
            CREATE TABLE IF NOT EXISTS delivery_finalizations (
                account TEXT NOT NULL,
                order_id TEXT NOT NULL,
                unit_index INTEGER NOT NULL DEFAULT 1,
                item_id TEXT,
                buyer_id TEXT,
                status TEXT NOT NULL,
                outbox_id TEXT,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(account,order_id,unit_index)
            );
            CREATE TABLE IF NOT EXISTS messaging_runtime (
                account TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                state TEXT NOT NULL,
                lease_until TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                owner_pid INTEGER,
                owner_create_time REAL,
                last_error TEXT
            );
        """)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(message_outbox)").fetchall()}
        if "total_units" not in columns:
            db.execute("ALTER TABLE message_outbox ADD COLUMN total_units INTEGER NOT NULL DEFAULT 1")
        runtime_columns = {row["name"] for row in db.execute("PRAGMA table_info(messaging_runtime)").fetchall()}
        if "owner_pid" not in runtime_columns:
            db.execute("ALTER TABLE messaging_runtime ADD COLUMN owner_pid INTEGER")
        if "owner_create_time" not in runtime_columns:
            db.execute("ALTER TABLE messaging_runtime ADD COLUMN owner_create_time REAL")


def _process_create_time(pid: int) -> float | None:
    try:
        import psutil
        return float(psutil.Process(int(pid)).create_time())
    except Exception:
        return None


def import_legacy_finalizations(store: Store, legacy_db: Path = LEGACY_DB) -> dict[str, Any]:
    """Read finalized legacy orders once without importing or mutating vendor code."""
    _schema(store)
    marker = store.setting("legacy_delivery_finalizations_import")
    if marker:
        return {**marker, "already_imported": True}
    if not legacy_db.is_file():
        return {"status": "source_missing", "imported": 0}
    source = sqlite3.connect(legacy_db.resolve().as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    imported = 0
    try:
        exists = source.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_finalization_states'"
        ).fetchone()
        if not exists:
            result = {"status": "table_missing", "imported": 0, "at": now()}
            store.set_setting("legacy_delivery_finalizations_import", result)
            return result
        rows = source.execute(
            "SELECT order_id,unit_index,cookie_id,item_id,buyer_id,status,updated_at "
            "FROM delivery_finalization_states"
        ).fetchall()
        with store.connect() as db:
            for row in rows:
                status = str(row["status"] or "").strip().casefold()
                if status not in {"finalized", "1", "true"} or not row["order_id"]:
                    continue
                account = str(row["cookie_id"] or "").strip()
                if not account:
                    continue
                db.execute(
                    "INSERT INTO delivery_finalizations(account,order_id,unit_index,item_id,buyer_id,status,outbox_id,source,updated_at) "
                    "VALUES(?,?,?,?,?,'finalized',NULL,'legacy_read_only_import',?) "
                    "ON CONFLICT(account,order_id,unit_index) DO NOTHING",
                    (account, str(row["order_id"]), int(row["unit_index"] or 1),
                     str(row["item_id"] or "") or None, str(row["buyer_id"] or "") or None,
                     str(row["updated_at"] or now())),
                )
                imported += db.execute("SELECT changes()").fetchone()[0]
    finally:
        source.close()
    result = {"status": "imported", "imported": imported, "at": now(), "source_modified": False}
    store.set_setting("legacy_delivery_finalizations_import", result)
    return result


class MessagingRunner:
    """Single-owner asynchronous messaging worker; inactive until explicitly enabled."""

    def __init__(
        self,
        store: Store,
        account: str,
        transport: Any,
        *,
        self_user_id: str | None = None,
        managed_item_ids: Iterable[str] | None = None,
        activation_authorized: bool = False,
        order_detail_provider: Callable[[str, str, str], OrderDetailEvidence | Awaitable[OrderDetailEvidence | None] | None] | None = None,
        order_refresher: Callable[[str], dict[str, Any] | Awaitable[dict[str, Any] | None] | None] | None = None,
        confirm_delivery: Callable[[str], Any | Awaitable[Any]] | None = None,
        lease_seconds: int = 30,
        reconcile_interval: float = 5.0,
    ):
        self.store = store
        self.account = str(account)
        self.activation_key = f"messaging_activated_at:{self.account}"
        self.transport = transport
        account_row = store.get("account", self.account, {})
        self.self_user_id = str(self_user_id or account_row.get("platform_user_id") or "").strip()
        self.managed_item_ids = frozenset(str(v) for v in (DEFAULT_MANAGED_ITEMS if managed_item_ids is None else managed_item_ids))
        self.activation_authorized = bool(activation_authorized)
        self.order_detail_provider = order_detail_provider or self._transport_order_detail
        self.order_refresher = order_refresher
        self.confirm_delivery = confirm_delivery
        self.lease_seconds = min(max(int(lease_seconds), 15), 120)
        self.reconcile_interval = min(max(float(reconcile_interval), 0.2), 300.0)
        self.owner_id = uuid.uuid4().hex
        self.owner_pid = os.getpid()
        self.owner_create_time = _process_create_time(self.owner_pid)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._active = False
        self._last_error: dict[str, str] | None = None
        _schema(store)
        self.legacy_finalizations = import_legacy_finalizations(store)
        if self._enabled() and self.store.setting(self.activation_key) is None:
            # A previously enabled installation without a recorded boundary
            # starts its safe reply window now; earlier sync history is ignored.
            self.store.set_setting(self.activation_key, now())

    def _enabled(self) -> bool:
        return bool(self.store.setting(
            f"messaging_enabled:{self.account}", self.store.setting("messaging_enabled", False)
        ))

    def set_enabled(self, enabled: bool, *, explicit_authorization: bool = False) -> dict[str, Any]:
        if enabled and not explicit_authorization:
            raise MessagingError("AUTHORIZATION_REQUIRED", "启用自动消息需要本次明确授权。")
        if enabled and self.store.setting(self.activation_key) is None:
            self.store.set_setting(self.activation_key, now())
        self.store.set_setting(f"messaging_enabled:{self.account}", bool(enabled))
        return self.status()

    def _activation_ms(self) -> int | None:
        value = self.store.setting(self.activation_key)
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            activated = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if activated.tzinfo is None:
                activated = activated.replace(tzinfo=CHINA)
            return int(activated.timestamp() * 1000)
        except (TypeError, ValueError):
            return None

    async def _transport_order_detail(
        self, order_id: str, item_id: str, buyer_id: str,
    ) -> OrderDetailEvidence | None:
        fetch = getattr(self.transport, "fetch_order_detail", None)
        if callable(fetch):
            return await fetch(order_id, item_id, buyer_id)
        cache = getattr(self.transport, "order_details", None)
        return cache.get(order_id) if cache and hasattr(cache, "get") else None

    async def _order_detail(self, order_id: str, item_id: str, buyer_id: str) -> OrderDetailEvidence | None:
        value = self.order_detail_provider(order_id, item_id, buyer_id)
        return await value if isawaitable(value) else value

    async def _refresh_order(self, order_id: str) -> dict[str, Any]:
        if not self.order_refresher:
            raise MessagingError("ORDER_REFRESH_REQUIRED", "发送交付内容前必须重新查询目标订单。")
        value = self.order_refresher(order_id)
        refreshed = await value if isawaitable(value) else value
        order = refreshed if isinstance(refreshed, dict) else self.store.get("order", order_id)
        if not isinstance(order, dict) or str(order.get("order_id") or "") != str(order_id):
            raise MessagingError("ORDER_REFRESH_FAILED", "没有取得目标订单的最新结构化状态。")
        return order

    def _acquire_lease(self) -> None:
        current = datetime.now(CHINA)
        lease_until = (current + timedelta(seconds=self.lease_seconds)).isoformat(timespec="seconds")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT owner_id,lease_until,owner_pid,owner_create_time "
                "FROM messaging_runtime WHERE account=?", (self.account,),
            ).fetchone()
            if row and row["owner_id"] != self.owner_id:
                try:
                    live = datetime.fromisoformat(row["lease_until"]) > current
                except (TypeError, ValueError):
                    live = False
                if live and row["owner_pid"] is not None and row["owner_create_time"] is not None:
                    observed_create_time = _process_create_time(int(row["owner_pid"]))
                    live = bool(
                        observed_create_time is not None
                        and abs(observed_create_time - float(row["owner_create_time"])) < 0.01
                    )
                if live:
                    raise MessagingError("DUPLICATE_OWNER", "该账号已有消息实例持有运行租约。")
            db.execute(
                "INSERT INTO messaging_runtime(account,owner_id,state,lease_until,heartbeat_at,owner_pid,owner_create_time,last_error) "
                "VALUES(?,?,'starting',?,?,?,?,NULL) ON CONFLICT(account) DO UPDATE SET "
                "owner_id=excluded.owner_id,state=excluded.state,lease_until=excluded.lease_until,"
                "heartbeat_at=excluded.heartbeat_at,owner_pid=excluded.owner_pid,"
                "owner_create_time=excluded.owner_create_time,last_error=NULL",
                (self.account, self.owner_id, lease_until, now(), self.owner_pid, self.owner_create_time),
            )

    def _heartbeat(self, state: str = "active") -> None:
        lease_until = (datetime.now(CHINA) + timedelta(seconds=self.lease_seconds)).isoformat(timespec="seconds")
        with self.store.connect() as db:
            changed = db.execute(
                "UPDATE messaging_runtime SET state=?,lease_until=?,heartbeat_at=?,last_error=? "
                "WHERE account=? AND owner_id=?",
                (state, lease_until, now(), json.dumps(self._last_error, ensure_ascii=False) if self._last_error else None,
                 self.account, self.owner_id),
            ).rowcount
        if not changed:
            raise MessagingError("LEASE_LOST", "消息运行租约已丢失，实例已停止。")

    def _release_lease(self, state: str = "stopped") -> None:
        with self.store.connect() as db:
            db.execute(
                "UPDATE messaging_runtime SET state=?,lease_until=?,heartbeat_at=?,last_error=? "
                "WHERE account=? AND owner_id=?",
                (state, now(), now(), json.dumps(self._last_error, ensure_ascii=False) if self._last_error else None,
                 self.account, self.owner_id),
            )

    def _recover_uncertain(self) -> int:
        with self.store.connect() as db:
            result = db.execute(
                "UPDATE message_outbox SET state='ambiguous',last_error=? "
                "WHERE account=? AND state='sending'",
                ("进程在发送状态结束；禁止自动重发，等待官方历史核对。", self.account),
            )
        return result.rowcount

    async def start(self) -> dict[str, Any]:
        if self._active:
            return self.status()
        if not self.activation_authorized or not self._enabled():
            raise MessagingError("MESSAGING_DISABLED", "自动消息尚未获得明确启用授权。")
        if not self.self_user_id:
            raise MessagingError("ACCOUNT_ID_MISSING", "账号缺少可核对的闲鱼用户 ID。")
        self._acquire_lease()
        try:
            self._recover_uncertain()
            await self.transport.connect()
            self._active = True
            self._stop.clear()
            self._heartbeat()
            self._task = asyncio.create_task(self._run_loop(), name=f"xianyu-messaging-{self.account}")
            return self.status()
        except Exception as exc:
            self._last_error = {"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}
            self._release_lease("blocked")
            raise

    async def run(self) -> None:
        await self.start()
        if self._task:
            await self._task

    async def stop(self) -> dict[str, Any]:
        self._stop.set()
        task, self._task = self._task, None
        if task and task is not asyncio.current_task():
            try:
                await asyncio.wait_for(task, timeout=5)
            except asyncio.TimeoutError:
                task.cancel()
        self._active = False
        await self.transport.close()
        self._release_lease()
        return self.status()

    async def _run_loop(self) -> None:
        heartbeat_due = 0.0
        reconcile_due = 0.0
        try:
            while not self._stop.is_set() and self._enabled():
                current = asyncio.get_running_loop().time()
                if current >= heartbeat_due:
                    self._heartbeat()
                    heartbeat_due = current + max(5, self.lease_seconds // 3)
                if current >= reconcile_due:
                    await self.reconcile_all_pending()
                    reconcile_due = current + self.reconcile_interval
                raw = await self.transport.recv(timeout=1.0)
                if raw is not None:
                    await self.process_frame(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = {"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}
            self._release_lease("blocked")
        finally:
            self._active = False
            await self.transport.close()
            self._release_lease("stopped" if self._stop.is_set() or not self._enabled() else "blocked")

    def _claim_inbox(self, event: MessageEvent) -> bool:
        with self.store.connect() as db:
            result = db.execute(
                "INSERT OR IGNORE INTO message_inbox(account,message_id,cid,kind,payload,state,received_at) "
                "VALUES(?,?,?,?,?,'received',?)",
                (self.account, event.message_id, event.cid, event.kind,
                 json.dumps(event.as_dict(), ensure_ascii=False), now()),
            )
        return result.rowcount == 1

    def _finish_inbox(self, event: MessageEvent, state: str, result: dict[str, Any]) -> None:
        with self.store.connect() as db:
            db.execute(
                "UPDATE message_inbox SET state=?,result=?,processed_at=? WHERE account=? AND message_id=?",
                (state, json.dumps(result, ensure_ascii=False), now(), self.account, event.message_id),
            )

    async def process_frame(self, raw: str | bytes | dict[str, Any]) -> list[dict[str, Any]]:
        results = []
        for event in decode_events(raw, account=self.account, self_user_id=self.self_user_id):
            result = await self.process_event(event)
            if result is not None:
                results.append(result)
        return results

    async def process_event(self, event: MessageEvent) -> dict[str, Any] | None:
        if event.account != self.account or not self._claim_inbox(event):
            return None
        if event.kind == "self_text":
            result = {"status": "observed_self_text", "message_id": event.message_id}
            self._finish_inbox(event, "processed", result)
            return result
        decision = self._keyword_decision(event) if event.kind == "buyer_text" else await self._delivery_decision(event)
        if decision.action == "blocked":
            result = self._record_block(event, decision)
            self._finish_inbox(event, "blocked", result)
            return result
        if decision.action == "none":
            result = {"status": "no_action", "reason": decision.reason}
            self._finish_inbox(event, "processed", result)
            return result
        try:
            result = await self._dispatch(event, decision)
            self._finish_inbox(event, "processed", result)
            return result
        except Exception as exc:
            result = {"status": "blocked", "code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}
            self._finish_inbox(event, "blocked", result)
            return result

    async def retry_blocked(self, message_id: str, *, explicit_authorization: bool = False) -> dict[str, Any]:
        """Explicitly retry a pre-send block after the missing evidence is supplied.

        This never retries an outbox row in an attempted state; `_dispatch` keeps
        the deterministic outbox id and suppresses those attempts.
        """
        if not explicit_authorization:
            raise MessagingError("AUTHORIZATION_REQUIRED", "重试被阻断的消息需要明确操作。")
        with self.store.connect() as db:
            row = db.execute(
                "SELECT payload,state FROM message_inbox WHERE account=? AND message_id=?",
                (self.account, str(message_id)),
            ).fetchone()
        if not row:
            raise MessagingError("INBOX_NOT_FOUND", "没有找到目标入站消息。")
        if row["state"] != "blocked":
            raise MessagingError("INBOX_NOT_BLOCKED", "目标消息当前不处于可重试的阻断状态。")
        payload = json.loads(row["payload"])
        event = MessageEvent(**payload)
        decision = self._keyword_decision(event) if event.kind == "buyer_text" else await self._delivery_decision(event)
        if decision.action == "blocked":
            result = self._record_block(event, decision)
            self._finish_inbox(event, "blocked", result)
            return result
        if decision.action == "none":
            result = {"status": "no_action", "reason": decision.reason}
            self._finish_inbox(event, "processed", result)
            return result
        result = await self._dispatch(event, decision)
        self._finish_inbox(event, "processed", result)
        return result

    def _owned_product(self, item_id: str | None) -> dict[str, Any] | None:
        if not item_id or str(item_id) not in self.managed_item_ids:
            return None
        product = self.store.get("product", product_key(self.account, str(item_id)))
        if not product or str(product.get("account") or self.account) != self.account:
            return None
        return product

    def _keyword_decision(self, event: MessageEvent) -> Decision:
        if event.created_ms is None:
            return Decision("blocked", "MESSAGE_TIME_MISSING")
        try:
            created_ms = int(event.created_ms)
            if created_ms < 100_000_000_000:
                created_ms *= 1000
        except (TypeError, ValueError):
            return Decision("blocked", "MESSAGE_TIME_INVALID")
        activated_ms = self._activation_ms()
        if activated_ms is None:
            return Decision("blocked", "ACTIVATION_TIME_MISSING")
        if created_ms < activated_ms:
            return Decision("none", "MESSAGE_BEFORE_ACTIVATION")
        current_ms = int(datetime.now(CHINA).timestamp() * 1000)
        if created_ms > current_ms + BUYER_FUTURE_SKEW_MS:
            return Decision("blocked", "MESSAGE_TIME_IN_FUTURE")
        if not event.cid or not event.sender_id or not event.text:
            return Decision("blocked", "INCOMPLETE_BUYER_MESSAGE")
        if event.sender_id == self.self_user_id:
            return Decision("none", "SELF_MESSAGE")
        if not self._owned_product(event.item_id):
            return Decision("blocked", "ITEM_NOT_MANAGED")
        candidates = []
        fallbacks = []
        for row in self.store.rows("keyword", self.account):
            if row.get("enabled") in (False, 0) or str(row.get("cookie_id") or self.account) != self.account:
                continue
            if row.get("activated_at"):
                try:
                    if created_ms < int(datetime.fromisoformat(row["activated_at"]).timestamp() * 1000):
                        continue
                except (TypeError, ValueError):
                    continue
            keyword = str(row.get("keyword") or "").strip()
            scoped_item = str(row.get("item_id") or "").strip()
            if row.get("match_mode") == "fallback":
                if scoped_item == str(event.item_id):
                    fallbacks.append(row)
                continue
            if not keyword or keyword.casefold() not in event.text.casefold():
                continue
            if scoped_item and scoped_item != str(event.item_id):
                continue
            candidates.append((1 if scoped_item else 0, len(keyword), row))
        fallback = not candidates
        if fallback:
            if not fallbacks:
                return Decision("none", "NO_KEYWORD_MATCH")
            if len(fallbacks) != 1:
                return Decision("blocked", "FALLBACK_REPLY_AMBIGUOUS")
            row = fallbacks[0]
        else:
            candidates.sort(key=lambda value: (value[0], value[1]), reverse=True)
            row = candidates[0][2]
        kind = str(row.get("type") or "text").strip().lower()
        if kind != "text":
            return Decision("blocked", f"UNSUPPORTED_KEYWORD_TYPE:{kind}")
        reply = _safe_format(str(row.get("reply") or ""), event).strip()
        if not reply:
            return Decision("blocked", "EMPTY_KEYWORD_REPLY")
        return Decision(
            "send", "DEFAULT_REPLY" if fallback else "KEYWORD_MATCH", cid=event.cid, recipient_id=event.sender_id,
            text=reply, purpose="default_reply" if fallback else "keyword_reply", item_id=event.item_id,
            metadata={"keyword_key": row.get("_key"), "keyword": row.get("keyword"),
                      "cooldown_seconds": 86400 if fallback else 600},
        )

    async def _delivery_decision(self, event: MessageEvent) -> Decision:
        if event.kind != "paid":
            return Decision("none", "NOT_PAID_EVENT")
        if not event.order_id:
            return Decision("blocked", "ORDER_ID_REQUIRED")
        try:
            order = await self._refresh_order(str(event.order_id))
        except MessagingError as exc:
            return Decision("blocked", exc.code)
        except Exception as exc:
            return Decision("blocked", f"ORDER_REFRESH_FAILED:{getattr(exc, 'code', type(exc).__name__)}")
        order_account = str(order.get("account") or order.get("cookie_id") or "")
        if order_account != self.account:
            return Decision("blocked", "ORDER_ACCOUNT_MISMATCH")
        status = str(order.get("order_status") or "").strip().casefold()
        if status not in PAID_STATUSES:
            return Decision("blocked", f"ORDER_NOT_PENDING_SHIP:{status or 'unknown'}")
        item_id = str(order.get("item_id") or "").strip()
        buyer_id = str(order.get("buyer_id") or "").strip()
        if not item_id or not buyer_id or not event.cid:
            return Decision("blocked", "ORDER_CONTEXT_INCOMPLETE")
        if event.item_id and str(event.item_id) != item_id:
            return Decision("blocked", "EVENT_ITEM_MISMATCH")
        if event.sender_id and str(event.sender_id) not in {buyer_id, self.self_user_id}:
            return Decision("blocked", "EVENT_BUYER_MISMATCH")
        if order.get("sid") and _normalize_cid(order.get("sid")) != _normalize_cid(event.cid):
            return Decision("blocked", "CONVERSATION_MISMATCH")
        product = self._owned_product(item_id)
        if not product:
            return Decision("blocked", "ITEM_NOT_MANAGED")
        with self.store.connect() as db:
            finalizations = db.execute(
                "SELECT status FROM delivery_finalizations WHERE account=? AND order_id=?",
                (self.account, event.order_id),
            ).fetchall()
        terminal = next((
            str(row["status"]).casefold() for row in finalizations
            if str(row["status"]).casefold() in {
                "confirmed", "platform_confirming", "platform_unconfirmed", "finalized",
            }
        ), None)
        if terminal:
            return Decision("none", f"DELIVERY_ALREADY_{terminal.upper()}")
        rule_cards: list[tuple[dict[str, Any], dict[str, Any], tuple[tuple[str, str], ...]]] = []
        title = str(product.get("title") or "").strip().casefold()
        for row in self.store.rows("delivery_rule", self.account):
            if row.get("enabled") in (False, 0):
                continue
            row_item = str(row.get("item_id") or "").strip()
            keyword = str(row.get("keyword") or "").strip()
            if row_item and row_item != item_id:
                continue
            if not row_item and keyword.casefold() not in {title, item_id.casefold()}:
                continue
            card_id = str(row.get("card_id") or "").strip()
            card = self.store.get("card", card_id)
            if not card:
                return Decision("blocked", "DELIVERY_CARD_MISSING")
            card_account = str(card.get("account") or card.get("cookie_id") or self.account)
            if card_account != self.account:
                return Decision("blocked", "DELIVERY_CARD_ACCOUNT_MISMATCH")
            if card.get("enabled") in (False, 0):
                continue
            expected_specs: list[tuple[str, str]] = []
            for number in ("", "_2"):
                name = str(card.get(f"spec_name{number}") or "").strip()
                value = str(card.get(f"spec_value{number}") or "").strip()
                if bool(name) != bool(value):
                    return Decision("blocked", "INCOMPLETE_SPEC_CONFIG")
                if name:
                    expected_specs.append((name, value))
            rule_cards.append((row, card, tuple(expected_specs)))
        if not rule_cards:
            return Decision("blocked", "DELIVERY_RULE_MISSING")
        configured_multi = _truthy(product.get("is_multi_spec")) or any(
            _truthy(card.get("is_multi_spec")) or bool(specs) for _rule, card, specs in rule_cards
        )
        quantity_value = order.get("quantity")
        evidence: OrderDetailEvidence | None = None
        if configured_multi or quantity_value in (None, ""):
            try:
                evidence = await self._order_detail(str(event.order_id), item_id, buyer_id)
                if not evidence:
                    return Decision("blocked", "ORDER_DETAIL_REQUIRED")
                evidence.validate(
                    order_id=str(event.order_id), item_id=item_id, buyer_id=buyer_id,
                    require_specs=configured_multi,
                )
            except OrderDetailError as exc:
                return Decision("blocked", f"{exc.code}:{exc}")
            except Exception as exc:
                return Decision("blocked", f"ORDER_DETAIL_FAILED:{getattr(exc, 'code', type(exc).__name__)}")
        if evidence and quantity_value not in (None, "") and evidence.quantity is not None:
            try:
                if int(quantity_value) != evidence.quantity:
                    return Decision("blocked", "ORDER_QUANTITY_MISMATCH")
            except (TypeError, ValueError):
                return Decision("blocked", "INVALID_DELIVERY_QUANTITY")
        if quantity_value in (None, ""):
            quantity_value = evidence.quantity if evidence else None
            quantity_source = "order_detail"
        else:
            quantity_source = "refreshed_order"
        try:
            quantity = int(quantity_value)
        except (TypeError, ValueError):
            return Decision("blocked", "ORDER_QUANTITY_REQUIRED")
        multi = configured_multi or bool(evidence and evidence.specs)
        observed = {(_normalize_spec(name), _normalize_spec(value)) for name, value in (evidence.specs if evidence else ())}
        selected = []
        for rule, card, expected_specs in rule_cards:
            expected = {(_normalize_spec(name), _normalize_spec(value)) for name, value in expected_specs}
            if multi and expected == observed and expected:
                selected.append((rule, card))
            elif not multi and not expected:
                selected.append((rule, card))
        if not selected:
            return Decision("blocked", "ORDER_SPEC_MISMATCH" if multi else "SPEC_CARD_FOR_SINGLE_ITEM")
        if len(selected) != 1:
            return Decision("blocked", "DELIVERY_RULE_AMBIGUOUS")
        rule, card = selected[0]
        fulfillment = rule.get("fulfillment", "delivery")
        if fulfillment not in {"delivery", "service_intake"} or card.get("fulfillment", "delivery") != fulfillment:
            return Decision("blocked", "FULFILLMENT_MODE_MISMATCH")
        card_id = str(rule.get("card_id") or "").strip()
        card_type = str(card.get("type") or "").strip().lower()
        if card_type != "text":
            return Decision("blocked", f"UNSUPPORTED_CARD_TYPE:{card_type or 'unknown'}")
        try:
            delivery_count = int(rule.get("delivery_count"))
        except (TypeError, ValueError):
            return Decision("blocked", "INVALID_DELIVERY_QUANTITY")
        if delivery_count != 1:
            return Decision("blocked", "UNSUPPORTED_MULTI_QUANTITY")
        if quantity < 1 or quantity > MAX_DELIVERY_UNITS:
            return Decision("blocked", "DELIVERY_QUANTITY_OUT_OF_RANGE")
        if quantity > 1 and not _truthy(product.get("multi_quantity_delivery")):
            return Decision("blocked", "MULTI_QUANTITY_NOT_ENABLED")
        text = str(card.get("text_content") or "").strip()
        if not text:
            return Decision("blocked", "EMPTY_TEXT_CARD")
        if "__IMAGE_SEND__" in text or "{DELIVERY_CONTENT}" in text:
            return Decision("blocked", "UNSUPPORTED_CARD_TEMPLATE")
        return Decision(
            "send", "PAID_SERVICE_INTAKE" if fulfillment == "service_intake" else "PAID_TEXT_DELIVERY",
            cid=event.cid, recipient_id=buyer_id,
            text=text, purpose=fulfillment, item_id=item_id, order_id=str(event.order_id),
            metadata={
                "rule_id": rule.get("id") or rule.get("_key"), "card_id": card_id,
                "total_units": quantity, "quantity_source": quantity_source,
            },
        )

    def _record_block(self, event: MessageEvent, decision: Decision) -> dict[str, Any]:
        key = hashlib.sha256(f"{self.account}:{event.message_id}:{decision.reason}".encode("utf-8")).hexdigest()
        value = {
            "id": key, "account": self.account, "message_id": event.message_id,
            "kind": event.kind, "item_id": event.item_id, "order_id": event.order_id,
            "reason": decision.reason, "status": "blocked", "created_at": now(),
        }
        self.store.put("messaging_block", key, value, account=self.account, source="owned_messaging")
        return value

    def _outbox_id(self, event: MessageEvent, decision: Decision) -> tuple[str, str]:
        payload_hash = hashlib.sha256(str(decision.text).encode("utf-8")).hexdigest()
        trigger_identity = decision.order_id if decision.purpose in {"delivery", "service_intake"} else event.message_id
        material = "|".join((
            self.account, decision.purpose or "", trigger_identity or "",
            decision.order_id or "", str(decision.unit_index), "1", decision.cid or "",
            decision.recipient_id or "", payload_hash,
        ))
        return hashlib.sha256(material.encode("utf-8")).hexdigest(), payload_hash

    async def _dispatch(self, event: MessageEvent, decision: Decision) -> dict[str, Any]:
        total_units = int(decision.metadata.get("total_units") or 1)
        if decision.purpose != "delivery" or total_units == 1:
            return await self._dispatch_unit(event, decision, total_units=1)
        unit_results: list[dict[str, Any]] = []
        for unit_index in range(1, total_units + 1):
            unit_decision = replace(decision, unit_index=unit_index)
            result = await self._dispatch_unit(event, unit_decision, total_units=total_units)
            unit_results.append(result)
            if result.get("status") == "ambiguous" or (
                result.get("status") == "duplicate_suppressed"
                and result.get("state") in {"sending", "sent_unconfirmed", "ambiguous"}
            ):
                break
        statuses = {str(row.get("status")) for row in unit_results}
        if statuses == {"confirmed"} and len(unit_results) == total_units:
            aggregate = "confirmed"
        elif "ambiguous" in statuses:
            aggregate = "ambiguous"
        elif "sent_unconfirmed" in statuses or len(unit_results) < total_units:
            aggregate = "sent_unconfirmed"
        else:
            aggregate = "partial"
        return {
            "status": aggregate,
            "order_id": decision.order_id,
            "total_units": total_units,
            "units": unit_results,
        }

    async def _dispatch_unit(
        self, event: MessageEvent, decision: Decision, *, total_units: int,
    ) -> dict[str, Any]:
        if not self.activation_authorized or not self._enabled():
            raise MessagingError("MESSAGING_DISABLED", "自动消息尚未获得明确启用授权。")
        if decision.purpose in {"delivery", "service_intake"} and decision.order_id:
            order = await self._refresh_order(str(decision.order_id))
            order_account = str(order.get("account") or order.get("cookie_id") or "")
            status = str(order.get("order_status") or "").strip().casefold()
            if order_account != self.account:
                raise MessagingError("ORDER_ACCOUNT_MISMATCH", "发送前订单账号核对失败。")
            if status not in PAID_STATUSES:
                raise MessagingError("ORDER_NOT_PENDING_SHIP", "发送前订单已不再处于待发货状态。")
            if str(order.get("item_id") or "") != str(decision.item_id or ""):
                raise MessagingError("ORDER_ITEM_MISMATCH", "发送前订单商品核对失败。")
            if str(order.get("buyer_id") or "") != str(decision.recipient_id or ""):
                raise MessagingError("ORDER_BUYER_MISMATCH", "发送前订单买家核对失败。")
            if order.get("sid") and _normalize_cid(order.get("sid")) != _normalize_cid(decision.cid):
                raise MessagingError("CONVERSATION_MISMATCH", "发送前订单会话核对失败。")
            refreshed_quantity = order.get("quantity")
            if refreshed_quantity in (None, ""):
                if decision.metadata.get("quantity_source") != "order_detail":
                    raise MessagingError("ORDER_QUANTITY_REQUIRED", "发送前订单缺少明确数量。")
            else:
                try:
                    if int(refreshed_quantity) != int(total_units):
                        raise MessagingError("ORDER_QUANTITY_MISMATCH", "发送前订单数量核对失败。")
                except (TypeError, ValueError):
                    raise MessagingError("ORDER_QUANTITY_REQUIRED", "发送前订单数量无效。")
        outbox_id, payload_hash = self._outbox_id(event, decision)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if decision.purpose == "service_intake":
                previous = db.execute(
                    "SELECT id,state FROM message_outbox WHERE account=? AND purpose='service_intake' AND order_id=? LIMIT 1",
                    (self.account, decision.order_id),
                ).fetchone()
                if previous:
                    return {"status": "duplicate_suppressed", "outbox_id": previous["id"], "state": previous["state"]}
            if decision.purpose in {"keyword_reply", "default_reply"}:
                since = (datetime.now(CHINA) - timedelta(seconds=int(decision.metadata.get("cooldown_seconds", 600)))).isoformat(timespec="seconds")
                query = ("SELECT id FROM message_outbox WHERE account=? AND cid=? AND recipient_id=? AND item_id=? "
                         "AND prepared_at>=?")
                params = [self.account, decision.cid, decision.recipient_id, decision.item_id, since]
                if decision.purpose == "keyword_reply":
                    query += " AND payload_hash=? AND purpose IN ('keyword_reply','default_reply')"
                    params.append(payload_hash)
                if db.execute(query + " LIMIT 1", params).fetchone():
                    return {"status": "no_action", "reason": "REPLY_COOLDOWN"}
            created = db.execute(
                "INSERT OR IGNORE INTO message_outbox(id,account,inbox_message_id,purpose,cid,recipient_id,item_id,order_id,"
                "unit_index,total_units,step_index,payload_hash,text_content,state,prepared_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,'prepared',?)",
                (outbox_id, self.account, event.message_id, decision.purpose, decision.cid,
                 decision.recipient_id, decision.item_id, decision.order_id, decision.unit_index, total_units,
                 payload_hash, decision.text, now()),
            ).rowcount
            row = db.execute("SELECT state FROM message_outbox WHERE id=?", (outbox_id,)).fetchone()
        if not created:
            return {"status": "duplicate_suppressed", "outbox_id": outbox_id, "state": row["state"]}
        with self.store.connect() as db:
            claimed = db.execute(
                "UPDATE message_outbox SET state='sending',attempted_at=? WHERE id=? AND state='prepared'",
                (now(), outbox_id),
            ).rowcount
        if not claimed:
            return {"status": "duplicate_suppressed", "outbox_id": outbox_id}
        try:
            await self.transport.send_text(
                cid=str(decision.cid), recipient_id=str(decision.recipient_id),
                self_user_id=self.self_user_id, text=str(decision.text),
            )
        except Exception as exc:
            with self.store.connect() as db:
                db.execute(
                    "UPDATE message_outbox SET state='ambiguous',last_error=? WHERE id=?",
                    (f"{getattr(exc, 'code', type(exc).__name__)}: {exc}", outbox_id),
                )
            self._set_delivery_state(decision, outbox_id, "ambiguous")
            return {"status": "ambiguous", "outbox_id": outbox_id, "message": "发送结果不确定，禁止自动重发。"}
        with self.store.connect() as db:
            db.execute("UPDATE message_outbox SET state='sent_unconfirmed' WHERE id=?", (outbox_id,))
        self._set_delivery_state(decision, outbox_id, "sent_unconfirmed")
        confirmed = False
        try:
            history = await self.transport.history(str(decision.cid), limit=50)
            confirmed = await self.reconcile_history(history, outbox_ids=[outbox_id]) == 1
        except Exception:
            # A send is never repeated because confirmation was unavailable.
            pass
        return {
            "status": "confirmed" if confirmed else "sent_unconfirmed",
            "outbox_id": outbox_id,
            "message": "已由官方历史确认。" if confirmed else "已尝试发送，等待官方历史核对；不会自动重发。",
        }

    def _set_delivery_state(self, decision: Decision, outbox_id: str, status: str) -> None:
        if decision.purpose != "delivery" or not decision.order_id:
            return
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO delivery_finalizations(account,order_id,unit_index,item_id,buyer_id,status,outbox_id,source,updated_at) "
                "VALUES(?,?,?,?,?,?,?,'owned_messaging',?) ON CONFLICT(account,order_id,unit_index) DO UPDATE SET "
                "status=excluded.status,outbox_id=excluded.outbox_id,updated_at=excluded.updated_at",
                (self.account, decision.order_id, decision.unit_index, decision.item_id,
                 decision.recipient_id, status, outbox_id, now()),
            )

    @staticmethod
    def _platform_confirmation_ok(value: Any, order_id: str) -> bool:
        return bool(
            isinstance(value, dict)
            and value.get("platform_success") is True
            and str(value.get("status") or "").casefold() == "finalized"
            and str(value.get("order_id") or "") == str(order_id)
        )

    @staticmethod
    def _history_identity(message: HistoryMessage) -> str:
        if message.message_id:
            return str(message.message_id)
        material = "|".join((
            _normalize_cid(message.cid), message.sender_id, message.text, str(message.created_ms),
        ))
        return "history-fallback:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    async def _maybe_finalize_order(self, order_id: str) -> bool:
        """Claim platform confirmation only after every expected unit is confirmed."""
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT unit_index,total_units,state,item_id,recipient_id,cid FROM message_outbox "
                "WHERE account=? AND purpose='delivery' AND order_id=? ORDER BY unit_index",
                (self.account, str(order_id)),
            ).fetchall()
            if not rows:
                return False
            expected = max(int(row["total_units"] or 1) for row in rows)
            confirmed_units = {
                int(row["unit_index"]) for row in rows if str(row["state"]) == "confirmed"
            }
            if confirmed_units != set(range(1, expected + 1)):
                return False
            finalizations = db.execute(
                "SELECT unit_index,status FROM delivery_finalizations "
                "WHERE account=? AND order_id=? ORDER BY unit_index",
                (self.account, str(order_id)),
            ).fetchall()
            if {int(row["unit_index"]) for row in finalizations} != set(range(1, expected + 1)):
                return False
            statuses = {str(row["status"]).casefold() for row in finalizations}
            if statuses & {"platform_confirming", "platform_unconfirmed", "finalized"}:
                return False
            if statuses != {"message_confirmed"}:
                return False
            changed = db.execute(
                "UPDATE delivery_finalizations SET status='platform_confirming',updated_at=? "
                "WHERE account=? AND order_id=? AND status='message_confirmed'",
                (now(), self.account, str(order_id)),
            ).rowcount
            if changed != expected:
                return False

        platform_status = "platform_unconfirmed"
        platform_error: str | None = None
        try:
            order = await self._refresh_order(str(order_id))
            first = rows[0]
            order_account = str(order.get("account") or order.get("cookie_id") or "")
            status = str(order.get("order_status") or "").strip().casefold()
            if order_account != self.account:
                raise MessagingError("ORDER_ACCOUNT_MISMATCH", "平台确认前订单账号核对失败。")
            if status not in PAID_STATUSES:
                raise MessagingError("ORDER_NOT_PENDING_SHIP", "平台确认前订单已不再处于待发货状态。")
            if str(order.get("item_id") or "") != str(first["item_id"] or ""):
                raise MessagingError("ORDER_ITEM_MISMATCH", "平台确认前订单商品核对失败。")
            if str(order.get("buyer_id") or "") != str(first["recipient_id"] or ""):
                raise MessagingError("ORDER_BUYER_MISMATCH", "平台确认前订单买家核对失败。")
            if order.get("sid") and _normalize_cid(order.get("sid")) != _normalize_cid(first["cid"]):
                raise MessagingError("CONVERSATION_MISMATCH", "平台确认前订单会话核对失败。")
        except Exception as exc:
            platform_error = f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"
        if platform_error is None and self.confirm_delivery:
            try:
                value = self.confirm_delivery(str(order_id))
                result = await value if isawaitable(value) else value
                if self._platform_confirmation_ok(result, str(order_id)):
                    platform_status = "finalized"
                else:
                    platform_error = "平台确认返回值不具备明确成功证据。"
            except Exception as exc:
                platform_error = f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"
        elif platform_error is None:
            platform_error = "未配置平台确认回调。"
        with self.store.connect() as db:
            db.execute(
                "UPDATE delivery_finalizations SET status=?,updated_at=? "
                "WHERE account=? AND order_id=? AND status='platform_confirming'",
                (platform_status, now(), self.account, str(order_id)),
            )
            if platform_error:
                db.execute(
                    "UPDATE message_outbox SET last_error=? "
                    "WHERE account=? AND purpose='delivery' AND order_id=?",
                    (platform_error, self.account, str(order_id)),
                )
        return platform_status == "finalized"

    async def reconcile_history(
        self, history: Iterable[HistoryMessage], *, outbox_ids: Iterable[str] | None = None
    ) -> int:
        history = list(history)
        params: list[Any] = [self.account]
        query = "SELECT * FROM message_outbox WHERE account=? AND state IN ('sent_unconfirmed','ambiguous')"
        selected = list(outbox_ids or [])
        if selected:
            query += " AND id IN (" + ",".join("?" for _ in selected) + ")"
            params.extend(selected)
        with self.store.connect() as db:
            rows = db.execute(query + " ORDER BY attempted_at,unit_index,id", params).fetchall()
            used_history_ids = {
                str(row["history_message_id"])
                for row in db.execute(
                    "SELECT history_message_id FROM message_outbox "
                    "WHERE account=? AND state='confirmed' AND history_message_id IS NOT NULL",
                    (self.account,),
                ).fetchall()
            }
        ordered_history = sorted(history, key=lambda message: (message.created_ms, self._history_identity(message)))
        confirmed = 0
        touched_orders: set[str] = set()
        for row in rows:
            try:
                attempted = datetime.fromisoformat(row["attempted_at"])
                attempted_ms = int(attempted.timestamp() * 1000)
            except (TypeError, ValueError):
                continue
            matches = [message for message in ordered_history
                       if _normalize_cid(message.cid) == _normalize_cid(row["cid"])
                       and message.sender_id == self.self_user_id
                       and message.text == row["text_content"]
                       and attempted_ms <= message.created_ms <= attempted_ms + 300_000
                       and self._history_identity(message) not in used_history_ids]
            if not matches:
                continue
            match = matches[0]
            history_id = self._history_identity(match)
            with self.store.connect() as db:
                changed = db.execute(
                    "UPDATE message_outbox SET state='confirmed',confirmed_at=?,history_message_id=?,last_error=NULL "
                    "WHERE id=? AND state IN ('sent_unconfirmed','ambiguous')",
                    (now(), history_id, row["id"]),
                ).rowcount
                if changed and row["purpose"] == "delivery" and row["order_id"]:
                    db.execute(
                        "UPDATE delivery_finalizations SET status='message_confirmed',updated_at=? "
                        "WHERE account=? AND order_id=? AND unit_index=? AND outbox_id=?",
                        (now(), self.account, row["order_id"], row["unit_index"], row["id"]),
                    )
            if not changed:
                continue
            used_history_ids.add(history_id)
            if row["purpose"] == "delivery" and row["order_id"]:
                touched_orders.add(str(row["order_id"]))
            confirmed += 1
        for order_id in sorted(touched_orders):
            await self._maybe_finalize_order(order_id)
        return confirmed

    async def reconcile_pending(self, cid: str) -> dict[str, Any]:
        history = await self.transport.history(cid, limit=100)
        return {"confirmed": await self.reconcile_history(history), "cid": _normalize_cid(cid)}

    async def reconcile_all_pending(self) -> dict[str, Any]:
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT DISTINCT cid FROM message_outbox "
                "WHERE account=? AND state IN ('sent_unconfirmed','ambiguous') ORDER BY cid",
                (self.account,),
            ).fetchall()
        confirmed = 0
        checked = 0
        errors: list[dict[str, str]] = []
        for row in rows:
            cid = str(row["cid"])
            try:
                history = await self.transport.history(cid, limit=100)
                confirmed += await self.reconcile_history(history)
                checked += 1
            except Exception as exc:
                errors.append({"cid": cid, "code": getattr(exc, "code", type(exc).__name__)})
        return {"checked": checked, "confirmed": confirmed, "errors": errors}

    def _counts(self, table: str, column: str = "state") -> dict[str, int]:
        with self.store.connect() as db:
            rows = db.execute(
                f"SELECT {column},COUNT(*) AS total FROM {table} WHERE account=? GROUP BY {column}",
                (self.account,),
            ).fetchall()
        return {str(row[column]): int(row["total"]) for row in rows}

    def _config_status(self) -> dict[str, Any]:
        keywords = self.store.rows("keyword", self.account)
        cards = self.store.rows("card", self.account)
        rules = self.store.rows("delivery_rule", self.account)
        unsupported_keywords = sum(1 for row in keywords if row.get("enabled") is not False and str(row.get("type") or "text").lower() != "text")
        unsupported_cards = sum(1 for row in cards if row.get("enabled") is not False and str(row.get("type") or "").lower() != "text")
        return {
            "keywords": len(keywords), "cards": len(cards), "rules": len(rules),
            "unsupported_keywords": unsupported_keywords, "unsupported_cards": unsupported_cards,
            "scope": "text_keyword_and_text_card",
        }

    def status(self) -> dict[str, Any]:
        with self.store.connect() as db:
            runtime = db.execute("SELECT * FROM messaging_runtime WHERE account=?", (self.account,)).fetchone()
        transport_status = self.transport.status() if hasattr(self.transport, "status") else {"ready": False}
        return {
            "account": self.account,
            "enabled": self._enabled(),
            "activated_at": self.store.setting(self.activation_key),
            "activation_authorized": self.activation_authorized,
            "managed_item_ids": sorted(self.managed_item_ids),
            "active": self._active,
            "self_user_id_available": bool(self.self_user_id),
            "runtime": dict(runtime) if runtime else {"state": "inactive"},
            "transport": transport_status,
            "inbox": self._counts("message_inbox"),
            "outbox": self._counts("message_outbox"),
            "finalizations": self._counts("delivery_finalizations", "status"),
            "blocks": len(self.store.rows("messaging_block", self.account)),
            "config": self._config_status(),
            "legacy_finalizations": self.legacy_finalizations,
            "last_error": self._last_error,
        }
