from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from console import analysis, listing, materials
from console.engine import ops
from console.marketplace import EDIT_DETAIL_API, ITEM_DETAIL_API, MarketError, normalize_item_status
from console.service import ConsoleService
from console.store import Store, product_key


PROJECT = Path(__file__).resolve().parents[1]
STAMP = "2026-09-22T01:10:31+08:00"


def editor_body():
    return {"itemId": "123", "userId": "0", "itemStatus": "0",
            "itemTextDTO": {"title": "阅读资料", "desc": "阅读资料\n\n完整正文", "titleDescSeparate": "false"},
            "imageInfoDOList": [{"url": "https://img.alicdn.com/main.jpg"}],
            "itemPriceDTO": {"priceInCent": "99"}, "itemCatDTO": {"catId": "456"}, "quantity": "10000"}


class ListingEvidenceTests(unittest.TestCase):
    def test_owner_response_keeps_identity_raw_text_status_and_unknown_skus(self):
        current = listing.owned_item(editor_body(), "123", STAMP, "owner")
        self.assertEqual(current["status"], "在线")
        self.assertEqual(current["status_code"], "0")
        self.assertEqual(current["description"], "阅读资料\n\n完整正文")
        self.assertFalse(current["title_desc_separate"])
        self.assertEqual(current["price_cents"], "99")
        self.assertEqual(current["field_sources"]["price_cents"], EDIT_DETAIL_API)
        self.assertIsNone(current["skus"])
        self.assertEqual(current["source"], "goofish_owned_edit_detail")

    def test_missing_unknown_and_boolean_states_are_not_online(self):
        for value in (None, "", False, True, "-9", -9, "future-code"):
            with self.subTest(value=value):
                self.assertEqual(normalize_item_status(value), "unknown")
        self.assertEqual(normalize_item_status(0), "unknown")
        self.assertEqual(normalize_item_status(0, owned=True), "在线")
        self.assertEqual(normalize_item_status("审核中"), "审核中")

    def test_wrong_missing_item_and_conflicting_owner_are_rejected(self):
        for field, value, code in (("itemId", "other", "ITEM_ID_MISMATCH"),
                                    ("itemId", None, "ITEM_ID_MISSING"),
                                    ("userId", "other-owner", "ITEM_OWNER_MISMATCH")):
            body = editor_body(); body[field] = value
            with self.assertRaises(MarketError) as caught:
                listing.owned_item(body, "123", STAMP, "owner")
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(MarketError):
            listing.public_item({"itemDO": {"itemId": "recommendation"}}, "123", STAMP)

    def test_copy_match_only_tolerates_declared_combined_title_line(self):
        desired = {"title": "阅读资料", "description": "完整正文"}
        current = listing.owned_item(editor_body(), "123", STAMP)
        self.assertEqual(materials.compare_listing_copy(current, desired), {
            "title": True, "description": True, "description_method": "combined_field_exact_title_prefix"})
        for changes in ({"title_desc_separate": None}, {"title_desc_separate": True},
                        {"description": "阅读资料完整正文"}, {"description": "阅读资料\n完整正文\n额外承诺"},
                        {"title": "其他资料"}):
            with self.subTest(changes=changes):
                self.assertFalse(materials.compare_listing_copy({**current, **changes}, desired)["description"])

    def test_buyer_page_rejects_recommendations_redirects_and_missing_body(self):
        expected = listing.owned_item(editor_body(), "123", STAMP)
        good = "阅读资料\n完整正文\n8浏览\n1人想要"
        url = "https://www.goofish.com/item?id=123"
        self.assertTrue(ops().verify_page_item(good, url, "123", expected)["match"])
        for body, actual_url in (("商品不存在\n推荐商品\n" + good, url),
                                 (good, "https://www.goofish.com/item?id=456"),
                                 ("阅读资料\n其他正文\n8浏览", url)):
            with self.subTest(body=body, url=actual_url):
                self.assertFalse(ops().verify_page_item(body, actual_url, "123", expected)["match"])


    def test_recommended_items_interest_count_is_not_the_owned_listing(self):
        body = "阅读资料\n8浏览\n完整正文\n为你推荐\n其他资料\n98人想要"
        metric = ops().parse_page_header(body)
        self.assertEqual(metric["browse"], 8)
        self.assertEqual(metric["want"], "not_visible")
        self.assertIsNone(metric["display_count_evidence"]["want"])
        self.assertEqual(metric["parser_version"], 2)


class IndependentReadTests(unittest.TestCase):
    def setUp(self):
        base = PROJECT / "data" / "test-tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base, prefix="listing-")
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "console.sqlite3")
        self.store.put("account", "acct", {"id": "acct", "auth_state": "verified"}, account="acct")
        self.store.save_cookie("acct", "unb=owner; _m_h5_tk=fixture_1")
        self.service = ConsoleService(self.store, import_legacy=False)

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def client(self, public, owned):
        class Client:
            cookies = {"unb": "owner"}
            calls = []

            async def __aenter__(self): return self
            async def __aexit__(self, *_): return None

            async def _post_mtop(self, *, api_name, payload):
                self.calls.append(api_name)
                value = public if api_name == ITEM_DETAIL_API else owned
                if isinstance(value, Exception): raise value
                return {"data": copy.deepcopy(value)}

            async def orders(self):
                self.calls.append("orders")
                return {"orders": [], "complete": True, "captured_at": STAMP}
        return Client()

    def read(self, client):
        with patch("console.service.MtopClient", return_value=client):
            return asyncio.run(self.service._read_platform("acct", ["123"]))

    def test_limited_public_api_does_not_skip_owner_detail_or_orders(self):
        self.store.put("api_block", product_key("acct", ITEM_DETAIL_API),
                       {"api": ITEM_DETAIL_API, "code": "FAIL_SYS_USER_VALIDATE"}, account="acct")
        client = self.client(MarketError("FAIL_SYS_USER_VALIDATE", "blocked"), editor_body())
        result = self.read(client)
        self.assertEqual(client.calls, [ITEM_DETAIL_API, EDIT_DETAIL_API, "orders"])
        self.assertEqual(result["items"]["123"]["source"], "goofish_owned_edit_detail")
        self.assertEqual(result["errors"], {})
        self.assertTrue(result["orders"]["complete"])
        self.assertEqual(result["source_warnings"]["123"][0]["api"], ITEM_DETAIL_API)
        self.assertIsNotNone(self.store.get("api_block", product_key("acct", ITEM_DETAIL_API)))

    def test_wrong_owner_item_is_not_accepted_and_order_read_remains_independent(self):
        wrong = editor_body(); wrong["itemId"] = "other"
        result = self.read(self.client(MarketError("FAIL_SYS_USER_VALIDATE", "blocked"), wrong))
        self.assertEqual(result["items"], {})
        self.assertEqual(result["errors"]["123"]["code"], "LISTING_READ_UNAVAILABLE")
        self.assertTrue(result["orders"]["complete"])

    def prepare_experiment(self):
        self.store.put("product", product_key("acct", "123"), {"account": "acct", "item_id": "123"}, account="acct")
        self.store.put("package", "package", {"package_id": "package", "account": "acct",
                       "desired": {"title": "阅读资料", "description": "完整正文"},
                       "baseline": {"price_cents": "99", "category_id": "456", "skus": None}}, account="acct")
        self.store.put("experiment", product_key("acct", "123"),
                       {"package_id": "package", "state": "pending_review", "content_live_at": None}, account="acct")

    def test_successful_owner_read_starts_at_actual_read_time_without_fabricating_skus(self):
        self.prepare_experiment()
        current = listing.owned_item(editor_body(), "123", STAMP)
        current["buyer_page_evidence"] = {"match": True}
        with patch.object(materials, "online_image_check", return_value={"match": True, "method": "fixture"}):
            result = self.service.observe_package("acct", "123", current)
        self.assertEqual(result["state"], "observing")
        self.assertEqual(result["content_live_at"], STAMP)
        self.assertEqual(result["checks"]["sku"], "unavailable")

    def test_owner_data_without_buyer_page_does_not_certify_publication(self):
        self.prepare_experiment()
        current = listing.owned_item(editor_body(), "123", STAMP)
        with patch.object(materials, "online_image_check", return_value={"match": True}):
            result = self.service.observe_package("acct", "123", current)
        self.assertEqual(result["state"], "evidence_incomplete")
        self.assertIsNone(result["content_live_at"])
        self.assertFalse(result["checks"]["buyer_page"])

    def test_transient_missing_field_does_not_end_existing_observation(self):
        self.prepare_experiment()
        experiment = self.store.get("experiment", product_key("acct", "123"))
        experiment.update({"state": "observing", "content_live_at": STAMP})
        self.store.put("experiment", product_key("acct", "123"), experiment, account="acct")
        body = editor_body(); body["itemTextDTO"].pop("desc")
        current = listing.owned_item(body, "123", STAMP)
        current["buyer_page_evidence"] = {"match": True}
        result = self.service.observe_package("acct", "123", current)
        self.assertEqual(result["state"], "evidence_incomplete")
        self.assertNotIn("content_ended_at", self.store.get("experiment", product_key("acct", "123")))

    def test_metric_uses_its_read_time_when_collection_began_before_publication(self):
        snapshot = {"account": "acct", "item_ids": ["123"], "captured_at": "2026-09-22T01:10:00+08:00",
                    "items": {"123": {"status": "在线"}}, "public_detail_metrics": [
                        {"item_id": "123", "captured_at": "2026-09-22T01:10:35+08:00",
                         "source": "public_item_detail_page", "status": "observed", "browse": 6, "want": 129}]}
        (self.root / "timing-snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
        history = analysis.snapshot_history("acct", "123", self.root)
        report = analysis.analyse(history, [], {"content_live_at": STAMP, "state": "observing"})
        self.assertEqual(report["state"], "observing")
        self.assertEqual(history[0]["captured_at"], "2026-09-22T01:10:35+08:00")
        self.assertIsNone(history[0]["want"])

    def test_conflicting_sources_cannot_start_observation(self):
        self.prepare_experiment()
        owned = listing.owned_item(editor_body(), "123", STAMP)
        current = listing.combine_item_reads({"status": "已删除", "price_cents": "99"}, owned)
        with patch.object(materials, "online_image_check", return_value={"match": True}):
            result = self.service.observe_package("acct", "123", current)
        self.assertEqual(result["state"], "needs_attention")
        self.assertIsNone(result["content_live_at"])
        self.assertFalse(result["checks"]["sources_agree"])

    def test_unknown_status_is_not_reported_as_pending_upload_or_review(self):
        self.prepare_experiment()
        body = editor_body(); body.pop("itemStatus")
        with patch.object(materials, "online_image_check", return_value={"match": True}):
            result = self.service.observe_package("acct", "123", listing.owned_item(body, "123", STAMP))
        self.assertEqual(result["state"], "status_unknown")
        experiment = self.store.get("experiment", product_key("acct", "123"))
        self.assertEqual(analysis.analyse([], [], experiment)["state"], "status_unknown")


if __name__ == "__main__":
    unittest.main()
