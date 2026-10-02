"""Application-owned listing, collection and phone-upload workflow."""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from . import analysis, listing, materials
from .engine import ops
from .marketplace import (MtopClient, MarketError, connect_browser_cookie, parse_cookie,
                         ORDER_LIST_API, DELIVERY_API, ITEM_DETAIL_API, EDIT_DETAIL_API, API_LABELS)
from .migration import migrate
from .paths import PROJECT, DATA, OPS, DEFAULT_ACCOUNT, DEFAULT_ITEM, DEFAULT_CDP
from .store import Store, now, product_key, CHINA

MANAGED_ITEMS = {"2534367850985", "2534016871941", "2534001733981"}


def clean_record(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


class ConsoleService:
    def __init__(self, store: Store | None = None, *, import_legacy: bool = True):
        self.store = store or Store()
        self.import_result = migrate(self.store) if import_legacy else {}
        for account in self.store.rows("account"):
            if self.store.get("credential", account["id"]):
                try:
                    platform_id = parse_cookie(self.store.cookie(account["id"])).get("unb")
                    if platform_id and (account.get("platform_user_id") != platform_id or "user_id" in account):
                        account = clean_record(account)
                        if "user_id" in account:
                            account["legacy_owner_id"] = account.pop("user_id")
                        account["platform_user_id"] = platform_id
                        self.store.put("account", account["id"], account, account=account["id"])
                except ValueError:
                    pass
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xianyu-owned")
        self.submit_lock = threading.Lock()
        self.cdp_url = self.store.setting("cdp_url", DEFAULT_CDP)
        self.messaging_runner = None

    async def configure_messaging(self, account: str, enabled: bool) -> dict:
        from .im_transport import EdgeImTransport
        from .messaging import MessagingRunner
        from .runtime import listener, process_kind
        legacy = listener(8090)
        if enabled and legacy and process_kind(legacy) == "legacy":
            raise ValueError("原后台仍在运行。先完成服务切换，再启用自动消息，避免重复回复或发货。")
        was_enabled = self.store.setting(f"messaging_enabled:{account}", False)
        if self.messaging_runner:
            self.messaging_runner.set_enabled(False)
            await self.messaging_runner.stop()
            self.messaging_runner = None
        if enabled and (not was_enabled or not self.store.setting(f"messaging_activated_at:{account}")):
            self.store.set_setting(f"messaging_activated_at:{account}", now())
        self.store.set_setting(f"messaging_enabled:{account}", bool(enabled))
        if not enabled:
            self.store.set_setting("messaging_state", "paused")
            self.store.set_setting("messaging_start_error", None)
            return self.messaging_status(account)

        async def refresh_order(order_id):
            async with MtopClient(self.store, account) as client:
                return await client.refresh_order(order_id)

        async def confirm_delivery(order_id):
            async with MtopClient(self.store, account) as client:
                return await client.confirm_delivery(order_id, explicit_authorization=True)

        identity = parse_cookie(self.store.cookie(account)).get("unb")
        self.messaging_runner = MessagingRunner(self.store, account,
            EdgeImTransport(self.cdp_url, prepare_page=True), self_user_id=identity,
            managed_item_ids=self.managed_item_ids(account), activation_authorized=True,
            order_refresher=refresh_order, confirm_delivery=confirm_delivery)
        self.messaging_runner.set_enabled(True, explicit_authorization=True)
        try:
            await self.messaging_runner.start()
            self.store.set_setting("messaging_state", "running")
            self.store.set_setting("messaging_start_error", None)
        except Exception as exc:
            self.store.set_setting("messaging_state", "blocked")
            self.store.set_setting("messaging_start_error", {"message": str(exc), "at": now()})
            raise
        return self.messaging_status(account)

    async def messaging_startup(self) -> None:
        if self.store.setting(f"messaging_enabled:{DEFAULT_ACCOUNT}", False):
            try:
                await self.configure_messaging(DEFAULT_ACCOUNT, True)
            except Exception as exc:
                self.store.set_setting("messaging_state", "blocked")
                self.store.set_setting("messaging_start_error", {"message": str(exc), "at": now()})

    async def messaging_shutdown(self) -> None:
        if self.messaging_runner:
            await self.messaging_runner.stop()

    def messaging_status(self, account: str) -> dict:
        result = self.messaging_runner.status() if self.messaging_runner else {
            "enabled": self.store.setting(f"messaging_enabled:{account}", False), "active": False,
            "runtime": {"state": self.store.setting("messaging_state", "not_enabled")},
            "last_error": self.store.setting("messaging_start_error")}
        with self.store.connect() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='message_outbox' AND type='table'").fetchone()
            rows = db.execute("SELECT o.purpose,o.order_id,o.item_id,o.state,o.prepared_at,o.last_error,f.status AS delivery_status "
                              "FROM message_outbox o LEFT JOIN delivery_finalizations f ON f.account=o.account AND f.outbox_id=o.id "
                              "WHERE o.account=? ORDER BY o.prepared_at DESC LIMIT 20", (account,)).fetchall() if exists else []
        result["recent"] = [dict(row) for row in rows]
        result["account_status"] = self.account_status(account)
        result["recent_blocks"] = sorted([clean_record(r) for r in self.store.rows("messaging_block", account)], key=lambda r:r.get("created_at", ""), reverse=True)[:20]
        return result

    def product(self, account: str, item_id: str) -> dict:
        product = self.store.get("product", product_key(account, item_id))
        if not product:
            raise ValueError("未找到该商品")
        return product

    def managed_item_ids(self, account: str) -> set[str]:
        return {str(p["item_id"]) for p in self.store.rows("product", account)
                if p.get("managed", p.get("item_id") in MANAGED_ITEMS)
                and p.get("account", account) == account}

    def focus_item(self, account: str) -> str | None:
        managed = self.managed_item_ids(account)
        chosen = self.store.setting(f"focus_item:{account}")
        if chosen in managed:
            return chosen
        return DEFAULT_ITEM if DEFAULT_ITEM in managed else next(iter(sorted(managed)), None)

    def messaging_health(self, account: str) -> dict:
        status = self.messaging_status(account)
        enabled = bool(status.get("enabled"))
        connected = bool(status.get("active") and status.get("transport", {}).get("ready"))
        account_status = self.account_status(account)
        order_read_ready = account_status.get("can_attempt_order_read", False)
        delivery_ready = account_status.get("can_attempt_delivery", False)
        return {"enabled": enabled, "connected": connected,
                "order_read_ready": order_read_ready,
                "state": "running" if connected and order_read_ready and delivery_ready else "blocked" if enabled else "paused",
                "error": status.get("last_error") or status.get("transport", {}).get("last_error")}

    def products(self, account: str = DEFAULT_ACCOUNT) -> list[dict]:
        rows = [clean_record(r) for r in self.store.rows("product", account)]
        for row in rows:
            row["managed"] = row.get("managed", row["item_id"] in MANAGED_ITEMS)
            row["experiment"] = self.store.get("experiment", product_key(account, row["item_id"]))
            history = analysis.snapshot_history(account, row["item_id"])
            row["latest_metrics"] = history[-1] if history else None
        focus = self.focus_item(account)
        return sorted(rows, key=lambda row: (row["item_id"] != focus, not row["managed"], row["item_id"]))

    def account_status(self, account: str) -> dict:
        row = self.store.get("account", account)
        if not row:
            return {"id": account, "auth_state": "not_connected", "credential_available": False}
        result = {k: row.get(k) for k in ("id", "label", "auth_state", "last_verified_at", "last_check_at", "error_code", "credential_imported_at")}
        result["credential_available"] = bool(self.store.get("credential", account))
        result["can_attempt_read"] = result["credential_available"] and result.get("auth_state") not in ("login_required", "verification_required")
        blocks = self.store.rows("api_block", account)
        result["api_blocks"] = [dict({k: r.get(k) for k in ("api", "code", "blocked_at")},
                                     label=API_LABELS.get(r.get("api"), r.get("label"))) for r in blocks]
        blocked_apis = {r.get("api") for r in blocks}
        result["can_attempt_order_read"] = result["can_attempt_read"] and ORDER_LIST_API not in blocked_apis
        result["can_attempt_delivery"] = result["can_attempt_order_read"] and DELIVERY_API not in blocked_apis
        return result

    def runtime_status(self, account: str) -> dict:
        browser = False
        try:
            with urllib.request.urlopen(self.cdp_url + "/json/version", timeout=2) as response:
                browser = response.status == 200
        except (OSError, TimeoutError):
            pass
        return {"account": self.account_status(account), "browser_ready": browser,
                "cdp_url": self.cdp_url, "backend": "owned", "legacy_http_required": False,
                "messaging": self.messaging_health(account)}

    def detail(self, account: str, item_id: str) -> dict:
        product = self.product(account, item_id)
        history = analysis.snapshot_history(account, item_id)
        experiment = self.store.get("experiment", product_key(account, item_id))
        orders = [clean_record(o) for o in self.store.rows("order", account) if str(o.get("item_id")) == item_id]
        packages = [clean_record(p) for p in self.store.rows("package", account) if p.get("item_id") == item_id]
        packages.sort(key=lambda p: p.get("created_at", ""), reverse=True)
        # UI needs order status/time/value, not private buyer identifiers or chat bodies.
        order_fields = ("order_id", "order_status", "amount", "platform_created_at", "platform_paid_at",
                        "platform_completed_at", "source", "observed_at")
        return {"product": product, "history": history, "experiment": experiment,
                "analysis": analysis.analyse(history, orders, experiment), "packages": packages,
                "orders": [{k: o.get(k) for k in order_fields} for o in orders]}

    def submit(self, action: str, account: str = DEFAULT_ACCOUNT, item_id: str | None = None, **options) -> dict:
        allowed = {"collect", "refresh_catalog", "connect_account", "prepare_bundle", "collect_all", "publish_listing", "reconcile_listing", "update_inventory", "edit_listing", "reconcile_content",
                   "quark_login", "quark_check", "quark_prepare", "quark_reconcile", "quark_bind", "quark_audit", "quark_adopt_share"}
        if action not in allowed:
            raise ValueError("不支持的操作")
        with self.submit_lock:
            pending = next((j for j in self.store.jobs(100) if j["state"] in ("queued", "running") and
                            j["action"] == action and j["account"] == account and j.get("item_id") == item_id), None)
            if pending:
                return pending
            job = self.store.new_job(action, account, item_id)
            self.executor.submit(self._run_job, job, options)
            return job

    def _run_job(self, job: dict, options: dict) -> None:
        self.store.update_job(job["id"], "running")
        try:
            account, item_id = job["account"], job.get("item_id")
            if job["action"] == "collect":
                result = self.collect(account, [item_id])
            elif job["action"] == "collect_all":
                if not self.store.setting("collection_enabled", True):
                    result = {"status": "paused", "message": "你已暂停定期采集；本次没有访问平台。"}
                else:
                    ids = [p["item_id"] for p in self.products(account) if p.get("watch")]
                    result = self.collect(account, ids)
            elif job["action"] == "refresh_catalog":
                result = asyncio.run(self.refresh_catalog(account))
            elif job["action"] == "connect_account":
                result = connect_browser_cookie(self.store, account, self.cdp_url)
            elif job["action"].startswith("quark_"):
                from . import quark
                cli = quark.QuarkCLI()
                if job["action"] == "quark_login":
                    with quark.operation_lock(self.store):
                        cli.install()
                        code = options.get("code")
                        cli.run("login", *(["--token", code] if code else []), timeout=180)
                        result = quark.connection(self.store, cli=cli)
                    result["message"] = "夸克账号已连接" if result["status"] == "connected" else result.get("message")
                elif job["action"] == "quark_check":
                    result = quark.connection(self.store, cli=cli)
                    result["message"] = "夸克连接已核对" if result["status"] == "connected" else result.get("message")
                elif job["action"] == "quark_audit":
                    audit = quark.audit_links(self.store, account, cli=cli)
                    result = {"status": "complete" if all(r["status"] == "accessible" for r in audit["links"]) else "partial",
                              "audit": audit, "message": "已有夸克发货链接已检查，请查看可访问性结果"}
                else:
                    if job["action"] == "quark_bind":
                        record = quark.bind(self.store, account, item_id, options["sha256"], cli=cli)
                        if self.messaging_runner and self.messaging_runner.account == account:
                            self.messaging_runner.managed_item_ids = frozenset(self.managed_item_ids(account))
                    elif job["action"] == "quark_adopt_share":
                        record = quark.adopt_share(self.store, account, item_id, options["url"], cli=cli)
                    else:
                        record = quark.prepare(self.store, account, item_id, cli=cli, reconcile_only=job["action"] == "quark_reconcile")
                    result = {"status": "complete", "delivery": record, "message": record["message"]}
            elif job["action"] in {"edit_listing", "reconcile_content"}:
                from . import listing_edits
                edit = asyncio.run(listing_edits.apply(self.store, account, item_id, **options) if job["action"] == "edit_listing"
                                   else listing_edits.reconcile(self.store, account, item_id))
                result = {"status": "complete" if edit["state"] == "verified" else "partial",
                          "edit": listing_edits.public_result(edit), "message": edit["message"]}
                if edit["state"] == "verified":
                    from .support import refresh_product
                    result["support"] = refresh_product(self.store, account, item_id)
            elif job["action"] in {"publish_listing", "reconcile_listing", "update_inventory"}:
                from . import publishing
                if job["action"] == "publish_listing":
                    publication = asyncio.run(publishing.publish(self.store, account, item_id, **options))
                elif job["action"] == "update_inventory":
                    publication = asyncio.run(publishing.update_inventory(self.store, account, item_id, **options))
                else:
                    publication = asyncio.run(publishing.reconcile(self.store, account, item_id))
                result = {"status": "complete" if publication["state"] == "published" else "partial",
                          "publication": publication, "message": publication["message"]}
                if job["action"] == "update_inventory" and publication.get("inventory_update", {}).get("state") != "verified":
                    result.update(status="partial", message=publication.get("inventory_update", {}).get("message", "库存待核对"))
            else:
                product = self.product(account, item_id)
                package = materials.prepare_bundle(self.store, product, title=options.get("title"), description=options.get("description"))
                result = {"status": "ready", "package_id": package["package_id"], "message": "手机素材包已准备好。"}
            state = "partial" if result.get("status") in ("partial", "blocked") else "succeeded"
            self.store.update_job(job["id"], state, result=result)
        except MarketError as exc:
            self.store.update_job(job["id"], "blocked", error=str(exc), result={"code": exc.code})
        except Exception as exc:
            # Unexpected failures remain visible, without serializing account payloads.
            message = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else f"操作未完成（{type(exc).__name__}），已保留原数据。"
            self.store.update_job(job["id"], "failed", error=message)

    async def refresh_catalog(self, account: str) -> dict:
        async with MtopClient(self.store, account) as client:
            products = await client.products()
        for incoming in products:
            key = product_key(account, incoming["item_id"])
            previous = self.store.get("product", key, {})
            previous.update(incoming)
            previous.setdefault("watch", incoming["item_id"] == DEFAULT_ITEM)
            previous.setdefault("managed", incoming["item_id"] in MANAGED_ITEMS)
            previous.setdefault("slug", "dsh-orangebook" if incoming["item_id"] == DEFAULT_ITEM else None)
            self.store.put("product", key, previous, account=account, source="goofish_item_list")
        self.store.set_setting(f"catalog_refreshed:{account}", now())
        return {"status": "updated", "products": len(products), "message": "在售商品已从平台刷新，历史导入记录保留。"}

    async def _read_platform(self, account: str, item_ids: list[str]) -> dict:
        result = {"items": {}, "errors": {}, "source_warnings": {}, "orders": None, "stop": False}
        async with MtopClient(self.store, account) as client:
            for item_id in item_ids:
                reads, failures = {}, []
                for api_name in (ITEM_DETAIL_API, EDIT_DETAIL_API):
                    try:
                        response = await client._post_mtop(api_name=api_name, payload={"itemId": item_id})
                        body = response.get("data") or {}
                        reads[api_name] = (listing.public_item(body, item_id, now()) if api_name == ITEM_DETAIL_API
                                          else listing.owned_item(body, item_id, now(), client.cookies.get("unb")))
                    except MarketError as exc:
                        failures.append({"api": api_name, "code": exc.code, "message": str(exc)})
                        if self.account_status(account).get("auth_state") in ("verification_required", "login_required"):
                            result["stop"] = True
                            break
                current = listing.combine_item_reads(reads.get(ITEM_DETAIL_API), reads.get(EDIT_DETAIL_API))
                if current:
                    current.setdefault("read_sources", list(reads))
                    current["read_warnings"] = failures
                    if EDIT_DETAIL_API not in reads:
                        current["edit_detail_status"] = "unavailable"
                    result["items"][item_id] = current
                    if failures:
                        result["source_warnings"][item_id] = failures
                else:
                    result["errors"][item_id] = {
                        "code": "LISTING_READ_UNAVAILABLE", "message": "公开详情与本人商品详情均未取得可用数据。",
                        "sources": failures,
                    }
                if result["stop"]:
                    break
            if not result["stop"]:
                try:
                    orders = await client.orders()
                    for order in orders["orders"]:
                        previous = self.store.get("order", order["order_id"], {})
                        previous.update(order)
                        self.store.put("order", order["order_id"], previous, account=account, source="goofish_seller_orders")
                    result["orders"] = {k: v for k, v in orders.items() if k != "orders"}
                    result["orders"]["status"] = "observed"
                    self.store.set_setting(f"orders_coverage:{account}", result["orders"])
                except MarketError as exc:
                    result["order_error"] = {"code": exc.code, "message": str(exc)}
                    if self.account_status(account).get("auth_state") in ("verification_required", "login_required"):
                        result["stop"] = True
        return result

    def collect(self, account: str, item_ids: list[str]) -> dict:
        item_ids = list(dict.fromkeys(str(v) for v in item_ids if v))
        if not item_ids:
            return {"status": "empty", "message": "还没有加入观察的商品。"}
        for item_id in item_ids:
            self.product(account, item_id)
        status = self.account_status(account)
        captured_at = now()
        recovery_attempted = False
        recovery_error = None
        if status.get("auth_state") == "login_required":
            recovery_attempted = True
            try:
                connect_browser_cookie(self.store, account, self.cdp_url, fresh_only=True)
                status = self.account_status(account)
            except MarketError as exc:
                recovery_error = {"code": exc.code, "message": str(exc)}
            except Exception:
                recovery_error = {"code": "BROWSER_SESSION_UNAVAILABLE", "message": "原浏览器会话暂时不可连接；未清空登录。"}
        if not status.get("can_attempt_read"):
            error = recovery_error or {"code": "ACCOUNT_ACTION_REQUIRED", "message": "后台登录需要同步或平台验证；请连接原浏览器现有登录。"}
            result = {"items": {}, "errors": {item_id: error for item_id in item_ids}, "orders": None, "stop": True}
        else:
            result = asyncio.run(self._read_platform(account, item_ids))
            # One recovery from a changed original-browser session, never from a CAPTCHA/verification challenge.
            if not recovery_attempted and result["stop"] and self.account_status(account).get("auth_state") == "login_required":
                recovery_attempted = True
                try:
                    if connect_browser_cookie(self.store, account, self.cdp_url, fresh_only=True).get("status") == "connected":
                        result = asyncio.run(self._read_platform(account, item_ids))
                except MarketError as exc:
                    result["errors"]["browser_session"] = {"code": exc.code, "message": str(exc)}
                except Exception:
                    result["errors"]["browser_session"] = {"code": "BROWSER_SESSION_UNAVAILABLE", "message": "原浏览器会话暂时不可连接；未清空登录。"}
        if result["stop"]:
            public = [{"item_id": i, "source": "public_item_detail_page", "status": "unavailable",
                       "browse": None, "want": None, "captured_at": now(), "notes": "平台要求账号动作，本轮未追加页面请求。"} for i in item_ids]
        else:
            online_ids = [i for i in item_ids if result["items"].get(i, {}).get("status") in ops().ONLINE_STATUS
                          and not result["items"][i].get("source_conflicts")]
            public = ops().public_metrics(online_ids, self.cdp_url, expected_items=result["items"]) if online_ids else []
            public.extend({"item_id": i, "source": "public_item_detail_page", "status": "unavailable",
                           "browse": None, "want": None, "captured_at": now(),
                           "notes": "目标商品未核实为在线，不采集页面推荐商品的读数。"}
                          for i in item_ids if i not in online_ids)
        for row in public:
            current = result["items"].get(row["item_id"])
            if current is not None:
                current["buyer_page_evidence"] = row.get("item_evidence", {"match": False})
        snapshot = {"schema": "xianyu-live-snapshot-v3", "captured_at": captured_at, "account": account,
                    "item_ids": item_ids, "runtime": {"backend": "owned", "edge_cdp": self.cdp_url},
                    "items": {}, "public_detail_metrics": public, "orders": {},
                    "order_coverage": result["orders"] or {"status": "unavailable", "error": result.get("order_error")},
                    "exposure": {"value": "unavailable", "source": "seller_backend", "status": "unavailable"},
                    "source_status": {"platform_items": "ok" if len(result["items"]) == len(item_ids) else "partial",
                                      "public_detail": "ok" if all(r.get("status") == "observed" for r in public) else "partial",
                                      "orders": "ok" if result["orders"] and result["orders"].get("complete") else "partial"}}
        changes = []
        for item_id in item_ids:
            product = self.product(account, item_id)
            current = result["items"].get(item_id)
            if current:
                product.update({k: current.get(k) for k in ("title", "description", "status", "status_code", "image_urls", "price_cents", "quantity", "category_id", "skus", "editable_invariants", "field_state", "field_sources", "title_desc_separate", "read_sources", "read_warnings", "source_conflicts", "buyer_page_evidence")})
                cents = current.get("price_cents")
                try:
                    product["price"] = f"¥{int(cents) / 100:.2f}" if cents is not None else None
                except (TypeError, ValueError):
                    product["price"] = None
                product.update({"source": current["source"], "observed_at": current["observed_at"], "source_updated_at": "unknown"})
                self.store.put("product", product_key(account, item_id), product, account=account, source=current["source"])
                snapshot["items"][item_id] = {k: product.get(k) for k in ("item_id", "title", "price", "status", "status_code", "source", "observed_at", "source_updated_at", "read_sources", "read_warnings", "source_conflicts")}
                changes.append(self.observe_package(account, item_id, current))
            else:
                snapshot["items"][item_id] = {"item_id": item_id, "status": "unavailable", "source": "goofish_mtop_detail"}
            orders = [o for o in self.store.rows("order", account) if str(o.get("item_id")) == item_id]
            fresh = bool(result["orders"])
            relevant = [o for o in orders if not fresh or o.get("source") == "goofish_seller_orders"]
            snapshot["orders"][item_id] = {"all_records": len(relevant), "statuses": dict(Counter(str(o.get("order_status", "unknown")) for o in relevant)),
                                          "source": "goofish_seller_orders" if fresh else "owned_historical_orders",
                                          "captured_at": captured_at, "status": "observed" if fresh else "historical_only",
                                          "source_updated_at": result["orders"].get("captured_at") if fresh else "unknown"}
        snapshot["status"] = "complete" if all(v == "ok" for v in snapshot["source_status"].values()) else "partial"
        if result["errors"]:
            snapshot["collection_error"] = result["errors"]
        directory = OPS / "snapshots"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(CHINA).strftime("%Y%m%d-%H%M%S-%f")
        path = directory / f"{stamp}-{account}-snapshot.json"
        ops().atomic_json(path, snapshot)
        self.store.set_setting(f"last_collection:{account}", {"at": captured_at, "status": snapshot["status"], "file": str(path.relative_to(PROJECT))})
        reviews = []
        for item_id in item_ids:
            detail = self.detail(account, item_id)
            summary = detail["analysis"]
            reviews.append({"item_id": item_id, "state": summary["state"], "heading": summary["heading"],
                            "recommendation": summary["recommendation"], "review_ready": summary["review_ready"]})
            experiment = detail["experiment"]
            if experiment and summary["review_ready"] and not experiment.get("review_completed_at"):
                experiment["review_completed_at"] = now()
                self.store.put("experiment", product_key(account, item_id), experiment, account=account)
                self.store.put("review", experiment["package_id"], summary, account=account)
        messaging = self.messaging_health(account)
        return {"status": snapshot["status"], "captured_at": captured_at, "snapshot": str(path.relative_to(PROJECT)),
                "item_ids": item_ids, "changes": changes, "reviews": reviews,
                "errors": result["errors"], "order_error": result.get("order_error"),
                "source_warnings": result.get("source_warnings", {}),
                "messaging": messaging,
                "warnings": ["自动执行尚有连接或账号验证阻塞，请检查交付与回复页。"]
                if messaging["enabled"] and messaging["state"] == "blocked" else []}

    def observe_package(self, account: str, item_id: str, current: dict) -> dict:
        key = product_key(account, item_id)
        experiment = self.store.get("experiment", key)
        if not experiment:
            return {"item_id": item_id, "state": "no_package"}
        package = self.store.get("package", experiment["package_id"])
        if not package:
            return {"item_id": item_id, "state": "package_missing"}
        desired = package["desired"]
        copy_match = materials.compare_listing_copy(current, desired)
        title_match, description_match = copy_match["title"], copy_match["description"]
        checks = {"title": title_match, "description": description_match, "image": False,
                  "online": None if current.get("status") in (None, "unknown") else current.get("status") in ops().ONLINE_STATUS,
                  "sources_agree": not bool(current.get("source_conflicts")),
                  "price": None, "category": None, "sku": "unavailable" if current.get("skus") is None else "observed"}
        checks["buyer_page"] = (ITEM_DETAIL_API in current.get("read_sources", [])
                                or current.get("source") == "goofish_mtop_detail"
                                or current.get("buyer_page_evidence", {}).get("match") is True)
        baseline = package["baseline"]
        if baseline.get("skus") is not None and current.get("skus") is not None:
            checks["sku"] = baseline["skus"] == current["skus"]
        for field, name in (("price_cents", "price"), ("category_id", "category")):
            if current.get(field) is not None and baseline.get(field) is not None:
                checks[name] = str(current[field]) == str(baseline[field])
        image = {"match": False, "method": "text_not_live"}
        if title_match and description_match and current.get("image_urls"):
            image = materials.online_image_check(self.store, package, current["image_urls"][0])
            checks["image"] = image["match"]
        previous_state = experiment.get("state")
        if all(checks[k] is True for k in ("title", "description", "image", "price", "category", "online", "sources_agree", "buyer_page")) and checks["sku"] is not False:
            experiment["state"] = "observing"
            # This timestamp certifies the buyer-facing content, not invisible SKU fields.
            if not experiment.get("content_live_at") or experiment.get("content_ended_at"):
                if experiment.get("content_ended_at"):
                    self.store.put("experiment_segment", experiment["package_id"] + ":" + experiment["content_live_at"], experiment, account=account)
                experiment["content_live_at"] = current["observed_at"]
                experiment.pop("content_ended_at", None)
                experiment["review_completed_at"] = None
                for rule in self.store.rows("delivery_rule", account):
                    if rule.get("keyword") == baseline.get("title"):
                        rule = clean_record(rule)
                        rule["keyword"] = desired["title"]
                        rule["item_id"] = item_id
                        self.store.put("delivery_rule", str(rule["id"]), rule, account=account)
        elif (checks["online"] is False and not experiment.get("content_live_at")
              and self.product(account, item_id).get("user_reported_status", {}).get("status") == "审核中"):
            experiment["state"] = "pending_review"
        elif checks["price"] is False or checks["category"] is False or checks["sku"] is False or checks["online"] is False or checks["sources_agree"] is False:
            experiment["state"] = "needs_attention"
        elif checks["online"] is None:
            experiment["state"] = "status_unknown"
        elif (not checks["buyer_page"] or any(current.get(k) is None for k in ("title", "description", "image_urls"))
              or checks["price"] is None or checks["category"] is None
              or image.get("method") in {"image_read_unavailable", "unsupported_image_host", "unreadable_image", "bundle_image_missing"}):
            experiment["state"] = "evidence_incomplete"
        elif experiment.get("content_live_at"):
            experiment["state"] = "content_changed"
        elif title_match and description_match:
            experiment["state"] = "checking_image"
        else:
            experiment["state"] = "awaiting_phone"
        if experiment.get("content_live_at") and experiment["state"] in {"needs_attention", "content_changed"}:
            experiment.setdefault("content_ended_at", current["observed_at"])
        experiment["verification"] = {"checked_at": now(), "checks": checks, "image": image,
                                      "source": current.get("source"), "status_code": current.get("status_code"),
                                      "copy_match": copy_match, "source_conflicts": current.get("source_conflicts", {}),
                                      "buyer_page_evidence": current.get("buyer_page_evidence"),
                                      "observed_stock": current.get("editable_invariants", {}).get("quantity"),
                                      "sku_note": "接口未返回完整规格时，保留未知；本流程只确认手机上传的内容，不宣称规格已被完整核验。"}
        self.store.put("experiment", key, experiment, account=account)
        return {"item_id": item_id, "state": experiment["state"], "changed": previous_state != experiment["state"],
                "content_live_at": experiment.get("content_live_at"), "checks": checks}

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
