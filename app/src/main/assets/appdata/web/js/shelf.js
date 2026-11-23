/* 书架：多书架（可自建）、图源收藏同步、浏览历史。 */
import { api } from './api.js';
import * as downloadsTab from './downloads.js';
import { seriesCard } from './cards.js';
import { activateCovers } from './lazy.js';
import {
  el, clear, toast, openModal, closeModal, confirmDialog, promptDialog,
  navigate, emptyState, sectionTitle, timeAgo, coverSrc, loadLocal, saveLocal,
} from './util.js';

const ACTIVE_SHELF_KEY = 'shelf.active';
/* 图源收藏动辄上千部，一次只同步最近几页；这里记住「还有多少没同步」 */

let rootEl = null;
let state = null;

/* ------------------------------ 数据 ------------------------------ */

function emptyStateData() {
  return {
    mode: 'shelf',
    data: null,
    items: [],
    history: [],
    activeShelfId: '',
    loading: true,
    error: '',
    /* 多选模式：批量移出 / 移动 / 置顶 */
    selecting: false,
    selected: new Set(),
    /* 下载缓存标签：由 downloads.js 挂载，切走时整体卸载（含 SSE 退订） */
    dlHost: null,
    dlMounted: false,
    alive: () => true,
  };
}

function activeShelf() {
  if (!state?.data) return null;
  return (state.data.shelves || []).find((item) => item.id === state.activeShelfId) || null;
}

function pickActiveShelf(shelves, wanted) {
  const list = shelves || [];
  if (!list.length) return '';
  const remembered = wanted || loadLocal(ACTIVE_SHELF_KEY, '');
  const hit = list.find((item) => item.id === remembered);
  if (hit) return hit.id;
  return state.data?.defaultShelfId || list[0].id;
}

async function loadContent() {
  if (!state.data) return;
  /* 下载缓存自己拉 /api/downloads，不用书架的接口 */
  if (state.mode === 'downloads') return;
  if (state.mode === 'history') {
    const res = await api.shelfHistory(200);
    state.history = res.items || [];
    return;
  }
  if (!state.activeShelfId) {
    state.items = [];
    return;
  }
  const res = await api.shelfItems(state.activeShelfId);
  state.items = res.items || [];
}

async function reload() {
  try {
    state.data = await api.shelf();
  } catch (error) {
    state.loading = false;
    state.error = error.message || String(error);
    if (rootEl) draw();
    return;
  }
  state.activeShelfId = pickActiveShelf(state.data.shelves, state.activeShelfId);
  try {
    await loadContent();
    state.error = '';
  } catch (error) {
    state.items = [];
    state.history = [];
    state.error = error.message || String(error);
  }
  state.loading = false;
  if (!state.alive()) return;
  draw();
}

/* ------------------------------ 动作 ------------------------------ */

function exitSelect() {
  if (!state) return;
  state.selecting = false;
  state.selected = new Set();
}

/* 标签与地址栏同步：切标签不整页重渲染，只换 hash，刷新/收藏还能落回同一个标签 */
function syncTabHash(mode) {
  const target = mode === 'shelf' ? '#/shelf' : `#/shelf?tab=${encodeURIComponent(mode)}`;
  if (location.hash === target) return;
  try {
    history.replaceState(null, '', target);
  } catch (error) {
    /* file:// 或某些 WebView 不允许改地址，忽略即可，页面状态已经切好了 */
  }
}

function leaveDownloads() {
  if (!state) return;
  downloadsTab.unmount();
  state.dlHost = null;
  state.dlMounted = false;
}

function mountDownloads(host) {
  if (!state || !host || state.dlMounted) return;
  state.dlMounted = true;
  downloadsTab.renderInto(host).catch((error) => {
    if (!state) return;
    state.dlMounted = false;
    toast(error.message || '下载队列没打开', { type: 'error' });
  });
}

async function setMode(mode) {
  if (!state || state.mode === mode) return;
  if (state.mode === 'downloads') leaveDownloads();
  state.mode = mode;
  syncTabHash(mode);
  exitSelect();
  if (mode === 'downloads') {
    // 队列页自己会转菊花再拉数据，不用书架的骨架屏
    state.loading = false;
    if (rootEl) draw();
    return;
  }
  state.loading = true;
  if (rootEl) draw();
  await loadContent();
  state.loading = false;
  if (state.alive()) draw();
}

/* ------------------------------ 动作 ------------------------------ */

async function createShelf() {
  const name = await promptDialog('新建书架', {
    placeholder: '书架名字（留空自动起名）',
    hint: '比如「在看」「想补」「本子」。',
    okText: '创建',
  });
  if (name === null) return;
  try {
    const res = await api.createShelf(name.trim());
    const shelf = res.shelf;
    toast(`书架「${shelf.name}」建好了`, { type: 'ok' });
    if (shelf?.id) state.activeShelfId = shelf.id;
    state.mode = 'shelf';
    syncTabHash('shelf');
    state.loading = true;
    draw();
    await reload();
  } catch (error) {
    toast(error.message || '新建失败', { type: 'error' });
  }
}

async function renameShelf(shelf) {
  if (!shelf) return;
  if (shelf.kind === 'source') {
    toast('图源书架的名字跟账号收藏一致，不用改', { type: 'info' });
    return;
  }
  const name = await promptDialog('重命名书架', { value: shelf.name, okText: '保存' });
  if (!name || name.trim() === shelf.name) return;
  try {
    await api.renameShelf(shelf.id, name.trim());
    await reload();
  } catch (error) {
    toast(error.message || '改名失败', { type: 'error' });
  }
}

async function deleteShelf(shelf) {
  if (!shelf) return;
  if (shelf.kind === 'source') {
    toast('图源书架不能删，可以先退出图源账号', { type: 'info' });
    return;
  }
  const ok = await confirmDialog('删除书架？', `「${shelf.name}」里的 ${shelf.count || 0} 本收藏会一起移除（不会删掉图源里的收藏）。`, {
    danger: true, okText: '删除',
  });
  if (!ok) return;
  try {
    await api.deleteShelf(shelf.id);
    state.activeShelfId = '';
    toast('书架已删除', { type: 'ok' });
    await reload();
  } catch (error) {
    toast(error.message || '删除失败', { type: 'error' });
  }
}

function pickShelf(shelfId) {
  state.activeShelfId = shelfId;
  saveLocal(ACTIVE_SHELF_KEY, shelfId);
  state.loading = true;
  if (rootEl) draw();
  loadContent()
    .catch((error) => toast(error.message || '载入失败', { type: 'error' }))
    .finally(() => {
      state.loading = false;
      if (state.alive()) draw();
    });
}

async function removeItem(item) {
  const ok = await confirmDialog('移出书架？', `确定把「${item.title || '这本'}」移出当前书架吗？`, { danger: true, okText: '移出' });
  if (!ok) return;
  try {
    const res = await api.shelfRemove(item.shelfId || state.activeShelfId, item.uid);
    if (res.remote === 'kept') {
      toast('已移出书架（别的书架里还有，图源收藏没动）', { type: 'ok' });
    } else if (res.remote === 'synced') {
      toast('已移出书架，图源收藏也取消了', { type: 'ok' });
    } else if (res.remote === 'failed') {
      toast('本地已移出，图源收藏没同步成功', { type: 'warn' });
    } else {
      toast('已移出书架', { type: 'ok' });
    }
    await reload();
  } catch (error) {
    toast(error.message || '移除失败', { type: 'error' });
  }
}
/* ------------------------------ 渲染 ------------------------------ */

function headEl() {
  if (state.mode === 'downloads') {
    // 标题区跟着标签走：下载缓存不再顶着「书架」大标题，也不重复解释
    return sectionTitle('下载缓存');
  }
  if (state.mode === 'history') {
    const count = state.data?.historyCount || 0;
    return sectionTitle('浏览历史', count ? `最近看过的 ${count} 部作品` : '看过的作品会记在这里');
  }
  return sectionTitle('书架', '多书架 / 图源收藏 / 浏览历史',
    el('button', { class: 'btn primary', type: 'button', on: { click: createShelf } }, '＋ 新建书架'));
}

function segmentEl() {
  const seg = el('div', { class: 'shelf-seg' });
  const mk = (mode, icon, ...kids) => el('button', {
    class: `shelf-seg-btn${state.mode === mode ? ' active' : ''}`,
    type: 'button',
    on: { click: () => setMode(mode) },
  }, el('span', { class: 'shelf-seg-icon', text: icon }), ...kids);
  const count = state.data?.historyCount || 0;
  seg.append(
    mk('shelf', '📚', '书架'),
    mk('history', '🕘', '浏览历史',
      count ? el('i', { class: 'shelf-seg-badge', text: count > 99 ? '99+' : String(count) }) : null),
    mk('downloads', '📥', '下载缓存'),
  );
  return seg;
}

function accountsEl() {
  const sources = (state.data?.sources || []).filter((item) => item.supportsFavorites);
  const logged = sources.filter((item) => item.loggedIn);
  const box = el('div', { class: 'shelf-accounts' });
  if (!logged.length) {
    box.append(
      el('p', { class: 'hint', text: '在「图源管理」里登录图源账号，就能查看它的收藏，挑着加进书架。' }),
      el('button', {
        class: 'btn ghost small',
        type: 'button',
        on: { click: () => navigate('#/sources') },
      }, '去图源管理'),
    );
    return box;
  }
  for (const source of logged) {
    box.append(el('div', { class: 'shelf-acc on' },
      el('div', { class: 'shelf-acc-main' },
        el('div', { class: 'shelf-acc-name' },
          el('span', { class: 'shelf-acc-dot' }),
          source.name,
        ),
        el('div', { class: 'shelf-acc-status', text: `已登录${source.username ? ` · ${source.username}` : ''} · 收藏由你自己挑` }),
      ),
      el('button', {
        class: 'btn primary small',
        type: 'button',
        on: { click: () => navigate(`#/favorites?source=${encodeURIComponent(source.key)}`) },
      }, '查看收藏'),
    ));
  }
  return box;
}

function tabsEl() {
  const row = el('div', { class: 'shelf-tabs' });
  for (const shelf of state.data?.shelves || []) {
    const active = shelf.id === state.activeShelfId;
    row.append(el('button', {
      class: `shelf-tab${active ? ' active' : ''}`,
      type: 'button',
      title: shelf.kind === 'source' ? '图源收藏架' : '本地书架',
      on: {
        click: () => pickShelf(shelf.id),
      },
    },
      el('span', { class: 'shelf-tab-icon', text: shelf.kind === 'source' ? '🔗' : '📚' }),
      el('span', { class: 'shelf-tab-name', text: shelf.name }),
      el('span', { class: 'shelf-tab-count', text: String(shelf.count || 0) }),
    ));
  }
  row.append(el('button', { class: 'shelf-tab add', type: 'button', on: { click: createShelf } }, '＋ 新书架'));
  return row;
}

function gridEl() {
  const shelf = activeShelf();
  const wrap = el('section', { class: 'shelf-panel' });
  wrap.append(el('div', { class: 'shelf-bar' },
    el('div', { class: 'shelf-bar-main' },
      el('strong', { text: shelf ? shelf.name : '书架' }),
      el('span', { class: 'shelf-bar-count', text: `${state.items.length} 本` }),
      shelf && shelf.kind === 'source' ? el('span', { class: 'shelf-acc-tag', text: '跟图源收藏同步' }) : null,
    ),
    shelf && shelf.kind === 'source'
      ? el('div', { class: 'shelf-bar-tools' },
        el('button', {
          class: 'btn tiny ghost',
          type: 'button',
          title: '从图源再拉一次收藏；连不上时书架里的旧条目仍然能打开',
          on: { click: (event) => syncSourceShelf(shelf, event.currentTarget) },
        }, '同步收藏'),
      )
      : null,
    shelf && shelf.kind !== 'source'
      ? el('div', { class: 'shelf-bar-tools' },
        el('button', {
          class: `btn tiny ${state.selecting ? 'primary' : 'ghost'}`,
          type: 'button',
          on: {
            click: () => {
              if (state.selecting) exitSelect();
              else state.selecting = true;
              draw();
            },
          },
        }, state.selecting ? '完成' : '选择'),
        el('button', { class: 'icon-btn', type: 'button', title: '重命名', on: { click: () => renameShelf(shelf) } }, '✏️'),
        el('button', { class: 'icon-btn', type: 'button', title: '删除书架', on: { click: () => deleteShelf(shelf) } }, '🗑️'),
      )
      : null,
  ));

  if (state.loading) {
    wrap.append(el('div', { class: 'shelf-skeleton' }, ...Array.from({ length: 6 }, () => el('div', { class: 'skeleton-card' }))));
    return wrap;
  }

  if (!state.items.length) {
    wrap.append(emptyState('📚', '这个书架还是空的', '打开作品页点「收藏」就能放进来；想去图源账号的收藏里挑，去「图源管理」。',
      el('button', { class: 'btn ghost', type: 'button', on: { click: () => navigate('#/browse') } }, '去发现作品')));
    return wrap;
  }

  const grid = el('div', { class: 'grid shelf-grid' });
  for (const item of state.items) {
    const card = seriesCard({
      uid: item.uid,
      title: item.title,
      coverUrl: item.coverUrl || item.cover,
      author: item.author,
      tags: item.tags,
      nsfw: item.nsfw,
      providerName: item.providerName || item.key,
    }, { coverWidth: 260, showSource: true, onOpen: () => navigate(`#/series/${item.uid}`) });
    const holder = el('div', {
      class: `shelf-item${state.selecting && state.selected.has(item.uid) ? ' selected' : ''}`,
    }, card);
    if (state.selecting) {
      holder.append(el('span', {
        class: 'shelf-check',
        text: state.selected.has(item.uid) ? '✓' : '',
      }));
      holder.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        toggleSelected(item.uid);
      }, true);
    } else {
      /* 长按进多选：手机上比找按钮快 */
      let pressTimer = 0;
      holder.addEventListener('touchstart', () => {
        pressTimer = setTimeout(() => {
          state.selecting = true;
          state.selected.add(item.uid);
          draw();
        }, 480);
      }, { passive: true });
      const cancelPress = () => clearTimeout(pressTimer);
      holder.addEventListener('touchmove', cancelPress, { passive: true });
      holder.addEventListener('touchend', cancelPress);
      holder.addEventListener('touchcancel', cancelPress);
    }
    if (Number(item.pin || 0) > 0) {
      holder.append(el('span', {
        class: 'chip',
        style: 'position:absolute;right:8px;top:8px;font-size:10px;pointer-events:none',
        title: '已置顶',
      }, '📌'));
    }
    const cached = Number(item.chapterCount || (Array.isArray(item.chapters) ? item.chapters.length : 0));
    if (cached) {
      holder.append(el('div', {
        class: 'chip',
        style: 'position:absolute;left:8px;bottom:8px;font-size:10px;pointer-events:none',
        title: '书架里存了章节快照，断网或图源挂掉时也能打开',
      }, `已存 ${cached} 章`));
    }
    holder.append(el('button', {
      class: 'shelf-item-x',
      type: 'button',
      title: '移出书架',
      on: {
        click: (event) => {
          event.preventDefault();
          event.stopPropagation();
          removeItem(item);
        },
      },
    }, '×'));
    grid.append(holder);
  }
  wrap.append(grid);
  if (state.selecting) wrap.append(selectBarEl(shelf));
  requestAnimationFrame(() => activateCovers(grid));
  return wrap;
}

/* 多选操作条：置顶 / 移动 / 移出 */
function selectBarEl(shelf) {
  const count = state.selected.size;
  const allChecked = count > 0 && count === state.items.length;
  return el('div', { class: 'shelf-select-bar' },
    el('button', {
      class: 'btn tiny ghost', type: 'button',
      on: {
        click: () => {
          if (allChecked) state.selected.clear();
          else state.selected = new Set(state.items.map((item) => item.uid));
          draw();
        },
      },
    }, allChecked ? '取消全选' : '全选'),
    el('span', { class: 'shelf-select-count', text: `已选 ${count} 本` }),
    el('div', { class: 'shelf-select-actions' },
      el('button', {
        class: 'btn tiny ghost', type: 'button', disabled: count ? null : true,
        on: { click: () => bulkAction('pin') },
      }, '置顶'),
      el('button', {
        class: 'btn tiny ghost', type: 'button', disabled: count ? null : true,
        on: { click: () => chooseShelfToMove() },
      }, '移动'),
      el('button', {
        class: 'btn tiny danger', type: 'button', disabled: count ? null : true,
        on: { click: () => bulkAction('remove') },
      }, '移出'),
    ),
  );
}

function toggleSelected(uid) {
  if (!state) return;
  if (state.selected.has(uid)) state.selected.delete(uid);
  else state.selected.add(uid);
  draw();
}

async function bulkAction(action, targetShelfId = '') {
  const uids = Array.from(state.selected);
  if (!uids.length) {
    toast('先选几本吧', { type: 'warn' });
    return;
  }
  if (action === 'remove') {
    const ok = await confirmDialog('移出书架？', `把选中的 ${uids.length} 本移出「${activeShelf()?.name || '书架'}」？图源收藏不会被取消。`, { danger: true, okText: '移出' });
    if (!ok) return;
  }
  try {
    const res = await api.shelfBulk({ shelfId: state.activeShelfId, uids, action, targetShelfId });
    const labels = { remove: '移出', move: '移动', pin: '置顶', unpin: '取消置顶' };
    toast(`已${labels[action] || action} ${res.count || uids.length} 本`, { type: 'ok' });
    exitSelect();
    await reload();
  } catch (error) {
    toast('操作没成功', { type: 'error', detail: error.message });
  }
}

/* 移动：弹层里挑目标书架 */
function chooseShelfToMove() {
  const shelves = (state.data?.shelves || []).filter((item) => item.id !== state.activeShelfId && item.kind !== 'source');
  if (!shelves.length) {
    toast('没有别的书架可以移过去', { type: 'warn' });
    return;
  }
  const list = el('div', { class: 'shelf-move-list' });
  const box = el('div', { style: 'min-width:260px' },
    el('h3', { style: 'margin:0 0 12px', text: '移到哪个书架' }),
    list,
  );
  openModal(box);
  for (const shelf of shelves) {
    list.append(el('button', {
      class: 'btn ghost', type: 'button', style: 'width:100%;justify-content:space-between',
      on: {
        click: () => {
          closeModal();
          bulkAction('move', shelf.id);
        },
      },
    },
      el('span', { text: shelf.name }),
      el('span', { class: 'hint', text: `${shelf.count || 0} 本` }),
    ));
  }
}

function historyEl() {
  const wrap = el('section', { class: 'shelf-panel' });
  wrap.append(el('div', { class: 'shelf-bar' },
    el('div', { class: 'shelf-bar-main' },
      el('strong', { text: '浏览历史' }),
      el('span', { class: 'shelf-bar-count', text: `${state.history.length} 条` }),
    ),
    state.history.length
      ? el('button', { class: 'btn ghost small', type: 'button', on: { click: clearHistory } }, '清空历史')
      : null,
  ));

  if (state.loading) {
    wrap.append(el('div', { class: 'shelf-skeleton' }, ...Array.from({ length: 5 }, () => el('div', { class: 'skeleton-row' }))));
    return wrap;
  }
  if (!state.history.length) {
    wrap.append(emptyState('🕘', '还没有浏览记录', '看过的章节会出现在这里，点一下就能接着看。',
      el('button', { class: 'btn ghost', type: 'button', on: { click: () => navigate('#/browse') } }, '去发现作品')));
    return wrap;
  }

  const list = el('div', { class: 'hist-list' });
  for (const item of state.history) {
    const cover = coverSrc(item.coverUrl || item.cover || '', 160);
    const percent = item.total ? Math.min(100, Math.round(((item.page || 0) / item.total) * 100)) : 0;
    const row = el('button', {
      class: 'hist-row',
      type: 'button',
      on: {
        click: () => {
          if (item.chapterUid) navigate(`#/read/${encodeURIComponent(item.chapterUid)}`);
          else navigate(`#/series/${item.uid}`);
        },
      },
    },
      el('div', { class: `hist-cover${cover ? '' : ' blank'}` }, cover ? el('img', { src: cover, alt: '', loading: 'lazy' }) : null),
      el('div', { class: 'hist-main' },
        el('div', { class: 'hist-title', text: item.title || '未命名作品' }),
        el('div', { class: 'hist-sub', text: [item.chapterTitle, item.total ? `${(item.page || 0) + 1}/${item.total} 页` : ''].filter(Boolean).join(' · ') }),
        el('div', { class: 'hist-meta' },
          el('span', { text: timeAgo(item.ts) }),
        ),
      ),
      el('div', { class: 'hist-progress', style: `--p:${percent}%` }),
    );
    row.append(el('span', {
      class: 'hist-x',
      role: 'button',
      title: '从历史里删掉',
      on: {
        click: (event) => {
          event.preventDefault();
          event.stopPropagation();
          removeHistory(item);
        },
      },
    }, '×'));
    list.append(row);
  }
  wrap.append(list);
  return wrap;
}

/* 源收藏架：手动再同步一次；图源连不上就只提示，不影响已存的条目 */
async function syncSourceShelf(shelf, button) {
  const source = shelf.source || String(shelf.id || '').replace(/^s_source_/, '');
  if (!source) {
    toast('这个书架没有记录图源', { type: 'warn' });
    return;
  }
  const label = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = '同步中…';
  }
  try {
    const res = await api.shelfSync(source, 2);
    const count = Number(res?.added ?? res?.total ?? 0);
    toast(count ? `已更新 ${count} 本` : '没有新的收藏', { type: 'ok' });
    await reload();
  } catch (error) {
    toast('这次没同步上，书架里的条目还能打开', { type: 'warn', detail: error.message });
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = label || '同步收藏';
    }
  }
}

async function clearHistory() {
  const ok = await confirmDialog('清空浏览历史？', '只清掉这台手机上的阅读进度记录，作品和图源收藏不受影响。', { danger: true, okText: '清空' });
  if (!ok) return;
  try {
    await api.shelfHistoryClear();
    state.history = [];
    toast('历史已清空', { type: 'ok' });
    if (state.alive()) draw();
  } catch (error) {
    toast(error.message || '清空失败', { type: 'error' });
  }
}

async function removeHistory(item) {
  try {
    await api.shelfHistoryRemove(item.uid);
    state.history = state.history.filter((row) => row.uid !== item.uid);
    if (state.alive()) draw();
  } catch (error) {
    toast(error.message || '删除失败', { type: 'error' });
  }
}

function draw() {
  if (!rootEl || !state) return;
  if (!state.data && state.error && state.mode !== 'downloads') {
    clear(rootEl);
    rootEl.append(headEl(), el('div', { class: 'empty-state' },
      el('div', { class: 'big', text: '😿' }),
      el('strong', { text: '书架没打开' }),
      el('div', { text: state.error }),
      el('button', { class: 'btn primary', type: 'button', on: { click: () => { state.loading = true; state.error = ''; draw(); reload(); } } }, '再试一次'),
    ));
    return;
  }

  clear(rootEl);
  rootEl.append(headEl(), segmentEl());
  if (state.mode === 'downloads') {
    // 先有容器再挂模块：下载队列自己管加载 / 重绘 / SSE
    if (!state.dlHost) state.dlHost = el('section', { class: 'shelf-panel dl-tab' });
    rootEl.append(state.dlHost);
    mountDownloads(state.dlHost);
    return;
  }
  if (state.data) {
    if (state.mode === 'history') {
      rootEl.append(historyEl());
    } else {
      rootEl.append(accountsEl(), tabsEl(), gridEl());
    }
  } else {
    rootEl.append(el('div', { class: 'shelf-skeleton' }, ...Array.from({ length: 4 }, () => el('div', { class: 'skeleton-row' }))));
  }
}


/* ------------------------------ 详情页：收藏到书架 ------------------------------ */

const REMOTE_TEXT = {
  synced: '已同步到图源收藏',
  unsupported: '这个图源没有官方收藏，只存本机',
  'not-logged-in': '只存本机（没登录图源账号，没推回官方收藏）',
  failed: '只存本机（图源收藏没同步成功）',
  kept: '别的书架里还有，图源收藏没动',
  '': '只存本机',
};

function pickerRow(shelf, checked) {
  const input = el('input', { type: 'checkbox', class: 'picker-check' });
  input.checked = Boolean(checked);
  const row = el('label', { class: 'picker-row' },
    input,
    el('span', { class: 'picker-icon', text: shelf.kind === 'source' ? '🔗' : '📚' }),
    el('span', { class: 'picker-name', text: shelf.name }),
    el('span', { class: 'picker-count', text: String(shelf.count || 0) }),
  );
  row.dataset.shelfId = shelf.id;
  return row;
}

/** 收藏弹窗：选书架（可现场新建）。返回 { saved } 或 null（取消）。 */
export async function openShelfPicker(series, snapshot = {}) {
  const uid = series && series.uid;
  if (!uid) {
    toast('这本书没有 uid，收不进书架', { type: 'error' });
    return null;
  }
  let data;
  let status;
  try {
    [data, status] = await Promise.all([api.shelf(), api.shelfStatus(uid)]);
  } catch (error) {
    toast(error.message || '书架读取失败', { type: 'error' });
    return null;
  }
  const shelves = data.shelves || [];
  const savedIds = new Set((status.shelves || []).map((item) => item.id));
  const sourceKey = series.provider || series.key || '';
  const sourceInfo = (data.sources || []).find((item) => item.key === sourceKey);

  const listBox = el('div', { class: 'picker-list' });
  const hint = el('p', { class: 'hint', text: '' });
  const confirm = el('button', { class: 'btn primary', type: 'button' }, '保存');

  const fillList = (list) => {
    clear(listBox);
    if (!list.length) {
      listBox.append(el('p', { class: 'hint', text: '还没有书架，先建一个吧。' }));
      return;
    }
    const defaultId = data.defaultShelfId || list[0].id;
    for (const shelf of list) {
      const checked = savedIds.has(shelf.id) || (!savedIds.size && shelf.id === defaultId);
      listBox.append(pickerRow(shelf, checked));
    }
  };
  fillList(shelves);

  if (sourceInfo && sourceInfo.supportsFavorites) {
    hint.textContent = sourceInfo.loggedIn
      ? '已登录图源账号，在这里收藏会同时推回官方收藏夹。'
      : '没登录图源账号：收藏只存在本机，登录后可以再同步。';
  } else if (sourceInfo) {
    hint.textContent = '这个图源没有官方收藏，收藏只存在本机书架。';
  }

  // 弹层是单例：这里不能再用 promptDialog（会把选书架的界面顶掉），改成弹层内联输入。
  const newName = el('input', { type: 'text', placeholder: '新书架名字（可留空）', style: 'flex:1;min-width:0' });
  const createBtn = el('button', { class: 'btn ghost small', type: 'button' }, '＋ 新建');
  let creating = false;
  createBtn.addEventListener('click', async () => {
    if (creating) return;
    creating = true;
    createBtn.disabled = true;
    try {
      const res = await api.createShelf(newName.value.trim());
      const created = res.shelf;
      shelves.push(created);
      data.shelves = shelves;
      newName.value = '';
      fillList(shelves);
      const row = listBox.querySelector(`.picker-row[data-shelf-id="${created.id}"] input`);
      if (row) row.checked = true;
    } catch (error) {
      toast(error.message || '新建失败', { type: 'error' });
    } finally {
      creating = false;
      createBtn.disabled = false;
    }
  });

  let busy = false;
  confirm.addEventListener('click', async () => {
    if (busy) return;
    busy = true;
    confirm.disabled = true;
    confirm.textContent = '保存中…';
    const wanted = [];
    const dropped = [];
    for (const row of listBox.querySelectorAll('.picker-row')) {
      const input = row.querySelector('input');
      if (!input) continue;
      const id = row.dataset.shelfId;
      if (input.checked && !savedIds.has(id)) wanted.push(id);
      if (!input.checked && savedIds.has(id)) dropped.push(id);
    }
    if (!wanted.length && !dropped.length) {
      closeModal();
      return;
    }
    const remotes = [];
    try {
      for (const shelfId of wanted) {
        const res = await api.shelfAdd({
          shelfId,
          uid,
          source: sourceKey,
          series,
          chapters: snapshot.chapters || [],
          summary: snapshot.summary || {},
        });
        remotes.push(res.remote || '');
      }
      for (const shelfId of dropped) {
        const res = await api.shelfRemove(shelfId, uid);
        remotes.push(res.remote || '');
      }
    } catch (error) {
      busy = false;
      confirm.disabled = false;
      confirm.textContent = '保存';
      toast(error.message || '保存失败', { type: 'error' });
      return;
    }
    closeModal();
    const remote = remotes.includes('synced') ? 'synced'
      : remotes.includes('failed') ? 'failed'
        : remotes.includes('not-logged-in') ? 'not-logged-in'
          : remotes.includes('kept') ? 'kept' : (remotes[0] || '');
    toast('书架已更新', { type: 'ok', detail: REMOTE_TEXT[remote] || '' });
    window.dispatchEvent(new CustomEvent('dumbcat:shelf'));
  });

  openModal(el('div', { class: 'shelf-picker' },
    el('h2', { text: '收藏到书架' }),
    el('p', { class: 'hint', text: series.title || '这本书' }),
    listBox,
    hint,
    el('div', { class: 'picker-foot' }, newName, createBtn),
    el('div', { class: 'modal-actions' },
      el('button', { class: 'btn ghost', type: 'button', on: { click: () => closeModal() } }, '取消'),
      confirm,
    ),
  ), { className: 'modal-picker' });
  return { saved: true };
}

/* ------------------------------ 路由入口 ------------------------------ */


export async function render(view, params = {}, ctx = {}) {
  rootEl = view;
  state = emptyStateData();
  const tab = String(params.tab || '').trim();
  state.mode = tab === 'history' || tab === 'downloads' ? tab : 'shelf';
  state.alive = ctx.alive || (() => true);
  draw();
  await reload();
}

export function unmount() {
  leaveDownloads();
  rootEl = null;
  state = null;
}

export { state as _state };
