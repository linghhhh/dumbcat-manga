"""把各图源千奇百怪的数据归一化成前端只认的一种形状。

这一层是「抗变化」的关键：新图源字段叫 ``cover`` 还是 ``poster`` 都不影响界面，
脏 HTML 里的空白、重复标签、相对地址也在这里清掉。
"""
from __future__ import annotations

import hashlib
import html
import re
import urllib.parse
from typing import Any, Iterable

_WS = re.compile(r"[\s\u3000]+")
_TAG_SPLIT = re.compile(r"[,，/|、;；]+")


_HTML_TAG = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")


def clean_text(value: Any, limit: int = 400) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    # 简介里常带 <p>/<br> 之类的标签，直接显示会露出尖括号
    text = _HTML_TAG.sub(" ", text)
    text = _WS.sub(" ", text).strip()
    return text[:limit]


def first_str(raw: dict[str, Any], *names: str) -> str:
    for name in names:
        value = raw.get(name)
        if isinstance(value, dict):
            value = value.get("url") or value.get("src") or value.get("name")
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        text = clean_text(value)
        if text:
            return text
    return ""


def absolute(url: str, base: str) -> str:
    text = clean_text(url, 2000)
    if not text:
        return ""
    if text.startswith("//"):
        return f"https:{text}"
    if text.startswith(("http://", "https://", "data:")):
        return text
    if not base:
        return text
    return urllib.parse.urljoin(base.rstrip("/") + "/", text)


def parse_tags(value: Any, limit: int = 16) -> list[str]:
    """标签可能是列表、列表套字典、逗号串——统统变成去重后的短列表。"""
    tags: list[str] = []
    if isinstance(value, str):
        candidates: Iterable[Any] = _TAG_SPLIT.split(value)
    elif isinstance(value, (list, tuple, set)):
        candidates = value
    else:
        candidates = []
    for item in candidates:
        if isinstance(item, dict):
            item = item.get("name") or item.get("title") or item.get("label") or ""
        text = clean_text(item, 40)
        if text and text not in tags:
            tags.append(text)
        if len(tags) >= limit:
            break
    return tags


def identity(raw: dict[str, Any]) -> str:
    """作品在当前图源内的稳定标识：优先 slug，其次详情地址，最后标题。"""
    for name in ("slug", "id", "series_id", "seriesId", "book_id", "bookId", "comic_id"):
        value = raw.get(name)
        if value not in (None, "", 0, "0"):
            return f"{name}:{value}"
    url = first_str(raw, "url", "link", "detail_url", "detailUrl")
    if url:
        return f"url:{url.split('#')[0]}"
    return f"title:{first_str(raw, 'title', 'name')}"


def series_uid(provider_key: str, raw: dict[str, Any]) -> str:
    digest = hashlib.sha1(identity(raw).encode("utf-8")).hexdigest()[:18]
    return f"{provider_key}.{digest}"


def chapter_uid(series_uid_value: str, raw: dict[str, Any]) -> str:
    key = "|".join(
        [
            first_str(raw, "url", "link"),
            first_str(raw, "slug", "id", "chapter_id", "chapterId"),
            first_str(raw, "title", "name", "chapter_name"),
        ]
    )
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return f"{series_uid_value}.{digest}"


def normalize_series(
    provider_key: str,
    provider_name: str,
    raw: dict[str, Any],
    *,
    base_url: str = "",
    nsfw: bool = False,
    uid: str = "",
) -> dict[str, Any]:
    """转成前端用的作品卡片。``coverRemote`` 是原图地址，``coverUrl`` 走后端代理。"""
    public_uid = uid or series_uid(provider_key, raw)
    cover = absolute(
        first_str(raw, "poster", "cover", "cover_url", "coverUrl", "thumbnail",
                 "thumbnailUrl", "image", "image_url", "thumb"),
        base_url,
    )
    tags = parse_tags(raw.get("tags") or raw.get("genres") or raw.get("tag")
                      or raw.get("categories"))
    title = clean_text(raw.get("title") or raw.get("name") or raw.get("book_name"), 200)
    author = first_str(raw, "author", "artist", "authors", "author_name")
    status = first_str(raw, "status", "serial_status", "publishStatus")
    summary = clean_text(raw.get("summary") or raw.get("description")
                         or raw.get("intro") or raw.get("desc"), 800)
    latest = first_str(raw, "latest", "latest_chapter", "last_chapter",
                       "lastChapter", "newest")
    return {
        "uid": public_uid,
        "provider": provider_key,
        "providerName": provider_name,
        "title": title or "未命名作品",
        "url": absolute(first_str(raw, "url", "link", "detail_url", "detailUrl"), base_url),
        "coverUrl": f"/img/cover/{public_uid}" if cover else "",
        "coverRemote": cover,
        "author": author,
        "status": status,
        "tags": tags,
        "summary": summary,
        "latest": latest,
        "nsfw": bool(nsfw),
        "locked": bool(raw.get("is_locked") or raw.get("locked")),
    }


def normalize_chapter(
    series_uid_value: str,
    provider_key: str,
    provider_name: str,
    raw: dict[str, Any],
    *,
    index: int = 0,
    base_url: str = "",
) -> dict[str, Any]:
    title = clean_text(
        raw.get("title") or raw.get("name") or raw.get("chapter_name")
        or raw.get("chapterName") or "",
        200,
    )
    number = first_str(raw, "number", "chapter_number", "chapterNumber", "index")
    if not title:
        title = f"第 {number or index + 1} 话"
    locked = bool(
        raw.get("is_locked") or raw.get("locked") or raw.get("is_premium")
        or raw.get("requiresPurchase") is True
        or raw.get("isFree") is False
    )
    if not locked:
        try:
            locked = float(raw.get("price") or 0) > 0
        except (TypeError, ValueError):
            locked = False
    return {
        "uid": chapter_uid(series_uid_value, raw),
        "seriesUid": series_uid_value,
        "provider": provider_key,
        "providerName": provider_name,
        "title": title,
        "number": number,
        "url": absolute(first_str(raw, "url", "link", "chapter_url", "chapterUrl"), base_url),
        "index": index,
        "locked": locked,
    }


def normalize_page(index: int, raw: Any, base_url: str = "") -> dict[str, Any]:
    if isinstance(raw, str):
        url = absolute(raw, base_url)
        return {"index": index, "url": url, "remote": url}
    if isinstance(raw, dict):
        url = absolute(
            first_str(raw, "url", "image_url", "imageUrl", "src", "image", "path"),
            base_url,
        )
        return {
            "index": index,
            "url": url,
            "remote": url,
            "width": raw.get("width") or raw.get("w") or 0,
            "height": raw.get("height") or raw.get("h") or 0,
        }
    return {"index": index, "url": "", "remote": ""}


def normalize_page_list(pages: Any, base_url: str = "") -> list[dict[str, Any]]:
    if isinstance(pages, dict):
        pages = pages.get("images") or pages.get("pages") or pages.get("list") or []
    if not isinstance(pages, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(pages):
        page = normalize_page(index, item, base_url)
        if page["url"]:
            result.append(page)
    return result


def series_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    """没有明确排序时的稳定顺序：有封面的在前，然后按标题。"""
    return (0 if item.get("coverUrl") else 1, item.get("title") or "")
