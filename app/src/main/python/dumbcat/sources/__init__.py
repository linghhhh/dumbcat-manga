"""漫源注册表与类型导出。

对界面层暴露的最小接口是 `build_providers()`（返回 key → provider 的字典）与
`SeriesProvider`（用于 `is_locked` 判定）；provider 的分页 / 分类能力见
`dumbcat.sources.base`。
"""
from __future__ import annotations

from dumbcat.sources.api import ApiClient, JmApiClient
from dumbcat.sources.base import (
    NATIVE_COMPLETE_SUFFIX,
    SITE_BASE_URLS,
    Category,
    PageResult,
    SeriesProvider,
)
from dumbcat.sources.providers import (
    COMIX_CHAPTER_RE,
    GENERIC_CHAPTER_RE,
    MANGA18FX_CHAPTER_RE,
    BrowserSiteProvider,
    ComixProvider,
    CopyComicProvider,
    JmProvider,
    Manga18fxProvider,
    QiMangaProvider,
    build_providers,
)

__all__ = [
    "ApiClient",
    "JmApiClient",
    "NATIVE_COMPLETE_SUFFIX",
    "SITE_BASE_URLS",
    "Category",
    "PageResult",
    "SeriesProvider",
    "COMIX_CHAPTER_RE",
    "GENERIC_CHAPTER_RE",
    "MANGA18FX_CHAPTER_RE",
    "BrowserSiteProvider",
    "ComixProvider",
    "CopyComicProvider",
    "JmProvider",
    "Manga18fxProvider",
    "QiMangaProvider",
    "build_providers",
]
