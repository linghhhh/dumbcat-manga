/* 下载：目录选择弹窗 + 队列页（SSE 实时进度）。 */
import { api } from './api.js';
import {
  el, clear, toast, openModal, closeModal, sectionTitle, emptyState,
  fmtBytes, timeAgo, confirmDialog, loadLocal, saveLocal,
} from './util.js';

const CHAPTER_TEXT = {
  pending: '未开始',
  fetching: '取图列表',
  queued: '排队中',
  running: '下载中',
  done: '已完成',
  partial: '有失败',
  failed: '全部失败',
  skipped: '已存在',
  locked: '付费跳过',
  empty: '没有图片',
  cancelled: '已取消',
};

/* 记住哪些任务的章节明细是展开的：队列每次刷新都会重绘，展开状态不能被重置。 */
const openChapters = new Set();

const STATE_TEXT = {
  queued: '排队中',
  running: '下载中',
  done: '已完成',
  cancelled: '已取消',
  error: '失败',
};

let unsubscribe = null;
let currentRoot = null;
let data = { tasks: [], history: [], stats: {} };

/* ------------------------------ 目标目录选择 ------------------------------ */
export async function chooseFolder(initial) {
  try {
    const result = await api.chooseFolder(initial || '');
    if (result && result.path) return result.path;
    if (result && result.cancelled) return '';
    return '';
  } catch (error) {
    toast('打开目录选择器失败', { type: 'error', detail: error.message });
    return '';
  }
}

function rememberOutputDir(path) {
  const value = String(path || '').trim();
  if (!value) return;
  const saved = loadLocal('recentOutputDirs', []);
  const list = (Array.isArray(saved) ? saved : [])
    .filter((item) => typeof item === 'string' && item && item !== value);
  list.unshift(value);
  saveLocal('recentOutputDirs', list.slice(0, 8));
}

export async function openDownloadDialog(options = {}) {
  const series = options.series || {};
  const selected = (options.selectedUids || []).slice();
  let folder = options.outputDir || loadLocal('outputDir', '') || (options.defaultDir || '');
  let workers = Number(options.workers) || 4;

  const folderInput = el('input', {
    type: 'text',
    value: folder,
    placeholder: 'E:\\manga\\yuanban 或别的目录',
    spellcheck: 'false',
  });
  const workersInput = el('input', {
    type: 'number', min: '1', max: '16', value: String(workers), style: 'width:90px',
  });

  const recentBox = el('div', { class: 'row', style: 'flex-wrap:wrap;gap:6px;margin-top:6px' });
  const pickButton = el('button', { class: 'btn small', type: 'button' }, '📁 选择目录…');
  const startButton = el('button', { class: 'btn primary', type: 'button' }, `开始下载 ${selected.length} 章`);

  pickButton.addEventListener('click', async () => {
    pickButton.disabled = true;
    pickButton.textContent = '正在打开…';
    const picked = await chooseFolder(folderInput.value.trim());
    pickButton.disabled = false;
    pickButton.textContent = '📁 选择目录…';
    if (picked) {
      folderInput.value = picked;
      folder = picked;
      saveLocal('outputDir', picked);
      rememberOutputDir(picked);
    }
  });

  const body = openModal(el('div', {},
    el('h2', { text: '下载到哪儿？' }),
    el('p', { class: 'hint' },
      `《${series.title || '未命名作品'}》 · 已选 ${selected.length} 章。呆猫会按「作品名 / 章节名」建好文件夹。`),
    el('div', { class: 'field' },
      el('label', { text: '保存目录' }),
      el('div', { class: 'row' }, folderInput, pickButton),
      recentBox,
    ),
    el('div', { class: 'field', style: 'max-width:220px' },
      el('label', { text: '同时下载的图片线程（越大越快，也越容易被站点限速）' }),
      workersInput,
    ),
    el('div', { class: 'modal-actions' },
      el('button', { class: 'btn ghost', type: 'button', on: { click: () => closeModal() } }, '先不下载'),
      startButton,
    ),
  ));

  // 最近用过的下载目录（本地历史，不拿输出目录里的作品文件夹充数）
  const recentList = loadLocal('recentOutputDirs', []);
  const recent = (Array.isArray(recentList) ? recentList : [])
    .filter((item) => typeof item === 'string' && item && item !== folder);
  for (const path of recent.slice(0, 5)) {
    const label = path.split(/[\\/]/).filter(Boolean).pop() || path;
    recentBox.append(el('button', {
      class: 'chip', type: 'button', title: path,
      on: {
        click: () => {
          folderInput.value = path;
          folder = path;
          saveLocal('outputDir', path);
        },
      },
    }, `📂 ${label}`));
  }

  startButton.addEventListener('click', async () => {
    const outputDir = folderInput.value.trim();
    if (!outputDir) {
      toast('先选一个保存目录吧', { type: 'error' });
      folderInput.focus();
      return;
    }
    if (!selected.length) {
      toast('还没选章节呢', { type: 'error' });
      return;
    }
    startButton.disabled = true;
    startButton.textContent = '正在排进队列…';
    try {
      const result = await api.download({
        seriesUid: series.uid,
        chapterUids: selected,
        outputDir,
        workers: Math.max(1, Math.min(16, parseInt(workersInput.value, 10) || 4)),
      });
      saveLocal('outputDir', outputDir);
      rememberOutputDir(outputDir);
      toast(`已加入下载队列（${selected.length} 章）`, { type: 'ok' });
      closeModal();
      window.dispatchEvent(new CustomEvent('dumbcat:downloads-refresh'));
      if (options.goToQueue !== false) {
        setTimeout(() => { location.hash = '#/downloads'; }, 350);
      }
    } catch (error) {
      toast('加入下载队列失败', { type: 'error', detail: error.message });
    } finally {
      startButton.disabled = false;
      startButton.textContent = `开始下载 ${selected.length} 章`;
    }
  });

  setTimeout(() => folderInput.focus(), 40);
}

/* ------------------------------ 逐章明细 ------------------------------ */
function chapterRow(item) {
  const state = String(item.state || 'pending');
  const pages = Number(item.pages) || 0;
  const done = (Number(item.downloaded) || 0) + (Number(item.skipped) || 0);
  const failed = Number(item.failed) || 0;
  const label = CHAPTER_TEXT[state] || state;
  let detail = '';
  if (failed) detail = `${done}/${pages || '?'} 页 · 失败 ${failed}`;
  else if (state === 'locked') detail = '付费章节';
  else if (state === 'empty') detail = '站点没给图';
  else if (state === 'pending') detail = '等待中';
  else if (state === 'fetching') detail = '读取图集…';
  else if (pages) detail = `${pages} 页`;

  // 站点给的标题常常就是「第 52 话」，和左侧的「第 52 章」重复，只在真有副标题时才显示。
  const name = String(item.title || '').trim();
  const showTitle = name && name !== String(item.label || '').trim()
    && !/^第\s*[0-9零一二三四五六七八九十百千]+\s*[章话集回卷节]/.test(name);

  return el('li', { class: `dl-chapter ${state}`, title: item.title || '' },
    el('i', { class: 'dot' }),
    el('span', { class: 'label', text: item.label || '章节' }),
    showTitle ? el('span', { class: 'ctitle', text: name }) : null,
    el('span', { class: 'stat', text: detail ? `${label} · ${detail}` : label }),
  );
}

function chapterBlock(task) {
  const list = Array.isArray(task.chapterList) ? task.chapterList : [];
  if (!list.length) return null;
  const failedList = list.filter((item) => item.state === 'failed' || item.state === 'partial');
  const settled = list.filter((item) => ['done', 'partial', 'failed', 'skipped', 'locked', 'empty'].includes(item.state)).length;
  const open = openChapters.has(task.id) || (failedList.length > 0 && task.state !== 'running');
  const summary = [
    `章节明细 ${settled}/${list.length}`,
    failedList.length ? `失败 ${failedList.length} 章` : '',
  ].filter(Boolean).join(' · ');

  return el('details', {
    class: 'dl-chapters',
    attrs: open ? { open: true } : {},
    on: {
      toggle: (event) => {
        if (event.target.open) openChapters.add(task.id);
        else openChapters.delete(task.id);
      },
    },
  },
    el('summary', {}, el('span', { text: summary })),
    el('ul', { class: 'dl-chapter-list' },
      ...(failedList.length
        ? [...failedList, ...list.filter((item) => !failedList.includes(item))]
        : list).map(chapterRow)),
  );
}

/* 历史里也带章节明细：哪一章失败，直接在这里看 */
function historyRow(task) {
  const list = Array.isArray(task.chapterList) ? task.chapterList : [];
  const failedList = Array.isArray(task.failedChapters) ? task.failedChapters.filter(Boolean) : [];
  const count = task.chapters || list.length || 0;
  return el('div', { class: 'hist-row' },
    el('div', { class: 'line', style: 'justify-content:space-between' },
      el('span', {},
        el('span', { class: `state ${task.state}`, text: STATE_TEXT[task.state] || task.state }),
        el('strong', { text: ` ${task.title || '未命名'}` }),
        el('span', { style: 'color:var(--ink-soft)', text: `（${count} 章）` }),
        failedList.length
          ? el('span', { class: 'hist-fail', text: ` ${failedList.length} 章节失败` })
          : null,
      ),
      el('span', { style: 'color:var(--ink-soft)', text: timeAgo(task.finishedAt || task.finished_at || task.created_at) }),
    ),
    chapterBlock(task),
  );
}

/* ------------------------------ 队列页 ------------------------------ */
function taskCard(task) {
  const percent = Math.max(0, Math.min(100, Number(task.percent) || 0));
  const stateText = STATE_TEXT[task.state] || task.state;
  const card = el('div', { class: 'dl-card', dataset: { id: task.id } },
    el('div', { class: 'head' },
      el('strong', { text: task.title || '未命名作品' }),
      el('span', { class: `state ${task.state}`, text: stateText }),
      el('span', { class: 'chip', text: task.providerName || task.provider || '' }),
      el('span', { style: 'margin-left:auto;font-size:11px;color:var(--ink-soft);font-family:var(--mono)' },
        task.state === 'done' ? timeAgo(task.finishedAt || task.finished_at) : `${percent}%`),
    ),
    el('div', { class: 'progress' }, el('i', { style: `width:${percent}%` })),
    el('div', { class: 'line' },
      el('span', { text: `${task.value || 0} / ${task.total || 0}` }),
      task.downloaded ? el('span', { text: `新下 ${task.downloaded}` }) : null,
      task.skipped ? el('span', { text: `跳过 ${task.skipped}` }) : null,
      task.failed ? el('span', { style: 'color:var(--danger)' }, `失败 ${task.failed}`) : null,
      task.locked ? el('span', { text: `锁定 ${task.locked}` }) : null,
      el('span', { text: `共 ${task.chapters || (task.chapterList || []).length || (task.chapterUids || []).length} 章` }),
    ),
    task.message ? el('div', { class: 'msg', text: task.message, title: task.message }) : null,
    chapterBlock(task),
    el('div', { class: 'line' },
      el('span', { style: 'max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap', title: task.finalDir || task.outputDir },
        `📂 ${task.finalDir || task.outputDir || ''}`),
      el('span', { style: 'margin-left:auto;display:flex;gap:6px' },
        task.outputDir || task.finalDir ? el('button', {
          class: 'btn tiny ghost', type: 'button',
          on: {
            click: async () => {
              try {
                await api.openFolder(task.finalDir || task.outputDir);
              } catch (error) {
                toast('打不开目录', { type: 'error', detail: error.message });
              }
            },
          },
        }, '打开目录') : null,
        task.state === 'running' || task.state === 'queued' ? el('button', {
          class: 'btn tiny danger', type: 'button',
          on: {
            click: async () => {
              if (!(await confirmDialog('取消下载？', '已经下好的图片会留在原处。', { danger: true, okText: '取消任务' }))) return;
              try {
                await api.cancelDownload(task.id);
                toast('已请求取消', { type: 'warn' });
              } catch (error) {
                toast('取消失败', { type: 'error', detail: error.message });
              }
            },
          },
        }, '取消') : null,
      ),
    ),
  );
  return card;
}

function renderList(root) {
  if (!root) return;
  clear(root);
  const active = (data.tasks || []).filter((task) => task.state === 'running' || task.state === 'queued');
  root.append(
    el('div', { class: 'view-head' },
      el('div', {},
        el('div', { class: 'sub', text: active.length ? `${active.length} 个任务正在进行` : '队列空闲中' }),
        el('h1', {}, el('span', { class: 'stroke', text: '下载' })),
      ),
      el('div', { class: 'head-actions' },
        el('button', {
          class: 'btn ghost small', type: 'button',
          on: { click: () => refresh() },
        }, '↻ 刷新'),
        el('button', {
          class: 'btn ghost small', type: 'button',
          on: {
            click: async () => {
              if (!(await confirmDialog('清掉已结束的任务？', '只清列表，不动已经在硬盘上的图片。'))) return;
              try {
                await api.clearDownloads();
                await refresh();
              } catch (error) {
                toast('清理失败', { type: 'error', detail: error.message });
              }
            },
          },
        }, '清空已结束'),
        el('button', {
          class: 'btn primary small', type: 'button',
          on: {
            click: () => {
              if (!data.tasks || !data.tasks.length) {
                toast('还没有任务，去作品页选章节吧', { type: 'info' });
                return;
              }
              toast('去作品页勾选章节，然后点「下载选中」', { type: 'info' });
            },
          },
        }, '＋ 新建下载'),
      ),
    ),
  );

  if (!(data.tasks || []).length && !(data.history || []).length) {
    root.append(emptyState('📥', '队列是空的', '在作品页勾选章节，点「下载选中」就会排到这里。'));
    return;
  }

  const wrap = el('div', { class: 'dl-list' });
  for (const task of data.tasks || []) wrap.append(taskCard(task));
  root.append(wrap);

  if (data.history && data.history.length) {
    root.append(el('div', { class: 'panel', style: 'margin-top:22px' },
      el('h2', { text: '下载历史' }),
      el('p', { class: 'hint', text: '最近完成 / 失败的任务，重启后还在。' }),
      el('div', { class: 'dl-list' },
        ...data.history.slice(0, 30).map(historyRow),
      ),
    ));
  }
}

function tasks_recent(task) {
  const finished = Number(task.finishedAt || task.finished_at || 0);
  return !finished || Date.now() / 1000 - finished < 600;
}

export async function refresh() {
  try {
    data = await api.downloads();
    if (currentRoot) renderList(currentRoot);
    return data;
  } catch (error) {
    console.warn(error);
    return data;
  }
}

export async function render(root) {
  currentRoot = root;
  clear(root);
  root.append(el('div', { class: 'load-bar' },
    el('div', { class: 'spinner' }), el('span', { text: '读取下载队列…' })));
  await refresh();

  const onTask = (event) => {
    const task = event.detail;
    if (!task || !task.id) return;
    const index = (data.tasks || []).findIndex((item) => item.id === task.id);
    if (index >= 0) data.tasks[index] = task;
    else data.tasks = [task, ...(data.tasks || [])];
    if (currentRoot) renderList(currentRoot);
  };
  const onRefresh = () => refresh();
  window.addEventListener('dumbcat:task', onTask);
  window.addEventListener('dumbcat:downloads', onRefresh);
  window.addEventListener('dumbcat:downloads-refresh', onRefresh);
  unsubscribe = () => {
    window.removeEventListener('dumbcat:task', onTask);
    window.removeEventListener('dumbcat:downloads', onRefresh);
    window.removeEventListener('dumbcat:downloads-refresh', onRefresh);
  };
}

export function unmount() {
  if (unsubscribe) {
    unsubscribe();
    unsubscribe = null;
  }
  currentRoot = null;
}
