/* 图源管理：开关、导入 .json、登录、删除。
 *
 * 内置图源的清单（enabled）只是出厂默认值，用户在页面上的开关由后端存进
 * config/sources-enabled.json 覆盖层，所以关掉的图源还会留在这里，随时能再打开。
 */
import { api } from './api.js';
import { el, clear, toast, confirmDialog, openModal, closeModal, navigate } from './util.js';
import { loadSources } from './app.js';

let rootRef = null;
let aliveFlag = false;
let state = { sources: [], logins: [], dir: '', sourceDir: '' };

export async function render(root) {
  rootRef = root;
  aliveFlag = true;
  clear(root);
  root.append(el('div', { class: 'load-bar', style: 'padding:80px 0' },
    el('div', { class: 'spinner' }),
    el('span', { text: '呆猫在读图源清单…' })));
  await refresh();
}

export function unmount() {
  aliveFlag = false;
  rootRef = null;
}

async function refresh() {
  if (!aliveFlag) return;
  try {
    const [list, pluginInfo] = await Promise.all([
      api.sources(),
      api.plugins().catch(() => ({})),
    ]);
    const items = Array.isArray(list) ? list : (list.sources || []);
    let logins = [];
    try {
      const raw = (await api.logins()).logins || [];
      logins = (Array.isArray(raw) ? raw : Object.values(raw)).filter((item) => item && item.key);
    } catch { /* 登录状态读不到不影响开关 */ }
    state = {
      sources: items,
      logins,
      dir: pluginInfo?.dir || '',
      sourceDir: pluginInfo?.sourceDir || '',
    };
  } catch (error) {
    if (!aliveFlag) return;
    clear(rootRef);
    rootRef.append(el('div', { class: 'empty-state' },
      el('strong', { text: '图源列表读不出来' }),
      el('div', { text: error.message }),
      el('button', {
        class: 'btn primary', type: 'button',
        on: { click: () => refresh() },
      }, '再试一次')));
    return;
  }
  if (aliveFlag) draw();
}

function draw() {
  const root = rootRef;
  if (!root) return;
  clear(root);

  const items = state.sources;
  const enabled = items.filter((item) => item.enabled !== false);
  const disabled = items.filter((item) => item.enabled === false);

  root.append(el('div', { class: 'view-head' },
    el('div', {},
      el('div', { class: 'sub', text: '打开、关闭、导入图源；关掉的也留在这里，随时能再打开。' }),
      el('h1', {}, el('span', { class: 'stroke', text: '图源管理' })),
    ),
    el('div', { class: 'head-actions' },
      el('label', { class: 'btn primary', style: 'cursor:pointer' },
        '📥 导入 .json',
        el('input', {
          type: 'file', accept: '.json,application/json', style: 'display:none',
          on: {
            change: async (event) => {
              const file = event.target.files?.[0];
              event.target.value = '';
              if (file) await importJson(file);
            },
          },
        }),
      ),
      state.sourceDir
        ? el('button', {
          class: 'btn ghost', type: 'button',
          on: {
            click: async () => {
              try { await api.openFolder(state.sourceDir); } catch (error) { toast(error.message, { type: 'error' }); }
            },
          },
        }, '📂 图源目录')
        : null,
    ),
  ));

  root.append(el('div', { class: 'panel' },
    el('div', { class: 'src-summary' },
      el('span', { class: 'src-count' },
        el('strong', { text: String(enabled.length) }),
        el('span', { text: ` 个已启用 / 共 ${items.length} 个` })),
    ),
    el('p', {
      class: 'hint',
      text: '程序自带的图源默认全部关闭，方便你按需要打开。数据都由本机去目标站点获取，'
        + '没有经过任何中间服务器；请自行确认你所在地区的法律与目标站点的服务条款，风险自负。',
    }),
  ));

  if (!items.length) {
    root.append(el('div', { class: 'empty-state', style: 'padding:40px' },
      el('strong', { text: '一个图源都没有' }),
      el('div', { text: '可以点上面的「导入 .json」加一个自己写的规则图源。' })));
    return;
  }

  if (enabled.length) {
    root.append(sectionHead(`已启用（${enabled.length}）`, '点开关就能关掉，配置不会丢。'));
    const grid = el('div', { class: 'plugin-grid' });
    for (const item of enabled) grid.append(sourceCard(item, true));
    root.append(grid);
  }

  if (disabled.length) {
    root.append(sectionHead(`已关闭（${disabled.length}）`, '打开后立刻可用；内置图源来自公开站点，仅供学习交流。'));
    const grid = el('div', { class: 'plugin-grid' });
    for (const item of disabled) grid.append(sourceCard(item, false));
    root.append(grid);
  }

  root.append(el('div', { class: 'panel', style: 'margin-top:14px' },
    el('h2', { text: '想要更多图源？' }),
    el('p', {
      class: 'hint',
      text: '规则型图源就是一个 .json（engine 为 rules，里面写 CSS 选择器），导入后自动出现在上面的列表里。'
        + '需要 Python 逻辑的图源请用插件形式放到用户插件目录。',
    }),
    el('div', { class: 'row', style: 'gap:8px;flex-wrap:wrap' },
      el('button', {
        class: 'btn small', type: 'button',
        on: { click: () => navigate('#/plugins') },
      }, '🧩 打开图源插件'),
      state.dir
        ? el('button', {
          class: 'btn small ghost', type: 'button',
          on: {
            click: async () => {
              try { await api.openFolder(state.dir); } catch (error) { toast(error.message, { type: 'error' }); }
            },
          },
        }, '📂 用户插件目录')
        : null,
    ),
  ));
}

function sectionHead(title, hint) {
  return el('div', { class: 'src-section-head' },
    el('h2', { text: title }),
    el('p', { class: 'hint', text: hint }));
}

function sourceCard(item, on) {
  const login = state.logins.find((entry) => entry.key === item.key);
  const ops = el('div', { class: 'ops' });

  const toggle = el('button', {
    class: `src-switch ${on ? 'on' : ''}`,
    type: 'button',
    title: on ? '点击关闭' : '点击打开',
    attrs: { 'aria-pressed': on ? 'true' : 'false' },
    on: {
      click: async () => {
        toggle.disabled = true;
        try {
          await api.setSourceEnabled(item.key, !on);
          toast(`${item.name || item.key} 已${on ? '关闭' : '打开'}`, { type: 'ok' });
          await loadSources();
          await refresh();
        } catch (error) {
          toggle.disabled = false;
          toast('切换失败', { type: 'error', detail: error.message });
        }
      },
    },
  }, el('span', { class: 'knob' }));

  ops.append(el('span', { class: 'src-state' },
    el('span', { class: `src-dot ${on ? 'on' : ''}` }),
    el('span', { text: on ? '已启用' : '已关闭' })));
  ops.append(toggle);

  if (login && on) {
    ops.append(el('button', {
      class: 'btn tiny', type: 'button',
      on: { click: () => openLogin(login) },
    }, login.loggedIn ? '账号已登录' : '登录账号'));
    if (login.loggedIn) {
      ops.append(el('button', {
        class: 'btn tiny ghost', type: 'button',
        on: { click: () => navigate(`#/favorites?source=${encodeURIComponent(item.key)}`) },
      }, '查看收藏'));
      ops.append(el('button', {
        class: 'btn tiny ghost', type: 'button',
        on: {
          click: async () => {
            const ok = await confirmDialog('退出登录？', `退出「${login.name || item.name || item.key}」后，这里就读不到它的收藏了。`, { danger: true, okText: '退出' });
            if (!ok) return;
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
    }
  }

  if (item.kind === 'plugin') {
    ops.append(el('button', {
      class: 'btn tiny ghost', type: 'button',
      on: { click: () => removeSource(item) },
    }, '删除'));
  }

  return el('div', { class: `plugin-card src-card ${on ? '' : 'off'}` },
    el('div', { class: 'top' },
      el('span', { class: 'icon', text: item.icon || '📚' }),
      el('strong', { text: item.name || item.key }),
      item.kind === 'plugin'
        ? el('span', { class: 'chip plugin', text: '导入' })
        : el('span', { class: 'chip', text: '内置' }),
      item.nsfw ? el('span', { class: 'badge nsfw', text: '18+' }) : null,
    ),
    el('div', {
      class: 'desc',
      text: item.description
        || (item.kind === 'plugin' ? '导入的规则图源，可以随时删掉。' : '内置图源适配器，直接打开就能用。'),
    }),
    el('div', { class: 'detail-meta', style: 'font-size:11.5px' },
      el('span', { class: 'chip', text: item.key }),
      item.baseUrl
        ? el('a', {
          class: 'chip',
          attrs: { href: item.baseUrl, target: '_blank', rel: 'noopener noreferrer' },
          text: '打开站点',
        })
        : null,
      item.error
        ? el('span', { class: 'chip', style: 'color:var(--danger)', text: `⚠ ${item.error}` })
        : null,
    ),
    ops,
  );
}

/* ------------------------------ 动作 ------------------------------ */

async function importJson(file) {
  let text = '';
  try {
    text = await file.text();
  } catch (error) {
    toast('读不了这个文件', { type: 'error', detail: error.message });
    return;
  }
  let cfg = null;
  try {
    cfg = JSON.parse(text);
  } catch (error) {
    toast('JSON 格式不对', { type: 'error', detail: error.message });
    return;
  }
  if (!cfg || typeof cfg !== 'object' || Array.isArray(cfg)) {
    toast('文件内容不是单个图源清单', { type: 'error' });
    return;
  }
  const engine = String(cfg.engine || '').toLowerCase();
  if (engine !== 'rules') {
    toast('这个图源不是规则型，没法直接导入', {
      type: 'warn',
      detail: '需要 Python 逻辑的图源请放到用户插件目录（.py 或带 plugin.json 的文件夹）。',
    });
    return;
  }
  const key = String(cfg.key || file.name.replace(/\.json$/i, '')).trim().toLowerCase();
  if (!/^[a-z][a-z0-9_]{1,31}$/.test(key)) {
    toast('图源标识不合法', {
      type: 'error',
      detail: 'key 要小写字母开头，只含小写字母 / 数字 / 下划线，2-32 位。',
    });
    return;
  }
  const ok = await confirmDialog(
    `导入「${cfg.name || key}」？`,
    '会写进用户插件目录并立刻启用；写错了可以在这里删掉。',
    { okText: '导入' },
  );
  if (!ok) return;
  try {
    await api.savePlugin(`${key}.json`, text);
    toast('导入成功，已启用', { type: 'ok' });
    await loadSources();
    await refresh();
  } catch (error) {
    toast('导入失败', { type: 'error', detail: error.message });
  }
}

async function removeSource(item) {
  const ok = await confirmDialog(
    `删除「${item.name || item.key}」？`,
    '会把用户插件目录里的这个 .json 删掉，内置图源不能删。',
    { danger: true, okText: '删除' },
  );
  if (!ok) return;
  try {
    await api.deletePlugin(item.key);
    toast('已删除', { type: 'ok' });
    await loadSources();
    await refresh();
  } catch (error) {
    toast('删除失败', { type: 'error', detail: error.message });
  }
}

/* ------------------------------ 登录 ------------------------------ */

async function openLogin(item) {
  try {
    const result = await api.loginOpen(item.key);
    showLoginModal(item, result);
  } catch (error) {
    toast('打不开登录页', { type: 'error', detail: error.message });
  }
}

function showLoginModal(item, opened) {
  const status = el('div', { class: 'hint', style: 'min-height:18px' });
  const account = el('input', { type: 'text', placeholder: '账号 / 手机号 / 邮箱', autocomplete: 'off' });
  const password = el('input', { type: 'password', placeholder: '密码', autocomplete: 'off' });

  const done = async (label) => {
    status.textContent = '正在读取登录状态…';
    try {
      const result = await api.loginCapture(item.key);
      toast(`${label}，已保存登录状态`, { type: 'ok' });
      closeModal();
      await loadSources();
      await refresh();
      return result;
    } catch (error) {
      status.textContent = '';
      toast('没抓到登录状态', { type: 'error', detail: error.message });
      return null;
    }
  };

  const body = el('div', {},
    el('h2', { text: `登录 ${item.name || item.key}` }),
    el('p', { class: 'hint', text: '登录用的是站点自己的账号，Cookie 只存在本机，用来取会员章节。' }),
    opened?.url
      ? el('p', { class: 'hint' }, '登录页：', el('a', {
        attrs: { href: opened.url, target: '_blank', rel: 'noopener noreferrer' },
        text: opened.url,
      }))
      : null,
    item.supportsPassword
      ? el('div', { style: 'margin-top:12px' },
        el('div', { class: 'field' }, el('label', { text: '账号密码登录（可选）' }), account),
        el('div', { class: 'field' }, password),
        el('button', {
          class: 'btn small', type: 'button',
          on: {
            click: async () => {
              const name = account.value.trim();
              if (!name || !password.value) {
                toast('账号和密码都要填', { type: 'warn' });
                return;
              }
              status.textContent = '正在登录…';
              try {
                await api.loginPassword(item.key, name, password.value);
                toast('登录成功', { type: 'ok' });
                closeModal();
                await loadSources();
                await refresh();
              } catch (error) {
                status.textContent = '';
                toast('登录失败', { type: 'error', detail: error.message });
              }
            },
          },
        }, '用账号密码登录'),
      )
      : null,
    status,
    el('div', { class: 'modal-actions' },
      el('button', {
        class: 'btn ghost', type: 'button',
        on: { click: () => closeModal() },
      }, '稍后再说'),
      el('button', {
        class: 'btn primary', type: 'button',
        on: { click: () => done('登录成功') },
      }, '我已登录'),
    ),
  );
  openModal(body);
}
