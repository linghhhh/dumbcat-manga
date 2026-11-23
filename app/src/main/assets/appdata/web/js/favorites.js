/* 图源收藏：列出某个已登录图源的官方收藏，由用户自己挑着加进书架。
   书架不再自动同步整份收藏，也不在书架里放登录框——登录统一在「图源管理」。 */
import { api } from './api.js';
import { el, clear, toast, emptyState, coverSrc, navigate } from './util.js';
import { openShelfPicker } from './shelf.js';

const state = {
  source: '',
  name: '',
  items: [],
  truncated: false,
  remoteTotal: 0,
  loading: false,
  error: '',
  added: new Set(),
  alive: true,
};

let rootEl = null;
let unmountHook = null;

export async function render(root, params = {}, ctx = {}) {
  rootEl = root;
  state.alive = () => (ctx.alive ? ctx.alive() : true);
  state.source = (params.source || querySource() || '').trim();
  state.error = '';
  state.loading = false;
  state.added = new Set();
  clear(root);
  draw();
  if (!state.source) {
    state.error = '缺少图源标识';
    draw();
    return;
  }
  await load(false);
}

export function unmount() {
  state.alive = () => false;
  if (typeof unmountHook === 'function') unmountHook();
  unmountHook = null;
  rootEl = null;
}

function querySource() {
  const raw = (location.hash.split('?')[1] || '');
  const value = new URLSearchParams(raw).get('source') || '';
  return value;
}

async function load(more) {
  if (state.loading) return;
  state.loading = true;
  state.error = '';
  draw();
  try {
    const data = await api.sourceFavorites(state.source, more ? 40 : 0);
    const items = Array.isArray(data.items) ? data.items : [];
    const seen = new Set(more ? state.items.map((item) => item.uid) : []);
    if (!more) state.items = [];
    for (const item of items) {
      if (!item || !item.uid || seen.has(item.uid)) continue;
      seen.add(item.uid);
      state.items.push(item);
    }
    state.name = data.name || state.name || state.source;
    state.truncated = Boolean(data.truncated);
    state.remoteTotal = Number(data.remoteTotal || 0);
  } catch (error) {
    state.error = error.message || String(error);
  }
  state.loading = false;
  if (!state.alive()) return;
  draw();
}

function draw() {
  const root = rootEl;
  if (!root) return;
  clear(root);
  const page = el('div', { class: 'favorites-page' });
  page.append(el('div', { class: 'view-head' },
    el('div', {},
      el('div', { class: 'sub', text: '这些是图源账号里的收藏，挑想看的加进书架就行。' }),
      el('h1', {}, el('span', { class: 'stroke', text: `${state.name || '图源'}的收藏` })),
    ),
    el('div', { class: 'head-actions' },
      el('button', { class: 'btn ghost small', type: 'button', on: { click: () => navigate('#/shelf') } }, '← 返回书架'),
      el('button', {
        class: 'btn small', type: 'button',
        on: { click: () => load(false) },
      }, state.loading ? '读取中…' : '↻ 重新读取'),
    ),
  ));

  if (state.error) {
    page.append(el('div', { class: 'panel' },
      el('p', { class: 'hint', text: state.error }),
      el('button', {
        class: 'btn ghost small', type: 'button',
        on: { click: () => { location.hash = '#/sources'; } },
      }, '去图源管理登录'),
    ));
  } else if (state.loading && !state.items.length) {
    page.append(el('div', { class: 'load-bar', style: 'padding:60px 0' },
      el('div', { class: 'spinner' }), el('span', { text: '正在读图源的收藏…' })));
  } else if (!state.items.length) {
    page.append(emptyState('📭', '这个账号还没有收藏', '在图源 App 里收藏几部，再回来刷新。'));
  } else {
    page.append(el('p', { class: 'hint', text: summaryText() }));
    const list = el('div', { class: 'fav-list' });
    for (const item of state.items) list.append(row(item));
    page.append(list);
    if (state.truncated) {
      page.append(el('div', { class: 'fav-more' },
        el('button', {
          class: 'btn ghost small', type: 'button',
          on: { click: () => load(true) },
        }, '再多读一些（会更慢）'),
      ));
    }
  }
  root.append(page);
}

function summaryText() {
  const bits = [`共读到 ${state.items.length} 部`];
  if (state.remoteTotal && state.remoteTotal > state.items.length) {
    bits.push(`图源里一共 ${state.remoteTotal} 部，先读最近这些`);
  }
  return bits.join('，') + '。';
}

function row(item) {
  const added = state.added.has(item.uid);
  const button = el('button', {
    class: `btn small${added ? ' ghost' : ''}`,
    type: 'button',
    on: {
      click: async () => {
        if (state.added.has(item.uid)) return;
        button.disabled = true;
        const picked = await openShelfPicker(item);
        button.disabled = false;
        if (picked) {
          state.added.add(item.uid);
          toast(`已加入书架：${item.title || ''}`, { type: 'ok' });
          if (state.alive()) draw();
        }
      },
    },
  }, added ? '已加入' : '＋ 加入书架');
  return el('div', { class: `fav-row${added ? ' added' : ''}` },
    el('img', { class: 'fav-cover', src: coverSrc(item.coverUrl || '', 160), alt: '', loading: 'lazy' }),
    el('div', { class: 'fav-main' },
      el('div', { class: 'fav-title', text: item.title || '未命名作品' }),
      el('div', { class: 'fav-meta', text: [item.author, item.total ? `${item.total} 话` : ''].filter(Boolean).join(' · ') }),
    ),
    button,
  );
}
