from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from console import analysis, app as app_module, materials, migration
from console.app import create_app
from console.marketplace import MarketError, MtopClient
from console.service import ConsoleService
from console.store import CHINA, Store, product_key


PROJECT = Path(__file__).resolve().parents[1]
TEST_TMP_ROOT = PROJECT / "data" / "test-tmp"


class _CapturingExecutor:
    """Executor double that records work without running platform-facing jobs."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, tuple, dict]] = []

    def submit(self, function, *args, **kwargs):
        self.calls.append((function, args, kwargs))
        return object()

    def shutdown(self, **_kwargs) -> None:
        return None


class _FakeMtopResponse:
    def __init__(self, result: dict, *, cookies: dict[str, str] | None = None) -> None:
        self.status = 200
        self._result = result
        self.cookies = SimpleCookie()
        for name, value in (cookies or {}).items():
            self.cookies[name] = value

    async def json(self, **_kwargs):
        return self._result


class _FakeResponseContext:
    def __init__(self, response: _FakeMtopResponse) -> None:
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return None


class _FakeMtopSession:
    def __init__(self, responses: list[_FakeMtopResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected extra platform call")
        return _FakeResponseContext(self.responses.pop(0))


def _replace_executor(service: ConsoleService) -> _CapturingExecutor:
    service.executor.shutdown(wait=False, cancel_futures=True)
    executor = _CapturingExecutor()
    service.executor = executor
    return executor


def _metric(at: datetime, browse, want=0, *, complete_orders: bool = False) -> dict:
    return {
        "captured_at": at.isoformat(),
        "file": at.strftime("%H%M") + "-snapshot.json",
        "status": "complete",
        "browse": browse,
        "want": want,
        "metric_source": "public_item_detail_page",
        "metric_status": "observed",
        "order_coverage": {
            "status": "observed" if complete_orders else "unavailable",
            "complete": complete_orders,
        },
    }


class OwnedConsoleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT, prefix="owned-console-")
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_store(self, name: str = "console.sqlite3") -> Store:
        return Store(self.root / name)

    def make_service(self, store: Store | None = None) -> ConsoleService:
        return ConsoleService(store or self.make_store(), import_legacy=False)


class MessagingHealthTests(OwnedConsoleTestCase):
    def test_listing_verification_does_not_block_ready_order_delivery(self) -> None:
        from console.marketplace import ORDER_LIST_API
        store = self.make_store()
        store.put("account", "acct", {"id": "acct", "auth_state": "verified"}, account="acct")
        store.save_cookie("acct", "unb=fixture; _m_h5_tk=fixture_1")
        listing_api = "mtop.taobao.idle.pc.detail"
        store.put("api_block", product_key("acct", listing_api),
                  {"api": listing_api, "code": "FAIL_SYS_USER_VALIDATE"}, account="acct")
        service = self.make_service(store)
        try:
            with patch.object(service, "messaging_status", return_value={
                "enabled": True, "active": True, "transport": {"ready": True},
            }):
                self.assertEqual(service.messaging_health("acct")["state"], "running")
                store.put("api_block", product_key("acct", ORDER_LIST_API),
                          {"api": ORDER_LIST_API, "code": "FAIL_SYS_USER_VALIDATE"}, account="acct")
                self.assertEqual(service.messaging_health("acct")["state"], "blocked")
        finally:
            service.close()

    def test_new_authorized_product_is_managed_without_admitting_unconfigured_items(self) -> None:
        from console.messaging import MessagingRunner
        store = self.make_store()
        service = self.make_service(store)
        try:
            for account, item, managed in (("acct", "new", True), ("acct", "unconfigured", False),
                                           ("other", "foreign", True)):
                store.put("product", product_key(account, item),
                          {"account": account, "item_id": item, "managed": managed}, account=account)
            store.set_setting("focus_item:acct", "new")
            runner = MessagingRunner(store, "acct", object(), managed_item_ids=service.managed_item_ids("acct"))
            self.assertIsNotNone(runner._owned_product("new"))
            self.assertIsNone(runner._owned_product("unconfigured"))
            self.assertIsNone(runner._owned_product("foreign"))
            self.assertEqual(service.focus_item("acct"), "new")
            self.assertEqual(service.products("acct")[0]["item_id"], "new")
            self.assertEqual(MessagingRunner(store, "acct", object(), managed_item_ids=set()).managed_item_ids, frozenset())
            store.set_setting("focus_item:acct", "foreign")
            self.assertEqual(service.focus_item("acct"), "new")
        finally:
            service.close()

    def test_deleted_listing_metrics_cannot_become_observation_evidence(self) -> None:
        at = datetime.now(CHINA).isoformat()
        raw = {"account": "acct", "item_ids": ["new"], "captured_at": at,
               "items": {"new": {"status": "已删除"}},
               "public_detail_metrics": [{"item_id": "new", "browse": 3, "want": 677,
                   "source": "public_item_detail_page", "status": "observed"}]}
        (self.root / "removed-snapshot.json").write_text(json.dumps(raw), encoding="utf-8")
        rows = analysis.snapshot_history("acct", "new", self.root)
        self.assertEqual(rows[0]["metric_status"], "unavailable")
        self.assertIsNone(rows[0]["browse"])
        self.assertIsNone(analysis.analyse(rows, [], None)["latest_valid"])

    def test_dead_worker_is_reported_blocked_despite_a_saved_running_state(self) -> None:
        store = self.make_store()
        store.set_setting("messaging_state", "running")
        service = self.make_service(store)
        failure = {"code": "DECODE_FAILED", "message": "worker stopped"}
        try:
            with patch.object(service, "messaging_status", return_value={
                "enabled": True, "active": False,
                "transport": {"ready": False}, "last_error": failure,
            }):
                health = service.messaging_health("acct")
            self.assertEqual(health["state"], "blocked")
            self.assertTrue(health["enabled"])
            self.assertFalse(health["connected"])
            self.assertEqual(health["error"], failure)
        finally:
            service.close()


class WebBoundaryTests(OwnedConsoleTestCase):
    def test_windows_javascript_mime_is_explicit_and_csrf_boundary_checks_host_and_origin(self) -> None:
        store = self.make_store()
        store.put("account", "acct", {"id": "acct", "auth_state": "unchecked"}, account="acct")
        service = self.make_service(store)

        with TestClient(create_app(service)) as client:
            script = client.get("/static/app.js")
            self.assertEqual(script.status_code, 200)
            self.assertEqual(script.headers["content-type"].split(";", 1)[0], "text/javascript")
            self.assertEqual(script.headers["x-content-type-options"], "nosniff")

            bad_host = client.get("/health", headers={"Host": "testserver.attacker.invalid"})
            self.assertEqual(bad_host.status_code, 403)

            cross_site = client.post(
                "/api/settings/collection",
                json={"enabled": False},
                headers={"Origin": "http://attacker.invalid"},
            )
            self.assertEqual(cross_site.status_code, 403)

            wrong_port = client.post(
                "/api/settings/collection",
                json={"enabled": False},
                headers={"Origin": "http://testserver:9999"},
            )
            self.assertEqual(wrong_port.status_code, 403)

            malformed_length = client.post(
                "/api/settings/collection",
                json={"enabled": False},
                headers={"Origin": "http://testserver", "Content-Length": "not-a-number"},
            )
            self.assertEqual(malformed_length.status_code, 400)

            negative_length = client.post(
                "/api/settings/collection",
                json={"enabled": False},
                headers={"Origin": "http://testserver", "Content-Length": "-1"},
            )
            self.assertEqual(negative_length.status_code, 400)

            same_origin = client.post(
                "/api/settings/collection",
                json={"enabled": False},
                headers={"Origin": "http://testserver"},
            )
            self.assertEqual(same_origin.status_code, 200)
            self.assertFalse(same_origin.json()["enabled"])

    def test_text_config_edits_persist_audit_and_reject_immutable_or_cross_account_changes(self) -> None:
        path = self.root / "config.sqlite3"
        store = Store(path)
        for account in ("acct", "other"):
            store.put("account", account, {"id": account, "auth_state": "unchecked"}, account=account)
        card_before = {
            "id": "card-public-id",
            "name": "old card",
            "type": "text",
            "enabled": True,
            "text_content": "old card text",
            "item_id": "item-1",
            "spec_name": "edition",
            "spec_value": "original",
        }
        keyword_before = {
            "id": "keyword-public-id",
            "keyword": "old keyword",
            "reply": "old reply",
            "enabled": True,
            "type": "text",
            "item_id": "item-1",
            "card_id": "card-public-id",
            "cookie_id": "acct",
        }
        store.put("card", "card-row", card_before, account="acct", source="legacy_import")
        store.put("keyword", "keyword-row", keyword_before, account="acct", source="legacy_import")
        service = self.make_service(store)

        card_edit = {"name": "new card", "text_content": "new card text", "enabled": False}
        keyword_edit = {"keyword": "new keyword", "reply": "new reply", "enabled": False}
        with TestClient(create_app(service)) as client:
            self.assertEqual(
                client.put("/api/config/card/card-row?account=acct", json=card_edit).status_code,
                200,
            )
            self.assertEqual(
                client.put("/api/config/keyword/keyword-row?account=acct", json=keyword_edit).status_code,
                200,
            )

            injected_card = client.put(
                "/api/config/card/card-row?account=acct",
                json={**card_edit, "item_id": "attacker-item", "spec_name": "attacker-spec"},
            )
            injected_keyword = client.put(
                "/api/config/keyword/keyword-row?account=acct",
                json={**keyword_edit, "item_id": "attacker-item", "card_id": "attacker-card"},
            )
            wrong_account = client.put(
                "/api/config/card/card-row?account=other",
                json={"name": "cross-account", "text_content": "cross-account", "enabled": True},
            )
            self.assertEqual(injected_card.status_code, 400)
            self.assertEqual(injected_keyword.status_code, 400)
            self.assertEqual(wrong_account.status_code, 404)

        persisted = Store(path)
        card_after = persisted.get("card", "card-row")
        keyword_after = persisted.get("keyword", "keyword-row")
        self.assertEqual({field: card_after[field] for field in card_edit}, card_edit)
        self.assertEqual({field: keyword_after[field] for field in keyword_edit}, keyword_edit)
        self.assertEqual(card_after["id"], card_before["id"])
        self.assertEqual(card_after["item_id"], card_before["item_id"])
        self.assertEqual(card_after["spec_name"], card_before["spec_name"])
        self.assertEqual(card_after["spec_value"], card_before["spec_value"])
        self.assertEqual(keyword_after["id"], keyword_before["id"])
        self.assertEqual(keyword_after["item_id"], keyword_before["item_id"])
        self.assertEqual(keyword_after["card_id"], keyword_before["card_id"])

        changes = persisted.rows("config_change", "acct")
        self.assertEqual(len(changes), 2)
        by_kind = {change["kind"]: change for change in changes}
        self.assertEqual(by_kind["card"]["key"], "card-row")
        self.assertEqual(by_kind["card"]["before"], card_before)
        self.assertEqual(by_kind["keyword"]["key"], "keyword-row")
        self.assertEqual(by_kind["keyword"]["before"], keyword_before)
        for kind, after in (("card", card_after), ("keyword", keyword_after)):
            audited_after = by_kind[kind]["after"]
            self.assertEqual(audited_after, after)
            self.assertEqual(by_kind[kind]["changed_at"], audited_after["updated_at"])


class AnalysisBoundaryTests(OwnedConsoleTestCase):
    def test_unknown_metrics_are_not_converted_to_zero(self) -> None:
        live = datetime(2026, 9, 1, 8, tzinfo=CHINA)
        experiment = {"content_live_at": live.isoformat(), "state": "observing"}
        at = live + timedelta(hours=74)

        unknown = analysis.analyse(
            [_metric(live, None, None), _metric(live + timedelta(hours=73), None, None)],
            [],
            experiment,
            at=at,
        )
        zero = analysis.analyse(
            [_metric(live, 0, 0), _metric(live + timedelta(hours=73), 0, 0)],
            [{"order_id": "paid-but-coverage-unknown", "source": "goofish_seller_orders", "order_status": "paid", "platform_paid_at": (live + timedelta(hours=1)).isoformat()}],
            experiment,
            at=at,
        )

        self.assertEqual(unknown["state"], "needs_data")
        self.assertIsNone(unknown["latest_valid"])
        self.assertIsNone(unknown["delta_browse"])
        self.assertFalse(unknown["review_ready"])
        self.assertEqual(zero["state"], "low_sample")
        self.assertEqual(zero["delta_browse"], 0)
        self.assertEqual(zero["delta_want"], 0)
        self.assertIsNone(zero["paid_orders"])
        self.assertIsNone(zero["paid_per_browse_pct"])
        self.assertTrue(zero["review_ready"])

    def test_paid_order_ratio_uses_only_the_observation_window_and_keeps_causal_uv_caveats(self) -> None:
        live = datetime(2026, 9, 1, 8, tzinfo=CHINA)
        end = live + timedelta(hours=73)
        history = [_metric(live, 100, 5), _metric(end, 200, 7, complete_orders=True)]
        orders = [
            {"order_id": "before", "source": "goofish_seller_orders", "order_status": "paid", "platform_paid_at": (live - timedelta(seconds=1)).isoformat()},
            {"order_id": "at-start", "source": "goofish_seller_orders", "order_status": "paid", "platform_paid_at": live.isoformat()},
            {"order_id": "inside", "source": "goofish_seller_orders", "order_status": "paid", "platform_paid_at": (live + timedelta(hours=1)).isoformat()},
            {"order_id": "inside", "source": "goofish_seller_orders", "order_status": "completed", "platform_paid_at": (live + timedelta(hours=2)).isoformat()},
            {"order_id": "at-end", "source": "goofish_seller_orders", "order_status": "completed", "platform_paid_at": end.isoformat()},
            {"order_id": "after", "source": "goofish_seller_orders", "order_status": "shipped", "platform_paid_at": (end + timedelta(seconds=1)).isoformat()},
            {"order_id": "unpaid", "source": "goofish_seller_orders", "order_status": "pending", "platform_paid_at": (live + timedelta(hours=3)).isoformat()},
            {"order_id": "wrong-source", "source": "legacy_local_database", "order_status": "paid", "platform_paid_at": (live + timedelta(hours=4)).isoformat()},
        ]

        result = analysis.analyse(
            history,
            orders,
            {"content_live_at": live.isoformat(), "state": "observing"},
            at=end + timedelta(hours=1),
        )

        self.assertEqual(result["window_start"], live.isoformat())
        self.assertEqual(result["window_end"], end.isoformat())
        self.assertEqual(result["delta_browse"], 100)
        self.assertEqual(result["paid_orders"], 2)
        self.assertEqual(result["paid_per_browse_pct"], 2.0)
        self.assertIn("不能证明订单由本次素材改动引起", result["recommendation"])
        self.assertTrue(any("公开浏览不是去重访客" in gap for gap in result["gaps"]))
        self.assertTrue(any("不能解释为买家转化概率" in gap for gap in result["gaps"]))

    def test_changed_content_interrupts_and_excludes_later_metrics_from_the_window(self) -> None:
        live = datetime(2026, 9, 1, 8, tzinfo=CHINA)
        ended = live + timedelta(hours=20)
        history = [
            _metric(live, 100, 5),
            _metric(live + timedelta(hours=10), 110, 6),
            _metric(ended, 400, 20),
            _metric(live + timedelta(hours=80), 900, 40, complete_orders=True),
        ]

        result = analysis.analyse(
            history,
            [],
            {"content_live_at": live.isoformat(), "content_ended_at": ended.isoformat(), "state": "content_changed"},
            at=live + timedelta(hours=100),
        )

        self.assertEqual(result["state"], "interrupted")
        self.assertFalse(result["review_ready"])
        self.assertEqual(result["window_end"], (live + timedelta(hours=10)).isoformat())
        self.assertEqual(result["delta_browse"], 10)


class MarketplaceTokenRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT, prefix="owned-console-token-")
        self.root = Path(self._tmp.name)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def make_client(self, name: str, responses: list[_FakeMtopResponse]) -> tuple[MtopClient, _FakeMtopSession, Store]:
        store = Store(self.root / f"{name}.sqlite3")
        store.put("account", "acct", {"id": "acct", "auth_state": "unchecked"}, account="acct")
        store.save_cookie("acct", "_m_h5_tk=oldtoken_1; unb=platform-user")
        client = MtopClient(store, "acct")
        session = _FakeMtopSession(responses)
        client.session = session
        return client, session, store

    async def test_read_token_retry_requires_rotated_expiry_and_never_retries_verification(self) -> None:
        client, session, store = self.make_client("rotated", [
            _FakeMtopResponse(
                {"ret": ["FAIL_SYS_TOKEN_EXOIRED::token expired"]},
                cookies={"_m_h5_tk": "freshtoken_2"},
            ),
            _FakeMtopResponse({"ret": ["SUCCESS::ok"], "data": {"item": "observed"}}),
        ])
        result = await client._post_mtop(
            api_name="mtop.taobao.idle.pc.detail",
            payload={"itemId": "item-1"},
        )
        self.assertEqual(result["data"], {"item": "observed"})
        self.assertEqual(len(session.calls), 2)
        self.assertIn("_m_h5_tk=oldtoken_1", session.calls[0]["headers"]["Cookie"])
        self.assertIn("_m_h5_tk=freshtoken_2", session.calls[1]["headers"]["Cookie"])
        self.assertNotEqual(session.calls[0]["params"]["sign"], session.calls[1]["params"]["sign"])
        self.assertIn("_m_h5_tk=freshtoken_2", store.cookie("acct"))

        challenge, challenge_session, challenge_store = self.make_client("challenge", [
            _FakeMtopResponse(
                {"ret": ["FAIL_SYS_USER_VALIDATE::RGV challenge"]},
                cookies={"_m_h5_tk": "challengefresh_2"},
            ),
            _FakeMtopResponse({"ret": ["SUCCESS::ok"], "data": {"module": {"items": []}}}),
        ])
        with self.assertRaises(MarketError) as challenge_error:
            await challenge._post_mtop(
                api_name="mtop.taobao.idle.pc.detail",
                payload={"itemId": "item-1"},
            )
        self.assertEqual(challenge_error.exception.code, "FAIL_SYS_USER_VALIDATE")
        self.assertEqual(len(challenge_session.calls), 1)
        self.assertEqual(challenge_store.get("account", "acct")["auth_state"], "unchecked")
        with self.assertRaises(MarketError) as blocked_again:
            await challenge._post_mtop(api_name="mtop.taobao.idle.pc.detail", payload={})
        self.assertEqual(blocked_again.exception.code, "FAIL_SYS_USER_VALIDATE")
        self.assertEqual(len(challenge_session.calls), 1)
        orders = await challenge._post_mtop(api_name="mtop.taobao.idle.trade.merchant.sold.get", payload={})
        self.assertEqual(orders["ret"], ["SUCCESS::ok"])
        with self.assertRaises(MarketError):
            await challenge._post_mtop(api_name="mtop.taobao.idle.pc.detail", payload={})
        self.assertEqual(len(challenge_session.calls), 2)

        unchanged, unchanged_session, unchanged_store = self.make_client("unchanged", [
            _FakeMtopResponse(
                {"ret": ["FAIL_SYS_TOKEN_EXPIRED::token expired"]},
                cookies={"_m_h5_tk": "oldtoken_1"},
            ),
        ])
        with self.assertRaises(MarketError) as unchanged_error:
            await unchanged._post_mtop(
                api_name="mtop.taobao.idle.pc.detail",
                payload={"itemId": "item-1"},
            )
        self.assertEqual(unchanged_error.exception.code, "FAIL_SYS_TOKEN_EXPIRED")
        self.assertEqual(len(unchanged_session.calls), 1)
        self.assertEqual(unchanged_store.get("account", "acct")["auth_state"], "login_required")


class BundleAndCredentialTests(OwnedConsoleTestCase):
    def test_bundle_contains_only_phone_materials_and_download_rejects_hash_tamper(self) -> None:
        fake_project = self.root / "project"
        fake_data = fake_project / "data"
        image = fake_project / "products" / "dsh-orangebook" / "listing" / "images" / "main.png"
        image.parent.mkdir(parents=True)
        image_bytes = b"fixture-image-bytes"
        image.write_bytes(image_bytes)
        store = Store(fake_data / "console.sqlite3")
        secret = "fixture-cookie-MUST-NOT-ENTER-ZIP"
        store.save_cookie("acct", secret)
        product = {
            "account": "acct",
            "item_id": "item-1",
            "slug": "dsh-orangebook",
            "title": "old",
            "description": "old description",
            "price": "19.90",
            "price_cents": 1990,
            "category_id": "cat-1",
            "quantity": 1,
            "skus": [],
            "image_urls": ["https://img.alicdn.com/old.png"],
            "observed_at": "2026-09-01T08:00:00+08:00",
            "source": "fixture",
        }
        store.put("account", "acct", {"id": "acct", "auth_state": "unchecked"}, account="acct")

        with patch.multiple(materials, PROJECT=fake_project, DATA=fake_data), patch.object(
            materials,
            "listing_sources",
            return_value={"title": "new title", "description": "new description", "image": image, "copy_file": None},
        ):
            package = materials.prepare_bundle(store, product)

        zip_path = fake_project / package["zip_file"]
        with zipfile.ZipFile(zip_path) as archive:
            names = set(archive.namelist())
            archive_contents = {name: archive.read(name) for name in archive.namelist()}
            contents = b"".join(archive_contents.values())
        self.assertEqual(names, {"01-\u4e3b\u56fe.png", "\u6807\u9898.txt", "\u5546\u54c1\u4ecb\u7ecd.txt", "\u624b\u673a\u4e0a\u4f20\u8bf4\u660e.txt"})
        self.assertEqual(archive_contents["01-\u4e3b\u56fe.png"], image_bytes)
        self.assertEqual(hashlib.sha256(image_bytes).hexdigest(), package["image"]["sha256"])
        self.assertEqual(hashlib.sha256(zip_path.read_bytes()).hexdigest(), package["zip_sha256"])
        self.assertNotIn(secret.encode(), contents)
        self.assertFalse(any("key" in name.lower() or "sqlite" in name.lower() or "manifest" in name.lower() for name in names))

        service = self.make_service(store)
        with patch.multiple(app_module, PROJECT=fake_project, DATA=fake_data):
            with TestClient(create_app(service)) as client:
                good = client.get(f"/api/packages/{package['package_id']}/download")
                self.assertEqual(good.status_code, 200)
                self.assertEqual(good.content, zip_path.read_bytes())

                with zip_path.open("ab") as handle:
                    handle.write(b"tamper")
                tampered = client.get(f"/api/packages/{package['package_id']}/download")
                self.assertEqual(tampered.status_code, 409)
                self.assertIn("\u53d1\u751f\u53d8\u5316", tampered.json()["detail"])

    def test_missing_credential_key_fails_closed_without_creating_a_replacement(self) -> None:
        store = self.make_store()
        store.save_cookie("acct", "first-secret")
        original_record = store.get("credential", "acct")
        key_path = store.path.parent / ".account.key"
        self.assertTrue(key_path.is_file())
        key_path.unlink()

        with self.assertRaisesRegex(ValueError, "\u5bc6\u94a5"):
            store.cookie("acct")
        self.assertFalse(key_path.exists())

        with self.assertRaisesRegex(ValueError, "\u5bc6\u94a5"):
            store.save_cookie("acct", "replacement-secret")
        self.assertFalse(key_path.exists())
        self.assertEqual(store.get("credential", "acct"), original_record)


class MigrationAndJobTests(OwnedConsoleTestCase):
    def make_legacy_database(self) -> tuple[Path, str]:
        source = self.root / "legacy" / "xianyu_data.db"
        source.parent.mkdir(parents=True)
        fake_secret = "fixture-legacy-cookie-never-print"
        db = sqlite3.connect(source)
        try:
            db.executescript(
                """
                CREATE TABLE cookies (id TEXT, value TEXT, user_id TEXT, proxy_type TEXT);
                CREATE TABLE item_info (
                    item_id TEXT, cookie_id TEXT, item_title TEXT, item_description TEXT,
                    item_price TEXT, item_detail TEXT, is_multi_spec INTEGER,
                    multi_quantity_delivery INTEGER, updated_at TEXT
                );
                """
            )
            db.execute("INSERT INTO cookies VALUES (?,?,?,?)", ("acct", fake_secret, "user-1", "none"))
            db.execute(
                "INSERT INTO item_info VALUES (?,?,?,?,?,?,?,?,?)",
                ("item-1", "acct", "title", "description", "19.9", json.dumps({"pic_info": {"picUrl": "https://img.alicdn.com/a.png"}}), 0, 0, "2026-09-01"),
            )
            db.commit()
        finally:
            db.close()
        return source, fake_secret

    def test_migration_is_read_only_one_time_and_does_not_print_credentials(self) -> None:
        source, fake_secret = self.make_legacy_database()
        store = self.make_store("owned.sqlite3")
        before_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        before_mtime = source.stat().st_mtime_ns
        stdout, stderr = io.StringIO(), io.StringIO()

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECRET_ENCRYPTION_KEY", None)
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                first = migration.migrate(store, source)

        self.assertEqual(first["status"], "imported")
        self.assertFalse(first["source_modified"])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before_hash)
        self.assertEqual(source.stat().st_mtime_ns, before_mtime)
        self.assertEqual(store.cookie("acct"), fake_secret)
        self.assertNotIn(fake_secret, stdout.getvalue() + stderr.getvalue() + json.dumps(first, ensure_ascii=False))
        self.assertNotIn(fake_secret.encode(), store.path.read_bytes())

        original_connect = sqlite3.connect

        def reject_legacy_reopen(database, *args, **kwargs):
            if "mode=ro" in str(database):
                raise AssertionError("one-time import reopened legacy DB")
            return original_connect(database, *args, **kwargs)

        with patch.object(migration.sqlite3, "connect", side_effect=reject_legacy_reopen):
            second = migration.migrate(store, source)
        self.assertTrue(second["already_imported"])
        self.assertEqual(len(store.rows("product", "acct")), 1)

    def test_jobs_deduplicate_persist_and_are_marked_interrupted_on_restart(self) -> None:
        path = self.root / "jobs.sqlite3"
        service = self.make_service(Store(path))
        executor = _replace_executor(service)

        first = service.submit("collect", "acct", "item-1")
        duplicate = service.submit("collect", "acct", "item-1")
        self.assertEqual(duplicate["id"], first["id"])
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(Store(path).job(first["id"])["state"], "queued")
        service.close()

        restarted = self.make_service(Store(path))
        _replace_executor(restarted)
        with TestClient(create_app(restarted)) as client:
            recovered = client.get(f"/api/jobs/{first['id']}")
            self.assertEqual(recovered.status_code, 200)
            self.assertEqual(recovered.json()["state"], "interrupted")
            self.assertIn("\u4e0a\u6b21\u505c\u6b62", recovered.json()["error"])


if __name__ == "__main__":
    unittest.main()
