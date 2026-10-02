"""Owned new-listing publisher. A frozen review permits one external write only."""
from __future__ import annotations

import hashlib
import copy
import io
import json
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse

import aiohttp
from PIL import Image

from . import commerce
from .marketplace import (MtopClient, MarketError, CATEGORY_API, PUBLISH_API, EDIT_API, EDIT_DETAIL_API, ITEM_LIST_API,
                          parse_cookie, serialize_cookie, normalize_item_status)
from .store import now, product_key

RETRYABLE = {"failed_before_publish", "rejected", "interrupted_before_publish"}
DEFINITE_REJECTIONS = {"FAIL_SYS_TOKEN_EMPTY", "FAIL_SYS_TOKEN_EXPIRED", "FAIL_SYS_TOKEN_EXOIRED",
                       "FAIL_SYS_SESSION_EXPIRED", "TOKEN_MISSING", "FAIL_SYS_USER_VALIDATE",
                       "FAIL_SYS_ILLEGAL_ACCESS", "FAIL_SYS_PARAM_MISSING", "FAIL_SYS_PARAM_FORMAT_ERROR",
                       "ACCOUNT_VERIFICATION_REQUIRED", "MULTI_INVENTORY_ITEM_CAN_NOT_PUBLISH",
                       "FAIL_BIZ_BOOK_BARCODE_NOT_NULL"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def identity(store, account):
    uid = parse_cookie(store.cookie(account)).get("unb", "")
    if not uid.isdigit() or store.get("account", account, {}).get("platform_user_id") != uid:
        raise ValueError("当前登录账号身份尚未核实，请先连接现有登录。")
    return uid


def image_bytes(slug, name):
    path = commerce.source_file(slug, "listing", name)
    raw = path.read_bytes()
    if not 0 < len(raw) <= 8_000_000:
        raise ValueError("上架图片须小于 8 MB")
    with Image.open(io.BytesIO(raw)) as pic:
        fmt, width, height = pic.format, pic.width, pic.height
        if fmt not in {"PNG", "JPEG", "WEBP"} or width * height > 25_000_000:
            raise ValueError("上架图片格式或尺寸不支持")
        pic.verify()
    return raw, {"name": name, "sha256": hashlib.sha256(raw).hexdigest(), "width": width, "height": height,
                 "mime": {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[fmt]}


def validate_text(spec, values):
    if spec["sale_type"] == "internal" or spec.get("limitations"):
        raise ValueError("内部工具或仍有交付缺口的商品不能直接上架")
    title, description = values.get("title"), values.get("description")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 60:
        raise ValueError("请填写 1–60 字标题")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 5000:
        raise ValueError("请填写 1–5000 字介绍")
    price, quantity = values.get("price_cents"), values.get("quantity", commerce.default_quantity(spec))
    if type(price) is not int or not 1 <= price <= 99_999_900:
        raise ValueError("价格须为正数，以整数分保存")
    if type(quantity) is not int or not 1 <= quantity <= 9999:
        raise ValueError("库存须为 1–9999 的整数")
    return {"title": title.strip(), "description": description.strip(), "price_cents": price, "quantity": quantity}


def recommended_category(data):
    predicted = data.get("categoryPredictResult", {})
    if not predicted.get("channelCatId"):
        selected = [value for card in data.get("cardList", [])
                    if str(card.get("cardData", {}).get("propertyId")) == "-10000"
                    for value in card["cardData"].get("valuesList", []) if str(value.get("isClicked")) == "1"]
        if len(selected) == 1:
            predicted = selected[0]
    category = {k: str(predicted[k]) for k in ("catId", "catName", "channelCatId", "tbCatId") if predicted.get(k) not in (None, "")}
    if any(not category.get(k, "").isdigit() for k in ("catId", "channelCatId")) or not category.get("catName"):
        raise ValueError("平台未返回完整分类；请调整文案后重新预览，不能猜测分类编号。")
    return category


def current(store, account, slug):
    return store.get("publication", product_key(account, slug))


def effective_content(record):
    content = record.get("content_current", {})
    preview = dict(content.get("preview", record["preview"]))
    inventory = record.get("inventory_update", {})
    if inventory.get("state") in {"sending", "acknowledged", "unknown", "verified"}:
        preview["quantity"] = inventory["quantity"]
    return preview, content.get("uploaded_images", record.get("uploaded_images", []))


def save(store, record):
    record["updated_at"] = now()
    store.put("publication", product_key(record["account"], record["slug"]), record, account=record["account"])
    # Keep attempts as external-operation evidence even after an explicitly reviewed retry.
    store.put("publication_history", record["id"], record, account=record["account"])
    return record


def recover(store):
    for record in store.rows("publication"):
        if record.get("inventory_update", {}).get("state") == "sending":
            record["inventory_update"].update(state="unknown", message="库存发送期间中断，只允许回读核对。")
            save(store, record)
        if record["state"] in {"claimed", "uploading"}:
            record.update(state="interrupted_before_publish", message="发布请求尚未开始，重新核对预览后可提交。")
            save(store, record)
        elif record["state"] == "sending":
            record.update(state="unknown", message="发送期间后台中断；只允许回读核对，不自动重发。")
            save(store, record)


async def prepare(store, account, slug, values, *, client_factory=MtopClient):
    if not isinstance(values, dict):
        raise ValueError("发布预览须为对象")
    existing = current(store, account, slug)
    if existing and existing["state"] not in RETRYABLE:
        raise ValueError("该商品已有发布记录，请先查看或回读现有记录，避免重复上架。")
    spec = commerce.offer(slug)
    listing = json.loads(commerce.source_file(slug, "listing", "listing.json").read_text(encoding="utf-8"))
    fields = validate_text(spec, {"title": listing["title"], "description": listing["description"],
                                "price_cents": listing["proposed_price_cents"], "quantity": commerce.default_quantity(spec), **values})
    bundle = commerce.build_bundles(store, account, slug)
    names = values.get("images", ["主图.png"])
    if not isinstance(names, list) or not 1 <= len(names) <= 9 or any(not isinstance(n, str) for n in names) or len(set(names)) != len(names):
        raise ValueError("请选择 1–9 张不同的上架图片")
    images = [image_bytes(slug, name)[1] for name in names]
    uid = identity(store, account)
    unique = str(time.time_ns() // 1000)
    async with client_factory(store, account) as client:
        products = await client.products()
        source_id = store.setting(f"focus_item:{account}")
        if source_id not in {p["item_id"] for p in products}:
            source_id = next((p["item_id"] for p in products), None)
        if not source_id:
            raise ValueError("当前账号没有可核实的发布地区，请先在闲鱼保存一件商品的地区。")
        owned = (await client._post_mtop(api_name=EDIT_DETAIL_API, payload={"itemId": source_id})).get("data", {})
        # editDetail currently redacts this field to "0" on the real account.
        # Membership in the authenticated own-item list supplies identity proof.
        if str(owned.get("userId") or "0") not in {"0", uid}:
            raise ValueError("地区来源商品不属于当前账号")
        addr = owned.get("itemAddrDTO", {})
        # Never copy precise GPS, POI or home address into a new listing.
        address = {k: str(addr.get(k) or "") for k in ("prov", "city", "area", "divisionId")}
        if not address["divisionId"].isdigit() or not address["city"] or not address["area"]:
            raise ValueError("平台没有返回可核实的区县信息")
        result = await client._post_mtop(api_name=CATEGORY_API, version="2.0", spm_cnt="a21ybx.publish.0.0",
            payload={"title": fields["title"], "description": fields["description"], "lockCpv": False,
                     "multiSKU": False, "publishScene": "mainPublish", "scene": "newPublishChoice",
                     "imageInfos": [], "uniqueCode": unique})
    category = recommended_category(result.get("data", {}))
    record = {"id": uuid.uuid4().hex, "account": account, "slug": slug, "user_id": uid, **fields,
              "images": images, "category": category, "address": address, "address_source_item": source_id,
              "shipping": "无需邮寄", "unique_code": unique, "created_at": now(),
              "known_item_ids": [p["item_id"] for p in products], "bundle_sha256": bundle["content_sha256"],
              "delivery": "在平台会话手动交付；本次上架不会创建自动发货规则"}
    record["digest"] = digest(record)
    store.put("publish_preview", record["id"], record, account=account)
    return record


def claim(store, account, slug, preview_id, approved_digest):
    preview = store.get("publish_preview", preview_id)
    if not preview or preview["account"] != account or preview["slug"] != slug or preview["digest"] != approved_digest:
        raise ValueError("预览不存在或内容已变化，请重新核对")
    if (datetime.fromisoformat(now()) - datetime.fromisoformat(preview["created_at"])).total_seconds() > 7200:
        raise ValueError("预览超过两小时，请重新核对当前账号和分类")
    if identity(store, account) != preview["user_id"]:
        raise ValueError("当前账号与预览不同")
    record = {"id": preview_id, "account": account, "slug": slug, "preview": preview, "state": "claimed",
              "created_at": now(), "message": "已确认内容，等待上传图片。", "producer": "codex",
              "producer_role": "controller", "producer_evidence": f"approved_preview:{preview_id}:{approved_digest}",
              "review_owner": "user", "review_state": "reviewed", "canonical_status": "record"}
    key = product_key(account, slug)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT payload FROM records WHERE kind='publication' AND key=?", (key,)).fetchone()
        if old:
            previous = json.loads(old[0])
            if previous["state"] not in RETRYABLE or previous["id"] == preview_id:
                raise ValueError("这份发布已执行或等待核对，不会再次提交。")
        db.execute("INSERT INTO records(kind,key,account,payload,source,saved_at) VALUES('publication',?,?,?,?,?) "
                   "ON CONFLICT(kind,key) DO UPDATE SET payload=excluded.payload,saved_at=excluded.saved_at",
                   (key, account, json.dumps(record, ensure_ascii=False), "approved_publish_preview", now()))
    return record


async def upload(client, raw, info):
    form = aiohttp.FormData()
    form.add_field("file", raw, filename=info["name"], content_type=info["mime"])
    headers = {"Cookie": serialize_cookie(client.cookies), "Origin": "https://www.goofish.com",
               "Referer": "https://www.goofish.com/", "User-Agent": client.user_agent}
    async with client.session.post("https://stream-upload.goofish.com/api/upload.api", data=form,
            params={"floderId": "0", "appkey": "xy_chat", "_input_charset": "utf-8"}, headers=headers,
            allow_redirects=False) as response:
        if response.status != 200:
            raise ValueError(f"图片上传返回 HTTP {response.status}，尚未发布商品")
        result = await response.json(content_type=None)
    uploaded = result.get("object", {}) if isinstance(result, dict) else {}
    url = uploaded.get("url", "") if isinstance(uploaded, dict) else ""
    if not isinstance(url, str):
        raise ValueError("图片上传结果缺少地址，尚未发布商品")
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not (parsed.hostname or "").endswith(".alicdn.com") or parsed.username or parsed.password:
        raise ValueError("图片上传未返回有效的平台图片地址，尚未发布商品")
    try:
        width, height = map(int, str(uploaded.get("pix", "")).split("x"))
        if width <= 0 or height <= 0 or width * height > 25_000_000:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError("图片上传结果缺少有效尺寸，尚未发布商品") from None
    return {"url": "https:" + url.split(":", 1)[1], "widthSize": width, "heightSize": height,
            "extraInfo": {"isH": "false", "isT": "false", "raw": "false"}, "isQrCode": False,
            "type": 0, "status": "done"}


def payload(preview, images):
    return {"freebies": False, "itemTypeStr": "b", "simpleItem": "true", "quantity": str(preview["quantity"]),
            "imageInfoDOList": [dict(img, major=(i == 0)) for i, img in enumerate(images)],
            "itemTextDTO": {"title": preview["title"], "desc": preview["description"], "titleDescSeparate": True},
            "itemPriceDTO": {"priceInCent": str(preview["price_cents"])}, "defaultPrice": False,
            "itemLabelExtList": [], "userRightsProtocols": [], "onlyTakeSelf": False,
            "itemPostFeeDTO": {"canFreeShipping": False, "supportFreight": False, "onlyTakeSelf": False, "templateId": "0"},
            "itemAddrDTO": dict(preview["address"], gps="", poiId="", poiName=preview["address"]["area"]),
            "itemCatDTO": preview["category"], "uniqueCode": preview["unique_code"],
            "sourceId": "pcMainPublish", "bizcode": "pcMainPublish", "publishScene": "pcMainPublish"}


def image_key(url):
    parsed = urlparse(str(url))
    path = parsed.path
    # Observed on the real publish/editDetail round trip: the complete asset
    # suffix is unchanged while the CDN switches these two storage prefixes.
    for prefix in ("/imgextra/", "/bao/uploaded/"):
        if path.startswith(prefix):
            path = "/uploaded/" + path[len(prefix):]
            break
    return (parsed.hostname, path)


def matches(preview, sent_images, data, *, item_id=None, owned_ids=()):
    text = data.get("itemTextDTO", {})
    images = data.get("imageInfoDOList", [])
    fees = data.get("itemPostFeeDTO", {})
    owner = str(data.get("userId") or "0")
    return {"item_id": item_id is not None and str(data.get("itemId")) == item_id,
            "owner": owner == preview["user_id"] or (owner == "0" and item_id in owned_ids),
            "title": text.get("title") == preview["title"], "description": text.get("desc") == preview["description"],
            "price": str(data.get("itemPriceDTO", {}).get("priceInCent")) == str(preview["price_cents"]),
            "quantity": str(data.get("quantity")) == str(preview["quantity"]),
            "category": str(data.get("itemCatDTO", {}).get("channelCatId")) == preview["category"]["channelCatId"],
            "district": str(data.get("itemAddrDTO", {}).get("divisionId")) == preview["address"]["divisionId"],
            "shipping": all(str(fees.get(k)).lower() == "false" for k in ("supportFreight", "onlyTakeSelf", "canFreeShipping")),
            "images": bool(sent_images) and [image_key(i.get("url")) for i in images] == [image_key(i["url"]) for i in sent_images]}


async def verify_image_content(client, preview, data):
    """The CDN rewrites imgextra to bao/uploaded; compare content, not just names."""
    blocked = client.store.get("publication_image_block", client.account) if hasattr(client, "store") else None
    if blocked:
        return False, [{"state": "blocked", "code": blocked["code"], "message": "图片内容读取仍需平台校验，未重复请求。"}]
    remote = data.get("imageInfoDOList", [])
    if not isinstance(remote, list) or len(remote) != len(preview["images"]):
        return False, []
    evidence = []
    for expected, image in zip(preview["images"], remote):
        url = image.get("url", "")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not (parsed.hostname or "").endswith(".alicdn.com") or parsed.username or parsed.password:
            return False, evidence
        secure_url = "https:" + url.split(":", 1)[1]
        async with client.session.get(secure_url, allow_redirects=False) as response:
            if response.status != 200:
                evidence.append({"url": secure_url, "state": "unavailable", "http_status": response.status})
                if response.status in {403, 420, 429} and hasattr(client, "store"):
                    client.store.put("publication_image_block", client.account,
                                     {"code": f"CDN_HTTP_{response.status}", "blocked_at": now()}, account=client.account)
                return False, evidence
            raw = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                raw.extend(chunk)
                if len(raw) > 8_000_000:
                    return False, evidence
        observed_hash = hashlib.sha256(raw).hexdigest()
        exact = observed_hash == expected["sha256"]
        pixels = False
        if not exact:
            local, actual = image_bytes(preview["slug"], expected["name"])
            if actual["sha256"] == expected["sha256"]:
                with Image.open(io.BytesIO(raw)) as online, Image.open(io.BytesIO(local)) as source:
                    if online.size == source.size and online.width * online.height <= 25_000_000:
                        pixels = online.convert("RGBA").tobytes() == source.convert("RGBA").tobytes()
        evidence.append({"url": secure_url, "sha256": observed_hash, "byte_match": exact, "pixel_match": pixels})
        if not (exact or pixels):
            return False, evidence
    return True, evidence


async def verify(store, record, client):
    preview, effective_images = effective_content(record)
    inventory = record.get("inventory_update", {})
    if inventory.get("state") in {"sending", "acknowledged", "unknown", "verified"}:
        preview["quantity"] = inventory["quantity"]
    item_id = record.get("item_id")
    list_block = store.get("api_block", product_key(record["account"], ITEM_LIST_API))
    known_owner = (item_id and record.get("checks", {}).get("owner") is True
                   and record.get("checks", {}).get("item_id") is True
                   and str(record.get("observed", {}).get("itemId")) == str(item_id)
                   and identity(store, record["account"]) == preview["user_id"])
    if list_block and known_owner:
        # Do not retry a blocked list just to reread a known, previously owned item.
        # Current editDetail must still match the exact ID and every listing field;
        # an explicit different owner is rejected by matches(), even with this proof.
        products, owned_ids = [], {item_id}
        record["ownership_evidence"] = "prior_owned_publication_and_current_exact_edit_detail"
    else:
        products = await client.products()
        owned_ids = {p["item_id"] for p in products}
        record["ownership_evidence"] = "current_owned_item_list_and_edit_detail"
    if not item_id:
        candidates = [p for p in products if p["item_id"] not in preview["known_item_ids"] and p.get("title") == preview["title"]]
        found = []
        for item in candidates[:5]:
            data = (await client._post_mtop(api_name=EDIT_DETAIL_API, payload={"itemId": item["item_id"]})).get("data", {})
            candidate_checks = matches(preview, record.get("uploaded_images", []), data, item_id=item["item_id"], owned_ids=owned_ids)
            if all(value for key, value in candidate_checks.items() if key != "images") and not candidate_checks["images"]:
                candidate_checks["images"], _ = await verify_image_content(client, preview, data)
            if all(candidate_checks.values()):
                found.append(item["item_id"])
        if len(found) != 1:
            record.update(state="unknown", message="尚未唯一确认发布结果；未重新发送发布请求。")
            return save(store, record)
        item_id = record["item_id"] = found[0]
    data = (await client._post_mtop(api_name=EDIT_DETAIL_API, payload={"itemId": item_id})).get("data", {})
    checks = matches(preview, effective_images, data, item_id=item_id, owned_ids=owned_ids)
    if inventory.get("state") in {"sending", "acknowledged", "unknown", "verified"}:
        inventory["preserved_checks"] = inventory_preserved(inventory["before"], data)
        if checks["quantity"] and all(inventory["preserved_checks"].values()):
            inventory.update(state="verified", verified_at=now(), message="库存及其他原有字段已回读核对。")
        checks["inventory_preserved"] = all(inventory["preserved_checks"].values())
    if not checks["images"]:
        checks["images"], record["image_evidence"] = await verify_image_content(client, preview, data)
    else:
        record["image_evidence"] = [{"state": "asset_identity_matched", "basis": "cdn_host_and_full_asset_key",
                                     "pixel_verification": "not_performed"}]
    state = normalize_item_status(data.get("itemStatus"), owned=True)
    record["observed"] = {key: data.get(key) for key in ("itemId", "uniqueCode", "itemStatus", "itemTextDTO",
                                                        "itemPriceDTO", "quantity", "itemCatDTO", "imageInfoDOList", "itemPostFeeDTO")}
    record.update(checks=checks, platform_status=state, verified_at=now(), item_url=f"https://www.goofish.com/item?id={item_id}")
    if all(checks.values()) and state == "在线":
        record.update(state="published", message="平台返回商品 ID，本人商品详情和图片标识已核对为在线；请打开闲鱼查看买家页面。")
    elif all(checks.values()) and state == "审核中":
        record.update(state="pending_review", message="平台已接收，商品仍在审核中。")
    elif all(checks.values()) and state == "unknown":
        record.update(state="needs_review", message=f"商品内容和库存已核对，平台状态码为 {data.get('itemStatus')}；尚不能确认正常在售，请在闲鱼查看该商品状态。")
    else:
        record.update(state="needs_review", message="已取得商品 ID，部分内容或平台状态需要核对；不会重发。")
    if checks["owner"]:
        key = product_key(record["account"], item_id)
        product = store.get("product", key, {})
        product.setdefault("managed", False)
        product.setdefault("watch", False)
        product.update({"account": record["account"], "item_id": item_id, "title": preview["title"],
                        "slug": record["slug"], "status": state, "price": preview["price_cents"] / 100,
                        "image_url": effective_images[0]["url"]})
        store.put("product", key, product, account=record["account"], source="owned_publication_readback")
    return save(store, record)


INVENTORY_FIELDS = ("itemTextDTO", "itemPriceDTO", "imageInfoDOList", "itemCatDTO", "itemAddrDTO",
                    "itemPostFeeDTO", "itemSkuList", "itemProperties", "properties", "userRightsProtocols")


def inventory_preserved(before, after):
    def semantic(value):
        if isinstance(value, dict):
            return {k: semantic(v) for k, v in value.items() if k not in {"descPath", "status", "isQrCode"}}
        if isinstance(value, list):
            return [semantic(v) for v in value]
        if isinstance(value, bool) or value in ("true", "false"):
            return str(value).lower()
        return str(value) if value is not None else None
    return {key: semantic(before.get(key)) == semantic(after.get(key)) for key in INVENTORY_FIELDS}


async def update_inventory(store, account, slug, quantity, *, client_factory=MtopClient):
    """Edit this publisher's existing item once; uncertain writes require reconciliation."""
    if type(quantity) is not int or not 1 <= quantity <= 9999:
        raise ValueError("库存须为 1–9999 的整数")
    from .listing_edits import current as current_edit, PENDING
    if (current_edit(store, account, slug) or {}).get("state") in PENDING:
        raise ValueError("文图更新尚未核对，请先回读后再改库存")
    record = current(store, account, slug)
    if not record or record["state"] != "published" or not record.get("item_id"):
        raise ValueError("请先完成已有商品的发布回读")
    if identity(store, account) != record["preview"]["user_id"]:
        raise ValueError("当前账号与发布记录不同")
    async with client_factory(store, account) as client:
        await verify(store, record, client)
        old = record.get("inventory_update", {})
        if old.get("state") in {"sending", "acknowledged", "unknown"}:
            raise ValueError("上次库存请求结果不确定，请先回读，不会重新发送。")
        if record["state"] != "published":
            raise ValueError("商品当前内容或状态已变化，未修改库存")
        data = (await client._post_mtop(api_name=EDIT_DETAIL_API, payload={"itemId": record["item_id"]})).get("data", {})
        expected, expected_images = effective_content(record)
        if not all(matches(expected, expected_images, data, item_id=record["item_id"],
                           owned_ids={record["item_id"]}).values()):
            raise ValueError("库存修改前的线上内容已变化，未覆盖商品")
        if data.get("itemSkuList"):
            raise ValueError("多规格商品须分别核对库存，未修改总库存")
        if str(data.get("quantity")) == str(quantity):
            return record
        # The first-party edit contract accepts editable fields and a publication stamp.
        # Preserve the live response; only quantity and request identity change.
        body = copy.deepcopy(data)
        body.update(quantity=str(quantity), uniqueCode=str(time.time_ns() // 1000),
                    sourceId="pcMainPublish", bizcode="pcMainPublish", publishScene="pcMainPublish")
        for key in ("freebies", "canBargain", "supportBargainPrice", "defaultPrice"):
            if body.get(key) in ("true", "false"):
                body[key] = body[key] == "true"
        for parent, names in (("itemPostFeeDTO", ("canFreeShipping", "supportFreight", "onlyTakeSelf")),
                              ("itemTextDTO", ("titleDescSeparate",))):
            for key in names:
                if body.get(parent, {}).get(key) in ("true", "false"):
                    body[parent][key] = body[parent][key] == "true"
        for protocol in body.get("userRightsProtocols", []):
            if protocol.get("enable") in ("true", "false"):
                protocol["enable"] = protocol["enable"] == "true"
        update = {"id": uuid.uuid4().hex, "quantity": quantity, "previous_quantity": data["quantity"],
                  "before": {k: data.get(k) for k in INVENTORY_FIELDS}, "state": "sending", "sent_at": now(),
                  "authorization": "update_this_listing_inventory", "account": account, "item_id": record["item_id"]}
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = json.loads(db.execute("SELECT payload FROM records WHERE kind='publication' AND key=?",
                                          (product_key(account, slug),)).fetchone()[0])
            if saved.get("inventory_update", {}).get("state") in {"sending", "acknowledged", "unknown"}:
                raise ValueError("已有库存请求正在执行或等待核对")
            record["inventory_update"] = update
            db.execute("UPDATE records SET payload=?,saved_at=? WHERE kind='publication' AND key=?",
                       (json.dumps(record, ensure_ascii=False), now(), product_key(account, slug)))
        try:
            await client._post_mtop(api_name=EDIT_API, payload=body, spm_cnt="a21ybx.publish.0.0",
                                    _inventory_authorized=True, _refresh_token_once=False)
            update.update(state="acknowledged", message="平台接收库存修改，等待回读。")
            save(store, record)
            return await verify(store, record, client)
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            update.update(state="rejected" if code in DEFINITE_REJECTIONS else "unknown", error_code=code,
                          message=str(exc) if isinstance(exc, (MarketError, ValueError)) else "库存请求中断，先回读核对。")
            return save(store, record)


async def publish(store, account, slug, preview_id, approved_digest, *, client_factory=MtopClient, uploader=upload):
    record = claim(store, account, slug, preview_id, approved_digest)
    preview = record["preview"]
    try:
        # All local checks happen before the first upload or publication.
        bundle = commerce.build_bundles(store, account, slug)
        if bundle["content_sha256"] != preview["bundle_sha256"]:
            raise ValueError("交付文件或上架素材已变化，请重新预览")
        blobs = [image_bytes(slug, info["name"]) for info in preview["images"]]
        if any(actual != expected for (_, actual), expected in zip(blobs, preview["images"])):
            raise ValueError("图片已变化，请重新预览")
        async with client_factory(store, account) as client:
            record.update(state="uploading", message="正在上传已确认的商品图片。")
            save(store, record)
            images = []
            for raw, info in blobs:
                images.append(await uploader(client, raw, info))
            if identity(store, account) != preview["user_id"]:
                raise ValueError("上传期间账号发生变化，未发布商品")
            record.update(state="sending", uploaded_images=images, sent_at=now(), message="已发送发布请求，等待平台确认。")
            save(store, record)  # durable before the external side effect
            response = await client._post_mtop(api_name=PUBLISH_API, payload=payload(preview, images),
                        spm_cnt="a21ybx.publish.0.0", _publish_authorized=True, _refresh_token_once=False)
            item_id = str(response.get("data", {}).get("itemId") or "")
            if not item_id.isdigit():
                record.update(state="unknown", message="发布接口返回成功但没有商品 ID；只能回读核对，不能重发。")
                return save(store, record)
            record.update(state="acknowledged", item_id=item_id, message="平台已返回商品 ID，正在回读核对。")
            save(store, record)
            return await verify(store, record, client)
    except Exception as exc:
        state = record["state"]
        code = getattr(exc, "code", type(exc).__name__)
        if state == "sending":
            state = "rejected" if isinstance(exc, MarketError) and code in DEFINITE_REJECTIONS else "unknown"
        elif state not in {"acknowledged", "published", "pending_review", "needs_review", "unknown"}:
            state = "failed_before_publish"
        message = str(exc) if isinstance(exc, (MarketError, ValueError)) else "网络或后台操作中断，请查看发布记录。"
        record.update(state=state, error_code=code, message=message)
        return save(store, record)


async def reconcile(store, account, slug, *, client_factory=MtopClient):
    from .listing_edits import current as current_edit, PENDING
    if (current_edit(store, account, slug) or {}).get("state") in PENDING:
        raise ValueError("现有文图更新待核对，请使用文图更新的回读入口")
    record = current(store, account, slug)
    if not record or record["state"] in {"claimed", "uploading", "sending"} | RETRYABLE:
        raise ValueError("当前没有可以回读的发布结果")
    if identity(store, account) != record["preview"]["user_id"]:
        raise ValueError("当前账号与发布记录不同")
    async with client_factory(store, account) as client:
        result = await verify(store, record, client)
        if result["state"] == "unknown" and not result.get("item_id") and result.get("error_code") in DEFINITE_REJECTIONS:
            result.update(state="rejected", message=f"平台明确拒绝发布（{result['error_code']}），回读也未发现对应新商品；可修正提案后重新预览。")
            save(store, result)
        return result
