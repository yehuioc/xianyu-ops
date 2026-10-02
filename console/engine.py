"""Reuse the existing tested parsing/normalization helpers, not its old server."""
import sys
from functools import lru_cache

from .paths import PROJECT


@lru_cache(maxsize=1)
def ops():
    script_dir = str(PROJECT / "scripts")
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    import xianyu_ops_core
    return xianyu_ops_core
