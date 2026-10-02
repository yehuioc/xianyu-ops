"""Phone-ready listing bundles and conservative online-image comparison."""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
import shutil
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from .paths import DATA, PROJECT, OPS
from .store import Store, now, product_key, CHINA
from .engine import ops


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def listing_sources(product: dict) -> dict:
    slug = product.get("slug")
    if slug == "dsh-orangebook":
        directory = PROJECT / "products" / "dsh-orangebook" / "listing"
        images = [p for p in (directory / "images").glob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
        image = next((p for p in images if p.name == "02-main-owned-console.png"), None)
        image = image or next((p for p in images if p.name == "01-main-feed-v2.png"), None)
        preferred = product.get("preferred_image")
        if preferred:
            candidate = (PROJECT / preferred).resolve()
            if candidate.is_relative_to(directory / "images") and candidate.is_file():
                image = candidate
        copy = directory / "商品文案.md"
        title, description = ops().parse_copy_file(copy)
        return {"title": title, "description": description, "image": image, "copy_file": copy}
    raise ValueError("该商品尚未配置本地发布材料；现有交付资料和记录已保留。")


def prepare_bundle(store: Store, product: dict, *, title: str | None = None,
                   description: str | None = None, image_path: Path | None = None) -> dict:
    sources = listing_sources(product)
    title = (title if title is not None else sources["title"]).strip()
    description = (description if description is not None else sources["description"]).strip()
    image_path = image_path or sources["image"]
    if not title or not description or len(title) > 200 or len(description) > 20000:
        raise ValueError("标题和介绍不能为空，且需保持在合理长度内。")
    if image_path is None or not image_path.is_file():
        raise ValueError("还没有可用的商品主图。")
    image_path = image_path.resolve()
    if not image_path.is_relative_to(PROJECT / "products"):
        raise ValueError("发布图片必须来自本项目商品目录。")
    account, item_id = product["account"], product["item_id"]
    if product.get("price_cents") is None or product.get("category_id") is None or not product.get("observed_at"):
        raise ValueError("先在工作台记录一轮真实商品状态，再生成有价格和类目基线的素材包。")
    package_id = f"{account}-{item_id}-{datetime.now(CHINA):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    folder = DATA / "bundles" / package_id
    folder.mkdir(parents=True, exist_ok=False)
    main_image = folder / ("01-主图" + image_path.suffix.lower())
    shutil.copyfile(image_path, main_image)
    (folder / "标题.txt").write_text(title, encoding="utf-8-sig")
    (folder / "商品介绍.txt").write_text(description, encoding="utf-8-sig")
    (folder / "手机上传说明.txt").write_text(
        f"目标商品：{product.get('title', '')}\n商品 ID：{item_id}\n"
        "本包用于 DSH 橙皮书电子阅读资料商品，请不要上传到安装器或简历资料商品。\n\n"
        "打开闲鱼中对应的现有商品，编辑标题和介绍，将 01-主图 放在第一张。\n"
        "保留原来的价格、规格、库存和交付设置，不要新建重复商品。\n"
        "保存后即可。后台会在后续采集中核对新内容，并开始观察这一版的数据。\n"
        "你也可以回到后台点击“检查是否已上线”，立即核对。\n", encoding="utf-8-sig")
    baseline = {k: product.get(k) for k in ("title", "description", "price", "price_cents", "category_id", "quantity", "skus", "image_urls", "observed_at", "source")}
    package = {"schema": "xianyu-phone-bundle-v1", "package_id": package_id, "account": account,
               "item_id": item_id, "created_at": now(), "state": "ready_for_phone",
               "desired": {"title": title, "description": description}, "baseline": baseline,
               "image": {"file": str(main_image.relative_to(PROJECT)), "sha256": digest(main_image)},
               "publication": "user_phone_only", "online_verified": False,
               "folder": str(folder.relative_to(PROJECT))}
    (folder / "manifest.json").write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
    zip_path = folder.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "x", zipfile.ZIP_DEFLATED) as archive:
        for path in folder.iterdir():
            if path.name != "manifest.json":
                archive.write(path, path.name)
    package["zip_file"] = str(zip_path.relative_to(PROJECT))
    package["zip_sha256"] = digest(zip_path)
    store.put("package", package_id, package, account=account)
    previous = store.get("experiment", product_key(account, item_id))
    if previous and previous.get("content_live_at"):
        store.put("experiment_archive", previous["package_id"], previous, account=account)
    experiment = {"account": account, "item_id": item_id, "package_id": package_id,
                  "state": "awaiting_phone", "created_at": now(), "content_live_at": None,
                  "verification": {}, "review_completed_at": None}
    store.put("experiment", product_key(account, item_id), experiment, account=account)
    return package


def normalized_text(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "")


def compare_listing_copy(current: dict, desired: dict) -> dict:
    title_match = normalized_text(current.get("title")) == normalized_text(desired.get("title"))
    raw = current.get("description") or ""
    expected = desired.get("description") or ""
    matched = bool(expected) and normalized_text(raw) == normalized_text(expected)
    method = "normalized_exact" if matched else "different"
    if not matched and title_match and current.get("title_desc_separate") is False:
        # The combined editor field may contain the exact title as its first line.
        # Only that declared representation is tolerated; the remaining body must match in full.
        first, separator, body = raw.partition("\n")
        if (separator and normalized_text(first) == normalized_text(desired.get("title"))
                and expected and normalized_text(body) == normalized_text(expected)):
            matched, method = True, "combined_field_exact_title_prefix"
    return {"title": title_match, "description": matched, "description_method": method}


def compare_image_bytes(expected: bytes, current: bytes) -> dict:
    if hashlib.sha256(expected).digest() == hashlib.sha256(current).digest():
        return {"match": True, "method": "sha256", "pixel_rmse": 0.0}
    try:
        with Image.open(io.BytesIO(expected)) as image:
            if image.width * image.height > 25_000_000:
                raise ValueError("source image too large")
            source_ratio = image.width / image.height
            source = image.convert("RGB").resize((256, 256), Image.Resampling.LANCZOS)
        with Image.open(io.BytesIO(current)) as image:
            if image.width * image.height > 25_000_000:
                raise ValueError("online image too large")
            ratio = image.width / image.height
            target = image.convert("RGB").resize((256, 256), Image.Resampling.LANCZOS)
        diff = ImageChops.difference(source, target)
        rmse = math.sqrt(sum(v * v for v in ImageStat.Stat(diff).rms) / 3) / 255
        def dhash(image):
            pixels = list(image.convert("L").resize((17, 16), Image.Resampling.LANCZOS).getdata())
            return [pixels[y * 17 + x] > pixels[y * 17 + x + 1] for y in range(16) for x in range(16)]
        differences = sum(a != b for a, b in zip(dhash(source), dhash(target)))
        match = abs(source_ratio - ratio) < .025 and rmse <= .025 and differences <= 8
        return {"match": match, "method": "normalized_pixels_and_dhash", "pixel_rmse": round(rmse, 5),
                "dhash_distance": differences, "aspect_ratio_delta": round(abs(source_ratio - ratio), 5)}
    except (OSError, ValueError, Image.DecompressionBombError):
        return {"match": False, "method": "unreadable_image"}


def online_image_check(store: Store, package: dict, url: str) -> dict:
    expected_path = (PROJECT / package["image"]["file"]).resolve()
    if not expected_path.is_relative_to(DATA / "bundles") or not expected_path.is_file():
        return {"match": False, "method": "bundle_image_missing"}
    if digest(expected_path) != package["image"]["sha256"]:
        return {"match": False, "method": "bundle_image_changed"}
    key = package["package_id"] + ":" + hashlib.sha256(url.encode()).hexdigest()[:16]
    cached = store.get("image_evidence", key)
    if cached and cached.get("expected_sha256") == package["image"]["sha256"]:
        return cached
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"https", "http"} or parsed.port not in {None, 80, 443} or not host.endswith(".alicdn.com"):
        return {"match": False, "method": "unsupported_image_host", "url": url}
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.goofish.com/"})
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            content = response.read(8_000_001)
        if len(content) > 8_000_000:
            raise ValueError("image size limit")
        expected_path = (PROJECT / package["image"]["file"]).resolve()
        if not expected_path.is_relative_to(DATA / "bundles"):
            raise ValueError("image outside bundle")
        result = compare_image_bytes(expected_path.read_bytes(), content)
        evidence_file = DATA / "evidence" / (hashlib.sha256(content).hexdigest() + ".image")
        if not evidence_file.exists():
            evidence_file.write_bytes(content)
        result.update({"url": url, "checked_at": now(), "package_id": package["package_id"],
                       "expected_sha256": package["image"]["sha256"], "online_sha256": hashlib.sha256(content).hexdigest(),
                       "file": str(evidence_file.relative_to(PROJECT))})
        store.put("image_evidence", key, result, account=package["account"])
        return result
    except (OSError, ValueError, TimeoutError):
        return {"match": False, "method": "image_read_unavailable", "url": url}
