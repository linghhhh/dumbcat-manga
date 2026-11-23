/* 与本地服务通信：所有请求带 token；SSE 走 query 参数（EventSource 不能带 header）。 */

const BOOT = window.__DUMBCAT__ || {};
const QUERY = new URLSearchParams(location.search);

export const TOKEN = BOOT.token || QUERY.get('token') || '';
export const VERSION = BOOT.version || '3.0.0';

function ensureToken() {
  if (!TOKEN) {
    throw new Error('缺少访问令牌，请从启动器重新打开呆猫漫画');
  }
}

async function parseError(response) {
  const text = await response.text().catch(() => '');
  let detail = text;
  try {
    const data = JSON.parse(text);
    detail = data.detail || data.message || text;
    if (Array.isArray(detail)) detail = detail.map((item) => item.msg || JSON.stringify(item)).join('；');
  } catch {}
  const error = new Error(String(detail || `请求失败（HTTP ${response.status}）`));
  error.status = response.status;
  return error;
}

async function request(path, options = {}) {
  ensureToken();
  const headers = { 'X-DumbCat-Token': TOKEN, ...(options.headers || {}) };
  if (options.body !== undefined && !(options.body instanceof Blob) && typeof options.body !== 'string') {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeout || 120000);
  try {
    const response = await fetch(path, { ...options, headers, signal: controller.signal });
    if (!response.ok) throw await parseError(response);
    const type = response.headers.get('content-type') || '';
    if (!type.includes('application/json')) return response;
    return response.json();
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('请求超时，图源可能有点慢，稍后再试');
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export function withToken(url) {
  if (!url) return '';
  if (url.startsWith('data:') || url.startsWith('blob:')) return url;
  const separator = url.includes('?') ? '&' : '?';
  return url.includes('token=') ? url : `${url}${separator}token=${encodeURIComponent(TOKEN)}`;
}

export const api = {
  health: () => request('/api/health'),
  settings: () => request('/api/settings'),
  saveSettings: (patch) => request('/api/settings', { method: 'POST', body: patch }),
  sources: () => request('/api/sources'),
  /* 图源开关：关掉的图源会留在列表里，可以再打开 */
  setSourceEnabled: (key, enabled) => request(`/api/sources/${encodeURIComponent(key)}/enabled`, {
    method: 'POST',
    body: { enabled },
    timeout: 180000,
  }),
  reloadSources: () => request('/api/sources/reload', { method: 'POST', timeout: 180000 }),
  browse: ({ source, page = 1, category = '' }) =>
    request(`/api/browse?source=${encodeURIComponent(source)}&page=${page}&category=${encodeURIComponent(category)}`, { timeout: 150000 }),
  categories: (source) =>
    request(`/api/categories?source=${encodeURIComponent(source)}`, { timeout: 60000 }),
  search: ({ q, sources = '' }) =>
    request(`/api/search?q=${encodeURIComponent(q)}&sources=${encodeURIComponent(sources)}`, { timeout: 150000 }),
  series: (uid) => request(`/api/series/${uid}`, { timeout: 150000 }),
  refreshChapters: (uid) => request(`/api/series/${uid}/refresh`, { method: 'POST', timeout: 180000 }),
  chapter: (chapterUid) => request(`/api/read/${chapterUid}`, { timeout: 180000 }),
  download: ({ seriesUid, chapterUids, outputDir = '', workers, series, chapters }) =>
    request('/api/download', {
      method: 'POST',
      body: { seriesUid, chapterUids, outputDir, workers, series: series || {}, chapters: chapters || [] },
    }),
  downloads: () => request('/api/downloads'),
  cancelDownload: (id) => request(`/api/downloads/${id}/cancel`, { method: 'POST' }),
  retryDownload: (id) => request(`/api/downloads/${id}/retry`, { method: 'POST' }),
  clearDownloads: () => request('/api/downloads/clear', { method: 'POST' }),
  plugins: () => request('/api/plugins'),
  /* 图源登录（Cookie）：浏览器抓取 or 账号密码（部分源支持） */
  logins: () => request('/api/logins', { timeout: 60000 }),
  loginInfo: (key) => request(`/api/sources/${encodeURIComponent(key)}/login`),
  loginOpen: (key) => request(`/api/sources/${encodeURIComponent(key)}/login/open`, { method: 'POST', timeout: 60000 }),
  loginCapture: (key) => request(`/api/sources/${encodeURIComponent(key)}/login/capture`, { method: 'POST', timeout: 90000 }),
  loginPassword: (key, account, password) => request(`/api/sources/${encodeURIComponent(key)}/login/password`, {
    method: 'POST',
    body: { account, password },
    timeout: 120000,
  }),
  logoutSource: (key) => request(`/api/sources/${encodeURIComponent(key)}/logout`, { method: 'POST' }),
  pluginSource: (file) => request(`/api/plugins/file?file=${encodeURIComponent(file)}`),
  pluginTemplate: (name, display, baseUrl) => request(
    `/api/plugins/template?name=${encodeURIComponent(name || '')}`
    + `&display=${encodeURIComponent(display || '')}`
    + `&base_url=${encodeURIComponent(baseUrl || '')}`,
  ),
  savePlugin: (filename, content) => request('/api/plugins/save', { method: 'POST', body: { filename, content } }),
  uploadPlugin: async (file) => {
    ensureToken();
    const buffer = await file.arrayBuffer();
    return request('/api/plugins/upload', {
      method: 'POST',
      body: buffer,
      headers: { 'Content-Type': 'application/octet-stream', 'X-File-Name': file.name },
      timeout: 120000,
    });
  },
  reloadPlugins: () => request('/api/plugins/reload', { method: 'POST', timeout: 120000 }),
  deletePlugin: (key) => request(`/api/plugins/${encodeURIComponent(key)}`, { method: 'DELETE' }),
  copyPlugin: (key) => request(`/api/plugins/${encodeURIComponent(key)}/copy`, { method: 'POST' }),
  chooseFolder: (path = '') => request('/api/choose-folder', { method: 'POST', body: { path }, timeout: 300000 }),
  // 安卓端「选择文件夹」用：列某个目录下的子目录，path 空 = 推荐默认目录
  fsDirs: (path = '') => request(`/api/fs/dirs?path=${encodeURIComponent(path)}`, { timeout: 30000 }),
  // 安卓端存储授权状态：{granted, need, can_ask}，403 时用来提示去哪儿开
  storagePermission: () => request('/api/storage/permission', { timeout: 15000 }),
  // 拉起系统授权页（安卓）；老后端没有就是 404/501
  requestStorageAccess: () => request('/api/storage/permission', { method: 'POST', timeout: 120000 }),
  openFolder: (path) => request('/api/open-folder', { method: 'POST', body: { path } }),
  exportDownloads: (path = '') => request('/api/export', { method: 'POST', body: { path }, timeout: 600000 }),
  cacheStats: () => request('/api/cache/stats'),
  clearCache: () => request('/api/cache/clear', { method: 'POST' }),
  history: () => request('/api/history'),
  folders: () => request('/api/folders'),

  /* ------------------------------ 书架 ------------------------------ */
  shelf: () => request('/api/shelf'),
  createShelf: (name) => request('/api/shelf/create', { method: 'POST', body: { name } }),
  renameShelf: (id, name) => request('/api/shelf/rename', { method: 'POST', body: { id, name } }),
  deleteShelf: (id) => request('/api/shelf/delete', { method: 'POST', body: { id } }),
  shelfItems: (shelfId = '') => request(`/api/shelf/items${shelfId ? `?shelfId=${encodeURIComponent(shelfId)}` : ''}`),
  shelfAdd: (payload) => request('/api/shelf/add', { method: 'POST', body: payload, timeout: 60000 }),
  shelfRemove: (shelfId, uid) => request('/api/shelf/remove', { method: 'POST', body: { shelfId, uid }, timeout: 60000 }),
  shelfBulk: ({ shelfId, uids, action, targetShelfId = '' }) =>
    request('/api/shelf/bulk', { method: 'POST', body: { shelfId, uids, action, targetShelfId }, timeout: 60000 }),
  shelfPin: ({ shelfId, uid, pinned = true }) =>
    request('/api/shelf/pin', { method: 'POST', body: { shelfId, uid, pinned }, timeout: 60000 }),
  sourceFavorites: (key, pages = 0) =>
    request(`/api/sources/${encodeURIComponent(key)}/favorites?pages=${pages}`, { timeout: 240000 }),
  shelfSync: (source, pages = 0) =>
    request('/api/shelf/sync', { method: 'POST', body: { source, pages }, timeout: 240000 }),
  shelfStatus: (uid) => request(`/api/shelf/status?uid=${encodeURIComponent(uid)}`),
  shelfHistory: (limit = 100) => request(`/api/shelf/history?limit=${limit}`),
  shelfHistoryAdd: (payload) => request('/api/shelf/history', { method: 'POST', body: payload }),
  shelfHistoryClear: () => request('/api/shelf/history/clear', { method: 'POST' }),
  shelfHistoryRemove: (uid) => request('/api/shelf/history/remove', { method: 'POST', body: { uid } }),
  /* 阅读进度存在服务端 SQLite，清缓存 / 换壳之后还能接着看 */
  progress: (seriesUid) => request(`/api/progress/${encodeURIComponent(seriesUid || '')}`),
  saveProgress: (payload) => request('/api/progress', { method: 'POST', body: payload }),
  clearProgress: (seriesUid) => request('/api/progress/clear', { method: 'POST', body: { seriesUid } }),
};

/* ------------------------------ SSE：全局事件 ------------------------------ */
export function subscribeEvents(handlers = {}) {
  let source = null;
  let closed = false;
  let attempt = 0;

  const connect = () => {
    if (closed) return;
    source = new EventSource(`/api/events?token=${encodeURIComponent(TOKEN)}`);
    source.onopen = () => {
      attempt = 0;
      handlers.open && handlers.open();
    };
    source.onmessage = (event) => {
      if (!event.data) return;
      try {
        handlers.message && handlers.message(JSON.parse(event.data));
      } catch (error) {
        console.warn('事件解析失败', error);
      }
    };
    // 后端用 topic 作为事件名，这里统一转发
    ['download', 'downloads', 'sources', 'log', 'task', 'plugin', 'plugins'].forEach((name) => {
      source.addEventListener(name, (event) => {
        try {
          handlers.message && handlers.message({ event: name, ...JSON.parse(event.data) });
        } catch {}
      });
    });
    source.onerror = () => {
      source.close();
      if (closed) return;
      attempt += 1;
      setTimeout(connect, Math.min(15000, 800 * attempt));
    };
  };

  connect();
  return {
    close() {
      closed = true;
      if (source) source.close();
    },
  };
}

/**
 * 全局搜索流：逐源推送结果。
 * handlers.onStart(payload) / onResult(payload) / onDone(payload) / onError(err)
 */
export function streamSearch(query, sources, handlers = {}) {
  const params = new URLSearchParams({ q: query });
  if (sources) params.set('sources', sources);
  params.set('token', TOKEN);
  const source = new EventSource(`/api/search/stream?${params.toString()}`);
  let finished = false;

  const finish = () => {
    if (finished) return;
    finished = true;
    source.close();
  };

  source.addEventListener('start', (event) => {
    try { handlers.onStart && handlers.onStart(JSON.parse(event.data)); } catch {}
  });
  source.addEventListener('result', (event) => {
    try { handlers.onResult && handlers.onResult(JSON.parse(event.data)); } catch (error) { console.warn(error); }
  });
  source.addEventListener('done', (event) => {
    try { handlers.onDone && handlers.onDone(JSON.parse(event.data)); } catch {}
    finish();
  });
  source.onerror = () => {
    if (finished) return;
    finish();
    handlers.onError && handlers.onError(new Error('搜索连接中断，请重试'));
  };
  return { close: finish };
}
