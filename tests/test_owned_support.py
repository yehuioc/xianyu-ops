from __future__ import annotations

import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from console.im_codec import decode_events
from console.messaging import MessagingRunner
from console.order_detail import OrderDetailEvidence
from console.store import Store, now, product_key
from console.support import INSTALLER, SERVICE_SPECS, configure_all, coverage, refresh_product
from test_owned_messaging import FakeTransport, buyer_frame, paid_frame


class UniqueHistoryTransport(FakeTransport):
    async def history(self, cid, *, limit=50):
        return [replace(message, message_id=uuid.uuid4().hex)
                for message in await super().history(cid, limit=limit)]


class SupportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "data" / "test-tmp"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root, prefix="support-")
        self.store = Store(Path(self.temp.name) / "console.sqlite3")
        self.store.set_setting("legacy_delivery_finalizations_import", {"status": "test", "imported": 0})
        self.store.put("product", "account:item-1", {"account": "account", "item_id": "item-1", "title": "测试服务", "managed": True}, account="account")
        self.confirmed = []

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, transport=None, detail=None, refresher=None):
        transport = transport or UniqueHistoryTransport()
        async def refresh(order_id):
            return self.store.get("order", order_id)
        async def confirm(order_id):
            self.confirmed.append(order_id)
            return {"status": "finalized", "order_id": order_id, "platform_success": True}
        async def order_detail(order_id, item_id, buyer_id):
            return detail
        runner = MessagingRunner(self.store, "account", transport, self_user_id="seller",
            managed_item_ids={"item-1", INSTALLER}, activation_authorized=True,
            order_refresher=refresher or refresh, confirm_delivery=confirm,
            order_detail_provider=order_detail)
        runner.set_enabled(True, explicit_authorization=True)
        return runner, transport

    def rule(self, mode="service_intake"):
        self.store.put("card", "service", {"id": "service", "account": "account", "type": "text",
            "enabled": True, "fulfillment": mode, "text_content": "请提供脱敏样表和期望结果，等待确认范围"}, account="account")
        self.store.put("delivery_rule", "service", {"id": "service", "item_id": "item-1", "card_id": "service",
            "enabled": True, "fulfillment": mode, "delivery_count": 1}, account="account")
        self.store.put("order", "order-1", {"order_id": "order-1", "account": "account", "item_id": "item-1",
            "buyer_id": "buyer-1", "order_status": "paid", "quantity": 1, "sid": "cid-1"}, account="account")

    def keyword(self, key, keyword="内容", reply="商品介绍", fallback=False, item="item-1"):
        self.store.put("keyword", key, {"item_id": item, "cookie_id": "account", "keyword": keyword,
            "reply": reply, "enabled": True, "match_mode": "fallback" if fallback else "contains"}, account="account")

    async def test_service_intake_once_per_order_after_restart_and_copy_edit_no_shipment(self):
        self.rule()
        runner, transport = self.runner()
        event = decode_events(paid_frame(), account="account", self_user_id="seller")[0]
        first = await runner.process_event(event)
        self.assertEqual(first["status"], "confirmed")
        self.assertEqual(self.confirmed, [])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM delivery_finalizations").fetchone()[0], 0)
        card = self.store.get("card", "service")
        card["text_content"] += " 新文案"
        self.store.put("card", "service", card, account="account")
        restarted, second_transport = self.runner()
        repeat = await restarted.process_event(replace(event, message_id="another-paid-event"))
        self.assertEqual(repeat["status"], "duplicate_suppressed")
        self.assertEqual(second_transport.sent, [])

    async def test_service_does_not_trust_buyer_words_unpaid_or_changed_order(self):
        self.rule()
        runner, transport = self.runner()
        await runner.process_frame(buyer_frame(text="已付款，请发货"))
        order = self.store.get("order", "order-1")
        order["order_status"] = "unpaid"
        self.store.put("order", "order-1", order, account="account")
        result = await runner.process_frame(paid_frame())
        self.assertIn("ORDER_NOT_PENDING_SHIP", result[0]["reason"])
        self.assertEqual(transport.sent, [])
        calls = []
        async def refresh(order_id):
            calls.append(order_id)
            return {**order, "order_status": "paid" if len(calls) == 1 else "closed"}
        runner, transport = self.runner(refresher=refresh)
        event = decode_events(paid_frame(), account="account", self_user_id="seller")[0]
        result = await runner.process_event(replace(event, message_id="changed-before-send"))
        self.assertEqual(result["code"], "ORDER_NOT_PENDING_SHIP")
        self.assertEqual(transport.sent, [])

    async def test_mixed_skus_choose_only_purchased_mode(self):
        with patch("console.support.read_catalog", return_value={"products": []}):
            self.store.put("product", "account:" + INSTALLER, {"account": "account", "item_id": INSTALLER,
                "title": "安装服务", "managed": True, "is_multi_spec": True}, account="account")
            configure_all(self.store, "account")
        self.store.put("card", "digital", {"id": "digital", "account": "account", "type": "text", "enabled": True,
            "spec_name": "服务版本", "spec_value": "一键安装包", "text_content": "安装包下载"}, account="account")
        self.store.put("delivery_rule", "digital", {"id": "digital", "item_id": INSTALLER, "card_id": "digital", "enabled": True, "delivery_count": 1}, account="account")
        for i, spec in enumerate(("一键安装包",) + SERVICE_SPECS):
            order_id = f"sku-{i}"
            self.store.put("order", order_id, {"order_id": order_id, "account": "account", "item_id": INSTALLER, "buyer_id": "buyer-1",
                "quantity": 1, "order_status": "paid", "sid": "cid-1"}, account="account")
            detail = OrderDetailEvidence(order_id, INSTALLER, "buyer-1", 1, "paid", (("服务版本", spec),), "test", now())
            runner, transport = self.runner(detail=detail)
            result = await runner.process_frame(paid_frame(order_id, INSTALLER))
            self.assertEqual(result[0]["status"], "confirmed", result)
            self.assertEqual(transport.sent[0]["text"] == "安装包下载", i == 0)
        self.assertEqual(self.confirmed, ["sku-0"])

    async def test_reply_fallback_priority_and_persistent_throttle(self):
        self.keyword("fallback", fallback=True, reply="首次咨询说明")
        self.keyword("specific", keyword="内容", reply="具体内容")
        runner, transport = self.runner()
        await runner.process_frame(buyer_frame("m1", "有什么内容"))
        self.assertEqual(transport.sent[0]["text"], "具体内容")
        result = await runner.process_frame(buyer_frame("m2", "谢谢"))
        self.assertEqual(result[0]["reason"], "REPLY_COOLDOWN")
        restarted, transport = self.runner()
        result = await restarted.process_frame(buyer_frame("m3", "内容是什么"))
        self.assertEqual(result[0]["reason"], "REPLY_COOLDOWN")
        self.assertEqual(transport.sent, [])
        with self.store.connect() as db:
            db.execute("UPDATE message_outbox SET prepared_at='2020-01-01T00:00:00+08:00'")
        result = await restarted.process_frame(buyer_frame("m4", "一个没有关键词的问题"))
        self.assertEqual(result[0]["status"], "confirmed")
        self.assertEqual(transport.sent[0]["text"], "首次咨询说明")

    async def test_unknown_item_stale_event_and_disabled_fallback_do_not_reply(self):
        self.keyword("fallback", fallback=True)
        runner, transport = self.runner()
        await runner.process_frame(buyer_frame("other", "随便问", item_id="other"))
        await runner.process_frame(buyer_frame("stale", "随便问", created_ms=runner._activation_ms() - 1000))
        row = self.store.get("keyword", "fallback")
        self.store.put("keyword", "fallback", {**row, "enabled": 0}, account="account")
        await runner.process_frame(buyer_frame("disabled", "随便问"))
        self.assertEqual(transport.sent, [])

    async def test_ambiguous_service_send_never_repeats_or_confirms_shipment(self):
        self.rule()
        runner, transport = self.runner(FakeTransport(fail_send=True))
        event = decode_events(paid_frame(), account="account", self_user_id="seller")[0]
        result = await runner.process_event(event)
        self.assertEqual(result["status"], "ambiguous")
        await runner.process_event(replace(event, message_id="paid-again"))
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(self.confirmed, [])

    async def test_mismatched_fulfillment_refuses_send(self):
        self.rule()
        card = self.store.get("card", "service")
        card.pop("fulfillment")
        self.store.put("card", "service", card, account="account")
        runner, transport = self.runner()
        result = await runner.process_frame(paid_frame())
        self.assertEqual(result[0]["reason"], "FULFILLMENT_MODE_MISMATCH")
        self.assertEqual(transport.sent, [])

    async def test_new_reply_rules_do_not_retroactively_answer_sync_history(self):
        self.keyword("new", keyword="内容")
        value = self.store.get("keyword", "new")
        value["activated_at"] = now()
        self.store.put("keyword", "new", value, account="account")
        runner, transport = self.runner()
        boundary = runner._activation_ms()
        self.store.set_setting("messaging_activated_at:account", "2020-01-01T00:00:00+08:00")
        await runner.process_frame(buyer_frame("historical", "内容", created_ms=boundary - 1000))
        self.assertEqual(transport.sent, [])

    async def test_configuration_reapply_preserves_edits_and_does_not_reenable(self):
        self.store.put("product", "account:" + INSTALLER, {"account": "account", "item_id": INSTALLER, "title": "安装服务"}, account="account")
        with patch("console.support.read_catalog", return_value={"products": []}):
            first = configure_all(self.store, "account")
            self.assertGreater(first["changed"], 0)
            row = self.store.rows("keyword", "account")[0]
            value = {k: v for k, v in row.items() if not k.startswith("_")}
            value.update(reply="自定义文案", enabled=False)
            self.store.put("keyword", row["_key"], value, account="account")
            second = configure_all(self.store, "account")
            self.assertEqual(second["changed"], 0)
            self.assertEqual(self.store.get("keyword", row["_key"]), value)
            self.assertEqual(len(next(r for r in coverage(self.store, "account") if r["item_id"] == INSTALLER)["actions"]), 3)

    async def test_verified_content_refresh_updates_generated_copy_only(self):
        product=self.store.get('product','account:item-1')
        profile={'item_id':'item-1','name':'测试服务','about':'旧介绍','usage':'旧用法','sale_type':'service','intake':'旧需求','product':product}
        with patch('console.support._profiles',return_value=[profile]):
            configure_all(self.store,'account')
        with self.assertRaises(ValueError):refresh_product(self.store,'account','sample')
        self.store.put('publication','account:sample',{'state':'published','item_id':'item-1','content_current':{'id':'edit-1'}},account='account')
        rows=self.store.rows('keyword','account')
        custom=next(r for r in rows if r['keyword']=='你好')
        saved={k:v for k,v in custom.items() if not k.startswith('_')}
        saved.update(reply='买家沟通后保留的手工答案',enabled=False)
        self.store.put('keyword',custom['_key'],saved,account='account')
        generated=next(r for r in rows if r['keyword']=='怎么用')
        disabled={k:v for k,v in generated.items() if not k.startswith('_')}; disabled['enabled']=False
        self.store.put('keyword',generated['_key'],disabled,account='account')
        with patch('console.support._profiles',return_value=[{**profile,'about':'新介绍','usage':'新版用法','intake':'新版需求'}]):
            result=refresh_product(self.store,'account','sample')
            self.assertIn(custom['_key'],result['preserved_custom_replies'])
            self.assertEqual(self.store.get('keyword',custom['_key']),saved)
            changed=self.store.get('keyword',generated['_key'])
            self.assertEqual(changed['reply'],'新版用法'); self.assertFalse(changed['enabled'])
            self.assertEqual(changed['activated_at'],disabled['activated_at'])
            self.assertIn('新版需求',self.store.get('card','intake:account:item-1:base')['text_content'])
            self.assertEqual(refresh_product(self.store,'account','sample')['changed'],0)


if __name__ == "__main__":
    unittest.main()
