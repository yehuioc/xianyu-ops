"""All mutable console state stays in this independent project."""
from __future__ import annotations

import json
import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("XIANYU_CONSOLE_DATA", str(PROJECT / "data" / "console"))).resolve()
if not DATA.is_relative_to(PROJECT):
    raise RuntimeError("Console data must remain inside the project")
OPS = PROJECT / "data" / "ops"
WEB = PROJECT / "console" / "web"
CONFIG_FILE = DATA / "local-settings.json"
LOCAL_SETTINGS = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig")) if CONFIG_FILE.is_file() else {}
if not isinstance(LOCAL_SETTINGS, dict):
    raise ValueError("本机配置必须是 JSON 对象")
DEFAULT_ACCOUNT = str(LOCAL_SETTINGS.get("account") or "local-account")
DEFAULT_ITEM = str(LOCAL_SETTINGS.get("focus_item") or "")
DEFAULT_CDP = str(LOCAL_SETTINGS.get("cdp_url") or "http://127.0.0.1:9223")
BROWSER_PROFILE = (PROJECT / str(LOCAL_SETTINGS.get("browser_profile") or "data/browser-profile")).resolve()
if not BROWSER_PROFILE.is_relative_to(PROJECT):
    raise ValueError("浏览器资料必须留在本项目中")
MANAGED_ITEMS = frozenset(str(item) for item in LOCAL_SETTINGS.get("managed_items", []))
ITEM_PROFILES = LOCAL_SETTINGS.get("item_profiles", {})
if not isinstance(ITEM_PROFILES, dict):
    raise ValueError("商品适配配置必须是 JSON 对象")


def prepare_directories() -> None:
    for path in (DATA, DATA / "bundles", DATA / "logs", DATA / "evidence"):
        path.mkdir(parents=True, exist_ok=True)
