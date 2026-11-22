/* 书架：多书架（可自建）、图源收藏同步、浏览历史。 */
import { api } from './api.js';
import { seriesCard } from './cards.js';
import { activateCovers } from './lazy.js';
import {
  el, clear, toast, openModal, closeModal, confirmDialog, promptDialog,
  navigate, emptyState, sectionTitle, timeAgo, coverSrc, loadLocal, saveLocal,
} from './util.js';

const ACTIVE_SHELF_KEY = 'shelf.active';
/* 图源收藏动辄上千部，一次只同步最近几页；这里记住「还有多少没同步」 */
const SYNC_TRUNC_KEY = 'shelf.synctrunc';

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
    syncKey: '',
    autoKey: '',
    syncTruncated: loadLocal(SYNC_TRUNC_KEY, {}) || {},
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
  autoSyncIfNeeded();
}

/* 图源账号已登录但收藏架是空的 / 太旧 → 后台自动同步一次。
   需求：「用户如果登录自己的图源账号，自动获取图源的收藏」。 */
async function autoSyncIfNeeded() {
  if (state.mode !== 'shelf' || state.autoKey || state.syncKey) return;
  const source = (state.data?.sources || []).find((item) => item.loggedIn && item.supportsFavorites);
  if (!source) return;
  const shelf = (state.data?.shelves || []).find((item) => item.kind === 'source' && item.source === source.key);
  const stale = !shelf || !shelf.updated || (Date.now() / 1000 - shelf.updated > 3600);
  if (!stale) return;
  state.autoKey = source.key;
  if (state.alive()) draw();
  try {
    await api.shelfSync(source.key);
  } catch {
    /* 自动同步失败不打扰用户，手动同步时再报错 */
  }
  state.autoKey = '';
  if (!state.alive()) return;
  await reload();
}

/* ------------------------------ 动作 ------------------------------ */

async function setMode(mode) {
  if (state.mode === mode) return;
  state.mode = mode;
  state.loading = true;
  if (rootEl) draw();
  await loadContent();
  state.loading = false;
  if (state.alive()) draw();
}

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

async function syncSource(source, pages = 0) {
  if (!source || state.syncKey) return;
  state.syncKey = source.key;
  if (rootEl) draw();
  try {
    const res = await api.shelfSync(source.key, pages);
    const bits = [];
    if (res.total) bits.push(`共 ${res.total} 本`);
    if (res.added) bits.push(`新增 ${res.added}`);
    if (res.removed) bits.push(`移除 ${res.removed}`);
    toast(`「${source.name}」收藏已同步${bits.length ? `：${bits.join('，')}` : ''}`, { type: 'ok' });
    if (res.truncated) {
      state.syncTruncated[source.key] = {
        synced: res.total || 0,
        remote: res.remoteTotal || 0,
        pages: res.pages || pages || 5,
      };
    } else {
      delete state.syncTruncated[source.key];
    }
    saveLocal(SYNC_TRUNC_KEY, state.syncTruncated);
  } catch (error) {
    toast(error.message || '同步失败', { type: 'error' });
  }
  state.syncKey = '';
  if (state.alive()) await reload();
}

async function logoutSource(source) {
  const ok = await confirmDialog('退出登录？', `退出「${source.name}」后，这个图源的收藏架不会再自动更新。已同步到本机的作品还在。`, {
    danger: true, okText: '退出',
  });
  if (!ok) return;
  try {
    await api.logoutSource(source.key);
    toast('已退出登录', { type: 'ok' });
    await reload();
  } catch (error) {
    toast(error.message || '退出失败', { type: 'error' });
  }
}

function openLogin(source) {
  const account = el('input', {
    type: 'text',
    placeholder: '账号 / 邮箱',
    style: 'width:100%',
    attrs: { autocapitalize: 'off', autocomplete: 'username', spellcheck: 'false' },
  });
  const password = el('input', {
    type: 'password',
    placeholder: '密码',
    style: 'width:100%',
    attrs: { autocomplete: 'current-password' },
  });
  const hint = el('p', { class: 'hint', text: '账号只保存在这台手机上，不会打进安装包。' });
  const submit = el('button', { class: 'btn primary', type: 'button' }, '登录');
  let working = false;

  const doLogin = async () => {
    if (working) return;
    const user = account.value.trim();
    const pass = password.value;
    if (!user || !pass) {
      hint.textContent = '账号和密码都要填。';
      return;
    }
    working = true;
    submit.disabled = true;
    hint.textContent = '正在登录…';
    try {
      await api.loginPassword(source.key, user, pass);
      closeModal();
      toast('登录成功，正在拉取收藏…', { type: 'ok' });
      state.syncKey = source.key;
      if (state.alive()) draw();
      try {
        const res = await api.shelfSync(source.key);
        toast(`收藏已同步：共 ${res.total || 0} 本`, { type: 'ok' });
      } catch (error) {
        toast(`登录成功，收藏同步失败：${error.message || '网络问题'}`, { type: 'warn' });
      }
      state.syncKey = '';
      await reload();
    } catch (error) {
      hint.textContent = error.message || '登录失败，检查账号密码再试。';
      submit.disabled = false;
    } finally {
      working = false;
    }
  };

  submit.addEventListener('click', doLogin);
  const onKey = (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      doLogin();
    }
  };
  account.addEventListener('keydown', onKey);
  password.addEventListener('keydown', onKey);

  openModal(el('div', { class: 'shelf-login' },
    el('h2', { text: `登录${source.name}` }),
    el('p', { class: 'hint', text: '登录后会把这个图源账号的收藏收进书架；在这里收藏的作品也会推回图源收藏夹。' }),
    el('div', { class: 'field' }, el('label', { text: '账号' }), account),
    el('div', { class: 'field' }, el('label', { text: '密码' }), password),
    hint,
    el('div', { class: 'modal-actions' },
      el('button', { class: 'btn ghost', type: 'button', on: { click: () => closeModal() } }, '取消'),
      submit,
    ),
  ), { className: 'modal-login', onClose: () => {} });
  setTimeout(() => account.focus(), 60);
}

/* ------------------------------ 渲染 ------------------------------ */

function headEl() {
  return sectionTitle('书架', '多书架 / 图源收藏 / 浏览历史',
    el('button', { class: 'btn primary', type: 'button', on: { click: createShelf } }, '＋ 新建书架'));
}

function segmentEl() {
  const seg = el('div', { class: 'shelf-seg' });
  const mk = (mode, label, icon) => el('button', {
    class: `shelf-seg-btn${state.mode === mode ? ' active' : ''}`,
    type: 'button',
    on: { click: () => setMode(mode) },
  }, el('span', { class: 'shelf-seg-icon', text: icon }), label);
  const count = state.data?.historyCount || 0;
  seg.append(mk('shelf', '书架', '📚'), mk('history', `浏览历史${count ? ` · ${count}` : ''}`, '🕘'));
  return seg;
}

function accountsEl() {
  const sources = state.data?.sources || [];
  const box = el('div', { class: 'shelf-accounts' });
  if (!sources.length) {
    box.append(el('p', { class: 'hint', text: '当前没有支持官方收藏的图源。收藏会只保存在本机书架里。' }));
    return box;
  }
  for (const source of sources) {
    const syncing = state.syncKey === source.key || state.autoKey === source.key;
    const trunc = (state.syncTruncated || {})[source.key];
    const status = syncing
      ? '正在同步收藏…'
      : source.loggedIn
        ? `已登录${source.username ? ` · ${source.username}` : ''}`
        : '未登录 · 登录后自动拉取该图源收藏';
    const actions = el('div', { class: 'shelf-acc-actions' });
    if (source.loggedIn) {
      actions.append(
        el('button', {
          class: 'btn ghost small',
          type: 'button',
          on: { click: () => syncSource(source) },
        }, syncing ? '同步中…' : '同步收藏'),
        el('button', { class: 'btn ghost small danger', type: 'button', on: { click: () => logoutSource(source) } }, '退出登录'),
      );
      if (trunc && trunc.remote > trunc.synced) {
        actions.append(el('button', {
          class: 'btn ghost small',
          type: 'button',
          on: { click: () => syncSource(source, Math.min(40, (trunc.pages || 5) + 10)) },
        }, `继续同步（还有 ${trunc.remote - trunc.synced} 部）`));
      }
    } else {
      actions.append(el('button', { class: 'btn primary small', type: 'button', on: { click: () => openLogin(source) } }, '登录'));
    }
    box.append(el('div', { class: `shelf-acc${source.loggedIn ? ' on' : ''}` },
      el('div', { class: 'shelf-acc-main' },
        el('div', { class: 'shelf-acc-name' },
          el('span', { class: 'shelf-acc-dot' }),
          source.name,
          source.supportsFavorites ? el('span', { class: 'shelf-acc-tag', text: '官方收藏' }) : null,
        ),
        el('div', { class: 'shelf-acc-status', text: status }),
        trunc && trunc.remote > trunc.synced
          ? el('div', { class: 'shelf-acc-status', text: `已同步最近 ${trunc.synced} 部，图源共 ${trunc.remote} 部` })
          : null,
      ),
      actions,
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
    shelf && shelf.kind !== 'source'
      ? el('div', { class: 'shelf-bar-tools' },
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
    wrap.append(emptyState('📚', '这个书架还是空的', '打开作品页点「收藏」就能放进来；登录图源账号后，收藏架会自动装满。',
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
    const holder = el('div', { class: 'shelf-item' }, card);
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
  requestAnimationFrame(() => activateCovers(grid));
  return wrap;
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
  if (!rootEl) return;
  if (!state.data && state.error) {
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
export async function openShelfPicker(series) {
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
        const res = await api.shelfAdd({ shelfId, uid, source: sourceKey, series });
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
  state.mode = params.tab === 'history' ? 'history' : 'shelf';
  state.alive = ctx.alive || (() => true);
  draw();
  await reload();
}

export function unmount() {
  rootEl = null;
  state = null;
}

export { state as _state };
