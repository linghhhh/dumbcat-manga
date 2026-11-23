/* 应用外壳：设置、图源栏、路由、全局搜索、下载徽标、快捷键。 */
import { api, subscribeEvents, VERSION, withToken } from './api.js';
import { $, el, clear, toast, openModal, closeModal, initModal, saveLocal, loadLocal } from './util.js';
import { setCoverConcurrency } from './lazy.js';
import * as browse from './browse.js';
import * as search from './search.js';
import * as detail from './detail.js';
import * as reader from './reader.js';
import * as downloads from './downloads.js';
import * as shelf from './shelf.js';
import * as plugins from './plugins.js';
import * as sourcesPage from './sources.js';
import * as settings from './settings.js';
import * as mine from './mine.js';

const ROUTES = [
  { name: 'browse', pattern: /^\/?browse\/?$/, mod: browse, title: '发现' },
  { name: 'search', pattern: /^\/?search\/?$/, mod: search, title: '搜索' },
  { name: 'series', pattern: /^\/?series\/(.+)$/, mod: detail, title: '作品' },
  { name: 'read', pattern: /^\/?read\/(.+)$/, mod: reader, title: '阅读' },
  { name: 'downloads', pattern: /^\/?downloads\/?$/, mod: downloads, title: '下载' },
  { name: 'shelf', pattern: /^\/?shelf\/?$/, mod: shelf, title: '书架' },
  { name: 'plugins', pattern: /^\/?plugins\/?$/, mod: plugins, title: '图源插件' },
  { name: 'sources', pattern: /^\/?sources\/?$/, mod: sourcesPage, title: '图源管理' },
  { name: 'settings', pattern: /^\/?settings\/?$/, mod: settings, title: '设置' },
  { name: 'mine', pattern: /^\/?mine\/?$/, mod: mine, title: '我的' },
];

export const App = {
  settings: null,
  sources: [],
  stats: {},
  route: { name: '', params: {} },
  chapters: new Map(), // seriesUid -> 章节列表（详情页写，阅读器读）
  view: null,
  version: VERSION,
};

let currentModule = null;
let eventStream = null;
let routeToken = 0;

/* 全局回退：记录用户在当前页面里走过多少步，供顶栏「返回」和 Alt+← / 退格 使用。 */
let backDepth = 0;
let backSuppressed = false;
let booted = false;
const MAX_BACK_DEPTH = 40;

export function canGoBack() {
  return backDepth > 0;
}

function syncBackButton() {
  const button = $('#backBtn');
  if (button) button.disabled = backDepth <= 0;
}

export function goBack() {
  if (document.body.classList.contains('reading')) return; // 阅读器自己处理返回
  if (backDepth <= 0) {
    if (!/^#\/browse/.test(location.hash || '')) navigate('#/browse');
    return;
  }
  backDepth = Math.max(0, backDepth - 1);
  backSuppressed = true;
  syncBackButton();
  history.back();
  // back() 是异步的：等 hashchange 走完再解除抑制
  setTimeout(() => { backSuppressed = false; }, 300);
}

/* ------------------------------ 设置应用 ------------------------------ */
export function applySettings(next) {
  App.settings = next;
  window.__DUMBCAT_SETTINGS__ = next;
  document.body.dataset.theme = next.theme === 'night' ? 'night' : 'cream';
  document.documentElement.style.setProperty('--cover-min', `${Math.max(120, Number(next.grid_min_width) || 168)}px`);
  document.body.classList.toggle('reduce-motion', Boolean(next.reduce_motion));
  setCoverConcurrency(next.image_workers);
  saveLocal('theme', next.theme);
}

/* ------------------------------ 图源栏 ------------------------------ */
/* 图源健康度：绿=刚成功过，黄=失败过一次但之前是通的，红=连续失败 ≥2 */
function healthClass(source) {
  if (source.error) return 'fail';
  const health = source.health;
  if (!health) return '';
  if (health.ok) return '';
  return (health.fails || 0) >= 2 ? 'fail' : 'warn';
}

function sourceTitle(source) {
  const base = source.description || source.name || '';
  const health = source.health;
  if (!health) return base;
  const bits = [];
  if (health.lastMs) bits.push(`上次 ${Math.round(health.lastMs)}ms`);
  if (health.fails) bits.push(`连续失败 ${health.fails} 次`);
  if (health.lastError) bits.push(health.lastError);
  return bits.length ? `${base} · ${bits.join(' · ')}` : base;
}

export function renderSources() {
  const list = $('#sourceList');
  if (!list) return;
  clear(list);
  if (!App.sources.length) {
    list.append(el('div', { class: 'skeleton-line' }));
    return;
  }
  for (const source of App.sources) {
    const active = App.route.name === 'browse' && App.route.params.source === source.key;
    const button = el('button', {
      class: `source-item ${active ? 'active' : ''}`,
      type: 'button',
      dataset: { kind: source.kind || 'builtin', key: source.key },
      title: source.error ? `加载失败：${source.error}` : sourceTitle(source),
      on: {
        click: () => {
          document.body.classList.remove('sidebar-open');
          location.hash = `#/browse?source=${encodeURIComponent(source.key)}`;
        },
      },
    },
      el('span', { class: `dot-mark ${healthClass(source)}` }),
      el('span', { text: source.name || source.key, style: 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap' }),
      source.error ? el('span', { class: 'err', text: '!' }) : null,
      source.nsfw ? el('span', { class: 'tag-18', text: '18+' }) : null,
      source.kind === 'plugin' ? el('span', { class: 'chip plugin', style: 'margin-left:auto;padding:0 4px;font-size:9px' }, '插件') : null,
    );
    list.append(button);
  }
}

export async function loadSources(options = {}) {
  try {
    const data = await api.sources();
    App.sources = data.sources || [];
    App.stats = data.stats || {};
    renderSources();
    if (options.announce) {
      const failed = App.sources.filter((item) => item.error).length;
      toast(
        failed ? `图源已刷新：${App.sources.length} 个可用，${failed} 个加载失败` : `图源已刷新：${App.sources.length} 个`,
        { type: failed ? 'warn' : 'ok' },
      );
    }
    return App.sources;
  } catch (error) {
    toast('图源加载失败', { type: 'error', detail: error.message });
    throw error;
  }
}

export function defaultSource() {
  const stored = App.settings && App.settings.last_source;
  if (stored && App.sources.some((item) => item.key === stored)) return stored;
  return App.sources[0]?.key || '';
}

/* ------------------------------ 导航 ------------------------------ */
export function navigate(hash) {
  // 同一个路由不重复渲染：重复渲染会让正在加载的视图丢掉自己的 ctx.alive，
  // 旧请求回来时被丢弃，新的视图又因为 loading 卡住，最后永远停在骨架屏。
  if (location.hash === hash) return;
  location.hash = hash;
}

function parseHash() {
  const raw = location.hash.replace(/^#/, '');
  const [pathPart, queryPart] = raw.split('?');
  const params = Object.fromEntries(new URLSearchParams(queryPart || ''));
  const path = pathPart || '/browse';
  for (const route of ROUTES) {
    const match = path.match(route.pattern);
    if (match) {
      if (route.name === 'series') params.uid = decodeURIComponent(match[1]);
      if (route.name === 'read') params.chapterUid = decodeURIComponent(match[1]);
      return { name: route.name, params, route };
    }
  }
  return { name: 'browse', params, route: ROUTES[0] };
}

function setNav(name) {
  for (const item of document.querySelectorAll('#mainNav .nav-item')) {
    item.classList.toggle('active', item.dataset.nav === name
      || (name === 'series' && item.dataset.nav === 'browse')
      || (name === 'read' && item.dataset.nav === 'browse'));
  }
  // 手机底部导航：详情/阅读归到「发现」，插件页没有对应 tab
  const tabName = (name === 'series' || name === 'read') ? 'browse' : name;
  for (const item of document.querySelectorAll('#tabBar .tab-item')) {
    item.classList.toggle('active', item.dataset.tab === tabName);
  }
}

async function route() {
  const parsed = parseHash();
  if (booted && !backSuppressed) {
    backDepth = Math.min(MAX_BACK_DEPTH, backDepth + 1);
  }
  syncBackButton();
  const param = { ...parsed.params };
  if (parsed.name === 'browse' && !param.source) param.source = defaultSource();
  // 记住上次看的图源：下次启动用来预热第一页，打开就能直接命中缓存。
  if (parsed.name === 'browse' && param.source
      && (!App.settings || App.settings.last_source !== param.source)) {
    if (App.settings) App.settings.last_source = param.source;
    api.saveSettings({ last_source: param.source }).catch(() => {});
  }
  if (parsed.name === 'search' && param.q) $('#searchInput').value = param.q;
  App.route = { name: parsed.name, params: param };

  const view = $('#view');
  if (!view) return;
  setNav(parsed.name);
  document.body.classList.remove('sidebar-open');
  renderSources();
  const token = ++routeToken;

  if (currentModule && currentModule.unmount) {
    try { currentModule.unmount(); } catch (error) { console.warn(error); }
  }
  currentModule = parsed.route.mod;
  document.title = parsed.route.title === '阅读'
    ? '阅读 · 呆猫漫画'
    : `${parsed.route.title} · 呆猫漫画`;
  if (parsed.name !== 'read') clear(view);
  try {
    await currentModule.render(view, param, { token, alive: () => token === routeToken });
  } catch (error) {
    console.error(error);
    if (token !== routeToken) return;
    clear(view);
    view.append(el('div', { class: 'empty-state' },
      el('div', { class: 'big', text: '😿' }),
      el('strong', { text: '这一页没打开' }),
      el('div', { text: error.message || String(error) }),
      el('button', {
        class: 'btn primary', type: 'button',
        on: { click: () => route() },
      }, '再试一次'),
    ));
  }
  if (parsed.name !== 'read') {
    window.scrollTo({ top: 0 });
  }
  booted = true;
  syncBackButton();
}

/* ------------------------------ 初始化 ------------------------------ */
async function boot() {
  initModal();
  wireChrome();
  window.__DUMBCAT_APP__ = App;

  const tip = $('#bootTip');
  const setTip = (text) => { if (tip) tip.textContent = text; };

  let settings = null;
  setTip('正在连接本地服务…');
  try {
    settings = await api.settings();
  } catch (error) {
    showBootError(error);
    return;
  }
  applySettings(settings);

  setTip('正在加载图源…');
  try {
    await loadSources();
  } catch (error) {
    showBootError(error);
    return;
  }

  eventStream = subscribeEvents({
    message(data) {
      if (data && data.id && data.state) {
        window.dispatchEvent(new CustomEvent('dumbcat:task', { detail: data }));
      }
    },
  });

  window.addEventListener('hashchange', route);
  // 用 replaceState 而不是给 location.hash 赋值：赋值会再触发一次 hashchange，
  // 导致首屏被渲染两次。
  if (!location.hash) history.replaceState(null, '', '#/browse');
  // 先把界面放出来，首屏的图源内容在后台继续加载：
  // 之前这里 await route()，只要有一个图源慢，整个软件就停在启动画面。
  const bootEl = $('#boot');
  const appEl = $('#app');
  if (appEl) appEl.hidden = false;
  setTip('正在翻开第一页…');
  const firstPaint = route();
  if (bootEl) {
    bootEl.classList.add('done');
    setTimeout(() => bootEl.remove(), 340);
  }
  console.info(`呆猫漫画 v${VERSION} 已启动`);
  try {
    await firstPaint;
  } catch (error) {
    console.error(error);
  }
}

function showBootError(error) {
  const bootEl = $('#boot');
  if (!bootEl) return;
  bootEl.innerHTML = '';
  bootEl.append(
    el('div', { class: 'boot-error' },
      el('strong', { text: '没能连上本地服务' }),
      el('div', { text: error.message || String(error) }),
    ),
    el('button', {
      class: 'btn primary', type: 'button',
      on: { click: () => location.reload() },
    }, '重新连接'));
}

function wireChrome() {
  const form = $('#globalSearch');
  const input = $('#searchInput');
  if (form) {
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const q = input.value.trim();
      if (!q) {
        input.focus();
        toast('先输入想找的作品名吧', { type: 'info' });
        return;
      }
      location.hash = `#/search?q=${encodeURIComponent(q)}`;
    });
  }

  $('#backBtn')?.addEventListener('click', () => goBack());
  $('#openShelf')?.addEventListener('click', () => navigate('#/shelf'));
  $('#openSidebar')?.addEventListener('click', () => document.body.classList.toggle('sidebar-open'));
  $('#drawerMask')?.addEventListener('click', () => document.body.classList.remove('sidebar-open'));
  $('#reloadSources')?.addEventListener('click', async () => {
    const button = $('#reloadSources');
    if (button) button.disabled = true;
    try {
      const result = await api.reloadSources();
      await loadSources({ announce: true });
      window.dispatchEvent(new CustomEvent('dumbcat:sources', { detail: result }));
    } catch (error) {
      toast('图源重载失败', { type: 'error', detail: error.message });
    } finally {
      if (button) button.disabled = false;
    }
  });

  window.addEventListener('mouseup', (event) => {
    if (event.button === 3) {
      event.preventDefault();
      goBack();
    }
  });

  document.addEventListener('keydown', (event) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '')
      || document.activeElement?.isContentEditable;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      input?.focus();
      input?.select();
      return;
    }
    if (event.key === '/' && !typing) {
      event.preventDefault();
      input?.focus();
      return;
    }
    if (event.key === 'Escape' && document.body.classList.contains('sidebar-open')) {
      document.body.classList.remove('sidebar-open');
      return;
    }
    // 回退：Alt+← / 退格（输入框、阅读器里不抢）
    const busy = typing || document.body.classList.contains('reading');
    if (!busy && event.altKey && event.key === 'ArrowLeft') {
      event.preventDefault();
      goBack();
      return;
    }
    if (!busy && (event.key === 'Backspace' || event.key === 'BrowserBack')) {
      event.preventDefault();
      goBack();
    }
  });
}

// 供各视图复用的小状态
export function chapterCacheKey(seriesUid) {
  return seriesUid;
}

window.addEventListener('error', (event) => {
  console.error(event.error || event.message);
});

boot();
