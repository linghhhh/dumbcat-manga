"""呆猫漫画安卓入口。

Java 壳在后台线程调用 :func:`start`：

1. 把 APK 里 `assets/appdata/` 下的网页资源解压到应用私有目录（只做一次）；
2. 设置目录环境变量，导入桌面版同一套 FastAPI 服务；
3. 在独立线程里跑 uvicorn，等 /api/health 通了以后把 ``端口|令牌`` 返回给 Java。

这样网页界面与桌面版完全一致，后端逻辑也一份代码。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

ASSET_PREFIX = "assets/appdata/"
MARKER = ".appdata-version"
#: 资源目录结构变化时改这个版本号，升级后会重新解压
#: 10 → 11：新增书架后端（dumbcat/shelf.py + /api/shelf*）与新网页资源，
#: 老版本升级时必须重解压，否则会继续跑旧 appdata（没有书架接口）。
#: 11 → 12：内置图源全部改为默认停用（json 里的 enabled: false），
#: 已安装的老版本升级后也必须重解压，否则还会加载旧的启用状态。
#: 12 → 13：图源管理页、下载设置页。
#: 13 → 14：阅读器手势模块（viewer.js）、离线书架/下载弹窗改动，
#: 老版本升级后必须重解压，否则前端还是旧版、后端新接口对不上。
#: 14 → 15：书架三标签、下载并入书架页、自选保存文件夹、阅读器去掉单页模式。
APPDATA_VERSION = "16"


def log(message: str) -> None:
    try:
        print(f"[dumbcat] {message}", flush=True)
    except Exception:
        pass


def _preserve_legacy_downloads(home: Path) -> None:
    """升级重解压 appdata 之前，把旧版的下载记录备份到 config 目录。

    书架后端首次加载时会把这份记录迁进"旧版下载"书架；这里只做一次复制，
    目标文件已存在就不覆盖（避免把已经迁移过的老数据又盖回去）。
    只能依赖启动参数，不能 import dumbcat：此刻服务代码还没加载。
    """
    source = home / ".DumbCatMangaRuntime" / "web" / "downloads.json"
    target = home.parent / "config" / "DumbCatManga" / "legacy_downloads.json"
    try:
        if not source.is_file() or target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        log(f"已备份旧版下载记录到 {target}")
    except Exception as error:  # noqa: BLE001 - 迁移失败不能拦住启动
        log(f"备份旧版下载记录失败：{error}")


def _extract_appdata(apk_path: str, home: Path) -> None:
    marker = home / MARKER
    try:
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() == APPDATA_VERSION:
            return
    except OSError:
        pass

    log(f"解压网页资源到 {home}")
    tmp = home.parent / (home.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(apk_path) as archive:
            for name in archive.namelist():
                if not name.startswith(ASSET_PREFIX) or name.endswith("/"):
                    continue
                relative = name[len(ASSET_PREFIX):]
                target = tmp / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, open(target, "wb") as sink:
                    shutil.copyfileobj(source, sink)
        _preserve_legacy_downloads(home)
        if home.exists():
            shutil.rmtree(home, ignore_errors=True)
        shutil.move(str(tmp), str(home))
        (home / MARKER).write_text(APPDATA_VERSION, encoding="utf-8")
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def _preload_native_libs(files: Path) -> None:
    """Pillow 这类 wheel 拆出来的 .so 在 Android 上按 DT_NEEDED 名字找不到，
    先按绝对路径 dlopen 一遍，把它们塞进进程的 link namespace 里。"""
    import ctypes

    try:
        import PIL  # noqa: F401  先 import 一次，Chaquopy 才会把 Pillow 的原生库解压出来
    except Exception:
        pass
    roots = sorted((files / "chaquopy").glob("**/lib/*.so"))
    for path in roots:
        try:
            ctypes.CDLL(str(path))
        except OSError as exc:
            log(f"预载 {path.name} 失败：{exc}")
    if roots:
        log(f"预载 {len(roots)} 个原生库")
    try:
        import PIL.Image  # noqa: F401
        log("Pillow 可用")
    except Exception as exc:  # noqa: BLE001
        log(f"Pillow 不可用：{exc}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(port: int, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/api/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def _writable_download_dir(default: Path) -> Path:
    """按「公共目录（已授权）→ 应用私有目录」的顺序找第一个能写的位置。"""
    from dumbcat.android_storage import default_public_dir, permission_info
    from dumbcat.download_paths import probe_writable

    candidates: list[Path] = []
    if permission_info().get("granted"):
        public = default_public_dir()
        if public is not None:
            candidates.append(public)
    candidates.append(default)
    for path in candidates:
        ok, reason = probe_writable(path)
        if ok:
            return path
        log(f"下载目录不可写：{path}（{reason}）")
    return default


def _prepare_settings(output_dir: Path, home: Path) -> None:
    """首次启动写入手机默认值；之后每次启动校验下载目录，失效就换个能写的地方。"""
    try:
        from dumbcat.download_paths import app_private_dir, probe_writable
        from dumbcat.android_storage import default_public_dir, permission_info
        from dumbcat.server.config import WEB_CONFIG_PATH, load_settings, save_settings

        settings = load_settings()
        changed = False
        if not WEB_CONFIG_PATH.exists():
            settings.output_dir = str(output_dir)
            # 手机上没有 Chromium，浏览器型图源先关掉，界面里还能手动打开
            settings.include_browser_sources = False
            settings.workers = min(settings.workers, 4)
            settings.image_workers = min(settings.image_workers, 4)
            settings.image_cache_mb = 256
            # 首屏别落在需要梯子/浏览器的源上，qimanga 直连 JSON 接口最稳
            settings.last_source = "qimanga"
            # 手机竖屏窄，卡片窄一点一屏能放两三列
            settings.grid_min_width = 110
            changed = True

        target: Path | None = None
        try:
            if str(settings.output_dir or "").strip():
                target = Path(str(settings.output_dir)).expanduser()
        except (OSError, ValueError):
            target = None
        ok, reason = probe_writable(target) if target is not None else (False, "未设置")
        if not ok:
            replacement = _writable_download_dir(output_dir)
            log(f"下载目录 {target} 不可用（{reason}），改用 {replacement}")
            settings.output_dir = str(replacement)
            changed = True
            target = replacement
        # 已经给了「所有文件访问」权限、但设置里还停在应用私有目录：换成公共目录，
        # 这样用户在文件管理器里能直接看到下载的漫画。
        private = app_private_dir()
        public = default_public_dir() if permission_info().get("granted") else None
        if target is not None and private is not None and public is not None and target == private:
            public_ok, public_reason = probe_writable(public)
            if public_ok:
                log(f"下载目录从私有目录切到公共目录：{public}")
                settings.output_dir = str(public)
                changed = True
            else:
                log(f"公共目录不可写，继续用私有目录：{public_reason}")

        if changed:
            save_settings(settings)
            log(f"已更新手机配置：下载目录={settings.output_dir}")
    except Exception as exc:  # noqa: BLE001
        log(f"写入首启配置失败：{exc}")


def _register_js_prelude(home: Path) -> None:
    """把 prelude.js 的绝对路径交给 Java 侧的 QuickJS 运行时。"""
    prelude = home / "jsruntime" / "prelude.js"
    if not prelude.is_file():
        log(f"没找到 {prelude}，JS 图源不可用")
        return
    try:
        from dumbcat.plugins import js_bridge

        js_bridge.set_prelude_path(str(prelude))
    except Exception as exc:  # noqa: BLE001 - 桌面版没有 java 模块
        log(f"登记 prelude 失败：{exc}")


def start(files_dir: str, cache_dir: str, apk_path: str,
          external_dir: str = "") -> str:
    """给 Java 壳调用的入口，返回 ``"端口|令牌"``。"""
    files = Path(files_dir)
    cache = Path(cache_dir)
    home = files / "appdata"
    config_home = files / "config"
    runtime = cache / "runtime"

    _extract_appdata(apk_path, home)
    _preload_native_libs(files)

    output = Path(external_dir) / "呆猫漫画" if external_dir else files / "downloads"
    for path in (home / "web", config_home, runtime, output):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # 以前这里是静默 pass：目录没建出来，用户点下载只看到一句英文报错
            log(f"创建目录失败：{path}（{exc}）")
    # 用户插件目录（解压包里不含空目录）
    (home / "用户插件").mkdir(parents=True, exist_ok=True)

    os.environ["DUMBCAT_ROOT"] = str(home)
    os.environ["DUMBCAT_APPDATA"] = str(config_home)
    os.environ["DUMBCAT_JSON_DIR"] = str(home / "json")
    os.environ["DUMBCAT_OUTPUT"] = str(output)
    #: 应用私有默认目录：用户没选目录时的兜底，也用来判断设置里是不是还没换过目录
    os.environ["DUMBCAT_APP_OUTPUT"] = str(output)
    os.environ["MANGA_RUNTIME_DIR"] = str(runtime)
    # 手机网络慢，首屏别在后台抢流量
    os.environ.setdefault("DUMBCAT_NO_WARMUP", "1")
    # 让设置页知道自己在手机上：下载根目录由用户自己选
    os.environ["DUMBCAT_ANDROID"] = "1"
    # JS 图源运行时：prelude.js 随 appdata 解压到 home/web 旁边
    _register_js_prelude(home)

    log(f"root={home} output={output}")

    _prepare_settings(output, home)

    import uvicorn

    from dumbcat import server  # noqa: F401  确保包路径可用
    from dumbcat.server.app import TOKEN, app

    port = _free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        loop="asyncio",
        log_config=None,
    )
    server_instance = uvicorn.Server(config)

    def serve() -> None:
        try:
            server_instance.run()
        except Exception as exc:  # noqa: BLE001
            log(f"服务退出：{exc}")

    thread = threading.Thread(target=serve, name="dumbcat-uvicorn", daemon=True)
    thread.start()

    if not _wait_ready(port):
        raise RuntimeError("本地服务启动超时")

    log(f"服务就绪：127.0.0.1:{port}")
    return f"{port}|{TOKEN}"
