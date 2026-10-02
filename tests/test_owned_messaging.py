from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import console.im_codec as im_codec
from console.im_codec import HistoryMessage, build_history_request, build_text_send_frame, decode_events, decode_history
from console.im_transport import EdgeImTransport
from console.messaging import MessagingError, MessagingRunner, import_legacy_finalizations
from console.order_detail import OrderDetailCache, OrderDetailError, extract_order_detail_evidence
from console.store import Store, product_key


TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / "data" / "test-tmp"
DEFAULT_CALLBACK = object()


def sync_frame(inner: dict) -> str:
    data = base64.b64encode(json.dumps(inner, ensure_ascii=False).encode("utf-8")).decode("ascii")
    return json.dumps({"body": {"syncPushPackage": {"data": [{"data": data}]}}}, ensure_ascii=False)


def sync_frames(*inners: dict) -> str:
    entries = [
        {"data": base64.b64encode(json.dumps(inner, ensure_ascii=False).encode("utf-8")).decode("ascii")}
        for inner in inners
    ]
    return json.dumps({"body": {"syncPushPackage": {"data": entries}}}, ensure_ascii=False)


def buyer_frame(
    message_id: str = "m-1", text: str = "你好", item_id: str = "item-1",
    *, created_ms: int | None = None, include_created: bool = True,
) -> str:
    message = {
        "2": "cid-1@goofish",
        "7": 2,
        "6": {"3": {"4": 1}},
        "10": {
            "senderUserId": "buyer-1",
            "senderNick": "买家甲",
            "reminderContent": text,
            "reminderUrl": f"https://www.goofish.com/item?id=x&itemId={item_id}",
            "bizTag": json.dumps({"messageId": message_id}),
        },
    }
    if include_created:
        message["5"] = int(created_ms if created_ms is not None else time.time() * 1000)
    return sync_frame({
        "1": {
            **message,
        }
    })


def paid_frame(order_id: str = "order-1", item_id: str = "item-1") -> str:
    return sync_frame({
        "1": "cid-1@goofish",
        "3": {"redReminder": "等待卖家发货", "orderId": order_id, "itemId": item_id},
    })


class FakeTransport:
    def __init__(self, *, confirm: bool = True, fail_send: bool = False, history_messages=None):
        self.confirm = confirm
        self.fail_send = fail_send
        self.history_messages = history_messages
        self.sent: list[dict] = []
        self.connected = False
        self.order_details = OrderDetailCache()

    async def connect(self):
        self.connected = True

    async def close(self):
        self.connected = False

    async def recv(self, *, timeout=1.0):
        await asyncio.sleep(min(float(timeout), 0.01))
        return None

    async def send_text(self, **payload):
        self.sent.append(payload)
        if self.fail_send:
            raise RuntimeError("connection changed during send")

    async def history(self, cid: str, *, limit: int = 50):
        if self.history_messages is not None:
            return list(self.history_messages)
        if not self.confirm or not self.sent:
            return []
        sent = self.sent[-1]
        return [HistoryMessage(cid, sent["self_user_id"], sent["text"], int(time.time() * 1000), "history-1")]

    def status(self):
        return {"ready": self.connected}


class QueueTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.frames: asyncio.Queue = asyncio.Queue()

    async def recv(self, *, timeout=1.0):
        try:
            return await asyncio.wait_for(self.frames.get(), timeout=float(timeout))
        except asyncio.TimeoutError:
            return None


class CodecAndEvidenceTests(unittest.TestCase):
    def test_decode_only_buyer_text_and_paid_system_event(self):
        buyer = decode_events(buyer_frame(), account="account", self_user_id="seller")
        self.assertEqual(len(buyer), 1)
        self.assertEqual(buyer[0].kind, "buyer_text")
        self.assertEqual(buyer[0].item_id, "item-1")
        self.assertEqual(buyer[0].message_id, "m-1")

        paid = decode_events(paid_frame(), account="account", self_user_id="seller")
        self.assertEqual(len(paid), 1)
        self.assertEqual((paid[0].kind, paid[0].order_id, paid[0].item_id), ("paid", "order-1", "item-1"))

        # Buyer text containing the same words remains buyer text, never a delivery trigger.
        buyer_words = decode_events(buyer_frame(text="等待卖家发货"), account="account", self_user_id="seller")
        self.assertEqual(buyer_words[0].kind, "buyer_text")

    def test_text_send_frame_is_scoped_to_buyer_and_self(self):
        frame = build_text_send_frame(cid="cid-1", recipient_id="buyer", self_user_id="seller", text="回复")
        self.assertEqual(frame["lwp"], "/r/MessageSend/sendByReceiverScope")
        self.assertEqual(frame["body"][0]["cid"], "cid-1@goofish")
        self.assertEqual(frame["body"][1]["actualReceivers"], ["buyer@goofish", "seller@goofish"])
        decoded = json.loads(base64.b64decode(frame["body"][0]["content"]["custom"]["data"]).decode("utf-8"))
        self.assertEqual(decoded, {"contentType": 1, "text": {"text": "回复"}})
        self.assertRegex(frame["headers"]["mid"], r"^\d{14,16} 0$")
        self.assertRegex(build_history_request("cid-1")["headers"]["mid"], r"^\d{14,16} 0$")

    def test_live_history_schema_uses_message_create_at_and_message_id(self):
        content = base64.b64encode(json.dumps({
            "contentType": 1, "text": {"text": "官方历史文本"},
        }, ensure_ascii=False).encode()).decode()
        body = {"userMessageModels": [{
            "readStatus": 1,
            "message": {
                "messageId": "live-history-id",
                "createAt": 1_800_000_000_123,
                "extension": {"senderUserId": "seller"},
                "content": {"custom": {"data": content}},
                "cid": "cid-1@goofish",
            },
        }]}
        decoded = decode_history(body, cid="cid-1")
        self.assertEqual(
            [(row.message_id, row.created_ms, row.sender_id, row.text) for row in decoded],
            [("live-history-id", 1_800_000_000_123, "seller", "官方历史文本")],
        )

    def test_fallback_ids_are_stable_across_sync_entry_reordering(self):
        first = json.loads(base64.b64decode(json.loads(buyer_frame(message_id=""))["body"]["syncPushPackage"]["data"][0]["data"]))
        second = json.loads(base64.b64decode(json.loads(buyer_frame(message_id="", text="第二条"))["body"]["syncPushPackage"]["data"][0]["data"]))
        left = {event.text: event.message_id for event in decode_events(sync_frames(first, second), account="a", self_user_id="s")}
        right = {event.text: event.message_id for event in decode_events(sync_frames(second, first), account="a", self_user_id="s")}
        self.assertEqual(left, right)

    def test_explicit_group_task_and_security_messages_are_rejected(self):
        for marker in ({"sessionType": 2}, {"taskId": "task-1"}, {"securityType": "risk"}):
            inner = json.loads(base64.b64decode(json.loads(buyer_frame())["body"]["syncPushPackage"]["data"][0]["data"]))
            inner["1"]["10"].update(marker)
            self.assertEqual(decode_events(sync_frame(inner), account="a", self_user_id="s"), [])

    def test_second_layer_messagepack_compatibility_path(self):
        inner = json.loads(base64.b64decode(json.loads(buyer_frame())["body"]["syncPushPackage"]["data"][0]["data"]))
        second_layer = base64.b64encode(b"packed").decode("ascii")
        outer = sync_frame({"unused": True})
        frame = json.loads(outer)
        frame["body"]["syncPushPackage"]["data"][0]["data"] = base64.b64encode(second_layer.encode()).decode()
        fake = SimpleNamespace(unpackb=lambda *_args, **_kwargs: inner)
        with patch.object(im_codec, "msgpack", fake):
            events = decode_events(frame, account="a", self_user_id="s")
        self.assertEqual([(row.kind, row.text) for row in events], [("buyer_text", "你好")])

    def test_order_detail_requires_explicit_matching_ids_and_named_specs(self):
        url = "https://h5api.m.goofish.com/h5/mtop.idle.web.trade.order.detail/1.0/"
        payload = {"data": {"orderId": "order-1", "itemId": "item-1", "buyerId": "buyer-1",
                            "quantity": 1, "skuInfo": [{"specName": "颜色", "specValue": "红色"}]}}
        evidence = extract_order_detail_evidence(
            url, payload, expected_order_id="order-1", expected_item_id="item-1",
        )
        self.assertEqual(evidence.specs, (("颜色", "红色"),))
        self.assertEqual(evidence.quantity, 1)
        evidence.validate(order_id="order-1", item_id="item-1", buyer_id="buyer-1")
        with self.assertRaises(OrderDetailError) as caught:
            extract_order_detail_evidence(url, payload, expected_order_id="another")
        self.assertEqual(caught.exception.code, "ORDER_MISMATCH")

    def test_seller_view_peer_is_buyer_only_with_explicit_seller_semantics(self):
        url = "https://h5api.m.goofish.com/h5/mtop.idle.web.trade.order.detail/1.0/"
        payload = {"data": {
            "orderId": "order-1", "itemId": "item-1", "seller": "true",
            "peerUserId": "buyer-1", "status": "4",
            "components": [{"data": {"itemInfo": {"buyAmount": "2"}}}],
        }}
        evidence = extract_order_detail_evidence(url, payload)
        self.assertEqual((evidence.buyer_id, evidence.quantity, evidence.status), ("buyer-1", 2, "4"))
        non_seller = {"data": {**payload["data"], "seller": "false"}}
        self.assertIsNone(extract_order_detail_evidence(url, non_seller).buyer_id)
        conflicting = {"data": {**payload["data"], "buyerId": "another"}}
        with self.assertRaises(OrderDetailError) as caught:
            extract_order_detail_evidence(url, conflicting)
        self.assertEqual(caught.exception.code, "BUYER_ID_CONFLICT")


class MessagingRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT, prefix="messaging-")
        self.store = Store(Path(self.temp.name) / "console.sqlite3")
        self.store.set_setting("legacy_delivery_finalizations_import", {"status": "test", "imported": 0})
        self.store.put("account", "account", {"id": "account", "user_id": "seller"}, account="account")
        self.store.put("product", product_key("account", "item-1"), {
            "account": "account", "item_id": "item-1", "title": "测试商品",
            "is_multi_spec": False, "multi_quantity_delivery": False,
        }, account="account")

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, transport=None, *, confirm_delivery=DEFAULT_CALLBACK, order_refresher=DEFAULT_CALLBACK, **kwargs):
        transport = transport or FakeTransport()
        if order_refresher is DEFAULT_CALLBACK:
            async def order_refresher(order_id):
                return self.store.get("order", order_id)
        if confirm_delivery is DEFAULT_CALLBACK:
            async def confirm_delivery(order_id):
                return {"status": "finalized", "order_id": order_id, "platform_success": True}
        runner = MessagingRunner(
            self.store, "account", transport, self_user_id="seller",
            managed_item_ids={"item-1"}, activation_authorized=True,
            order_refresher=order_refresher, confirm_delivery=confirm_delivery, **kwargs,
        )
        runner.set_enabled(True, explicit_authorization=True)
        return runner, transport

    async def test_default_disabled_blocks_lifecycle_without_connecting(self):
        transport = FakeTransport()
        runner = MessagingRunner(
            self.store, "account", transport, self_user_id="seller",
            managed_item_ids={"item-1"}, activation_authorized=False,
        )
        with self.assertRaises(MessagingError) as caught:
            await runner.start()
        self.assertEqual(caught.exception.code, "MESSAGING_DISABLED")
        self.assertFalse(transport.connected)

    async def test_legacy_local_user_id_is_not_used_as_platform_identity(self):
        self.store.put("account", "account", {"id": "account", "user_id": "legacy-local-owner"}, account="account")
        runner = MessagingRunner(
            self.store, "account", FakeTransport(), managed_item_ids={"item-1"},
            activation_authorized=True,
        )
        runner.set_enabled(True, explicit_authorization=True)
        with self.assertRaises(MessagingError) as caught:
            await runner.start()
        self.assertEqual(caught.exception.code, "ACCOUNT_ID_MISSING")

    async def test_enabling_runner_records_activation_before_accepting_buyer_text(self):
        runner = MessagingRunner(
            self.store, "account", FakeTransport(), self_user_id="seller",
            managed_item_ids={"item-1"}, activation_authorized=True,
        )
        self.assertIsNone(self.store.setting("messaging_activated_at:account"))
        status = runner.set_enabled(True, explicit_authorization=True)
        activated_at = self.store.setting("messaging_activated_at:account")
        self.assertIsInstance(activated_at, str)
        self.assertEqual(status["activated_at"], activated_at)
        self.assertTrue(status["enabled"])

    async def test_existing_enabled_install_backfills_activation_and_ignores_older_sync(self):
        self.store.set_setting("messaging_enabled:account", True)
        self.store.put("keyword", "k", {
            "cookie_id": "account", "keyword": "你好", "reply": "回复", "type": "text", "enabled": True,
        }, account="account")
        transport = FakeTransport()
        runner = MessagingRunner(
            self.store, "account", transport, self_user_id="seller",
            managed_item_ids={"item-1"}, activation_authorized=True,
        )
        activated_ms = runner._activation_ms()
        self.assertIsNotNone(activated_ms)
        result = await runner.process_frame(buyer_frame(created_ms=activated_ms - 1))
        self.assertEqual(result, [{"status": "no_action", "reason": "MESSAGE_BEFORE_ACTIVATION"}])
        self.assertEqual(transport.sent, [])

    async def test_buyer_text_missing_or_future_time_is_blocked_without_send(self):
        self.store.put("keyword", "k", {
            "cookie_id": "account", "keyword": "你好", "reply": "回复", "type": "text", "enabled": True,
        }, account="account")
        runner, transport = self.runner()
        missing = await runner.process_frame(buyer_frame(message_id="m-missing", include_created=False))
        future = await runner.process_frame(buyer_frame(
            message_id="m-future", created_ms=int(time.time() * 1000) + 600_000,
        ))
        self.assertEqual((missing[0]["status"], missing[0]["reason"]), ("blocked", "MESSAGE_TIME_MISSING"))
        self.assertEqual((future[0]["status"], future[0]["reason"]), ("blocked", "MESSAGE_TIME_IN_FUTURE"))
        self.assertEqual(transport.sent, [])

    async def test_control_frames_do_not_stop_runner_before_current_buyer_sync(self):
        self.store.put("keyword", "k", {
            "cookie_id": "account", "keyword": "你好", "reply": "回复", "type": "text", "enabled": True,
        }, account="account")
        transport = QueueTransport()
        runner, transport = self.runner(transport)
        await runner.start()
        try:
            controls = [
                json.dumps({"body": []}),
                json.dumps({"body": "heartbeat"}),
                json.dumps({"body": {"syncPushPackage": {"data": "not-a-list"}}}),
                json.dumps({"body": {"syncPushPackage": {
                    "data": [[], "not-an-object", {"data": []}, {"data": "not-base64"}],
                }}}),
            ]
            for frame in controls:
                await transport.frames.put(frame)
            await transport.frames.put(buyer_frame(message_id="m-after-controls"))
            deadline = asyncio.get_running_loop().time() + 2
            while not transport.sent and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            self.assertEqual([row["text"] for row in transport.sent], ["回复"])
            self.assertTrue(runner.status()["active"])
            self.assertIsNone(runner.status()["last_error"])
        finally:
            await runner.stop()

    async def test_keyword_reply_is_durable_confirmed_and_duplicate_suppressed(self):
        self.store.put("keyword", "k-global", {
            "cookie_id": "account", "keyword": "你好", "reply": "{send_user_name}，收到",
            "item_id": "", "type": "text", "enabled": True,
        }, account="account")
        self.store.put("keyword", "k-item", {
            "cookie_id": "account", "keyword": "你好", "reply": "商品专属回复",
            "item_id": "item-1", "type": "text", "enabled": True,
        }, account="account")
        runner, transport = self.runner()
        first = await runner.process_frame(buyer_frame())
        second = await runner.process_frame(buyer_frame())
        self.assertEqual(first[0]["status"], "confirmed")
        self.assertEqual(transport.sent[0]["text"], "商品专属回复")
        self.assertEqual(second, [])
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(runner.status()["outbox"], {"confirmed": 1})

    async def test_disabled_and_unsupported_keywords_do_not_send(self):
        self.store.put("keyword", "disabled", {
            "cookie_id": "account", "keyword": "你好", "reply": "不应发送", "type": "text", "enabled": False,
        }, account="account")
        self.store.put("keyword", "image", {
            "cookie_id": "account", "keyword": "图片", "reply": "", "type": "image", "enabled": True,
        }, account="account")
        runner, transport = self.runner()
        no_match = await runner.process_frame(buyer_frame(message_id="m-off", text="你好"))
        blocked = await runner.process_frame(buyer_frame(message_id="m-image", text="图片"))
        self.assertEqual(no_match[0]["status"], "no_action")
        self.assertIn("UNSUPPORTED_KEYWORD_TYPE", blocked[0]["reason"])
        self.assertEqual(transport.sent, [])

    async def test_ambiguous_send_is_never_automatically_retried(self):
        self.store.put("keyword", "k", {
            "cookie_id": "account", "keyword": "你好", "reply": "回复", "type": "text", "enabled": True,
        }, account="account")
        runner, transport = self.runner(FakeTransport(fail_send=True))
        first = await runner.process_frame(buyer_frame())
        duplicate = await runner.process_frame(buyer_frame())
        self.assertEqual(first[0]["status"], "ambiguous")
        self.assertEqual(duplicate, [])
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(runner.status()["outbox"], {"ambiguous": 1})

    async def test_history_before_attempt_is_rejected(self):
        self.store.put("keyword", "k", {
            "cookie_id": "account", "keyword": "你好", "reply": "回复", "type": "text", "enabled": True,
        }, account="account")
        runner, _transport = self.runner(FakeTransport(confirm=False))
        result = await runner.process_frame(buyer_frame())
        self.assertEqual(result[0]["status"], "sent_unconfirmed")
        with self.store.connect() as db:
            row = db.execute("SELECT id,attempted_at FROM message_outbox").fetchone()
        attempted_ms = int(datetime.fromisoformat(row["attempted_at"]).timestamp() * 1000)
        stale = [HistoryMessage("cid-1", "seller", "回复", attempted_ms - 1, "old")]
        self.assertEqual(await runner.reconcile_history(stale), 0)
        fresh = [HistoryMessage("cid-1", "seller", "回复", attempted_ms + 1, "new")]
        self.assertEqual(await runner.reconcile_history(fresh), 1)

    async def test_restart_periodically_reconciles_persisted_unconfirmed_outbox(self):
        self.store.put("keyword", "k", {
            "cookie_id": "account", "keyword": "你好", "reply": "回复", "type": "text", "enabled": True,
        }, account="account")
        first_runner, _first_transport = self.runner(FakeTransport(confirm=False))
        first = await first_runner.process_frame(buyer_frame())
        self.assertEqual(first[0]["status"], "sent_unconfirmed")
        with self.store.connect() as db:
            row = db.execute("SELECT attempted_at FROM message_outbox").fetchone()
        attempted_ms = int(datetime.fromisoformat(row["attempted_at"]).timestamp() * 1000)
        history = [HistoryMessage("cid-1", "seller", "回复", attempted_ms + 1, "restart-history")]
        restarted, _transport = self.runner(
            FakeTransport(history_messages=history), reconcile_interval=0.2,
        )
        await restarted.start()
        await asyncio.sleep(0.3)
        await restarted.stop()
        self.assertEqual(restarted.status()["outbox"], {"confirmed": 1})

    async def test_dead_process_owner_does_not_block_restart_during_old_lease(self):
        runner, _transport = self.runner()
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO messaging_runtime(account,owner_id,state,lease_until,heartbeat_at,owner_pid,owner_create_time) "
                "VALUES(?,?,'active',?,?,?,?)",
                ("account", "dead-owner", "2999-01-01T00:00:00+08:00", "2026-01-01T00:00:00+08:00", 2147483647, 1.0),
            )
        runner._acquire_lease()
        try:
            with self.store.connect() as db:
                row = db.execute("SELECT owner_id,owner_pid FROM messaging_runtime WHERE account='account'").fetchone()
            self.assertEqual((row["owner_id"], row["owner_pid"]), (runner.owner_id, runner.owner_pid))
        finally:
            runner._release_lease()

    async def test_live_process_owner_still_blocks_duplicate_runner(self):
        first, _transport = self.runner()
        second, _transport = self.runner()
        first._acquire_lease()
        try:
            with self.assertRaises(MessagingError) as caught:
                second._acquire_lease()
            self.assertEqual(caught.exception.code, "DUPLICATE_OWNER")
        finally:
            first._release_lease()

    async def test_only_paid_event_can_deliver_text_card(self):
        self.store.put("card", "5", {
            "id": 5, "name": "文本卡", "type": "text", "text_content": "交付内容", "enabled": True,
            "is_multi_spec": False,
        }, account="account")
        self.store.put("delivery_rule", "7", {
            "id": 7, "keyword": "测试商品", "card_id": 5, "delivery_count": 1, "enabled": True,
        }, account="account")
        self.store.put("order", "order-1", {
            "order_id": "order-1", "item_id": "item-1", "buyer_id": "buyer-1",
            "cookie_id": "account", "order_status": "paid", "quantity": "1", "sid": "cid-1@goofish",
        }, account="account")
        runner, transport = self.runner()
        buyer_words = await runner.process_frame(buyer_frame(message_id="buyer-paid-words", text="等待卖家发货"))
        self.assertEqual(buyer_words[0]["status"], "no_action")
        paid = await runner.process_frame(paid_frame())
        self.assertEqual(paid[0]["status"], "confirmed")
        self.assertEqual([row["text"] for row in transport.sent], ["交付内容"])
        self.assertEqual(runner.status()["finalizations"], {"finalized": 1})

    async def test_missing_quantity_never_defaults_to_one(self):
        self.store.put("card", "5", {
            "id": 5, "type": "text", "text_content": "交付内容", "enabled": True,
            "is_multi_spec": False,
        }, account="account")
        self.store.put("delivery_rule", "7", {
            "id": 7, "keyword": "测试商品", "card_id": 5, "delivery_count": 1, "enabled": True,
        }, account="account")
        self.store.put("order", "order-1", {
            "order_id": "order-1", "item_id": "item-1", "buyer_id": "buyer-1",
            "cookie_id": "account", "order_status": "paid",
        }, account="account")
        runner, transport = self.runner()
        result = await runner.process_frame(paid_frame())
        self.assertEqual((result[0]["status"], result[0]["reason"]), ("blocked", "ORDER_DETAIL_REQUIRED"))
        self.assertEqual(transport.sent, [])

    async def test_structured_detail_quantity_can_prove_missing_order_quantity(self):
        self.store.put("card", "5", {
            "id": 5, "type": "text", "text_content": "交付内容", "enabled": True,
            "is_multi_spec": False,
        }, account="account")
        self.store.put("delivery_rule", "7", {
            "id": 7, "keyword": "测试商品", "card_id": 5, "delivery_count": 1, "enabled": True,
        }, account="account")
        self.store.put("order", "order-1", {
            "order_id": "order-1", "item_id": "item-1", "buyer_id": "buyer-1",
            "cookie_id": "account", "order_status": "paid",
        }, account="account")
        transport = FakeTransport()
        transport.order_details.observe(
            "https://h5api.m.goofish.com/h5/mtop.idle.web.trade.order.detail/1.0/",
            {"data": {"orderId": "order-1", "itemId": "item-1", "seller": "true",
                      "peerUserId": "buyer-1", "status": "4", "quantity": 1}},
        )
        runner, transport = self.runner(transport)
        result = await runner.process_frame(paid_frame())
        self.assertEqual(result[0]["status"], "confirmed")
        self.assertEqual([row["text"] for row in transport.sent], ["交付内容"])

    async def test_multispec_without_observed_detail_is_visibly_blocked(self):
        product = self.store.get("product", product_key("account", "item-1"))
        product["is_multi_spec"] = True
        self.store.put("product", product_key("account", "item-1"), product, account="account")
        self.store.put("card", "5", {
            "id": 5, "type": "text", "text_content": "红色交付", "enabled": True,
            "is_multi_spec": True, "spec_name": "颜色", "spec_value": "红色",
        }, account="account")
        self.store.put("delivery_rule", "7", {
            "id": 7, "keyword": "测试商品", "card_id": 5, "delivery_count": 1, "enabled": True,
        }, account="account")
        self.store.put("order", "order-1", {
            "order_id": "order-1", "item_id": "item-1", "buyer_id": "buyer-1",
            "cookie_id": "account", "order_status": "paid", "quantity": 1,
        }, account="account")
        runner, transport = self.runner()
        result = await runner.process_frame(paid_frame())
        self.assertEqual(result[0]["reason"], "ORDER_DETAIL_REQUIRED")
        self.assertEqual(result[0]["status"], "blocked")
        self.assertEqual(transport.sent, [])

    async def test_multispec_exact_observed_detail_can_deliver(self):
        product = self.store.get("product", product_key("account", "item-1"))
        product["is_multi_spec"] = True
        self.store.put("product", product_key("account", "item-1"), product, account="account")
        self.store.put("card", "5", {
            "id": 5, "type": "text", "text_content": "红色交付", "enabled": True,
            "is_multi_spec": True, "spec_name": "颜色", "spec_value": "红色",
        }, account="account")
        self.store.put("delivery_rule", "7", {
            "id": 7, "keyword": "测试商品", "card_id": 5, "delivery_count": 1, "enabled": True,
        }, account="account")
        self.store.put("order", "order-1", {
            "order_id": "order-1", "item_id": "item-1", "buyer_id": "buyer-1",
            "cookie_id": "account", "order_status": "paid", "quantity": 1,
        }, account="account")
        transport = FakeTransport()
        transport.order_details.observe(
            "https://h5api.m.goofish.com/h5/mtop.idle.web.trade.order.detail/1.0/",
            {"data": {"orderId": "order-1", "itemId": "item-1", "buyerId": "buyer-1",
                      "skuInfo": [{"specName": "颜色", "specValue": "红色"}]}},
        )
        runner, transport = self.runner(transport)
        result = await runner.process_frame(paid_frame())
        self.assertEqual(result[0]["status"], "confirmed")
        self.assertEqual(transport.sent[0]["text"], "红色交付")

    async def test_three_same_item_rules_filter_by_exact_spec_before_ambiguity(self):
        product = self.store.get("product", product_key("account", "item-1"))
        product["is_multi_spec"] = True
        self.store.put("product", product_key("account", "item-1"), product, account="account")
        for index, value in enumerate(("蓝色", "红 色", "绿色"), start=1):
            self.store.put("card", str(index), {
                "id": index, "type": "text", "text_content": f"{value}交付", "enabled": True,
                "is_multi_spec": True, "spec_name": "颜 色", "spec_value": value,
            }, account="account")
            self.store.put("delivery_rule", str(index), {
                "id": index, "item_id": "item-1", "card_id": index,
                "delivery_count": 1, "enabled": True,
            }, account="account")
        self.store.put("order", "order-1", {
            "order_id": "order-1", "item_id": "item-1", "buyer_id": "buyer-1",
            "cookie_id": "account", "order_status": "paid", "quantity": 1,
        }, account="account")
        transport = FakeTransport()
        transport.order_details.observe(
            "https://h5api.m.goofish.com/h5/mtop.idle.web.trade.order.detail/1.0/",
            {"data": {"orderId": "order-1", "itemId": "item-1", "buyerId": "buyer-1",
                      "quantity": 1, "skuInfo": [{"specName": "颜色", "specValue": "红色"}]}},
        )
        refreshed = []
        async def refresh(order_id):
            refreshed.append(order_id)
            return self.store.get("order", order_id)
        runner, transport = self.runner(transport, order_refresher=refresh)
        result = await runner.process_frame(paid_frame())
        self.assertEqual(result[0]["status"], "confirmed")
        self.assertEqual(transport.sent[0]["text"], "红 色交付")
        self.assertEqual(refreshed, ["order-1", "order-1", "order-1"])

    async def test_platform_confirmation_runs_only_after_exact_history_match(self):
        self.store.put("card", "5", {
            "id": 5, "type": "text", "text_content": "交付内容", "enabled": True,
            "is_multi_spec": False,
        }, account="account")
        self.store.put("delivery_rule", "7", {
            "id": 7, "keyword": "测试商品", "card_id": 5, "delivery_count": 1, "enabled": True,
        }, account="account")
        self.store.put("order", "order-1", {
            "order_id": "order-1", "item_id": "item-1", "buyer_id": "buyer-1",
            "cookie_id": "account", "order_status": "paid", "quantity": 1,
        }, account="account")
        calls = []
        async def confirm(order_id):
            calls.append(order_id)
            return {"status": "finalized", "order_id": order_id, "platform_success": True}
        runner, _transport = self.runner(FakeTransport(confirm=False), confirm_delivery=confirm)
        result = await runner.process_frame(paid_frame())
        self.assertEqual(result[0]["status"], "sent_unconfirmed")
        self.assertEqual(calls, [])
        with self.store.connect() as db:
            row = db.execute("SELECT attempted_at FROM message_outbox").fetchone()
        attempted_ms = int(datetime.fromisoformat(row["attempted_at"]).timestamp() * 1000)
        history = [HistoryMessage("cid-1", "seller", "交付内容", attempted_ms + 1, "official")]
        self.assertEqual(await runner.reconcile_history(history), 1)
        self.assertEqual(calls, ["order-1"])
        self.assertEqual(runner.status()["finalizations"], {"finalized": 1})

    async def test_multi_quantity_confirms_platform_once_after_every_unit_history(self):
        product = self.store.get("product", product_key("account", "item-1"))
        product["multi_quantity_delivery"] = True
        self.store.put("product", product_key("account", "item-1"), product, account="account")
        self.store.put("card", "5", {
            "id": 5, "type": "text", "text_content": "单位交付", "enabled": True,
            "is_multi_spec": False,
        }, account="account")
        self.store.put("delivery_rule", "7", {
            "id": 7, "keyword": "测试商品", "card_id": 5, "delivery_count": 1, "enabled": True,
        }, account="account")
        self.store.put("order", "order-1", {
            "order_id": "order-1", "item_id": "item-1", "buyer_id": "buyer-1",
            "cookie_id": "account", "order_status": "paid", "quantity": 2,
        }, account="account")
        calls = []
        async def confirm(order_id):
            calls.append(order_id)
            return {"status": "finalized", "order_id": order_id, "platform_success": True}
        runner, transport = self.runner(FakeTransport(confirm=False), confirm_delivery=confirm)
        result = await runner.process_frame(paid_frame())
        self.assertEqual(result[0]["status"], "sent_unconfirmed")
        self.assertEqual([row["text"] for row in transport.sent], ["单位交付", "单位交付"])
        self.assertEqual(calls, [])
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT unit_index,total_units,attempted_at FROM message_outbox ORDER BY unit_index"
            ).fetchall()
        self.assertEqual([(row["unit_index"], row["total_units"]) for row in rows], [(1, 2), (2, 2)])
        attempted_ms = [int(datetime.fromisoformat(row["attempted_at"]).timestamp() * 1000) for row in rows]
        first = HistoryMessage("cid-1", "seller", "单位交付", min(attempted_ms) + 1, "unit-history-1")
        second = HistoryMessage("cid-1", "seller", "单位交付", max(attempted_ms) + 2, "unit-history-2")
        self.assertEqual(await runner.reconcile_history([first]), 1)
        self.assertEqual(calls, [])
        self.assertEqual(await runner.reconcile_history([first, second]), 1)
        self.assertEqual(calls, ["order-1"])
        self.assertEqual(runner.status()["finalizations"], {"finalized": 2})
        self.assertEqual(await runner.reconcile_history([first, second]), 0)
        self.assertEqual(calls, ["order-1"])

    async def test_message_confirmation_without_platform_proof_never_finalizes(self):
        self.store.put("card", "5", {
            "id": 5, "type": "text", "text_content": "交付内容", "enabled": True,
            "is_multi_spec": False,
        }, account="account")
        self.store.put("delivery_rule", "7", {
            "id": 7, "keyword": "测试商品", "card_id": 5, "delivery_count": 1, "enabled": True,
        }, account="account")
        self.store.put("order", "order-1", {
            "order_id": "order-1", "item_id": "item-1", "buyer_id": "buyer-1",
            "cookie_id": "account", "order_status": "paid", "quantity": 1,
        }, account="account")
        runner, _transport = self.runner(confirm_delivery=None)
        result = await runner.process_frame(paid_frame())
        self.assertEqual(result[0]["status"], "confirmed")
        self.assertEqual(runner.status()["outbox"], {"confirmed": 1})
        self.assertEqual(runner.status()["finalizations"], {"platform_unconfirmed": 1})
        self.assertFalse(runner._platform_confirmation_ok(
            {"status": "finalized", "order_id": "order-1", "platform_success": False}, "order-1",
        ))
        self.assertFalse(runner._platform_confirmation_ok(
            {"status": "finalized", "order_id": "another", "platform_success": True}, "order-1",
        ))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    class BridgePage:
        def __init__(self, socket_open=False):
            self.socket_open = socket_open
            self.send_evaluations = 0
            self.reloads = 0
        async def evaluate(self, script, *_args):
            if "ws.send(payload)" in script:
                self.send_evaluations += 1
            if "splice(0" in script:
                return []
            return self.socket_open
        async def reload(self, **_kwargs):
            self.reloads += 1

    async def test_disconnect_clears_ready_after_bounded_passive_recovery(self):
        page = self.BridgePage(socket_open=False)
        transport = EdgeImTransport(
            connect_timeout=2, reconnect_limit=1, reconnect_backoff=0.1, poll_interval=0.05,
        )
        transport._page = page
        transport._ready = True
        transport._connection_state = "connected"
        await transport._pump()
        status = transport.status()
        self.assertFalse(status["ready"])
        self.assertEqual((status["connection_state"], status["reconnect_attempts"]), ("blocked", 1))
        self.assertEqual(page.send_evaluations, 0)

    async def test_passive_socket_reconnect_restores_ready_without_sending_frames(self):
        page = self.BridgePage(socket_open=False)
        transport = EdgeImTransport(
            reconnect_limit=2, reconnect_backoff=0.1, poll_interval=0.05,
        )
        transport._page = page
        transport._ready = True
        transport._connection_state = "connected"
        task = asyncio.create_task(transport._pump())
        await asyncio.sleep(0.05)
        page.socket_open = True
        await asyncio.sleep(0.2)
        self.assertTrue(transport.status()["ready"])
        self.assertEqual(transport.status()["connection_state"], "connected")
        self.assertEqual(page.send_evaluations, 0)
        transport._closing = True
        await asyncio.wait_for(task, timeout=1)

    async def test_connect_registers_current_session_init_and_reloads_stale_v2_page(self):
        class Page:
            url = "https://www.goofish.com/im"
            def __init__(self):
                self.ready = False
                self.init_scripts = 0
                self.reloads = 0
                self.send_evaluations = 0
            async def evaluate(self, script, *_args):
                if "ws.send(payload)" in script:
                    self.send_evaluations += 1
                if script.startswith("Number("):
                    return 2  # Stale document state from the previous CDP session.
                if "splice(0" in script:
                    return []
                return self.ready
            async def add_init_script(self, _script):
                self.init_scripts += 1
            async def reload(self, **_kwargs):
                self.reloads += 1
                self.ready = True

        page = Page()
        context = SimpleNamespace(pages=[page], on=lambda *_args: None)
        browser = SimpleNamespace(contexts=[context])
        class Chromium:
            async def connect_over_cdp(self, *_args, **_kwargs):
                return browser
        class Playwright:
            chromium = Chromium()
            async def stop(self):
                return None
        class Starter:
            async def start(self):
                return Playwright()
        fake_async_api = SimpleNamespace(async_playwright=lambda: Starter())
        transport = EdgeImTransport(
            prepare_page=True, connect_timeout=2, operation_timeout=1, poll_interval=0.05,
        )
        with patch.dict(sys.modules, {
            "playwright": SimpleNamespace(async_api=fake_async_api),
            "playwright.async_api": fake_async_api,
        }):
            await transport.connect()
        try:
            self.assertTrue(transport.status()["ready"])
            self.assertEqual((page.init_scripts, page.reloads), (1, 1))
            self.assertEqual(page.send_evaluations, 0)
        finally:
            await transport.close()

    async def test_reconnect_waits_for_post_reload_handshake_without_fast_reload_loop(self):
        class DelayedReadyPage(self.BridgePage):
            def __init__(self):
                super().__init__(socket_open=False)
                self.ready_handle = None
                self.reload_times = []
            async def reload(self, **_kwargs):
                self.reloads += 1
                self.reload_times.append(asyncio.get_running_loop().time())
                self.socket_open = False
                if self.ready_handle:
                    self.ready_handle.cancel()
                self.ready_handle = asyncio.get_running_loop().call_later(
                    0.35, setattr, self, "socket_open", True,
                )

        page = DelayedReadyPage()
        transport = EdgeImTransport(
            prepare_page=True, connect_timeout=2, operation_timeout=1,
            reconnect_limit=3, reconnect_backoff=0.1, poll_interval=0.05,
        )
        transport._page = page
        started = asyncio.get_running_loop().time()
        recovered = await transport._recover_socket()
        elapsed = asyncio.get_running_loop().time() - started
        if page.ready_handle:
            page.ready_handle.cancel()
        self.assertTrue(recovered)
        self.assertTrue(transport.status()["ready"])
        self.assertEqual(page.reloads, 1)
        self.assertGreaterEqual(elapsed, 0.35)
        self.assertEqual(page.send_evaluations, 0)

    async def test_order_detail_fetch_uses_and_closes_one_temporary_tab(self):
        response = SimpleNamespace(
            url="https://h5api.m.goofish.com/h5/mtop.idle.web.trade.order.detail/1.0/",
            json=lambda: None,
        )
        async def response_json():
            return {"data": {"orderId": "order-1", "itemId": "item-1", "buyerId": "buyer-1", "quantity": 1}}
        response.json = response_json

        class FakePage:
            def __init__(self):
                self.handler = None
                self.closed = False
                self.url = None
            def on(self, _name, handler):
                self.handler = handler
            def remove_listener(self, _name, _handler):
                self.handler = None
            async def goto(self, url, **_kwargs):
                self.url = url
                self.handler(response)
                await asyncio.sleep(0)
            async def close(self):
                self.closed = True

        page = FakePage()
        class FakeContext:
            async def new_page(self):
                return page
        transport = EdgeImTransport(operation_timeout=1)
        transport._ready = True
        transport._page = SimpleNamespace(context=FakeContext())
        evidence = await transport.fetch_order_detail("order-1", "item-1", "buyer-1")
        self.assertEqual((evidence.order_id, evidence.quantity), ("order-1", 1))
        self.assertIn("orderId=order-1", page.url)
        self.assertTrue(page.closed)


class LegacyFinalizationTests(unittest.TestCase):
    def test_read_only_legacy_finalized_rows_prevent_redelivery(self):
        TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT, prefix="legacy-final-") as temp:
            root = Path(temp)
            legacy = root / "legacy.sqlite3"
            db = sqlite3.connect(legacy)
            db.execute("""CREATE TABLE delivery_finalization_states (
                order_id TEXT, unit_index INTEGER, cookie_id TEXT, item_id TEXT,
                buyer_id TEXT, status TEXT, updated_at TEXT)""")
            db.execute("INSERT INTO delivery_finalization_states VALUES(?,?,?,?,?,?,?)",
                       ("order-old", 1, "account", "item-1", "buyer-1", "finalized", "2026-09-20T00:00:00+08:00"))
            db.commit()
            db.close()
            original = legacy.read_bytes()
            store = Store(root / "owned.sqlite3")
            result = import_legacy_finalizations(store, legacy)
            self.assertEqual(result["imported"], 1)
            self.assertEqual(legacy.read_bytes(), original)
            with store.connect() as owned:
                row = owned.execute("SELECT status,source FROM delivery_finalizations").fetchone()
            self.assertEqual((row["status"], row["source"]), ("finalized", "legacy_read_only_import"))


if __name__ == "__main__":
    unittest.main()
