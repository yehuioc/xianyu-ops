"""Owned marketplace adapter with read-only defaults and narrow delivery confirmation.

Protocol and response-field facts were checked against the locally installed
reference project. This module does not import or run that project.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .paths import DEFAULT_CDP
from .store import Store, now, product_key, CHINA

ITEM_DETAIL_API = "mtop.taobao.idle.pc.detail"
EDIT_DETAIL_API = "mtop.idle.pc.idleitem.editDetail"
ITEM_LIST_API = "mtop.idle.web.xyh.item.list"
SEARCH_API = "mtop.taobao.idlemtopsearch.pc.search"
CATEGORY_API = "mtop.taobao.idle.kgraph.property.recommend"
PUBLISH_API = "mtop.idle.pc.idleitem.publish"
EDIT_API = "mtop.idle.pc.idleitem.edit"
READ_APIS = frozenset({
    ITEM_DETAIL_API, EDIT_DETAIL_API, ITEM_LIST_API, SEARCH_API, CATEGORY_API, "mtop.taobao.idle.trade.merchant.sold.get",
})
ORDER_LIST_API = "mtop.taobao.idle.trade.merchant.sold.get"
DELIVERY_API = "mtop.taobao.idle.logistic.consign.dummy"
API_LABELS = {
    "mtop.taobao.idle.pc.detail": "公开商品详情接口",
    "mtop.idle.pc.idleitem.editDetail": "本人商品详情读取",
    "mtop.idle.web.xyh.item.list": "商品列表读取",
    SEARCH_API: "公开市场搜索",
    ORDER_LIST_API: "订单读取",
    DELIVERY_API: "平台发货确认",
    CATEGORY_API: "发布分类推荐",
    PUBLISH_API: "新商品发布",
    EDIT_API: "已有商品库存修改",
}
APP_KEY = "34839810"
TOKEN_ERRORS = frozenset({"FAIL_SYS_TOKEN_EMPTY", "FAIL_SYS_TOKEN_EXPIRED", "FAIL_SYS_TOKEN_EXOIRED", "TOKEN_MISSING"})
COOKIE_URLS = ["https://www.goofish.com", "https://h5api.m.goofish.com"]


def item_list_payload(uid: str, page: int = 1) -> dict:
    return {"needGroupInfo": False, "pageNumber": page, "pageSize": 20, "groupName": "在售",
            "groupId": "58877261", "defaultGroup": True, "userId": uid}


def mtop_params(api: str, version: str, payload: dict, token: str, *,
                spm_cnt: str = "a21ybx.item.0.0", spm_pre: str = "") -> tuple[str, dict]:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    stamp = str(int(time.time() * 1000))
    sign = hashlib.md5(f"{token}&{stamp}&{APP_KEY}&{data}".encode("utf-8")).hexdigest()
    return data, {"jsv": "2.7.2", "appKey": APP_KEY, "t": stamp, "sign": sign, "v": version,
                  "type": "originaljson", "accountSite": "xianyu", "dataType": "json", "timeout": "20000",
                  "api": api, "sessionOption": "AutoLoginOnly", "spm_cnt": spm_cnt, "spm_pre": spm_pre}


class MarketError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def parse_cookie(cookie: str) -> dict[str, str]:
    return {k.strip(): v.strip() for part in cookie.split(";") if "=" in part
            for k, v in [part.split("=", 1)] if k.strip()}


def serialize_cookie(values: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in values.items())


def success(response: Any) -> bool:
    return isinstance(response, dict) and any(str(v).startswith("SUCCESS::") for v in response.get("ret", []))


def platform_time(value: Any) -> str | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        if isinstance(value, (float, int)) or str(value).isdigit():
            stamp = float(value)
            if stamp > 10**12:
                stamp /= 1000
            return datetime.fromtimestamp(stamp, CHINA).isoformat(timespec="seconds")
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=CHINA)
        return parsed.astimezone(CHINA).isoformat(timespec="seconds")
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def normalize_order_status(value: Any) -> str:
    raw = str(value or "unknown")
    aliases = {
        "交易成功": "completed", "交易完成": "completed", "已完成": "completed", "TRADE_FINISHED": "completed",
        "已发货": "shipped", "待收货": "shipped", "WAIT_BUYER_CONFIRM_GOODS": "shipped",
        "待发货": "paid", "买家已付款": "paid", "WAIT_SELLER_SEND_GOODS": "paid",
        "交易关闭": "cancelled", "已取消": "cancelled", "TRADE_CLOSED": "cancelled",
        "待付款": "unpaid", "等待买家付款": "unpaid", "WAIT_BUYER_PAY": "unpaid",
        "退款成功": "refunded", "退款中": "refunding",
    }
    return aliases.get(raw, raw)


def normalize_item_status(value: Any, *, owned: bool = False) -> str:
    """Keep absent and unverified numeric states unknown, including negative codes."""
    if value is None or isinstance(value, bool):
        return "unknown"
    raw = str(value).strip()
    if owned and raw == "0":
        return "在线"
    if raw in {"在线", "在售", "审核中", "审核不通过", "已下架", "下架", "已售出", "售罄", "已删除"}:
        return "在线" if raw == "在售" else raw
    return "unknown"


class MtopClient:
    def __init__(self, store: Store, account: str):
        self.store = store
        self.account = account
        self.cookies = parse_cookie(store.cookie(account))
        self.session: aiohttp.ClientSession | None = None
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35))
        return self

    async def __aexit__(self, *_):
        if self.session:
            await self.session.close()

    @staticmethod
    def is_success_response(response: Any) -> bool:
        return success(response)

    def _auth_state(self, state: str, code: str | None = None) -> None:
        account = self.store.get("account", self.account, {"id": self.account, "label": self.account})
        account["auth_state"] = state
        account["last_check_at"] = now()
        if state == "verified":
            account["last_verified_at"] = now()
            account.pop("error_code", None)
        elif code:
            account["error_code"] = code
        self.store.put("account", self.account, account, account=self.account)

    async def _post_mtop(self, *, api_name: str, version: str = "1.0", payload: dict,
                         spm_cnt: str = "a21ybx.item.0.0", spm_pre: str = "", _refresh_token_once: bool = True,
                         _delivery_authorized: bool = False, _publish_authorized: bool = False,
                         _inventory_authorized: bool = False, _content_authorized: bool = False, **_) -> dict:
        if api_name not in READ_APIS:
            if not ((_delivery_authorized and api_name == DELIVERY_API) or
                    (_publish_authorized and api_name == PUBLISH_API) or
                    ((_inventory_authorized or _content_authorized) and api_name == EDIT_API)):
                raise MarketError("PHONE_UPLOAD_ONLY", "当前操作没有商品写入授权；已有商品编辑仍使用手机素材包。")
        if not self.session:
            raise RuntimeError("MtopClient must be used as an async context manager")
        if self.store.get("account", self.account, {}).get("auth_state") == "verification_required":
            raise MarketError("ACCOUNT_VERIFICATION_REQUIRED", "请先在原浏览器完成人工验证并连接现有登录；未再次请求平台。")
        blocked = self.store.get("api_block", product_key(self.account, api_name))
        if blocked:
            raise MarketError(blocked["code"], f"{API_LABELS.get(api_name, '当前接口')}仍受平台校验限制；未重复请求。")
        token = self.cookies.get("_m_h5_tk", "").split("_", 1)[0]
        if not token:
            self._auth_state("login_required", "TOKEN_MISSING")
            raise MarketError("TOKEN_MISSING", "登录资料缺少有效令牌，请从原浏览器重新连接账号。")
        data, params = mtop_params(api_name, version, payload, token, spm_cnt=spm_cnt, spm_pre=spm_pre)
        seller = api_name == "mtop.taobao.idle.trade.merchant.sold.get"
        origin = "https://seller.goofish.com" if seller else "https://www.goofish.com"
        headers = {"Cookie": serialize_cookie(self.cookies), "Origin": origin, "Referer": origin + "/",
                   "User-Agent": self.user_agent, "Accept": "application/json"}
        if seller:
            headers["idle_site_biz_code"] = "COMMONPRO"
        try:
            async with self.session.post(f"https://h5api.m.goofish.com/h5/{api_name.lower()}/{version}/",
                                         params=params, data={"data": data}, headers=headers, allow_redirects=False) as response:
                if response.status != 200:
                    raise MarketError("HTTP_ERROR", f"平台读取返回 HTTP {response.status}，未重试。")
                rotated = False
                for name, morsel in response.cookies.items():
                    if morsel.value and self.cookies.get(name) != morsel.value:
                        self.cookies[name] = morsel.value
                        rotated = True
                if rotated:
                    self.store.save_cookie(self.account, serialize_cookie(self.cookies))
                result = await response.json(content_type=None)
        except MarketError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise MarketError("NETWORK_ERROR", "平台连接失败，已保留原记录；请查看网络或稍后重试。") from exc
        if not isinstance(result, dict):
            raise MarketError("INVALID_RESPONSE", "平台没有返回可识别的数据。")
        if success(result):
            self._auth_state("verified")
            return result
        codes = [str(v).split("::", 1)[0].split(":", 1)[0][:100] for v in result.get("ret", [])]
        code = codes[0] if codes else "UNKNOWN_RESPONSE"
        refreshed_token = self.cookies.get("_m_h5_tk", "").split("_", 1)[0]
        if (_refresh_token_once and api_name in READ_APIS and code in {"FAIL_SYS_TOKEN_EXPIRED", "FAIL_SYS_TOKEN_EXOIRED"}
                and refreshed_token and refreshed_token != token):
            return await self._post_mtop(api_name=api_name, version=version, payload=payload,
                                        spm_cnt=spm_cnt, spm_pre=spm_pre, _refresh_token_once=False)
        if any("VALIDATE" in v or "RGV" in v or "ILLEGAL_ACCESS" in v for v in codes):
            self.store.put("api_block", product_key(self.account, api_name),
                           {"api": api_name, "label": API_LABELS.get(api_name, "当前接口"),
                            "code": code, "blocked_at": now()}, account=self.account)
            raise MarketError(code, f"{API_LABELS.get(api_name, '当前接口')}被平台要求校验；已暂停该接口，不据此判断整个账号失效。")
        if any("TOKEN" in v or "SESSION" in v or "LOGIN" in v for v in codes):
            self._auth_state("login_required", code)
            raise MarketError(code, "后台保存的登录凭据需要同步；先连接原浏览器现有登录，只有网页也退出时才需重新登录。")
        raise MarketError(code, f"平台暂未提供这项数据（{code}），原记录保持不变。")

    async def refresh_order(self, order_id: str) -> dict:
        result = await self.orders()
        found = None
        for order in result["orders"]:
            existing = self.store.get("order", order["order_id"], {})
            existing.update(order)
            self.store.put("order", order["order_id"], existing, account=self.account, source="goofish_seller_orders")
            if order["order_id"] == str(order_id):
                found = existing
        if not found:
            raise MarketError("ORDER_NOT_OBSERVED", "平台订单列表未找到该订单，未执行交付。")
        return found

    async def confirm_delivery(self, order_id: str, *, explicit_authorization: bool = False) -> dict:
        if not explicit_authorization or not str(order_id).isdigit():
            raise MarketError("DELIVERY_AUTHORIZATION_REQUIRED", "确认发货需要已授权的明确订单。")
        result = await self._post_mtop(api_name="mtop.taobao.idle.logistic.consign.dummy",
                    payload={"orderId": str(order_id), "tradeText": "", "picList": [], "newUnconsign": True},
                    _delivery_authorized=True, _refresh_token_once=False)
        return {"status": "finalized", "order_id": str(order_id), "confirmed_at": now(), "platform_success": success(result)}

    async def products(self, max_pages: int = 10) -> list[dict]:
        uid = self.cookies.get("unb")
        if not uid:
            raise MarketError("ACCOUNT_ID_MISSING", "登录资料中缺少账号标识，无法刷新在售商品。")
        products = []
        seen = set()
        for page in range(1, max_pages + 1):
            response = await self._post_mtop(api_name=ITEM_LIST_API, payload=item_list_payload(uid, page))
            data = response.get("data") or {}
            cards = data.get("cardList") or []
            if not cards:
                break
            added = 0
            for card in cards:
                item = card.get("cardData") or {}
                item_id = str(item.get("id") or "")
                if not item_id or item_id in seen:
                    continue
                seen.add(item_id)
                added += 1
                price = item.get("priceInfo") or {}
                picture = item.get("picInfo") or {}
                products.append({"account": self.account, "item_id": item_id, "title": item.get("title") or "未命名商品",
                                 "price": str(price.get("preText") or "") + str(price.get("price") or ""),
                                 "category_id": item.get("categoryId"), "status": normalize_item_status(item.get("itemStatus"), owned=True),
                                 "status_code": item.get("itemStatus"),
                                 "image_urls": [picture["picUrl"]] if picture.get("picUrl") else [],
                                 "source": "goofish_item_list", "observed_at": now(), "source_updated_at": "unknown"})
            total_pages = data.get("totalPage")
            if not added or (str(total_pages or "").isdigit() and page >= int(total_pages)) or len(cards) < 20:
                break
        return products

    async def orders(self, max_pages: int = 10) -> dict:
        orders: dict[str, dict] = {}
        has_next = False
        for page in range(1, max_pages + 1):
            result = await self._post_mtop(api_name="mtop.taobao.idle.trade.merchant.sold.get", payload={
                "pageNumber": page, "rowsPerPage": 20, "orderIds": "", "queryCode": "ALL", "orderSearchParam": "{}",
            })
            module = (result.get("data") or {}).get("module") or {}
            entries = module.get("items")
            if not isinstance(entries, list):
                raise MarketError("ORDER_SCHEMA_UNKNOWN", "订单响应缺少列表，未将原订单清空。")
            for entry in entries:
                common = entry.get("commonData") or {}
                order_id = str(common.get("orderId") or "")
                if not order_id:
                    continue
                price = entry.get("priceVO") or {}
                buyer = entry.get("buyerInfoVO") or {}
                orders[order_id] = {"order_id": order_id, "item_id": str(common.get("itemId") or ""),
                    "cookie_id": self.account, "account": self.account,
                    "buyer_id": str(buyer.get("buyerId") or "") or None,
                    "order_status": normalize_order_status(common.get("orderStatus")),
                    "amount": price.get("totalPrice") or price.get("confirmFee") or price.get("auctionPrice"),
                    "platform_created_at": platform_time(common.get("createTime")),
                    "platform_paid_at": platform_time(common.get("paySuccessTime")),
                    "platform_completed_at": platform_time(common.get("finishTime")),
                    "source": "goofish_seller_orders", "observed_at": now()}
            has_next = str(module.get("nextPage", "false")).lower() == "true"
            if not entries or not has_next:
                break
        return {"orders": list(orders.values()), "complete": not has_next, "captured_at": now(),
                "source": "goofish_seller_orders", "pages": page}


async def client(options: dict):
    """Narrow compatibility for the existing operations engine's read path."""
    store = Store()
    return store, MtopClient(store, str(options["account"]))


def browser_cookies(context) -> dict[str, str]:
    return {c["name"]: c["value"] for c in context.cookies(COOKIE_URLS) if c.get("value")}


def verify_browser_session(store: Store, account: str, context, uid: str) -> tuple[dict, int]:
    """Use the browser's cookie jar for one read and at most one token renewal.

    A missing MTOP token is not proof of logout. The first normal MTOP request
    can issue a token through Set-Cookie; APIRequestContext updates the same
    browser cookie jar. Neither the IM page nor the user's tabs are reloaded.
    """
    api = ITEM_LIST_API
    payload = item_list_payload(uid)
    blocked = store.get("api_block", product_key(account, api))
    if blocked:
        # An endpoint-specific block must not make it the sole login gate.
        # Use only an existing item previously confirmed through our owner API.
        candidates = [p for p in store.rows("product", account)
                      if p.get("managed") and str(p.get("item_id", "")).isdigit()
                      and (p.get("source") == "goofish_owned_edit_detail"
                           or EDIT_DETAIL_API in (p.get("read_sources") or []))]
        candidates.sort(key=lambda p: (not p.get("watch"), str(p["item_id"])))
        if not candidates or store.get("api_block", product_key(account, EDIT_DETAIL_API)):
            raise MarketError(blocked["code"], "商品列表读取仍受平台校验限制，且没有可用的本人商品核验入口；未重复请求或清除限制。")
        api = EDIT_DETAIL_API
        payload = {"itemId": str(candidates[0]["item_id"])}
    from playwright.sync_api import Error as PlaywrightError
    for attempt in range(2):
        values = browser_cookies(context)
        if values.get("unb") != uid:
            raise MarketError("ACCOUNT_MISMATCH", "浏览器账号已变化，未覆盖原登录资料。")
        token = values.get("_m_h5_tk", "").split("_", 1)[0]
        data, params = mtop_params(api, "1.0", payload, token)
        try:
            response = context.request.post(f"https://h5api.m.goofish.com/h5/{api.lower()}/1.0/",
                params=params, form={"data": data}, headers={"Origin": "https://www.goofish.com",
                "Referer": "https://www.goofish.com/"}, timeout=20_000, max_retries=0, max_redirects=0)
            try:
                if response.status != 200:
                    raise MarketError("HTTP_ERROR", f"登录验证返回 HTTP {response.status}，未重试。")
                result = response.json()
            finally:
                response.dispose()
        except PlaywrightError as exc:
            raise MarketError("BROWSER_SESSION_UNAVAILABLE", "未能连接原浏览器会话，请检查原 Edge 与网络；未清空登录。") from exc
        except (ValueError, TypeError) as exc:
            raise MarketError("INVALID_RESPONSE", "登录验证未返回可识别的数据，未覆盖原凭据。") from exc
        if not isinstance(result, dict):
            raise MarketError("INVALID_RESPONSE", "登录验证未返回可识别的数据，未覆盖原凭据。")
        updated = browser_cookies(context)
        if updated.get("unb") != uid:
            raise MarketError("ACCOUNT_MISMATCH", "浏览器账号已变化，未覆盖原登录资料。")
        if success(result):
            body = result.get("data")
            if not isinstance(body, dict):
                raise MarketError("LOGIN_PROBE_SCHEMA_UNKNOWN", "平台已响应，但本人商品数据结构未通过核对；未宣称连接成功。")
            if api == ITEM_LIST_API:
                if not isinstance(body.get("cardList"), list):
                    raise MarketError("LOGIN_PROBE_SCHEMA_UNKNOWN", "本人商品列表结构未通过核对；未宣称连接成功。")
            else:
                from .listing import owned_item
                item = owned_item(body, payload["itemId"], now(), uid)
                if not item.get("title"):
                    raise MarketError("LOGIN_PROBE_SCHEMA_UNKNOWN", "本人商品详情缺少可核对的标题；未宣称连接成功。")
            if not updated.get("_m_h5_tk"):
                raise MarketError("TOKEN_MISSING", "网页会话存在，但接口令牌尚未就绪；未判定网页退出。")
            return updated, attempt + 1
        codes = [str(v).split("::", 1)[0].split(":", 1)[0][:100] for v in result.get("ret", [])]
        code = codes[0] if codes else "UNKNOWN_RESPONSE"
        if any("VALIDATE" in v or "RGV" in v or "ILLEGAL_ACCESS" in v for v in codes):
            store.put("api_block", product_key(account, api),
                      {"api": api, "label": API_LABELS[api], "code": code, "blocked_at": now()}, account=account)
            raise MarketError(code, f"{API_LABELS[api]}要求平台验证；已暂停该接口，未重试或清空网页登录。")
        new_token = updated.get("_m_h5_tk", "").split("_", 1)[0]
        if attempt == 0 and code in TOKEN_ERRORS and new_token and new_token != token:
            continue
        if code in TOKEN_ERRORS:
            raise MarketError(code, "网页账号仍有登录资料，但接口令牌未能续期；本轮已停止，请稍后再次连接。")
        if any("SESSION_EXPIRED" in v or "LOGIN_REQUIRED" in v for v in codes):
            raise MarketError(code, "平台确认网页会话已过期，请在原 Edge 完成登录后再连接。")
        raise MarketError(code, f"登录验证未通过（{code}），未覆盖原凭据或反复重试。")
    raise MarketError("TOKEN_REFRESH_FAILED", "接口令牌未能续期，本轮已停止。")


def connect_browser_cookie(store: Store, account: str, cdp_url: str = DEFAULT_CDP, *, fresh_only: bool = False) -> dict:
    from playwright.sync_api import sync_playwright, Error as PlaywrightError
    old_values = parse_cookie(store.cookie(account))
    row = store.get("account", account, {"id": account, "label": account})
    old_uid = old_values.get("unb") or row.get("platform_user_id")
    if fresh_only and row.get("auth_state") == "verification_required":
        raise MarketError("ACCOUNT_VERIFICATION_REQUIRED", "账号正在等待人工验证，未自动刷新登录。")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=10_000)
            if not browser.contexts:
                raise MarketError("BROWSER_CONTEXT_MISSING", "原浏览器中没有可用登录会话。")
            candidates = [(c, browser_cookies(c)) for c in browser.contexts]
            matches = [(c, v) for c, v in candidates if v.get("unb") and (not old_uid or v["unb"] == old_uid)]
            if not matches:
                if any(v.get("unb") for _, v in candidates):
                    raise MarketError("ACCOUNT_MISMATCH", "浏览器登录的账号与当前账号不同，未覆盖原登录资料。")
                raise MarketError("LOGIN_REQUIRED", "原浏览器缺少账号登录资料，请在原 Edge 登录后再连接。")
            if len(matches) != 1:
                raise MarketError("BROWSER_CONTEXT_AMBIGUOUS", "原浏览器有多个匹配会话，请保留一个目标账号会话后再连接。")
            context, values = matches[0]
            # A fresh short token cannot repair a server-confirmed expired session.
            auth_keys = (("cookie2", "sgcookie") if row.get("error_code") in {"FAIL_SYS_SESSION_EXPIRED", "SESSION_EXPIRED"}
                         else ("_m_h5_tk", "_m_h5_tk_enc", "cookie2", "sgcookie"))
            changed = any(values.get(k) and values.get(k) != old_values.get(k) for k in auth_keys)
            if fresh_only and not changed and row.get("error_code") not in TOKEN_ERRORS:
                return {"status": "unchanged", "account": account}
            values, attempts = verify_browser_session(store, account, context, values["unb"])
            # Save only after a real authenticated read; never erase unrelated API blocks.
            store.save_cookie(account, serialize_cookie(values))
            row.update({"auth_state": "verified", "credential_imported_at": now(), "last_verified_at": now(),
                        "last_check_at": now(), "platform_user_id": values["unb"]})
            row.pop("user_id", None)
            row.pop("error_code", None)
            store.put("account", account, row, account=account)
            return {"status": "connected", "account": account, "verified": True, "probe_attempts": attempts,
                    "token_renewed": values.get("_m_h5_tk") != old_values.get("_m_h5_tk"),
                    "message": "已同步原浏览器会话，本人商品读取验证成功。"}
            # Context manager disconnects this client, preserving Edge and the IM tab.
    except PlaywrightError as exc:
        raise MarketError("BROWSER_SESSION_UNAVAILABLE", "原 Edge 会话暂时不可连接；请检查浏览器，未清空登录。") from exc
    except MarketError as exc:
        if exc.code in TOKEN_ERRORS or exc.code in {"LOGIN_REQUIRED", "FAIL_SYS_SESSION_EXPIRED", "SESSION_EXPIRED"}:
            row.update({"auth_state": "login_required", "error_code": exc.code, "last_check_at": now()})
            store.put("account", account, row, account=account)
        raise
