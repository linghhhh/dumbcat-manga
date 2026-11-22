"""站点通用页面控制器（浏览器型漫源：绅士漫画 / 漫画柜 / 包子漫画 / 哔哩哔哩 / 快看）。

统一走 `dumbcat.browser.hub` 的共享 Chromium：每个站点一个独立 page（Cookie 互相
隔离），整个程序只有一个进程、桌面上最多一个窗口。普通抓取（搜索 / 分类 / 分页 /
章节 / 取图）全部在 hidden 模式下完成，不弹窗；只有用户主动「打开网站」登录或
需要站点原生阅读页时才把共享窗口显示出来，并用 `show_window()` 复用同一个窗口。
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

from dumbcat.browser.hub import BrowserHubError, hub


class SiteBrowserError(RuntimeError):
    pass


class SiteBrowserClient:
    """A persistent browser session (per site page) for sites that need a real page."""

    def __init__(
        self,
        site_key: str,
        site_base: str,
        favorite_path: str = "",
        search_path: str = "",
        *,
        sort: str = "",
        page_param: str = "page",
    ) -> None:
        self.site_key = site_key
        self.site_base = site_base.rstrip("/")
        self.favorite_path = favorite_path
        self.search_path = search_path
        #: 列表页排序参数（部分站点需要显式排序才输出完整列表）
        self.sort = sort
        #: 列表页分页用的查询参数名（绅士漫画是 page，其它站点多为 page）
        self.page_param = page_param
        self._lock = threading.RLock()
        self._closed = False
        self._window_open = False
        #: 分类导航基本不变：抓一次就够，别每次换分类都重新导航一遍
        self._categories_cache: list[dict[str, Any]] | None = None
        #: 轻量抓取模式（屏蔽图片/媒体/字体）当前是否生效
        self._light_mode = False

    # ---------- 页面获取（全部经 hub 线程派发） ----------
    @property
    def _page(self) -> Any:
        try:
            page = hub().page_for(f"site:{self.site_key}")
        except BrowserHubError as exc:
            raise SiteBrowserError(str(exc)) from exc
        self._apply_light_mode(page)
        return page

    def _execute(self, callback: Any, *args: Any) -> Any:
        """把回调派发到 hub 的唯一工作线程上执行。

        Playwright 同步 API 只能在其创建线程里使用，因此这里**不能**再各自
        起线程池：全部漫源共用 `hub().run()`，页面的创建与使用始终在同一个
        线程上，切换漫源才不会抛 `greenlet.error`。
        """
        with self._lock:
            # 共享 Chromium 一直在：切回一个用过的漫源时自动复活（page 由 hub
            # 按需重建），不需要用户重新打开程序。
            self._closed = False
        try:
            return hub().run(callback, *args)
        except SiteBrowserError:
            raise
        except BrowserHubError as exc:
            raise SiteBrowserError(str(exc)) from exc
        except Exception as exc:
            raise SiteBrowserError(str(exc)) from exc

    @staticmethod
    def _absolutize(value: str, base: str) -> str:
        return value if value.startswith(("http://", "https://")) else urllib.parse.urljoin(base, value)

    # ---------- 轻量模式：抓数据时不要图片 / 媒体 / 字体 ----------
    #: 列表页最重的是几十张封面图加字体文件；抓数据只关心 DOM，全部拦掉。
    #: 用户点「打开网站」登录时要恢复成正常页面，所以做成可开关的。
    BLOCKED_RESOURCE_TYPES = ("image", "media", "font")

    def _route_handler(self, route: Any) -> None:
        try:
            if route.request.resource_type in self.BLOCKED_RESOURCE_TYPES:
                route.abort()
            else:
                route.continue_()
        except Exception:  # noqa: BLE001 - 路由回调里出错不能影响导航
            try:
                route.continue_()
            except Exception:  # noqa: BLE001
                pass

    def _apply_light_mode(self, page: Any) -> None:
        """抓取用：拦掉图片/媒体/字体，页面体积和加载时间都大幅下降。"""
        if self._light_mode:
            return
        try:
            page.route("**/*", self._route_handler)
            self._light_mode = True
        except Exception:  # noqa: BLE001 - 装不上就退回普通抓取
            self._light_mode = False

    def _apply_full_mode(self, page: Any) -> None:
        """给用户看时：恢复正常加载，登录页 / 站点原生界面不能缺图缺样式。"""
        if not self._light_mode:
            return
        try:
            page.unroute("**/*", self._route_handler)
        except Exception:  # noqa: BLE001
            try:
                page.unroute("**/*")
            except Exception:  # noqa: BLE001
                pass
        self._light_mode = False

    # ---------- 条件等待（替代固定 sleep） ----------
    def _wait_for_rows(
        self,
        page: Any,
        expression: str,
        *,
        timeout: float = 12.0,
        scroll: bool = True,
    ) -> list[dict[str, Any]]:
        """等抽取脚本真的拿到条目再返回，而不是先睡固定毫秒。

        旧实现每读一页固定睡 900ms，再「滚到底 + 500ms」两次 ≈ 1.9 秒纯等待，
        占了单页耗时的一大半，慢站点上照样读到空列表。现在改成：先轮询到有
        内容，再滚一遍触发懒加载，最后等条目数稳定。
        """
        rows: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self._evaluate_rows(page, expression)
            if rows:
                break
            page.wait_for_timeout(120)
        if not rows and scroll:
            # 懒加载站点首屏可能确实没有卡片：滚到底再等一轮
            try:
                page.evaluate(
                    "window.scrollTo(0, document.documentElement.scrollHeight)"
                )
            except Exception:  # noqa: BLE001
                pass
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                rows = self._evaluate_rows(page, expression)
                if rows:
                    break
                page.wait_for_timeout(150)
        if scroll:
            rows = self._settle_rows(page, expression, rows)
            try:
                page.evaluate("window.scrollTo(0, 0);")
            except Exception:  # noqa: BLE001
                pass
        return rows

    @staticmethod
    def _evaluate_rows(page: Any, expression: str) -> list[dict[str, Any]]:
        try:
            raw = page.evaluate(expression)
        except Exception:  # noqa: BLE001 - 页面还在跳转时 evaluate 会失败
            return []
        return (
            [item for item in raw if isinstance(item, dict)]
            if isinstance(raw, list)
            else []
        )

    def _settle_rows(
        self,
        page: Any,
        expression: str,
        rows: list[dict[str, Any]],
        *,
        timeout: float = 4.0,
        quiet_samples: int = 2,
    ) -> list[dict[str, Any]]:
        """滚到底触发懒加载，然后等条目数不再增长（连续两次一样就收工）。"""
        try:
            page.evaluate(
                "window.scrollTo(0, document.documentElement.scrollHeight)"
            )
        except Exception:  # noqa: BLE001
            return rows
        best = rows
        stable = 0
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            page.wait_for_timeout(120)
            current = self._evaluate_rows(page, expression)
            if len(current) > len(best):
                best = current
                stable = 0
                continue
            stable += 1
            if stable >= quiet_samples:
                break
        return best

    # ---------- 提取脚本 ----------
    # 说明：旧实现只取"卡片里第一张图"，实测经常抓到站点 logo / 1x1 占位 / 页头
    # banner，阅读页也会混进广告图，用户看到的就是"乱七八糟的图"。现在统一按
    # 「已加载尺寸 + 关键词」打分挑最优，并在阅读页优先限定在章节内容容器内。
    SEARCH_EXTRACT = r"""(() => {
        const seen = new Map();
        const skip = /^(javascript:|#|mailto:)/i;
        const isImage = (value) => !!value && !/^data:/i.test(value);

        // 封面不能只找 <a> 的直接子节点：实测绅士漫画/快看的封面在兄弟节点
        // 或卡片容器里。改为“从锚点向外找容器，容器内挑最像封面的一张图”。
        const pickPoster = (anchor) => {
            // 取图片真实地址。顺序很重要：懒加载站点把 src 写成 data: 占位图，
            // 真实地址在 data-src / data-original 等属性里；先看 src 就永远拿到占位图。
            const real = (image) => [
                image.currentSrc,
                image.getAttribute('data-src'),
                image.getAttribute('data-original'),
                image.getAttribute('data-lazy-src'),
                image.getAttribute('data-echo'),
                image.getAttribute('data-url'),
                image.getAttribute('src'),
            ].find(isImage);

            // 卡片里常混有 1x1 占位 gif、站点 logo/图标、页头 banner，因此不能
            // 简单取第一张图，要在容器内评估所有图片并挑最优。
            // 注意：Chromium 对“尚未加载的图”会报 naturalWidth=16，所以尺寸
            // 过滤只能作用于“确实加载成功”的图片；懒加载地址不能用尺寸判断。
            const bestIn = (scope) => {
                let best = null;
                let fallback = null;
                for (const image of scope.querySelectorAll('img')) {
                    const loaded = image.currentSrc
                        || (image.complete && image.naturalWidth > 0 ? image.getAttribute('src') : '');
                    const isLoaded = !!loaded && isImage(loaded);
                    const lazy = isLoaded ? '' : real(image);
                    const width = image.naturalWidth || 0;
                    const height = image.naturalHeight || 0;
                    const area = width * height;
                    let score = 0;
                    if (isLoaded) {
                        if (area > 4000000) continue;          // 页头大横幅
                        if (area < 4500) continue;             // 1x1 占位/小图标
                        score = area * 2;
                    } else if (lazy) {
                        score = 1;                             // 懒加载：尺寸未知，靠后
                    } else {
                        continue;
                    }
                    if (/sprite|logo|icon|avatar|placeholder/i.test(loaded || lazy)) {
                        score -= 50000;
                    }
                    const entry = { value: isLoaded ? loaded : lazy, score: score, alt: image.alt || '' };
                    // 同一容器里优先用已加载的真实封面，其次才考虑懒加载地址。
                    if (isLoaded) {
                        if (!best || score > best.score) best = entry;
                    } else if (!fallback) {
                        fallback = entry;
                    }
                }
                return best || fallback;
            };

            let scope = anchor;
            for (let depth = 0; depth < 5 && scope; depth += 1) {
                const tag = (scope.tagName || '').toLowerCase();
                // 不要把 body/html 当容器：否则页面顶部 banner 会被当成卡片封面。
                if (tag === 'body' || tag === 'html') break;
                const best = bestIn(scope);
                if (best) {
                    return { poster: best.value || '', alt: best.alt, scope: scope };
                }
                const withBg = scope.querySelector('[style*="background-image"]');
                if (withBg) {
                    const match = (withBg.getAttribute('style') || '')
                        .match(/url\(\s*['"]?([^'")]+)['"]?\s*\)/i);
                    if (match && isImage(match[1])) {
                        return { poster: match[1], alt: '', scope: scope };
                    }
                }
                scope = scope.parentElement;
            }
            return { poster: '', alt: '', scope: anchor };
        };

        // 标题抽取。优先使用**语义节点**（h1-h4 / [class*=title|name]）的文本，
        // 因为整体锚文本常常是"标题+副标题+分类+更新信息"拼在一起。
        // 实测哔哩哔哩卡片：<a class="big-card"> 里标题是 <h3>重生复仇排队斩杀</h3>，
        // 副标题是 <p>…</p>，而 a.textContent 把两者与标签信息全拼了起来。
        const PLACEHOLDER = /^(cover|image|img|封面|图片|thumb|thumbnail|banner|logo|blank|\d+)$/i;
        const CHAPTERISH = /(第\s*\d|\d+\s*[话話章回卷]|更新至|更新到|小时前|分鐘前|分钟前|天前|已完结|连载中|最新)/;
        const HEADING_TAGS = ['H1', 'H2', 'H3', 'H4'];
        const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
        const usableTitle = (value) =>
            value.length >= 2 && value.length <= 120 && !PLACEHOLDER.test(value);

        // 在给定容器内收集候选标题及其优先级
        const titleCandidatesIn = (scope) => {
            const out = [];
            for (const node of scope.querySelectorAll(
                'h1,h2,h3,h4,h5,[class*="title" i],[class*="name" i]'
            )) {
                const text = clean(node.textContent);
                if (usableTitle(text)) out.push({ text: text, tag: node.tagName });
            }
            const attrTitle = clean(scope.getAttribute('title'));
            if (usableTitle(attrTitle)) out.push({ text: attrTitle, tag: 'ATTR' });
            return out;
        };

        const scoreTitle = (candidate) => {
            if (!usableTitle(candidate.text)) return -1000;
            let score = 0;
            if (CHAPTERISH.test(candidate.text)) score -= 100;  // 章节/时间信息
            if (candidate.tag === 'ATTR') score += 20;           // 显式 title 属性最可信
            if (HEADING_TAGS.indexOf(candidate.tag) >= 0) score += 15;
            if (/title|name/i.test(candidate.tag)) score += 8;
            // 长度相近时偏向信息完整的短标题
            score += Math.max(0, 40 - candidate.text.length) / 10;
            return score;
        };

        const titleFor = (anchor, alt, scope) => {
            const body = clean(anchor.textContent || anchor.getAttribute('title'));
            const candidates = [];
            if (body.length <= 140) candidates.push({ text: body, tag: 'ANCHOR' });
            for (const extra of titleCandidatesIn(scope)) candidates.push(extra);
            if (usableTitle(clean(alt)) && (!body || CHAPTERISH.test(body))) {
                candidates.push({ text: clean(alt), tag: 'ALT' });
            }
            let best = '';
            let bestScore = -Infinity;
            for (const candidate of candidates) {
                const score = scoreTitle(candidate);
                if (score > bestScore) {
                    bestScore = score;
                    best = candidate.text;
                }
            }
            if (bestScore <= -1000) {
                if (usableTitle(clean(alt))) return clean(alt);
                return body;
            }
            return best || body;
        };

        for (const anchor of document.querySelectorAll('a[href]')) {
            const href = anchor.href || '';
            const text = (anchor.textContent || anchor.getAttribute('title') || '').trim();
            if (!href || skip.test(href) || text.length < 2 || text.length > 180) continue;
            // 作品详情页的路径首段。实测：绅士/漫画柜/包子 用 album|comic，
            // 哔哩哔哩用 detail，快看用 web|webs。此前只列了 album|manga 等，
            // 导致 bilibili/快看 被整体过滤掉（条目为 0）。
            if (!/\/(manga|manhua|comic|comics|album|book|photo|gallery|view|read|works?|detail|series|title|subject|web|webs|photos-index|photos-slide)[\/\-_?]/i.test(href)) continue;
            const found = pickPoster(anchor);
            const title = titleFor(anchor, found.alt, found.scope);
            // 放宽 URL 正则后会混进站点的功能页（原创投稿/我的钱包/会员中心/
            // IP合作/自动购买管理…）。这些不是作品，按标题关键词排除。
            // 实测快看还会出现"自动购买管理""我的钱包"等账号页。
            if (/投稿|钱包|会员|签到|客服|合作|协议|隐私|关于|登录|注册|下载\s*APP|开放平台|历史\/关注|排行榜|分类|首页|管理|设置|消息|通知|帮助|反馈/.test(title)) continue;
            const row = {
                href,
                title,
                poster: found.poster,
            };
            const existing = seen.get(href);
            // 同一作品可能出现多次：保留拿得到封面的那一条。
            if (!existing) {
                seen.set(href, row);
            } else if (!existing.poster && row.poster) {
                seen.set(href, row);
            }
        }
        return [...seen.values()].slice(0, 120);
    })()"""
    CHAPTER_EXTRACT = r"""(() => {
        const seen = new Set();
        const rows = [];
        for (const anchor of document.querySelectorAll('a[href]')) {
            const href = anchor.href || '';
            const text = (anchor.textContent || anchor.getAttribute('title') || '').trim();
            if (!href || text.length < 1 || text.length > 180) continue;
            if (!/\/(chapter|chap|photo|read|view|comic|album|photos-slide)[\/\-_?]/i.test(href)) continue;
            if (seen.has(href)) continue;
            seen.add(href);
            rows.push({href, label: text.replace(/\s+/g, ' ')});
        }
        return rows.slice(0, 1000);
    })()"""
    # 阅读页取图：优先限定在章节内容容器内，并按尺寸/关键词打分，剔除广告位、
    # 站点 logo、二维码、导航图标等"看起来怪怪的"图片。
    IMAGE_EXTRACT = r"""(() => {
        const CONTENT_SELECTORS = [
            '#photo_body', '#photo_wrap', '.photo-body', '.comic_wraCon',
            '.chapter-content', '.view-panel', '.view-content', '.manga-content',
            '.reader-content', '.reading-content', '#chapter-content',
            '.chapter-img', '.view-images', '.comic-page', '.manga-image',
        ];
        const AD = /(ad|ads|adv|banner|sponsor|promo|logo|icon|avatar|qrcode|qr_code|placeholder|blank|loading|spacer)/i;
        const BAD_EXT = /\.(svg|gif|ico)(?:[?#]|$)/i;
        const goods = (img) => {
            const width = img.naturalWidth || 0;
            const height = img.naturalHeight || 0;
            return {
                width: width,
                height: height,
                loaded: !!(img.complete && width > 1 && height > 1),
            };
        };
        const pick = (scope) => {
            const out = [];
            for (const img of scope.querySelectorAll('img')) {
                const url = img.currentSrc || img.getAttribute('data-src')
                    || img.getAttribute('data-original') || img.getAttribute('data-lazy-src')
                    || img.getAttribute('data-echo') || img.src;
                if (!url || !/^https?:/i.test(url) || BAD_EXT.test(url)) continue;
                const info = goods(img);
                if (info.loaded && (info.width < 120 || info.height < 120)) continue;
                let score = info.loaded ? info.width * info.height : 1;
                if (AD.test(url)) score -= 5000000;
                if (info.loaded && info.width / Math.max(1, info.height) > 3.5) score -= 2000000;
                out.push({ url: url, score: score, order: out.length });
            }
            return out;
        };
        let best = [];
        for (const selector of CONTENT_SELECTORS) {
            const node = document.querySelector(selector);
            if (!node) continue;
            const found = pick(node);
            if (found.length > best.length) best = found;
        }
        if (best.length < 2) {
            best = pick(document.body || document.documentElement);
        }
        // 先去广告类，再按页面出现顺序输出，保证页序与站点一致
        const filtered = best.filter((item) => item.score > 0);
        const chosen = filtered.length >= 2 ? filtered : best;
        chosen.sort((a, b) => a.order - b.order);
        const urls = [];
        const seen = new Set();
        for (const item of chosen) {
            if (seen.has(item.url)) continue;
            seen.add(item.url);
            urls.push(item.url);
        }
        return urls;
    })()"""
    FAVORITE_FIND = r"""(() => {
        const words = /favorite|favourite|bookmark|shelf|收藏|書架|书架|最愛|お気に入り/i;
        const links = [...document.querySelectorAll('a[href]')];
        const match = links.find((node) => words.test(
            `${node.textContent || ''} ${node.getAttribute('title') || ''} ${node.href || ''}`
        ));
        return match ? match.href : '';
    })()"""
    CONTENT_ONLY_STYLE = r"""
        (() => {
            if (document.getElementById('__manga_reader_clean_style')) return;
            const style = document.createElement('style');
            style.id = '__manga_reader_clean_style';
            style.textContent = `
                header, footer, nav, aside,
                [class*="ad" i], [id*="ad" i], [class*="banner" i],
                [class*="popup" i], [class*="modal" i], [class*="overlay" i],
                [class*="float" i], [class*="sticky" i], [class*="share" i],
                iframe, video, object, embed,
                a[href*="telegram" i], a[href*="discord" i],
                a[href*="sponsor" i], a[href*="donate" i] {
                    display: none !important;
                }
                html, body { background: #151515 !important; }
                body { margin: 0 !important; }
                img { max-width: 100% !important; height: auto !important; }
            `;
            document.documentElement.appendChild(style);
        })()
    """
    # 分类 / 标签：只从站点自己的筛选导航里取，不做猜测
    CATEGORY_EXTRACT = r"""(() => {
        const PATTERN = /\/(manga-genre|manga-tag|genre|genres|tag|tags|category|categories|cate|sort|filter|topics?|theme|themes)\b/i;
        const SKIP_TEXT = /^(首页|home|登录|注册|全部|更多|排行|更新|完结|连载)$/i;
        const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
        const rows = [];
        const seen = new Set();
        const push = (href, label, kind) => {
            if (!href || !label) return;
            if (seen.has(href)) return;
            if (label.length < 2 || label.length > 24) return;
            if (SKIP_TEXT.test(label)) return;
            seen.add(href);
            rows.push({ href: href, label: label, kind: kind });
        };
        for (const anchor of document.querySelectorAll('a[href]')) {
            const href = anchor.getAttribute('href') || '';
            if (!PATTERN.test(href)) continue;
            push(anchor.href || href, clean(anchor.textContent || anchor.getAttribute('title')), 'genre');
        }
        for (const node of document.querySelectorAll('[data-genre],[data-genres],[data-tag],[data-category]')) {
            const value = node.getAttribute('data-genre') || node.getAttribute('data-genres')
                || node.getAttribute('data-tag') || node.getAttribute('data-category');
            if (!value) continue;
            push(value, clean(node.textContent), 'genre');
        }
        return rows.slice(0, 200);
    })()"""
    # 下一页：站点自带的分页控件（很多站点是 JS 渲染，只能从 DOM 里找）
    NEXT_PAGE_EXTRACT = r"""(() => {
        const words = /^(下一页|下页|后页|next|Next|›|»|>)$/;
        for (const anchor of document.querySelectorAll('a[href]')) {
            const text = String(anchor.textContent || '').replace(/\s+/g, ' ').trim();
            const rel = (anchor.getAttribute('rel') || '').toLowerCase();
            if (words.test(text) || rel === 'next') {
                return anchor.href || '';
            }
        }
        const numbered = [...document.querySelectorAll('a[href]')]
            .filter((anchor) => /^\d+$/.test(String(anchor.textContent || '').trim()))
            .map((anchor) => ({ href: anchor.href, value: parseInt(anchor.textContent.trim(), 10) }))
            .filter((item) => Number.isFinite(item.value));
        const active = document.querySelector('.active, .current, [aria-current="page"]');
        const current = active ? parseInt(String(active.textContent || '').trim(), 10) : NaN;
        if (Number.isFinite(current)) {
            const next = numbered.find((item) => item.value === current + 1);
            if (next) return next.href;
        }
        return '';
    })()"""
    NATIVE_DOWNLOAD_EXTRACT = r"""(() => {
        const links = [...document.querySelectorAll('a[href]')];
        const download = links.find((anchor) => {
            const text = `${anchor.textContent || ''} ${anchor.getAttribute('title') || ''}`;
            const href = anchor.getAttribute('href') || '';
            return /download-index-aid-|下載漫畫|下载漫画/i.test(`${text} ${href}`);
        });
        return download ? download.href : '';
    })()"""
    DOWNLOAD_PAGE_DATA = r"""(() => ({
        scripts: [...document.scripts].map((script) => script.textContent || '').join('\n'),
        links: [...document.querySelectorAll('a[href]')]
            .map((anchor) => anchor.href || '')
            .filter((href) => /\.zip(?:[?#]|$)/i.test(href))
    }))()"""

    def _list_url(self, page: int = 1, category: str = "") -> str:
        """列表页地址：支持站点自带分类链接与分页参数。"""
        if category.startswith(("http://", "https://")):
            target = category
        elif category.startswith("/"):
            target = urllib.parse.urljoin(f"{self.site_base}/", category)
        else:
            target = self.search_path and urllib.parse.urljoin(f"{self.site_base}/", self.search_path) or f"{self.site_base}/"
        parsed = urllib.parse.urlparse(target)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if self.sort:
            query.setdefault("sort", self.sort)
        # 已有 page-N 形式的站点（绅士漫画 albums-index-page-N.html）单独处理
        path = re.sub(r"(page-)\d+(\.html)$", f"\\g<1>{max(1, page)}\\g<2>", parsed.path)
        if re.search(r"page-\d+\.html$", path):
            return urllib.parse.urlunparse(parsed._replace(path=path, query=urllib.parse.urlencode(query)))
        if page and page > 1:
            query[self.page_param] = str(page)
        return urllib.parse.urlunparse(
            parsed._replace(query=urllib.parse.urlencode(query))
        )

    # ---------- 导航 ----------
    def _open_impl(self, url: str, content_only: bool = False) -> str:
        page = self._page
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        # 等页面真的出了内容，而不是固定睡 1200ms
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if self._evaluate_rows(page, self.SEARCH_EXTRACT):
                break
            page.wait_for_timeout(120)
        if content_only:
            page.evaluate(self.CONTENT_ONLY_STYLE)
        return str(page.url)

    def open(self, url: str | None = None) -> str:
        """显示共享浏览器窗口并打开站点（切换漫源时复用同一个窗口）。"""
        target = url or self.site_base
        if target.startswith("/"):
            target = urllib.parse.urljoin(f"{self.site_base}/", target)
        return str(self._execute(self._open_shared, target))

    def _open_shared(self, target: str) -> str:
        try:
            hub().ensure_visible(True)
            self._window_open = True
            return hub().show_window(target)
        except BrowserHubError as exc:
            raise SiteBrowserError(str(exc)) from exc

    def open_reader(self, chapter_url: str) -> str:
        target = urllib.parse.urljoin(f"{self.site_base}/", chapter_url)
        return str(self._execute(self._open_shared, target))

    # ---------- 搜索 / 浏览 / 分类 ----------
    def _search_impl(self, query: str) -> list[dict[str, Any]]:
        page = self._page
        if self.site_key == "wnacg":
            params = urllib.parse.urlencode({
                "q": query.strip(),
                "f": "_all",
                "s": "create_time_DESC",
                "syn": "yes",
            })
            search_page = f"{self.site_base}/search/?{params}"
        else:
            search_page = urllib.parse.urljoin(
                f"{self.site_base}/",
                self.search_path or "/",
            )
        page.goto(search_page, wait_until="domcontentloaded", timeout=45000)
        if self.site_key == "wnacg":
            # 搜索结果是服务端渲染的，直接等条目出现即可
            return self._wait_for_rows(page, self.SEARCH_EXTRACT, timeout=8.0)
        selectors = (
            "input[type='search']",
            "input[name*='search' i]",
            "input[name*='keyword' i]",
            "input[placeholder*='搜索']",
            "input[placeholder*='搜尋']",
            "input[placeholder*='search' i]",
            "input[title*='搜索']",
            "input[title*='搜尋']",
        )
        field = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and field is None:
            for selector in selectors:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    field = locator
                    break
            if field is None:
                page.wait_for_timeout(100)
        if field is None:
            rows = self._wait_for_rows(page, self.SEARCH_EXTRACT, timeout=6.0)
            query_lower = query.casefold()
            return [
                row for row in rows
                if query_lower in str(row.get("title") or "").casefold()
            ]
        field.fill(query.strip())
        field.press("Enter")
        return self._wait_for_rows(page, self.SEARCH_EXTRACT, timeout=15.0)

    def search(self, query: str) -> list[dict[str, Any]]:
        return list(self._execute(self._search_impl, query))

    def _rows_impl(self, url: str, scroll: bool = True) -> list[dict[str, Any]]:
        page = self._page
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        return self._wait_for_rows(page, self.SEARCH_EXTRACT, scroll=scroll)

    def list_rows(self, page: int = 1, category: str = "") -> list[dict[str, Any]]:
        """按站点自带分类 / 分页读取一页作品列表。"""
        return list(self._execute(self._rows_impl, self._list_url(page, category)))

    def _browse_impl(self) -> list[dict[str, Any]]:
        return self._rows_impl(self._list_url(1, ""))

    def browse(self) -> list[dict[str, Any]]:
        return list(self._execute(self._browse_impl))

    def _categories_impl(self) -> list[dict[str, Any]]:
        if self._categories_cache is not None:
            return list(self._categories_cache)
        page = self._page
        page.goto(self._list_url(1, ""), wait_until="domcontentloaded", timeout=45000)
        # 分类导航通常在页头，等它出现就行，不用固定睡 900ms
        deadline = time.monotonic() + 8.0
        rows: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            raw = page.evaluate(self.CATEGORY_EXTRACT)
            rows = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
            if rows:
                break
            page.wait_for_timeout(150)
        self._categories_cache = rows
        return list(rows)

    def categories(self) -> list[dict[str, Any]]:
        """读取站点自带的分类 / 标签导航（一次读取后本次运行内复用）。"""
        return list(self._execute(self._categories_impl))

    def _has_next_impl(self, page_number: int, category: str = "") -> bool:
        page = self._page
        target = self._list_url(page_number, category)
        if str(page.url) != target:
            page.goto(target, wait_until="domcontentloaded", timeout=45000)
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                if self._evaluate_rows(page, self.SEARCH_EXTRACT):
                    break
                page.wait_for_timeout(150)
        return bool(str(page.evaluate(self.NEXT_PAGE_EXTRACT) or ""))

    def has_next_page(self, page_number: int, category: str = "") -> bool:
        """探测是否还有下一页（站点自带分页优先，其次用固定页数上限兜底）。"""
        try:
            return bool(self._execute(self._has_next_impl, page_number, category))
        except SiteBrowserError:
            return False

    # ---------- 作品 / 章节 / 图片 ----------
    def _extract_chapters_impl(self, series_url: str) -> list[dict[str, Any]]:
        page = self._page
        page.goto(series_url, wait_until="domcontentloaded", timeout=45000)
        return self._wait_for_rows(page, self.CHAPTER_EXTRACT, timeout=12.0)

    def chapters(self, series_url: str) -> list[dict[str, Any]]:
        return list(self._execute(self._extract_chapters_impl, series_url))

    def _extract_pages_impl(self, chapter_url: str) -> list[str]:
        page = self._page
        # 阅读页要读真实图片地址：这里必须让图片正常加载（有些站点的 lazyload
        # 只有在真正请求图片后才会把地址写进 src），所以临时退出轻量模式。
        self._apply_full_mode(page)
        page.goto(chapter_url, wait_until="domcontentloaded", timeout=45000)
        # 站点阅读页多为 JS 懒加载：边滚边等图片地址出现并稳定下来
        urls: list[str] = []
        stable = 0
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            page.wait_for_timeout(200)
            urls = self._evaluate_urls(page)
            if urls:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
            try:
                page.evaluate(
                    "window.scrollTo(0, document.documentElement.scrollHeight);"
                )
            except Exception:  # noqa: BLE001
                pass
        try:
            page.evaluate("window.scrollTo(0, 0);")
        except Exception:  # noqa: BLE001
            pass
        return self._evaluate_urls(page) or urls

    @staticmethod
    def _evaluate_urls(page: Any) -> list[str]:
        try:
            raw = page.evaluate(SiteBrowserClient.IMAGE_EXTRACT)
        except Exception:  # noqa: BLE001
            return []
        return [str(item) for item in raw if isinstance(item, str)] if isinstance(raw, list) else []

    def pages(self, chapter_url: str) -> list[str]:
        return list(self._execute(self._extract_pages_impl, chapter_url))

    def _favorite_impl(self) -> str:
        if self.favorite_path:
            return self._open_impl(
                urllib.parse.urljoin(f"{self.site_base}/", self.favorite_path)
            )
        page = self._page
        page.goto(self.site_base, wait_until="domcontentloaded", timeout=45000)
        target = ""
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and not target:
            target = str(page.evaluate(self.FAVORITE_FIND) or "")
            if not target:
                page.wait_for_timeout(150)
        if target:
            page.goto(target, wait_until="domcontentloaded", timeout=45000)
            self._wait_for_rows(page, self.SEARCH_EXTRACT, timeout=8.0)
        return str(page.url)

    def open_favorites(self) -> str:
        return str(self._execute(self._favorite_impl))

    def _favorites_impl(self) -> list[dict[str, Any]]:
        if self.favorite_path:
            self._open_impl(
                urllib.parse.urljoin(f"{self.site_base}/", self.favorite_path)
            )
        else:
            self._favorite_impl()
        raw = self._page.evaluate(self.SEARCH_EXTRACT)
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def favorites(self) -> list[dict[str, Any]]:
        return list(self._execute(self._favorites_impl))

    # ---------- 图片下载 ----------
    def _download_impl(self, url: str, referer: str, destination: str) -> str:
        response = self._request().get(
            url,
            headers={"Referer": referer, "Accept": "image/*,*/*;q=0.8"},
            timeout=45000,
        )
        if not response.ok:
            raise SiteBrowserError(f"图片请求失败：HTTP {response.status}")
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f"{path.name}.part")
        try:
            partial.write_bytes(response.body())
            os.replace(partial, path)
        except BaseException:
            try:
                partial.unlink()
            except OSError:
                pass
            raise
        return "downloaded"

    def _request(self) -> Any:
        try:
            return hub().request_context(f"site:{self.site_key}")
        except BrowserHubError as exc:
            raise SiteBrowserError(str(exc)) from exc

    def _fetch_image_impl(self, url: str, referer: str) -> bytes:
        response = self._request().get(
            url,
            headers={
                "Referer": referer or f"{self.site_base}/",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
            timeout=45000,
        )
        if not response.ok:
            raise SiteBrowserError(f"封面请求失败：HTTP {response.status}")
        return response.body()

    def fetch_image(self, url: str, referer: str = "") -> bytes:
        return bytes(self._execute(self._fetch_image_impl, url, referer))

    def _download_archive_impl(self, series_url: str, destination: str) -> str:
        page = self._page
        detail_url = urllib.parse.urljoin(f"{self.site_base}/", series_url)
        page.goto(detail_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(900)
        download_url = str(page.evaluate(self.NATIVE_DOWNLOAD_EXTRACT) or "")
        if not download_url:
            match = re.search(r"photos-index-aid-(\d+)", str(page.url))
            if match:
                download_url = urllib.parse.urljoin(
                    f"{self.site_base}/",
                    f"/download-index-aid-{match.group(1)}.html",
                )
        if not download_url:
            raise SiteBrowserError("未找到站点原生下载入口")

        page.goto(download_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(700)
        data = page.evaluate(self.DOWNLOAD_PAGE_DATA)
        if not isinstance(data, dict):
            raise SiteBrowserError("原生下载页读取失败")
        scripts = str(data.get("scripts") or "")
        links = [str(item) for item in data.get("links", []) if isinstance(item, str)]

        def js_value(key: str) -> str:
            match = re.search(rf"(?:^|[\\s,{{]){key}\s*:\s*[\"']([^\"']+)", scripts)
            return match.group(1) if match else ""

        routes: list[str] = []
        worker_api = js_value("WORKER_API")
        file_key = js_value("FILE_KEY")
        file_name = js_value("FILE_NAME")
        request = self._request()
        if worker_api and file_key:
            response = request.post(
                worker_api,
                data=json.dumps({"file_key": file_key, "file_name": file_name}),
                headers={
                    "Content-Type": "application/json",
                    "Referer": download_url,
                },
                timeout=45000,
            )
            if response.ok:
                try:
                    payload = response.json()
                except Exception:
                    payload = {}
                if isinstance(payload, dict) and payload.get("success") and payload.get("url"):
                    routes.append(str(payload["url"]))
        routes.extend(link for link in links if link not in routes)
        if not routes:
            raise SiteBrowserError("原生下载页没有可用下载线路")

        last_error = ""
        for route in routes:
            try:
                response = request.get(
                    route,
                    headers={"Referer": download_url},
                    timeout=120000,
                )
                if not response.ok:
                    last_error = f"HTTP {response.status}"
                    continue
                body = response.body()
                if not body.startswith(b"PK"):
                    last_error = "响应不是 ZIP 文件"
                    continue
                try:
                    with zipfile.ZipFile(io.BytesIO(body)) as archive:
                        has_images = any(
                            name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"))
                            and not name.endswith("/")
                            for name in archive.namelist()
                        )
                except zipfile.BadZipFile:
                    has_images = False
                if not has_images:
                    last_error = "ZIP 中没有图片"
                    continue
                path = Path(destination)
                path.parent.mkdir(parents=True, exist_ok=True)
                partial = path.with_name(f"{path.name}.part")
                try:
                    partial.write_bytes(body)
                    os.replace(partial, path)
                except BaseException:
                    try:
                        partial.unlink()
                    except OSError:
                        pass
                    raise
                return "downloaded"
            except Exception as exc:
                last_error = str(exc)
        raise SiteBrowserError(f"站点原生下载失败：{last_error or '未知错误'}")

    def download_archive(self, series_url: str, destination: Path) -> str:
        if destination.exists() and destination.stat().st_size > 0:
            return "skipped"
        try:
            return str(self._execute(self._download_archive_impl, series_url, str(destination)))
        except Exception:
            partial = destination.with_name(f"{destination.name}.part")
            try:
                partial.unlink()
            except OSError:
                pass
            raise

    def download_file(self, url: str, referer: str, destination: Path) -> str:
        if destination.exists() and destination.stat().st_size > 0:
            return "skipped"
        try:
            return str(self._execute(self._download_impl, url, referer, str(destination)))
        except Exception:
            partial = destination.with_name(f"{destination.name}.part")
            try:
                partial.unlink()
            except OSError:
                pass
            raise

    # ---------- 生命周期 ----------
    def clear_cache(self) -> int:
        """清掉共享 Chromium 的磁盘缓存（不关闭窗口，避免反复闪退）。"""
        return hub().clear_caches()

    def close(self) -> None:
        """关闭本漫源的 page，但保留共享 Chromium（切换漫源不重开浏览器）。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            hub().close_page(f"site:{self.site_key}")
        except Exception:  # noqa: BLE001 - 关闭阶段尽力而为
            pass
        self._window_open = False
