import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from console import quark, commerce
from public_fixtures import install_catalog
from console.app import create_app
from console.service import ConsoleService
from console.store import Store, product_key

ROOT = Path(__file__).resolve().parents[1]
TEMP = ROOT / "data/test-tmp"
TEMP.mkdir(parents=True, exist_ok=True)


class FakeCloud:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.files = []
        self.bytes = b""
        self.fail_upload = False
        self.fail_share = False
        self.corrupt = False
        self.extra_share_file = False
        self.wrong_share_fid = False
        self.account = "cloud-a"

    def status(self):
        return {"status": "connected", "account_fingerprint": self.account, "nickname": "测试账号"}

    def identity(self):
        return self.account

    def browse(self, fid):
        self.calls.append(("browse", fid))
        return list(self.files) if fid != "0" else [{"fid":"folder", "filename":"闲鱼交付-a", "file_type":"0"}]

    def download_hash(self, fid, name, digest):
        self.calls.append(("download", fid))
        actual = hashlib.sha256(self.bytes + (b"changed" if self.corrupt else b"")).hexdigest()
        if actual != digest:
            raise quark.QuarkError("HASH_MISMATCH", "云端内容与交付包不同")
        return actual

    def run(self, command, *args, **kwargs):
        self.calls.append((command, *args))
        if command == "create-folder":
            return {"data":{"fid":"folder"}, "rows":[]}
        if command == "upload":
            path = Path(args[0]); self.bytes = path.read_bytes()
            self.files = [{"fid":"file-1", "filename":path.name, "size":len(self.bytes), "file_type":"1"}]
            if self.fail_upload:
                raise quark.QuarkError("TIMEOUT", "结果丢失")
            return {"data":{"successCount":1, "fileCount":1, "fids":["file-1"]}, "rows":[]}
        if command == "share":
            if self.fail_share:
                raise quark.QuarkError("TIMEOUT", "分享响应丢失")
            return {"data":{"share_url":"https://pan.quark.cn/s/test123", "passcode":"aB12"}, "rows":[]}
        if command == "share-detail":
            files = [dict(f) for f in self.files]
            if self.wrong_share_fid:
                files[0]["fid"] = "different-file"
            if self.extra_share_file:
                files.append(dict(files[0], fid="extra"))
            return {"data":{"files":files, "file_count":len(files)}, "rows":[]}
        raise AssertionError(command)


class QuarkWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        install_catalog(self, self.temp.name)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "store.sqlite3")
        self.store.put("account", "a", {"id":"a"}, account="a")
        self.store.put("publication", product_key("a","career-kit"), {"account":"a", "state":"published", "item_id":"222"}, account="a")
        self.store.put("product", product_key("a","222"), {"account":"a", "item_id":"222", "title":"简历工具包", "managed":False, "is_multi_spec":False}, account="a")
        self.patch = patch.object(commerce, "DATA", self.root)
        self.patch.start()
        self.cli = FakeCloud(self.root / "quark")

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def prepare(self, **kwargs):
        return quark.prepare(self.store, "a", "career-kit", cli=self.cli, **kwargs)

    def test_package_upload_download_share_bind_and_repeat(self):
        row = self.prepare()
        self.assertEqual(row["state"], "verified")
        self.assertEqual(row["sha256"], row["download_sha256"])
        self.assertEqual(row["share_url"], "https://pan.quark.cn/s/test123?pwd=aB12")
        self.assertFalse(self.store.get("product", "a:222")["managed"])
        bound = quark.bind(self.store,"a","career-kit",row["sha256"],cli=self.cli)
        self.assertEqual(bound["item_id"], "222")
        self.assertTrue(self.store.get("product", "a:222")["managed"])
        rule = self.store.get("delivery_rule", "quark:a:career-kit")
        self.assertEqual(rule["item_id"], "222")
        self.assertIn(row["share_url"], self.store.get("card", rule["card_id"])["text_content"])
        self.prepare()
        self.assertEqual(sum(c[0]=="upload" for c in self.cli.calls), 1)
        self.assertEqual(sum(c[0]=="share" for c in self.cli.calls), 1)

    def test_service_sample_never_uploads_or_binds(self):
        with self.assertRaisesRegex(ValueError, "服务"):
            quark.prepare(self.store,"a","excel-cleaning",cli=self.cli)
        with self.assertRaisesRegex(ValueError, "数字成品"):
            quark.bind(self.store,"a","excel-cleaning","0"*64,cli=self.cli)
        self.assertEqual(self.cli.calls, [])

    def test_lost_upload_response_reconciles_without_second_upload(self):
        self.cli.fail_upload = True
        with self.assertRaises(quark.QuarkError): self.prepare()
        row = self.prepare(reconcile_only=True)
        self.assertEqual(row["state"], "uploaded")
        self.assertFalse(any(c[0]=="share" for c in self.cli.calls))
        self.cli.fail_upload = False
        self.assertEqual(self.prepare()["state"], "verified")
        self.assertEqual(sum(c[0]=="upload" for c in self.cli.calls), 1)

    def test_unseen_upload_result_stops_without_retry(self):
        self.cli.fail_upload = True
        with self.assertRaises(quark.QuarkError): self.prepare()
        self.cli.files = []
        with self.assertRaisesRegex(quark.QuarkError, "未自动重传"): self.prepare()
        self.assertEqual(sum(c[0]=="upload" for c in self.cli.calls), 1)

    def test_lost_share_response_requires_verified_existing_link(self):
        self.cli.fail_share = True
        with self.assertRaises(quark.QuarkError): self.prepare()
        with self.assertRaisesRegex(quark.QuarkError, "重复创建"): self.prepare()
        self.assertEqual(sum(c[0]=="share" for c in self.cli.calls), 1)
        self.cli.wrong_share_fid = True
        with self.assertRaises(quark.QuarkError):
            quark.adopt_share(self.store,"a","career-kit","https://pan.quark.cn/s/wrong",cli=self.cli)
        self.cli.wrong_share_fid = False
        row = quark.adopt_share(self.store,"a","career-kit","https://pan.quark.cn/s/test123?pwd=aB12",cli=self.cli)
        self.assertEqual(row["state"], "verified")

    def test_recovered_opaque_locator_resolves_by_both_downloaded_contents(self):
        row=self.prepare()
        row.update(state="sharing", fid="~signed-opaque|stable-fingerprint", recovered_via_browse=True)
        row.pop("share_url")
        self.store.put("quark_delivery","a:career-kit",row,account="a")
        result=quark.adopt_share(self.store,"a","career-kit","https://pan.quark.cn/s/test123",cli=self.cli)
        self.assertEqual(result["fid"],"file-1")
        self.assertEqual(result["recovered_file_locator"],"~signed-opaque|stable-fingerprint")
        self.assertEqual(sum(c[0]=="download" for c in self.cli.calls),2)

    def test_corrupt_remote_bytes_prevent_sharing(self):
        self.cli.corrupt = True
        with self.assertRaisesRegex(quark.QuarkError, "内容"): self.prepare()
        self.assertFalse(any(c[0]=="share" for c in self.cli.calls))
        self.assertEqual(self.store.rows("card"), [])

    def test_extra_file_in_share_prevents_binding(self):
        self.cli.extra_share_file = True
        with self.assertRaisesRegex(quark.QuarkError, "唯一"): self.prepare()
        self.assertEqual(self.store.rows("delivery_rule"), [])

    def test_cloud_account_change_does_not_reuse_or_overwrite(self):
        row = self.prepare()
        self.cli.account = "cloud-b"
        with self.assertRaisesRegex(ValueError, "夸克账号"): self.prepare()
        with self.assertRaisesRegex(ValueError, "账号不一致"):
            quark.bind(self.store,"a","career-kit",row["sha256"],cli=self.cli)
        self.assertFalse(self.store.get("product", "a:222")["managed"])

    def test_stale_confirmation_and_conflicting_rule_leave_old_configuration(self):
        row = self.prepare()
        with self.assertRaises(ValueError): quark.bind(self.store,"a","career-kit","0"*64,cli=self.cli)
        self.store.put("delivery_rule","existing",{"id":"existing","item_id":"222","enabled":True},account="a")
        with self.assertRaisesRegex(ValueError,"其他发货规则"):
            quark.bind(self.store,"a","career-kit",row["sha256"],cli=self.cli)
        self.assertEqual(self.store.rows("card"), [])

    def test_new_local_content_does_not_trigger_upload_from_bind(self):
        row = self.prepare()
        with patch.object(commerce,"build_bundles",return_value={"delivery_zip_sha256":"0"*64}):
            with self.assertRaisesRegex(ValueError,"已变化"):
                quark.bind(self.store,"a","career-kit",row["sha256"],cli=self.cli)
        self.assertEqual(sum(c[0]=="upload" for c in self.cli.calls), 1)

    def test_folder_creation_interruption_recovers_by_reading(self):
        self.store.put("quark_folder","a:cloud-a",{"state":"creating"},account="a")
        self.prepare()
        self.assertFalse(any(c[0]=="create-folder" for c in self.cli.calls))

    def test_cross_process_lock_rejects_concurrent_writes(self):
        with quark.operation_lock(self.store):
            code = "from pathlib import Path;from console.quark import operation_lock;from console.store import Store\nwith operation_lock(Store(Path(" + repr(str(self.store.path)) + "))): print('UNSAFE')"
            import sys
            p = subprocess.run([sys.executable,"-X","utf8","-B","-c",code],cwd=ROOT,capture_output=True,text=True,encoding="utf8")
            self.assertNotEqual(p.returncode, 0)
            self.assertNotIn("UNSAFE",p.stdout)
            self.assertIn("另一个夸克交付操作正在运行",p.stderr)

    def test_bound_cloud_card_only_delivers_for_verified_paid_order_once(self):
        import asyncio
        from console.messaging import MessagingRunner
        from test_owned_messaging import FakeTransport, paid_frame, buyer_frame
        self.store.set_setting("legacy_delivery_finalizations_import", {"status":"test","imported":0})
        row = self.prepare()
        quark.bind(self.store,"a","career-kit",row["sha256"],cli=self.cli)
        transport = FakeTransport()
        confirmations = []
        async def refresh_order(order_id): return self.store.get("order",order_id)
        async def confirm_delivery(order_id):
            confirmations.append(order_id)
            return {"status":"finalized","platform_success":True,"order_id":order_id}
        runner = MessagingRunner(self.store,"a",transport,self_user_id="seller",managed_item_ids={"222"},
                                 activation_authorized=True,order_refresher=refresh_order,confirm_delivery=confirm_delivery)
        runner.set_enabled(True,explicit_authorization=True)
        order={"order_id":"order-1","item_id":"222","buyer_id":"buyer-1","cookie_id":"a",
               "order_status":"unpaid","quantity":"1","sid":"cid-1@goofish"}
        self.store.put("order","order-1",order,account="a")
        async def exercise():
            await runner.process_frame(buyer_frame(message_id="pretend-paid",text="我已经付款了",item_id="222"))
            unpaid=await runner.process_frame(paid_frame(item_id="222"))
            self.assertNotEqual(unpaid[0]["status"],"confirmed")
            self.assertEqual(transport.sent,[])
            paid_order={**order,"order_id":"order-2","order_status":"paid"}
            self.store.put("order","order-2",paid_order,account="a")
            result=await runner.process_frame(paid_frame(order_id="order-2",item_id="222"))
            self.assertEqual(result[0]["status"],"confirmed")
            await runner.process_frame(paid_frame(order_id="order-2",item_id="222"))
        asyncio.run(exercise())
        self.assertEqual(len(transport.sent),1)
        self.assertIn(row["share_url"],transport.sent[0]["text"])
        self.assertEqual(confirmations,["order-2"])

    def test_api_rejects_missing_ack_service_sample_and_foreign_origin(self):
        service = ConsoleService(self.store, import_legacy=False)
        with TestClient(create_app(service)) as client:
            self.assertEqual(client.post("/api/commerce/career-kit/quark/prepare?account=a",json={}).status_code,400)
            self.assertEqual(client.post("/api/commerce/excel-cleaning/quark/prepare?account=a",json={"acknowledgment":"upload_this_buyer_package"}).status_code,400)
            self.assertEqual(client.post("/api/quark/connect?account=a",json={},headers={"origin":"https://evil.example"}).status_code,403)
            with patch.object(service,"submit",return_value={"id":"job"}) as submit:
                response=client.post("/api/commerce/career-kit/quark/bind?account=a",json={"sha256":"0"*64,"acknowledgment":"enable_this_verified_delivery"})
                self.assertEqual(response.status_code,200)
                submit.assert_called_once_with("quark_bind","a","career-kit",sha256="0"*64)


class CliContractTests(unittest.TestCase):
    def test_browse_uses_complete_artifact_and_rejects_preview_or_external_path(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as root:
            cli=quark.QuarkCLI(Path(root))
            path=cli.runtime/'codex/search/test/files.jsonl';path.parent.mkdir(parents=True)
            files=[{"fid":str(i),"filename":f"file-{i}"} for i in range(8)]
            path.write_text("\n".join(json.dumps(f) for f in files),encoding="utf8")
            result={"rows":[],"data":{"total":8,"file_list":files[:5]},"artifacts":[{"format":"jsonl","count":8,"file_path":str(path)}]}
            with patch.object(cli,"run",return_value=result):
                self.assertEqual(len(cli.browse("folder")),8)
                result['artifacts'][0]['count']=9
                with self.assertRaises(quark.QuarkError):cli.browse("folder")
                result['artifacts'][0]['file_path']=str(ROOT/'README.md')
                with self.assertRaises(quark.QuarkError):cli.browse("folder")
                result['artifacts']=[]
                with self.assertRaises(quark.QuarkError):cli.browse("folder")

    def test_runtime_change_and_account_change_stop_before_execution(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as root:
            cli=quark.QuarkCLI(Path(root))
            cli.entry.parent.mkdir(parents=True)
            cli.entry.write_text("tampered",encoding="utf8")
            with patch.object(quark.subprocess,"run") as run:
                with self.assertRaisesRegex(quark.QuarkError,"已核对版本"): cli.run("upload","file.zip")
                run.assert_not_called()
            cli.expected_identity="original-account"
            with patch.object(cli,"installed",return_value=True), patch.object(cli,"identity",return_value="changed-account"), patch.object(quark.subprocess,"run") as run:
                with self.assertRaisesRegex(quark.QuarkError,"账号在操作中发生变化"): cli.run("share","file-1")
                run.assert_not_called()

    def test_child_environment_does_not_inherit_unrelated_credentials(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as root:
            cli=quark.QuarkCLI(Path(root))
            with patch.dict(quark.os.environ,{"OPENAI_API_KEY":"must-not-inherit","CODEX_THREAD_ID":"private-task"}):
                env=cli.env()
            self.assertNotIn("OPENAI_API_KEY",env)
            self.assertNotIn("CODEX_THREAD_ID",env)
            for key in ("HOME","USERPROFILE","TEMP","TMP"):
                self.assertTrue(Path(env[key]).is_relative_to(Path(root)))

    def test_final_failure_overrides_progress_and_exit_zero(self):
        cli = quark.QuarkCLI()
        events = [{"type":"list","code":0,"data":{"fileId":"one"}},
                  {"type":"result","action":"upload","code":-204,"data":{"successCount":1,"fileCount":2}}]
        completed = SimpleNamespace(returncode=0, stdout="\n".join(json.dumps(e) for e in events))
        with patch.object(cli,"installed",return_value=True), patch.object(cli,"env",return_value={}), patch.object(quark.subprocess,"run",return_value=completed):
            with self.assertRaises(quark.QuarkError): cli.run("upload","example.zip")

    def test_untrusted_share_urls_rejected(self):
        for value in ["http://pan.quark.cn/s/abc","https://pan.quark.cn.evil.test/s/abc","https://evil.test/s/abc","https://pan.quark.cn:443/s/abc","https://pan.quark.cn/s/abc?pwd=<bad>"]:
            with self.assertRaises(ValueError): quark.share_url(value)

    def test_raw_errors_cannot_leak_tokens(self):
        cli = quark.QuarkCLI()
        result = {"type":"result","action":"login","code":-100,"msg":"access_token=private-secret"}
        completed = SimpleNamespace(returncode=1, stdout=json.dumps(result))
        with patch.object(cli,"installed",return_value=True), patch.object(cli,"env",return_value={}), patch.object(quark.subprocess,"run",return_value=completed):
            with self.assertRaises(quark.QuarkError) as caught: cli.run("login")
        self.assertNotIn("private-secret",str(caught.exception))


if __name__ == "__main__":
    unittest.main()
