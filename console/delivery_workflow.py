"""Compose existing console operations; never duplicate publication or delivery logic."""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
import urllib.error
import urllib.parse
import urllib.request

from . import commerce
from .publishing import RETRYABLE, digest
from .paths import PROJECT
from .store import Store, product_key, now


class WorkflowStop(ValueError):
    def __init__(self, message, *, job_id=None):
        super().__init__(message)
        self.job_id = job_id


@contextmanager
def batch_lock(store):
    path = store.path.parent / "workflow" / "batch.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise WorkflowStop("已有资料批次正在执行，请等待它结束后接续；未并行提交。") from exc
    try:
        yield
    finally:
        handle.close()


class Backend:
    def __init__(self, account, port=8090, wait_seconds=45):
        self.account = account
        self.base = f"http://127.0.0.1:{int(port)}"
        self.wait_seconds = wait_seconds

    def request(self, method, path, body=None):
        url = self.base + path + "?" + urllib.parse.urlencode({"account": self.account})
        request = urllib.request.Request(url, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # Console errors are sanitized by the existing HTTP exception handlers.
            try:
                error = json.loads(exc.read())
                detail = error.get("message") or error.get("detail") or f"后台返回 HTTP {exc.code}"
            except (ValueError, AttributeError):
                detail = f"后台返回 HTTP {exc.code}"
            raise WorkflowStop(str(detail)) from exc
        except (OSError, ValueError) as exc:
            raise WorkflowStop("后台响应未确认；未重发请求。查看 status 或 jobs 后继续。") from exc

    def check(self):
        health = self.request("GET", "/health")
        if health.get("service") != "xianyu-owned-console" or health.get("status") != "healthy":
            raise WorkflowStop("目标端口不是健康的自有闲鱼后台")

    def job(self, path, body=None):
        job = self.request("POST", path, body if body is not None else {})
        deadline = time.monotonic() + self.wait_seconds
        while job.get("state") in {"queued", "running"}:
            if time.monotonic() >= deadline:
                raise WorkflowStop("后台任务仍在执行；保留任务 ID，完成后重跑原命令继续，不重复提交平台操作。", job_id=job["id"])
            time.sleep(1)
            job = self.request("GET", "/api/jobs/" + job["id"])
        if job.get("state") != "succeeded":
            raise WorkflowStop(job.get("error") or (job.get("result") or {}).get("message") or "任务尚未核对通过", job_id=job.get("id"))
        return job["result"]


class DeliveryWorkflow:
    def __init__(self, store: Store, backend: Backend, account: str, slug: str):
        self.store, self.backend, self.account, self.slug = store, backend, account, slug
        commerce.offer(slug)  # Validate path before composing HTTP routes.
        if not store.get("account", account):
            raise WorkflowStop("账号不存在")
        self.path = "/api/commerce/" + slug
        self.key = product_key(account, slug)

    def row(self, kind):
        return self.store.get(kind, self.key, {})

    def require_digital(self):
        spec = commerce.offer(self.slug)
        if spec["sale_type"] != "digital" or spec.get("limitations"):
            raise WorkflowStop("此串联入口只处理已完成的数字成品；服务样例或有交付缺口的商品不可自动发货")

    def idle(self):
        pending = [j for j in self.store.jobs(100) if j["account"] == self.account
                   and j.get("item_id") == self.slug and j["state"] in {"queued", "running"}]
        if pending:
            raise WorkflowStop("此商品已有后台任务执行中，请等它完成后重跑原命令。", job_id=pending[0]["id"])

    def bundle(self):
        return self.backend.request("POST", self.path + "/bundle", {})

    def prepare(self, values=None):
        self.require_digital()
        if values is not None and not isinstance(values, dict):
            raise WorkflowStop("发布字段必须是 JSON 对象")
        self.idle()
        publication = self.row("publication")
        if publication and publication.get("state") not in RETRYABLE:
            raise WorkflowStop("该商品已有发布记录；用 finish 接续原预览，或 verify 核对现有商品，不另建商品")
        bundle = self.bundle()
        cloud = self.row("quark_delivery")
        if cloud.get("state") in {"uploading", "sharing"}:
            self.backend.job(self.path + "/quark/reconcile")
        result = self.backend.job(self.path + "/quark/prepare", {"acknowledgment": "upload_this_buyer_package"})
        cloud = result["delivery"]
        if cloud.get("state") not in {"verified", "bound"} or cloud.get("sha256") != bundle["delivery_zip_sha256"]:
            raise WorkflowStop("云端交付版本未与本轮买家包一致，停止生成发布预览")
        preview = self.backend.request("POST", self.path + "/publish-preview", values or {})
        if preview.get("bundle_sha256") != bundle["content_sha256"]:
            raise WorkflowStop("准备期间本地文件变化，请重新检查后准备")
        return {"state": "prepared_for_review", "account": self.account, "slug": self.slug,
                "preview_id": preview["id"], "digest": preview["digest"],
                "delivery_sha256": cloud["sha256"], "title": preview["title"],
                "description": preview["description"], "price_cents": preview["price_cents"],
                "quantity": preview["quantity"], "images": preview["images"],
                "delivery_files": commerce.file_list(self.slug, "delivery"),
                "category": preview["category"], "address": preview["address"],
                "message": "买家包已上传并核对，尚未发布；核对本体、文图、价格与交付后，用这些参数执行 finish。"}

    def validate_finish(self, preview_id, approved_digest, delivery_sha256):
        self.require_digital()
        self.idle()
        preview = self.store.get("publish_preview", preview_id, {})
        if (preview.get("account") != self.account or preview.get("slug") != self.slug
                or preview.get("digest") != approved_digest):
            raise WorkflowStop("指定预览不属于此账号与商品，或摘要不一致")
        if digest({k: v for k, v in preview.items() if k != "digest"}) != approved_digest:
            raise WorkflowStop("保存的发布预览内容已变化，未继续发布")
        publication = self.row("publication")
        if (not publication or publication.get("state") in RETRYABLE) and (
                datetime.fromisoformat(now()) - datetime.fromisoformat(preview["created_at"])).total_seconds() > 7200:
            raise WorkflowStop("预览超过两小时，请重新核对当前账号和分类")
        bundle = self.bundle()
        if bundle["content_sha256"] != preview.get("bundle_sha256") or bundle["delivery_zip_sha256"] != delivery_sha256:
            raise WorkflowStop("确认后的交付或上架素材已变化，未继续发布或绑定")
        cloud = self.row("quark_delivery")
        if (cloud.get("sha256") != delivery_sha256 or cloud.get("download_sha256") != delivery_sha256
                or cloud.get("state") not in {"verified", "bound"}):
            raise WorkflowStop("已确认买家包没有对应的云端下载核对，未发布")
        return preview

    def finish(self, preview_id, digest, delivery_sha256):
        self.validate_finish(preview_id, digest, delivery_sha256)
        publication = self.row("publication")
        if publication and publication.get("id") == preview_id:
            if publication.get("state") in {"claimed", "uploading", "sending"} | RETRYABLE:
                raise WorkflowStop("原发布尚未结束或已失败；检查原记录，需要新预览时重新 prepare，不能重复发送旧预览")
            # An uncertain response can only be reconciled, never re-published.
            self.backend.job(self.path + "/publish-reconcile")
        elif publication and publication.get("state") not in RETRYABLE:
            raise WorkflowStop("已有另一份发布记录，停止创建或覆盖")
        else:
            self.backend.job(self.path + "/publish", {"preview_id": preview_id, "digest": digest,
                "acknowledgment": "publish_this_reviewed_listing"})
        publication = self.row("publication")
        if publication.get("state") != "published":
            raise WorkflowStop("发布尚未回读核对在线，未绑定自动发货")
        # bind rechecks local bytes, cloud identity, share contents, ownership and rule conflicts.
        self.backend.job(self.path + "/quark/bind", {"sha256": delivery_sha256,
            "acknowledgment": "enable_this_verified_delivery"})
        return self.status()

    def verify(self):
        self.require_digital()
        self.idle()
        # Read remote state through existing reconciliation, without upload, publish, bind or send.
        failures = []
        for suffix in ("/quark/reconcile", "/publish-reconcile"):
            try:
                self.backend.job(self.path + suffix)
            except WorkflowStop as exc:
                failures.append({"step": suffix, "message": str(exc), "job_id": exc.job_id})
                # A running job must finish before another task is submitted.
                if exc.job_id and (self.store.job(exc.job_id) or {}).get("state") in {"queued", "running"}:
                    break
        result = self.status()
        result["verification_errors"] = failures
        result["remote_rechecked"] = not failures
        if failures:
            result["state"] = "needs_attention"
        return result

    def status(self):
        publication, cloud, binding = (self.row(k) for k in ("publication", "quark_delivery", "quark_binding"))
        item_id = str(publication.get("item_id") or "")
        product = self.store.get("product", product_key(self.account, item_id), {})
        card = self.store.get("card", binding.get("card_id", ""), {})
        rule = self.store.get("delivery_rule", binding.get("card_id", ""), {})
        delivery = self.backend.request("GET", "/api/delivery")
        messaging = delivery.get("messaging", {})
        account_status = messaging.get("account_status", {})
        checks = {
            "owned_listing_online": publication.get("state") == "published" and bool(publication.get("checks")) and all(publication["checks"].values()),
            "cloud_download_matches": bool(cloud.get("sha256")) and cloud.get("download_sha256") == cloud.get("sha256"),
            "share_verified": cloud.get("state") in {"verified", "bound"} and bool(cloud.get("share_verification")),
            "cloud_has_no_unresolved_error": not cloud.get("last_error"),
            "bound_to_exact_item": bool(item_id) and binding.get("item_id") == item_id and product.get("account") == self.account,
            "bound_to_exact_version": bool(binding.get("sha256")) and binding.get("sha256") == cloud.get("sha256") == card.get("delivery_sha256"),
            "delivery_rule_enabled": bool(rule.get("enabled") and card.get("enabled")) and str(rule.get("item_id", "")) == item_id and rule.get("card_id") == binding.get("card_id"),
            "bound_link_matches": bool(binding.get("share_url")) and binding.get("share_url") == cloud.get("share_url") and binding["share_url"] in card.get("text_content", ""),
            "message_connection_ready": bool(messaging.get("enabled") and messaging.get("active") and messaging.get("transport", {}).get("ready")),
            "message_runner_watches_item": bool(item_id) and item_id in messaging.get("managed_item_ids", []),
            "platform_order_and_delivery_ready": bool(account_status.get("can_attempt_order_read") and account_status.get("can_attempt_delivery")),
            "final_delivery_not_service_intake": rule.get("fulfillment", "delivery") == card.get("fulfillment", "delivery") == "delivery",
        }
        local_file = (PROJECT / cloud.get("local_file", "__missing__")).resolve()
        root = (commerce.DATA / "commerce" / "bundles" / self.slug).resolve()
        checks["bound_local_package_intact"] = (local_file.is_relative_to(root) and local_file.is_file()
            and commerce.sha256(local_file) == cloud.get("sha256"))
        return {"state": "ready_for_paid_order" if all(checks.values()) else "needs_attention",
                "account": self.account, "slug": self.slug, "item_id": item_id or None,
                "checks": checks, "publication_state": publication.get("state", "not_started"),
                "cloud_state": cloud.get("state", "not_started"),
                "delivery_sha256": binding.get("sha256"), "listing_verified_at": publication.get("verified_at"),
                "share_verified_at": cloud.get("share_verification", {}).get("checked_at"),
                "live_order_proof": "not_established_by_this_check",
                "message": "就绪仅表示技术配置与已保存证据相符；真实付款后的买家收件仍需实际订单验证。status 不刷新远端，用 verify 回读。"}


class BatchWorkflow:
    """Sequential composition of the one-product workflow, using its durable receipts."""

    def __init__(self, store, backend, account, *, flow_factory=DeliveryWorkflow, interval=60):
        self.store, self.backend, self.account = store, backend, account
        self.flow_factory, self.interval = flow_factory, interval

    def flow(self, slug):
        return self.flow_factory(self.store, self.backend, self.account, slug)

    def save(self, batch):
        batch["updated_at"] = now()
        self.store.put("workflow_batch", batch["id"], batch, account=self.account)
        return batch

    def load(self, batch_id):
        batch = self.store.get("workflow_batch", batch_id, {})
        if batch.get("account") != self.account:
            raise WorkflowStop("批次不存在或不属于当前账号")
        return batch

    def plan(self, slugs, values=None, *, price_cents=99):
        if (not isinstance(slugs, list) or not 1 <= len(slugs) <= 20
                or len(set(slugs)) != len(slugs)):
            raise WorkflowStop("每批选择 1–20 件不同的数字成品")
        if type(price_cents) is not int or price_cents not in {59, 99, 199}:
            raise WorkflowStop("此资料流程的默认试售价请选择 59、99 或 199 分")
        if values is not None and (not isinstance(values, dict) or set(values) - set(slugs)):
            raise WorkflowStop("批量发布字段须为按商品标识分组的 JSON 对象")
        if any(not isinstance(v, dict) for v in (values or {}).values()):
            raise WorkflowStop("每件商品的发布字段须为 JSON 对象")
        # Check every selection before creating packages or uploading anything.
        for slug in slugs:
            self.flow(slug).require_digital()
        rows, content_seen = [], set()
        for slug in slugs:
            fields = {"price_cents": price_cents, **(values or {}).get(slug, {})}
            if type(fields.get("price_cents")) is not int or fields["price_cents"] not in {59, 99, 199}:
                raise WorkflowStop("资料商品试售价须为 59、99 或 199 分")
            bundle = commerce.build_bundles(self.store, self.account, slug)
            fingerprint = digest(sorted(f["sha256"] for f in bundle["delivery_files"]))
            if fingerprint in content_seen:
                raise WorkflowStop("选中的商品交付内容相同，不能换标题重复铺货")
            content_seen.add(fingerprint)
            searches = [s for s in self.store.rows("market_search", self.account)
                        if s.get("keyword") == commerce.offer(slug).get("query")]
            sample = max(searches, key=lambda s: s.get("captured_at", ""), default={})
            rows.append({"slug": slug, "name": commerce.offer(slug)["name"], "values": fields,
                         "content_sha256": bundle["content_sha256"],
                         "delivery_sha256": bundle["delivery_zip_sha256"],
                         "delivery_files": bundle["delivery_files"],
                         "market_sample": {k: sample.get(k) for k in ("id", "keyword", "status", "captured_at")},
                         "state": "planned", "plan": None, "result": None})
        batch = {"id": uuid.uuid4().hex, "account": self.account, "products": rows,
                 "state": "planned", "created_at": now(), "review_digest": None,
                 "producer": "codex", "producer_role": "controller",
                 "producer_evidence": "explicitly_selected_product_slugs_and_existing_buyer_files",
                 "review_owner": "user", "review_state": "draft", "canonical_status": "record",
                 "message": "本地批次已准备，尚未上传或发布。逐项核对成品及公开挂牌样本；旧样本不代表当前需求或销量。"}
        return self.save(batch)

    def unchanged(self, batch):
        for row in batch["products"]:
            try:
                bundle = commerce.build_bundles(self.store, self.account, row["slug"])
                if bundle["content_sha256"] != row["content_sha256"] or bundle["delivery_zip_sha256"] != row["delivery_sha256"]:
                    raise WorkflowStop("批次中的成品或上架素材已变化，请重新规划并质检：" + row["slug"])
            except (ValueError, OSError) as exc:
                self.stopped(batch, row, "content_check", exc)
                raise

    @staticmethod
    def review_digest(batch):
        return digest({"account": batch["account"], "products": [
            {k: row[k] for k in ("slug", "content_sha256", "delivery_sha256", "plan")}
            for row in batch["products"]]})

    def stopped(self, batch, row, phase, error):
        batch.update(state="needs_attention", message=str(error), stopped_slug=row["slug"],
                     stopped_phase=phase, job_id=getattr(error, "job_id", None))
        return self.save(batch)

    def prepare(self, batch_id):
        with batch_lock(self.store):
            return self._prepare(batch_id)

    def _prepare(self, batch_id):
        batch = self.load(batch_id)
        self.unchanged(batch)
        if any(row["state"] == "configured" for row in batch["products"]):
            raise WorkflowStop("此批次已有线上商品，请接续 finish 或回读，不重新准备")
        for row in batch["products"]:
            if row["plan"]:
                try:
                    self.flow(row["slug"]).validate_finish(row["plan"]["preview_id"], row["plan"]["digest"], row["delivery_sha256"])
                except (ValueError, OSError) as exc:
                    return self.stopped(batch, row, "prepare_preflight", exc)
                continue
            self.save({**batch, "state": "preparing"})
            try:
                row["plan"] = self.flow(row["slug"]).prepare(row["values"])
                row["state"] = "prepared"
            except (ValueError, OSError) as exc:
                return self.stopped(batch, row, "prepare", exc)
            self.save(batch)
        batch.update(state="prepared_for_review", review_digest=self.review_digest(batch),
                     message="买家包已上传核对，尚未上架。逐项核对标题、价格、图片、内容及交付版本后，使用 review_digest 完成这一批。")
        return self.save(batch)

    def finish(self, batch_id, approved_digest):
        with batch_lock(self.store):
            return self._finish(batch_id, approved_digest)

    def _finish(self, batch_id, approved_digest):
        batch = self.load(batch_id)
        if (not batch.get("review_digest") or batch["review_digest"] != approved_digest
                or self.review_digest(batch) != approved_digest):
            raise WorkflowStop("批次确认摘要不一致，未发布任何新商品")
        self.unchanged(batch)
        # All pending items must pass before the first external publication.
        for row in batch["products"]:
            if row["state"] != "configured":
                if not row["plan"]:
                    raise WorkflowStop("批次仍有未准备的商品")
                try:
                    self.flow(row["slug"]).validate_finish(row["plan"]["preview_id"], row["plan"]["digest"], row["delivery_sha256"])
                except (ValueError, OSError) as exc:
                    self.stopped(batch, row, "finish_preflight", exc)
                    raise
        batch["review_state"] = "reviewed"
        for row in batch["products"]:
            if row["state"] == "configured":
                continue
            last_write = max(batch.get("last_write_epoch", 0),
                             self.store.setting("workflow_last_write:" + self.account, 0))
            remaining = self.interval - (time.time() - last_write)
            if remaining > 0:
                batch.update(state="waiting_between_listings", wait_seconds=remaining,
                             message="上一件已配置，按发布间隔继续下一件；已完成的商品不会重发。")
                return self.save(batch)
            self.save({**batch, "state": "finishing"})
            plan = row["plan"]
            try:
                # Retain the account-wide interval even when a write response is lost.
                self.store.set_setting("workflow_last_write:" + self.account, time.time())
                row["result"] = self.flow(row["slug"]).finish(plan["preview_id"], plan["digest"], row["delivery_sha256"])
                if row["result"].get("publication_state") != "published" or row["result"].get("cloud_state") != "bound":
                    raise WorkflowStop("线上发布或交付绑定未核对完成")
                row["state"] = "configured"
                batch["last_write_epoch"] = time.time()
                self.store.set_setting("workflow_last_write:" + self.account, batch["last_write_epoch"])
            except (ValueError, OSError) as exc:
                return self.stopped(batch, row, "finish", exc)
            self.save(batch)
        batch.update(state="configured", message="本批商品已上架并绑定各自交付包。就绪状态需核对订单与消息通道；真实付款后买家收件仍待实际订单验证。")
        return self.save(batch)

    def status(self, batch_id, *, verify=False):
        batch = self.load(batch_id)
        for row in batch["products"]:
            try:
                flow = self.flow(row["slug"])
                row["status"] = flow.verify() if verify and row["plan"] else flow.status()
            except (ValueError, OSError) as exc:
                row["status"] = {"state": "needs_attention", "message": str(exc)}
        if verify:
            self.save(batch)
        return batch
