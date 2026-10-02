from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import xianyu_ops_core as ops  # noqa: E402


TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / "data" / "test-tmp"


class XianyuOpsCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)

    def test_parse_copy_and_package_hash_are_stable(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT, prefix="core-") as temp:
            path = Path(temp) / "copy.md"
            path.write_text("## 标题\n标题\n## 主要介绍\n介绍正文\n", encoding="utf-8")
            self.assertEqual(ops.parse_copy_file(path), ("标题", "介绍正文"))
        package = {"package_id": "p", "desired": {"title": "a"}}
        package["package_sha256"] = ops.package_hash(package)
        self.assertEqual(ops.package_hash(package), package["package_sha256"])
        package["desired"]["title"] = "changed"
        self.assertNotEqual(ops.package_hash(package), package["package_sha256"])

    def test_review_window_starts_only_from_verified_at(self) -> None:
        self.assertEqual(ops.review_window(None)["status"], "pending_verified_at")
        verified = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)
        before = ops.review_window(verified.isoformat(), verified + timedelta(hours=71.99))
        due = ops.review_window(verified.isoformat(), verified + timedelta(hours=72))
        self.assertFalse(before["due"])
        self.assertTrue(due["due"])

    def test_snapshot_keeps_partial_sources_and_file(self) -> None:
        def fake_http(_base, method, path, payload=None, timeout=20):
            if path == "/items/account":
                raise ops.OpsError("items unavailable")
            if path == "/api/orders":
                return {"data": []}
            if path == "/items/get-all-from-account":
                return {"ok": True}
            raise AssertionError((method, path))

        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT, prefix="core-") as temp:
            with patch.object(ops, "http_json", side_effect=fake_http), patch.object(
                ops,
                "public_metrics",
                return_value=[{"item_id": "1", "status": "observed", "browse": 2, "want": 1}],
            ):
                snapshot, path = ops.snapshot_once(
                    "account",
                    ["1"],
                    api_base="http://local",
                    cdp_url="http://cdp",
                    sync_account=True,
                    output_dir=Path(temp),
                )
            self.assertEqual(snapshot["status"], "partial")
            self.assertTrue(path.is_file())
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["source_status"]["local_items"]["status"], "unavailable")
            self.assertEqual(saved["public_detail_metrics"][0]["browse"], 2)

    def test_snapshot_writes_file_even_when_every_source_is_unavailable(self) -> None:
        def unavailable(*_args, **_kwargs):
            raise ops.OpsError("runtime unavailable")

        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT, prefix="core-") as temp:
            with patch.object(ops, "http_json", side_effect=unavailable), patch.object(
                ops,
                "public_metrics",
                return_value=[{"item_id": "1", "status": "unavailable"}],
            ):
                snapshot, path = ops.snapshot_once(
                    "account",
                    ["1"],
                    api_base="http://local",
                    cdp_url="http://cdp",
                    sync_account=True,
                    output_dir=Path(temp),
                )
            self.assertEqual(snapshot["status"], "partial")
            self.assertTrue(path.is_file())

    def test_parse_page_header_supports_thousands_and_ten_thousands(self) -> None:
        result = ops.parse_page_header("商品正文 1.2万浏览，1,234人想要")
        self.assertEqual(result["browse"], 12000)
        self.assertEqual(result["want"], 1234)

    def test_delivery_rules_accept_real_list_response(self) -> None:
        with patch.object(ops, "http_json", return_value=[{"id": 1, "keyword": "old"}]):
            self.assertEqual(ops._delivery_rules("http://offline"), [{"id": 1, "keyword": "old"}])

    def test_snapshot_does_not_turn_business_failures_or_missing_item_into_success(self) -> None:
        def fake_http(_base, method, path, payload=None, timeout=20):
            del method, payload, timeout
            if path == "/items/get-all-from-account":
                return {"success": False, "message": "sync failed"}
            if path == "/items/account":
                return {"items": []}
            if path == "/api/orders":
                return {"success": False, "message": "orders unavailable"}
            raise AssertionError(path)

        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT, prefix="core-") as temp:
            with patch.object(ops, "http_json", side_effect=fake_http), patch.object(
                ops,
                "public_metrics",
                return_value=[{"item_id": "1", "status": "observed", "browse": 1, "want": 0}],
            ):
                snapshot, _path = ops.snapshot_once(
                    "account",
                    ["1"],
                    api_base="http://local",
                    cdp_url="http://cdp",
                    sync_account=True,
                    output_dir=Path(temp),
                )
        self.assertEqual(snapshot["source_status"]["account_sync"]["status"], "unavailable")
        self.assertEqual(snapshot["source_status"]["local_items"]["status"], "partial")
        self.assertEqual(snapshot["source_status"]["orders"]["status"], "unavailable")



if __name__ == "__main__":
    unittest.main()
