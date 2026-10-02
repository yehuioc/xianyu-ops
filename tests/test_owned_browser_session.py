from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from console.marketplace import (EDIT_DETAIL_API, ITEM_DETAIL_API, ITEM_LIST_API, MarketError,
                                 connect_browser_cookie, parse_cookie)
from console.store import Store, product_key
from console.service import ConsoleService


class BrowserContext:
    def __init__(self, values, replies=()):
        self.values = dict(values)
        self.replies = list(replies)
        self.calls = []
        self.request = self

    def cookies(self, _urls):
        return [{"name": k, "value": v} for k, v in self.values.items()]

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.replies:
            raise AssertionError("unexpected platform retry")
        body, changes = self.replies.pop(0)
        self.values.update(changes)
        return SimpleNamespace(status=200, json=lambda: body, dispose=lambda: None)


SUCCESS = {"ret": ["SUCCESS::ok"], "data": {"cardList": []}}
EMPTY = {"ret": ["FAIL_SYS_TOKEN_EMPTY::missing"]}
EXPIRED = {"ret": ["FAIL_SYS_TOKEN_EXOIRED::expired"]}


class BrowserSessionTest(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "data" / "test-tmp"
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root, prefix="browser-session-")
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "test.sqlite3")
        self.original = "unb=owner; _m_h5_tk=old_1; cookie2=session"
        self.store.save_cookie("acct", self.original)
        self.store.put("account", "acct", {"id": "acct", "platform_user_id": "owner",
                       "auth_state": "login_required", "error_code": "FAIL_SYS_TOKEN_EXOIRED"}, account="acct")
        self.block = {"api": ITEM_DETAIL_API, "code": "FAIL_SYS_USER_VALIDATE", "blocked_at": "earlier"}
        self.store.put("api_block", product_key("acct", ITEM_DETAIL_API), self.block, account="acct")

    def connect(self, *contexts, fresh_only=False):
        manager = Mock()
        manager.__enter__ = Mock(return_value=SimpleNamespace(chromium=SimpleNamespace(
            connect_over_cdp=lambda *_args, **_kwargs: SimpleNamespace(contexts=contexts))))
        manager.__exit__ = Mock(return_value=False)
        with patch("playwright.sync_api.sync_playwright", return_value=manager):
            return connect_browser_cookie(self.store, "acct", fresh_only=fresh_only)

    def test_missing_short_token_renews_without_calling_it_logout(self):
        context = BrowserContext({"unb": "owner", "cookie2": "session"}, [
            (EMPTY, {"_m_h5_tk": "renewed_2", "_m_h5_tk_enc": "encrypted"}), (SUCCESS, {})])
        result = self.connect(context, fresh_only=True)
        self.assertTrue(result["verified"])
        self.assertEqual(result["probe_attempts"], 2)
        self.assertEqual(self.store.get("account", "acct")["auth_state"], "verified")
        self.assertEqual(parse_cookie(self.store.cookie("acct"))["_m_h5_tk"], "renewed_2")
        self.assertEqual(self.store.get("api_block", product_key("acct", ITEM_DETAIL_API)), self.block)
        self.assertTrue(all(call[1]["max_retries"] == 0 for call in context.calls))
        self.assertNotIn("renewed_2", str(result))

    def test_manual_connect_is_verified_and_preserves_other_api_blocks(self):
        context = BrowserContext({"unb": "owner", "_m_h5_tk": "fresh_2"}, [(SUCCESS, {})])
        self.assertTrue(self.connect(context)["verified"])
        self.assertEqual(len(context.calls), 1)
        self.assertEqual(self.store.get("api_block", product_key("acct", ITEM_DETAIL_API)), self.block)

    def test_expired_unchanged_token_is_not_retried(self):
        context = BrowserContext(parse_cookie(self.original), [(EXPIRED, {})])
        with self.assertRaises(MarketError) as error:
            self.connect(context, fresh_only=True)
        self.assertEqual(error.exception.code, "FAIL_SYS_TOKEN_EXOIRED")
        self.assertEqual(len(context.calls), 1)
        self.assertEqual(self.store.cookie("acct"), self.original)

    def blocked_catalog_with_owned_item(self):
        block = {"api": ITEM_LIST_API, "code": "FAIL_SYS_ILLEGAL_ACCESS", "blocked_at": "earlier"}
        self.store.put("api_block", product_key("acct", ITEM_LIST_API), block, account="acct")
        self.store.put("product", product_key("acct", "123"),
                       {"item_id": "123", "managed": True, "watch": True,
                        "source": "goofish_owned_edit_detail"}, account="acct")
        return block

    def test_blocked_catalog_does_not_prevent_owner_read_verification(self):
        block = self.blocked_catalog_with_owned_item()
        body = {"ret": ["SUCCESS::ok"], "data": {"itemId": "123", "userId": "0",
                "itemTextDTO": {"title": "Owned item"}, "itemStatus": "0"}}
        context = BrowserContext({"unb": "owner", "_m_h5_tk": "fresh_2"}, [(body, {})])
        self.assertTrue(self.connect(context)["verified"])
        self.assertEqual(len(context.calls), 1)
        self.assertEqual(context.calls[0][1]["params"]["api"], EDIT_DETAIL_API)
        self.assertEqual(self.store.get("api_block", product_key("acct", ITEM_LIST_API)), block)
        self.assertEqual(self.store.get("api_block", product_key("acct", ITEM_DETAIL_API)), self.block)

    def test_owner_probe_rejects_wrong_item_owner_and_incomplete_schema(self):
        self.blocked_catalog_with_owned_item()
        for body in ({"itemId": "456", "itemTextDTO": {"title": "Wrong item"}},
                     {"itemId": "123", "userId": "other", "itemTextDTO": {"title": "Wrong owner"}},
                     {"itemId": "123"}):
            context = BrowserContext({"unb": "owner", "_m_h5_tk": "fresh_2"},
                                     [({"ret": ["SUCCESS::ok"], "data": body}, {})])
            with self.assertRaises(MarketError):
                self.connect(context)
            self.assertEqual(self.store.cookie("acct"), self.original)

    def test_owner_probe_challenge_blocks_only_that_api_and_never_retries(self):
        self.blocked_catalog_with_owned_item()
        context = BrowserContext({"unb": "owner", "_m_h5_tk": "fresh_2"},
                                 [({"ret": ["FAIL_SYS_USER_VALIDATE::challenge"]}, {})])
        with self.assertRaises(MarketError):
            self.connect(context)
        with self.assertRaises(MarketError):
            self.connect(context)
        self.assertEqual(len(context.calls), 1)
        self.assertTrue(self.store.get("api_block", product_key("acct", EDIT_DETAIL_API)))
        self.assertEqual(self.store.cookie("acct"), self.original)

    def test_no_known_owner_item_does_not_probe_another_api(self):
        self.store.put("api_block", product_key("acct", ITEM_LIST_API),
                       {"api": ITEM_LIST_API, "code": "FAIL_SYS_ILLEGAL_ACCESS"}, account="acct")
        self.store.put("product", product_key("acct", "123"),
                       {"item_id": "123", "managed": True, "source": "public"}, account="acct")
        context = BrowserContext({"unb": "owner", "_m_h5_tk": "fresh_2"})
        with self.assertRaises(MarketError):
            self.connect(context)
        self.assertEqual(context.calls, [])

    def test_second_token_failure_stops_even_with_another_new_token(self):
        context = BrowserContext({"unb": "owner"}, [(EMPTY, {"_m_h5_tk": "first_2"}),
                                                   (EXPIRED, {"_m_h5_tk": "second_3"})])
        with self.assertRaises(MarketError):
            self.connect(context)
        self.assertEqual(len(context.calls), 2)
        self.assertEqual(self.store.cookie("acct"), self.original)

    def test_challenge_stops_before_token_retry(self):
        context = BrowserContext({"unb": "owner"}, [
            ({"ret": ["FAIL_SYS_USER_VALIDATE::challenge"]}, {"_m_h5_tk": "fresh_2"})])
        with self.assertRaises(MarketError) as error:
            self.connect(context)
        self.assertEqual(error.exception.code, "FAIL_SYS_USER_VALIDATE")
        self.assertEqual(len(context.calls), 1)
        self.assertEqual(self.store.cookie("acct"), self.original)
        with self.assertRaises(MarketError):
            self.connect(context)
        self.assertEqual(len(context.calls), 1, "blocked endpoint must not be re-requested")

    def test_wrong_account_and_mid_request_account_switch_never_overwrite(self):
        wrong = BrowserContext({"unb": "someone-else"})
        with self.assertRaises(MarketError) as error:
            self.connect(wrong)
        self.assertEqual(error.exception.code, "ACCOUNT_MISMATCH")
        self.assertEqual(wrong.calls, [])
        switched = BrowserContext({"unb": "owner"}, [(SUCCESS, {"unb": "someone-else", "_m_h5_tk": "new_2"})])
        with self.assertRaises(MarketError) as error:
            self.connect(switched)
        self.assertEqual(error.exception.code, "ACCOUNT_MISMATCH")
        self.assertEqual(self.store.cookie("acct"), self.original)

    def test_no_identity_does_not_make_a_platform_request(self):
        context = BrowserContext({"_m_h5_tk": "anonymous_1"})
        with self.assertRaises(MarketError) as error:
            self.connect(context)
        self.assertEqual(error.exception.code, "LOGIN_REQUIRED")
        self.assertEqual(context.calls, [])

    def test_selects_matching_context_not_first(self):
        wrong = BrowserContext({"unb": "another-user"})
        right = BrowserContext({"unb": "owner", "_m_h5_tk": "fresh_2"}, [(SUCCESS, {})])
        self.assertTrue(self.connect(wrong, right)["verified"])
        self.assertEqual(wrong.calls, [])
        self.assertEqual(len(right.calls), 1)

    def test_schema_drift_and_unknown_provider_error_are_not_success(self):
        for body, code in [({"ret": ["SUCCESS::ok"], "data": None}, "LOGIN_PROBE_SCHEMA_UNKNOWN"),
                           ({"ret": ["FAIL_SYS_UNEXPECTED::unknown"]}, "FAIL_SYS_UNEXPECTED")]:
            context = BrowserContext({"unb": "owner", "_m_h5_tk": "fresh_2"}, [(body, {})])
            with self.assertRaises(MarketError) as error:
                self.connect(context)
            self.assertEqual(error.exception.code, code)
            self.assertEqual(len(context.calls), 1)
            self.assertEqual(self.store.cookie("acct"), self.original)

    def test_real_session_expiry_is_not_an_automatic_token_retry(self):
        context = BrowserContext({"unb": "owner"}, [
            ({"ret": ["FAIL_SYS_SESSION_EXPIRED::expired"]}, {"_m_h5_tk": "fresh_2"})])
        with self.assertRaises(MarketError) as error:
            self.connect(context)
        self.assertEqual(error.exception.code, "FAIL_SYS_SESSION_EXPIRED")
        self.assertEqual(len(context.calls), 1)
        self.assertEqual(self.connect(context, fresh_only=True)["status"], "unchanged")
        self.assertEqual(len(context.calls), 1)

    def test_collection_keeps_specific_browser_failure_and_does_not_read_platform(self):
        self.store.put("product", product_key("acct", "123"), {"item_id": "123"}, account="acct")
        service = ConsoleService(self.store, import_legacy=False)
        self.addCleanup(service.close)
        with patch("console.service.connect_browser_cookie", side_effect=MarketError("ACCOUNT_MISMATCH", "账号不匹配")) as recover, \
             patch.object(service, "_read_platform", new_callable=AsyncMock) as read, \
             patch("console.service.OPS", Path(self.tmp.name) / "ops"), \
             patch.object(service, "messaging_health", return_value={"enabled": False}):
            result = service.collect("acct", ["123"])
        self.assertEqual(result["errors"]["123"]["code"], "ACCOUNT_MISMATCH")
        recover.assert_called_once()
        read.assert_not_awaited()

    def test_collection_cannot_start_a_second_recovery_cycle(self):
        self.store.put("product", product_key("acct", "123"), {"item_id": "123"}, account="acct")
        service = ConsoleService(self.store, import_legacy=False)
        self.addCleanup(service.close)

        def recover_once(*_args, **_kwargs):
            row = self.store.get("account", "acct")
            row["auth_state"] = "verified"
            self.store.put("account", "acct", row, account="acct")
            return {"status": "connected"}

        async def expires_again(*_args):
            row = self.store.get("account", "acct")
            row["auth_state"] = "login_required"
            self.store.put("account", "acct", row, account="acct")
            return {"items": {}, "errors": {}, "orders": None, "stop": True}

        with patch("console.service.connect_browser_cookie", side_effect=recover_once) as recover, \
             patch.object(service, "_read_platform", side_effect=expires_again) as read, \
             patch("console.service.OPS", Path(self.tmp.name) / "ops"), \
             patch.object(service, "messaging_health", return_value={"enabled": False}):
            service.collect("acct", ["123"])
        recover.assert_called_once()
        read.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
