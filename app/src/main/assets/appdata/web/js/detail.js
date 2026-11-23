/* 作品详情：封面信息、章节勾选（Ctrl+A / Shift 区间 / 话号范围）、下载与阅读。 */
import { api, withToken } from './api.js';
import {
  el, clear, toast, fmtCount, confirmDialog, coverSrc,
} from './util.js';
import { openDownloadDialog } from './downloads.js';
import { openShelfPicker } from './shelf.js';
import { navigate } from './util.js';

let state = null;
let rootRef = null;
let unmountHook = null;
/* 章节列表一次全渲染：几百话的作品也不用再点「再显示 300 章」。 */
const ALL_CHAPTERS = 100000;

export async function render(root, params, ctx = {}) {
  const uid = params.uid;
  const alive = ctx.alive || (() => true);
  rootRef = root;
  clear(root);
  root.append(el('div', { class: 'load-bar', style: 'padding:90px 0' },
    el('div', { class: 'spinner' }), el('span', { text: '呆猫去拿这本书的资料…' })));

  let data;
  try {
    data = await api.series(uid);
  } catch (error) {
    if (!alive()) return;
    clear(root);
    root.append(el('div', { class: 'empty-state' },
      el('div', { class: 'big', text: '😿' }),
      el('strong', { text: '这本书打不开了' }),
      el('div', { text: error.message }),
      el('button', {
        class: 'btn primary', type: 'button',
        on: { click: () => navigate('#/browse') },
      }, '回书架'),
    ));
    return;
  }
  if (!alive()) return;

  const series = data.series || {};
  state = {
    uid: series.uid || uid,
    series,
    chapters: data.chapters || [],
    selected: new Set(),
    filter: '',
    rendered: ALL_CHAPTERS,
    lastIndex: null,
    allSelected: false,
    pickMode: false,
    saved: false,
  };

  draw();
  bindKeys();
  refreshShelfState();
}

/* ------------------------------ 全量绘制 ------------------------------ */
function isMobileLayout() {
  return window.matchMedia('(max-width: 900px)').matches;
}

function draw() {
  const root = rootRef;
  clear(root);

  const batchBox = toolbarEl();
  batchBox.id = 'batchBox';
  batchBox.classList.add('dtl-batch');
  batchBox.hidden = true;

  root.append(
    bannerEl(),
    chipsEl(),
    introEl(),
    likeSection(),
    el('section', { class: 'dtl-sec' },
      el('div', { class: 'dtl-sec-head' },
        el('h2', { text: '目录' }),
        el('span', { class: 'dtl-sec-sub', text: `共 ${state.chapters.length} 话` }),
        el('span', { class: 'spacer' }),
        el('button', {
          class: 'btn small ghost', id: 'batchToggle', type: 'button',
          on: { click: () => togglePickMode() },
        }, '📥 下载'),
      ),
      batchBox,
      listEl(),
    ),
    actionBar(),
    el('div', { class: 'dtl-spacer' }),
  );
  refreshToolbar();
}

/* 顶部大图：封面做背景 + 渐变压暗 + 标题/统计 */
function bannerEl() {
  const series = state.series;
  const bg = el('img', { class: 'dtl-bg', alt: '', loading: 'eager' });
  if (series.coverUrl) bg.src = withToken(coverSrc(series.coverUrl, 900));
  const stats = [
    `${state.chapters.length} 话`,
    series.status || '',
    series.providerName || series.provider || '',
  ].filter(Boolean).join(' · ');
  return el('header', { class: 'dtl-banner' },
    bg,
    el('span', { class: 'dtl-bgmask' }),
    el('div', { class: 'dtl-banner-text' },
      el('h1', { text: series.title || '未命名作品' }),
      el('div', { class: 'dtl-stat', text: stats }),
    ),
  );
}

/* 作者 / 标签 / 复制标题 */
function chipsEl() {
  const series = state.series;
  return el('div', { class: 'dtl-chips' },
    series.author ? el('span', { class: 'chip', text: `✍ ${series.author}` }) : null,
    series.nsfw ? el('span', { class: 'chip', text: '18+' }) : null,
    ...(series.tags || []).slice(0, 12).map((tag) => el('span', {
      class: 'chip tag', text: `# ${tag}`,
      on: {
        // 标签搜索只在本图源内进行，避免跳到全源结果让用户重新找
        click: () => navigate(`#/search?q=${encodeURIComponent(tag)}&sources=${encodeURIComponent(series.provider || '')}`),
      },
    })),
    el('span', { class: 'spacer' }),
    el('button', {
      class: 'btn tiny ghost', type: 'button',
      on: {
        click: async () => {
          await navigator.clipboard?.writeText(series.title || '').then(
            () => toast('标题已复制', { type: 'ok' }),
            () => toast('复制失败', { type: 'error' }),
          );
        },
      },
    }, '复制标题'),
  );
}

/* 简介：默认三行，够长才给「加载更多」 */
function introEl() {
  const text = String(state.series.summary || '').trim();
  if (!text) return el('div', { hidden: true });
  const para = el('p', { class: 'dtl-intro clamp', text });
  const more = el('button', {
    class: 'dtl-more', type: 'button',
    on: {
      click: () => {
        const clamped = para.classList.toggle('clamp');
        more.textContent = clamped ? '加载更多' : '收起';
      },
    },
  }, '加载更多');
  const wrap = el('div', { class: 'dtl-intro-wrap' }, para);
  if (text.length > 90) wrap.append(more);
  return wrap;
}

/* 猜你喜欢：同图源其它作品，横向滚动 */
function likeSection() {
  const section = el('section', { class: 'dtl-sec dtl-like', hidden: true });
  const row = el('div', { class: 'dtl-like-row' });
  section.append(
    el('div', { class: 'dtl-sec-head' }, el('h2', { text: '猜你喜欢' }), el('span', { class: 'spacer' })),
    row,
  );
  const provider = state.series.provider;
  if (!provider) return section;
  api.browse({ source: provider, page: 1 }).then((data) => {
    if (!state) return;
    const items = (data.items || []).filter((item) => item && item.uid && item.uid !== state.uid).slice(0, 14);
    if (!items.length) return;
    clear(row);
    for (const item of items) {
      row.append(el('a', {
        class: 'dtl-like-card', href: `#/series/${item.uid}`,
        on: {
          click: (event) => {
            event.preventDefault();
            navigate(`#/series/${item.uid}`);
          },
        },
      },
        el('img', { src: withToken(coverSrc(item.coverUrl, 260)), alt: '', loading: 'lazy' }),
        el('span', { class: 'dtl-like-title', text: item.title || '' }),
      ));
    }
    section.hidden = false;
  }).catch(() => {});
  return section;
}

/* 底部操作条：下载 / 收藏 / 开始阅读 */
function actionBar() {
  const shelfBtn = el('button', {
    class: `btn ghost shelf-btn${state.saved ? ' on' : ''}`,
    id: 'barShelfBtn', type: 'button',
    on: { click: () => shelfAction() },
  }, state.saved ? '★ 已收藏' : '☆ 收藏');
  return el('div', { class: 'dtl-actionbar' },
    el('button', {
      class: 'btn ghost', id: 'barDownloadBtn', type: 'button',
      on: { click: () => togglePickMode() },
    }, '📥 下载'),
    shelfBtn,
    el('button', {
      class: 'btn primary', id: 'barReadBtn', type: 'button',
      on: { click: () => readFirst() },
    }, '开始阅读'),
  );
}

/* 收藏状态：查一下这本书在哪些书架里，按钮显示实心/空心 */
async function refreshShelfState() {
  if (!state) return;
  try {
    const res = await api.shelfStatus(state.uid);
    state.saved = Boolean(res.saved);
  } catch {
    state.saved = false;
  }
  paintShelfBtn();
}

function paintShelfBtn() {
  const button = document.getElementById('barShelfBtn');
  if (!button) return;
  button.classList.toggle('on', Boolean(state && state.saved));
  button.textContent = state && state.saved ? '★ 已收藏' : '☆ 收藏';
}

async function shelfAction() {
  if (!state) return;
  const res = await openShelfPicker(state.series);
  if (res) await refreshShelfState();
}

/* 下载模式：只有点了「下载」才展开勾选与批量按钮 */
function togglePickMode(force) {
  state.pickMode = typeof force === 'boolean' ? force : !state.pickMode;
  const box = document.getElementById('batchBox');
  if (box) box.hidden = !state.pickMode;
  const toggle = document.getElementById('batchToggle');
  if (toggle) toggle.textContent = state.pickMode ? '收起' : '📥 下载';
  if (!state.pickMode) {
    state.selected.clear();
    state.lastIndex = null;
    state.allSelected = false;
  }
  refreshToolbar();
  renderList();
}

/* ------------------------------ 章节工具栏 ------------------------------ */
function toolbarEl() {
  const rangeInput = el('input', {
    type: 'text',
    placeholder: '话号范围，如 1-20,25',
    style: 'width:170px',
  });
  rangeInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      selectByRange(rangeInput.value);
    }
  });

  const filterInput = el('input', {
    type: 'text', placeholder: '筛选章节名…', style: 'width:150px',
  });
  filterInput.addEventListener('input', () => {
    state.filter = filterInput.value.trim().toLowerCase();
    state.rendered = ALL_CHAPTERS;
    renderList();
  });

  return el('div', { class: 'chapter-toolbar', id: 'chapterToolbar' },
    el('button', {
      class: 'btn small', id: 'selectAllBtn', type: 'button',
      on: { click: () => toggleSelectAll() },
    }, '全选（Ctrl+A）'),
    el('button', {
      class: 'btn small ghost', type: 'button',
      on: {
        click: () => {
          state.selected.clear();
          state.lastIndex = null;
          refreshToolbar();
          renderList();
        },
      },
    }, '清空选择'),
    el('button', {
      class: 'btn small ghost', type: 'button',
      on: { click: () => invertSelection() },
    }, '反选'),
    rangeInput,
    el('button', {
      class: 'btn small ghost', type: 'button',
      on: { click: () => selectByRange(rangeInput.value) },
    }, '按话号选'),
    filterInput,
    el('span', { class: 'spacer' }),
    el('span', { class: 'sel-count', id: 'selCount', text: '已选 0 章' }),
  );
}

function refreshToolbar() {
  const count = state.selected.size;
  const label = document.getElementById('selCount');
  if (label) label.textContent = `已选 ${count} / ${state.chapters.length} 章`;
  const all = document.getElementById('selectAllBtn');
  if (all) all.textContent = state.allSelected ? '取消全选（Ctrl+A）' : '全选（Ctrl+A）';
  const downloadBtn = document.getElementById('downloadSelBtn');
  if (downloadBtn) {
    downloadBtn.textContent = count ? `📥 下载选中（${count}）` : '📥 下载选中';
    downloadBtn.disabled = count === 0;
  }
  const bar = document.getElementById('barDownloadBtn');
  if (bar) {
    if (state.pickMode) {
      bar.textContent = count ? `📥 下载选中（${count}）` : '选好章节再下载';
      bar.disabled = count === 0;
      bar.classList.add('primary');
      bar.classList.remove('ghost');
    } else {
      bar.textContent = '📥 下载';
      bar.disabled = false;
      bar.classList.remove('primary');
      bar.classList.add('ghost');
    }
  }
}

/* ------------------------------ 章节列表 ------------------------------ */
function chaptersVisible() {
  let list = state.chapters;
  if (state.filter) {
    list = list.filter((chapter) => (chapter.title || '').toLowerCase().includes(state.filter));
  }
  return list;
}

function listEl() {
  const wrap = el('div', { id: 'chapterListHost' });
  requestAnimationFrame(() => renderListInto(wrap));
  return wrap;
}

function renderList() {
  const host = document.getElementById('chapterListHost');
  if (host) renderListInto(host);
}

function renderListInto(host) {
  if (!host || !state) return;
  clear(host);
  const list = chaptersVisible();
  if (!list.length) {
    host.append(el('div', { class: 'empty-state', style: 'padding:36px' },
      el('strong', { text: state.filter ? '没有匹配的章节' : '这本书还没有章节' }),
      el('div', { text: state.filter ? '换个关键词试试。' : '可能是图源还没放出，或者需要登录。' }),
    ));
    return;
  }
  const limit = Math.min(list.length, state.rendered);
  const container = el('div', { class: 'chapter-list' });
  for (let index = 0; index < limit; index += 1) {
    container.append(chapterRow(list[index], index));
  }
  host.append(container);
  if (limit < list.length) {
    host.append(el('div', { class: 'load-bar' },
      el('button', {
        class: 'btn small', type: 'button',
        on: {
          click: (event) => {
            state.rendered += 300;
            renderList();
            event.target.closest('.load-bar')?.remove();
          },
        },
      }, `再显示 300 章（还有 ${list.length - limit}）`),
    ));
  }
}

function chapterRow(chapter, index) {
  const locked = Boolean(chapter.locked);
  const selected = state.selected.has(chapter.uid);
  const row = el('div', {
    class: `chapter-row ${selected ? 'selected' : ''} ${locked ? 'locked' : ''} ${state.pickMode ? 'picking' : ''}`,
    dataset: { index: String(index), uid: chapter.uid },
    title: locked ? '这个章节被锁住了（可能需要登录或付费）' : (chapter.title || ''),
    on: {
      click: (event) => {
        if (locked) {
          toast('这章被锁住了，下不了也看不了', { type: 'warn' });
          return;
        }
        if (!state.pickMode) {
          openReader(chapter);
          return;
        }
        if (event.shiftKey && state.lastIndex !== null) {
          const [from, to] = [state.lastIndex, index].sort((a, b) => a - b);
          const list = chaptersVisible();
          const target = !selected;
          for (let i = from; i <= to; i += 1) {
            const item = list[i];
            if (!item || item.locked) continue;
            if (target) state.selected.add(item.uid);
            else state.selected.delete(item.uid);
          }
        } else {
          if (selected) state.selected.delete(chapter.uid);
          else state.selected.add(chapter.uid);
          state.lastIndex = index;
        }
        state.allSelected = state.selected.size > 0
          && state.selected.size >= state.chapters.filter((item) => !item.locked).length;
        refreshToolbar();
        const node = document.querySelector(`.chapter-row[data-index="${index}"]`);
        if (node) node.classList.toggle('selected', state.selected.has(chapter.uid));
      },
      dblclick: () => {
        if (!locked) openReader(chapter);
      },
    },
  },
    state.pickMode ? el('span', { class: 'box', text: selected ? '✓' : '' }) : null,
    el('span', { class: 'name', text: chapter.title || `第 ${chapter.number || index + 1} 话` }),
    el('span', { class: 'num', text: chapter.number ? `#${chapter.number}` : '' }),
    el('span', { class: 'go' },
      locked ? el('span', { class: 'chip', text: '🔒 锁定' })
        : el('button', {
          class: 'btn tiny ghost', type: 'button',
          on: {
            click: (event) => {
              event.stopPropagation();
              openReader(chapter);
            },
          },
        }, '阅读')),
  );
  return row;
}

/* ------------------------------ 选择逻辑 ------------------------------ */
function unlockedChapters() {
  return chaptersVisible().filter((chapter) => !chapter.locked);
}

function toggleSelectAll() {
  const list = unlockedChapters();
  const allSelected = list.length > 0 && list.every((chapter) => state.selected.has(chapter.uid));
  if (allSelected) {
    state.selected.clear();
    state.allSelected = false;
  } else {
    for (const chapter of list) state.selected.add(chapter.uid);
    state.allSelected = true;
  }
  state.lastIndex = null;
  refreshToolbar();
  renderList();
}

function selectAllUnlocked(value) {
  const list = state.chapters.filter((chapter) => !chapter.locked);
  if (value) {
    state.selected = new Set(list.map((chapter) => chapter.uid));
    state.allSelected = true;
  } else {
    state.selected.clear();
    state.allSelected = false;
  }
  refreshToolbar();
  renderList();
}

function invertSelection() {
  const list = state.chapters.filter((chapter) => !chapter.locked);
  const next = new Set();
  for (const chapter of list) if (!state.selected.has(chapter.uid)) next.add(chapter.uid);
  state.selected = next;
  state.allSelected = next.size > 0 && next.size === list.length;
  refreshToolbar();
  renderList();
}

function numberValue(chapter) {
  const raw = String(chapter.number ?? '').replace(/[^\d.]/g, '');
  const value = parseFloat(raw);
  return Number.isFinite(value) ? value : null;
}

function selectByRange(text) {
  const spec = String(text || '').trim();
  if (!spec) {
    toast('先输入话号，比如 1-20,25', { type: 'info' });
    return;
  }
  const ranges = [];
  for (const part of spec.split(/[,，\s]+/)) {
    if (!part) continue;
    const match = part.match(/^(\d+(?:\.\d+)?)\s*[-~到]\s*(\d+(?:\.\d+)?)$/);
    if (match) {
      const [a, b] = [parseFloat(match[1]), parseFloat(match[2])].sort((x, y) => x - y);
      ranges.push([a, b]);
    } else if (/^\d+(?:\.\d+)?$/.test(part)) {
      const value = parseFloat(part);
      ranges.push([value, value]);
    }
  }
  if (!ranges.length) {
    toast('格式看不懂，用 1-20,25 这样写', { type: 'error' });
    return;
  }
  let hit = 0;
  for (const chapter of state.chapters) {
    if (chapter.locked) continue;
    const value = numberValue(chapter);
    if (value === null) continue;
    if (ranges.some(([from, to]) => value >= from && value <= to)) {
      state.selected.add(chapter.uid);
      hit += 1;
    }
  }
  state.allSelected = false;
  refreshToolbar();
  renderList();
  toast(hit ? `按话号选中 ${hit} 章` : '没有话号落在这些范围里', { type: hit ? 'ok' : 'warn' });
}

/* ------------------------------ 动作 ------------------------------ */
function readFirst() {
  const list = state.chapters.filter((chapter) => !chapter.locked);
  if (!list.length) {
    toast('这本书还没有能看的章节', { type: 'error' });
    return;
  }
  // 后端可配置新章在前；开始阅读取最早的一章
  openReader(list[list.length - 1]);
}

function openReader(chapter) {
  if (!chapter || !chapter.uid) return;
  navigate(`#/read/${chapter.uid}`);
}

function openDownloadForSelection() {
  if (!state.selected.size) {
    toast('先勾选要下载的章节吧', { type: 'info' });
    return;
  }
  const settings = window.__DUMBCAT_SETTINGS__ || {};
  openDownloadDialog({
    series: state.series,
    selectedUids: Array.from(state.selected),
    defaultDir: settings.output_dir || '',
    workers: settings.workers || 4,
  });
}

async function refreshChapters(button) {
  if (button) {
    button.disabled = true;
    button.textContent = '刷新中…';
  }
  try {
    const data = await api.refreshChapters(state.uid);
    state.chapters = data.chapters || [];
    state.rendered = ALL_CHAPTERS;
    state.selected = new Set();
    state.allSelected = false;
    draw();
    toast(`章节已更新：${state.chapters.length} 章`, { type: 'ok' });
  } catch (error) {
    toast('刷新章节失败', { type: 'error', detail: error.message });
    if (button) {
      button.disabled = false;
      button.textContent = '↻ 刷新章节';
    }
  }
}

/* ------------------------------ 快捷键 ------------------------------ */
function bindKeys() {
  const onKey = (event) => {
    const tag = document.activeElement?.tagName || '';
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(tag) || document.activeElement?.isContentEditable;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'a' && !typing) {
      event.preventDefault();
      toggleSelectAll();
    }
    if (event.key === 'Escape' && state.selected.size && !document.body.classList.contains('reading')) {
      // 不抢 Esc：留给弹窗关闭；这里只在没有弹窗时清空选择
      if (document.getElementById('modalRoot')?.hidden !== false) {
        state.selected.clear();
        state.allSelected = false;
        refreshToolbar();
        renderList();
      }
    }
  };
  document.addEventListener('keydown', onKey);
  unmountHook = () => document.removeEventListener('keydown', onKey);
}

export function unmount() {
  if (unmountHook) {
    try { unmountHook(); } catch {}
    unmountHook = null;
  }
  state = null;
  rootRef = null;
}
