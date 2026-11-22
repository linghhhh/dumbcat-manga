"""呆猫漫画的共享配置：运行时目录、品牌、字体与站点常量。

界面无关的叶子模块：只依赖标准库与 dumbcat.runtime_cleanup，不导入任何界面
库（PySide6），也不依赖漫源实现，因此界面层与后端都可以安全引用。
站点地址与请求头以本模块为单一来源，漫源 provider 只做引用。
"""
from __future__ import annotations

import os
from pathlib import Path

from dumbcat.runtime_cleanup import configure_process_temp

# 导入即把进程临时目录指向运行时目录。必须在任何可能缓存临时路径的模块
# （浏览器栈等）之前生效，因此这里是模块级调用而不是赋值给一个常量。
configure_process_temp()

APP_NAME = "呆猫漫画"
APP_ID = "DumbCatManga"
LEGACY_APP_ID = "NuviatoonDownloader"
UI_FONT_CANDIDATES = (
    "MiSans",
    "Microsoft YaHei UI",
    "Noto Sans CJK SC",
    "Segoe UI",
)
MANGA18FX_SITE_BASE = os.getenv(
    "MANGA18FX_SITE_BASE",
    "https://manga18fx.com",
).rstrip("/")
_APPDATA_ROOT = Path(os.getenv("DUMBCAT_APPDATA") or os.getenv("APPDATA") or str(Path.home()))
APP_CONFIG_DIR = _APPDATA_ROOT / APP_ID
# 旧版本把配置放在 NuviatoonDownloader 下；只探测不迁移，保证已有设置与
# 浏览器登录态在更名后仍然可用。
LEGACY_CONFIG_DIR = _APPDATA_ROOT / LEGACY_APP_ID
CONFIG_PATH = APP_CONFIG_DIR / "config.json"
QIMANGA_SITE_BASE = "https://qimanga.com"
QIMANGA_API_BASE = "https://api.qimanga.com/api/v1"
QIMANGA_API_V2_BASE = "https://api.qimanga.com/api/v2"
COMIX_SITE_BASE = "https://comix.to"
WNACG_SITE_BASE = os.getenv("WNACG_SITE_BASE", "https://www.wnacg.com").rstrip("/")
COMIC18_SITE_BASE = "https://18comic.vip"
COPYMANGA_SITE_BASE = os.getenv(
    "COPYMANGA_SITE_BASE",
    "https://www.mangacopy.com",
).rstrip("/")
COPYMANGA_API_BASE = os.getenv(
    "COPYMANGA_API_BASE",
    "https://api.mangacopy.com/api/v3",
).rstrip("/")
BAOZIMH_SITE_BASE = os.getenv(
    "BAOZIMH_SITE_BASE",
    "https://baozimh.org",
).rstrip("/")
JM_API_BASE = os.getenv("JM_API_BASE", "https://www.cdnhjk.net").rstrip("/")
JM_IMAGE_BASE = os.getenv(
    "JM_IMAGE_BASE",
    "https://cdn-msp3.jmdanjonproxy.vip",
).rstrip("/")
JM_VERSION = "2.0.20"
JM_AES_SEEDS = ("185Hcomic3PAPP7R", "18comicAPPContent")
#: 下载目录；安卓壳会通过 DUMBCAT_OUTPUT 指到应用私有目录
DEFAULT_OUTPUT = Path(os.getenv("DUMBCAT_OUTPUT") or (Path.home() / "Downloads" / "呆猫漫画"))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)
#: 通用 JSON 请求头；各漫源自己再把 Referer / Origin 补成对应站点
API_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
QIMANGA_API_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{QIMANGA_SITE_BASE}/",
    "Origin": QIMANGA_SITE_BASE,
}
MANGA18FX_HTML_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{MANGA18FX_SITE_BASE}/",
}
COMIX_IMAGE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
IMAGE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"}
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
