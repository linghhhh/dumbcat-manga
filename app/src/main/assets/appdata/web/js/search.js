/* 全局搜索：一次问所有图源，谁有结果就先显示谁的名字。 */
import { streamSearch } from './api.js';
import { el, clear, toast, emptyState, fmtCount } from './util.js';
import { seriesCard } from './cards.js';
import { activateCovers } from './lazy.js';

let activeStream = null;
let unmountHook = null;

const state = {
  query: '',
  sources: '',
  results: new Map(), // key -> { source, items, error, hasMore, total, done, elapsed }
  order: [],
  started: 0,
  finished: 0,
  activeSource: '',
  dom: null,
};

function reset(query, sources = '') {
  state.query = query;
  state.sources = sources;
  state.results = new Map();
  state.order = [];
  state.started = 0;
  state.finished = 0;
  state.activeSource = '';
  state.dom = null;
}

function visibleSources() {
  if (state.activeSource) {
    const item = state.results.get(state.activeSource);
    return item ? [item] : [];
  }
  return state.order.map((key) => state.results.get(key)).filter(Boolean);
}

export async function render(root, params, ctx = {}) {
  const query = (params.q || '').trim();
  // 从详情页标签点进来时只搜当前图源；普通搜索为空串＝全源
  const sources = String(params.sources || '').split(',').map((item) => item.trim()).filter(Boolean).join(',');
  const alive = ctx.alive || (() => true);
  clear(root);

  if (!query) {
    root.append(el('div', { class: 'search-top' },
      el('div', { class: 'search-query-line' }, el('span', { class: 'q', text: '想找点什么？' })),
    ), emptyState('🔍', '全局搜索', '在顶部输入作品名，呆猫会同时翻遍所有图源；哪家有结果，就先亮哪家的名字。'));
    return;
  }

  if (state.query !== query || state.sources !== sources) reset(query, sources);

  const queryLine = el('div', { class: 'search-query-line' },
    el('span', { class: 'q', text: `“${query}”` }),
    el('span', { class: 'chip', text: sources ? '本图源搜索' : '全局搜索' }),
  );
  const tabs = el('div', { class: 'source-tabs' });
  const status = el('div', { class: 'search-status' }, el('div', { class: 'spinner' }), el('span', { text: '呆猫挨家挨户敲门…' }));
  const sections = el('div');
  root.append(el('div', { class: 'search-top' }, queryLine, tabs, status), sections);

  state.dom = { tabs, status, sections };

  if (activeStream) activeStream.close();
  activeStream = streamSearch(query, sources, {
    onStart(payload) {
      if (!alive()) return;
      state.started = (payload.sources || []).length;
      state.order = (payload.sources || []).map((item) => item.key);
      for (const item of payload.sources || []) {
        state.results.set(item.key, {
          source: item, items: [], error: '', hasMore: false, total: 0, done: false, elapsed: 0,
        });
      }
      renderTabs();
      updateStatus();
    },
    onResult(payload) {
      if (!alive()) return;
      const key = payload.source?.key || '';
      if (!key) return;
      state.results.set(key, {
        source: payload.source,
        items: payload.items || [],
        error: payload.error || '',
        hasMore: Boolean(payload.hasMore),
        total: payload.total || 0,
        done: true,
        elapsed: payload.elapsed || 0,
      });
      state.finished += 1;
      if (!state.order.includes(key)) state.order.push(key);
      renderTabs();
      renderSections();
      updateStatus();
    },
    onDone() {
      if (!alive()) return;
      state.finished = state.started || state.finished;
      renderTabs();
      updateStatus(true);
      const hits = Array.from(state.results.values()).filter((item) => item.items.length).length;
      if (!hits) updateStatus(true);
    },
    onError(error) {
      if (!alive()) return;
      updateStatus(true, error.message);
    },
  });

  unmountHook = () => {
    if (activeStream) {
      activeStream.close();
      activeStream = null;
    }
  };

  renderTabs();
  renderSections();
}

function renderTabs() {
  const { tabs } = state.dom || {};
  if (!tabs) return;
  clear(tabs);
  const totalHits = Array.from(state.results.values()).filter((item) => item.items.length).length;
  const all = el('button', {
    class: `source-tab ${state.activeSource ? '' : 'active'}`,
    type: 'button',
    on: {
      click: () => {
        state.activeSource = '';
        renderTabs();
        renderSections();
      },
    },
  }, `全部图源`, el('span', { class: 'n', text: String(totalHits) }));
  // 只在本图源里搜时，没有"全部"这个概念（列表里就一个源）
  if (!state.sources) tabs.append(all);

  const ordered = [...state.order];
  for (const key of ordered) {
    const item = state.results.get(key);
    if (!item) continue;
    const count = item.items.length;
    const tab = el('button', {
      class: `source-tab ${state.activeSource === key ? 'active' : ''} ${count ? '' : 'no-result'} ${item.error ? 'broken' : ''}`,
      type: 'button',
      title: item.error ? `失败：${item.error}` : `${item.source?.name || key}：${count} 条`,
      dataset: { key },
      on: {
        click: () => {
          if (!count && !item.error) {
            toast(`${item.source?.name || key} 这次没有搜到结果`, { type: 'info' });
          }
          state.activeSource = state.activeSource === key ? '' : key;
          renderTabs();
          renderSections();
        },
      },
    },
      item.error ? el('span', { class: 'x', text: '⚠' }) : null,
      el('span', { text: item.source?.name || key }),
      item.done ? el('span', { class: 'n', text: String(count) }) : null,
    );
    tabs.append(tab);
  }
}

function updateStatus(done = false, errorText = '') {
  const { status } = state.dom || {};
  if (!status) return;
  clear(status);
  const total = state.started || state.results.size;
  const finished = state.finished;
  const hits = Array.from(state.results.values()).filter((item) => item.items.length).length;
  if (errorText) {
    status.append(el('span', { text: `😿 ${errorText}` }));
    return;
  }
  if (!done && finished < total) {
    status.append(el('div', { class: 'spinner' }),
      el('span', { text: `已询问 ${finished}/${total} 个图源，命中 ${hits} 个…` }));
    return;
  }
  const failures = Array.from(state.results.values()).filter((item) => item.error).length;
  status.append(
    el('span', { text: `问了 ${total} 个图源，${hits} 个有结果${failures ? `，${failures} 个没打开` : ''}。点图源名字只看它家的。` }),
  );
  if (hits) {
    status.append(el('span', { class: 'cat-paw' }));
  }
}

function renderSections() {
  const { sections, status } = state.dom || {};
  if (!sections) return;
  clear(sections);
  const visible = visibleSources().filter((item) => item.items.length || item.error);
  if (!visible.length) {
    if (state.finished >= state.started && state.started > 0) {
      clear(sections);
      sections.append(emptyState('🐈‍⬛', '一家都没搜到', `“${state.query}” 换个写法？比如作者名、罗马音或原版标题。`));
    }
    return;
  }
  for (const item of visible) {
    const key = item.source?.key || '';
    const section = el('section', { class: 'search-section' },
      el('div', { class: 'search-section-head' },
        el('h2', {}, el('span', { text: item.source?.name || key })),
        item.source?.nsfw ? el('span', { class: 'badge nsfw', text: '18+' }) : null,
        el('span', { class: 'chip', text: `${item.items.length} 条 · ${item.elapsed.toFixed ? item.elapsed.toFixed(1) : item.elapsed}s` }),
        item.error ? el('span', { class: 'err', text: `加载失败：${item.error}` }) : null,
        item.items.length > 0 ? el('button', {
          class: 'btn tiny ghost more', type: 'button',
          on: { click: () => { state.activeSource = key; renderTabs(); renderSections(); } },
        }, '只看这家 →') : null,
      ),
    );
    if (item.items.length) {
      const grid = el('div', { class: 'grid' });
      for (const series of item.items) grid.append(seriesCard(series, { showSource: false }));
      section.append(grid);
      sections.append(section);
      activateCovers(grid);
    } else {
      section.append(el('div', { class: 'hint', text: `${item.source?.name || key} 这里没有结果（不是报错，就是没搜到）。` }));
      sections.append(section);
    }
  }
  if (state.activeSource) {
    status?.append(el('button', {
      class: 'btn tiny ghost', type: 'button', style: 'margin-left:8px',
      on: { click: () => { state.activeSource = ''; renderTabs(); renderSections(); } },
    }, '看全部图源'));
  }
}

export function unmount() {
  if (unmountHook) {
    try { unmountHook(); } catch {}
    unmountHook = null;
  }
  if (activeStream) {
    activeStream.close();
    activeStream = null;
  }
}
