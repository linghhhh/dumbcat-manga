/* 下载设置：下载目录、每章图片上限、并发与缓存。手机版根目录固定在应用私有目录，只改子目录。 */
import { api } from './api.js';
import { el, clear, toast } from './util.js';

let rootRef = null;
let saved = null;
let android = false;

export async function render(root) {
  rootRef = root;
  clear(root);
  root.append(el('div', { class: 'load-bar', style: 'padding:80px 0' },
    el('div', { class: 'spinner' }), el('span', { text: '呆猫在翻下载设置…' })));
  try {
    saved = await api.settings();
    android = Boolean(saved.android);
  } catch (error) {
    clear(root);
    root.append(el('div', { class: 'empty-state' },
      el('strong', { text: '下载设置读不出来' }),
      el('div', { text: error.message })));
    return;
  }
  draw();
}

export function unmount() {
  rootRef = null;
  saved = null;
}

function draw() {
  const root = rootRef;
  if (!root) return;
  clear(root);
  root.append(el('div', { class: 'view-head' },
    el('div', {},
      el('div', { class: 'sub', text: android ? '根目录固定在应用里，可以再选一层子目录。' : '改完马上生效，下一次下载就走新目录。' }),
      el('h1', {}, el('span', { class: 'stroke', text: '下载' })),
    ),
    el('div', { class: 'head-actions' },
      el('button', {
        class: 'btn ghost small', type: 'button',
        on: { click: () => { location.hash = '#/downloads'; } },
      }, '查看队列'),
    ),
  ));
  const grid = el('div', { class: 'settings-grid' });
  grid.append(dirPanel(), limitPanel(), speedPanel());
  root.append(grid);
}

function panel(title, hint, ...children) {
  return el('div', { class: 'panel' },
    el('h2', { text: title }),
    hint ? el('p', { class: 'hint', text: hint }) : null,
    ...children,
  );
}

async function commit(patch, okText = '已保存') {
  try {
    const res = await api.saveSettings(patch);
    if (res && res.settings) saved = { ...saved, ...res.settings };
    toast(okText, { type: 'ok' });
  } catch (error) {
    toast('没存上', { type: 'error', detail: error.message });
  }
}

function fullPath() {
  const root = String(saved.output_dir || '').replace(/[\\/]+$/, '');
  const sub = String(saved.output_subdir || '').replace(/^[\\/]+|[\\/]+$/g, '');
  return sub ? `${root}/${sub}` : root;
}

function numberField(label, key, { min = 0, max = 32, step = 1, hint = '' } = {}) {
  const input = el('input', {
    type: 'number', value: saved[key] ?? '', min, max, step,
    on: {
      change: () => {
        const value = Math.max(min, Math.min(max, Number(input.value) || 0));
        input.value = String(value);
        commit({ [key]: value });
      },
    },
  });
  return el('div', { class: 'field' },
    el('label', { text: label }),
    el('div', { class: 'row' }, input),
    hint ? el('p', { class: 'hint', text: hint }) : null,
  );
}

function switchField(label, key, hint = '') {
  const box = el('input', { type: 'checkbox' });
  box.checked = Boolean(saved[key]);
  box.addEventListener('change', () => commit({ [key]: box.checked }));
  return el('label', { class: 'switch-row' },
    el('span', { class: 'switch-text' },
      el('strong', { text: label }),
      hint ? el('span', { class: 'hint', text: hint }) : null,
    ),
    box,
  );
}

function dirPanel() {
  const pathChip = el('code', { class: 'chip', text: fullPath() });
  const subInput = el('input', { type: 'text', value: saved.output_subdir || '', placeholder: '留空 = 直接放在根目录' });
  subInput.addEventListener('change', async () => {
    await commit({ output_subdir: subInput.value.trim() }, '子目录已保存');
    pathChip.textContent = fullPath();
  });

  const copyBtn = el('button', {
    class: 'btn small ghost', type: 'button',
    on: {
      click: async () => {
        const text = fullPath();
        try {
          await navigator.clipboard.writeText(text);
          toast('路径已复制', { type: 'ok' });
        } catch {
          toast(text, { type: 'ok' });
        }
      },
    },
  }, '📋 复制路径');

  const children = [el('div', { class: 'field' }, el('label', { text: '完整路径' }), pathChip)];

  if (!android) {
    const dirInput = el('input', { type: 'text', value: saved.output_dir || '' });
    dirInput.addEventListener('change', async () => {
      await commit({ output_dir: dirInput.value.trim() });
      pathChip.textContent = fullPath();
    });
    children.push(el('div', { class: 'field' },
      el('label', { text: '下载根目录' }),
      el('div', { class: 'row' },
        dirInput,
        el('button', {
          class: 'btn small', type: 'button',
          on: {
            click: async () => {
              try {
                const result = await api.chooseFolder(dirInput.value.trim());
                if (result && result.path) {
                  dirInput.value = result.path;
                  await commit({ output_dir: result.path });
                  pathChip.textContent = fullPath();
                }
              } catch (error) {
                toast('没选成目录', { type: 'error', detail: error.message });
              }
            },
          },
        }, '📁 选择目录'),
      ),
    ));
  }

  children.push(el('div', { class: 'field' },
    el('label', { text: '子目录（可以写多级，如「合集/2026」）' }),
    el('div', { class: 'row' }, subInput, copyBtn),
    el('p', { class: 'hint', text: android
      ? '手机系统不允许 App 随便写外部存储，所以根目录在应用私有目录里；卸载 App 会连同下载一起删掉，想长期保存就自己再拷出来。'
      : '留空就下到根目录；每部作品还会各自建一个文件夹。' }),
  ));

  return panel('下载目录', null, ...children);
}

function limitPanel() {
  return panel('下载数量', '图多的作品可以只下前面几页，先看看画风。',
    numberField('每章最多下载图片数', 'max_images_per_chapter', {
      min: 0, max: 5000, step: 1,
      hint: '0 = 不限，按章节原有页数全部下载',
    }),
    numberField('图片缓存上限（MB）', 'image_cache_mb', {
      min: 64, max: 4096, step: 64,
      hint: '超出后自动清最旧的；不影响已经下载到文件夹里的图',
    }),
  );
}

function speedPanel() {
  return panel('速度', '网络差的时候调小一点，别把站点打急了。',
    numberField('同时下载几张', 'workers', {
      min: 1, max: 32, step: 1,
      hint: '一次并发下载的图片数；图床慢时 16–24 通常最快',
    }),
    android ? null : switchField('下载完成后打开文件夹', 'open_after_download', '桌面版会直接弹资源管理器'),
  );
}
