// 呆猫漫画 · JS 图源示例：jjmhw（HTML 站）
//
// 用法：把本文件放进「用户插件」目录，或在「图源管理 → 导入图源」里选它。
// 它和内置的 jjmhw 抓的是同一个站，只有列表/详情/章节/图片四段选择器，
// 用来演示 .js 图源怎么写；站点改版时照着改选择器就行。
//
// 运行环境：内嵌 QuickJS（ES2020），没有 window / document / fetch / DOMParser。
// 网络用 Network.*，HTML 用 Html.parse() 返回的轻量 DOM，加密用 Crypto.*。
// 所有函数必须是同步函数，返回值必须是普通对象。

registerSource({
  key: 'jjmhw_js',
  name: '禁漫客栈（JS 示例）',
  baseUrl: 'https://www.jjmhw.cc',
  version: '0.1.0',
  icon: '📚',
  nsfw: false,
  pageSize: 20,

  // 列表页：/booklist?page=N，分类参数由 payload.category 透传（形如 "area=1&status=2"）
  browse: function (payload) {
    var base = payload.baseUrl;
    var query = payload.category ? '&' + payload.category : '';
    var url = base + '/booklist?page=' + payload.page + query;
    var doc = Html.parse(Network.get(url, { Referer: base + '/' }));
    var items = doc.findAll('.mh-item').map(function (node) {
      var anchor = node.find('h2.title a') || node.find("a[href^='/book/']");
      if (!anchor) {
        return null;
      }
      var href = anchor.attr('href');
      var cover = node.find('.mh-cover');
      var style = String((cover && cover.attr('style')) || '');
      var coverMatch = style.match(/url\(\s*['"]?([^'")]+)['"]?\s*\)/);
      return {
        id: Text.match(href || '', '/book/(\\d+)'),
        title: anchor.text() || (node.getText('h2.title') || ''),
        url: Tools.absUrl(base, href),
        poster: coverMatch ? Tools.absUrl(base, coverMatch[1]) : '',
      };
    }).filter(function (item) {
      return item && item.url && item.title;
    });
    return { items: items, hasMore: items.length >= payload.pageSize };
  },

  // 搜索页：/search?keyword=…；站点搜不到时也会渲染一批推荐，所以先看它的“共找 N 个”计数
  search: function (payload) {
    var base = payload.baseUrl;
    var url = base + '/search?keyword=' + encodeURIComponent(payload.query)
      + (payload.page > 1 ? '&page=' + payload.page : '');
    var doc = Html.parse(Network.get(url, { Referer: base + '/' }));
    var totalText = doc.getText('.search-total') || doc.text();
    var total = Number(Text.match(totalText, '共找\\s*(\\d+)\\s*个') || 0);
    if (total === 0) {
      return { items: [], hasMore: false };
    }
    var items = doc.findAll('.mh-item').map(function (node) {
      var anchor = node.find('h2.title a') || node.find("a[href^='/book/']");
      if (!anchor) {
        return null;
      }
      return {
        id: Text.match(anchor.attr('href') || '', '/book/(\\d+)'),
        title: anchor.text(),
        url: Tools.absUrl(base, anchor.attr('href')),
      };
    }).filter(function (item) {
      return item && item.url;
    });
    return { items: items, hasMore: items.length >= payload.pageSize, total: total };
  },

  // 详情页：作者/状态/简介都在 .detail 里，封面在 .book-cover img
  detail: function (payload) {
    var base = payload.baseUrl;
    var doc = Html.parse(Network.get(payload.url, { Referer: base + '/' }));
    var info = doc.find('.detail') || doc.find('.book-detail');
    return {
      author: info ? info.getText('.author') : '',
      status: info ? info.getText('.status') : '',
      summary: info ? info.getText('.intro') : '',
      poster: Tools.absUrl(base, doc.get('img@data-src') || doc.get('img@src')),
      tags: doc.findAll('.tag a').map(function (node) {
        return node.text();
      }),
    };
  },

  // 章节列表：站点默认新→旧，这里按标题里的数字重新排成旧→新
  chapters: function (payload) {
    var base = payload.baseUrl;
    var doc = Html.parse(Network.get(payload.url, { Referer: base + '/' }));
    var rows = doc.findAll("#detail-list-select a[href*='/chapter/']").map(function (node) {
      var href = node.attr('href') || '';
      var order = Number(Text.match(node.text(), '(\\d+(?:\\.\\d+)?)') || 0);
      return {
        title: node.text(),
        url: Tools.absUrl(base, href),
        number: order,
      };
    }).filter(function (row) {
      return row.url;
    });
    rows.sort(function (left, right) {
      return left.number - right.number;
    });
    rows.forEach(function (row, index) {
      row.number = index + 1;
    });
    return rows;
  },

  // 图片列表：懒加载地址在 data-original，缺失时退回 data-src / src
  pages: function (payload) {
    var base = payload.baseUrl;
    var url = payload.chapterUrl || payload.url;
    var doc = Html.parse(Network.get(url, { Referer: base + '/' }));
    var nodes = doc.findAll('.comiclist img');
    if (!nodes.length) {
      nodes = doc.findAll('img[data-original]');
    }
    return {
      urls: nodes.map(function (node) {
        return Tools.absUrl(base, node.attr('data-original') || node.attr('data-src') || node.attr('src'));
      }).filter(function (item) {
        return !!item;
      }),
      referer: url,
    };
  },

  // 图片要带 Referer，默认下载器带不上，所以自己取一次
  fetchImage: function (payload) {
    return Network.getBase64(payload.url, { Referer: payload.referer || payload.baseUrl });
  },
});
