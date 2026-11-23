/* 阅读器：竖向滚动（条漫）阅读，记进度、键盘翻页、预取下一章。 */
import { api, withToken } from './api.js';
import { el, clear, toast, loadLocal, saveLocal, openModal, closeModal } from './util.js';
import { navigate } from './util.js';
import { createViewer } from './viewer.js';

let state = null;
let rootRef = null;
let cleanup = null;

function progressKey(seriesUid) {
  return `reader.progress.${seriesUid || 'unknown'}`;
}

export async function render(root, params, ctx = {}) {
  const chapterUid = params.chapterUid;
  const alive = ctx.alive || (() => true);
  rootRef = root;
  clear(root);
  document.body.classList.add('reading');

  root.append(el('div', { class: 'load-bar', style: 'padding:120px 0' },
    el('div', { class: 'spinner' }), el('span', { text: '呆猫正在铺开画页…' })));

  let data;
  try {
    data = await api.chapter(chapterUid);
  } catch (error) {
    if (!alive()) return;
    clear(root);
    document.body.classList.remove('reading');
    root.append(el('div', { class: 'empty-state' },
      el('div', { class: 'big', text: '😿' }),
      el('strong', { text: '这一章打不开' }),
      el('div', { text: error.message }),
      el('button', {
        class: 'btn primary', type: 'button',
        on: { click: () => navigate('#/browse') },
      }, '回书架'),
    ));
    return;
  }
  if (!alive()) return;

  /* 付费 / 会员 / 站点未登录的章节会返回 0 张图，这里给个明确交代 */
  if (!(data.pages || []).length) {
    clear(root);
    document.body.classList.remove('reading');
    const chapterTitle = String(data.chapter?.title || '').trim();
    root.append(el('div', { class: 'empty-state' },
      el('div', { class: 'big', text: '🔒' }),
      el('strong', { text: '这一章暂时拿不到图' }),
      el('div', { text: `${chapterTitle ? chapterTitle + ' ' : '这一章 '}可能是会员 / 付费章节，或者站点要求登录后再看。` }),
      el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;justify-content:center' },
        el('button', {
          class: 'btn primary', type: 'button',
          on: { click: () => navigate('#/shelf') },
        }, '去书架登录图源'),
        el('button', {
          class: 'btn', type: 'button',
          on: { click: () => history.back() },
        }, '返回上一页'),
      ),
    ));
    return;
  }

  /* 进度：本地的即时，服务端的权威（换壳/清缓存后本地就没了）。
     服务端更新更晚才覆盖本地，离线时本地照样能接着看。 */
  let saved = loadLocal(progressKey(data.series?.uid), null);
  try {
    const remote = await api.progress(data.series?.uid || '');
    const served = remote && remote.progress;
    if (served && served.chapterUid
      && Number(served.updatedAt) * 1000 > Number(saved?.ts || 0)) {
      saved = {
        chapterUid: served.chapterUid,
        index: Number(served.page) || 0,
        offsetRatio: Number(served.offsetRatio) || 0,
        ts: Number(served.updatedAt) * 1000,
      };
      saveLocal(progressKey(data.series?.uid), saved);
    }
  } catch (error) {
    /* 拿不到服务端进度就当没有，本地记录继续用 */
  }
  state = {
    series: data.series || {},
    chapter: data.chapter || {},
    chapterUid: data.chapterUid || chapterUid,
    pages: (data.pages || []).map((page, index) => ({ index, ...page })),
    chapters: [],
    index: 0,
    prefetch: null,
    saved: saved && saved.chapterUid === (data.chapterUid || chapterUid) ? saved : null,
    nextUid: null,
    prevUid: null,
    saveTimer: 0,
    hideTimer: 0,
    scrollHandler: null,
    keyHandler: null,
    viewer: null,
    restoreObserver: null,
  };
  if (!state.pages.length) {
    clear(root);
    root.append(el('div', { class: 'empty-state', style: 'padding:90px' },
      el('div', { class: 'big', text: '🫥' }),
      el('strong', { text: '这一章没有图片' }),
      el('div', { text: '可能图源还没放出内容，或者需要登录。' }),
    ));
    return;
  }
  if (data.offline) {
    toast('离线模式：正在读本地缓存', { type: 'info' });
  }
  state.index = Math.min(state.saved?.index || 0, state.pages.length - 1);

  draw();
  bindKeys();
  await loadChapterList();
  restoreScroll();
  reportHistory(true);
}

/* ------------------------------ 章节表 ------------------------------ */
async function loadChapterList() {
  const uid = state.series.uid;
  if (!uid) return;
  try {
    const data = await api.series(uid);
    state.chapters = data.chapters || [];
    const index = state.chapters.findIndex((chapter) => chapter.uid === state.chapterUid);
    if (index >= 0) {
      state.prevUid = state.chapters[index + 1]?.uid || null;
      state.nextUid = state.chapters[index - 1]?.uid || null;
      // 老版本 /api/read 命中缓存时不回章节信息，这里用章节表补一次标题，
      // 保证阅读器标题栏与浏览历史上报都有话名。
      state.chapter = { ...(state.chapter || {}), ...state.chapters[index] };
    }
    updatePager();
  } catch (error) {
    console.warn('章节表读取失败', error);
  }
}

/* ------------------------------ 结构 ------------------------------ */
function isTouchLayout() {
  return window.matchMedia('(pointer: coarse)').matches || window.innerWidth <= 900;
}

function draw() {
  const root = rootRef;
  clear(root);
  const container = el('div', { class: 'reader' });
  container.append(topBar(), el('div', { class: 'reader-progress' }, el('i', { id: 'readerProgress' })));
  const pages = el('div', { class: 'reader-pages', id: 'readerPages' });
  state.pages.forEach((page) => pages.append(pageEl(page)));
  prefetch(state.pages.slice(1, 5));
  container.append(pages, pagerEl());
  root.append(container);
  container.classList.toggle('light', loadLocal('reader.light', false) === true);
  requestAnimationFrame(() => {
    const pagesEl = document.getElementById('readerPages');
    state.scrollHandler = onScroll;
    (pagesEl || window).addEventListener('scroll', state.scrollHandler, { passive: true });
    updateProgressBar();
  });
  /* 手势层：双指 / 双击按坐标找图缩放，scale === 1 时完全不拦截滚动 */
  state.viewer?.destroy();
  state.viewer = createViewer(pages);
  bindUiToggle(container);
}

/* 手机上点画面隐藏/唤出上下栏，3 秒不动自动收起 */
function bindUiToggle(container) {
  const pages = document.getElementById('readerPages');
  if (!pages) return;
  let moved = false;
  let startY = 0;
  pages.addEventListener('touchstart', (event) => {
    moved = false;
    startY = event.touches[0]?.clientY || 0;
  }, { passive: true });
  pages.addEventListener('touchmove', (event) => {
    if (Math.abs((event.touches[0]?.clientY || 0) - startY) > 8) moved = true;
  }, { passive: true });
  pages.addEventListener('click', (event) => {
    /* 刚做过滑动翻页 / 双击缩放时，浏览器仍可能补一个 click，别让它把菜单切出来 */
    if (Number(pages.dataset.suppressUntil || 0) > Date.now()) return;
    if (moved) return;
    if (!isTouchLayout()) return;
    if (event.target.closest('button, select, input, a')) return;
    container.classList.toggle('ui-hidden');
    if (!container.classList.contains('ui-hidden')) scheduleHideUi(container);
  });
  scheduleHideUi(container);
}

function scheduleHideUi(container) {
  clearTimeout(state.hideTimer);
  if (!container || !isTouchLayout()) return;
  state.hideTimer = setTimeout(() => {
    if (!document.body.classList.contains('reading')) return;
    if (document.querySelector('.modal-root:not([hidden])')) return;
    container.classList.add('ui-hidden');
  }, 3000);
}

function topBar() {
  return el('div', { class: 'reader-top' },
    el('button', {
      class: 'btn small ghost', type: 'button',
      on: { click: () => navigate(`#/series/${encodeURIComponent(state.series.uid || '')}`) },
    }, '← 详情'),
    el('div', { class: 'rt-text' },
      el('div', { class: 'rt-title', text: state.series.title || '阅读' }),
      el('div', { class: 'rt-sub', text: state.chapter.title || '' }),
    ),
  );
}

/* 先悄悄拉前几张，翻到那里就是秒开：浏览器缓存命中，图片元素直接复用。 */
function prefetch(pages) {
  pages.forEach((page) => {
    const img = new Image();
    img.decoding = 'async';
    img.src = withToken(page.url);
  });
}

function pageEl(page) {
  const img = el('img', {
    alt: `第 ${page.index + 1} 页`,
    loading: page.index < 3 ? 'eager' : 'lazy',
    decoding: 'async',
    draggable: 'false',
    dataset: { index: String(page.index), attempt: '0' },
  });
  img.addEventListener('load', () => {
    img.classList.remove('retrying');
    img.classList.add('loaded');
  });
  img.addEventListener('error', () => {
    /* 图床偶尔抽风，先自己重试两次，别急着让用户看到裂图。 */
    const attempt = Number(img.dataset.attempt || 0);
    if (attempt < 2) {
      img.dataset.attempt = String(attempt + 1);
      img.classList.add('retrying');
      setTimeout(() => {
        img.src = withToken(page.url) + `&retry=${attempt + 1}`;
      }, 700 * (attempt + 1));
      return;
    }
    img.classList.remove('retrying');
    img.classList.add('failed');
    img.alt = `第 ${page.index + 1} 页加载失败，点一下重试`;
    img.style.minHeight = '120px';
  });
  img.src = withToken(page.url);
  img.addEventListener('click', () => {
    if (!img.classList.contains('failed')) return;
    img.classList.remove('failed');
    img.dataset.attempt = '0';
    img.style.minHeight = '';
    img.src = withToken(page.url);
  });
  return img;
}

function pagerEl() {
  const slider = el('input', {
    id: 'readerSlider',
    class: 'reader-slider',
    type: 'range',
    min: '1',
    max: String(Math.max(1, state.pages.length)),
    value: String(Math.min(state.pages.length, state.index + 1)),
    on: {
      input: (event) => {
        jumpTo(Math.max(0, Number(event.target.value) - 1));
      },
    },
  });
  return el('div', { class: 'reader-pager' },
    el('div', { class: 'pager-row' },
      el('button', {
        class: 'btn small ghost pager-chapter', id: 'prevChapterBtn', type: 'button',
        on: { click: () => goChapter(state.prevUid) },
      }, '‹ 上一话'),
      slider,
      el('button', {
        class: 'btn small ghost pager-chapter', id: 'nextChapterBtn', type: 'button',
        on: { click: () => goChapter(state.nextUid) },
      }, '下一话 ›'),
    ),
    el('div', { class: 'pager-row pager-tools' },
      el('button', {
        class: 'btn tiny ghost', type: 'button',
        on: { click: openDirectory },
      }, '📑 目录'),
      el('span', { class: 'page-pill', id: 'readerCounter', text: '1 / 1' }),
    ),
  );
}

function openDirectory() {
  if (!state.chapters.length) {
    toast('章节列表还没加载好，稍等一下', { type: 'info' });
    return;
  }
  const list = el('div', { class: 'dir-list' });
  state.chapters.forEach((chapter, index) => {
    const active = chapter.uid === state.chapterUid;
    list.append(el('button', {
      class: `dir-item ${active ? 'active' : ''}`,
      type: 'button',
      on: {
        click: () => {
          closeModal();
          if (!active) navigate(`#/read/${chapter.uid}`);
        },
      },
    },
      el('span', { class: 'dir-name', text: `${chapter.locked ? '🔒 ' : ''}${chapter.title || `第 ${chapter.number || index + 1} 话`}` }),
      active ? el('span', { class: 'dir-now', text: '在读' }) : null,
    ));
  });
  const current = list.querySelector('.dir-item.active');
  openModal(el('div', { class: 'dir-wrap' },
    el('div', { class: 'dir-head' },
      el('strong', { text: `目录 · ${state.chapters.length} 话` }),
      el('span', { class: 'sub', text: state.series.title || '' }),
    ),
    list,
  ), { className: 'modal-wide dir-modal' });
  if (current) setTimeout(() => current.scrollIntoView({ block: 'center' }), 60);
}

function updatePager() {
  const counter = document.getElementById('readerCounter');
  if (counter) counter.textContent = `${Math.min(state.pages.length, Math.max(1, state.index + 1))} / ${state.pages.length}`;
  const slider = document.getElementById('readerSlider');
  if (slider) {
    slider.max = String(Math.max(1, state.pages.length));
    slider.value = String(Math.min(state.pages.length, Math.max(1, state.index + 1)));
  }
  const prev = document.getElementById('prevChapterBtn');
  const next = document.getElementById('nextChapterBtn');
  if (prev) prev.disabled = !state.prevUid;
  if (next) next.disabled = !state.nextUid;
}

function scrollBox() {
  return document.getElementById('readerPages');
}

/* 图片在滚动容器内容坐标里的顶部：用 rect 算，不依赖 offsetParent，
   桌面端（上面还有顶栏）和手机端（容器 absolute）都能对齐。 */
function imgTop(img, pages) {
  return img.getBoundingClientRect().top - pages.getBoundingClientRect().top + pages.scrollTop;
}

function jumpTo(index) {
  const pages = scrollBox();
  const img = document.querySelector(`#readerPages img[data-index="${index}"]`);
  if (img && pages) {
    pages.scrollTo({ top: imgTop(img, pages), behavior: 'smooth' });
    state.index = index;
    updatePager();
  }
}

function goChapter(uid) {
  if (!uid) {
    toast('已经到头啦', { type: 'info' });
    return;
  }
  navigate(`#/read/${uid}`);
}

/* ------------------------------ 进度 ------------------------------ */
function currentScrollIndex() {
  const pages = scrollBox();
  if (!pages) return state.index;
  const images = Array.from(pages.querySelectorAll('img'));
  const contentTop = pages.getBoundingClientRect().top - pages.scrollTop;
  const center = pages.scrollTop + pages.clientHeight / 2;
  let best = 0;
  let bestDistance = Infinity;
  for (const img of images) {
    const rect = img.getBoundingClientRect();
    const middle = rect.top - contentTop + rect.height / 2;
    const distance = Math.abs(middle - center);
    if (distance < bestDistance) {
      bestDistance = distance;
      best = Number(img.dataset.index);
    }
  }
  return best;
}

function onScroll() {
  const index = currentScrollIndex();
  if (index !== state.index) {
    state.index = index;
    updatePager();
  }
  updateProgressBar();
  scheduleSave();
  prefetchNearEnd();
}

function updateProgressBar() {
  const fill = document.getElementById('readerProgress');
  if (!fill) return;
  const pages = scrollBox();
  let percent = 0;
  if (pages) {
    const scrollable = Math.max(1, pages.scrollHeight - pages.clientHeight);
    percent = ((pages.scrollTop + pages.clientHeight) / (scrollable + pages.clientHeight)) * 100;
  }
  fill.style.width = `${Math.max(1, Math.min(100, percent))}%`;
}

/* 浏览历史上报：换章立刻报，翻页 2 秒节流（后端按 uid 去重，只留最新一条）。 */
const historyReporter = { at: 0, key: '', timer: 0 };

function reportHistory(force = false) {
  if (!state) return;
  const key = `${state.chapterUid}:${state.index}`;
  const now = Date.now();
  if (!force && key === historyReporter.key) return;
  if (!force && now - historyReporter.at < 2000) {
    clearTimeout(historyReporter.timer);
    historyReporter.timer = setTimeout(() => reportHistory(true), 2000 - (now - historyReporter.at));
    return;
  }
  historyReporter.key = key;
  historyReporter.at = now;
  const series = state.series || {};
  const chapters = (state.chapters || []).map((chapter) => ({
    uid: chapter.uid,
    title: chapter.title || '',
    number: chapter.number ?? null,
    index: chapter.index ?? 0,
    locked: Boolean(chapter.locked),
  }));
  api.shelfHistoryAdd({
    uid: series.uid || '',
    source: series.provider || series.key || '',
    title: series.title || '',
    cover: series.coverUrl || '',
    chapterUid: state.chapterUid,
    chapterTitle: state.chapter.title || '',
    page: state.index,
    total: state.pages.length,
    chapters,
    summary: {
      description: series.description || '',
      status: series.status || '',
      tags: series.tags || [],
      url: series.url || '',
      nsfw: Boolean(series.nsfw),
      providerName: series.providerName || '',
      coverUrl: series.coverUrl || '',
    },
  }).catch(() => {});
}

function scheduleSave() {
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveProgressNow, 400);
}

function saveProgressNow() {
  if (!state) return;
  const ratio = currentOffsetRatio();
  const payload = {
    chapterUid: state.chapterUid,
    index: state.index,
    /* 章内偏移：条漫一张图可能一屏放不下，只记 index 会跳回图顶 */
    offsetRatio: ratio,
    title: state.chapter.title || '',
    at: Date.now(),
    ts: Date.now(),
  };
  saveLocal(progressKey(state.series.uid), payload);
  api.saveProgress({
    seriesUid: state.series.uid || '',
    chapterUid: state.chapterUid,
    page: state.index,
    offsetRatio: ratio,
  }).catch(() => {});
  reportHistory();
}

/* 视口中线落在当前页的百分之几（0~1），条漫定位就靠它 */
function currentOffsetRatio() {
  const pages = scrollBox();
  if (!pages) return 0;
  const img = pages.querySelector(`img[data-index="${state.index}"]`);
  const height = img?.getBoundingClientRect().height || 0;
  if (!img || !height) return 0;
  const raw = (pages.scrollTop + pages.clientHeight / 2 - imgTop(img, pages)) / height;
  return Math.max(0, Math.min(1, raw));
}

/* 恢复阅读位置：等目标图真正解码完再定位（占位高度会让位置算错），
   之后图片陆续撑开高度时用 ResizeObserver 校正几次，用户一动就交还控制权。 */
function restoreScroll() {
  const saved = state.saved;
  if (!saved) return;
  if (!saved.index && !saved.offsetRatio) return;
  const pages = scrollBox();
  const img = pages?.querySelector(`img[data-index="${saved.index}"]`);
  if (!pages || !img) return;

  const ratio = Math.max(0, Math.min(0.98, Number(saved.offsetRatio) || 0));
  const apply = () => {
    if (!state || scrollBox() !== pages) return;
    const max = Math.max(0, pages.scrollHeight - pages.clientHeight);
    const height = img.getBoundingClientRect().height;
    const top = imgTop(img, pages) + height * ratio - pages.clientHeight / 2;
    pages.scrollTop = Math.max(0, Math.min(max, top));
  };
  const ready = (img.complete && img.naturalWidth > 0)
    ? Promise.resolve()
    : new Promise((resolve) => {
      const done = () => {
        img.removeEventListener('load', done);
        img.removeEventListener('error', done);
        resolve();
      };
      img.addEventListener('load', done);
      img.addEventListener('error', done);
      setTimeout(done, 4000);
    });

  ready.then(() => {
    if (!state || scrollBox() !== pages) return;
    apply();
    const stop = () => {
      state?.restoreObserver?.disconnect();
      if (state) state.restoreObserver = null;
      clearTimeout(timer);
      pages.removeEventListener('wheel', stop);
      pages.removeEventListener('touchstart', stop);
    };
    let timer = 0;
    if (typeof ResizeObserver !== 'function') {
      setTimeout(apply, 600);
      return;
    }
    let corrections = 0;
    state.restoreObserver = new ResizeObserver(() => {
      apply();
      corrections += 1;
      if (corrections >= 5) stop();
    });
    state.restoreObserver.observe(pages);
    timer = setTimeout(stop, 5000);
    pages.addEventListener('wheel', stop, { passive: true, once: true });
    pages.addEventListener('touchstart', stop, { passive: true, once: true });
  });
}

/* ------------------------------ 预取 ------------------------------ */
function prefetchNearEnd() {
  if (!state.nextUid || state.prefetch) return;
  if (state.index < state.pages.length - 3) return;
  state.prefetch = state.nextUid;
  api.chapter(state.nextUid).then((data) => {
    state.prefetchData = data;
  }).catch(() => {
    state.prefetch = null;
  });
}

/* ------------------------------ 键盘 ------------------------------ */
function bindKeys() {
  const onKey = (event) => {
    const tag = document.activeElement?.tagName || '';
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag)) return;
    if (document.querySelector('.modal-root:not([hidden])')) return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const key = event.key;
    if (key === 'ArrowRight' || key === 'PageDown' || key === ' ') {
      event.preventDefault();
      if (state.index + 1 < state.pages.length) jumpTo(state.index + 1);
      else goChapter(state.nextUid);
      return;
    }
    if (key === 'ArrowLeft' || key === 'PageUp') {
      event.preventDefault();
      if (state.index > 0) jumpTo(state.index - 1);
      return;
    }
    if (key === 'f' || key === 'F') {
      event.preventDefault();
      if (document.fullscreenElement) document.exitFullscreen?.();
      else document.documentElement.requestFullscreen?.().catch(() => {});
      return;
    }
    if (key === 'Escape') {
      navigate(`#/series/${encodeURIComponent(state.series.uid || '')}`);
    }
  };
  document.addEventListener('keydown', onKey);
  cleanup = () => document.removeEventListener('keydown', onKey);
}

export function unmount() {
  if (state) {
    clearTimeout(state.hideTimer);
    saveProgressNow();
    clearTimeout(historyReporter.timer);
    reportHistory(true);
  }
  if (cleanup) {
    try { cleanup(); } catch {}
    cleanup = null;
  }
  if (state?.restoreObserver) {
    try { state.restoreObserver.disconnect(); } catch {}
  }
  if (state?.viewer) {
    try { state.viewer.destroy(); } catch {}
  }
  document.body.classList.remove('reading');
  if (state?.scrollHandler) {
    const pages = scrollBox();
    (pages || window).removeEventListener('scroll', state.scrollHandler);
  }
  state = null;
  rootRef = null;
}
