"""本地 HTTP 服务：给 Web UI 与 Qt 壳用的一套 JSON API。

只监听 127.0.0.1，启动时生成一次性 token；页面拿 token 调接口、取图片，别的
程序即使猜到端口也进不来。所有图源调用都在线程池里跑，接口本身是 async 的，
所以搜索 / 浏览 / 下载可以同时进行而不互相卡住。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
import urllib.parse
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dumbcat.server.config import (
    PROJECT_ROOT,
    WEB_RUNTIME,
    ensure_dirs,
    load_settings,
    output_root,
    update_settings,
)
from dumbcat.server.events import bus
from dumbcat.server.imagecache import image_cache
from dumbcat.server.normalize import (
    normalize_chapter,
    normalize_page_list,
    normalize_series,
    series_sort_key,
)
from dumbcat.server.provider_shim import series_folder_name
from dumbcat.sources.manifest import JSON_SOURCE_DIR
from dumbcat.server.registry import (
    SourceError,
    health_snapshot,
    note_health,
    plugin_template,
    registry,
)
from dumbcat.server.store import (
    PageList,
    SeriesRecord,
    series_of_chapter,
    store,
)
from dumbcat.server.tasks import tasks

log = logging.getLogger("dumbcat.server")

WEB_DIR = PROJECT_ROOT / "web"
VERSION = "0.2.0"
TOKEN = secrets.token_urlsafe(24)


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class DownloadRequest(BaseModel):
    seriesUid: str
    chapterUids: list[str]
    outputDir: str = ""
    workers: int | None = None


class SettingsPatch(BaseModel):
    output_dir: str | None = None
    output_subdir: str | None = None
    max_images_per_chapter: int | None = None
    workers: int | None = None
    image_workers: int | None = None
    theme: str | None = None
    reduce_motion: bool | None = None
    grid_min_width: int | None = None
    auto_load_more: bool | None = None
    chapter_newest_first: bool | None = None
    include_browser_sources: bool | None = None
    last_source: str | None = None
    open_after_download: bool | None = None
    image_cache_mb: int | None = None
    search_concurrency: int | None = None


class PluginSave(BaseModel):
    filename: str
    content: str


class SourceToggle(BaseModel):
    enabled: bool = True


class PathRequest(BaseModel):
    path: str


class LoginPassword(BaseModel):
    account: str
    password: str


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------
app = FastAPI(title="呆猫漫画", version=VERSION, docs_url=None, redoc_url=None)

_started_at = time.time()


def _warmup_browse() -> None:
    """启动预热：把上次用的那个图源的第一页先抓进缓存。

    界面打开得比网络快，预热让「发现」页第一屏尽量直接命中缓存；
    和界面请求同时到达时由 _browse_single_flight 合并成一次网络请求。
    """
    if os.environ.get("DUMBCAT_NO_WARMUP"):
        return
    try:
        settings = load_settings()
        preferred = getattr(settings, "last_source", "") or ""
        adapters = [a for a in _adapters() if a.info.enabled]
        if not adapters:
            return
        target = next((a for a in adapters if a.key == preferred), adapters[0])
        if _browse_cached(target.key, 1, "") is not None:
            return

        def build() -> dict[str, Any]:
            return _browse_payload(target, 1, "")

        started = time.time()
        payload = _browse_single_flight(target.key, 1, "", build)
        _browse_store(target.key, 1, "", payload)
        log.info("预热 %s 首页完成：%.1fs / %s 部", target.key, time.time() - started, len(payload["items"]))
    except Exception as exc:  # noqa: BLE001 - 预热失败不影响启动
        log.info("预热跳过：%s", exc)


@app.on_event("startup")
async def _startup() -> None:
    ensure_dirs()
    bus.bind_loop(asyncio.get_running_loop())
    await asyncio.to_thread(registry.reload_all)
    tasks.start_worker()
    log.info("呆猫漫画服务已启动，图源 %s 个", len(registry.sources()))
    threading.Thread(target=_warmup_browse, name="dumbcat-warmup", daemon=True).start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    tasks.stop()


# ---------- 安全中间件 ----------
@app.middleware("http")
async def _guard(request: Request, call_next):  # noqa: ANN001
    path = request.url.path
    host = (request.headers.get("host") or "").split(":")[0].lower()
    allowed = {"127.0.0.1", "localhost", "::1", "[::1]"}
    if os.getenv("DUMBCAT_ALLOW_TEST_HOST"):
        allowed.add("testserver")
    if host not in allowed:
        return JSONResponse({"error": "forbidden host"}, status_code=403)
    if path.startswith(("/api/", "/img/")) and path != "/api/health":
        supplied = (
            request.headers.get("x-dumbcat-token")
            or request.query_params.get("token")
            or ""
        )
        if not secrets.compare_digest(supplied, TOKEN):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


# ---------- 静态资源 ----------
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


def _index_html() -> str:
    index = WEB_DIR / "index.html"
    if not index.exists():
        return "<h1>呆猫漫画</h1><p>web/index.html 还没建好。</p>"
    text = index.read_text(encoding="utf-8")
    return (
        text.replace("__DUMBCAT_TOKEN__", TOKEN)
        .replace("__DUMBCAT_VERSION__", VERSION)
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    response = HTMLResponse(_index_html())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/favicon.ico")
async def favicon() -> Response:
    icon = WEB_DIR / "assets" / "favicon.svg"
    if icon.exists():
        return FileResponse(icon, media_type="image/svg+xml")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# 健康 / 设置 / 图源
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "version": VERSION,
        "uptime": round(time.time() - _started_at, 1),
        "sources": registry.stats(),
        "store": store.stats(),
        "tasks": tasks.stats(),
        "cacheBytes": image_cache.size_bytes(),
    }


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    settings = load_settings()
    data = settings.__dict__.copy()
    data["projectRoot"] = str(PROJECT_ROOT)
    data["pluginDir"] = str(registry.plugin_dir())
    data["cacheBytes"] = image_cache.size_bytes()
    data["version"] = VERSION
    #: 手机版把下载根目录锁在应用私有目录，界面上只允许改子目录
    data["android"] = bool(os.getenv("DUMBCAT_ANDROID"))
    return data


@app.post("/api/settings")
async def post_settings(patch: SettingsPatch) -> dict[str, Any]:
    before = load_settings()
    body = {k: v for k, v in patch.dict(exclude_none=True).items()}
    settings = update_settings(body)
    if (
        "include_browser_sources" in body
        and bool(body["include_browser_sources"]) != bool(before.include_browser_sources)
    ):
        await asyncio.to_thread(registry.reload_all)
    return {"ok": True, "settings": settings.__dict__}


@app.get("/api/sources")
async def get_sources() -> dict[str, Any]:
    health = health_snapshot()
    sources = []
    for item in registry.sources():
        if isinstance(item, dict):
            merged = dict(item)
            stat = health.get(str(merged.get("key") or ""))
            if stat:
                merged["health"] = stat
            sources.append(merged)
        else:  # pragma: no cover - 防御性
            sources.append(item)
    return {
        "sources": sources,
        "stats": registry.stats(),
        "error": registry.last_error,
    }


@app.post("/api/sources/reload")
async def reload_sources() -> dict[str, Any]:
    clear_browse_cache()
    await asyncio.to_thread(registry.reload_all)
    return {"ok": True, "stats": registry.stats()}


@app.post("/api/sources/{key}/enabled")
async def set_source_enabled(key: str, req: SourceToggle) -> dict[str, Any]:
    """图源管理页的开关：关掉的图源仍留在列表里，可以再打开。"""
    try:
        result = await asyncio.to_thread(registry.set_source_enabled, key, req.enabled)
    except SourceError as exc:
        raise HTTPException(400, str(exc)) from exc
    clear_browse_cache()
    return {"ok": True, **result}


# ---------- 官方账号登录（Cookie） ----------
def _adapter_or_404(key: str) -> Any:
    adapter = registry.get(key)
    if adapter is None:
        raise HTTPException(404, "图源不存在")
    return adapter


async def _login_info(adapter: Any) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(adapter.login_info)
    except Exception as exc:  # noqa: BLE001 - 登录态查询失败不该 500
        raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc


@app.get("/api/logins")
async def list_logins() -> dict[str, Any]:
    """一次性返回所有图源的登录状态（插件页用，避免逐个请求）。"""
    def work() -> dict[str, Any]:
        out: dict[str, Any] = {}
        for item in registry.sources():
            adapter = registry.get(item["key"])
            if adapter is None:
                continue
            try:
                out[item["key"]] = adapter.login_info()
            except Exception:  # noqa: BLE001
                out[item["key"]] = {"key": item["key"], "enabled": False, "loggedIn": False}
        return out

    return {"logins": await asyncio.to_thread(work)}


@app.get("/api/sources/{key}/login")
async def source_login_state(key: str) -> dict[str, Any]:
    return await _login_info(_adapter_or_404(key))


@app.post("/api/sources/{key}/login/open")
async def source_login_open(key: str) -> dict[str, Any]:
    adapter = _adapter_or_404(key)
    try:
        url = await asyncio.to_thread(adapter.open_login)
    except SourceError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 浏览器起不来时给出可读原因
        raise HTTPException(502, f"打开登录页失败：{type(exc).__name__}: {exc}") from exc
    return {"ok": True, "url": url, "hint": await asyncio.to_thread(adapter.login_hint)}


@app.post("/api/sources/{key}/login/capture")
async def source_login_capture(key: str) -> dict[str, Any]:
    adapter = _adapter_or_404(key)
    try:
        count = await asyncio.to_thread(adapter.capture_login)
    except SourceError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"采集登录态失败：{type(exc).__name__}: {exc}") from exc
    clear_browse_cache()
    info = await _login_info(adapter)
    return {"ok": True, "captured": count, **info}


@app.post("/api/sources/{key}/login/password")
async def source_login_password(key: str, req: LoginPassword) -> dict[str, Any]:
    adapter = _adapter_or_404(key)
    try:
        await asyncio.to_thread(adapter.password_login, req.account, req.password)
    except SourceError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"登录失败：{type(exc).__name__}: {exc}") from exc
    clear_browse_cache()
    info = await _login_info(adapter)
    return {"ok": True, **info}


@app.post("/api/sources/{key}/logout")
async def source_logout(key: str) -> dict[str, Any]:
    adapter = _adapter_or_404(key)
    try:
        await asyncio.to_thread(adapter.logout_source)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"退出登录失败：{type(exc).__name__}: {exc}") from exc
    clear_browse_cache()
    return {"ok": True, **(await _login_info(adapter))}


# ---------------------------------------------------------------------------
# 浏览 / 搜索
# ---------------------------------------------------------------------------
def _store_series(adapter: Any, raw: dict[str, Any], base_url: str = "") -> dict[str, Any]:
    public = normalize_series(
        adapter.key,
        adapter.name,
        raw,
        base_url=base_url or adapter.info.base_url,
        nsfw=adapter.info.nsfw,
    )
    if public["coverUrl"]:
        public["coverUrl"] = f"{public['coverUrl']}?token={urllib.parse.quote(TOKEN)}"
    record = SeriesRecord(
        uid=public["uid"],
        provider=adapter.key,
        provider_name=adapter.name,
        raw=raw,
        public=public,
        base_url=base_url or adapter.info.base_url,
        nsfw=adapter.info.nsfw,
    )
    store.put_series(record)
    return public


_CATEGORY_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


CATEGORY_TTL = float(os.environ.get("DUMBCAT_CATEGORY_TTL", "900"))


def _browse_payload(adapter: Any, page: int, category: str) -> dict[str, Any]:
    """抓一页列表并组装成接口/预热共用的结构（分类只读缓存，不挡列表）。"""
    started = time.time()
    items, has_more, total = adapter.browse(page, category)
    note_health(adapter.key, True, time.time() - started)
    public_items = [_store_series(adapter, raw) for raw in items if isinstance(raw, dict)]
    public_items.sort(key=series_sort_key)
    categories = _cached_categories(adapter, wait=False)
    return {
        "source": {
            "key": adapter.key,
            "name": adapter.name,
            "icon": adapter.info.icon,
            "nsfw": adapter.info.nsfw,
        },
        "categories": categories,
        "categoriesPending": not categories,
        "page": page,
        "hasMore": bool(has_more),
        "total": total,
        "items": public_items,
    }


def _cached_categories(adapter: Any, wait: bool = True) -> list[dict[str, Any]]:
    """取分类。``wait=False`` 只读缓存，不为了分类去等一次网络。

    分类（尤其是浏览器型图源的分类）可能要几秒到几十秒，而首屏列表只要几百
    毫秒；把它挡在列表前面会把「打开图源」一起拖慢，所以浏览接口只拿现成的，
    没有就返回空 + categoriesPending，前端再单独补拉一次。
    """
    now = time.time()
    cached = _CATEGORY_CACHE.get(adapter.key)
    if cached and now - cached[0] < CATEGORY_TTL:
        return cached[1]
    if not wait:
        return list(cached[1]) if cached else []
    items = adapter.categories() or []
    _CATEGORY_CACHE[adapter.key] = (now, items)
    return items


# ---------------------------------------------------------------------------
# 浏览缓存：同一图源同一页短时间内重复请求直接复用结果，并且同一页同时只打
# 一次网络（单飞合并）——切图源、来回翻页、启动预热都不会重复等一遍网络。
# ---------------------------------------------------------------------------
BROWSE_TTL = float(os.environ.get("DUMBCAT_BROWSE_TTL", "300"))
_browse_cache: dict[tuple[str, int, str], tuple[float, dict[str, Any]]] = {}
_browse_inflight: dict[tuple[str, int, str], Future] = {}
_browse_lock = threading.Lock()


def _browse_cached(source: str, page: int, category: str) -> dict[str, Any] | None:
    key = (source, page, category)
    now = time.time()
    with _browse_lock:
        hit = _browse_cache.get(key)
        if hit and now - hit[0] < BROWSE_TTL:
            return hit[1]
        if hit:
            _browse_cache.pop(key, None)
    return None


def _browse_store(source: str, page: int, category: str, payload: dict[str, Any]) -> None:
    key = (source, page, category)
    with _browse_lock:
        _browse_cache[key] = (time.time(), payload)
        if len(_browse_cache) > 240:
            stale = sorted(_browse_cache.items(), key=lambda item: item[1][0])[:80]
            for stale_key, _ in stale:
                _browse_cache.pop(stale_key, None)


def clear_browse_cache() -> None:
    with _browse_lock:
        _browse_cache.clear()


def _browse_single_flight(
    source: str,
    page: int,
    category: str,
    build,
) -> dict[str, Any]:
    """同一页的并发请求只真正执行一次，其余请求等同一个结果。"""
    key = (source, page, category)
    with _browse_lock:
        fut = _browse_inflight.get(key)
        owner = fut is None
        if owner:
            fut = Future()
            _browse_inflight[key] = fut
    if not owner:
        return fut.result(timeout=150)
    try:
        payload = build()
        fut.set_result(payload)
        return payload
    except BaseException as exc:  # noqa: BLE001
        fut.set_exception(exc)
        raise
    finally:
        with _browse_lock:
            _browse_inflight.pop(key, None)


@app.get("/api/browse")
async def browse(
    source: str = Query(..., min_length=1),
    page: int = Query(1, ge=1, le=2000),
    category: str = "",
) -> dict[str, Any]:
    adapter = registry.get(source)
    if adapter is None:
        raise HTTPException(404, "图源不存在")

    cached = _browse_cached(source, page, category)
    if cached is not None:
        return {**cached, "cached": True}

    def build() -> dict[str, Any]:
        return _browse_payload(adapter, page, category)

    try:
        payload = await asyncio.to_thread(_browse_single_flight, source, page, category, build)
    except SourceError as exc:
        note_health(source, False, error=str(exc))
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        note_health(source, False, error=f"{type(exc).__name__}: {exc}")
        log.warning("浏览失败 %s: %s", source, exc)
        raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc
    _browse_store(source, page, category, payload)
    return payload


@app.get("/api/categories")
async def categories(source: str = Query(..., min_length=1)) -> dict[str, Any]:
    """单独取某个图源的分类（列表接口不再为它等网络）。"""
    adapter = registry.get(source)
    if adapter is None:
        raise HTTPException(404, "图源不存在")
    cached = _CATEGORY_CACHE.get(source)
    try:
        items = await asyncio.to_thread(_cached_categories, adapter)
    except Exception as exc:  # noqa: BLE001 - 分类拿不到不该让界面报错
        log.warning("分类失败 %s: %s", source, exc)
        return {"categories": list(cached[1]) if cached else [], "error": f"{type(exc).__name__}: {exc}"}
    return {"categories": items}


@app.get("/api/search")
async def search(q: str = Query(...), sources: str = "", page: int = 1) -> dict[str, Any]:
    """非流式搜索（脚本 / 调试用）；界面走 /api/search/stream。"""
    query = q.strip()
    keys = [item for item in sources.split(",") if item] or [
        adapter.key for adapter in _adapters()
    ]
    results: list[dict[str, Any]] = []
    for key in keys:
        adapter = registry.get(key)
        if adapter is None:
            continue
        try:
            items, has_more, total = await asyncio.to_thread(adapter.search, query, page)
        except Exception as exc:  # noqa: BLE001
            results.append({
                "source": {"key": key, "name": key},
                "error": str(exc),
                "items": [],
                "hasMore": False,
            })
            continue
        results.append({
            "source": {"key": adapter.key, "name": adapter.name, "icon": adapter.info.icon},
            "items": [_store_series(adapter, raw) for raw in items if isinstance(raw, dict)],
            "hasMore": bool(has_more),
            "total": total,
        })
    return {"query": query, "results": results, "page": page}


def _adapters() -> list[Any]:
    return [
        adapter for adapter in (registry.get(item["key"]) for item in registry.sources())
        if adapter is not None
    ]


async def _search_stream(query: str, keys: list[str]) -> AsyncIterator[str]:
    adapters = [registry.get(key) for key in keys]
    adapters = [adapter for adapter in adapters if adapter is not None]
    settings = load_settings()
    limit = settings.search_concurrency or len(adapters) or 1
    semaphore = asyncio.Semaphore(max(1, int(limit)))
    counters = {"done": 0, "hit": 0}
    query_started = time.time()

    def sse(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    yield sse("start", {
        "query": query,
        "sources": [{"key": a.key, "name": a.name, "icon": a.info.icon} for a in adapters],
    })

    async def run_one(adapter: Any) -> None:
        async with semaphore:
            started = time.time()
            try:
                items, has_more, total = await asyncio.to_thread(
                    adapter.search, query, 1
                )
                public_items = [_store_series(adapter, raw) for raw in items if isinstance(raw, dict)]
                public_items.sort(key=series_sort_key)
                payload = {
                    "source": {
                        "key": adapter.key,
                        "name": adapter.name,
                        "icon": adapter.info.icon,
                        "nsfw": adapter.info.nsfw,
                    },
                    "items": public_items,
                    "hasMore": bool(has_more),
                    "total": total,
                    "elapsed": round(time.time() - started, 2),
                }
                if public_items:
                    counters["hit"] += 1
            except Exception as exc:  # noqa: BLE001 - 单个图源失败不影响整体
                payload = {
                    "source": {
                        "key": adapter.key,
                        "name": adapter.name,
                        "icon": adapter.info.icon,
                    },
                    "items": [],
                    "hasMore": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed": round(time.time() - started, 2),
                }
            counters["done"] += 1
            await _out.put(payload)

    _out: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    workers = [asyncio.create_task(run_one(adapter)) for adapter in adapters]
    total_count = len(adapters)
    try:
        for _ in range(total_count):
            payload = await _out.get()
            if payload is None:
                continue
            yield sse("result", payload)
        yield sse("done", {
            "sources": total_count,
            "withResults": counters["hit"],
            "elapsed": round(time.time() - query_started, 2),
        })
    finally:
        for worker in workers:
            worker.cancel()
        try:
            await asyncio.gather(*workers, return_exceptions=True)
        except Exception:
            pass


@app.get("/api/search/stream")
async def search_stream(q: str = Query(...), sources: str = "") -> StreamingResponse:
    query = q.strip()
    if not query:
        raise HTTPException(400, "搜索词不能为空")
    keys = [item for item in sources.split(",") if item] or [
        info["key"] for info in registry.sources() if not info.get("error")
    ]
    return StreamingResponse(
        _search_stream(query, keys),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# 详情 / 章节 / 阅读
# ---------------------------------------------------------------------------
@app.get("/api/series/{uid:path}")
async def series_detail(uid: str) -> dict[str, Any]:
    record = store.get_series(uid)
    if record is None:
        raise HTTPException(404, "作品数据已过期，请重新搜索")
    adapter = registry.get(record.provider)
    if adapter is None:
        raise HTTPException(404, "图源不可用")
    raw = dict(record.raw)
    detail_at = time.time()
    try:
        extra = await asyncio.to_thread(adapter.detail, raw)
        note_health(adapter.key, True, time.time() - detail_at)
    except Exception as exc:  # noqa: BLE001 - 详情失败就先用列表里的字段
        note_health(adapter.key, False, error=f"详情：{type(exc).__name__}: {exc}")
        extra = {}
    if extra:
        raw.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
        record.raw.update(raw)
        public = normalize_series(
            adapter.key, adapter.name, raw, base_url=record.base_url, nsfw=record.nsfw
        )
        # 保留 uid，避免同一作品因为详情页字段变化换了 id
        public["uid"] = record.uid
        record.public.update(public)
        store.put_series(record)

    cached = store.get_chapters(record.uid)
    if cached is None:
        chapters, raw_items = await _load_chapters(record, adapter)
    else:
        chapters, raw_items = cached.items, cached.raw_items
    return {
        "series": record.public,
        "chapters": _order_chapters(chapters),
        "cached": cached is not None,
    }


async def _load_chapters(record: SeriesRecord, adapter: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        raw_items = await asyncio.to_thread(adapter.chapters, dict(record.raw))
    except SourceError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc
    chapters = []
    kept_raw = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            continue
        public = normalize_chapter(
            record.uid, adapter.key, adapter.name, raw, index=index,
            base_url=record.base_url,
        )
        chapters.append(public)
        kept_raw.append(raw)
    store.put_chapters(record.uid, chapters, kept_raw)
    return chapters, kept_raw


def _order_chapters(chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    settings = load_settings()
    if not settings.chapter_newest_first:
        return chapters
    # 后端返回的通常是「旧 → 新」，界面默认新章在前
    return list(reversed(chapters))


@app.post("/api/series/{uid:path}/refresh")
async def refresh_chapters(uid: str) -> dict[str, Any]:
    record = store.get_series(uid)
    if record is None:
        raise HTTPException(404, "作品数据已过期")
    adapter = registry.get(record.provider)
    if adapter is None:
        raise HTTPException(404, "图源不可用")
    chapters, _ = await _load_chapters(record, adapter)
    return {"series": record.public, "chapters": _order_chapters(chapters), "cached": False}


@app.get("/api/read/{chapter_uid:path}")
async def read_chapter(chapter_uid: str) -> dict[str, Any]:
    series_uid = series_of_chapter(chapter_uid)
    record = store.get_series(series_uid)
    if record is None:
        raise HTTPException(404, "作品数据已过期，请返回重新打开")
    adapter = registry.get(record.provider)
    if adapter is None:
        raise HTTPException(404, "图源不可用")
    cached = store.get_pages(chapter_uid)
    if cached is not None:
        # 命中缓存也要带上章节信息：阅读器靠它显示标题、上报浏览历史
        return {"chapterUid": chapter_uid, "series": record.public,
                "chapter": store.find_chapter_public(series_uid, chapter_uid),
                "pages": _public_pages(chapter_uid, cached.items), "cached": True}
    raw_chapter = store.find_chapter(series_uid, chapter_uid)
    if raw_chapter is None:
        raise HTTPException(404, "章节数据已过期，请重新打开作品")
    public_chapter = store.find_chapter_public(series_uid, chapter_uid)
    try:
        raw_pages = await asyncio.to_thread(
            adapter.pages, dict(record.raw), dict(raw_chapter)
        )
    except SourceError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc
    pages = normalize_page_list(raw_pages, record.base_url)
    referer = ""
    try:
        referer = adapter.referer(dict(record.raw), dict(raw_chapter)) or ""
    except Exception:
        referer = str(raw_chapter.get("url") or record.base_url or "")
    store.put_pages(PageList(chapter_uid, series_uid, pages, referer))
    return {
        "chapterUid": chapter_uid,
        "series": record.public,
        "chapter": public_chapter,
        "pages": _public_pages(chapter_uid, pages),
        "cached": False,
    }


def _public_pages(chapter_uid: str, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "index": page["index"],
            "url": f"/img/page/{chapter_uid}/{page['index']}?token={TOKEN}",
            "width": page.get("width") or 0,
            "height": page.get("height") or 0,
        }
        for page in pages
    ]


# ---------------------------------------------------------------------------
# 图片代理
# ---------------------------------------------------------------------------
def _image_response(data: bytes, mime: str, cache_key: str = "") -> Response:
    headers = {
        "Cache-Control": "public, max-age=31536000, immutable" if cache_key else "no-cache",
        "Content-Length": str(len(data)),
    }
    return Response(content=data, media_type=mime, headers=headers)


@app.get("/img/cover/{uid:path}")
async def cover(uid: str, w: int = Query(0, ge=0, le=2000)) -> Response:
    """封面代理。``w`` 给了就返回等宽缩略图（列表页用，省一个数量级的流量）。"""
    record = store.get_series(uid)
    if record is None:
        raise HTTPException(404, "封面不存在")
    remote = str(record.public.get("coverRemote") or "")
    if not remote:
        raise HTTPException(404, "没有封面")
    adapter = registry.get(record.provider)
    if adapter is None:
        raise HTTPException(404, "图源不可用")
    referer = record.base_url or str(record.raw.get("url") or "")

    def fetch() -> tuple[bytes, str]:
        fetcher = lambda: adapter.fetch_image(remote, referer)  # noqa: E731
        if w:
            return image_cache.get_thumb(remote, referer, w, fetcher)
        return image_cache.get_or_fetch(remote, referer, fetcher)

    try:
        data, mime = await asyncio.to_thread(fetch)
    except Exception as exc:  # noqa: BLE001
        log.info("封面失败 %s: %s", remote, exc)
        raise HTTPException(502, "封面加载失败") from exc
    return _image_response(data, mime, f"{remote}#w{w}" if w else remote)


@app.get("/img/page/{chapter_uid}/{index:int}")
async def page_image(chapter_uid: str, index: int) -> Response:
    entry = store.get_pages(chapter_uid)
    series_uid = series_of_chapter(chapter_uid)
    record = store.get_series(series_uid)
    if record is None:
        raise HTTPException(404, "作品数据已过期")
    adapter = registry.get(record.provider)
    if adapter is None:
        raise HTTPException(404, "图源不可用")
    if entry is None:
        raise HTTPException(404, "章节图片未加载")
    page = next((item for item in entry.items if int(item.get("index", -1)) == index), None)
    if page is None:
        raise HTTPException(404, "页码超出范围")
    remote = str(page.get("remote") or page.get("url") or "")
    referer = entry.referer or record.base_url

    def fetch() -> tuple[bytes, str]:
        return image_cache.get_or_fetch(
            remote, referer, lambda: adapter.fetch_image(remote, referer)
        )

    try:
        data, mime = await asyncio.to_thread(fetch)
    except Exception as exc:  # noqa: BLE001
        log.info("图片失败 %s: %s", remote, exc)
        raise HTTPException(502, "图片加载失败") from exc
    return _image_response(data, mime, remote)


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------
@app.post("/api/download")
async def start_download(req: DownloadRequest) -> dict[str, Any]:
    try:
        task = await asyncio.to_thread(
            tasks.submit, req.seriesUid, req.chapterUids, req.outputDir, req.workers
        )
    except SourceError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "task": task}


@app.get("/api/downloads")
async def list_downloads(limit: int = 100) -> dict[str, Any]:
    return {"tasks": tasks.list(limit), "history": tasks.history(50), "stats": tasks.stats()}


@app.post("/api/downloads/{task_id}/cancel")
async def cancel_download(task_id: str) -> dict[str, Any]:
    try:
        return {"ok": True, "task": tasks.cancel(task_id)}
    except SourceError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/downloads/clear")
async def clear_downloads() -> dict[str, Any]:
    return {"ok": True, "removed": tasks.clear_finished()}


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    sub = bus.subscribe(["*"])
    return StreamingResponse(
        sub.stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# 插件管理
# ---------------------------------------------------------------------------
@app.get("/api/plugins")
async def list_plugins() -> dict[str, Any]:
    sources = [item for item in registry.sources() if item["kind"] == "plugin"]
    return {
        "plugins": sources,
        "failed": registry.failed(),
        "dir": str(registry.plugin_dir()),
        "sourceDir": str(JSON_SOURCE_DIR),
        "sourceFiles": sorted(p.name for p in JSON_SOURCE_DIR.glob("*.json"))
        if JSON_SOURCE_DIR.is_dir()
        else [],
        "template": plugin_template(),
        "version": VERSION,
    }


@app.get("/api/plugins/file")
async def read_plugin_file(file: str = "") -> dict[str, Any]:
    try:
        return {"ok": True, **registry.read_plugin_file(file)}
    except SourceError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/plugins/template")
async def get_plugin_template(name: str = "my_source", display: str = "我的图源",
                              base_url: str = "https://example.com") -> dict[str, Any]:
    text = plugin_template(name=name, display=display, base_url=base_url)
    filename = re.sub(r"[^a-z0-9_]", "_", name.strip().lower()) or "my_source"
    return {"filename": f"{filename}.py", "content": text}


@app.post("/api/plugins/save")
async def save_plugin(req: PluginSave) -> dict[str, Any]:
    try:
        return {"ok": True, **(await asyncio.to_thread(
            registry.save_plugin_file, req.filename, req.content
        ))}
    except SourceError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/plugins/upload")
async def upload_plugin(request: Request) -> dict[str, Any]:
    data = await request.body()
    if not data:
        raise HTTPException(400, "空文件")
    try:
        result = await asyncio.to_thread(registry.install_from_zip, data)
    except SourceError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, **result}


@app.post("/api/plugins/reload")
async def reload_plugins() -> dict[str, Any]:
    result = await asyncio.to_thread(registry.reload_plugins)
    return {"ok": True, **result, "plugins": [i for i in registry.sources() if i["kind"] == "plugin"]}


@app.delete("/api/plugins/{key}")
async def delete_plugin(key: str) -> dict[str, Any]:
    try:
        return {"ok": True, **(await asyncio.to_thread(registry.delete_plugin, key))}
    except SourceError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/plugins/{key}/copy")
async def copy_plugin(key: str) -> dict[str, Any]:
    try:
        return {"ok": True, **(await asyncio.to_thread(registry.copy_builtin_to_user, key))}
    except SourceError as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------------------
# 系统：选目录 / 打开目录 / 缓存
# ---------------------------------------------------------------------------
_PICK_SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms | Out-Null
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择呆猫漫画的下载目录'
$dialog.ShowNewFolderButton = $true
try {
  $initial = [Environment]::GetEnvironmentVariable('DUMBCAT_INITIAL_DIR')
  if ($initial -and (Test-Path $initial)) { $dialog.SelectedPath = $initial }
} catch {}
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
  [Console]::Out.WriteLine($dialog.SelectedPath)
}
"""


@app.post("/api/choose-folder")
async def choose_folder(req: PathRequest | None = None) -> dict[str, Any]:
    initial = (req.path if req else "") or load_settings().output_dir
    if sys.platform != "win32":
        raise HTTPException(501, "当前系统不支持原生目录选择")
    env = dict(os.environ)
    env["DUMBCAT_INITIAL_DIR"] = initial
    script = _PICK_SCRIPT
    try:
        process = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile", "-STA", "-Command", script,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
    except asyncio.TimeoutError as exc:
        raise HTTPException(408, "选择目录超时") from exc
    except OSError as exc:
        raise HTTPException(500, f"无法打开目录选择框：{exc}") from exc
    encoding = "mbcs" if sys.platform == "win32" else "utf-8"
    try:
        decoded_out = stdout.decode(encoding, "ignore")
    except LookupError:
        decoded_out = stdout.decode("utf-8", "ignore")
    picked = decoded_out.strip().splitlines()
    path = picked[-1].strip() if picked else ""
    if not path:
        try:
            feedback = stderr.decode(encoding, "ignore")
        except LookupError:
            feedback = ""
        for line in feedback.splitlines():
            log.info("目录选择：%s", line)
    return {"ok": bool(path), "path": path}


@app.post("/api/open-folder")
async def open_folder(req: PathRequest) -> dict[str, Any]:
    path = Path(req.path)
    if not path.exists():
        raise HTTPException(404, "目录不存在")
    if sys.platform != "win32":
        # 手机上没有资源管理器可唤起，让界面提示用文件管理器
        raise HTTPException(501, "手机版请用文件管理器打开下载目录")
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(path)])  # noqa: S603
    except OSError as exc:
        raise HTTPException(500, str(exc)) from exc
    return {"ok": True}


@app.get("/api/cache/stats")
async def cache_stats() -> dict[str, Any]:
    return {
        "bytes": image_cache.size_bytes(),
        "settings": {"image_cache_mb": load_settings().image_cache_mb},
    }


@app.post("/api/cache/clear")
async def cache_clear() -> dict[str, Any]:
    removed = await asyncio.to_thread(image_cache.clear)
    return {"ok": True, "removed": removed}


@app.get("/api/history")
async def download_history() -> dict[str, Any]:
    return {"history": tasks.history(100), "outputDir": load_settings().output_dir}


@app.get("/api/folders")
async def suggest_folders() -> dict[str, Any]:
    root = output_root()
    children = []
    try:
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower())[:80]:
            if child.is_dir():
                children.append({"name": child.name, "path": str(child)})
    except OSError:
        pass
    return {"root": str(root), "folders": children}

# ---------------------------------------------------------------------------
# 书架 / 官方收藏 / 阅读历史
# 契约：呆猫漫画-安卓/docs/书架接口.md 第 2、3、4 节
# 说明：本段整体追加在文件末尾，不改动上面的任何既有路由与函数。
# ---------------------------------------------------------------------------

from dumbcat import shelf as shelf_store  # noqa: E402
from dumbcat.sources.base import FavoritesUnsupported, LoginRequired  # noqa: E402

SHELF_REMOTE_TIMEOUT = 20.0
SHELF_SYNC_TIMEOUT = 60.0
SHELF_OVERVIEW_TIMEOUT = 1.5
#: 一次同步最多翻多少页收藏（禁漫一页 20 条、单页 6~8 秒，页数越多越慢）
SHELF_SYNC_DEFAULT_PAGES = 5
SHELF_SYNC_MAX_PAGES = 40


class ShelfCreateRequest(BaseModel):
    name: str = ""


class ShelfRenameRequest(BaseModel):
    id: str = ""
    name: str = ""


class ShelfDeleteRequest(BaseModel):
    id: str = ""


class ShelfAddRequest(BaseModel):
    shelfId: str = ""
    uid: str = ""
    source: str = ""
    series: dict[str, Any] = {}


class ShelfRemoveRequest(BaseModel):
    shelfId: str = ""
    uid: str = ""


class ShelfSyncRequest(BaseModel):
    source: str = ""
    #: 本次最多翻多少页收藏（0/缺省按 SHELF_SYNC_DEFAULT_PAGES）
    pages: int = 0


class ShelfHistoryRequest(BaseModel):
    uid: str = ""
    source: str = ""
    title: str = ""
    cover: str = ""
    coverUrl: str = ""
    chapterUid: str = ""
    chapterTitle: str = ""
    page: int = 0
    total: int = 0
    ts: int = 0


class ShelfHistoryRemoveRequest(BaseModel):
    uid: str = ""


def _shelf_provider(source: str) -> tuple[Any, Any]:
    adapter = registry.get(source) if source else None
    if adapter is None:
        return None, None
    provider = getattr(adapter, "provider", None) or adapter
    return adapter, provider


def _shelf_supports_favorites(provider: Any) -> bool:
    if provider is None:
        return False
    try:
        return bool(provider.supports_favorites())
    except Exception:  # noqa: BLE001 - 能力探测失败按不支持处理
        return False


def _shelf_is_logged_in(provider: Any) -> bool:
    if provider is None:
        return False
    try:
        return bool(provider.is_logged_in())
    except Exception:  # noqa: BLE001 - 登录态校验失败按未登录处理
        return False


def _shelf_username(provider: Any) -> str:
    try:
        session = getattr(provider, "_session", None)
        if session is None:
            session = getattr(provider, "session", None)
        data = getattr(session, "data", None) or {}
        return str(data.get("username") or "")
    except Exception:  # noqa: BLE001
        return ""


def _shelf_display_name(adapter: Any, provider: Any, fallback: str = "") -> str:
    for value in (
        getattr(provider, "display_name", ""),
        getattr(adapter, "name", ""),
        fallback,
    ):
        text = str(value or "").strip()
        if text:
            return text
    return fallback


def _shelf_sources_snapshot(network: bool) -> list[dict[str, Any]]:
    """只列支持官方收藏的图源；network=False 时读会话状态，不发请求。"""
    out: list[dict[str, Any]] = []
    for info in registry.sources():
        key = str(info.get("key") or "")
        adapter = registry.get(key)
        if adapter is None:
            continue
        provider = getattr(adapter, "provider", None) or adapter
        if not _shelf_supports_favorites(provider):
            continue
        if network:
            logged_in = _shelf_is_logged_in(provider)
            username = _shelf_username(provider)
        else:
            try:
                session = getattr(provider, "_session", None) or getattr(provider, "session", None)
            except Exception:  # noqa: BLE001
                session = None
            logged_in = bool(getattr(session, "confirmed", False))
            username = _shelf_username(provider) if logged_in else ""
        out.append(
            {
                "key": key,
                "name": _shelf_display_name(adapter, provider, str(info.get("name") or key)),
                "supportsFavorites": True,
                "loggedIn": logged_in,
                "username": username,
                "shelfId": f"{shelf_store.SOURCE_SHELF_PREFIX}{key}",
            }
        )
    return out


def _shelf_guess_comic_id(url: str) -> str:
    text = str(url or "")
    match = re.search(r"/(\d{4,})(?:[/?#]|$)", text) or re.search(r"(\d{4,})", text)
    return match.group(1) if match else ""


def _shelf_remote_series(uid: str, data: dict[str, Any]) -> dict[str, Any]:
    """给 provider.add_favorite/remove_favorite 用的原始 series。

    前端从 /api/series/{uid} 拿到的 public dict 里没有 id/slug，所以先从本地
    series store 的 raw 里补，再尝试从详情地址里认出禁漫的作品 id。
    """
    merged: dict[str, Any] = {}
    try:
        record = store.get_series(uid) if uid else None
    except Exception:  # noqa: BLE001
        record = None
    if record is not None and isinstance(getattr(record, "raw", None), dict):
        merged.update(record.raw)
    clean = dict(data or {})
    if "shelfId" in clean:
        # 这是书架 item，不是 series：item["id"] 是 i_xxx，不能当作品 id 传给图源
        clean = {
            key: value
            for key, value in clean.items()
            if key not in ("id", "shelfId", "added", "remote")
        }
    merged.update({key: value for key, value in clean.items() if value not in (None, "")})
    if not merged.get("id") and not merged.get("slug"):
        comic_id = _shelf_guess_comic_id(str(merged.get("url") or ""))
        if comic_id:
            merged["id"] = comic_id
    return merged


async def _shelf_remote_call(fn: Any, *args: Any) -> str:
    """在线程里调远端收藏操作，返回 remote 状态；任何异常都降级。"""
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(fn, *args), timeout=SHELF_REMOTE_TIMEOUT
        )
    except asyncio.TimeoutError:
        log.warning("图源收藏操作超时：%s", getattr(fn, "__name__", fn))
        return "failed"
    except FavoritesUnsupported:
        return "unsupported"
    except LoginRequired:
        return "not-logged-in"
    except Exception as exc:  # noqa: BLE001 - 远端失败不能影响本地
        log.warning("图源收藏操作失败：%s", exc)
        return "failed"
    return "failed" if result is False else "synced"


async def _shelf_remote_state_for(source: str) -> tuple[Any, Any, str]:
    adapter, provider = _shelf_provider(source)
    if not _shelf_supports_favorites(provider):
        return adapter, provider, "unsupported"
    try:
        logged_in = await asyncio.wait_for(
            asyncio.to_thread(_shelf_is_logged_in, provider),
            timeout=SHELF_REMOTE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return adapter, provider, "failed"
    if not logged_in:
        return adapter, provider, "not-logged-in"
    return adapter, provider, ""


@app.get("/api/shelf")
async def shelf_overview() -> dict[str, Any]:
    shelves = await asyncio.to_thread(shelf_store.list_shelves)
    default_id = await asyncio.to_thread(shelf_store.default_shelf_id)
    history_count = await asyncio.to_thread(shelf_store.history_count)
    try:
        sources = await asyncio.wait_for(
            asyncio.to_thread(_shelf_sources_snapshot, True),
            timeout=SHELF_OVERVIEW_TIMEOUT,
        )
    except asyncio.TimeoutError:
        sources = await asyncio.to_thread(_shelf_sources_snapshot, False)
    return {
        "shelves": shelves,
        "defaultShelfId": default_id,
        "sources": sources,
        "historyCount": history_count,
    }


@app.get("/api/shelf/items")
async def shelf_items(shelfId: str = "") -> dict[str, Any]:
    rows = await asyncio.to_thread(shelf_store.list_items, shelfId)
    return {"items": rows}


@app.get("/api/shelf/status")
async def shelf_status(uid: str = "") -> dict[str, Any]:
    target = uid.strip()
    if not target:
        raise HTTPException(400, "缺少作品 uid")
    shelves = await asyncio.to_thread(shelf_store.shelves_of, target)
    return {"shelves": shelves, "saved": bool(shelves)}


@app.get("/api/shelf/history")
async def shelf_history(limit: int = 100) -> dict[str, Any]:
    rows = await asyncio.to_thread(shelf_store.history_list, limit)
    return {"items": rows}


@app.post("/api/shelf/create")
async def shelf_create(req: ShelfCreateRequest) -> dict[str, Any]:
    try:
        shelf = await asyncio.to_thread(shelf_store.create_shelf, req.name)
    except shelf_store.ShelfError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"shelf": shelf}


@app.post("/api/shelf/rename")
async def shelf_rename(req: ShelfRenameRequest) -> dict[str, Any]:
    try:
        shelf = await asyncio.to_thread(shelf_store.rename_shelf, req.id, req.name)
    except shelf_store.ShelfError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"shelf": shelf}


@app.post("/api/shelf/delete")
async def shelf_delete(req: ShelfDeleteRequest) -> dict[str, Any]:
    try:
        await asyncio.to_thread(shelf_store.delete_shelf, req.id)
    except shelf_store.ShelfError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.post("/api/shelf/add")
async def shelf_add(req: ShelfAddRequest) -> dict[str, Any]:
    payload = dict(req.series or {})
    uid = (req.uid or "").strip() or str(payload.get("uid") or "").strip()
    source = (
        (req.source or "").strip()
        or str(payload.get("provider") or payload.get("source") or "").strip()
        or shelf_store.source_from_uid(uid)
    )
    if not uid:
        raise HTTPException(400, "缺少作品 uid")
    if not source:
        raise HTTPException(400, "缺少图源标识")
    payload["uid"] = uid
    payload["source"] = source
    shelf_id = (req.shelfId or "").strip()
    if not shelf_id:
        shelf_id = await asyncio.to_thread(shelf_store.default_shelf_id)

    existing = await asyncio.to_thread(shelf_store.get_item, shelf_id, uid)
    if existing is not None:
        shelf = await asyncio.to_thread(shelf_store.get_shelf, shelf_id)
        return {"item": existing, "remote": existing.get("remote") or "", "shelf": shelf}

    adapter, provider, remote = await _shelf_remote_state_for(source)
    try:
        item = await asyncio.to_thread(shelf_store.add_item, shelf_id, payload, remote)
    except shelf_store.ShelfError as exc:
        raise HTTPException(400, str(exc)) from exc
    if remote == "" and provider is not None:
        remote = await _shelf_remote_call(
            provider.add_favorite, _shelf_remote_series(uid, payload), None
        )
        item = await asyncio.to_thread(
            shelf_store.set_item_remote, shelf_id, uid, remote
        ) or item
    shelf = await asyncio.to_thread(shelf_store.get_shelf, shelf_id)
    return {"item": item, "remote": item.get("remote") or remote, "shelf": shelf}


@app.post("/api/shelf/remove")
async def shelf_remove(req: ShelfRemoveRequest) -> dict[str, Any]:
    shelf_id = (req.shelfId or "").strip()
    uid = (req.uid or "").strip()
    if not shelf_id:
        raise HTTPException(400, "缺少书架 id")
    if not uid:
        raise HTTPException(400, "缺少作品 uid")
    item = await asyncio.to_thread(shelf_store.get_item, shelf_id, uid)
    try:
        await asyncio.to_thread(shelf_store.remove_item, shelf_id, uid)
    except shelf_store.ShelfError as exc:
        raise HTTPException(400, str(exc)) from exc
    remaining = await asyncio.to_thread(shelf_store.shelves_of, uid)
    if remaining:
        return {"ok": True, "remote": "kept"}
    if item is None:
        return {"ok": True, "remote": ""}
    source = str(item.get("source") or "")
    adapter, provider, remote = await _shelf_remote_state_for(source)
    if remote == "" and provider is not None:
        remote = await _shelf_remote_call(
            provider.remove_favorite, _shelf_remote_series(uid, item)
        )
    return {"ok": True, "remote": remote}


@app.get("/api/sources/{key}/favorites")
async def source_favorites(key: str, pages: int = Query(0, ge=0, le=40)) -> dict[str, Any]:
    """只读地列出某个图源的官方收藏，交给用户自己挑着加进书架。"""
    adapter, provider = _shelf_provider(key)
    if adapter is None or provider is None:
        raise HTTPException(400, "图源不存在或未启用")
    display_name = _shelf_display_name(adapter, provider, key)
    if not _shelf_supports_favorites(provider):
        raise HTTPException(400, f"{display_name}不支持官方收藏夹")
    if not await asyncio.to_thread(_shelf_is_logged_in, provider):
        raise HTTPException(400, f"还没登录{display_name}")

    pages = int(pages or 0) or SHELF_SYNC_DEFAULT_PAGES
    pages = max(1, min(pages, SHELF_SYNC_MAX_PAGES))
    budget = max(SHELF_SYNC_TIMEOUT, 25.0 + pages * 3.6)

    def work() -> list[dict[str, Any]]:
        try:
            rows = provider.favorite_series("", max_pages=pages)
        except TypeError:
            rows = provider.favorite_series("")
        out: list[dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            try:
                out.append(_store_series(adapter, row))
            except Exception as exc:  # noqa: BLE001 - 单条坏数据跳过
                log.warning("读取收藏项失败：%s", exc)
        return out

    try:
        items = await asyncio.wait_for(asyncio.to_thread(work), timeout=budget)
    except asyncio.TimeoutError as exc:
        raise HTTPException(400, "读取收藏超时，请稍后重试") from exc
    except FavoritesUnsupported as exc:
        raise HTTPException(400, f"{display_name}不支持官方收藏夹") from exc
    except LoginRequired as exc:
        raise HTTPException(400, f"还没登录{display_name}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"读取收藏失败：{exc}") from exc

    total_hint = 0
    try:
        hint = getattr(provider, "favorite_total_hint", None)
        if callable(hint):
            total_hint = int(hint() or 0)
    except Exception:  # noqa: BLE001
        total_hint = 0
    return {
        "source": key,
        "name": display_name,
        "items": items,
        "pages": pages,
        "remoteTotal": total_hint,
        "truncated": bool(total_hint and total_hint > len(items)),
    }


@app.post("/api/shelf/sync")
async def shelf_sync(req: ShelfSyncRequest) -> dict[str, Any]:
    source = (req.source or "").strip()
    if not source:
        raise HTTPException(400, "缺少图源标识")
    adapter, provider = _shelf_provider(source)
    if adapter is None or provider is None:
        raise HTTPException(400, "图源不存在或未启用")
    display_name = _shelf_display_name(adapter, provider, source)
    if not _shelf_supports_favorites(provider):
        raise HTTPException(400, f"{display_name}不支持官方收藏夹")
    if not await asyncio.to_thread(_shelf_is_logged_in, provider):
        raise HTTPException(400, f"还没登录{display_name}")

    pages = int(req.pages or 0) or SHELF_SYNC_DEFAULT_PAGES
    pages = max(1, min(pages, SHELF_SYNC_MAX_PAGES))
    #: 单页实测 6~8 秒、并发 3 路，按页数放大等待预算，避免「收藏上千条必超时」
    budget = max(SHELF_SYNC_TIMEOUT, 25.0 + pages * 3.6)

    def work() -> list[dict[str, Any]]:
        try:
            rows = provider.favorite_series("", max_pages=pages)
        except TypeError:
            #: 老图源不认识 max_pages
            rows = provider.favorite_series("")
        publics: list[dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            try:
                publics.append(_store_series(adapter, row))
            except Exception as exc:  # noqa: BLE001 - 单条坏数据跳过
                log.warning("同步收藏项失败：%s", exc)
        return publics

    try:
        publics = await asyncio.wait_for(
            asyncio.to_thread(work), timeout=budget
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(400, "同步收藏超时，请稍后重试") from exc
    except FavoritesUnsupported as exc:
        raise HTTPException(400, f"{display_name}不支持官方收藏夹") from exc
    except LoginRequired as exc:
        raise HTTPException(400, f"还没登录{display_name}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"同步收藏失败：{exc}") from exc

    result = await asyncio.to_thread(
        shelf_store.sync_source_items, source, publics, display_name
    )
    if isinstance(result, dict):
        total_hint = 0
        try:
            hint = getattr(provider, "favorite_total_hint", None)
            if callable(hint):
                total_hint = int(hint() or 0)
        except Exception:  # noqa: BLE001
            total_hint = 0
        result["remoteTotal"] = total_hint
        result["pages"] = pages
        result["truncated"] = bool(total_hint and total_hint > len(publics))
    return result


@app.post("/api/shelf/history")
async def shelf_history_add(req: ShelfHistoryRequest) -> dict[str, Any]:
    record = {
        "uid": req.uid,
        "source": req.source,
        "title": req.title,
        "cover": req.cover or req.coverUrl,
        "chapterUid": req.chapterUid,
        "chapterTitle": req.chapterTitle,
        "page": req.page,
        "total": req.total,
        "ts": req.ts,
    }
    try:
        await asyncio.to_thread(shelf_store.history_add, record)
    except shelf_store.ShelfError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.post("/api/shelf/history/clear")
async def shelf_history_clear() -> dict[str, Any]:
    removed = await asyncio.to_thread(shelf_store.history_clear)
    return {"ok": True, "removed": removed}


@app.post("/api/shelf/history/remove")
async def shelf_history_remove(req: ShelfHistoryRemoveRequest) -> dict[str, Any]:
    try:
        await asyncio.to_thread(shelf_store.history_remove, req.uid)
    except shelf_store.ShelfError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}
