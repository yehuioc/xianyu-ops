"""Synthetic, disposable product fixtures; no commercial assets or live writes."""
import json
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from console import commerce


def install_catalog(case, temporary_root):
    products = Path(temporary_root) / "synthetic-products"
    products.mkdir()
    rows = []
    for slug, sale_type in (("career-kit", "digital"), ("excel-cleaning", "service")):
        root = products / slug
        for section in ("delivery", "listing", "sample"):
            (root / section).mkdir(parents=True)
        (root / "delivery" / "示例.txt").write_text("合成测试交付，不是实际商品。", encoding="utf-8")
        (root / "sample" / "样例.txt").write_text("合成测试样例。", encoding="utf-8")
        (root / "listing" / "listing.json").write_text(json.dumps({"title": "合成测试工具包", "description": "仅供本机自动化测试，不代表在售商品。", "proposed_price_cents": 1990}, ensure_ascii=False), encoding="utf-8")
        Image.new("RGB", (64, 64), "white").save(root / "listing" / "主图.png")
        rows.append({"slug": slug, "name": "合成测试商品", "sale_type": sale_type, "required_files": ["示例.txt"], "limitations": []})
    catalog = products / "catalog.json"
    catalog.write_text(json.dumps({"products": rows, "methods": [], "deferred": []}, ensure_ascii=False), encoding="utf-8")
    for name, value in (("CATALOG", catalog), ("PRODUCTS", products)):
        handle = patch.object(commerce, name, value)
        handle.start()
        case.addCleanup(handle.stop)
