/* 作品卡片 / 网格 / 分类筛选条：浏览、搜索、详情共用。 */
import { el, fmtCount, coverSrc } from './util.js';
import { navigate } from './util.js';

export function openSeries(uid) {
  if (!uid) return;
  navigate(`#/series/${uid}`);
}

export function seriesCard(item, options = {}) {
  const uid = item.uid || '';
  const title = item.title || '未命名作品';
  // 列表卡片只要 400px 宽的缩略图：一屏 20 张卡片能省掉一个数量级的流量
  const coverUrl = coverSrc(item.coverUrl || '', options.coverWidth || 400);
  const badges = [];
  if (item.nsfw) badges.push(el('span', { class: 'badge nsfw', text: '18+' }));
  if (item.latest) badges.push(el('span', { class: 'badge', text: String(item.latest) }));
  if (options.showSource && item.providerName) {
    badges.push(el('span', { class: 'badge src', text: item.providerName }));
  }

  const card = el('article', {
    class: 'card',
    tabindex: '0',
    role: 'link',
    dataset: { uid, cover: coverUrl },
    title,
    on: {
      click: (event) => {
        if (event.target.closest('button')) return;
        (options.onOpen || openSeries)(uid);
      },
      keydown: (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          (options.onOpen || openSeries)(uid);
        }
      },
    },
  },
    el('div', { class: 'cover skeleton' },
      el('div', { class: 'badges' }, ...badges),
      el('div', { class: 'hoverbar' },
        el('button', {
          type: 'button',
          on: {
            click: (event) => {
              event.stopPropagation();
              (options.onOpen || openSeries)(uid);
            },
          },
        }, '查看'),
      ),
    ),
    el('div', { class: 'card-title', text: title }),
    el('div', { class: 'card-meta' },
      options.showSource && item.providerName
        ? el('span', { class: 'src-name', text: item.providerName })
        : null,
      item.author ? el('span', { text: item.author }) : null,
      !item.author && item.tags && item.tags.length
        ? el('span', { text: item.tags.slice(0, 2).join(' · ') })
        : null,
      item.total ? el('span', { text: `${fmtCount(item.total)} 话` }) : null,
    ),
  );
  return card;
}

export function seriesGrid(items, options = {}) {
  const grid = el('div', { class: 'grid' });
  for (const item of items || []) grid.append(seriesCard(item, options));
  return grid;
}

/** 分类 / 标签筛选条；items: [{token,label,kind}] */
export function categoryBar(categories, activeToken, onPick) {
  if (!categories || !categories.length) return null;
  const bar = el('div', { class: 'source-tabs', style: 'margin-bottom:14px' });
  const all = el('button', {
    class: `source-tab ${activeToken ? '' : 'active'}`,
    type: 'button',
    on: { click: () => onPick('') },
  }, '全部');
  bar.append(all);
  for (const category of categories) {
    const token = String(category.token ?? category.value ?? category.key ?? category.label ?? '');
    const label = category.label || category.name || token;
    if (!token) continue;
    const button = el('button', {
      class: `source-tab ${activeToken === token ? 'active' : ''}`,
      type: 'button',
      title: label,
      dataset: { token },
      on: { click: () => onPick(token) },
    }, label);
    bar.append(button);
  }
  return bar;
}
