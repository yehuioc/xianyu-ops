"""Pinned official CLI adapter and verified buyer-package delivery workflow."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode

from . import commerce
from .paths import DATA, PROJECT
from .store import Store, now, product_key

PACKAGE_URL = "https://pdds.quark.cn/download/stfile/uu66xuuuuuvyuw8wx/quarkclouddrive-1.0.20.zip"
PACKAGE_HASH = "4e57e4328c80cf539caf518d6e773b4efee4a096f9822bf46acd0201762cfc24"
SCRIPT_HASHES = {
    "quark-drive.cjs": "39aa12702181ef3736f61bfb3159a04dfe88371163fc1f53f1b9c22efa38be9f",
    "hash-worker.cjs": "c9e76fa0fd418e731980636c13bfe1379bdb60e117f1d6ed7dcf83ff2187a690",
}
LOCK = threading.RLock()


@contextmanager
def operation_lock(store):
    """A process crash releases this lock; persisted write intents remain for reconciliation."""
    path = store.path.parent / ".quark-operation.lock"
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise QuarkError("BUSY", "另一个夸克交付操作正在运行，请等待当前任务结束") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class QuarkError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)


def share_url(value: str, passcode: str = "") -> str:
    p = urlparse(str(value))
    if p.scheme != "https" or p.netloc != "pan.quark.cn" or not re.fullmatch(r"/s/[A-Za-z0-9]+", p.path):
        raise ValueError("需要有效的夸克分享链接")
    code = passcode or parse_qs(p.query).get("pwd", [""])[0]
    if code and not re.fullmatch(r"[A-Za-z0-9]{4,12}", code):
        raise ValueError("夸克提取码格式不正确")
    return "https://pan.quark.cn" + p.path + ("?" + urlencode({"pwd": code}) if code else "")


class QuarkCLI:
    def __init__(self, root: Path | None = None):
        self.root = (root or DATA / "quark").resolve()
        if not self.root.is_relative_to(PROJECT):
            raise ValueError("夸克运行资料必须保存在本项目内")
        self.runtime = self.root / "runtime"
        self.entry = self.runtime / "scripts" / "quark-drive.cjs"

    def installed(self) -> bool:
        return all((self.entry.parent / name).is_file() and commerce.sha256(self.entry.parent / name) == digest
                   for name, digest in SCRIPT_HASHES.items())

    def install(self) -> dict:
        if self.installed():
            return {"status": "installed", "version": "1.0.20-5be3987"}
        self.runtime.mkdir(parents=True, exist_ok=True)
        archive = self.runtime / "official-1.0.20.zip"
        if not archive.is_file() or commerce.sha256(archive) != PACKAGE_HASH:
            with urllib.request.urlopen(PACKAGE_URL, timeout=45) as response:
                raw = response.read(5_000_001)
            if len(raw) > 5_000_000 or hashlib.sha256(raw).hexdigest() != PACKAGE_HASH:
                raise QuarkError("PACKAGE_CHANGED", "夸克工具包与已核对版本不一致，未安装")
            archive.write_bytes(raw)
        with zipfile.ZipFile(archive) as z:
            for name, digest in SCRIPT_HASHES.items():
                raw = z.read("scripts/" + name)
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise QuarkError("PACKAGE_CHANGED", "夸克工具文件校验失败")
                target = self.entry.parent / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
        return {"status": "installed", "version": "1.0.20-5be3987"}

    def env(self) -> dict:
        keys = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "PROGRAMFILES", "PROGRAMFILES(X86)",
                "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "LANG", "LC_ALL"}
        env = {k: v for k, v in os.environ.items() if k.upper() in keys}
        profile = self.root / "profile"
        profile.mkdir(parents=True, exist_ok=True)
        for key in ("HOME", "USERPROFILE", "TEMP", "TMP", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
            env[key] = str(profile)
        env["CODEX_SHELL"] = "1"
        env["NO_COLOR"] = "1"
        return env

    def run(self, command: str, *args: str, timeout: int = 120) -> dict:
        if command not in {"get-user-info", "login", "browse", "create-folder", "upload", "share", "share-detail", "download"}:
            raise ValueError("不支持的夸克操作")
        if not self.installed():
            raise QuarkError("NOT_INSTALLED", "请先安装已核对版本的夸克工具")
        expected = getattr(self, "expected_identity", None)
        if expected and self.identity() != expected:
            raise QuarkError("ACCOUNT_CHANGED", "夸克账号在操作中发生变化，已停止后续操作")
        node = shutil.which("node")
        if not node:
            raise QuarkError("NODE_MISSING", "未找到 Node.js")
        try:
            completed = subprocess.run([node, str(self.entry), command, *map(str, args)],
                cwd=self.root, env=self.env(), capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise QuarkError("TIMEOUT", "夸克操作超时；写入结果需要回读核对，不会自动重发") from None
        events = []
        for line in completed.stdout.splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
            except ValueError:
                continue
        results = [e for e in events if e.get("type") == "result"]
        result = results[-1] if results else {}
        code = result.get("code")
        if command == "login" and code == -118:
            return {"data": {"already_authorized": True}, "rows": []}
        if completed.returncode != 0 or code != 0 or result.get("action") != command:
            message = "夸克操作未成功（" + str(code if code is not None else "无完整结果") + "）；原配置保留"
            if code in {-103, -1408}:
                message = "夸克尚未授权或授权已过期，请连接夸克账号"
            elif command == "login":
                message = "授权尚未完成；请在已打开页面扫码，完成后可在后台填写一次性授权码"
            raise QuarkError(str(code), message)
        if any(e.get("code", 0) != 0 for e in events if e.get("type") == "list"):
            raise QuarkError("PARTIAL", "夸克仅完成部分操作，需要回读核对")
        return {"data": result.get("data") or {}, "rows": [e.get("data") or {} for e in events if e.get("type") == "list"],
                "artifacts": [e.get("data") or {} for e in events if e.get("type") == "artifact" and e.get("code") == 0 and e.get("action") == command]}

    def identity(self) -> str:
        # Stable account identity, never the access/refresh token. Do not expose the config.
        p = self.runtime / "codex" / "config.json"
        try:
            config = json.loads(p.read_text(encoding="utf-8"))
            uid = config["agent_auth"]["codex"]["userId"]
            if not isinstance(uid, str) or not uid:
                raise ValueError()
            return hashlib.sha256(uid.encode()).hexdigest()
        except (OSError, ValueError, KeyError, TypeError):
            raise QuarkError("IDENTITY_MISSING", "未能核对夸克授权账号，请重新连接") from None

    def status(self) -> dict:
        if not self.installed():
            return {"status": "not_installed", "message": "夸克工具尚未安装"}
        try:
            result = self.run("get-user-info", timeout=35)["data"]
            return {"status": "connected", "nickname": result.get("userInfo", {}).get("nickname", "已授权账号"),
                    "account_fingerprint": self.identity(), "version": "1.0.20-5be3987", "checked_at": now()}
        except QuarkError as exc:
            return {"status": "blocked", "message": str(exc), "code": exc.code, "checked_at": now()}

    def browse(self, fid: str) -> list[dict]:
        result = self.run("browse", "--parent-fid", fid, "--all")
        artifacts = result.get("artifacts", [])
        if len(artifacts) != 1 or artifacts[0].get("format") != "jsonl":
            raise QuarkError("BROWSE_INCOMPLETE", "夸克未返回完整目录结果，不能据预览判断文件不存在")
        artifact = artifacts[0]
        path = Path(artifact.get("file_path", "")).resolve()
        allowed = (self.runtime / "codex" / "search").resolve()
        if not path.is_relative_to(allowed) or path.suffix != ".jsonl" or not path.is_file() or path.stat().st_size > 50_000_000:
            raise QuarkError("BROWSE_ARTIFACT", "夸克完整目录文件不在预期位置，已停止读取")
        try:
            files = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (ValueError, OSError):
            raise QuarkError("BROWSE_INCOMPLETE", "夸克完整目录无法读取，不能继续上传") from None
        if (isinstance(artifact.get("count"), bool) or artifact.get("count") != len(files)
                or any(not isinstance(f, dict) or not isinstance(f.get("fid"), str) or not f["fid"] for f in files)):
            raise QuarkError("BROWSE_INCOMPLETE", "夸克完整目录的内容与条数不一致")
        return files

    def download_hash(self, fid: str, name: str, digest: str) -> str:
        target = self.root / "verification" / digest
        target.mkdir(parents=True, exist_ok=True)
        self.run("download", "--fid", fid, "--output-dir", str(target), "--overwrite", timeout=300)
        path = (target / name).resolve()
        if not path.is_relative_to(target) or not path.is_file():
            raise QuarkError("DOWNLOAD_MISSING", "网盘文件已读取，但未找到对应下载成品")
        observed = commerce.sha256(path)
        if observed != digest:
            raise QuarkError("HASH_MISMATCH", "云端文件下载后的内容与买家包不一致，不能用于发货")
        return observed


def connection(store: Store, *, cli=None) -> dict:
    result = (cli or QuarkCLI()).status()
    store.set_setting("quark_connection", result)
    return result


def _save(store, account, slug, row):
    row["updated_at"] = now()
    store.put("quark_delivery", product_key(account, slug), row, account=account)


def _folder(store, account, identity, cli):
    key = product_key(account, identity)
    record = store.get("quark_folder", key, {})
    name = "闲鱼交付-" + account
    if record.get("fid"):
        return record["fid"]
    if record.get("state") == "creating":
        found = [f for f in cli.browse("0") if f.get("filename") == name and str(f.get("file_type")) == "0"]
        if len(found) != 1:
            raise QuarkError("FOLDER_UNKNOWN", "交付目录创建结果待核对，未再次创建")
        fid = found[0]["fid"]
    else:
        store.put("quark_folder", key, {"state": "creating", "name": name}, account=account)
        fid = cli.run("create-folder", "--dir-path", name, "--parent-fid", "0")["data"].get("fid")
    if not isinstance(fid, str) or not fid:
        raise QuarkError("FOLDER_UNKNOWN", "交付目录编号未返回，需要回读")
    store.put("quark_folder", key, {"state": "ready", "name": name, "fid": fid}, account=account)
    return fid


def _share_check(cli, record):
    data = cli.run("share-detail", "--url", share_url(record["share_url"]))["data"]
    files = data.get("files")
    if not isinstance(files, list) or len(files) != 1 or data.get("file_count") != 1:
        raise QuarkError("SHARE_CONTENT", "分享内容不是唯一买家交付包，不能启用发货")
    remote = files[0]
    if (remote.get("filename") != record["remote_name"] or remote.get("size") != record["size"]
            or str(remote.get("file_type")) != "1" or not isinstance(remote.get("fid"), str) or not remote["fid"]):
        raise QuarkError("SHARE_MISMATCH", "分享中的文件与本商品交付包不对应，不能启用发货")
    if remote["fid"] != record["fid"]:
        # Browse returns a signed opaque locator, while upload/share return the raw FID.
        # Resolve only recovered locators, by downloading the share's actual file too.
        if not (record.get("recovered_via_browse") and record["fid"].startswith("~") and "|" in record["fid"]):
            raise QuarkError("SHARE_MISMATCH", "分享文件编号与上传记录不一致")
        cli.download_hash(remote["fid"], record["remote_name"], record["sha256"])
        record["recovered_file_locator"] = record["fid"]
        record["fid"] = remote["fid"]
        record["locator_resolution"] = "browse_and_share_files_both_downloaded_with_matching_sha256"
    return {"checked_at": now(), "fid": remote["fid"], "filename": remote["filename"], "size": remote["size"]}


def prepare(store: Store, account: str, slug: str, *, cli=None, reconcile_only=False) -> dict:
    """Upload only catalog buyer deliverables; durable intent precedes every cloud write."""
    with LOCK, operation_lock(store):
        spec = commerce.offer(slug)
        if spec["sale_type"] != "digital":
            raise ValueError("定制服务和内部工具不能把样例设为买家自动交付包")
        cli = cli or QuarkCLI()
        status = connection(store, cli=cli)
        if status["status"] != "connected":
            raise QuarkError("NOT_CONNECTED", status["message"])
        row = store.get("quark_delivery", product_key(account, slug))
        if reconcile_only and not row:
            raise ValueError("没有需要回读的交付记录")
        bundle = ({"delivery_zip": row["local_file"], "delivery_zip_sha256": row["sha256"]}
                  if reconcile_only else commerce.build_bundles(store, account, slug))
        path = (PROJECT / bundle["delivery_zip"]).resolve()
        buyer_root = (commerce.DATA / "commerce" / "bundles" / slug).resolve()
        if not path.is_relative_to(buyer_root) or not path.is_file():
            raise ValueError("交付包路径不正确")
        digest = commerce.sha256(path)
        if digest != bundle["delivery_zip_sha256"]:
            raise ValueError("交付包已变化，请重新打包")
        identity = status["account_fingerprint"]
        cli.expected_identity = identity
        if row and row["account_fingerprint"] != identity:
            raise ValueError("当前夸克账号与已保存交付记录不同，未改写原链接")
        if row and row["sha256"] != digest:
            if reconcile_only or row["state"] in {"uploading", "sharing"}:
                raise ValueError("旧包操作结果尚待核对，不能换包继续")
            store.put("quark_delivery_history", product_key(account, slug) + ":" + row["sha256"], row, account=account)
            row = None
        if not row:
            if reconcile_only:
                raise ValueError("没有需要回读的交付记录")
            row = {"account": account, "slug": slug, "name": spec["name"], "sha256": digest,
                   "size": path.stat().st_size, "local_file": str(path.relative_to(PROJECT)),
                   "account_fingerprint": identity, "remote_name": slug + "-交付包-" + digest[:12] + ".zip",
                   "state": "prepared", "created_at": now(), "message": "买家包已核对，等待上传"}
            _save(store, account, slug, row)
        try:
            if row["state"] in {"verified", "bound"}:
                row["share_verification"] = _share_check(cli, row)
                row.pop("last_error", None)
                _save(store, account, slug, row)
                return row
            if not row.get("folder_fid"):
                if reconcile_only:
                    raise ValueError("尚未取得交付目录，请继续准备交付包")
                row["folder_fid"] = _folder(store, account, identity, cli)
                _save(store, account, slug, row)
            if not row.get("fid"):
                remote = [f for f in cli.browse(row["folder_fid"]) if f.get("filename") == row["remote_name"]]
                if len(remote) > 1:
                    raise QuarkError("DUPLICATE_FILES", "网盘中有多个同名交付包，需要人工核对")
                if remote:
                    f = remote[0]
                    if f.get("size") != row["size"] or str(f.get("file_type")) != "1":
                        raise QuarkError("REMOTE_MISMATCH", "同名云端文件的类型或大小不正确")
                    row["fid"] = f["fid"]
                    row["recovered_via_browse"] = True
                elif row["state"] == "uploading" or reconcile_only:
                    raise QuarkError("UPLOAD_UNKNOWN", "未回读到交付文件，上传结果仍不确定，未自动重传")
                else:
                    staging = cli.root / "uploads" / digest / row["remote_name"]
                    staging.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, staging)
                    if commerce.sha256(staging) != digest:
                        raise ValueError("上传前交付包已变化")
                    row.update(state="uploading", message="正在上传买家包；中断后只回读")
                    _save(store, account, slug, row)
                    response = cli.run("upload", str(staging), "--parent-fid", row["folder_fid"], timeout=300)
                    data = response["data"]
                    fids = data.get("fids", [])
                    if data.get("successCount") != 1 or data.get("fileCount") != 1 or len(fids) != 1:
                        raise QuarkError("UPLOAD_PARTIAL", "上传没有返回唯一成功文件，需要回读")
                    row["fid"] = fids[0]
                row.update(state="uploaded", message="文件已上传，等待下载核对")
                _save(store, account, slug, row)
            if not row.get("download_sha256"):
                row["download_sha256"] = cli.download_hash(row["fid"], row["remote_name"], digest)
                row["download_verified_at"] = now()
                _save(store, account, slug, row)
            if not row.get("share_url"):
                if row["state"] == "sharing":
                    raise QuarkError("SHARE_UNKNOWN", "分享创建结果不确定；请粘贴夸克中该文件的现有分享链接，避免重复创建")
                if reconcile_only:
                    row["message"] = "上传文件已回读一致，继续准备即可创建分享"
                    _save(store, account, slug, row)
                    return row
                row.update(state="sharing", message="正在创建永久加密分享；中断后不会再次创建")
                _save(store, account, slug, row)
                data = cli.run("share", row["fid"], "--title", spec["name"], "--url-type", "2", "--expired-type", "1")["data"]
                if not data.get("passcode"):
                    raise QuarkError("PASSCODE_MISSING", "分享未返回提取码，暂不启用发货")
                row["share_url"] = share_url(data.get("share_url", ""), data["passcode"])
                _save(store, account, slug, row)
            row["share_verification"] = _share_check(cli, row)
            row.update(state="verified", message="云端文件下载一致，分享内容已核对，可以接入自动发货")
            row.pop("last_error", None)
            _save(store, account, slug, row)
            return row
        except (QuarkError, ValueError) as exc:
            row["last_error"] = str(exc)
            _save(store, account, slug, row)
            raise


def adopt_share(store, account, slug, url, *, cli=None):
    with LOCK, operation_lock(store):
        row = store.get("quark_delivery", product_key(account, slug))
        if not row or row["state"] != "sharing" or row.get("download_sha256") != row["sha256"]:
            raise ValueError("当前交付包不处于分享待核对状态")
        cli = cli or QuarkCLI()
        if cli.identity() != row["account_fingerprint"]:
            raise ValueError("夸克账号与交付记录不一致")
        cli.expected_identity = row["account_fingerprint"]
        candidate = dict(row, share_url=share_url(url))
        candidate["share_verification"] = _share_check(cli, candidate)
        candidate.update(state="verified", message="现有分享链接已核对，可以接入自动发货")
        candidate.pop("last_error", None)
        _save(store, account, slug, candidate)
        return candidate


def bind(store: Store, account: str, slug: str, expected_sha256: str, *, cli=None) -> dict:
    with LOCK, operation_lock(store):
        if commerce.offer(slug)["sale_type"] != "digital":
            raise ValueError("只有数字成品可以接入自动发货")
        row = store.get("quark_delivery", product_key(account, slug))
        if not row or row["state"] not in {"verified", "bound"} or row["sha256"] != expected_sha256:
            raise ValueError("交付包尚未核对，或已更换版本，请重新查看")
        bundle = commerce.build_bundles(store, account, slug)
        if bundle["delivery_zip_sha256"] != expected_sha256 or row.get("download_sha256") != expected_sha256:
            raise ValueError("本地交付包已变化，需要重新上传核对")
        cli = cli or QuarkCLI()
        status = connection(store, cli=cli)
        if status.get("status") != "connected" or status.get("account_fingerprint") != row["account_fingerprint"]:
            raise ValueError("夸克连接或账号不一致，未改写发货配置")
        cli.expected_identity = row["account_fingerprint"]
        row["share_verification"] = _share_check(cli, row)
        publication = store.get("publication", product_key(account, slug), {})
        item_id = str(publication.get("item_id") or "")
        product = store.get("product", product_key(account, item_id), {})
        if (publication.get("account") != account or publication.get("state") != "published"
                or not item_id.isdigit() or product.get("account") != account or product.get("is_multi_spec")):
            raise ValueError("需要已核对在线、属于当前账号的单规格商品")
        rule_id = "quark:" + product_key(account, slug)
        for rule in store.rows("delivery_rule", account):
            if str(rule.get("id")) == rule_id or rule.get("enabled") is False:
                continue
            if str(rule.get("item_id") or "") == item_id or (not rule.get("item_id") and str(rule.get("keyword", "")).casefold() in {item_id, str(product.get("title", "")).casefold()}):
                raise ValueError("此闲鱼商品已有其他发货规则，未覆盖原配置")
        text = f"感谢购买《{row['name']}》。\n买家交付包：{row['share_url']}\n下载后先解压，按包内使用说明开始。此链接为本商品的数字成品，无需邮寄。\n如无法打开或文件缺失，请在当前订单联系我。"
        card = {"id": rule_id, "account": account, "name": row["name"] + " · 夸克交付", "type": "text",
                "enabled": True, "is_multi_spec": False, "text_content": text, "delivery_sha256": row["sha256"]}
        rule = {"id": rule_id, "account": account, "item_id": item_id, "keyword": product["title"],
                "card_id": rule_id, "enabled": True, "delivery_count": 1, "description": "唯一商品 ID 对应已核对买家交付包"}
        product = {**product, "managed": True}
        binding = {"account": account, "slug": slug, "item_id": item_id, "card_id": rule_id,
                   "sha256": row["sha256"], "share_url": row["share_url"], "bound_at": now()}
        row = {**row, "state": "bound", "item_id": item_id, "updated_at": now(), "message": "已接入此商品的付款后自动发货"}
        with store.connect() as db:
            previous = store.get("quark_binding", product_key(account, slug))
            values = [("card", rule_id, card), ("delivery_rule", rule_id, rule),
                      ("product", product_key(account, item_id), product),
                      ("quark_binding", product_key(account, slug), binding),
                      ("quark_delivery", product_key(account, slug), row)]
            if previous and any(previous.get(k) != binding.get(k) for k in ("item_id", "sha256", "share_url")):
                values.append(("quark_binding_history", uuid.uuid4().hex, previous))
            for kind, key, value in values:
                db.execute("INSERT INTO records(kind,key,account,payload,source,saved_at) VALUES(?,?,?,?,?,?) "
                           "ON CONFLICT(kind,key) DO UPDATE SET account=excluded.account,payload=excluded.payload,source=excluded.source,saved_at=excluded.saved_at",
                           (kind, key, account, json.dumps(value, ensure_ascii=False), "quark_verified_delivery", now()))
        return row


def audit_links(store: Store, account: str, *, cli=None) -> dict:
    cli = cli or QuarkCLI()
    reports = []
    for card in store.rows("card", account):
        urls = re.findall(r"https://pan\.quark\.cn/s/[A-Za-z0-9]+(?:\?pwd=[A-Za-z0-9]+)?", card.get("text_content", ""))
        for url in urls:
            row = {"card_id": str(card["id"]), "name": card.get("name"), "checked_at": now()}
            try:
                data = cli.run("share-detail", "--url", share_url(url))["data"]
                row.update(status="accessible", file_count=data.get("file_count"),
                           files=[{k: f.get(k) for k in ("filename", "size", "file_type")} for f in data.get("files", [])])
            except QuarkError as exc:
                row.update(status="blocked", message=str(exc))
            reports.append(row)
    result = {"checked_at": now(), "links": reports, "boundary": "核对链接可访问及顶层文件；不代表内容质量或真实订单验收"}
    store.put("quark_link_audit", account, result, account=account)
    return result
