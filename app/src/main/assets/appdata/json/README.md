# 图源清单（一个文件 = 一个图源）

这个目录里每个 `.json` 就是一个图源。**加图源、停用图源、改名字/图标/18+，
都只需要改这里的文件**，改完在「图源插件」页点一下「↻ 热重载」即可，不用重启。

> **开源首版说明**：仓库里内置的图源全部是 `enabled: false`。保留这些文件是为了
> 展示适配器写法和字段含义，不代表本项目默认提供对相应站点的访问。要用哪个，把它的
> `enabled` 改成 `true` 后热重载即可；使用前请自行确认所在地区的法律与该站点的使用条款。

## 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `key` | 是 | 图源 id，小写字母/数字/下划线，例如 `qimanga`。改了等于换了图源（书架里旧记录会找不到对应源）。 |
| `name` | 是 | 显示名，例如 `QiManga`。 |
| `engine` | 否 | `python`（默认）用现成适配器；`rules` 用纯 CSS 选择器规则（见下）。 |
| `module` | python 引擎 | 实现类所在模块，例如 `dumbcat.sources.providers`。 |
| `class` | python 引擎 | 实现类名，例如 `QiMangaProvider`。 |
| `args` | 否 | 构造参数列表，`BrowserSiteProvider` 这类需要传 key/名字/站点地址。 |
| `kwargs` | 否 | 构造参数字典。 |
| `icon` | 否 | 侧栏/卡片上的小图标（一个 emoji）。 |
| `nsfw` | 否 | 是否 18+，会在界面上打 `18+` 标记。 |
| `enabled` | 否 | 写 `false` 就不加载这个图源（文件留着，随时开回来）。 |
| `breeze` | 否 | 写 `true` 表示「需要梯子/需要浏览器」，只有设置里勾了「包含需要浏览器渲染的图源」才加载。 |
| `note` | 否 | 一句话说明，显示在图源说明和插件页里。 |

## 两个例子

接口型/浏览器型站点（继续用稳定的 Python 适配器）：

```json
{
  "key": "qimanga",
  "name": "QiManga",
  "engine": "python",
  "module": "dumbcat.sources.providers",
  "class": "QiMangaProvider",
  "icon": "🍥",
  "nsfw": false
}
```

纯规则站点（不用写 Python，自己加站点就写这种）：

```json
{
  "key": "mysite",
  "name": "我的站点",
  "engine": "rules",
  "base_url": "https://example.com",
  "browse": {
    "url": "/list/{category}/{page}.html",
    "list": ".book-list .item",
    "title": ".title",
    "link": "a@href",
    "cover": "img@data-src"
  },
  "chapters": { "list": ".chapter-list li", "title": "a", "link": "a@href" },
  "pages": { "list": ".reader img", "attr": "data-original" }
}
```

规则型也可以直接放进 `用户插件/` 目录，效果一样（那边加载的图源标记为「插件」）。

字段细节、选择器语法、`{page}` / `{category}` / `{query}` 占位符，见
项目根目录的 `插件开发指南.md`。
