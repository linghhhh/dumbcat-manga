/* 封面懒加载：靠近视口才请求（前后各 1400px 预热），加载过的封面不再卸载。
   封面现在走服务端缩略图（/img/cover/xxx?w=400 → WebP，通常几百字节到几十 KB），
   所以留着比反复请求划算：回滑、翻页、切分类都是秒开。 */
import { withToken } from './api.js';

const managers = new Set();
let concurrency = 6;
let inflight = 0;
const waiters = [];

function acquire() {
  if (inflight < concurrency) {
    inflight += 1;
    return Promise.resolve();
  }
  return new Promise((resolve) => waiters.push(resolve));
}

function release() {
  inflight = Math.max(0, inflight - 1);
  const next = waiters.shift();
  if (next) {
    inflight += 1;
    next();
  }
}

class Manager {
  constructor(container) {
    this.container = container;
    this.byCard = new Map();
    this.io = ('IntersectionObserver' in window)
      ? new IntersectionObserver((records) => {
        for (const record of records) {
          if (!record.isIntersecting) continue;
          const entry = this.byCard.get(record.target);
          if (entry) this.load(entry);
        }
      }, { rootMargin: '1400px 0px', threshold: 0.01 })
      : null;
  }

  scan() {
    // 清掉上一屏留下的死卡片
    for (const [card, entry] of this.byCard) {
      if (!card.isConnected) {
        this.byCard.delete(card);
        this.io?.unobserve(card);
        entry.state = 'idle';
      }
    }
    const cards = this.container.querySelectorAll('[data-cover]');
    for (const card of cards) {
      if (this.byCard.has(card)) continue;
      if (!card.dataset.cover) continue;
      const entry = { card, url: card.dataset.cover, state: 'idle', img: null };
      this.byCard.set(card, entry);
      if (this.io) this.io.observe(card);
      else this.load(entry);
    }
    this.prefetchNear();
  }

  /* 先把离屏幕最近的几张排进队列：IO 触发有延迟，首屏能少等一拍。 */
  prefetchNear() {
    const pending = [];
    const vh = window.innerHeight || 800;
    for (const entry of this.byCard.values()) {
      if (entry.state !== 'idle') continue;
      const rect = entry.card.getBoundingClientRect();
      const distance = rect.top > vh ? rect.top - vh : (rect.bottom < 0 ? -rect.bottom : 0);
      pending.push([distance, entry]);
    }
    pending.sort((a, b) => a[0] - b[0]);
    for (const [, entry] of pending.slice(0, concurrency * 2)) this.load(entry);
  }

  async load(entry) {
    if (entry.state !== 'idle') return;
    if (!entry.card.isConnected || !entry.url) return;
    entry.state = 'queued';
    await acquire();
    if (!entry.card.isConnected) {
      entry.state = 'idle';
      release();
      return;
    }
    entry.state = 'loading';
    const cover = entry.card.querySelector('.cover') || entry.card;
    let img = cover.querySelector('img');
    if (!img) {
      img = document.createElement('img');
      img.alt = '';
      img.decoding = 'async';
      img.loading = 'lazy';
      img.draggable = false;
      cover.prepend(img);
    }
    entry.img = img;
    const finish = (ok) => {
      entry.state = ok ? 'loaded' : 'failed';
      cover.classList.remove('skeleton');
      if (ok) {
        img.classList.add('loaded');
        cover.querySelector('.noimg')?.remove();
      } else {
        img.remove();
        entry.img = null;
        if (!cover.querySelector('.noimg')) {
          const fallback = document.createElement('div');
          fallback.className = 'noimg';
          fallback.textContent = '🐾';
          cover.append(fallback);
        }
      }
      release();
    };
    img.onload = () => finish(true);
    img.onerror = () => finish(false);
    img.src = withToken(entry.url);
  }

  destroy() {
    this.io?.disconnect();
    this.byCard.clear();
  }
}

/** 在容器里激活封面懒加载（可在追加内容后重复调用）。 */
export function activateCovers(container) {
  if (!container) return;
  let manager = null;
  for (const item of managers) {
    if (item.container === container) {
      manager = item;
      break;
    }
  }
  if (!manager) {
    manager = new Manager(container);
    managers.add(manager);
  }
  manager.scan();
  // 容器换内容后把已经不用的 manager 收掉
  for (const item of Array.from(managers)) {
    if (!item.container.isConnected) {
      item.destroy();
      managers.delete(item);
    }
  }
}

export function setCoverConcurrency(value) {
  concurrency = Math.max(1, Math.min(16, Number(value) || 6));
}

/** 滚动到顶（切路由时用） */
export function scrollViewTop() {
  window.scrollTo({ top: 0, behavior: 'instant' in window ? 'instant' : 'auto' });
}
