/* 设置：外观、浏览、下载、缓存与关于。改动即时保存。 */
import { api } from './api.js';
import { el, clear, toast, confirmDialog, fmtBytes } from './util.js';
import { App, applySettings, loadSources } from './app.js';

let rootRef = null;
let saved = null;
let saveTimer = 0;

export async function render(root) {
  rootRef = root;
  clear(root);
  root.append(el('div', { class: 'load-bar', style: 'padding:80px 0' },
    el('div', { class: 'spinner' }), el('span', { text: '呆猫在翻设置本…' })));
  try {
    saved = await api.settings();
  } catch (error) {
    clear(root);
    root.append(el('div', { class: 'empty-state' },
      el('strong', { text: '设置读不出来' }),
      el('div', { text: error.message })));
    return;
  }
  draw();
}

function draw() {
  const root = rootRef;
  if (!root) return;
  clear(root);

  root.append(el('div', { class: 'view-head' },
    el('div', {},
      el('div', { class: 'sub', text: '改完马上生效，不用重启。' }),
      el('h1', {}, el('span', { class: 'stroke', text: '设置' })),
    ),
    el('div', { class: 'head-actions' },
      el('span', { class: 'chip', id: 'saveState', text: '已同步' }),
    ),
  ));

  const grid = el('div', { class: 'settings-grid' });
  grid.append(appearancePanel(), browsePanel(), downloadPanel(), cachePanel(), aboutPanel());
  root.append(grid);
}

/* ------------------------------ 通用控件 ------------------------------ */
function panel(title, hint, ...children) {
  return el('div', { class: 'panel' },
    el('h2', { text: title }),
    hint ? el('p', { class: 'hint', text: hint }) : null,
    ...children,
  );
}

function numberField(label, key, { min = 1, max = 16, step = 1, hint = '' } = {}) {
  const input = el('input', {
    type: 'number', value: saved[key] ?? '', min, max, step,
    on: {
      change: () => {
        const value = Math.max(min, Math.min(max, Number(input.value) || min));
        input.value = value;
        commit({ [key]: value });
      },
    },
  });
  return el('div', { class: 'field' },
    el('label', { text: label }),
    input,
    hint ? el('div', { class: 'hint', text: hint }) : null,
  );
}

function switchField(label, key, hint = '', onChange = null) {
  const input = el('input', {
    type: 'checkbox',
    on: {
      change: () => {
        commit({ [key]: input.checked });
        if (onChange) onChange(input.checked);
      },
    },
  });
  input.checked = Boolean(saved[key]);
  return el('label', { class: 'switch' },
    input, el('i', {}), el('span', { text: label }),
    hint ? el('span', { class: 'hint', style: 'margin-left:4px', text: hint }) : null,
  );
}

async function commit(patch) {
  const state = document.getElementById('saveState');
  if (state) state.textContent = '保存中…';
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      const result = await api.saveSettings(patch);
      saved = { ...saved, ...(result.settings || patch), ...patch };
      applySettings(saved);
      if (state) {
        state.textContent = '已同步';
        state.style.color = '';
      }
    } catch (error) {
      if (state) {
        state.textContent = '保存失败';
        state.style.color = 'var(--danger)';
      }
      toast('设置没保存上', { type: 'error', detail: error.message });
    }
  }, 180);
}

/* ------------------------------ 面板 ------------------------------ */
function appearancePanel() {
  const themeGroup = el('div', { class: 'seg', id: 'themeSeg' },
    el('button', {
      class: `btn small ${saved.theme !== 'night' ? 'primary' : 'ghost'}`,
      type: 'button', dataset: { theme: 'cream' },
      on: {
        click: () => {
          saved.theme = 'cream';
          commit({ theme: 'cream' });
          markTheme('cream');
        },
      },
    }, '☀ 日间'),
    el('button', {
      class: `btn small ${saved.theme === 'night' ? 'primary' : 'ghost'}`,
      type: 'button', dataset: { theme: 'night' },
      on: {
        click: () => {
          saved.theme = 'night';
          commit({ theme: 'night' });
          markTheme('night');
        },
      },
    }, '🌙 夜间'),
  );

  const widthInput = el('input', {
    type: 'range', min: '120', max: '280', step: '8', value: saved.grid_min_width || 168,
    on: {
      input: () => {
        const label = document.getElementById('gridWidthLabel');
        if (label) label.textContent = `${widthInput.value}px`;
      },
      change: () => commit({ grid_min_width: Number(widthInput.value) }),
    },
  });

  return panel('外观', '白底蓝调，看着干净；夜里看漫画可以切深色。',
    el('div', { class: 'field' },
      el('label', { text: '主题' }),
      themeGroup,
    ),
    el('div', { class: 'field' },
      el('label', { text: `封面最小宽度 · ${saved.grid_min_width || 168}px` }),
      widthInput,
      el('div', { class: 'hint', text: '越大封面越大；窄屏会自动减少列数。' }),
    ),
    switchField('减少动画', 'reduce_motion', '头晕/省电时打开'),
  );
}

function markTheme(theme) {
  document.querySelectorAll('#themeSeg .btn').forEach((button) => {
    const active = button.dataset.theme === theme;
    button.classList.toggle('primary', active);
    button.classList.toggle('ghost', !active);
  });
}

function browsePanel() {
  return panel('浏览与加载', '封面按需加载，滑到底部自动翻页。',
    switchField('滑到底自动加载下一页', 'auto_load_more', '关掉后需要点「加载下一页」'),
    switchField('章节新→旧排列', 'chapter_newest_first', '下次打开详情页生效'),
    switchField('包含需要浏览器渲染的图源', 'include_browser_sources', '慢一些，但能抓到动态站', async () => {
      try {
        await loadSources({ announce: false });
        toast('图源已按新设置重载', { type: 'ok' });
      } catch {}
    }),
  );
}

function downloadPanel() {
  const dirInput = el('input', { type: 'text', value: saved.output_dir || '' });
  dirInput.addEventListener('change', () => commit({ output_dir: dirInput.value.trim() }));

  const pick = async () => {
    try {
      const result = await api.chooseFolder(dirInput.value.trim());
      if (result && result.path) {
        dirInput.value = result.path;
        commit({ output_dir: result.path });
      }
    } catch (error) {
      toast('没选成目录', { type: 'error', detail: error.message });
    }
  };

  return panel('下载', '默认下到这里；每次下载前也可以临时改。',
    el('div', { class: 'field' },
      el('label', { text: '下载目录' }),
      el('div', { class: 'row' },
        dirInput,
        el('button', { class: 'btn small', type: 'button', on: { click: pick } }, '📁 选择目录'),
        el('button', {
          class: 'btn small ghost', type: 'button',
          on: {
            click: async () => {
              try { await api.openFolder(dirInput.value.trim()); } catch (error) { toast(error.message, { type: 'error' }); }
            },
          },
        }, '打开'),
      ),
    ),
    numberField('下载线程数', 'workers', { min: 1, max: 32, hint: '一次下几张图；图床慢时 16–24 通常最快' }),
    numberField('封面并发', 'image_workers', { min: 1, max: 12, hint: '同时加载几个封面' }),
    switchField('下载完成后打开文件夹', 'open_after_download'),
  );
}

function cachePanel() {
  const bytes = Number(saved.cacheBytes || 0);
  return panel('缓存与存储', '封面和各种页面图会存在本地，删了也不影响书源。',
    el('div', { class: 'detail-meta' },
      el('span', { class: 'chip', text: `图片缓存 ${fmtBytes(bytes)}` }),
      el('span', { class: 'chip', text: `上限 ${saved.image_cache_mb || 512}MB` }),
    ),
    numberField('缓存上限（MB）', 'image_cache_mb', { min: 64, max: 4096, step: 64, hint: '超出后自动清最旧的' }),
    el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap' },
      el('button', {
        class: 'btn small ghost', type: 'button',
        on: { click: () => refreshCacheStats() },
      }, '刷新统计'),
      el('button', {
        class: 'btn small danger', type: 'button',
        on: {
          click: async () => {
            const ok = await confirmDialog('清空图片缓存？', '下次看会重新下载，不会删已下载的漫画。', { danger: true, okText: '清空' });
            if (!ok) return;
            try {
              await api.clearCache();
              await refreshCacheStats();
              toast('缓存已清空', { type: 'ok' });
            } catch (error) {
              toast('清空失败', { type: 'error', detail: error.message });
            }
          },
        },
      }, '清空缓存'),
    ),
  );
}

async function refreshCacheStats() {
  try {
    const data = await api.cacheStats();
    saved.cacheBytes = data.bytes ?? data.cacheBytes ?? 0;
    const chips = rootRef?.querySelectorAll('.panel .chip');
    if (chips && chips.length) chips[0].textContent = `图片缓存 ${fmtBytes(saved.cacheBytes)}`;
    toast('缓存统计已更新', { type: 'ok' });
  } catch (error) {
    toast(error.message, { type: 'error' });
  }
}

function aboutPanel() {
  return panel('关于', '',
    el('div', { class: 'detail-meta' },
      el('span', { class: 'chip', text: `呆猫漫画 v${saved.version || App.version || '3.0.0'}` }),
      el('span', { class: 'chip', text: `${App.sources.length} 个图源` }),
    ),
    el('div', { class: 'field' },
      el('label', { text: '项目目录' }),
      el('div', { class: 'row' },
        el('input', { type: 'text', value: saved.projectRoot || '', readonly: true }),
        el('button', {
          class: 'btn small ghost', type: 'button',
          on: {
            click: async () => {
              try { await api.openFolder(saved.projectRoot || ''); } catch (error) { toast(error.message, { type: 'error' }); }
            },
          },
        }, '打开'),
      ),
    ),
    el('div', { class: 'field' },
      el('label', { text: '用户插件目录' }),
      el('div', { class: 'row' },
        el('input', { type: 'text', value: saved.pluginDir || '', readonly: true }),
        el('button', {
          class: 'btn small ghost', type: 'button',
          on: {
            click: async () => {
              try { await api.openFolder(saved.pluginDir || ''); } catch (error) { toast(error.message, { type: 'error' }); }
            },
          },
        }, '打开'),
        el('button', {
          class: 'btn small', type: 'button',
          on: { click: () => { location.hash = '#/plugins'; } },
        }, '管理插件'),
      ),
    ),
    el('p', { class: 'hint', text: '呆猫漫画 · 本地运行，不上传任何数据。启动页那张插画就是呆猫和 Doro。' }),
  );
}

export function unmount() {
  clearTimeout(saveTimer);
  rootRef = null;
  saved = null;
}
