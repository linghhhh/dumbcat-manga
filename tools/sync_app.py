"""把桌面版呆猫漫画的代码/网页资源同步到安卓工程，并生成启动图标。

用法：python tools/sync_app.py
"""
from __future__ import annotations

import math
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT.parent / "呆猫漫画v3"
PY_DIR = ROOT / "app" / "src" / "main" / "python"
ASSET_DIR = ROOT / "app" / "src" / "main" / "assets" / "appdata"

SKIP_DIRS = {"__pycache__", "ui", "tools", "手册"}
SKIP_SUFFIX = {".pyc", ".pyo", ".log", ".webp.bak"}


def _rmtree(path: Path) -> None:
    """删目录；Windows 上偶发被索引/杀软占住，重试几次并把只读位摘掉。"""
    import stat
    import time

    def onerror(func, target, exc_info):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    for attempt in range(6):
        if not path.exists():
            return
        try:
            shutil.rmtree(path, onerror=onerror)
        except OSError:
            time.sleep(0.4)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def copy_tree(src: Path, dst: Path) -> None:
    _rmtree(dst)
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def sync_python() -> None:
    target = PY_DIR / "dumbcat"
    _rmtree(target)
    shutil.copytree(
        SRC / "dumbcat",
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "ui", "ui_*"),
    )
    patch_python()
    print(f"python: {target}")


def sync_assets() -> None:
    _rmtree(ASSET_DIR)
    ASSET_DIR.mkdir(parents=True)
    copy_tree(SRC / "web", ASSET_DIR / "web")
    copy_tree(SRC / "plugins_src", ASSET_DIR / "plugins_src")
    copy_tree(SRC / "dumbcat" / "sources" / "json", ASSET_DIR / "json")
    patch_assets()
    print(f"assets: {ASSET_DIR}")


def _replace(path: Path, old: str, new: str) -> None:
    """按锚点改一行；已经改过就跳过，锚点丢了直接报错（避免静默失败）。"""
    text = path.read_text(encoding="utf-8")
    if new in text:
        return
    if old not in text:
        raise SystemExit(f"补丁锚点没找到：{path.as_posix()} -> {old[:50]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def patch_python() -> None:
    """把桌面版里写死 Windows 路径/状态的地方改成读环境变量（安卓壳会设）。"""
    app_config = PY_DIR / "dumbcat" / "app_config.py"
    _replace(
        app_config,
        '_APPDATA_ROOT = Path(os.getenv("APPDATA", str(Path.home())))',
        '_APPDATA_ROOT = Path(os.getenv("DUMBCAT_APPDATA") or os.getenv("APPDATA") or str(Path.home()))',
    )
    _replace(
        app_config,
        'DEFAULT_OUTPUT = Path(r"E:\manga\yuanban")',
        'DEFAULT_OUTPUT = Path(os.getenv("DUMBCAT_OUTPUT") or r"E:\manga\yuanban")',
    )

    server_config = PY_DIR / "dumbcat" / "server" / "config.py"
    _replace(
        server_config,
        "PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent",
        'PROJECT_ROOT = Path(os.getenv("DUMBCAT_ROOT") or Path(__file__).resolve().parent.parent.parent)',
    )
    _replace(
        server_config,
        "    include_browser_sources: bool = True",
        "    # 安卓版默认关闭：手机上没有 Chromium\n    include_browser_sources: bool = False",
    )

    manifest = PY_DIR / "dumbcat" / "sources" / "manifest.py"
    text = manifest.read_text(encoding="utf-8")
    if "\nimport os\n" not in text:
        text = text.replace(
            "from __future__ import annotations\n",
            "from __future__ import annotations\n\nimport os\n",
            1,
        )
        manifest.write_text(text, encoding="utf-8")
    _replace(
        manifest,
        'JSON_SOURCE_DIR = Path(__file__).resolve().parent / "json"',
        'JSON_SOURCE_DIR = Path(os.getenv("DUMBCAT_JSON_DIR") or (Path(__file__).resolve().parent / "json"))',
    )

    app_py = PY_DIR / "dumbcat" / "server" / "app.py"
    _replace(
        app_py,
        '    if not path.exists():\n        raise HTTPException(404, "目录不存在")\n    try:\n        if sys.platform == "win32":',
        '    if not path.exists():\n        raise HTTPException(404, "目录不存在")\n'
        '    if sys.platform != "win32":\n'
        '        # 手机上没有资源管理器可唤起，让界面提示用文件管理器\n'
        '        raise HTTPException(501, "手机版请用文件管理器打开下载目录")\n'
        '    try:\n        if sys.platform == "win32":',
    )

    imagecache = PY_DIR / "dumbcat" / "server" / "imagecache.py"
    text = imagecache.read_text(encoding="utf-8")
    # v3.2.1 起桌面版 imagecache 自带 android_webp / webp_writable 兜底（没有 WebP
    # 编码器时退回 JPEG），下面两组老补丁的锚点已经不存在：直接跳过。旧代码照旧打补丁。
    if "webp_writable" in text:
        return
    if "log = logging.getLogger" not in text:
        text = text.replace(
            "import io\nimport os\n",
            "import io\nimport logging\nimport os\n",
            1,
        ).replace(
            "from dumbcat.server.config import IMAGE_CACHE_DIR, load_settings\n",
            "from dumbcat.server.config import IMAGE_CACHE_DIR, load_settings\n\n"
            'log = logging.getLogger("dumbcat.imagecache")\n',
            1,
        )
        imagecache.write_text(text, encoding="utf-8")
    _replace(
        imagecache,
        '            resized = target.resize((width, height), Image.LANCZOS)\n'
        '            out = io.BytesIO()\n'
        '            resized.save(out, format="WEBP", quality=82, method=4)\n'
        '            return out.getvalue()\n'
        '    except Exception:  # noqa: BLE001 - 坏图 / 格式不支持时退回原图\n'
        '        return None',
        '            resized = target.resize((width, height), Image.LANCZOS)\n'
        '            out = io.BytesIO()\n'
        '            try:\n'
        '                resized.save(out, format="WEBP", quality=82, method=4)\n'
        '            except Exception:  # noqa: BLE001 - 缺 WebP 编码器就退回 JPEG\n'
        '                out = io.BytesIO()\n'
        '                resized.save(out, format="JPEG", quality=85)\n'
        '            return out.getvalue()\n'
        '    except Exception as exc:  # noqa: BLE001 - 坏图 / 格式不支持时退回原图\n'
        '        log.warning("缩略图生成失败：%s", exc)\n'
        '        return None',
    )
    _replace(
        imagecache,
        '    try:\n        from PIL import Image\n    except Exception:  # noqa: BLE001 - 没装 Pillow 就退回原图，不影响功能\n        return None',
        '    try:\n        from PIL import Image\n    except Exception as exc:  # noqa: BLE001 - 没装 Pillow 就退回原图，不影响功能\n'
        '        log.warning("缩略图不可用（缺 Pillow）：%s", exc)\n'
        '        return None',
    )


def patch_assets() -> None:
    """手机上没有 Chromium，把完全依赖浏览器的图源标成 breeze，默认不装。"""
    import json

    comix = ASSET_DIR / "json" / "comix.json"
    if comix.exists():
        spec = json.loads(comix.read_text(encoding="utf-8"))
        spec["breeze"] = True
        spec["note"] = "需要 Chromium 渲染；手机版默认不启用。"
        comix.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _rounded_mask(size: int, radius: int):
    from PIL import Image, ImageDraw

    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def draw_icon(size: int = 512, rounded: bool = True):
    from PIL import Image, ImageDraw

    scale = size / 512.0
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # 蓝色渐变底
    bg = Image.new("RGB", (size, size))
    top = (47, 107, 255)
    bottom = (94, 146, 255)
    px = bg.load()
    for y in range(size):
        t = y / max(1, size - 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(size):
            px[x, y] = color
    image.paste(bg, (0, 0))

    draw = ImageDraw.Draw(image)
    cx, cy = 256 * scale, 282 * scale
    r = 132 * scale

    # 耳朵
    ear_l = [(120 * scale, 210 * scale), (172 * scale, 96 * scale), (238 * scale, 196 * scale)]
    ear_r = [(392 * scale, 210 * scale), (340 * scale, 96 * scale), (274 * scale, 196 * scale)]
    for ear in (ear_l, ear_r):
        draw.polygon(ear, fill=(255, 246, 230, 255))
        inner = [
            (ear[0][0] + (ear[1][0] - ear[0][0]) * 0.18 + (ear[2][0] - ear[0][0]) * 0.18,
             ear[0][1] + (ear[1][1] - ear[0][1]) * 0.18 + (ear[2][1] - ear[0][1]) * 0.18),
            (ear[0][0] + (ear[1][0] - ear[0][0]) * 0.72 + (ear[2][0] - ear[0][0]) * 0.12,
             ear[0][1] + (ear[1][1] - ear[0][1]) * 0.72 + (ear[2][1] - ear[0][1]) * 0.12),
            (ear[0][0] + (ear[1][0] - ear[0][0]) * 0.12 + (ear[2][0] - ear[0][0]) * 0.72,
             ear[0][1] + (ear[1][1] - ear[0][1]) * 0.12 + (ear[2][1] - ear[0][1]) * 0.72),
        ]
        draw.polygon(inner, fill=(255, 205, 214, 255))

    # 脸
    draw.ellipse((cx - r, cy - r * 0.94, cx + r, cy + r * 0.94), fill=(255, 246, 230, 255))

    # 眼睛
    eye_r = 17 * scale
    for ex in (cx - 52 * scale, cx + 52 * scale):
        ey = cy - 8 * scale
        draw.ellipse((ex - eye_r, ey - eye_r * 1.35, ex + eye_r, ey + eye_r * 1.35),
                     fill=(31, 43, 69, 255))
        draw.ellipse((ex - eye_r * 0.34, ey - eye_r * 0.95,
                      ex + eye_r * 0.34, ey - eye_r * 0.1), fill=(255, 255, 255, 255))

    # 鼻子
    draw.polygon([(cx, cy + 26 * scale),
                  (cx - 13 * scale, cy + 12 * scale),
                  (cx + 13 * scale, cy + 12 * scale)], fill=(255, 148, 160, 255))

    # 嘴
    draw.arc((cx - 26 * scale, cy + 20 * scale, cx, cy + 52 * scale), 20, 160,
             fill=(31, 43, 69, 220), width=max(2, int(4 * scale)))
    draw.arc((cx, cy + 20 * scale, cx + 26 * scale, cy + 52 * scale), 20, 160,
             fill=(31, 43, 69, 220), width=max(2, int(4 * scale)))

    # 书（右下角）
    book = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bd = ImageDraw.Draw(book)
    bd.rounded_rectangle((286 * scale, 366 * scale, 430 * scale, 462 * scale),
                         radius=int(12 * scale), fill=(255, 255, 255, 240))
    bd.rounded_rectangle((286 * scale, 366 * scale, 358 * scale, 462 * scale),
                         radius=int(12 * scale), fill=(255, 214, 102, 255))
    bd.line((358 * scale, 366 * scale, 358 * scale, 462 * scale),
            fill=(230, 190, 80, 255), width=max(2, int(4 * scale)))
    image.alpha_composite(book)

    if rounded:
        mask = _rounded_mask(size, int(96 * scale))
        image.putalpha(mask)
    return image


def make_icons() -> None:
    """图标统一交给 tools/make_icons.py（用用户照片生成），这里只负责触发。"""
    import subprocess
    import sys

    script = ROOT / "tools" / "make_icons.py"
    if not script.exists():
        print("跳过图标：缺少 tools/make_icons.py")
        return
    subprocess.run([sys.executable, str(script)], check=True)


if __name__ == "__main__":
    # 可传一个参数指定桌面版目录；不传就用默认位置。
    if len(sys.argv) > 1:
        SRC = Path(sys.argv[1]).resolve()
    if not SRC.is_dir():
        raise SystemExit(
            f"找不到桌面版源码目录：{SRC}\n"
            "本仓库 app/src/main/python 已是可独立构建的源码副本，不需要同步也能打包；\n"
            "只有要从桌面版重新同步时才跑这个脚本，用法：python tools/sync_app.py <桌面版目录>"
        )
    sync_python()
    sync_assets()
    make_icons()
