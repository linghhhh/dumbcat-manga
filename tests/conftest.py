"""测试共用环境：把数据目录指到临时路径，再把后端加进 sys.path。

必须在导入 ``dumbcat`` 之前设置环境变量——``config`` 在导入时就算好了路径。
pytest 会先导入 conftest，所以这里直接做，不用 fixture。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_DIR = ROOT / "app" / "src" / "main" / "python"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

_TMP = Path(tempfile.mkdtemp(prefix="dumbcat-tests-"))
os.environ["DUMBCAT_APPDATA"] = str(_TMP / "appdata")
os.environ["DUMBCAT_DB"] = str(_TMP / "dumbcat.db")
os.environ["DUMBCAT_OUTPUT"] = str(_TMP / "out")
os.environ["DUMBCAT_ALLOW_TEST_HOST"] = "1"
os.environ.pop("DUMBCAT_ANDROID", None)

ROOT_DIR = ROOT
TMP_DIR = _TMP

__all__ = ["ROOT_DIR", "TMP_DIR"]
