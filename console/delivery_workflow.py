"""Compose existing console operations; never duplicate publication or delivery logic."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import commerce
from .publishing import RETRYABLE
from .paths import PROJECT
from .store import Store, product_key


class WorkflowStop(ValueError):
    def __init__(self, message, *, job_id=None):
        super().__init__(message)
        self.job_id = job_id


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

    def finish(self, preview_id, digest, delivery_sha256):
        self.require_digital()
        self.idle()
        preview = self.store.get("publish_preview", preview_id, {})
        if (preview.get("account") != self.account or preview.get("slug") != self.slug
                or preview.get("digest") != digest):
            raise WorkflowStop("指定预览不属于此账号与商品，或摘要不一致")
        bundle = self.bundle()
        if bundle["content_sha256"] != preview.get("bundle_sha256") or bundle["delivery_zip_sha256"] != delivery_sha256:
            raise WorkflowStop("确认后的交付或上架素材已变化，未继续发布或绑定")
        cloud = self.row("quark_delivery")
        if (cloud.get("sha256") != delivery_sha256 or cloud.get("download_sha256") != delivery_sha256
                or cloud.get("state") not in {"verified", "bound"}):
            raise WorkflowStop("已确认买家包没有对应的云端下载核对，未发布")
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
