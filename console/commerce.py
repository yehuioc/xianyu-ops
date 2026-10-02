"""Source-led product workbench. Local preparation never implies a marketplace sale."""
from __future__ import annotations

import hashlib
import json
import re
import statistics
import uuid
import zipfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .marketplace import MtopClient, MarketError, SEARCH_API
from .paths import PROJECT, DATA
from .store import Store, now, product_key

CATALOG = PROJECT / "products" / "monetization" / "catalog.json"
PRODUCTS = CATALOG.parent
SLUG = re.compile(r"^[a-z][a-z0-9-]{1,70}$")


def default_quantity(spec: dict) -> int:
    return 9999 if spec["sale_type"] == "digital" else 99


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def product_directory(slug: str) -> Path:
    if not isinstance(slug, str) or not SLUG.fullmatch(slug):
        raise ValueError("商品标识不正确")
    path = (PRODUCTS / slug).resolve()
    if not path.is_relative_to(PRODUCTS.resolve()) or not path.is_dir():
        raise ValueError("商品目录不存在")
    return path


def read_catalog() -> dict:
    if not CATALOG.is_file():
        return {"products": [], "methods": [], "deferred": []}
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def offer(slug: str) -> dict:
    product_directory(slug)
    item = next((row for row in read_catalog()["products"] if row["slug"] == slug), None)
    if item is None:
        raise ValueError("未找到该商品")
    return item


def source_file(slug: str, section: str, relative: str) -> Path:
    if section not in {"delivery", "listing", "sample"}:
        raise ValueError("文件类型不正确")
    root = product_directory(slug)
    base = root / section
    path = (base / relative).resolve()
    if not base.resolve().is_relative_to(root) or not path.is_relative_to(base.resolve()) or not path.is_file():
        raise ValueError("文件不存在或超出该商品目录")
    return path


def file_list(slug: str, section: str) -> list[dict]:
    base = product_directory(slug) / section
    if not base.exists() or not base.resolve().is_relative_to(product_directory(slug)):
        return []
    return [{"name": p.relative_to(base).as_posix(), "bytes": p.stat().st_size, "sha256": sha256(p)}
            for p in sorted(base.rglob("*")) if p.is_file() and not p.is_symlink()
            and p.resolve().is_relative_to(base.resolve())
            and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".tmp", ".ndjson"}]


def portfolio(store: Store, account: str) -> dict:
    catalog = read_catalog()
    rows = []
    for spec in catalog["products"]:
        row = dict(spec)
        row["default_quantity"] = default_quantity(spec)
        slug = spec["slug"]
        row["delivery_files"] = file_list(slug, "delivery")
        row["sample_files"] = file_list(slug, "sample")
        row["listing_files"] = file_list(slug, "listing")
        names = {f["name"] for f in row["delivery_files"]}
        row["missing_files"] = [name for name in spec.get("required_files", []) if name not in names]
        row["listing"] = json.loads(source_file(slug, "listing", "listing.json").read_text(encoding="utf-8"))
        row["publication"] = store.get("publication", product_key(account, slug))
        from .listing_edits import public_result
        edit = store.get("listing_edit", product_key(account, slug))
        row["listing_edit"] = public_result(edit) if edit else None
        row["quark_delivery"] = store.get("quark_delivery", product_key(account, slug))
        row["quark_binding"] = store.get("quark_binding", product_key(account, slug))
        row["bundles"] = [r for r in store.rows("commerce_bundle", account) if r["slug"] == slug][-2:]
        # Customer work still requires that customer's input and acceptance.
        row["state"] = ("in_production" if row["missing_files"] else
                        "internal_only" if spec["sale_type"] == "internal" else
                        "sample_partial" if spec.get("limitations") else
                        "service_prepared" if spec["sale_type"] == "service" and row["delivery_files"] else
                        "local_ready" if row["delivery_files"] else "in_production")
        rows.append(row)
    searches = sorted(store.rows("market_search", account), key=lambda r: r["captured_at"], reverse=True)
    return {**catalog, "products": rows, "searches": searches[:30], "captured_at": now(),
            "publishing": "review_then_automatic_publish", "revenue_claim": "no_sales_claim"}


def build_bundles(store: Store, account: str, slug: str) -> dict:
    spec = offer(slug)
    delivery, listing = file_list(slug, "delivery"), file_list(slug, "listing")
    if not delivery or not listing:
        raise ValueError("交付成品或上架素材尚未完成")
    missing = set(spec.get("required_files", [])) - {f["name"] for f in delivery}
    if missing:
        raise ValueError("交付缺少必要文件：" + "、".join(sorted(missing)))
    digest = hashlib.sha256(json.dumps({"delivery": delivery, "listing": listing}, sort_keys=True).encode()).hexdigest()
    key = product_key(account, slug) + ":" + digest[:16]
    old = store.get("commerce_bundle", key)
    if old and all((PROJECT / old[k]).is_file() and sha256(PROJECT / old[k]) == old[k + "_sha256"] for k in ("delivery_zip", "listing_zip")):
        return old
    base = DATA / "commerce" / "bundles" / slug / digest[:16]
    base.mkdir(parents=True, exist_ok=True)
    record = {"id": key, "slug": slug, "account": account, "content_sha256": digest,
              "created_at": now(), "status": "local_ready", "sale_type": spec["sale_type"]}
    for section, files in (("delivery", delivery), ("listing", listing)):
        delivery_name = "买家交付包.zip" if spec["sale_type"] == "digital" else "服务样例包.zip" if spec["sale_type"] == "service" else "内部工具包.zip"
        dest = base / (delivery_name if section == "delivery" else "手机上架包.zip")
        staging = dest.with_name("." + uuid.uuid4().hex + ".tmp")
        with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                # Internal source IDs, proposals and account information never enter buyer packages.
                if section == "listing" and f["name"] == "listing.json":
                    continue
                path = source_file(slug, section, f["name"])
                info = zipfile.ZipInfo.from_file(path, f["name"])
                content = path.read_bytes()
                if len(content) != f["bytes"] or hashlib.sha256(content).hexdigest() != f["sha256"]:
                    raise ValueError("打包期间文件已变化，请重新核对后打包：" + f["name"])
                z.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED)
        staging.replace(dest)
        record[section + "_zip"] = str(dest.relative_to(PROJECT))
        record[section + "_zip_sha256"] = sha256(dest)
        record[section + "_files"] = files
    store.put("commerce_bundle", key, record, account=account)
    return record


def parse_amount(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        value = str(value).strip().replace("¥", "").replace("￥", "").replace(",", "")
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            return None
        return int((number * 100).quantize(Decimal("1"))) if number <= Decimal('1000000000') else None
    except (InvalidOperation, ValueError):
        return None


def normalize_search(response: dict, keyword: str, limit: int) -> list[dict]:
    def obj(value):
        return value if isinstance(value, dict) else {}
    results = obj(obj(response).get("data")).get("resultList")
    if not isinstance(results, list):
        raise MarketError("SEARCH_SCHEMA_UNKNOWN", "搜索结果结构已变化，没有把缺失数据记为零。")
    rows, seen = [], set()
    for entry in results:
        if not isinstance(entry, dict):
            continue
        node = obj(obj(entry.get("data")).get("item"))
        main = obj(node.get("main"))
        arguments = obj(obj(main.get("clickParam")).get("args"))
        fields = obj(main.get("exContent") or node.get("exContent"))
        ident = str(arguments.get("item_id") or arguments.get("id") or "")
        title = fields.get("title") or obj(fields.get("detailParams")).get("title")
        if not ident.isdigit() or not isinstance(title, str) or not title.strip() or ident in seen:
            continue
        seen.add(ident)
        raw_want = arguments.get("wantNum")
        if raw_want is None:
            raw_want = fields.get("want")
        # No popularity inference from badges, recommendation text, or seller sales.
        want = int(str(raw_want)) if raw_want is not None and str(raw_want).isdigit() else None
        raw_price = arguments.get("price")
        if raw_price in (None, ""):
            raw_price = arguments.get("displayPrice")
        rows.append({"item_id": ident, "title": title.strip(), "asking_price_cents": parse_amount(raw_price),
                     "want": want, "want_raw": str(raw_want) if raw_want is not None else None,
                     "location": str(arguments.get("p_city") or fields.get("area") or ""),
                     "url": "https://www.goofish.com/item?id=" + ident, "query": keyword,
                     "source": "goofish_public_search", "sales": None})
        if len(rows) >= limit:
            break
    if results and not rows:
        raise MarketError("SEARCH_ROWS_UNKNOWN", "搜索返回了内容但无法识别商品，没有将它写成市场无需求。")
    return rows


async def search_market(store: Store, account: str, keyword: str, *, limit: int = 15) -> dict:
    if not isinstance(keyword, str) or not 1 <= len(keyword.strip()) <= 60:
        raise ValueError("搜索词须为 1 到 60 字")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 15:
        raise ValueError("单次只读取一页，最多 15 条")
    keyword = keyword.strip()
    previous = sorted([r for r in store.rows("market_search", account) if r["keyword"] == keyword],
                      key=lambda r: r["captured_at"], reverse=True)
    if previous and (datetime.fromisoformat(now()) - datetime.fromisoformat(previous[0]["captured_at"])).total_seconds() < 60:
        return {**previous[0], "reused": True, "message": "一分钟内同词复用刚才的结果，没有再次请求平台。"}
    record = {"id": uuid.uuid4().hex, "account": account, "keyword": keyword, "captured_at": now(),
              "source": "goofish_public_search", "scope": "first_page_max_15", "items": [],
              "status": "unavailable", "sales": "unavailable"}
    try:
        async with MtopClient(store, account) as client:
            response = await client._post_mtop(api_name=SEARCH_API, payload={
                "keyword": keyword, "pageNumber": 1, "rowsPerPage": limit, "fromFilter": False,
                "sortValue": "", "sortField": "", "customDistance": "", "gps": "",
                "propValueStr": {}, "customGps": "", "searchReqFromPage": "pcSearch",
                "extraFilterValue": "{}", "userPositionJson": "{}",
            })
        rows = normalize_search(response, keyword, limit)
        prices = [r["asking_price_cents"] for r in rows if r["asking_price_cents"] is not None]
        record.update({"status": "observed", "items": rows,
                       "price_summary": {"minimum_cents": min(prices) if prices else None,
                                         "median_cents": statistics.median(prices) if prices else None,
                                         "maximum_cents": max(prices) if prices else None},
                       "interpretation": "挂牌样本，仅能说明有人供应；想要不等于销量，低价可能为基础档或引流价。"})
        old_prices = {r["item_id"]: r["asking_price_cents"] for r in (previous[0]["items"] if previous and previous[0]["status"] == "observed" else [])}
        record["changes"] = [{"item_id": r["item_id"], "previous_cents": old_prices[r["item_id"]],
                              "current_cents": r["asking_price_cents"]} for r in rows
                             if r["item_id"] in old_prices and old_prices[r["item_id"]] is not None
                             and r["asking_price_cents"] is not None and r["asking_price_cents"] != old_prices[r["item_id"]]]
    except (MarketError, ValueError) as exc:
        record.update({"error_code": getattr(exc, "code", "ACCOUNT_UNAVAILABLE"), "message": str(exc)})
    store.put("market_search", record["id"], record, account=account)
    return record


def bundle_download(store: Store, account: str, slug: str, kind: str) -> Path:
    if kind not in {"delivery", "listing"}:
        raise ValueError("未知素材包类型")
    record = build_bundles(store, account, slug)
    path = (PROJECT / record[kind + "_zip"]).resolve()
    if not path.is_relative_to((DATA / "commerce" / "bundles").resolve()) or not path.is_file():
        raise ValueError("素材包不存在")
    return path
