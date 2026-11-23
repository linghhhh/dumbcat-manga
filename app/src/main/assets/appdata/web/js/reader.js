/* 阅读器：条漫/单页两种模式，记进度、键盘翻页、预取下一章。 */
import { api, withToken } from './api.js';
import { el, clear, toast, loadLocal, saveLocal, openModal, closeModal } from './util.js';
import { navigate } from './util.js';

let state = null;
let rootRef = null;
let cleanup = null;

const MODES = {
  scroll: { label: '条漫', icon: '☰' },
  single: { label: '单页', icon: '▣' },
};

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

  const saved = loadLocal(progressKey(data.series?.uid), null);
  state = {
    series: data.series || {},
    chapter: data.chapter || {},
    chapterUid: data.chapterUid || chapterUid,
    pages: (data.pages || []).map((page, index) => ({ index, ...page })),
    chapters: [],
    index: 0,
    mode: loadLocal('reader.mode', 'scroll') === 'single' ? 'single' : 'scroll',
    fit: loadLocal('reader.fit', 'width'),
    prefetch: null,
    saved: saved && saved.chapterUid === (data.chapterUid || chapterUid) ? saved : null,
    nextUid: null,
    prevUid: null,
    saveTimer: 0,
    hideTimer: 0,
    scrollHandler: null,
    keyHandler: null,
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
  state.index = Math.min(state.saved?.index || 0, state.pages.length - 1);

  draw();
  bindKeys();
  await loadChapterList();
  if (state.mode === 'single') showSingle(state.index, { silent: true });
  else restoreScroll();
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
    const select = document.getElementById('readerChapterSelect');
    if (select) fillChapterSelect(select);
    updatePager();
  } catch (error) {
    console.warn('章节表读取失败', error);
  }
}

function fillChapterSelect(select) {
  clear(select);
  state.chapters.forEach((chapter, index) => {
    const option = el('option', {
      value: chapter.uid,
      text: `${chapter.locked ? '🔒 ' : ''}${chapter.title || `第 ${chapter.number || index + 1} 话`}`,
    });
    if (chapter.uid === state.chapterUid) option.selected = true;
    select.append(option);
  });
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
  const pages = el('div', { class: 'reader-pages', id: 'readerPages', dataset: { mode: state.mode } });
  state.pages.forEach((page) => pages.append(pageEl(page)));
  prefetch(state.pages.slice(1, 5));
  container.append(pages, pagerEl());
  root.append(container);
  container.classList.toggle('light', loadLocal('reader.light', false) === true);
  requestAnimationFrame(() => {
    const pagesEl = document.getElementById('readerPages');
    state.scrollHandler = () => {
      if (state.mode === 'scroll') onScroll();
      else updateProgressBar();
    };
    (pagesEl || window).addEventListener('scroll', state.scrollHandler, { passive: true });
    updateProgressBar();
  });
  applyModeStyles();
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
  const chapterSelect = el('select', {
    id: 'readerChapterSelect',
    title: '跳章节',
    on: {
      change: (event) => {
        const uid = event.target.value;
        if (uid && uid !== state.chapterUid) navigate(`#/read/${uid}`);
      },
    },
  });

  return el('div', { class: 'reader-top' },
    el('button', {
      class: 'btn small ghost', type: 'button',
      on: { click: () => navigate(`#/series/${encodeURIComponent(state.series.uid || '')}`) },
    }, '← 详情'),
    el('div', { class: 'rt-text' },
      el('div', { class: 'rt-title', text: state.series.title || '阅读' }),
      el('div', { class: 'rt-sub', text: state.chapter.title || '' }),
    ),
    chapterSelect,
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
  img.addEventListener('dblclick', () => {
    if (state.mode === 'single') return;
    const index = Number(img.dataset.index);
    toggleMode();
    showSingle(index);
  });
  img.addEventListener('click', () => {
    if (state.mode === 'single' || img.classList.contains('failed')) {
      img.classList.remove('failed');
      img.dataset.attempt = '0';
      img.style.minHeight = '';
      img.src = withToken(page.url);
    }
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
        const index = Math.max(0, Number(event.target.value) - 1);
        if (state.mode === 'single') showSingle(index);
        else jumpTo(index);
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
      el('button', {
        class: 'btn tiny ghost', id: 'readerModeBtn', type: 'button',
        on: { click: toggleMode },
      }, `${MODES[state.mode].icon} ${MODES[state.mode].label}`),
      el('button', {
        class: 'btn tiny ghost', id: 'readerFitBtn', type: 'button',
        on: { click: toggleFit },
      }, '适应宽度'),
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

function applyModeStyles() {
  const pages = document.getElementById('readerPages');
  if (!pages) return;
  pages.dataset.mode = state.mode;
  pages.dataset.fit = state.fit;
  const fitButton = document.getElementById('readerFitBtn');
  if (fitButton) fitButton.textContent = state.fit === 'height' ? '适应高度' : '适应宽度';
  const modeButton = document.getElementById('readerModeBtn');
  if (modeButton) modeButton.textContent = `${MODES[state.mode].icon} ${MODES[state.mode].label}`;
  pages.querySelectorAll('img').forEach((img) => {
    img.hidden = state.mode === 'single' && Number(img.dataset.index) !== state.index;
  });
}

/* ------------------------------ 单页模式 ------------------------------ */
function showSingle(index, options = {}) {
  const target = Math.min(Math.max(0, index), state.pages.length - 1);
  state.index = target;
  const pages = scrollBox();
  if (!pages) return;
  pages.querySelectorAll('img').forEach((img) => {
    img.hidden = Number(img.dataset.index) !== target;
  });
  pages.scrollTop = 0;
  updatePager();
  updateProgressBar();
  if (!options.silent) saveProgressNow();
  prefetchNearEnd();
}

function scrollBox() {
  return document.getElementById('readerPages');
}

function jumpTo(index) {
  const pages = scrollBox();
  if (state.mode === 'single') {
    showSingle(index);
    return;
  }
  const img = document.querySelector(`#readerPages img[data-index="${index}"]`);
  if (img && pages) {
    pages.scrollTo({ top: img.offsetTop, behavior: 'smooth' });
    state.index = index;
    updatePager();
  }
}

function toggleMode() {
  if (state.mode === 'scroll' && state.index >= 0) {
    state.mode = 'single';
    saveLocal('reader.mode', 'single');
    applyModeStyles();
    showSingle(state.index, { silent: true });
  } else {
    state.mode = 'scroll';
    saveLocal('reader.mode', 'scroll');
    applyModeStyles();
    document.getElementById('readerPages')?.querySelectorAll('img')
      .forEach((img) => { img.hidden = false; });
    jumpTo(state.index);
  }
}

function toggleFit() {
  state.fit = state.fit === 'height' ? 'width' : 'height';
  saveLocal('reader.fit', state.fit);
  applyModeStyles();
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
  let best = 0;
  let bestDistance = Infinity;
  for (const img of images) {
    const center = img.offsetTop + img.offsetHeight / 2;
    const distance = Math.abs(center - (pages.scrollTop + pages.clientHeight / 2));
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
  const total = state.pages.length || 1;
  const pages = scrollBox();
  let percent;
  if (state.mode === 'single') {
    percent = ((state.index + 1) / total) * 100;
  } else if (pages) {
    const scrollable = Math.max(1, pages.scrollHeight - pages.clientHeight);
    percent = ((pages.scrollTop + pages.clientHeight) / (scrollable + pages.clientHeight)) * 100;
  } else {
    percent = 0;
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
  api.shelfHistoryAdd({
    uid: series.uid || '',
    source: series.provider || series.key || '',
    title: series.title || '',
    cover: series.coverUrl || '',
    chapterUid: state.chapterUid,
    chapterTitle: state.chapter.title || '',
    page: state.index,
    total: state.pages.length,
  }).catch(() => {});
}

function scheduleSave() {
  clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(saveProgressNow, 400);
}

function saveProgressNow() {
  if (!state) return;
  saveLocal(progressKey(state.series.uid), {
    chapterUid: state.chapterUid,
    index: state.index,
    title: state.chapter.title || '',
    at: Date.now(),
  });
  reportHistory();
}

function restoreScroll() {
  if (!state.saved?.index) return;
  requestAnimationFrame(() => {
    const pages = scrollBox();
    const img = document.querySelector(`#readerPages img[data-index="${state.saved.index}"]`);
    if (pages && img) pages.scrollTop = img.offsetTop;
  });
}

/* ------------------------------ 预取 ------------------------------ */
function prefetchNearEnd() {
  if (!state.nextUid || state.prefetch || state.mode !== 'scroll') return;
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
      if (state.mode === 'single') {
        if (state.index + 1 < state.pages.length) showSingle(state.index + 1);
        else goChapter(state.nextUid);
      } else {
        if (state.index + 1 < state.pages.length) jumpTo(state.index + 1);
        else goChapter(state.nextUid);
      }
      return;
    }
    if (key === 'ArrowLeft' || key === 'PageUp') {
      event.preventDefault();
      if (state.mode === 'single') {
        if (state.index > 0) showSingle(state.index - 1);
        else goChapter(state.prevUid);
      } else if (state.index > 0) {
        jumpTo(state.index - 1);
      }
      return;
    }
    if (key === 'ArrowDown' || key === 'ArrowUp') {
      if (state.mode === 'single') {
        event.preventDefault();
        showSingle(state.index + (key === 'ArrowDown' ? 1 : -1));
      }
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
  document.body.classList.remove('reading');
  if (state?.scrollHandler) {
    const pages = scrollBox();
    (pages || window).removeEventListener('scroll', state.scrollHandler);
  }
  state = null;
  rootRef = null;
}
