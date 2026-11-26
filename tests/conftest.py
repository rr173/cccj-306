import os
import tempfile
from pathlib import Path

# Keep importing app.main's module-level ASGI app from creating microcred.db
# in the project tree during tests. Individual tests still create their own
# temporary databases and call create_app(path).
_TEST_TMP = tempfile.TemporaryDirectory(prefix="microcred-pytest-")
os.environ.setdefault(
    "MICROCERT_DB", str(Path(_TEST_TMP.name) / "global-app.db")
)
