"""All mutable console state stays in this independent project."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("XIANYU_CONSOLE_DATA", str(PROJECT / "data" / "console"))).resolve()
if not DATA.is_relative_to(PROJECT):
    raise RuntimeError("Console data must remain inside the project")
OPS = PROJECT / "data" / "ops"
WEB = PROJECT / "console" / "web"
DEFAULT_ACCOUNT = "demo-account"
DEFAULT_ITEM = "2534367850985"
DEFAULT_CDP = "http://127.0.0.1:9223"


def prepare_directories() -> None:
    for path in (DATA, DATA / "bundles", DATA / "logs", DATA / "evidence"):
        path.mkdir(parents=True, exist_ok=True)
