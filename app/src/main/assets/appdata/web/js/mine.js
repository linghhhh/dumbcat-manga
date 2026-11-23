/* 我的：版本信息、书架/历史入口、缓存清理、关于。 */
import { api, VERSION, withToken } from './api.js';
import { el, fmtBytes, toast, confirmDialog, openModal, closeModal, navigate } from './util.js';

let aliveFlag = false;

function row(icon, label, hint, onClick) {
  return el('button', {
    class: 'mine-row', type: 'button',
    on: { click: onClick },
  },
  el('span', { class: 'mine-row-icon', text: icon }),
  el('span', { class: 'mine-row-text' },
    el('span', { class: 'mine-row-label', text: label }),
    hint ? el('span', { class: 'mine-row-hint', text: hint }) : null,
  ),
  el('span', { class: 'mine-row-arrow', text: '›' }));
}

export async function render(root, params, ctx = {}) {
  aliveFlag = true;
  const alive = () => aliveFlag && (ctx.alive ? ctx.alive() : true);

  const sourceCount = el('span', { class: 'mine-stat-n', text: '—' });
  const cacheSize = el('span', { class: 'mine-stat-n', text: '—' });

  root.append(el('div', { class: 'mine-page' },
    el('div', { class: 'mine-hero' },
      el('img', { class: 'mine-logo', src: '/static/assets/logo.png', alt: '呆猫漫画' }),
      el('div', { class: 'mine-hero-text' },
        el('h1', { class: 'mine-title', text: '呆猫漫画' }),
        el('p', { class: 'mine-sub', text: `v${VERSION} · 手机版` }),
      ),
    ),
    el('div', { class: 'mine-stats' },
      el('div', { class: 'mine-stat' }, sourceCount, el('span', { class: 'mine-stat-l', text: '图源' })),
      el('div', { class: 'mine-stat' }, cacheSize, el('span', { class: 'mine-stat-l', text: '图片缓存' })),
    ),
    el('div', { class: 'mine-list' },
      row('📚', '我的书架', '多个书架、图源收藏、自建书架', () => navigate('#/shelf')),
      row('🕘', '浏览历史', '看过的章节，接着上次的地方看', () => navigate('#/shelf?tab=history')),
      row('🔗', '图源管理', '打开/关闭、导入图源，管理登录账号', () => navigate('#/sources')),
      row('⬇️', '下载', '下载目录、每章图片数、同时下载几张', () => navigate('#/download-settings')),
      row('🧹', '清空图片缓存', '不会删已下载的漫画', async () => {
        const ok = await confirmDialog('清空图片缓存？', '下次看会重新下载，不会删已下载的漫画。', { danger: true, okText: '清空' });
        if (!ok) return;
        try {
          await api.clearCache();
          toast('缓存已清空', { type: 'ok' });
          loadStats();
        } catch (err) {
          toast(`清空失败：${err.message || err}`, { type: 'error' });
        }
      }),
      row('ℹ️', '关于呆猫', '版本、致谢', () => {
        openModal(el('div', { class: 'about-card' },
          el('img', { class: 'about-logo', src: '/static/assets/logo.png', alt: '' }),
          el('h2', { text: '呆猫漫画' }),
          el('p', { class: 'muted', text: `版本 v${VERSION}（安卓版）` }),
          el('p', { text: '把桌面版的抓取内核塞进了手机：Python 跑在本机，网页做界面，不经过任何第三方服务器。' }),
          el('p', { class: 'muted', text: '图源来自公开站点，仅供学习交流；喜欢请支持正版。' }),
          el('button', { class: 'btn', type: 'button', on: { click: () => closeModal() } }, '好'),
        ), { className: 'modal-about' });
      }),
    ),
  ));

  async function loadStats() {
    try {
      const data = await api.cacheStats();
      if (!alive()) return;
      cacheSize.textContent = fmtBytes(data.bytes ?? data.size ?? 0);
    } catch { /* 统计失败不影响页面 */ }
  }

  try {
    const list = await api.sources();
    if (alive()) {
      const items = Array.isArray(list) ? list : (list.sources || []);
      sourceCount.textContent = String(items.length);
    }
  } catch { /* 忽略 */ }
  loadStats();

  // 让图片走 token（static 目录不需要，留着以后换到 /img/ 时不用改）
  void withToken;
}

export function unmount() {
  aliveFlag = false;
}
