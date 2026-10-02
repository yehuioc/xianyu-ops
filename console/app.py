"""Local-only web application owned by xianyu-ops."""
from __future__ import annotations

import json
import base64
import hashlib
import io
import uuid
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from .paths import PROJECT, DATA, WEB, DEFAULT_ACCOUNT
from .service import ConsoleService, clean_record
from . import materials, commerce, publishing, quark, listing_edits
from .marketplace import MarketError
from .store import now, product_key


def create_app(service: ConsoleService | None = None) -> FastAPI:
    service = service or ConsoleService()

    @asynccontextmanager
    async def lifespan(app):
        service.store.recover_jobs()
        publishing.recover(service.store)
        listing_edits.recover(service.store)
        await service.messaging_startup()
        yield
        await service.messaging_shutdown()
        service.close()

    app = FastAPI(title="闲鱼小后台", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.service = service

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.url.hostname not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse({"message": "仅允许本机访问"}, status_code=403)
        origin = request.headers.get("origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1", "testserver"} or parsed.port != request.url.port:
                return JSONResponse({"message": "不接受其他网站发起的请求"}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            try:
                size = int(request.headers.get("content-length", "0") or "0")
            except ValueError:
                return JSONResponse({"message": "请求长度不正确"}, status_code=400)
            if size < 0:
                return JSONResponse({"message": "请求长度不正确"}, status_code=400)
            if size > 12_000_000:
                return JSONResponse({"message": "请求太大"}, status_code=413)
            if not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"message": "操作需要 JSON 请求"}, status_code=415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data: https://*.alicdn.com http://*.alicdn.com; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"message": str(exc)}, status_code=400)

    @app.exception_handler(MarketError)
    async def market_error(request, exc):
        return JSONResponse({"message": str(exc), "code": exc.code}, status_code=409)

    def account_id(value: str) -> str:
        if not service.store.get("account", value):
            raise HTTPException(404, "未找到该账号")
        return value

    def package_record(package_id: str) -> dict:
        package = service.store.get("package", package_id)
        if not package:
            raise HTTPException(404, "未找到素材包")
        return package

    def bundle_file(relative: str) -> Path:
        path = (PROJECT / relative).resolve()
        if not path.is_relative_to(DATA / "bundles") or not path.is_file():
            raise HTTPException(404, "素材文件不存在")
        return path

    @app.get("/health")
    def health():
        return {"status": "healthy", "service": "xianyu-owned-console", "checked_at": now(), "legacy_backend_required": False}

    @app.get("/api/bootstrap")
    def bootstrap(account: str = DEFAULT_ACCOUNT):
        account_id(account)
        settings = {"collection_enabled": service.store.setting("collection_enabled", True),
                    "focus_item": service.focus_item(account),
                    "collection_time": service.store.setting("collection_time", "21:00"),
                    "timezone": service.store.setting("timezone", "Asia/Shanghai"),
                    "automation": service.store.setting("automation", {"status": "not_configured"}),
                    "last_collection": service.store.setting(f"last_collection:{account}")}
        return {"account": account, "runtime": service.runtime_status(account), "settings": settings,
                "products": service.products(account), "jobs": service.store.jobs(20),
                "migration": service.import_result, "server_time": now()}

    @app.get("/api/products/{item_id}")
    def product(item_id: str, account: str = DEFAULT_ACCOUNT):
        return service.detail(account_id(account), item_id)

    @app.get("/api/commerce")
    def commerce_portfolio(account: str = DEFAULT_ACCOUNT):
        return commerce.portfolio(service.store, account_id(account))

    @app.post("/api/commerce/search")
    async def commerce_search(request: Request, account: str = DEFAULT_ACCOUNT):
        account_id(account)
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("搜索请求须为对象")
        return await commerce.search_market(service.store, account, body.get("keyword"), limit=body.get("limit", 15))

    @app.post("/api/commerce/{slug}/bundle")
    def commerce_bundle(slug: str, account: str = DEFAULT_ACCOUNT):
        return commerce.build_bundles(service.store, account_id(account), slug)

    @app.get("/api/quark")
    def quark_status(account: str = DEFAULT_ACCOUNT):
        account = account_id(account)
        return {"connection": service.store.setting("quark_connection", {"status": "unchecked", "message": "尚未检查夸克连接"}),
                "installed": quark.QuarkCLI().installed(), "audit": service.store.get("quark_link_audit", account)}

    @app.post("/api/quark/connect")
    async def quark_connect(request: Request, account: str = DEFAULT_ACCOUNT):
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"code"}:
            raise ValueError("连接参数不正确")
        code = body.get("code")
        if code is not None and (not isinstance(code, str) or not re.fullmatch(r"AAC-[A-Za-z0-9]{32}", code)):
            raise ValueError("一次性授权码格式不正确")
        return service.submit("quark_login", account_id(account), code=code)

    @app.post("/api/quark/{action}")
    def quark_read_action(action: str, account: str = DEFAULT_ACCOUNT):
        if action not in {"check", "audit"}:
            raise ValueError("不支持的夸克操作")
        return service.submit("quark_" + action, account_id(account))

    @app.post("/api/commerce/{slug}/quark/{action}")
    async def quark_delivery_action(slug: str, action: str, request: Request, account: str = DEFAULT_ACCOUNT):
        account = account_id(account)
        if commerce.offer(slug)["sale_type"] != "digital":
            raise ValueError("服务样例和内部工具不接入自动发货")
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("操作参数不正确")
        if action == "prepare":
            if body != {"acknowledgment": "upload_this_buyer_package"}:
                raise ValueError("请确认将此商品买家包上传夸克并创建分享")
            return service.submit("quark_prepare", account, slug)
        if action == "reconcile" and not body:
            return service.submit("quark_reconcile", account, slug)
        if action == "bind":
            if set(body) != {"sha256", "acknowledgment"} or body.get("acknowledgment") != "enable_this_verified_delivery" or not re.fullmatch(r"[0-9a-f]{64}", str(body.get("sha256", ""))):
                raise ValueError("请核对当前交付包，再启用此商品的自动发货")
            return service.submit("quark_bind", account, slug, sha256=body["sha256"])
        if action == "adopt-share" and set(body) == {"url"}:
            return service.submit("quark_adopt_share", account, slug, url=quark.share_url(body["url"]))
        raise ValueError("夸克交付操作不正确")

    @app.post("/api/commerce/{slug}/publish-preview")
    async def publish_preview(slug: str, request: Request, account: str = DEFAULT_ACCOUNT):
        return await publishing.prepare(service.store, account_id(account), slug, await request.json())

    @app.post("/api/commerce/{slug}/publish")
    async def publish_listing(slug: str, request: Request, account: str = DEFAULT_ACCOUNT):
        account_id(account)
        body = await request.json()
        if not isinstance(body, dict) or body.get("acknowledgment") != "publish_this_reviewed_listing":
            raise ValueError("请先核对预览，再明确确认发布")
        preview = service.store.get("publish_preview", body.get("preview_id", ""))
        if not preview or preview["account"] != account or preview["slug"] != slug or preview["digest"] != body.get("digest"):
            raise ValueError("发布内容与预览不一致")
        return service.submit("publish_listing", account, slug, preview_id=preview["id"], approved_digest=preview["digest"])

    @app.post("/api/commerce/{slug}/publish-reconcile")
    def reconcile_listing(slug: str, account: str = DEFAULT_ACCOUNT):
        return service.submit("reconcile_listing", account_id(account), slug)

    @app.post("/api/commerce/{slug}/inventory")
    async def update_inventory(slug: str, request: Request, account: str = DEFAULT_ACCOUNT):
        body = await request.json()
        if not isinstance(body, dict) or body.get("acknowledgment") != "update_this_listing_inventory":
            raise ValueError("请明确确认该商品的新库存")
        quantity = body.get("quantity")
        if type(quantity) is not int or not 1 <= quantity <= 9999:
            raise ValueError("库存须为 1–9999 的整数")
        return service.submit("update_inventory", account_id(account), slug, quantity=quantity)

    @app.post("/api/commerce/{slug}/content-preview")
    async def content_preview(slug: str, request: Request, account: str = DEFAULT_ACCOUNT):
        return await listing_edits.prepare(service.store, account_id(account), slug, await request.json())

    @app.post("/api/commerce/{slug}/content-update")
    async def content_update(slug: str, request: Request, account: str = DEFAULT_ACCOUNT):
        account = account_id(account)
        body = await request.json()
        if not isinstance(body, dict) or body.get("acknowledgment") != "update_this_reviewed_listing_content":
            raise ValueError("请核对现有商品的新介绍与商品图")
        preview = service.store.get("listing_edit_preview", body.get("preview_id", ""))
        if not preview or preview["account"] != account or preview["slug"] != slug or preview["digest"] != body.get("digest"):
            raise ValueError("更新内容与预览不一致")
        return service.submit("edit_listing", account, slug, preview_id=preview["id"], approved_digest=preview["digest"])

    @app.post("/api/commerce/{slug}/content-reconcile")
    def content_reconcile(slug: str, account: str = DEFAULT_ACCOUNT):
        return service.submit("reconcile_content", account_id(account), slug)

    @app.get("/api/commerce/{slug}/download/{kind}")
    def commerce_download(slug: str, kind: str, account: str = DEFAULT_ACCOUNT):
        path = commerce.bundle_download(service.store, account_id(account), slug, kind)
        return FileResponse(path, filename=path.name, media_type="application/zip")

    @app.get("/api/commerce/{slug}/files/{section}/{relative:path}")
    def commerce_file(slug: str, section: str, relative: str, account: str = DEFAULT_ACCOUNT):
        account_id(account)
        path = commerce.source_file(slug, section, relative)
        # HTML/SVG source is downloaded, never executed with console-origin authority.
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    @app.get("/api/commerce/{slug}/cover")
    def commerce_cover(slug: str, account: str = DEFAULT_ACCOUNT):
        account_id(account)
        return FileResponse(commerce.source_file(slug, "listing", "主图.png"), media_type="image/png")

    @app.get("/api/products/{item_id}/source-image")
    def source_image(item_id: str, account: str = DEFAULT_ACCOUNT):
        source = materials.listing_sources(service.product(account_id(account), item_id))
        path = source["image"]
        if not path:
            raise HTTPException(404, "暂无图片")
        return FileResponse(path)

    @app.get("/api/products/{item_id}/draft")
    def draft(item_id: str, account: str = DEFAULT_ACCOUNT, from_file: bool = False):
        value = materials.listing_sources(service.product(account_id(account), item_id))
        saved = None if from_file else service.store.get("draft", product_key(account, item_id))
        return {"title": saved["title"] if saved else value["title"],
                "description": saved["description"] if saved else value["description"], "has_image": bool(value["image"])}

    @app.post("/api/products/{item_id}/draft")
    async def save_draft(item_id: str, request: Request, account: str = DEFAULT_ACCOUNT):
        service.product(account_id(account), item_id)
        body = await request.json()
        if not isinstance(body, dict) or any(not isinstance(body.get(k), str) for k in ("title", "description")):
            raise ValueError("请填写标题和介绍")
        if len(body["title"]) > 200 or len(body["description"]) > 20000:
            raise ValueError("文案过长")
        service.store.put("draft", product_key(account, item_id), {"title": body["title"], "description": body["description"], "saved_at": now()}, account=account)
        return {"status": "saved"}

    @app.post("/api/products/{item_id}/image")
    async def upload_image(item_id: str, request: Request, account: str = DEFAULT_ACCOUNT):
        product = service.product(account_id(account), item_id)
        if product.get("slug") != "dsh-orangebook":
            raise ValueError("该商品尚未配置素材目录")
        body = await request.json()
        encoded = body.get("data") if isinstance(body, dict) else None
        if not isinstance(encoded, str) or len(encoded) > 11_000_000:
            raise ValueError("请选择小于 8 MB 的 PNG、JPEG 或 WebP 图片")
        try:
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > 8_000_000:
                raise ValueError("image too large")
            with Image.open(io.BytesIO(raw)) as picture:
                extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(picture.format)
                if not extension or picture.width * picture.height > 25_000_000:
                    raise ValueError("image format/size unsupported")
                picture.verify()
        except Exception as exc:
            raise ValueError("这张图片无法读取，请使用 PNG、JPEG 或 WebP。") from exc
        directory = PROJECT / "products" / product["slug"] / "listing" / "images"
        path = directory / ("user-main-" + hashlib.sha256(raw).hexdigest()[:16] + extension)
        if not path.exists():
            path.write_bytes(raw)
        product["preferred_image"] = str(path.relative_to(PROJECT))
        service.store.put("product", product_key(account, item_id), product, account=account)
        return {"status": "saved", "message": "主图已保存，未提交到平台。"}

    @app.post("/api/products/{item_id}/collect")
    def collect(item_id: str, account: str = DEFAULT_ACCOUNT):
        account_id(account)
        service.product(account, item_id)
        return service.submit("collect", account, item_id)

    @app.post("/api/products/{item_id}/package")
    async def prepare(item_id: str, request: Request, account: str = DEFAULT_ACCOUNT):
        account_id(account)
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("请求格式不正确")
        for key in ("title", "description"):
            if key in body and not isinstance(body[key], str):
                raise ValueError("文案必须是文字")
        return service.submit("prepare_bundle", account, item_id, title=body.get("title"), description=body.get("description"))

    @app.post("/api/products/{item_id}/watch")
    async def watch(item_id: str, request: Request, account: str = DEFAULT_ACCOUNT):
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("watch"), bool):
            raise ValueError("请选择加入或暂停观察")
        product = service.product(account_id(account), item_id)
        product["watch"] = body["watch"]
        service.store.put("product", product_key(account, item_id), product, account=account)
        return {"watch": product["watch"]}

    @app.post("/api/catalog/refresh")
    def catalog(account: str = DEFAULT_ACCOUNT):
        return service.submit("refresh_catalog", account_id(account))

    @app.post("/api/accounts/{account}/connect")
    def connect(account: str):
        return service.submit("connect_account", account_id(account))

    @app.post("/api/collect")
    def collect_all(account: str = DEFAULT_ACCOUNT):
        return service.submit("collect_all", account_id(account))

    @app.post("/api/settings/collection")
    async def collection_setting(request: Request):
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
            raise ValueError("请选择开启或暂停")
        service.store.set_setting("collection_enabled", body["enabled"])
        return {"enabled": body["enabled"]}

    @app.get("/api/jobs")
    def jobs():
        return {"jobs": service.store.jobs(50)}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        row = service.store.job(job_id)
        if not row:
            raise HTTPException(404, "未找到任务")
        return row

    @app.get("/api/packages/{package_id}/image")
    def package_image(package_id: str):
        return FileResponse(bundle_file(package_record(package_id)["image"]["file"]))

    @app.get("/api/packages/{package_id}/download")
    def package_download(package_id: str):
        package = package_record(package_id)
        path = bundle_file(package["zip_file"])
        if materials.digest(path) != package["zip_sha256"]:
            raise HTTPException(409, "素材包文件发生变化，请重新生成")
        return FileResponse(path, filename="闲鱼手机素材包-" + package["item_id"] + ".zip", media_type="application/zip")

    @app.get("/api/delivery")
    def delivery(account: str = DEFAULT_ACCOUNT):
        account_id(account)
        from .support import coverage
        card_fields = ("id", "name", "type", "enabled", "spec_name", "spec_value", "text_content", "fulfillment")
        rule_fields = ("id", "keyword", "card_id", "enabled", "description", "item_id", "fulfillment")
        keyword_fields = ("id", "keyword", "reply", "response", "item_id", "cookie_id", "type", "enabled", "match_mode")
        messaging = service.messaging_status(account)
        return {"execution_state": "running" if messaging.get("active") else messaging.get("runtime", {}).get("state", "paused"),
                "messaging": messaging,
                "coverage": coverage(service.store, account),
                "cards": [{k: r.get(k) for k in card_fields} for r in service.store.rows("card", account)],
                "rules": [{k: r.get(k) for k in rule_fields} for r in service.store.rows("delivery_rule", account)],
                "keywords": [{**{k: r.get(k) for k in keyword_fields}, "key": r["_key"], "enabled": r.get("enabled", True)} for r in service.store.rows("keyword", account)],
                "ai_enabled": False,
                "history": [{k: r.get(k) for k in ("id", "order_id", "status", "created_at")} for r in service.store.rows("delivery_log", account)]}

    @app.post("/api/messaging")
    async def messaging_setting(request: Request, account: str = DEFAULT_ACCOUNT):
        account_id(account)
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
            raise ValueError("请选择启用或暂停自动消息")
        if body["enabled"] and body.get("acknowledgment") != "reply_and_deliver_existing_items":
            raise ValueError("启用会回复咨询、发送订单资料并确认发货，请先阅读页面说明。")
        try:
            return await service.configure_messaging(account, body["enabled"])
        except Exception as exc:
            raise ValueError(str(exc)) from exc

    @app.put("/api/config/{kind}/{key}")
    async def edit_config(kind: str, key: str, request: Request, account: str = DEFAULT_ACCOUNT):
        account_id(account)
        allowed = {"card": {"name", "text_content", "enabled"}, "keyword": {"keyword", "reply", "enabled"}}
        if kind not in allowed:
            raise HTTPException(404, "未找到配置类型")
        row = next((r for r in service.store.rows(kind, account) if r["_key"] == key), None)
        if row is None:
            raise HTTPException(404, "未找到该账号的配置")
        if row.get("type", "text") != "text":
            raise ValueError("当前编辑器只支持已迁入的文本配置")
        body = await request.json()
        if not isinstance(body, dict) or set(body) != allowed[kind]:
            raise ValueError("请完整填写配置")
        if not isinstance(body["enabled"], bool):
            raise ValueError("启用状态必须是开关")
        for field in allowed[kind] - {"enabled"}:
            if not isinstance(body[field], str) or not body[field].strip() or len(body[field]) > (200 if field in {"name", "keyword"} else 20000):
                raise ValueError("名称、关键词和内容不能为空或过长")
        before = clean_record(row)
        updated = {**before, **body, "updated_at": now()}
        service.store.put("config_change", uuid.uuid4().hex, {"kind": kind, "key": key, "before": before,
                          "after": updated, "changed_at": now()}, account=account)
        service.store.put(kind, key, updated, account=account)
        return {"status": "saved", "message": "配置已保存，原值保留在本地修改记录。"}

    @app.get("/api/requirements")
    def requirements():
        path = PROJECT / "console" / "requirements.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"entries": []}

    # Compatibility endpoints for existing project readers. They return owned
    # data and never proxy requests to the downloaded admin server.
    @app.get("/items/{account}")
    def legacy_items(account: str):
        rows = service.products(account_id(account))
        return {"success": True, "items": [{"item_id": r["item_id"], "item_title": r["title"],
                "item_description": r.get("description"), "item_price": r.get("price"),
                "item_status": r.get("status"), "updated_at": r.get("observed_at")} for r in rows]}

    @app.get("/api/orders")
    def orders(account: str = DEFAULT_ACCOUNT):
        return {"success": True, "data": [clean_record(r) for r in service.store.rows("order", account_id(account))]}

    @app.get("/cookies/{account}/runtime-status")
    def legacy_runtime(account: str):
        state = service.account_status(account_id(account))
        return {"runtime_status": {"backend": "owned", "running": True,
                "can_attempt_read": state["can_attempt_read"], "session_ready": state.get("auth_state") == "verified",
                "auth_state": state.get("auth_state"), "ws_ready": False, "message_stream_ready": False,
                "connection_state": state.get("auth_state"), "im_transport_mode": "not_migrated"}}

    @app.get("/delivery-rules")
    def legacy_rules(account: str = DEFAULT_ACCOUNT):
        return [clean_record(r) for r in service.store.rows("delivery_rule", account_id(account))]

    @app.put("/delivery-rules/{rule_id}")
    async def legacy_rule_update(rule_id: str, request: Request):
        row = service.store.get("delivery_rule", rule_id)
        if not row:
            raise HTTPException(404, "未找到规则")
        body = await request.json()
        fields = {"keyword", "card_id", "delivery_count", "enabled", "description"}
        if not isinstance(body, dict) or set(body) - fields:
            raise ValueError("规则字段不正确")
        row.update(body)
        service.store.put("delivery_rule", rule_id, row, account=DEFAULT_ACCOUNT)
        return {"success": True, "message": "发货规则更新成功"}

    @app.post("/items/get-all-from-account")
    async def legacy_catalog(request: Request):
        body = await request.json()
        result = await service.refresh_catalog(account_id(str(body.get("cookie_id") or DEFAULT_ACCOUNT)))
        return {"success": True, **result}

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html", media_type="text/html")

    @app.get("/static/app.js")
    def javascript():
        # Windows registry MIME mappings can classify .js as text/plain.
        return FileResponse(WEB / "app.js", media_type="text/javascript")

    app.mount("/static", StaticFiles(directory=str(WEB)), name="static")
    return app
