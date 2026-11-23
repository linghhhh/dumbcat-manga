"""下载管线：把章节图片并发抓到本地（.part 临时文件 + 原子替换）。

界面无关，只依赖 dumbcat.sources.base 的 provider 抽象与 leaf 工具模块。
"""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import os
import queue
import re
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# 进程临时目录必须在使用浏览器的模块被导入之前指向运行时目录；该调用是幂等的，
# dumbcat.app_config 导入时也会做同样一件事。
from dumbcat.runtime_cleanup import configure_process_temp

configure_process_temp()

from dumbcat.app_config import (
    BAOZIMH_SITE_BASE,
    COMIC18_SITE_BASE,
    COMIX_IMAGE_HEADERS,
    COMIX_SITE_BASE,
    COPYMANGA_API_BASE,
    COPYMANGA_SITE_BASE,
    IMAGE_EXTENSIONS,
    IMAGE_HEADERS,
    JM_AES_SEEDS,
    JM_API_BASE,
    JM_IMAGE_BASE,
    JM_VERSION,
    MANGA18FX_HTML_HEADERS,
    MANGA18FX_SITE_BASE,
    QIMANGA_API_BASE,
    QIMANGA_API_HEADERS,
    QIMANGA_API_V2_BASE,
    QIMANGA_SITE_BASE,
    RETRYABLE_STATUS,
    USER_AGENT,
    WNACG_SITE_BASE,
)

from dumbcat.naming import (
    INVALID_FILENAME_CHARS,
    WINDOWS_RESERVED_NAMES,
    chapter_number_text,
    chapter_number_value,
    chapter_sort_key,
    format_bytes,
    format_chapter_number,
    format_view_count,
    parse_retry_after,
    sanitize_path_component,
)

from dumbcat.errors import ApiError, friendly_network_error, normalize_image_url
from dumbcat.sources.base import NATIVE_COMPLETE_SUFFIX, SeriesProvider


@dataclass(frozen=True)
class PageJob:
    destination: Path
    image_url: str
    referer: str
    chapter_label: str
    page_number: int
    provider_key: str
    #: 在本次下载的章节序列里的下标（用于逐章状态统计，0 表示未标注）
    chapter_index: int = 0
    #: 章节标题（界面显示用，可为空）
    chapter_title: str = ""


class Downloader:
    def __init__(
        self,
        providers: dict[str, SeriesProvider],
        emit: Callable[..., None],
        cancel_event: threading.Event,
        request_id: int = 0,
    ) -> None:
        self.providers = providers
        self.emit = emit
        self.cancel_event = cancel_event
        # 随下载事件回传，供 GUI 丢弃“上一轮下载”的迟到结果。
        self.request_id = request_id
        #: 每章最多下载多少张图（0 = 不限），由设置页传入。
        self.max_images = 0

    @staticmethod
    def is_locked(chapter: dict[str, Any]) -> bool:
        return SeriesProvider.is_locked(chapter)

    @staticmethod
    def _image_extension(url: str) -> str:
        suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            return suffix
        return ".jpg"

    @staticmethod
    def _chapter_files_complete(destinations: list[Path]) -> bool:
        if not destinations:
            return False
        for destination in destinations:
            try:
                if not destination.is_file() or destination.stat().st_size <= 0:
                    return False
            except OSError:
                return False
        return True

    @staticmethod
    def _page_records(chapter: dict[str, Any]) -> list[tuple[int, str]]:
        records: list[tuple[int, str]] = []
        raw_images = chapter.get("images")
        if isinstance(raw_images, list):
            for index, image_url in enumerate(raw_images, start=1):
                if isinstance(image_url, str) and image_url:
                    records.append((index, image_url))
                elif isinstance(image_url, dict):
                    url = image_url.get("url") or image_url.get("image_url")
                    if isinstance(url, str) and url:
                        try:
                            order = int(image_url.get("order") or index)
                        except (TypeError, ValueError):
                            order = index
                        records.append((order, url))
        return Downloader._dedupe_page_records(records)

    @staticmethod
    def _dedupe_page_records(records: list[tuple[int, str]]) -> list[tuple[int, str]]:
        deduplicated: dict[int, str] = {}
        for index, (page_number, image_url) in enumerate(records, start=1):
            if not isinstance(image_url, str) or not image_url:
                continue
            try:
                normalized_number = int(page_number)
            except (TypeError, ValueError):
                normalized_number = index
            if normalized_number <= 0:
                normalized_number = index
            # 远端可能给出极大页码，避免生成超长文件名（也防止 MAX_PATH 溢出）。
            normalized_number = (normalized_number - 1) % 9999 + 1
            deduplicated[normalized_number] = image_url
        return sorted(deduplicated.items())

    def _fetch_page_records(
        self,
        provider: SeriesProvider,
        series: dict[str, Any],
        chapter: dict[str, Any],
    ) -> list[tuple[int, str]]:
        try:
            pages = provider.get_chapter_pages(series, chapter, self.cancel_event)
        except ApiError as exc:
            if exc.status in {401, 402, 403, 404}:
                self.emit(
                    "log",
                    message=f"章节 {chapter_number_text(chapter)} 的图片接口不可用，已跳过。",
                    level="warning",
                )
                return []
            raise

        records: list[tuple[int, str]] = []
        for index, page in enumerate(pages, start=1):
            image_url = page.get("image_url") or page.get("url")
            if not isinstance(image_url, str) or not image_url:
                continue
            try:
                number = int(page.get("page_number") or page.get("order") or index)
            except (TypeError, ValueError):
                number = index
            records.append((number, image_url))
        if not records:
            records = self._page_records(chapter)
        else:
            records = self._dedupe_page_records(records)
        limit = max(0, int(getattr(self, "max_images", 0) or 0))
        if limit and len(records) > limit:
            self.emit(
                "log",
                message=(
                    f"第 {chapter_number_text(chapter)} 章共 {len(records)} 张，"
                    f"按设置只下载前 {limit} 张。"
                ),
                level="warning",
            )
            records = records[:limit]
        return records

    def _prepare_jobs(
        self,
        series: dict[str, Any],
        chapters: list[dict[str, Any]],
        output_root: Path,
    ) -> tuple[list[PageJob], int, int]:
        grouped_chapters: list[dict[str, PageJob]] = []
        series_name = sanitize_path_component(
            series.get("title") or series.get("slug"),
            "Unknown Series",
            90,
        )
        provider_key = str(series.get("_provider") or "")
        provider = self.providers.get(provider_key)
        if provider is None:
            raise ApiError(f"未知站点：{provider_key}")
        series_root = provider.output_root(output_root, series) / series_name
        locked_count = 0
        skipped_chapters = 0

        for chapter in sorted(chapters, key=chapter_sort_key):
            if self.cancel_event.is_set():
                break
            number_text = chapter_number_text(chapter)
            if self.is_locked(chapter):
                locked_count += 1
                price = chapter.get("price")
                suffix = f"，需要 {price} 金币" if price else ""
                self.emit(
                    "log",
                    message=f"跳过付费章节 {number_text}{suffix}。",
                    level="warning",
                )
                continue

            self.emit(
                "download_status",
                message=f"正在获取第 {number_text} 章的图片列表...",
            )
            referer = provider.chapter_referer(series, chapter)
            try:
                records = self._fetch_page_records(provider, series, chapter)
            except ApiError as exc:
                self.emit(
                    "log",
                    message=f"第 {number_text} 章图片列表获取失败：{exc}",
                    level="error",
                )
                continue
            finally:
                try:
                    removed_bytes = provider.cleanup_chapter_cache()
                except Exception as exc:
                    self.emit(
                        "log",
                        message=f"第 {number_text} 章临时缓存清理失败：{exc}",
                        level="warning",
                    )
                else:
                    if removed_bytes > 0:
                        self.emit(
                            "log",
                            message=(
                                f"第 {number_text} 章浏览器临时缓存已清理："
                                f"{format_bytes(removed_bytes)}"
                            ),
                        )

            if not records:
                self.emit(
                    "log",
                    message=f"第 {number_text} 章没有可用图片，已跳过。",
                    level="warning",
                )
                continue

            title = sanitize_path_component(chapter.get("title"), "", 60)
            folder_name = f"Chapter {format_chapter_number(chapter)}"
            if title:
                folder_name = f"{folder_name} - {title}"
            chapter_folder = series_root / sanitize_path_component(
                folder_name,
                f"Chapter {format_chapter_number(chapter)}",
                130,
            )

            chapter_jobs: dict[str, PageJob] = {}
            chapter_label = f"第 {number_text} 章"
            for page_number, image_url in records:
                extension = self._image_extension(image_url)
                destination = chapter_folder / f"{page_number:04d}{extension}"
                key = str(destination).casefold()
                if key in chapter_jobs:
                    continue
                chapter_jobs[key] = PageJob(
                    destination=destination,
                    image_url=image_url,
                    referer=referer,
                    chapter_label=chapter_label,
                    page_number=page_number,
                    provider_key=provider_key,
                )
            destinations = [job.destination for job in chapter_jobs.values()]
            if self._chapter_files_complete(destinations):
                skipped_chapters += 1
                self.emit(
                    "log",
                    message=f"第 {number_text} 章已完整下载，已跳过。",
                )
                continue
            chapter_folder.mkdir(parents=True, exist_ok=True)
            if chapter_jobs:
                grouped_chapters.append(chapter_jobs)

        jobs: list[PageJob] = []
        seen: set[str] = set()
        for chapter_jobs in grouped_chapters:
            for key, job in chapter_jobs.items():
                if key in seen:
                    continue
                seen.add(key)
                jobs.append(job)
        return jobs, locked_count, skipped_chapters

    def _download_one(self, job: PageJob) -> tuple[str, str | None]:
        try:
            provider = self.providers.get(job.provider_key)
            if provider is None:
                return "failed", f"未知站点：{job.provider_key}"
            status = provider.download_file(
                job.image_url,
                job.destination,
                cancel_event=self.cancel_event,
                referer=job.referer,
            )
            return status, None
        except Exception as exc:
            return "failed", str(exc)

    def download(
        self,
        series: dict[str, Any],
        chapters: list[dict[str, Any]],
        output_root: Path,
        workers: int = 4,
        max_images: int = 0,
    ) -> None:
        """边取图边下载：图片地址一到就开工，不再等所有章节扫描完。

        生产者线程按顺序读章节图片列表，消费者线程池立刻下载；未付费、
        无图、已下完的章节照旧跳过。进度条的 maximum 会随扫描增长。
        """
        output_root.mkdir(parents=True, exist_ok=True)
        # 图床通常和站点分离，32 个并发还压得住，能明显拉满带宽。
        workers = max(1, min(int(workers), 32))
        self.max_images = max(0, int(max_images or 0))
        self.emit("log", message=f"开始下载：{series.get('title') or series.get('slug')}")
        provider_key = str(series.get("_provider") or "")
        provider = self.providers.get(provider_key)
        if provider is not None and chapters:
            try:
                native_result = provider.download_native(
                    series,
                    chapters,
                    output_root,
                    self.emit,
                    self.cancel_event,
                )
            except Exception as exc:
                native_result = None
                self.emit(
                    "log",
                    message=f"站点原生下载不可用，将回退逐页下载：{exc}",
                    level="warning",
                )
            if native_result is True:
                self.emit("progress", maximum=1, value=1, message="站点原生下载完成")
                self.emit(
                    "download_done",
                    request_id=self.request_id,
                    cancelled=False,
                    downloaded=1,
                    skipped=0,
                    failed=0,
                    locked=0,
                    skipped_chapters=0,
                    total=1,
                    cancelled_count=0,
                )
                return
            if native_result is False and self.cancel_event.is_set():
                self.emit(
                    "download_done",
                    request_id=self.request_id,
                    cancelled=True,
                    downloaded=0,
                    skipped=0,
                    failed=0,
                    locked=0,
                    skipped_chapters=0,
                    total=0,
                    cancelled_count=1,
                )
                return
        if provider is None:
            raise ApiError(f"未知站点：{provider_key}")

        series_name = sanitize_path_component(
            series.get("title") or series.get("slug"),
            "Unknown Series",
            90,
        )
        series_root = provider.output_root(output_root, series) / series_name
        # 清掉上次被强行结束（直接关进程）留下的 .part；正常完成/失败都会自己清理。
        try:
            for stale in series_root.rglob("*.part"):
                try:
                    stale.unlink()
                except OSError:
                    pass
        except OSError:
            pass

        pending: "queue.Queue[PageJob | None]" = queue.Queue(
            maxsize=max(96, workers * 12)
        )
        stats = {"total": 0, "locked": 0, "skipped_chapters": 0}

        # 逐章状态：下载页要能看出"哪一章没下全"，所以每一章都留一条记录。
        ordered_chapters = sorted(chapters, key=chapter_sort_key)
        chapter_states: dict[int, dict[str, Any]] = {}
        for index, chapter in enumerate(ordered_chapters):
            number_text = chapter_number_text(chapter)
            chapter_states[index] = {
                "index": index,
                "label": f"第 {number_text} 章",
                "title": str(chapter.get("title") or ""),
                "state": "pending",   # pending|fetching|queued|running|done|partial|failed|skipped|locked|empty|cancelled
                "pages": 0,
                "downloaded": 0,
                "skipped": 0,
                "failed": 0,
            }
        chapter_lock = threading.Lock()

        def chapter_snapshot() -> list[dict[str, Any]]:
            with chapter_lock:
                return [dict(item) for item in chapter_states.values()]

        def chapter_update(index: int, **changes: Any) -> None:
            with chapter_lock:
                item = chapter_states.get(index)
                if item is None:
                    return
                item.update(changes)

        def chapter_bump(index: int, status: str) -> None:
            """累加一张图的结果，并按完成情况推进章节状态。"""
            with chapter_lock:
                item = chapter_states.get(index)
                if item is None:
                    return
                if status == "downloaded":
                    item["downloaded"] += 1
                elif status == "skipped":
                    item["skipped"] += 1
                elif status == "cancelled":
                    pass
                else:
                    item["failed"] += 1
                finished = item["downloaded"] + item["skipped"] + item["failed"]
                if item["pages"] and finished >= item["pages"]:
                    if item["failed"] and item["downloaded"] == 0 and item["skipped"] == 0:
                        item["state"] = "failed"
                    elif item["failed"]:
                        item["state"] = "partial"
                    elif item["downloaded"] == 0 and item["skipped"]:
                        item["state"] = "skipped"
                    else:
                        item["state"] = "done"
                elif not item["pages"] and item["state"] in ("pending",):
                    item["state"] = "fetching"

        def put_job(job: PageJob) -> bool:
            while not self.cancel_event.is_set():
                try:
                    pending.put(job, timeout=0.4)
                    return True
                except queue.Full:
                    continue
            return False

        def produce() -> None:
            try:
                for chapter_index, chapter in enumerate(ordered_chapters):
                    if self.cancel_event.is_set():
                        break
                    number_text = chapter_number_text(chapter)
                    if self.is_locked(chapter):
                        stats["locked"] += 1
                        price = chapter.get("price")
                        suffix = f"，需要 {price} 金币" if price else ""
                        chapter_update(chapter_index, state="locked")
                        self.emit(
                            "log",
                            message=f"跳过付费章节 {number_text}{suffix}。",
                            level="warning",
                        )
                        self.emit("chapters", items=chapter_snapshot())
                        continue

                    chapter_update(chapter_index, state="fetching")
                    self.emit("chapters", items=chapter_snapshot())
                    self.emit(
                        "download_status",
                        message=f"正在获取第 {number_text} 章的图片列表...",
                    )
                    referer = provider.chapter_referer(series, chapter)
                    try:
                        records = self._fetch_page_records(provider, series, chapter)
                    except ApiError as exc:
                        self.emit(
                            "log",
                            message=f"第 {number_text} 章图片列表获取失败：{exc}",
                            level="error",
                        )
                        continue
                    finally:
                        try:
                            removed_bytes = provider.cleanup_chapter_cache()
                        except Exception as exc:
                            self.emit(
                                "log",
                                message=f"第 {number_text} 章临时缓存清理失败：{exc}",
                                level="warning",
                            )
                        else:
                            if removed_bytes > 0:
                                self.emit(
                                    "log",
                                    message=(
                                        f"第 {number_text} 章浏览器临时缓存已清理："
                                        f"{format_bytes(removed_bytes)}"
                                    ),
                                )

                    if not records:
                        chapter_update(chapter_index, state="empty")
                        self.emit("chapters", items=chapter_snapshot())
                        self.emit(
                            "log",
                            message=f"第 {number_text} 章没有可用图片，已跳过。",
                            level="warning",
                        )
                        continue

                    title = sanitize_path_component(chapter.get("title"), "", 60)
                    folder_name = f"Chapter {format_chapter_number(chapter)}"
                    if title:
                        folder_name = f"{folder_name} - {title}"
                    chapter_folder = series_root / sanitize_path_component(
                        folder_name,
                        f"Chapter {format_chapter_number(chapter)}",
                        130,
                    )

                    chapter_jobs: dict[str, PageJob] = {}
                    chapter_label = f"第 {number_text} 章"
                    for page_number, image_url in records:
                        extension = self._image_extension(image_url)
                        destination = chapter_folder / f"{page_number:04d}{extension}"
                        key = str(destination).casefold()
                        if key in chapter_jobs:
                            continue
                        chapter_jobs[key] = PageJob(
                            destination=destination,
                            image_url=image_url,
                            referer=referer,
                            chapter_label=chapter_label,
                            page_number=page_number,
                            provider_key=provider_key,
                            chapter_index=chapter_index,
                            chapter_title=str(chapter.get("title") or ""),
                        )
                    if self._chapter_files_complete(
                        [job.destination for job in chapter_jobs.values()]
                    ):
                        stats["skipped_chapters"] += 1
                        chapter_update(
                            chapter_index,
                            state="skipped",
                            pages=len(chapter_jobs),
                            skipped=len(chapter_jobs),
                        )
                        self.emit("chapters", items=chapter_snapshot())
                        self.emit(
                            "log",
                            message=f"第 {number_text} 章已完整下载，已跳过。",
                        )
                        continue

                    chapter_folder.mkdir(parents=True, exist_ok=True)
                    stats["total"] += len(chapter_jobs)
                    if os.environ.get("DUMBCAT_DL_DEBUG"):
                        print(f"[dl] chapter {chapter_index} queued jobs={len(chapter_jobs)} total={stats['total']}", file=sys.stderr, flush=True)
                    chapter_update(
                        chapter_index,
                        state="queued",
                        pages=len(chapter_jobs),
                    )
                    self.emit("chapters", items=chapter_snapshot())
                    for job in chapter_jobs.values():
                        if not put_job(job):
                            break
            except BaseException as exc:  # noqa: BLE001
                # 生产者一旦出错必须让人看见，否则任务会"莫名其妙完成 0 张"。
                error_text = f"{type(exc).__name__}: {exc}"
                self.emit(
                    "log",
                    message=f"取图线程出错，本次下载已中止：{error_text}",
                    level="error",
                )
                with chapter_lock:
                    for item in chapter_states.values():
                        if item["state"] in {"pending", "fetching", "queued", "running"}:
                            item["state"] = "failed"
                            item["error"] = error_text
                self.emit("chapters", items=chapter_snapshot())
            finally:
                while True:
                    try:
                        pending.put(None, timeout=0.5)
                        break
                    except queue.Full:
                        if self.cancel_event.is_set():
                            break

        threading.Thread(
            target=produce,
            name="download-prepare",
            daemon=True,
        ).start()

        downloaded = 0
        skipped = 0
        failed = 0
        cancelled = 0
        completed = 0
        error_samples: list[str] = []
        pending_done = False
        stopped_early = False
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="image-download",
        )
        futures: dict[concurrent.futures.Future[tuple[str, str | None]], PageJob] = {}
        max_pending = workers * 2
        last_chapters_emit = time.time()
        self.emit("progress", maximum=1, value=0, message="边取图边下载中…")
        try:
            while True:
                while (
                    not pending_done
                    and not self.cancel_event.is_set()
                    and len(futures) < max_pending
                ):
                    try:
                        item = pending.get(timeout=0.2)
                    except queue.Empty:
                        break
                    if item is None:
                        pending_done = True
                        break
                    try:
                        fut = executor.submit(self._download_one, item)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[dl] submit failed: {exc!r}", file=sys.stderr, flush=True)
                        raise
                    futures[fut] = item
                    if os.environ.get("DUMBCAT_DL_DEBUG"):
                        print(f"[dl] submitted {item.chapter_label} p{item.page_number}", file=sys.stderr, flush=True)

                if os.environ.get("DUMBCAT_DL_DEBUG"):
                    print(f"[dl] iter futures={len(futures)} done={pending_done} qsize={pending.qsize()}", file=sys.stderr, flush=True)
                if self.cancel_event.is_set():
                    stopped_early = True
                    for future in list(futures):
                        if future.cancel():
                            futures.pop(future, None)
                    break
                if pending_done and not futures:
                    if os.environ.get("DUMBCAT_DL_DEBUG"):
                        print(f"[dl] loop end total={stats['total']} completed={completed} dl={downloaded} skip={skipped} fail={failed} cancelled={cancelled} cancel_ev={self.cancel_event.is_set()}", file=sys.stderr, flush=True)
                    break
                if not futures:
                    continue

                done, _pending = concurrent.futures.wait(
                    futures,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                    timeout=0.25,
                )
                if not done:
                    continue

                for future in done:
                    job = futures.pop(future, None)
                    if job is None:
                        continue
                    completed += 1
                    try:
                        status, error = future.result()
                    except concurrent.futures.CancelledError:
                        status, error = "cancelled", None
                    except Exception as exc:
                        status, error = "failed", str(exc)
                    if status == "downloaded":
                        downloaded += 1
                    elif status == "skipped":
                        skipped += 1
                    elif status == "cancelled":
                        cancelled += 1
                    else:
                        failed += 1
                        if len(error_samples) < 30:
                            error_samples.append(
                                f"{job.chapter_label} 第 {job.page_number} 页：{error}"
                            )
                    chapter_bump(job.chapter_index, status)
                    if os.environ.get("DUMBCAT_DL_DEBUG"):
                        print(f"[dl] job {job.chapter_label} p{job.page_number} -> {status} {error or ''}", file=sys.stderr, flush=True)

                # 章节是边取边排队的，total 会一路涨；这里只要保证进度条不越界。
                maximum = max(1, stats["total"], completed + len(futures))
                self.emit(
                    "progress",
                    maximum=maximum,
                    value=completed,
                    message=(
                        f"{completed}/{stats['total']} · 新下载 {downloaded} · "
                        f"已存在 {skipped} · 失败 {failed}"
                    ),
                )
                now = time.time()
                if now - last_chapters_emit >= 0.5:
                    last_chapters_emit = now
                    self.emit("chapters", items=chapter_snapshot())
        finally:
            if self.cancel_event.is_set():
                for future in futures:
                    future.cancel()
            executor.shutdown(wait=True, cancel_futures=self.cancel_event.is_set())

        total = stats["total"]
        locked_count = stats["locked"]
        skipped_chapters = stats["skipped_chapters"]
        if stopped_early:
            cancelled = max(cancelled, total - completed)

        runtime_cache_removed = 0
        for provider in self.providers.values():
            try:
                runtime_cache_removed += provider.cleanup_runtime_cache()
            except Exception as exc:
                self.emit(
                    "log",
                    message=f"下载临时缓存清理失败：{exc}",
                    level="warning",
                )
        if runtime_cache_removed > 0:
            self.emit(
                "log",
                message=f"本次下载临时缓存已清理：{format_bytes(runtime_cache_removed)}",
            )

        for sample in error_samples:
            self.emit("log", message=f"下载失败：{sample}", level="error")
        if len(error_samples) == 30 and failed > 30:
            self.emit("log", message=f"其余 {failed - 30} 个失败项已省略。", level="error")

        # 提前停止时，没轮到的章节留个 cancelled，下载页才能解释"为什么这一章没有"。
        with chapter_lock:
            for item in chapter_states.values():
                if item["state"] in ("pending", "fetching", "queued", "running"):
                    if self.cancel_event.is_set():
                        item["state"] = "cancelled"

        self.emit(
            "download_done",
            request_id=self.request_id,
            cancelled=self.cancel_event.is_set(),
            downloaded=downloaded,
            skipped=skipped,
            failed=failed,
            locked=locked_count,
            skipped_chapters=skipped_chapters,
            total=total,
            cancelled_count=cancelled,
            chapters=chapter_snapshot(),
        )

def _run_comix_smoke(output_path: str, query: str = "Adulterous Boss") -> int:
    # 延迟导入：providers 反过来依赖 loader 的 PageJob 之外的东西，
    # 顶层导入会形成循环。
    from dumbcat.sources.providers import ComixProvider

    report: dict[str, Any] = {"ok": False}
    provider = ComixProvider()
    try:
        results = provider.search_series(
            query,
            cancel_event=threading.Event(),
        )
        report["search_count"] = len(results)
        if not results:
            raise RuntimeError("Comix 搜索没有返回结果")
        report["search_titles"] = [
            str(item.get("title") or "") for item in results[:10]
        ]
        normalized_query = query.strip().casefold()
        series = next(
            (
                item
                for item in results
                if str(item.get("title") or "").strip().casefold() == normalized_query
            ),
            results[0],
        )
        report["series_title"] = series.get("title")
        chapters = provider.get_chapters(series, threading.Event())
        report["chapter_count"] = len(chapters)
        free_chapter = next(
            (chapter for chapter in chapters if not provider.is_locked(chapter)),
            None,
        )
        if free_chapter is None:
            raise RuntimeError("Comix 没有可用免费章节")
        report["chapter_number"] = chapter_number_text(free_chapter)
        pages = provider.get_chapter_pages(series, free_chapter, threading.Event())
        report["page_count"] = len(pages)
        if not pages:
            raise RuntimeError("Comix 免费章节没有读取到图片")
        image_url = str(pages[0].get("image_url") or pages[0].get("url") or "")
        if not image_url:
            raise RuntimeError("Comix 图片地址为空")
        destination = Path(tempfile.mkdtemp(prefix="manga-comix-smoke-")) / "001.jpg"
        status = provider.download_file(
            image_url,
            destination,
            referer=provider.chapter_referer(series, free_chapter),
            cancel_event=threading.Event(),
        )
        downloaded_bytes = destination.stat().st_size
        report["download_status"] = status
        report["downloaded_bytes"] = downloaded_bytes
        report["ok"] = status == "downloaded" and downloaded_bytes > 0
        destination.unlink(missing_ok=True)
        destination.parent.rmdir()
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        provider.close()

    target = Path(output_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0 if report.get("ok") else 1


# Comix 抓取自检：不依赖界面，供维护时手动核对站点改版。
# 用法：python manga_core.py --comix-smoke <输出目录> [关键词]
if __name__ == "__main__":
    if len(sys.argv) in {3, 4} and sys.argv[1] == "--comix-smoke":
        query = sys.argv[3] if len(sys.argv) == 4 else "Adulterous Boss"
        raise SystemExit(_run_comix_smoke(sys.argv[2], query))
    raise SystemExit(
        "用法：python manga_core.py --comix-smoke <输出目录> [关键词]"
    )
