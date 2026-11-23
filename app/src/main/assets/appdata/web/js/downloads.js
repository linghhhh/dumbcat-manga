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

function settings() {
  return window.__DUMBCAT_SETTINGS__ || {};
}

/** 路径末尾一段，用来给人看；不透明 token 也能显示个名字。 */
export function folderLabel(path) {
  const clean = String(path || '').trim().replace(/[\\/]+$/, '');
  if (!clean) return '';
  if (/^content:\/\//i.test(clean)) {
    try {
      const last = decodeURIComponent(clean.split('/').filter(Boolean).pop() || '');
      return last || '手机文件夹';
    } catch (error) {
      return '手机文件夹';
    }
  }
  return clean.split(/[\\/]/).filter(Boolean).pop() || clean;
}

/** 调后端选择器：返回 {path,label}；取消 / 路径为空返回 null。 */
export async function chooseFolderInfo(initial) {
  try {
    const result = await api.chooseFolder(initial || '');
    if (!result || result.cancelled || result.ok === false) return null;
    const path = String(result.path || '').trim();
    if (!path) return null;
    const label = String(result.label || '').trim() || folderLabel(path);
    return { path, label };
  } catch (error) {
    if (error.status === 403) {
      // 403 = 没授权：把后端 detail 原文给用户，再按权限状态补一句去哪儿开
      let hint = '去系统设置给「呆猫漫画」开「所有文件访问权限」，回来再点一次。';
      try {
        const perm = await api.storagePermission();
        if (perm && perm.granted === false && perm.can_ask === false) {
          hint = '去「设置 → 应用 → 呆猫漫画 → 权限」里手动打开「所有文件访问权限」，回来再点一次。';
        }
      } catch (ignored) {
        /* 老版本后端没有 /api/storage/permission，不影响提示 */
      }
      toast('还需要文件访问权限', { type: 'warn', detail: `${error.message}。${hint}` });
    } else {
      toast('打开文件夹选择器失败', { type: 'error', detail: error.message });
    }
    return null;
  }
}

/* 旧接口：只要路径字符串，没有路径就是空串 */
export async function chooseFolder(initial) {
  const info = await chooseFolderInfo(initial);
  return info ? info.path : '';
}

/** 当前下载保存位置：安卓记用户选的文件夹，桌面沿用「上次用的目录」。 */
export function readTarget() {
  const config = settings();
  const basePath = String(config.output_dir || '').trim();
  if (config.android) {
    const stored = loadLocal('downloadTarget', null);
    if (stored && typeof stored === 'object' && String(stored.path || '').trim()) {
      const path = String(stored.path).trim();
      return { path, label: String(stored.label || '').trim() || folderLabel(path) };
    }
    return { path: basePath, label: basePath ? folderLabel(basePath) : '' };
  }
  const folder = String(loadLocal('outputDir', '') || basePath).trim();
  return { path: folder, label: folder ? folderLabel(folder) : '' };
}

export function saveTarget(target) {
  const path = String(target?.path || '').trim();
  if (!path) return;
  const label = String(target?.label || '').trim() || folderLabel(path);
  if (settings().android) saveLocal('downloadTarget', { path, label });
  else saveLocal('outputDir', path);
}

/* ------------------------------ 文件夹浏览（安卓） ------------------------------ */

/** 安卓授权引导弹窗：标题 + 说明 + 「去系统设置」/「先算了」，返回是否要去。 */
function askPermissionDialog() {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    openModal(el('div', {},
      el('h2', { text: '需要「所有文件访问权限」' }),
      el('p', {
        class: 'hint',
        style: 'margin-top:6px',
        text: '要保存到手机公共目录（比如「下载/呆猫漫画」），需要先给「呆猫漫画」开「所有文件访问权限」。现在去开吗？',
      }),
      el('div', { class: 'modal-actions' },
        el('button', { class: 'btn ghost', type: 'button', on: { click: () => { done(false); closeModal(); } } }, '先算了'),
        el('button', { class: 'btn primary', type: 'button', on: { click: () => { done(true); closeModal(); } } }, '去系统设置'),
      ),
    ), { onClose: () => done(false) });
  });
}

/** 安卓上是否还没拿到「所有文件访问权限」；老后端 404/501 或非安卓一律算已拿到。 */
async function lacksStorageAccess() {
  if (!settings().android) return false;
  try {
    const permission = await api.storagePermission();
    return Boolean(permission && permission.granted === false);
  } catch (error) {
    if (error.status === 404 || error.status === 501) return false;
    return error.status === 403;
  }
}

/**
 * 安卓选文件夹之前先过一道授权：
 * 已授权 / 非安卓 / 老后端没有接口 → 直接放行；没授权 → 弹引导，确认后拉起系统授权页。
 * 返回 false 表示「先别选」，调用方直接放弃（避免再吃一次 403）。
 */
export async function ensureStorageAccess() {
  if (!settings().android) return true;
  if (!(await lacksStorageAccess())) return true;
  if (!(await askPermissionDialog())) return false;
  try {
    const result = await api.requestStorageAccess();
    if (result && result.granted === true) {
      toast('权限已开，现在可以选文件夹了', { type: 'ok' });
      return true;
    }
  } catch (error) {
    toast('没能拉起系统授权页', { type: 'warn', detail: error.message });
  }
  toast('授权后回到这里，再点一次「选择文件夹」', { type: 'info' });
  return false;
}

/**
 * 在 App 里浏览文件夹：后端 /api/fs/dirs 列子目录，点进去选一个。
 * 后端还没实现浏览接口时退回它给的原生选择器 / 推荐目录。
 */
export async function pickFolder(initial = '') {
  if (!(await ensureStorageAccess())) return null;
  const stored = loadLocal('downloadTarget', null);
  const start = String(initial || (stored && typeof stored === 'object' ? stored.path : '') || '').trim();
  const recommended = await chooseFolderInfo(start);
  if (!recommended) return null;
  let listing;
  try {
    listing = await api.fsDirs(recommended.path);
  } catch (error) {
    if (error.status === 404 || error.status === 501) return recommended;
    toast('列文件夹失败，先用这个位置', { type: 'error', detail: error.message });
    return recommended;
  }
  return browseFolders(recommended, listing);
}

function browseFolders(start, firstListing) {
  return new Promise((resolve) => {
    let listing = firstListing;
    let current = { path: listing?.path || start.path, label: start.label || folderLabel(start.path) };
    let settled = false;

    const settle = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };

    const crumb = el('code', { class: 'dl-browse-path', text: current.path });
    const hint = el('p', { class: 'hint' });
    const list = el('div', { class: 'dl-browse-list' });
    const upButton = el('button', { class: 'btn ghost small', type: 'button' }, '⬆ 上一级');
    const okButton = el('button', { class: 'btn primary', type: 'button' }, '就下到这里');

    const enter = async (path) => {
      try {
        listing = await api.fsDirs(path);
        const nextPath = String(listing.path || path);
        current = { path: nextPath, label: folderLabel(nextPath) };
        paint();
      } catch (error) {
        toast('进不去这个文件夹', { type: 'error', detail: error.message });
      }
    };

    const paint = () => {
      crumb.textContent = listing?.path || current.path;
      upButton.disabled = !listing?.parent;
      clear(list);
      const dirs = Array.isArray(listing?.dirs) ? listing.dirs : [];
      if (!dirs.length) {
        hint.textContent = '这里没有子文件夹，可以直接点「就下到这里」。';
      } else {
        hint.textContent = '点文件夹进去，选好后点「就下到这里」。';
      }
      for (const dir of dirs) {
        list.append(el('button', {
          class: `dl-browse-dir${dir.writable === false ? ' locked' : ''}`,
          type: 'button',
          title: dir.path,
          on: { click: () => enter(dir.path) },
        },
          el('span', { class: 'dl-browse-ico', text: '📁' }),
          el('span', { class: 'dl-browse-name', text: dir.name || folderLabel(dir.path) || '文件夹' }),
          dir.writable === false ? el('i', { class: 'dl-browse-lock', text: '不可写' }) : null,
        ));
      }
    };

    upButton.addEventListener('click', () => {
      if (listing?.parent) enter(listing.parent);
    });
    okButton.addEventListener('click', async () => {
      okButton.disabled = true;
      okButton.textContent = '确认中…';
      try {
        const info = await chooseFolderInfo(current.path);
        if (info) {
          settle(info);
          closeModal();
        }
      } finally {
        okButton.disabled = false;
        okButton.textContent = '就下到这里';
      }
    });

    paint();
    openModal(el('div', { class: 'dl-browse' },
      el('h2', { text: '选下载文件夹' }),
      el('p', { class: 'hint', text: '下载会直接写进这里；进到想要的文件夹再点「就下到这里」。' }),
      el('div', { class: 'dl-browse-bar' },
        upButton,
        crumb,
      ),
      list,
      hint,
      el('div', { class: 'modal-actions' },
        el('button', { class: 'btn ghost', type: 'button', on: { click: () => { settle(null); closeModal(); } } }, '取消'),
        okButton,
      ),
    ), {
      className: 'modal-browse',
      onClose: () => settle(null),
    });
  });
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
  const config = settings();
  const android = Boolean(config.android);
  const fixedRoot = String(config.output_dir || options.defaultDir || '').trim();
  let target = android ? readTarget() : null;
  let folder = android ? '' : (options.outputDir || loadLocal('outputDir', '') || (options.defaultDir || fixedRoot));
  const workers = Number(options.workers) || 4;

  const folderInput = android ? null : el('input', {
    type: 'text',
    value: folder,
    placeholder: 'E:\\manga\\yuanban 或别的目录',
    spellcheck: 'false',
  });
  const workersInput = el('input', {
    type: 'number', min: '1', max: '16', value: String(workers), style: 'width:90px',
  });

  const recentBox = el('div', { class: 'row', style: 'flex-wrap:wrap;gap:6px;margin-top:6px' });
  const startButton = el('button', { class: 'btn primary', type: 'button' }, `开始下载 ${selected.length} 章`);

  /* 安卓：显示当前选中的文件夹 + 「选择文件夹…」；桌面：目录输入 + 浏览按钮 */
  const targetName = el('strong', { class: 'dl-pick-name' });
  const targetPathEl = el('span', { class: 'dl-pick-path' });
  const applyTargetView = () => {
    const info = target && String(target.path || '').trim()
      ? { path: String(target.path).trim(), label: String(target.label || '').trim() || folderLabel(target.path) }
      : { path: '', label: '还没选文件夹' };
    targetName.textContent = info.label;
    targetPathEl.textContent = info.path || '点「选择文件夹…」挑一个保存位置';
  };
  applyTargetView();

  const pickButton = el('button', { class: 'btn small', type: 'button' },
    android ? '📁 选择文件夹…' : '📁 选择目录…');
  pickButton.addEventListener('click', async () => {
    pickButton.disabled = true;
    pickButton.textContent = '正在打开…';
    try {
      if (android) {
        const picked = await pickFolder(target?.path || '');
        if (picked) {
          target = picked;
          saveTarget(picked);
          applyTargetView();
        }
      } else {
        const picked = await chooseFolderInfo(folderInput.value.trim());
        if (picked) {
          folderInput.value = picked.path;
          folder = picked.path;
          saveLocal('outputDir', picked.path);
          rememberOutputDir(picked.path);
        }
      }
    } finally {
      pickButton.disabled = false;
      pickButton.textContent = android ? '📁 选择文件夹…' : '📁 选择目录…';
    }
  });

  const targetField = android
    ? el('div', { class: 'field' },
        el('label', { text: '保存文件夹' }),
        el('div', { class: 'dl-pick' },
          el('div', { class: 'dl-pick-main' }, targetName, targetPathEl),
          pickButton,
        ),
        el('p', { class: 'hint', text: '下载直接写进这个文件夹；想换位置就点「选择文件夹…」。' }),
      )
    : el('div', { class: 'field' },
        el('label', { text: '保存目录' }),
        el('div', { class: 'row' }, folderInput, pickButton),
        recentBox,
      );

  openModal(el('div', {},
    el('h2', { text: android ? '开始下载？' : '下载到哪儿？' }),
    el('p', { class: 'hint' },
      `《${series.title || '未命名作品'}》 · 已选 ${selected.length} 章。呆猫会按「作品名 / 章节名」建好文件夹。`),
    targetField,
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
  const recentList = android ? [] : loadLocal('recentOutputDirs', []);
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
    const outputDir = android
      ? String(target?.path || '').trim()
      : (folderInput ? folderInput.value.trim() : '');
    if (!outputDir) {
      if (android) toast('还没选保存文件夹，点「选择文件夹…」挑一个吧', { type: 'error' });
      else {
        toast('先选一个保存目录吧', { type: 'error' });
        folderInput.focus();
      }
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
        series: series.snapshot || series.public || series,
        chapters: options.chapterSnapshot || options.chapters || [],
      });
      if (android) saveTarget(target);
      else {
        saveLocal('outputDir', outputDir);
        rememberOutputDir(outputDir);
      }
      toast(`已加入下载队列（${selected.length} 章）`, { type: 'ok' });
      closeModal();
      window.dispatchEvent(new CustomEvent('dumbcat:downloads-refresh'));
      if (options.goToQueue !== false) {
        setTimeout(() => { location.hash = '#/shelf?tab=downloads'; }, 350);
      }
    } catch (error) {
      toast('加入下载队列失败', { type: 'error', detail: error.message });
    } finally {
      startButton.disabled = false;
      startButton.textContent = `开始下载 ${selected.length} 章`;
    }
  });
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
      el('span', { style: 'display:flex;align-items:center;gap:8px;flex-wrap:wrap' },
        folderButton(task, 'btn tiny ghost'),
        Array.isArray(task.failedUids) && task.failedUids.length
          ? el('button', {
            class: 'btn tiny ghost', type: 'button',
            on: {
              click: async (event) => {
                const button = event.currentTarget;
                button.disabled = true;
                try {
                  await api.retryDownload(task.id);
                  toast('已把失败章节重新排进队列', { type: 'ok' });
                  refresh();
                } catch (error) {
                  toast('重试失败', { type: 'error', detail: error.message });
                  button.disabled = false;
                }
              },
            },
          }, '重试')
          : null,
        el('span', { style: 'color:var(--ink-soft)', text: timeAgo(task.finishedAt || task.finished_at || task.created_at) }),
      ),
    ),
    chapterBlock(task),
  );
}

/* ------------------------------ 队列页 ------------------------------ */
/* 这几个状态算「已经结束」：不再占队列位，只在下载历史里出现 */
const FINISHED_STATES = ['done', 'failed', 'partial', 'cancelled', 'skipped', 'locked', 'empty'];

function isFinished(task) {
  return FINISHED_STATES.includes(task.state);
}

/** 任务卡 / 历史行共用的「导出到手机」（安卓）或「打开目录」（桌面）按钮。 */
function folderButton(task, cls = 'btn tiny ghost') {
  const path = task.finalDir || task.outputDir || '';
  if (!path) return null;
  const android = Boolean(settings().android);
  return el('button', {
    class: cls, type: 'button',
    on: {
      click: async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          if (android) {
            const result = await api.exportDownloads(path);
            toast(`已导出 ${result.files || 0} 个文件到「下载/呆猫漫画」`, { type: 'ok' });
          } else {
            await api.openFolder(path);
          }
        } catch (error) {
          toast(android ? '导出失败' : '打不开目录', { type: 'error', detail: error.message });
        } finally {
          button.disabled = false;
        }
      },
    },
  }, android ? '导出到手机' : '打开目录');
}

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
      el('span', { style: 'margin-left:auto;display:flex;gap:6px;flex-wrap:wrap' },
        folderButton(task),
        task.failed > 0 && task.state !== 'running' && task.state !== 'queued' ? el('button', {
          class: 'btn tiny', type: 'button',
          on: {
            click: async (event) => {
              const button = event.currentTarget;
              button.disabled = true;
              button.textContent = '排队中…';
              try {
                await api.retryDownload(task.id);
                toast('已把失败章节重新排进队列', { type: 'ok' });
                refresh();
              } catch (error) {
                toast('重试失败', { type: 'error', detail: error.message });
                button.disabled = false;
                button.textContent = '重试失败章节';
              }
            },
          },
        }, '重试失败章节') : null,
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

/** 下载缓存标签里的「保存位置」卡片：显示当前文件夹，点「更换」重新选。 */
function targetCard() {
  const android = Boolean(settings().android);
  const target = readTarget();
  const path = String(target.path || '').trim();
  const label = target.label || (path ? folderLabel(path) : '') || '应用默认目录';
  const change = el('button', { class: 'btn ghost small', type: 'button' }, '更换…');
  change.addEventListener('click', async () => {
    change.disabled = true;
    change.textContent = '正在打开…';
    try {
      const picked = android ? await pickFolder(path) : await chooseFolderInfo(path);
      if (!picked) return;
      saveTarget(picked);
      toast(`保存位置改成「${picked.label}」`, { type: 'ok' });
      if (currentRoot) renderList(currentRoot);
    } finally {
      change.disabled = false;
      change.textContent = '更换…';
    }
  });
  // 未授权时给一行小字 +「去授权」，不用等用户点「更换」吃 403
  const permHint = el('div', { class: 'dl-target-perm', attrs: { hidden: true } });
  if (android) {
    lacksStorageAccess().then((lacks) => {
      if (!lacks) return;
      clear(permHint);
      permHint.hidden = false;
      permHint.append(
        el('span', {
          class: 'dl-target-perm-text',
          text: '还没开「所有文件访问权限」，现在会先存到应用私有目录；开好之后才能用上面的文件夹。',
        }),
        el('button', {
          class: 'btn tiny ghost', type: 'button',
          on: {
            click: async () => {
              const allowed = await ensureStorageAccess();
              if (allowed && currentRoot) renderList(currentRoot);
            },
          },
        }, '去授权'),
      );
    }).catch(() => {});
  }
  return el('div', { class: 'dl-target' },
    el('div', { class: 'dl-target-main' },
      el('span', { class: 'dl-target-ic', text: android ? '📱' : '📂' }),
      el('div', { class: 'dl-target-text' },
        el('span', { class: 'dl-target-label', text: android ? '下载保存文件夹' : '下载保存目录' }),
        el('strong', { class: 'dl-target-name', text: label, title: path || label }),
        el('span', { class: 'dl-target-path', text: path || '还没选，下载时会用应用默认目录' }),
      ),
    ),
    change,
    permHint,
  );
}

function renderList(root) {
  if (!root) return;
  clear(root);
  const tasks = data.tasks || [];
  const history = data.history || [];
  // 队列只放没结束的；结束的一律只进「下载历史」，避免同一条任务两处重复
  const active = tasks.filter((task) => !isFinished(task));
  const finished = tasks.length - active.length;
  const statusHint = active.length
    ? `已结束 ${finished} 个，见下方「下载历史」`
    : (finished
      ? `已完成 ${finished} 个，见下方「下载历史」`
      : `共 ${tasks.length} 个任务 · 历史 ${history.length} 条`);
  root.append(
    el('div', { class: 'dl-tab-bar' },
      el('div', { class: 'dl-tab-status' },
        el('strong', { text: active.length ? `${active.length} 个任务进行中` : '队列空闲' }),
        el('span', { class: 'hint', text: statusHint }),
      ),
      el('div', { class: 'dl-tab-actions' },
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
      ),
    ),
    targetCard(),
  );

  if (!active.length && !history.length) {
    root.append(emptyState('📥', '队列是空的', '在作品页勾选章节，点「下载选中」就会排到这里。'));
    return;
  }

  if (active.length) {
    const wrap = el('div', { class: 'dl-list' });
    for (const task of active) wrap.append(taskCard(task));
    root.append(wrap);
  }

  if (history.length) {
    root.append(el('div', { class: 'panel', style: 'margin-top:22px' },
      el('h2', { text: '下载历史' }),
      el('p', { class: 'hint', text: '最近完成 / 失败的任务，重启后还在。' }),
      el('div', { class: 'dl-list' },
        ...history.slice(0, 30).map(historyRow),
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

/* SSE 进度事件可能一秒好几个：合并到下一帧再重绘，别把整页按钮刷得点不中 */
let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  const raf = window.requestAnimationFrame || ((callback) => setTimeout(callback, 16));
  raf(() => {
    renderQueued = false;
    if (currentRoot) renderList(currentRoot);
  });
}

function bindEvents() {
  if (unsubscribe) return;
  const onTask = (event) => {
    const task = event.detail;
    if (!task || !task.id) return;
    const index = (data.tasks || []).findIndex((item) => item.id === task.id);
    if (index >= 0) data.tasks[index] = task;
    else data.tasks = [task, ...(data.tasks || [])];
    // 结束的任务不留在队列里，拉一次 /api/downloads 把它带进「下载历史」
    if (isFinished(task)) refresh();
    else scheduleRender();
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

/** 嵌进书架第三个标签：开局拉一次 /api/downloads，再订阅 SSE。 */
export async function renderInto(root) {
  currentRoot = root;
  if (!root) return;
  clear(root);
  root.append(el('div', { class: 'load-bar' },
    el('div', { class: 'spinner' }), el('span', { text: '读取下载队列…' })));
  bindEvents();
  await refresh();
}

/* 兼容旧的独立页面入口（现在都走书架标签，保留导出不吃亏） */
export async function render(root) {
  return renderInto(root);
}

export function unmount() {
  if (unsubscribe) {
    unsubscribe();
    unsubscribe = null;
  }
  currentRoot = null;
  renderQueued = false;
}
