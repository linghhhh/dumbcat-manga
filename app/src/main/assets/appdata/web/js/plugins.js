/* 图源插件管理：新建/编辑/导入/重载/删除，内置源可复制成模板。 */
import { api } from './api.js';
import {
  el, clear, toast, openModal, closeModal, confirmDialog, promptDialog,
  loadLocal, saveLocal, fmtBytes,
} from './util.js';
import { App, loadSources } from './app.js';

let rootRef = null;
let cache = null;

export async function render(root) {
  rootRef = root;
  clear(root);
  root.append(el('div', { class: 'load-bar', style: 'padding:80px 0' },
    el('div', { class: 'spinner' }), el('span', { text: '呆猫在翻插件抽屉…' })));
  await refresh();
}

async function refresh() {
  try {
    cache = await api.plugins();
    try {
      const raw = (await api.logins()).logins || [];
      // 后端按 key 返回对象，这里统一成数组
      cache.logins = (Array.isArray(raw) ? raw : Object.values(raw))
        .filter((item) => item && item.key);
    } catch (error) {
      cache.logins = [];
      console.warn('登录状态读取失败', error);
    }
  } catch (error) {
    clear(rootRef);
    rootRef.append(el('div', { class: 'empty-state' },
      el('strong', { text: '插件列表读不出来' }),
      el('div', { text: error.message }),
      el('button', {
        class: 'btn primary', type: 'button',
        on: { click: () => refresh() },
      }, '再试一次')));
    return;
  }
  draw();
}

function draw() {
  const root = rootRef;
  if (!root) return;
  clear(root)
;
  const plugins = cache?.plugins || [];
  const users = plugins.filter((item) => String(item.origin || '').includes('用户插件'));
  const examples = plugins.filter((item) => !users.includes(item));
  const failed = cache?.failed || [];
  const dir = cache?.dir || '';

  root.append(el('div', { class: 'view-head' },
    el('div', {},
      el('div', { class: 'sub', text: '每个图源就是一个 .json 文件，改完点「热重载」就生效。' }),
      el('h1', {}, el('span', { class: 'stroke', text: '图源插件' })),
    ),
    el('div', { class: 'head-actions' },
      el('button', {
        class: 'btn primary', type: 'button',
        on: { click: () => createPlugin() },
      }, '＋ 新建插件'),
      el('label', { class: 'btn', style: 'cursor:pointer' },
        '📦 导入 zip',
        el('input', {
          type: 'file', accept: '.zip', style: 'display:none',
          on: {
            change: async (event) => {
              const file = event.target.files?.[0];
              event.target.value = '';
              if (file) await uploadZip(file);
            },
          },
        }),
      ),
      el('button', {
        class: 'btn ghost', type: 'button',
        on: { click: () => reloadAll() },
      }, '↻ 热重载'),
    ),
  ));

  const sourceDir = cache?.sourceDir || '';
  const sourceFiles = cache?.sourceFiles || [];
  if (sourceDir) {
    root.append(el('div', { class: 'panel' },
      el('h2', { text: `图源目录 · ${sourceFiles.length} 个 .json` }),
      el('p', {
        class: 'hint',
        text: '内置图源都在这里：一个文件就是一个图源。改名字、改图标、18+、开/关（enabled）、要不要梯子（breeze），甚至换成 CSS 规则（engine: rules）都只改 JSON。',
      }),
      el('div', { class: 'field' },
        el('label', { text: '图源文件目录' }),
        el('div', { class: 'row' },
          el('input', { type: 'text', value: sourceDir, readonly: true }),
          el('button', {
            class: 'btn small', type: 'button',
            on: {
              click: async () => {
                try { await api.openFolder(sourceDir); } catch (error) { toast(error.message, { type: 'error' }); }
              },
            },
          }, '打开目录'),
        ),
      ),
      el('div', { class: 'row', style: 'flex-wrap:wrap;gap:6px;margin-top:10px' },
        ...sourceFiles.slice(0, 24).map((name) =>
          el('span', { class: 'chip', style: 'font-size:11px', text: name.replace(/\.json$/, '') })),
      ),
    ));
  }

  root.append(el('div', { class: 'panel' },
    el('h2', { text: `插件目录 · ${users.length} 个用户插件` }),
    examples.length ? el('p', { class: 'hint', text: `另有 ${examples.length} 个内置示例，可点「复制成我的」再改。` }) : null,
    el('p', { class: 'hint', text: '支持单个 .py，或带 plugin.json 的文件夹；改完保存后会自动重载。' }),
    el('div', { class: 'field' },
      el('label', { text: '用户插件目录' }),
      el('div', { class: 'row' },
        el('input', { type: 'text', value: dir, readonly: true }),
        el('button', {
          class: 'btn small', type: 'button',
          on: {
            click: async () => {
              try { await api.openFolder(dir); } catch (error) { toast(error.message, { type: 'error' }); }
            },
          },
        }, '打开目录'),
      ),
    ),
    el('div', { class: 'upload-zone', id: 'pluginDrop' },
      el('div', { text: '把插件 zip 拖到这里' }),
      el('div', { style: 'font-size:12px;color:var(--ink-soft)', text: '或者点上面的「导入 zip」' }),
    ),
    el('div', { style: 'margin-top:10px' },
      el('button', {
        class: 'btn small ghost', type: 'button',
        on: { click: () => openEditor({ title: '插件模板', text: cache?.template || '' }) },
      }, '👀 看最小模板'),
    ),
  ));

  root.append(loginPanel());

  const grid = el('div', { class: 'plugin-grid', style: 'margin-top:14px' });
  for (const plugin of plugins) grid.append(pluginCard(plugin));
  if (!plugins.length) {
    grid.append(el('div', { class: 'empty-state', style: 'grid-column:1/-1;padding:30px' },
      el('strong', { text: '还没有用户插件' }),
      el('div', { text: '点「新建插件」会生成一个能跑的模板，照着改 CSS 选择器就行。' }),
    ));
  }
  root.append(grid);

  const builtins = (App.sources || []).filter((item) => item.kind === 'builtin' && !item.error);
  if (builtins.length) {
    root.append(el('div', { class: 'view-head', style: 'margin-top:26px' },
      el('div', {},
        el('div', { class: 'sub', text: '随程序附带；想改造哪一个，复制一份进用户插件目录再动手。' }),
        el('h2', { style: 'margin:0;font-size:20px', text: `内置图源（${builtins.length}）` }),
      ),
    ));
    const builtinGrid = el('div', { class: 'plugin-grid' });
    for (const source of builtins) builtinGrid.append(builtinCard(source));
    root.append(builtinGrid);
  }

  if (failed.length) {
    root.append(el('div', { class: 'panel', style: 'margin-top:14px;border-color:var(--danger)' },
      el('h2', { text: `加载失败的插件（${failed.length}）` }),
      ...failed.map((item) => el('div', { style: 'font-size:12.5px;margin-bottom:6px' },
        el('strong', { text: item.name || item.key }),
        el('span', { style: 'color:var(--danger)', text: ` — ${item.error || '未知错误'}` }),
      )),
    ));
  }

  const drop = document.getElementById('pluginDrop');
  if (drop) {
    drop.addEventListener('dragover', (event) => {
      event.preventDefault();
      drop.classList.add('drag');
    });
    drop.addEventListener('dragleave', () => drop.classList.remove('drag'));
    drop.addEventListener('drop', async (event) => {
      event.preventDefault();
      drop.classList.remove('drag');
      const file = event.dataTransfer?.files?.[0];
      if (file) await uploadZip(file);
    });
  }
}

/* ------------------------------ 图源登录（Cookie） ------------------------------ */
function loginRow(item) {
  const loggedIn = Boolean(item.loggedIn);
  const cookieText = loggedIn
    ? `已登录 · ${item.cookieCount || 0} 条 Cookie${item.username ? ` · ${item.username}` : ''}`
    : '未登录：付费/会员章节会取不到图';
  const ops = el('div', { class: 'lops' });
  if (loggedIn) {
    ops.append(el('button', {
      class: 'btn tiny ghost', type: 'button',
      on: {
        click: async () => {
          if (!(await confirmDialog('退出登录？', `会清掉 ${item.name} 在这台电脑上保存的 Cookie。`, { danger: true, okText: '退出登录' }))) return;
          try {
            await api.logoutSource(item.key);
            toast('已退出登录', { type: 'ok' });
            await refresh();
          } catch (error) {
            toast('退出失败', { type: 'error', detail: error.message });
          }
        },
      },
    }, '退出登录'));
    ops.append(el('button', {
      class: 'btn tiny', type: 'button',
      on: { click: () => openLogin(item) },
    }, '重新登录'));
  } else {
    ops.append(el('button', {
      class: 'btn tiny primary', type: 'button',
      on: { click: () => openLogin(item) },
    }, '去登录'));
  }

  return el('div', { class: 'login-row' },
    el('div', {},
      el('div', { class: 'lname' },
        el('span', { text: item.name || item.key }),
        el('span', { class: `login-state ${loggedIn ? 'on' : ''}`, text: loggedIn ? '已登录' : '未登录' }),
        item.supportsPassword ? el('span', { class: 'chip', text: '支持账号密码' }) : null,
      ),
      el('div', { class: 'lhint', text: cookieText }),
    ),
    ops,
  );
}

function loginPanel() {
  const logins = cache?.logins || [];
  const panel = el('div', { class: 'panel' },
    el('h2', { text: '图源登录' }),
    el('p', {
      class: 'hint',
      text: '登录用漫画站点自己的 Cookie：点「去登录」会打开该站点的登录页，登好回来点「完成登录」，就把 Cookie 存到本机（只存在本机，用来取会员章节）。',
    }),
  );
  if (!logins.length) {
    panel.append(el('div', { class: 'hint', text: '没有可登录的图源。' }));
  } else {
    for (const item of logins) panel.append(loginRow(item));
  }
  return panel;
}

async function openLogin(item) {
  // 打开站点登录页（在本机的浏览器窗口里）
  try {
    await api.loginOpen(item.key);
  } catch (error) {
    toast('打不开登录页', { type: 'error', detail: error.message });
    return;
  }

  const hint = el('p', { class: 'hint', text: `在弹出的窗口里登录 ${item.name}，登好后点下面的「我已登录」。` });
  const status = el('div', { class: 'hint', style: 'min-height:18px' });
  const account = el('input', { type: 'text', placeholder: '账号 / 手机号 / 邮箱', autocomplete: 'off' });
  const password = el('input', { type: 'password', placeholder: '密码', autocomplete: 'off' });

  const finish = el('button', {
    class: 'btn primary', type: 'button',
    on: {
      click: async () => {
        finish.disabled = true;
        status.textContent = '正在读取登录状态…';
        try {
          const result = await api.loginCapture(item.key);
          toast(`登录成功，抓到 ${result.cookies || 0} 条 Cookie`, { type: 'ok' });
          closeModal();
          await refresh();
          await loadSources();
        } catch (error) {
          status.textContent = '';
          toast('没抓到登录状态', { type: 'error', detail: error.message });
        } finally {
          finish.disabled = false;
        }
      },
    },
  }, '我已登录');

  const body = el('div', {},
    el('h2', { text: `登录 ${item.name}` }),
    hint,
    el('p', { class: 'hint', text: `登录页：${item.loginUrl || '站点首页'}` }),
    item.supportsPassword ? el('div', { style: 'margin-top:14px' },
      el('div', { class: 'field' }, el('label', { text: '账号密码登录（可选）' }), account),
      el('div', { class: 'field' }, password),
      el('button', {
        class: 'btn small', type: 'button',
        on: {
          click: async () => {
            const accountValue = account.value.trim();
            if (!accountValue || !password.value) {
              toast('账号和密码都要填', { type: 'warn' });
              return;
            }
            status.textContent = '正在登录…';
            try {
              await api.loginPassword(item.key, accountValue, password.value);
              toast('登录成功', { type: 'ok' });
              closeModal();
              await refresh();
              await loadSources();
            } catch (error) {
              status.textContent = '';
              toast('登录失败', { type: 'error', detail: error.message });
            }
          },
        },
      }, '用账号密码登录'),
    ) : null,
    status,
    el('div', { class: 'modal-actions' },
      el('button', {
        class: 'btn ghost', type: 'button',
        on: { click: () => closeModal() },
      }, '稍后再说'),
      finish,
    ),
  );
  openModal(body);
}

/* ------------------------------ 卡片 ------------------------------ */
function builtinCard(source) {
  return el('div', { class: 'plugin-card' },
    el('div', { class: 'top' },
      el('span', { class: 'icon', text: source.icon || '📚' }),
      el('strong', { text: source.name || source.key }),
      el('span', { class: 'chip', text: '内置' }),
      source.nsfw ? el('span', { class: 'badge nsfw', text: '18+' }) : null,
    ),
    el('div', { class: 'desc', text: source.description || `图源标识：${source.key}` }),
    el('div', { class: 'detail-meta', style: 'font-size:11.5px' },
      el('span', { class: 'chip', text: source.key }),
      source.base_url
        ? el('a', {
          class: 'chip',
          attrs: { href: source.base_url, target: '_blank', rel: 'noopener noreferrer' },
          text: '打开站点',
        })
        : null,
    ),
    el('div', { class: 'ops' },
      el('span', {
        class: 'chip',
        text: '随程序提供 · 想改造请按指南写用户插件',
        style: 'font-size:11px',
      }),
    ),
  );
}

function pluginCard(plugin) {
  const isUser = String(plugin.origin || '').includes('用户插件');
  return el('div', { class: `plugin-card ${plugin.error ? 'broken' : ''}` },
    el('div', { class: 'top' },
      el('span', { class: 'icon', text: plugin.icon || '🧩' }),
      el('strong', { text: plugin.name || plugin.key }),
      el('span', { class: 'chip', text: `v${plugin.version || '0.1'}` }),
      plugin.nsfw ? el('span', { class: 'badge nsfw', text: '18+' }) : null,
    ),
    el('div', { class: 'desc', text: plugin.description || '（没有写说明）' }),
    el('div', { class: 'detail-meta', style: 'font-size:11.5px' },
      el('span', { class: 'chip', text: `@${plugin.author || '匿名'}` }),
      el('span', { class: 'chip', text: isUser ? '用户插件' : '内置示例' }),
      plugin.error ? el('span', { class: 'chip', style: 'color:var(--danger)', text: `⚠ ${plugin.error}` }) : null,
    ),
    el('div', { class: 'ops' },
      isUser ? el('button', {
        class: 'btn tiny', type: 'button',
        on: { click: () => editPlugin(plugin) },
      }, '编辑源码') : el('button', {
        class: 'btn tiny primary', type: 'button',
        on: { click: () => copyPlugin(plugin) },
      }, '复制成我的'),
      isUser ? el('button', {
        class: 'btn tiny ghost', type: 'button',
        on: { click: () => deletePlugin(plugin) },
      }, '删除') : null,
      el('button', {
        class: 'btn tiny ghost', type: 'button',
        on: {
          click: async () => {
            try { await api.openFolder(plugin.origin || ''); } catch (error) { toast(error.message, { type: 'error' }); }
          },
        },
      }, '位置'),
    ),
  );
}

/* ------------------------------ 动作 ------------------------------ */
async function createPlugin() {
  const name = await promptDialog('新建插件', {
    label: '插件标识（小写字母/数字/下划线，例如 my_source）',
    placeholder: 'my_source',
    okText: '下一步',
    hint: '标识要唯一；会用来生成文件名和内部 key。',
  });
  if (!name) return;
  const display = await promptDialog('新建插件', {
    label: '显示名称（界面上看到的名字）',
    value: name,
    okText: '下一步',
  });
  if (!display) return;
  const baseUrl = await promptDialog('新建插件', {
    label: '站点首页（可留空，之后自己改）',
    placeholder: 'https://example.com',
    okText: '生成模板',
  });
  if (baseUrl === null) return;

  let created;
  try {
    created = api.pluginTemplate
      ? await api.pluginTemplate(name, display, baseUrl || 'https://example.com')
      : { filename: `${name}.py`, content: cache?.template || '' };
  } catch (error) {
    toast('模板生成失败', { type: 'error', detail: error.message });
    return;
  }
  await openEditor({
    title: `新插件 · ${display}`,
    filename: created?.filename || `${String(name).replace(/[^a-z0-9_]/g, '_') || 'my_source'}.py`,
    text: created?.content || '',
    created: true,
  });
}

async function editPlugin(plugin) {
  if (!String(plugin.origin || '').includes('用户插件')) {
    toast('内置示例不能直接改，先「复制成我的」', { type: 'info' });
    return;
  }
  try {
    const data = await api.pluginSource(plugin.origin);
    await openEditor({ title: `编辑 · ${plugin.name}`, filename: data.file || `${plugin.key}.py`, text: data.content || '' });
  } catch (error) {
    toast('读不到源码', { type: 'error', detail: error.message });
  }
}

function openEditor({ title, filename = '', text = '', created = false }) {
  return new Promise((resolve) => {
    const nameInput = el('input', { type: 'text', value: filename, placeholder: 'my_source.py' });
    const area = el('textarea', { spellcheck: 'false' });
    area.value = text;
    let closed = false;

    const body = el('div', {},
      el('h2', { text: title }),
      el('p', { class: 'hint', text: '保存后会立刻热重载；写错了会在下面的失败列表里看到原因。' }),
      el('div', { class: 'field' },
        el('label', { text: '文件名（必须 .py，保存到用户插件目录）' }),
        nameInput,
      ),
      el('div', { class: 'field' },
        el('label', { text: '源码' }),
        area,
      ),
      el('div', { class: 'modal-actions' },
        el('button', {
          class: 'btn ghost', type: 'button',
          on: { click: () => finish(false) },
        }, '取消'),
        el('button', {
          class: 'btn primary', type: 'button',
          on: {
            click: async (event) => {
              const file = nameInput.value.trim();
              if (!/\.py$/i.test(file)) {
                toast('文件名要以 .py 结尾', { type: 'error' });
                return;
              }
              const button = event.target;
              button.disabled = true;
              button.textContent = '保存中…';
              try {
                await api.savePlugin(file, area.value);
                toast('插件已保存并重载', { type: 'ok' });
                await loadSources();
                await refresh();
                finish(true);
              } catch (error) {
                toast('保存失败', { type: 'error', detail: error.message });
              } finally {
                button.disabled = false;
                button.textContent = created ? '保存并启用' : '保存';
              }
            },
          },
        }, created ? '保存并启用' : '保存'),
      ),
    );

    const finish = (ok) => {
      if (closed) return;
      closed = true;
      closeModal();
      resolve(ok);
    };

    openModal(body, {
      title,
      className: 'modal-wide',
      onClose: () => finish(false),
      dismissible: false,
    });
  });
}

async function uploadZip(file) {
  if (!/\.zip$/i.test(file.name)) {
    toast('只支持 .zip 插件包', { type: 'error' });
    return;
  }
  try {
    const result = await api.uploadPlugin(file);
    toast(`插件已安装：${result.count || 0} 个文件`, { type: 'ok' });
    await loadSources();
    await refresh();
  } catch (error) {
    toast('导入失败', { type: 'error', detail: error.message });
  }
}

async function reloadAll() {
  try {
    const result = await api.reloadPlugins();
    await loadSources();
    await refresh();
    const count = (result.plugins || []).length;
    toast(`重载完成：${count} 个用户插件`, { type: 'ok' });
  } catch (error) {
    toast('重载失败', { type: 'error', detail: error.message });
  }
}

async function deletePlugin(plugin) {
  const ok = await confirmDialog(
    '删除插件？',
    `《${plugin.name || plugin.key}》的源码会从磁盘删掉，删了可以重新导入。`,
    { danger: true, okText: '删除' },
  );
  if (!ok) return;
  try {
    await api.deletePlugin(plugin.key);
    toast('插件已删除', { type: 'ok' });
    await loadSources();
    await refresh();
  } catch (error) {
    toast('删除失败', { type: 'error', detail: error.message });
  }
}

async function copyPlugin(plugin) {
  try {
    const result = await api.copyPlugin(plugin.key);
    toast(`已复制到 ${result.copied || '用户插件目录'}`, { type: 'ok' });
    await loadSources();
    await refresh();
  } catch (error) {
    toast('复制失败', { type: 'error', detail: error.message });
  }
}

export function unmount() {
  rootRef = null;
  cache = null;
}
