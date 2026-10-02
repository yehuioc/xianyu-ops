import asyncio
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import test_owned_publishing as publishing_fixtures
import test_owned_quark as quark_fixtures
from console import commerce, publishing, quark
from console.delivery_workflow import Backend, DeliveryWorkflow, BatchWorkflow, WorkflowStop, batch_lock
from console.marketplace import MarketError, PUBLISH_API


class LocalBackend:
    """Real business functions and persistent store, with fake external platforms only."""
    def __init__(self, fixture, cloud):
        self.fixture, self.cloud = fixture, cloud
        self.store = fixture.store
        self.calls = []
        self.message_ready = True
        self.order_ready = True

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        if path == "/api/delivery":
            return {"messaging": {"enabled": True, "active": self.message_ready, "managed_item_ids": ["222"],
                "transport": {"ready": self.message_ready}, "account_status": {
                    "can_attempt_order_read": self.order_ready, "can_attempt_delivery": self.order_ready}}}
        if path.endswith("/bundle"):
            return commerce.build_bundles(self.store, "a", "career-kit")
        if path.endswith("/publish-preview"):
            return self.fixture.preview(**body)
        raise AssertionError((method, path))

    def job(self, path, body=None):
        self.calls.append(("POST", path))
        body = body or {}
        try:
            if path.endswith("/quark/prepare") or path.endswith("/quark/reconcile"):
                return {"delivery": quark.prepare(self.store, "a", "career-kit", cli=self.cloud,
                    reconcile_only=path.endswith("/quark/reconcile"))}
            if path.endswith("/quark/bind"):
                return {"delivery": quark.bind(self.store, "a", "career-kit", body["sha256"], cli=self.cloud)}
            if path.endswith("/publish"):
                result = self.fixture.run_publish(self.store.get("publish_preview", body["preview_id"]))
            elif path.endswith("/publish-reconcile"):
                result = asyncio.run(publishing.reconcile(self.store, "a", "career-kit", client_factory=self.fixture.client))
            else:
                raise AssertionError(path)
            if result["state"] != "published":
                raise WorkflowStop(result["message"])
            return {"publication": result}
        except ValueError as exc:
            raise WorkflowStop(str(exc)) from exc


class DeliveryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = publishing_fixtures.PublisherTests()
        self.fixture.setUp()
        self.cloud = quark_fixtures.FakeCloud(Path(self.fixture.temp.name) / "cloud")
        self.backend = LocalBackend(self.fixture, self.cloud)
        self.flow = DeliveryWorkflow(self.fixture.store, self.backend, "a", "career-kit")

    def tearDown(self):
        self.fixture.tearDown()

    def finish(self, plan):
        return self.flow.finish(plan["preview_id"], plan["digest"], plan["delivery_sha256"])

    def test_prepare_publish_bind_verify_and_repeat_use_one_upload_and_publication(self):
        plan = self.flow.prepare({"price_cents": 2500})
        self.assertEqual(plan["state"], "prepared_for_review")
        self.assertEqual(self.fixture.store.rows("publication"), [])
        self.assertEqual(self.fixture.store.rows("delivery_rule"), [])
        self.assertEqual(self.finish(plan)["state"], "ready_for_paid_order")
        self.flow = DeliveryWorkflow(self.fixture.store, self.backend, "a", "career-kit")
        self.assertEqual(self.finish(plan)["state"], "ready_for_paid_order")
        verified = self.flow.verify()
        self.assertTrue(verified["remote_rechecked"])
        self.assertEqual(verified["live_order_proof"], "not_established_by_this_check")
        self.assertEqual(sum(c[0] == "upload" for c in self.cloud.calls), 1)
        self.assertEqual(sum(c[0] == "share" for c in self.cloud.calls), 1)
        self.assertEqual(sum(c["api_name"] == PUBLISH_API for c in self.fixture.calls), 1)

    def test_lost_publication_response_resumes_only_with_readback(self):
        plan = self.flow.prepare()
        self.fixture.failure = MarketError("NETWORK_ERROR", "response lost")
        with self.assertRaises(WorkflowStop):
            self.finish(plan)
        self.assertEqual(self.flow.row("publication")["state"], "unknown")
        self.assertEqual(self.fixture.store.rows("delivery_rule"), [])
        self.fixture.failure = None
        self.assertEqual(self.finish(plan)["state"], "ready_for_paid_order")
        self.assertEqual(sum(c["api_name"] == PUBLISH_API for c in self.fixture.calls), 1)

    def test_lost_upload_response_recovers_file_without_second_upload(self):
        self.cloud.fail_upload = True
        with self.assertRaises(WorkflowStop):
            self.flow.prepare()
        self.cloud.fail_upload = False
        plan = self.flow.prepare()
        self.assertEqual(plan["state"], "prepared_for_review")
        self.assertEqual(sum(c[0] == "upload" for c in self.cloud.calls), 1)

    def test_changed_bundle_after_confirmation_stops_before_publication(self):
        plan = self.flow.prepare()
        original = self.flow.bundle()
        with patch.object(self.flow, "bundle", return_value={**original, "content_sha256": "changed"}):
            with self.assertRaisesRegex(WorkflowStop, "已变化"):
                self.finish(plan)
        self.assertEqual(self.fixture.store.rows("publication"), [])

    def test_wrong_preview_and_wrong_delivery_version_never_publish(self):
        plan = self.flow.prepare()
        for values in (("wrong", plan["digest"], plan["delivery_sha256"]),
                       (plan["preview_id"], plan["digest"], "0" * 64)):
            with self.assertRaises(WorkflowStop):
                self.flow.finish(*values)
        self.assertEqual(self.fixture.store.rows("publication"), [])

    def test_status_detects_wrong_item_tampered_package_and_stopped_order_channel(self):
        plan = self.flow.prepare()
        self.finish(plan)
        binding = self.flow.row("quark_binding")
        self.fixture.store.put("quark_binding", self.flow.key, {**binding, "item_id": "999"}, account="a")
        self.assertFalse(self.flow.status()["checks"]["bound_to_exact_item"])
        self.fixture.store.put("quark_binding", self.flow.key, binding, account="a")
        self.backend.order_ready = False
        self.assertFalse(self.flow.status()["checks"]["platform_order_and_delivery_ready"])
        self.backend.order_ready = True
        (commerce.PROJECT / self.flow.row("quark_delivery")["local_file"]).write_bytes(b"broken")
        self.assertFalse(self.flow.status()["checks"]["bound_local_package_intact"])

    def test_unknown_share_does_not_repeat_share_and_service_never_enters_workflow(self):
        self.cloud.fail_share = True
        with self.assertRaises(WorkflowStop):
            self.flow.prepare()
        with self.assertRaises(WorkflowStop):
            self.flow.prepare()
        self.assertEqual(sum(c[0] == "share" for c in self.cloud.calls), 1)
        service = DeliveryWorkflow(self.fixture.store, self.backend, "a", "excel-cleaning")
        before = len(self.backend.calls)
        with self.assertRaisesRegex(WorkflowStop, "服务样例"):
            service.prepare()
        self.assertEqual(len(self.backend.calls), before)

    def test_pending_job_and_failed_remote_verification_are_not_reported_ready(self):
        job = self.fixture.store.new_job("quark_prepare", "a", "career-kit")
        with self.assertRaises(WorkflowStop) as caught:
            self.flow.prepare()
        self.assertEqual(caught.exception.job_id, job["id"])
        self.fixture.store.update_job(job["id"], "succeeded", result={})
        plan = self.flow.prepare()
        self.finish(plan)
        self.cloud.extra_share_file = True
        result = self.flow.verify()
        self.assertEqual(result["state"], "needs_attention")
        self.assertFalse(result["remote_rechecked"])
        self.cloud.extra_share_file = False
        result = self.flow.verify()
        self.assertEqual(result["state"], "ready_for_paid_order")
        self.assertTrue(result["checks"]["cloud_has_no_unresolved_error"])


class BundleRaceTests(unittest.TestCase):
    def test_file_changes_after_manifest_were_read_never_produce_an_accepted_package(self):
        fixture = publishing_fixtures.PublisherTests()
        fixture.setUp()
        try:
            real = commerce.file_list
            def stale(slug, section):
                rows = copy.deepcopy(real(slug, section))
                if section == "delivery":
                    rows[0]["sha256"] = "0" * 64
                return rows
            with patch.object(commerce, "file_list", side_effect=stale):
                with self.assertRaisesRegex(ValueError, "打包期间文件已变化"):
                    commerce.build_bundles(fixture.store, "a", "career-kit")
            self.assertEqual(fixture.store.rows("commerce_bundle"), [])
        finally:
            fixture.tearDown()


class BatchWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = publishing_fixtures.PublisherTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.cloud = quark_fixtures.FakeCloud(Path(self.fixture.temp.name) / "cloud")
        self.backend = LocalBackend(self.fixture, self.cloud)
        self.batch = BatchWorkflow(self.fixture.store, self.backend, "a", interval=0)

    def multi_batch(self):
        catalog = commerce.read_catalog()
        catalog["products"][1]["sale_type"] = "digital"
        commerce.CATALOG.write_text(json.dumps(catalog), encoding="utf-8")
        commerce.source_file("excel-cleaning", "delivery", "示例.txt").write_text("不同的独立合成成品。", encoding="utf-8")
        self.flows = {}
        for slug in ("career-kit", "excel-cleaning"):
            flow = MagicMock()
            flow.prepare.return_value = {"preview_id": slug, "digest": "review-" + slug}
            flow.finish.return_value = {"publication_state": "published", "cloud_state": "bound", "state": "needs_attention"}
            self.flows[slug] = flow
        batch = BatchWorkflow(self.fixture.store, self.backend, "a",
                              flow_factory=lambda store, backend, account, slug: self.flows[slug])
        return batch

    def test_real_product_flow_upload_publish_bind_and_repeat_batch(self):
        batch = self.batch.plan(["career-kit"])
        self.assertEqual(batch["products"][0]["values"]["price_cents"], 99)
        self.assertEqual(self.cloud.calls, [])
        prepared = self.batch.prepare(batch["id"])
        self.assertEqual(prepared["state"], "prepared_for_review")
        self.assertEqual(self.fixture.store.rows("publication"), [])
        done = self.batch.finish(batch["id"], prepared["review_digest"])
        self.assertEqual(done["state"], "configured")
        self.batch.finish(batch["id"], prepared["review_digest"])
        self.assertEqual(sum(c["api_name"] == PUBLISH_API for c in self.fixture.calls), 1)
        self.assertEqual(sum(c[0] == "upload" for c in self.cloud.calls), 1)
        self.assertEqual(done["products"][0]["result"]["live_order_proof"], "not_established_by_this_check")

    def test_swapped_batch_receipts_and_changed_second_product_stop_before_any_publication(self):
        flow = self.multi_batch()
        batch = flow.plan(["career-kit", "excel-cleaning"])
        prepared = flow.prepare(batch["id"])
        with self.assertRaises(WorkflowStop):
            flow.finish(batch["id"], "0" * 64)
        commerce.source_file("excel-cleaning", "delivery", "示例.txt").write_text("after review", encoding="utf-8")
        with self.assertRaisesRegex(WorkflowStop, "已变化"):
            flow.finish(batch["id"], prepared["review_digest"])
        self.assertEqual(flow.load(batch["id"])["state"], "needs_attention")
        self.assertEqual(flow.load(batch["id"])["stopped_slug"], "excel-cleaning")
        for f in self.flows.values():
            f.finish.assert_not_called()

    def test_uncertain_first_publication_stops_before_second_and_resumes_same_receipt(self):
        flow = self.multi_batch()
        flow.interval = 0
        batch = flow.prepare(flow.plan(["career-kit", "excel-cleaning"])["id"])
        self.flows["career-kit"].finish.side_effect = WorkflowStop("response unknown", job_id="original-job")
        stopped = flow.finish(batch["id"], batch["review_digest"])
        self.assertEqual(stopped["state"], "needs_attention")
        self.assertEqual(stopped["job_id"], "original-job")
        self.flows["excel-cleaning"].finish.assert_not_called()
        self.flows["career-kit"].finish.side_effect = None
        done = flow.finish(batch["id"], batch["review_digest"])
        self.assertEqual(done["state"], "configured")
        self.assertEqual(self.flows["career-kit"].finish.call_args_list[0], self.flows["career-kit"].finish.call_args_list[1])

    def test_completed_first_product_is_retained_across_restart_and_publish_interval(self):
        flow = self.multi_batch()
        batch = flow.prepare(flow.plan(["career-kit", "excel-cleaning"])["id"])
        with patch("console.delivery_workflow.time.time", return_value=1000):
            waiting = flow.finish(batch["id"], batch["review_digest"])
        self.assertEqual(waiting["state"], "waiting_between_listings")
        self.assertEqual(waiting["wait_seconds"], 60)
        self.flows["excel-cleaning"].finish.assert_not_called()
        resumed = BatchWorkflow(self.fixture.store, self.backend, "a", flow_factory=flow.flow_factory)
        with patch("console.delivery_workflow.time.time", return_value=1061):
            done = resumed.finish(batch["id"], batch["review_digest"])
        self.assertEqual(done["state"], "configured")
        self.flows["career-kit"].finish.assert_called_once()
        self.flows["excel-cleaning"].finish.assert_called_once()

    def test_second_preflight_failure_does_not_publish_first(self):
        flow = self.multi_batch()
        batch = flow.prepare(flow.plan(["career-kit", "excel-cleaning"])["id"])
        self.flows["excel-cleaning"].validate_finish.side_effect = WorkflowStop("share mismatch")
        with self.assertRaisesRegex(WorkflowStop, "share mismatch"):
            flow.finish(batch["id"], batch["review_digest"])
        self.flows["career-kit"].finish.assert_not_called()

    def test_duplicate_content_service_and_account_mismatch_are_rejected(self):
        with self.assertRaises(WorkflowStop):
            self.batch.plan(["career-kit", "career-kit"])
        with self.assertRaises(WorkflowStop):
            self.batch.plan(["career-kit", "excel-cleaning"])
        batch = self.batch.plan(["career-kit"])
        with self.assertRaises(WorkflowStop):
            BatchWorkflow(self.fixture.store, self.backend, "other").prepare(batch["id"])
        flow = self.multi_batch()
        commerce.source_file("excel-cleaning", "delivery", "示例.txt").write_bytes(
            commerce.source_file("career-kit", "delivery", "示例.txt").read_bytes())
        with self.assertRaisesRegex(WorkflowStop, "交付内容相同"):
            flow.plan(["career-kit", "excel-cleaning"])
        self.assertEqual(self.cloud.calls, [])

    def test_saved_preview_tampering_with_unchanged_digest_is_rejected(self):
        plan = DeliveryWorkflow(self.fixture.store, self.backend, "a", "career-kit").prepare()
        preview = self.fixture.store.get("publish_preview", plan["preview_id"])
        preview["price_cents"] = 1
        self.fixture.store.put("publish_preview", preview["id"], preview, account="a")
        with self.assertRaisesRegex(WorkflowStop, "预览内容已变化"):
            DeliveryWorkflow(self.fixture.store, self.backend, "a", "career-kit").finish(
                plan["preview_id"], plan["digest"], plan["delivery_sha256"])
        self.assertEqual(self.fixture.store.rows("publication"), [])

    def test_parallel_batch_is_refused_before_upload_or_publish(self):
        batch = self.batch.plan(["career-kit"])
        with batch_lock(self.fixture.store):
            with self.assertRaisesRegex(WorkflowStop, "未并行提交"):
                self.batch.prepare(batch["id"])
        self.assertEqual(self.cloud.calls, [])


class BackendFailureTests(unittest.TestCase):
    def test_http_error_preserves_the_console_platform_block_reason(self):
        import io
        import urllib.error
        import json
        reason = "商品列表读取仍受平台校验限制；未重复请求。"
        error = urllib.error.HTTPError("http://127.0.0.1:8090/", 409, "Conflict", {},
            io.BytesIO(json.dumps({"message": reason, "code": "FAIL_SYS_ILLEGAL_ACCESS"}).encode()))
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(WorkflowStop, reason):
                Backend("a").request("POST", "/api/commerce/test/publish-preview", {})

    def test_wait_timeout_preserves_job_id_without_second_post(self):
        backend = Backend("a", wait_seconds=0)
        with patch.object(backend, "request", return_value={"state": "running", "id": "job-1"}) as call:
            with self.assertRaises(WorkflowStop) as caught:
                backend.job("/api/commerce/test/quark/prepare")
        self.assertEqual(caught.exception.job_id, "job-1")
        self.assertEqual(call.call_count, 1)

    def test_failed_job_with_null_result_has_meaningful_error(self):
        backend = Backend("a")
        with patch.object(backend, "request", return_value={"state": "failed", "id": "job-1", "result": None}):
            with self.assertRaisesRegex(WorkflowStop, "尚未核对"):
                backend.job("/api/commerce/test/quark/prepare")


if __name__ == "__main__":
    unittest.main()
