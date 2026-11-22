/* 发现页：按图源浏览，滑到底部自动加载下一页，也支持手动跳页。 */
import { api } from './api.js';
import { el, clear, toast, sectionTitle, emptyState, fmtCount, rafThrottle } from './util.js';
import { seriesCard, categoryBar } from './cards.js';
import { activateCovers } from './lazy.js';

const states = new Map();
let unmountHook = null;

function freshState(source) {
  return {
    source,
    items: [],
    seen: new Set(),
    page: 0,
    hasMore: true,
    total: 0,
    category: '',
    categories: [],
    categoryPending: false,
    sourceName: '',
    nsfw: false,
    loading: false,
    error: '',
    scrollY: 0,
  };
}

function getState(source) {
  if (!states.has(source)) states.set(source, freshState(source));
  return states.get(source);
}

function dedupe(state, items) {
  const out = [];
  for (const item of items || []) {
    const uid = item.uid;
    if (!uid || state.seen.has(uid)) continue;
    state.seen.add(uid);
    out.push(item);
  }
  return out;
}

function cardsSkeleton(count = 8) {
  const fragment = document.createDocumentFragment();
  for (let i = 0; i < count; i += 1) {
    fragment.append(el('div', { class: 'card ghost' },
      el('div', { class: 'cover skeleton' }),
      el('div', { class: 'skeleton-line', style: 'height:13px;margin:0' }),
      el('div', { class: 'skeleton-line', style: 'height:10px;width:55%;margin:0' }),
    ));
  }
  return fragment;
}

export async function render(root, params, ctx = {}) {
  const source = params.source || '';
  if (!source) {
    clear(root);
    root.append(emptyState('🐾', '还没有可用的图源', '去「图源插件」页看看，或者重启一次让内置图源加载。'));
    return;
  }
  const state = getState(source);
  // 上一次挂载可能留下 loading=true（旧请求被 alive 丢弃时），这里复位，
  // 否则重新进入这个图源会一直卡在「正在翻书架」。
  state.loading = false;
  const alive = ctx.alive || (() => true);

  clear(root);
  const headHost = el('div');
  const catHost = el('div');
  const grid = el('div', { class: 'grid' });
  const footer = el('div', { class: 'load-bar' });
  const sentinel = el('div', { class: 'sentinel' });
  root.append(headHost, catHost, grid, footer, sentinel);

  /* ---------- 头部 ---------- */
  function renderHead() {
    clear(headHost);
    headHost.append(sectionTitle(
      state.sourceName || source,
      state.total ? `共 ${fmtCount(state.total)} 部左右 · 第 ${state.page || 1} 页` : '滑到底部会自动加载下一页',
      el('button', {
        class: 'btn ghost small', type: 'button', title: '清掉本页缓存重新拉取',
        on: { click: () => reload() },
      }, '↻ 重新加载'),
    ));
  }

  /* ---------- 分类 ---------- */
  function renderCategories() {
    clear(catHost);
    const bar = categoryBar(state.categories, state.category, (token) => {
      if (token === state.category) return;
      state.category = token;
      resetItems();
      requestPage(1, { replace: true });
      renderCategories();
      renderHead();
    });
    if (bar) catHost.append(bar);
  }

  /* ---------- 网格 ---------- */
  function appendCards(items) {
    for (const item of items) grid.append(seriesCard(item));
    activateCovers(grid);
    grid.querySelectorAll('.card.ghost').forEach((node) => node.remove());
  }

  function resetItems() {
    state.items = [];
    state.seen = new Set();
    state.page = 0;
    state.hasMore = true;
    state.total = 0;
    state.error = '';
    state.loading = false;
    clear(grid);
    grid.append(cardsSkeleton());
  }

  /* ---------- 底部状态 ---------- */
  function renderFooter() {
    clear(footer);
    if (state.error) {
      footer.append(
        el('span', { text: `加载失败：${state.error}` }),
        el('button', {
          class: 'btn small danger', type: 'button',
          on: { click: () => requestPage(Math.max(1, state.page + 1 || 1)) },
        }, '重试'),
      );
      return;
    }
    if (state.loading) {
      footer.append(el('div', { class: 'spinner' }),
        el('span', { text: state.page ? `正在翻第 ${state.page + 1} 页…` : '呆猫正在翻书架…' }));
      return;
    }
    if (!state.hasMore && state.items.length) {
      footer.append(el('span', { text: '到底啦，呆猫把这一架翻完了。' }),
        el('span', { class: 'cat-paw' }));
      return;
    }
    const pagePill = el('span', { class: 'page-pill', text: `已加载 ${state.page} 页` });
    const pageInput = el('input', {
      type: 'number',
      min: '1',
      max: '2000',
      value: String((state.page || 0) + 1),
      class: 'range-box',
      title: '输入页码后回车跳转',
      style: 'width:64px',
    });
    const jump = () => {
      const target = Math.max(1, Math.min(2000, parseInt(pageInput.value, 10) || 1));
      requestPage(target, { replace: true });
    };
    pageInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        jump();
      }
    });
    footer.append(
      el('button', {
        class: 'btn small', type: 'button',
        on: { click: () => requestPage(Math.max(1, state.page + 1)) },
      }, '加载下一页'),
      el('span', { text: '页码' }), pageInput,
      el('button', { class: 'btn tiny ghost', type: 'button', on: { click: jump } }, '跳转'),
      pagePill,
    );
  }

  /* ---------- 请求 ---------- */
  async function requestPage(page, options = {}) {
    if (state.loading) return;
    state.loading = true;
    state.error = '';
    renderFooter();
    try {
      const data = await api.browse({ source, page, category: state.category });
      if (!alive()) return;
      state.sourceName = data.source?.name || state.sourceName;
      state.nsfw = Boolean(data.source?.nsfw);
      state.categories = data.categories || state.categories;
      // 分类和列表解耦了：列表先回来，分类稍后单独补一次（不挡首屏）
      if (data.categoriesPending && !state.categoryPending) {
        state.categoryPending = true;
        api.categories(source)
          .then((res) => {
            if (!alive()) return;
            const list = (res && res.categories) || [];
            if (list.length) {
              state.categories = list;
              renderCategories();
            }
          })
          .catch(() => { /* 分类拿不到就算了，不影响浏览 */ })
          .finally(() => { state.categoryPending = false; });
      }
      state.total = data.total || 0;
      state.page = page;
      state.hasMore = Boolean(data.hasMore);
      if (options.replace) {
        // 跳页 / 换分类：只保留这一页，页码才对得上
        state.items = [];
        state.seen = new Set();
      }
      const items = dedupe(state, data.items || []);
      state.items.push(...items);
      if (options.replace) {
        clear(grid);
        appendCards(state.items);
      } else {
        appendCards(items);
      }
      state.loading = false;
      if (!state.items.length) {
        clear(grid);
        grid.append(emptyState('🐈', '这一页空空的', '换一页或者换个图源看看？'));
      }
      renderCategories();
      renderHead();
      renderFooter();
    } catch (error) {
      if (!alive()) return;
      state.loading = false;
      state.error = error.message || String(error);
      renderFooter();
      if (!state.items.length) {
        clear(grid);
        grid.append(emptyState('😿', '这个图源没打开', state.error));
      }
    }
  }

  async function reload() {
    resetItems();
    state.category = '';
    renderCategories();
    await requestPage(1, { replace: true });
  }

  /* ---------- 懒加载触发器 ---------- */
  let observer = null;
  function armObserver() {
    if (observer) observer.disconnect();
    observer = new IntersectionObserver((records) => {
      if (!records.some((record) => record.isIntersecting)) return;
      if (state.loading || !state.hasMore || state.error) return;
      if (App_settings_blockAuto()) return;
      requestPage(Math.max(1, state.page + 1));
    }, { rootMargin: '900px 0px' });
    observer.observe(sentinel);
  }

  const onScroll = rafThrottle(() => {
    if (state.items.length) state.scrollY = window.scrollY;
  });
  window.addEventListener('scroll', onScroll, { passive: true });

  unmountHook = () => {
    observer?.disconnect();
    window.removeEventListener('scroll', onScroll);
    state.scrollY = window.scrollY;
  };

  renderHead();
  renderCategories();
  renderFooter();
  armObserver();

  if (state.items.length) {
    appendCards(state.items);
    renderFooter();
    const target = state.scrollY;
    if (target > 0) {
      requestAnimationFrame(() => requestAnimationFrame(() => window.scrollTo({ top: target })));
    }
  } else {
    grid.append(cardsSkeleton());
    await requestPage(1, { replace: true });
  }
}

function App_settings_blockAuto() {
  try {
    return window.__DUMBCAT_SETTINGS__?.auto_load_more === false;
  } catch {
    return false;
  }
}

export function unmount() {
  if (unmountHook) {
    try { unmountHook(); } catch {}
    unmountHook = null;
  }
}

export function invalidate(source) {
  if (source) states.delete(source);
  else states.clear();
}

export function currentSourceState(source) {
  return states.get(source) || null;
}
