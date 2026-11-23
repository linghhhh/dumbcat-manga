"""前端语法自检：把 ES module 复制成 ``.mjs`` 再交给 ``node --check``。

浏览器里能跑不代表语法没错（少个括号要等点进那个页面才发现），CI 和发版前
都跑一遍。只做语法检查，不执行代码。

用法::

    python tools/check_js.py
    python tools/check_js.py --node /path/to/node
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_JS = ROOT / "app" / "src" / "main" / "assets" / "appdata" / "web" / "js"
PRELUDE = ROOT / "app" / "src" / "main" / "assets" / "appdata" / "jsruntime" / "prelude.js"


def find_node(explicit: str = "") -> str:
    for candidate in (explicit, os.environ.get("NODE", ""), shutil.which("node") or ""):
        if candidate and Path(candidate).exists():
            return candidate
    for candidate in (
        Path("C:/Program Files/nodejs/node.exe"),
        Path("/usr/bin/node"),
        Path("/usr/local/bin/node"),
    ):
        if candidate.exists():
            return str(candidate)
    return ""


def check(node: str, files: list[Path]) -> list[str]:
    failures: list[str] = []
    base = ROOT / "build" / "js-check"
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="js-", dir=str(base)) as tmp:
        tmp_dir = Path(tmp)
        for index, source in enumerate(files):
            target = tmp_dir / f"{index:02d}_{source.stem}.mjs"
            target.write_bytes(source.read_bytes())
            out_path = tmp_dir / f"{index:02d}.out"
            err_path = tmp_dir / f"{index:02d}.err"
            # 输出走文件：某些沙箱环境不允许 subprocess / os.system 建管道
            with out_path.open("w", encoding="utf-8") as out, err_path.open("w", encoding="utf-8") as err:
                code = subprocess.run(
                    [node, "--check", str(target)],
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                ).returncode
            if code != 0:
                detail = err_path.read_text(encoding="utf-8", errors="replace").strip()
                failures.append(str(source.relative_to(ROOT)) + chr(10) + detail)
            else:
                print(f"ok   {source.relative_to(ROOT)}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="检查前端 ES module 语法")
    parser.add_argument("--node", default="", help="node 可执行文件路径")
    args = parser.parse_args()

    node = find_node(args.node)
    if not node:
        print("找不到 node：装一个 Node.js 20+，或用 --node 指定路径", file=sys.stderr)
        return 2

    files = sorted(WEB_JS.glob("*.js"))
    if PRELUDE.exists():
        files.append(PRELUDE)
    if not files:
        print(f"没有找到前端文件：{WEB_JS}", file=sys.stderr)
        return 2

    failures = check(node, files)
    if failures:
        print("\n语法错误：", file=sys.stderr)
        for item in failures:
            print(item, file=sys.stderr)
        return 1
    print(f"\nJS_OK {len(files)} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
