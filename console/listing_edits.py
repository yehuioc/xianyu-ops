"""Reviewed text/image replacement for an existing, owned single-SKU listing.

Keeps the original publication evidence. A write is attempted once; uncertainty
can only be reconciled by reading the existing item, never by republishing it.
"""
from __future__ import annotations

import copy
import json
import time
import uuid

from . import commerce, publishing as pub
from .marketplace import EDIT_API, EDIT_DETAIL_API, MarketError, MtopClient, normalize_item_status
from .store import now, product_key

PENDING = {"claimed", "uploading", "sending", "acknowledged", "unknown", "needs_review"}


def current(store, account, slug):
    return store.get("listing_edit", product_key(account, slug))


def _save(store, row):
    row["updated_at"] = now()
    for kind, key in (("listing_edit", product_key(row["account"], row["slug"])), ("listing_edit_attempt", row["id"])):
        store.put(kind, key, row, account=row["account"])
    return row


def _fingerprint(data):
    return pub.digest({key: data.get(key) for key in pub.INVENTORY_FIELDS + ("quantity", "itemId", "userId", "uniqueCode", "itemStatus")})


def public_preview(row):
    return {key: row.get(key) for key in ("id", "digest", "account", "slug", "item_id", "title", "description",
            "images", "price_cents", "quantity", "created_at", "delivery_sha256")}


def public_result(row):
    return {key: row.get(key) for key in ("id", "account", "slug", "item_id", "state", "message",
            "error_code", "updated_at", "verified_at", "checks", "preserved_checks")}


def _check_delivery(store, account, slug, item_id, digest):
    if commerce.offer(slug)["sale_type"] == "digital":
        binding = store.get("quark_binding", product_key(account, slug), {})
        if binding.get("item_id") != item_id or binding.get("sha256") != digest:
            raise ValueError("请先核对新版交付包并接入该商品发货，再更新对外介绍")


async def prepare(store, account, slug, values=None, *, client_factory=MtopClient):
    values = values or {}
    if not isinstance(values, dict) or set(values) - {"title", "description", "images"}:
        raise ValueError("本入口只修改介绍与商品图，不修改价格、库存或规格")
    record = pub.current(store, account, slug)
    if not record or record.get("state") != "published" or not record.get("item_id"):
        raise ValueError("需要本后台已核实发布的商品")
    if (current(store, account, slug) or {}).get("state") in PENDING:
        raise ValueError("上次文图更新尚未核对，只允许回读")
    if record.get("inventory_update", {}).get("state") in {"sending", "acknowledged", "unknown"}:
        raise ValueError("已有库存修改待核对")
    spec = commerce.offer(slug)
    listing = json.loads(commerce.source_file(slug, "listing", "listing.json").read_text(encoding="utf-8"))
    expected, expected_images = pub.effective_content(record)
    identity = pub.identity(store, account)
    if identity != expected["user_id"]:
        raise ValueError("当前账号与原商品不同")
    fields = pub.validate_text(spec, {"title": values.get("title", listing["title"]),
        "description": values.get("description", listing["description"]),
        "price_cents": expected["price_cents"], "quantity": expected["quantity"]})
    names = values.get("images", listing.get("images", ["主图.png"]))
    if not isinstance(names, list) or not 1 <= len(names) <= 9 or any(not isinstance(n, str) for n in names) or len(set(names)) != len(names):
        raise ValueError("请选择 1–9 张不同商品图")
    images = [pub.image_bytes(slug, name)[1] for name in names]
    bundle = commerce.build_bundles(store, account, slug)
    _check_delivery(store, account, slug, record["item_id"], bundle["delivery_zip_sha256"])
    async with client_factory(store, account) as client:
        owned_ids = {p["item_id"] for p in await client.products()}
        data = (await client._post_mtop(api_name=EDIT_DETAIL_API, payload={"itemId": record["item_id"]})).get("data", {})
    if data.get("itemSkuList"):
        raise ValueError("本入口仅处理已自动上架的单规格商品")
    if not all(pub.matches(expected, expected_images, data, item_id=record["item_id"], owned_ids=owned_ids).values()):
        raise ValueError("线上基线已变化，未覆盖，请先核对当前商品")
    if normalize_item_status(data.get("itemStatus"), owned=True) != "在线":
        raise ValueError("商品当前未核实为在线")
    row = {"id": uuid.uuid4().hex, "account": account, "slug": slug, "item_id": record["item_id"],
           "created_at": now(), **fields, "images": images, "before": data, "before_digest": _fingerprint(data),
           "preview": {**expected, **fields, "images": images}, "delivery_sha256": bundle["delivery_zip_sha256"]}
    row["digest"] = pub.digest(row)
    store.put("listing_edit_preview", row["id"], row, account=account)
    return public_preview(row)


def _claim(store, account, slug, preview_id, approved_digest):
    preview = store.get("listing_edit_preview", preview_id)
    if not preview or preview["account"] != account or preview["slug"] != slug or preview["digest"] != approved_digest:
        raise ValueError("更新内容与核对的预览不一致")
    if pub.identity(store, account) != preview["preview"]["user_id"]:
        raise ValueError("登录账号发生变化")
    bundle = commerce.build_bundles(store, account, slug)
    if bundle["delivery_zip_sha256"] != preview["delivery_sha256"]:
        raise ValueError("交付内容已变化，请重新核对")
    _check_delivery(store, account, slug, preview["item_id"], preview["delivery_sha256"])
    for expected in preview["images"]:
        if pub.image_bytes(slug, expected["name"])[1] != expected:
            raise ValueError("商品图片已变化，请重新核对")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        attempted = db.execute("SELECT payload FROM records WHERE kind='listing_edit_attempt' AND key=?", (preview_id,)).fetchone()
        if attempted:
            row = json.loads(attempted[0])
            if row["state"] == "verified":
                return row, False
            raise ValueError("该更新已尝试，只允许回读；不能重复提交")
        previous = db.execute("SELECT payload FROM records WHERE kind='listing_edit' AND key=?", (product_key(account, slug),)).fetchone()
        if previous and json.loads(previous[0]).get("state") in PENDING:
            raise ValueError("已有文图更新正在进行或等待核对")
        row = {**preview, "state": "claimed", "authorization": "update_this_reviewed_listing_content", "updated_at": now(),
               "producer": "codex", "producer_role": "controller", "producer_evidence": f"approved_content_preview:{preview_id}:{approved_digest}",
               "review_owner": "codex-controller", "review_state": "reviewed", "canonical_status": "record"}
        for kind, key in (("listing_edit", product_key(account, slug)), ("listing_edit_attempt", row["id"])):
            db.execute("INSERT INTO records(kind,key,account,payload,source,saved_at) VALUES(?,?,?,?,?,?) "
                       "ON CONFLICT(kind,key) DO UPDATE SET payload=excluded.payload,saved_at=excluded.saved_at",
                       (kind, key, account, json.dumps(row, ensure_ascii=False), "owned_listing_edit", now()))
    return row, True


def edit_payload(before, row):
    body = copy.deepcopy(before)
    body.update(itemTextDTO={"title": row["title"], "desc": row["description"], "titleDescSeparate": True},
                imageInfoDOList=[dict(image, major=(i == 0)) for i, image in enumerate(row["uploaded_images"])],
                uniqueCode=str(time.time_ns() // 1000), sourceId="pcMainPublish", bizcode="pcMainPublish", publishScene="pcMainPublish")
    for key in ("freebies", "canBargain", "supportBargainPrice", "defaultPrice"):
        if body.get(key) in ("true", "false"):
            body[key] = body[key] == "true"
    for key in ("canFreeShipping", "supportFreight", "onlyTakeSelf"):
        if body.get("itemPostFeeDTO", {}).get(key) in ("true", "false"):
            body["itemPostFeeDTO"][key] = body["itemPostFeeDTO"][key] == "true"
    for protocol in body.get("userRightsProtocols", []):
        if protocol.get("enable") in ("true", "false"):
            protocol["enable"] = protocol["enable"] == "true"
    return body


def preserved(before, after):
    checks = pub.inventory_preserved(before, after)
    for key in ("itemTextDTO", "imageInfoDOList"):
        checks.pop(key)
    checks["quantity"] = str(before.get("quantity")) == str(after.get("quantity"))
    return checks


async def _verify(store, row, client):
    owned_ids = {p["item_id"] for p in await client.products()}
    data = (await client._post_mtop(api_name=EDIT_DETAIL_API, payload={"itemId": row["item_id"]})).get("data", {})
    checks = pub.matches(row["preview"], row["uploaded_images"], data, item_id=row["item_id"], owned_ids=owned_ids)
    keep = preserved(row["before"], data)
    row.update(checks=checks, preserved_checks=keep, platform_status=normalize_item_status(data.get("itemStatus"), owned=True),
               verified_at=now(), observed={key: data.get(key) for key in ("itemId", "itemStatus", "itemTextDTO", "imageInfoDOList", "quantity", "itemPriceDTO")})
    if not all(checks.values()) or not all(keep.values()) or row["platform_status"] != "在线":
        row.update(state="needs_review", message="文图更新已发送，回读未完全匹配或尚未在线；停止重复提交。")
        return _save(store, row)
    # Adopt a verified current view, preserving the original publish preview and
    # all earlier edit attempts as evidence. Historical stock checks belong to
    # their old baseline; subsequent stock edits start from this current view.
    record = pub.current(store, row["account"], row["slug"])
    record["content_current"] = {"id": row["id"], "preview": row["preview"], "uploaded_images": row["uploaded_images"], "verified_at": now()}
    if record.get("inventory_update"):
        store.put("publication_inventory_history", record["inventory_update"]["id"], record.pop("inventory_update"), account=row["account"])
    record.update(state="published", verified_at=now(), checks=checks, observed=row["observed"], message="新版介绍、商品图和原价格库存已核对在线。")
    pub.save(store, record)
    key = product_key(row["account"], row["item_id"])
    product = store.get("product", key)
    if product:
        product.update(title=row["title"], description=row["description"], image_url=row["uploaded_images"][0]["url"], status="在线")
        store.put("product", key, product, account=row["account"])
    row.update(state="verified", message="新版介绍和商品图已更新；价格、库存、规格、地区及邮寄设置保持原值。")
    return _save(store, row)


async def apply(store, account, slug, preview_id, approved_digest, *, client_factory=MtopClient, uploader=pub.upload):
    row, fresh = _claim(store, account, slug, preview_id, approved_digest)
    if not fresh:
        return row
    try:
        async with client_factory(store, account) as client:
            data = (await client._post_mtop(api_name=EDIT_DETAIL_API, payload={"itemId": row["item_id"]})).get("data", {})
            if _fingerprint(data) != row["before_digest"]:
                raise ValueError("商品在核对后已发生变化，未覆盖")
            row.update(state="uploading", uploaded_images=[])
            _save(store, row)
            for expected in row["images"]:
                raw, actual = pub.image_bytes(slug, expected["name"])
                if actual != expected:
                    raise ValueError("图片内容发生变化，停止更新")
                row["uploaded_images"].append(await uploader(client, raw, actual))
                _save(store, row)
            # Uploads may take time. Do not overwrite a sale/stock change or an
            # edit made on the phone while they were in flight.
            data = (await client._post_mtop(api_name=EDIT_DETAIL_API, payload={"itemId": row["item_id"]})).get("data", {})
            if _fingerprint(data) != row["before_digest"]:
                raise ValueError("上传期间商品已发生变化，未覆盖")
            if pub.identity(store, account) != row["preview"]["user_id"]:
                raise ValueError("上传期间账号发生变化，停止更新")
            if commerce.build_bundles(store, account, slug)["delivery_zip_sha256"] != row["delivery_sha256"]:
                raise ValueError("上传期间交付内容发生变化，停止更新")
            _check_delivery(store, account, slug, row["item_id"], row["delivery_sha256"])
            row.update(state="sending", sent_at=now())
            _save(store, row)
            await client._post_mtop(api_name=EDIT_API, payload=edit_payload(data, row), spm_cnt="a21ybx.publish.0.0",
                                    _content_authorized=True, _refresh_token_once=False)
            row.update(state="acknowledged", message="平台已接收一次更新，等待回读")
            _save(store, row)
            return await _verify(store, row, client)
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
        before_send = row["state"] in {"claimed", "uploading"}
        row.update(state="failed_before_send" if before_send else "rejected" if row["state"] == "sending" and code in pub.DEFINITE_REJECTIONS else "unknown",
                   error_code=code, message=str(exc) if isinstance(exc, (MarketError, ValueError)) else "更新结果待核对；不会重复发送")
        return _save(store, row)


async def reconcile(store, account, slug, *, client_factory=MtopClient):
    row = current(store, account, slug)
    if not row or row.get("state") not in {"sending", "acknowledged", "unknown", "needs_review", "verified"}:
        raise ValueError("没有可回读的文图更新")
    if pub.identity(store, account) != row["preview"]["user_id"]:
        raise ValueError("账号发生变化，停止回读")
    if row["state"] == "verified":
        return row
    async with client_factory(store, account) as client:
        return await _verify(store, row, client)


def recover(store):
    for row in store.rows("listing_edit"):
        if row.get("state") in {"claimed", "uploading", "sending"}:
            row.update(state="unknown" if row["state"] == "sending" else "failed_before_send",
                       message="后台在更新期间中断，发送后只允许回读")
            _save(store, row)
