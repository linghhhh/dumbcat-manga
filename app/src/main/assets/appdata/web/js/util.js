/* 小工具：建 DOM、提示、弹窗、格式化。所有远端文本都走 textContent，避免注入。 */

export function $(selector, root) {
  return (root || document).querySelector(selector);
}

export function $$(selector, root) {
  return Array.from((root || document).querySelectorAll(selector));
}

const PROPS = new Set(['class', 'className', 'text', 'html', 'style', 'dataset', 'attrs', 'on', 'title']);

/**
 * el('div', { class: 'card', on: { click: fn }, dataset: { uid } }, '文本', child)
 */
export function el(tag, props, ...children) {
  const node = document.createElement(tag);
  if (props && typeof props === 'object' && !(props instanceof Node)) {
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'class' || key === 'className') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'html') node.innerHTML = value; // 仅用于内联 SVG 等可信内容
      else if (key === 'style') {
        if (typeof value === 'string') node.setAttribute('style', value);
        else Object.assign(node.style, value);
      } else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key === 'attrs') {
        for (const [name, val] of Object.entries(value)) {
          if (val !== null && val !== undefined && val !== false) node.setAttribute(name, val === true ? '' : val);
        }
      } else if (key === 'on') {
        for (const [name, fn] of Object.entries(value)) node.addEventListener(name, fn);
      } else if (key === 'title') node.title = value;
      else if (!PROPS.has(key)) node.setAttribute(key, value);
    }
  } else if (props instanceof Node) {
    children.unshift(props);
  } else if (typeof props === 'string') {
    children.unshift(props);
  }
  for (const child of children.flat(4)) {
    if (child === null || child === undefined || child === false || child === true) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/**
 * 给封面地址加上缩略图宽度（后端按 w 生成 WebP 缩略图）。
 * 列表页用小图、详情页用中大图，避免每张卡片都拉几百 KB 原图。
 */
export function coverSrc(url, width) {
  if (!url) return '';
  if (!width) return url;
  const join = url.includes('?') ? '&' : '?';
  return `${url}${join}w=${Math.round(width)}`;
}

export function clear(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function svg(markup) {
  const wrap = document.createElement('div');
  wrap.innerHTML = markup.trim();
  return wrap.firstElementChild;
}

export function debounce(fn, wait = 200) {
  let timer = 0;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

export function rafThrottle(fn) {
  let scheduled = false;
  let lastArgs = null;
  return (...args) => {
    lastArgs = args;
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      fn(...lastArgs);
    });
  };
}

export function fmtCount(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return '';
  if (n >= 100000000) return `${(n / 100000000).toFixed(1).replace(/\.0$/, '')}亿`;
  if (n >= 10000) return `${(n / 10000).toFixed(1).replace(/\.0$/, '')}万`;
  return String(Math.round(n));
}

export function fmtBytes(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

export function timeAgo(timestamp) {
  const seconds = Math.max(0, Date.now() / 1000 - Number(timestamp || 0));
  if (!Number.isFinite(seconds)) return '';
  if (seconds < 60) return '刚刚';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)} 天前`;
  return new Date(Number(timestamp) * 1000).toLocaleDateString('zh-CN');
}

export function fmtDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m > 0 ? `${m} 分 ${String(s).padStart(2, '0')} 秒` : `${s} 秒`;
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/* ------------------------------ Toast ------------------------------ */
const TOAST_ICONS = { ok: '✅', error: '😿', info: '🐾', warn: '⚠️' };

export function toast(message, options = {}) {
  const root = $('#toastRoot');
  if (!root) return null;
  const type = options.type || 'info';
  const node = el('div', { class: `toast ${type}` },
    el('span', { text: `${TOAST_ICONS[type] || ''} ${message}`.trim() }),
    options.detail ? el('small', { text: options.detail }) : null,
  );
  root.append(node);
  const ttl = options.ttl ?? (type === 'error' ? 5200 : 2600);
  const timer = setTimeout(() => {
    node.style.transition = 'opacity .2s ease, transform .2s ease';
    node.style.opacity = '0';
    node.style.transform = 'translateY(6px)';
    setTimeout(() => node.remove(), 220);
  }, ttl);
  node.addEventListener('click', () => {
    clearTimeout(timer);
    node.remove();
  });
  return node;
}

/* ------------------------------ Modal ------------------------------ */
let modalCloser = null;

export function openModal(content, options = {}) {
  const root = $('#modalRoot');
  const body = $('#modalBody');
  if (!root || !body) return;
  clear(body);
  body.className = `modal ${options.className || ''}`.trim();
  if (typeof content === 'string') body.innerHTML = content;
  else body.append(content);
  if (options.dismissible !== false) {
    body.prepend(el('button', {
      class: 'icon-btn modal-close',
      type: 'button',
      title: '关闭',
      'aria-label': '关闭',
      on: { click: () => closeModal() },
    }, '✕'));
  }
  root.hidden = false;
  document.body.style.overflow = 'hidden';
  modalCloser = options.onClose || null;
  const focusable = body.querySelector('input,textarea,select,button');
  if (focusable && options.autofocus !== false) {
    setTimeout(() => {
      try { focusable.focus({ preventScroll: true }); } catch {}
    }, 40);
  }
  return body;
}

export function closeModal() {
  const root = $('#modalRoot');
  if (!root || root.hidden) return;
  root.hidden = true;
  document.body.style.overflow = '';
  const closer = modalCloser;
  modalCloser = null;
  if (closer) {
    try { closer(); } catch (err) { console.warn(err); }
  }
}

export function initModal() {
  const root = $('#modalRoot');
  if (!root) return;
  root.addEventListener('click', (event) => {
    if (event.target instanceof Element && event.target.dataset.close) closeModal();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeModal();
  });
}

export function confirmDialog(title, message, options = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const body = openModal(el('div', {},
      el('h2', { text: title }),
      message ? el('p', { class: 'hint', text: message, style: 'margin-top:6px' }) : null,
      el('div', { class: 'modal-actions' },
        el('button', { class: 'btn ghost', type: 'button', on: { click: () => { finish(false); closeModal(); } } }, '算啦'),
        el('button', {
          class: `btn ${options.danger ? 'danger' : 'primary'}`,
          type: 'button',
          on: { click: () => { finish(true); closeModal(); } },
        }, options.okText || '确定'),
      ),
    ), { onClose: () => finish(false) });
    body.querySelector('.btn.primary,.btn.danger')?.focus();
  });
}

export function promptDialog(title, options = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const input = el('input', {
      type: 'text',
      value: options.value || '',
      placeholder: options.placeholder || '',
      style: 'width:100%',
    });
    const body = openModal(el('div', {},
      el('h2', { text: title }),
      options.hint ? el('p', { class: 'hint', text: options.hint }) : null,
      el('div', { class: 'field' }, options.label ? el('label', { text: options.label }) : null, input),
      el('div', { class: 'modal-actions' },
        el('button', { class: 'btn ghost', type: 'button', on: { click: () => { finish(null); closeModal(); } } }, '取消'),
        el('button', {
          class: 'btn primary', type: 'button',
          on: { click: () => { finish(input.value.trim()); closeModal(); } },
        }, options.okText || '确定'),
      ),
    ), { onClose: () => finish(null) });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        finish(input.value.trim());
        closeModal();
      }
    });
    setTimeout(() => input.focus(), 30);
  });
}

/* ------------------------------ Storage ------------------------------ */
export function loadLocal(key, fallback) {
  try {
    const raw = localStorage.getItem(`dumbcat.${key}`);
    return raw === null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

export function saveLocal(key, value) {
  try {
    localStorage.setItem(`dumbcat.${key}`, JSON.stringify(value));
  } catch {}
}

/* ------------------------------ URL ------------------------------ */
export function buildQuery(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, value);
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

export function navigate(hash) {
  if (location.hash === hash) {
    window.dispatchEvent(new HashChangeEvent('hashchange'));
  } else {
    location.hash = hash;
  }
}

export function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

export function sectionTitle(title, subtitle, actions) {
  return el('div', { class: 'view-head' },
    el('div', {},
      subtitle ? el('div', { class: 'sub', text: subtitle }) : null,
      el('h1', {}, el('span', { class: 'stroke', text: title })),
    ),
    actions ? el('div', { class: 'head-actions' }, actions) : null,
  );
}

export function emptyState(icon, title, hint, action) {
  return el('div', { class: 'empty-state' },
    el('div', { class: 'big', text: icon }),
    el('strong', { text: title }),
    hint ? el('div', { text: hint }) : null,
    action || null,
  );
}

export function sourceBadge(source) {
  const nsfw = source && source.nsfw ? el('span', { class: 'badge nsfw', text: '18+' }) : null;
  return nsfw;
}
