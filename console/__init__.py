"""Project-owned Xianyu console: local operations and marketplace adapters."""
from pathlib import Path
import sys

# Optional project-local wheels keep additional runtime dependencies inside this project.
_local_libraries = Path(__file__).resolve().parents[1] / ".runtime-libs"
if _local_libraries.is_dir() and str(_local_libraries) not in sys.path:
    sys.path.insert(0, str(_local_libraries))
