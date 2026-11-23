# JS 脚本图源开发指南

从 0.2.0（versionCode 9）开始，呆猫漫画的安卓版内置了 QuickJS，可以直接把一段 **JavaScript** 当成图源用。
规则型 JSON 图源（`engine: rules`，只发 GET、只解析 HTML）解决不了登录、签名、加密、自定义请求头、图片切条还原这些事，
这些以前只能写 Python 插件、并跟着 APK 一起发版；现在可以写一个 `.js`，放进用户插件目录就能用。

> 桌面版（Windows 调试模式）没有 QuickJS，`.js` 图源会显示成「加载失败：当前版本不支持 JS 图源（需要安卓版应用）」。
> 这是预期行为，不是脚本写错了。

## 一、安装与调试

1. 写一个 `xxx.js`，内容以 `registerSource({...})` 开头。
2. 三种安装方式：
   - App 内「图源管理 → 📥 导入图源」选 `.js`；
   - 手动放进用户插件目录（「图源管理 → 📂 用户插件目录」），然后点「热重载」；
   - 打成 zip（里面要有 `.js` 或 `.py`）走 `/api/plugins/upload`。
3. 加载失败时，图源管理页底部会出现「加载失败（N）」区块，直接显示脚本报的错。
4. `console.log()` / `console.warn()` / `console.error()` 会写进 Android 日志，tag 是 `DumbCatJs`。

## 二、最小骨架

```js
registerSource({
  key: 'my_source',        // 必填，小写字母开头，2-32 位，只含小写字母 / 数字 / 下划线
  name: '我的图源',         // 必填，显示名
  baseUrl: 'https://example.com',
  version: '0.1.0',
  icon: '🐾',
  nsfw: false,
  pageSize: 20,            // 影响「还有下一页吗」的推断

  browse: function (payload) { ... },
  search: function (payload) { ... },
  detail: function (payload) { ... },
  chapters: function (payload) { ... },
  pages: function (payload) { ... },
  fetchImage: function (payload) { ... },   // 可选
});
```

只有 `browse` / `search` 至少实现一个才是一个能用的图源；其余缺失时按「没有这个能力」处理。

## 三、函数契约

所有函数都**必须是同步函数**（不能 `async`、不能返回 Promise），入参和返回值都是普通 JS 对象（内部用 JSON 传，函数、DOM 节点、BigInt 都带不过去）。
每次调用最多 60 秒，超时会中断并把错误显示给用户。

| 函数 | 入参 | 返回 |
| --- | --- | --- |
| `browse` | `{page, category, baseUrl, pageSize}` | `{items: [...], hasMore?: boolean, total?: number}` |
| `search` | `{query, page, baseUrl, pageSize}` | 同上 |
| `detail` | `{id, url, title, baseUrl}` | `{author, summary, status, tags, poster, ...}`，返回 `undefined` 表示没有额外信息 |
| `chapters` | `{id, url, title, baseUrl}` | `[{title, url, number?, id?}, ...]`（数组顺序就是阅读顺序） |
| `pages` | `{id, url, title, chapterUrl, chapterId, chapterTitle, number, baseUrl}` | `{urls: [...], referer?}` |
| `fetchImage` | `{url, referer, baseUrl}` | `{base64}`（自己取回的字节，做切条还原/解密都用它）或 `{url, headers}`（让宿主带这些头去下载） |
| `categories` | `{}` | `[{key, name}, ...]`，可选，用来给列表页做分类 |
| `referer` | 同 `pages` | 字符串，可选；不实现就用章节地址当 Referer |

列表条目至少要有 `title` 和 `url`；`url` 用相对地址也行，包装层会用 `baseUrl` 补全。可选字段：`poster` / `cover`、`author`、`status`、`latest`、`summary`、`tags`（字符串或数组）。

`pages` 里的 `urls` 可以是字符串数组，也可以是 `{url, index}` 数组；重复地址会自动去重，`index` 从 1 重新编号。

`fetchImage` 里返回 `{url, headers}` 时，图片由宿主用这些请求头下载；返回 `{base64}` 时直接用这份字节。切条还原（禁漫那种）可以在 `fetchImage` 里做图片重排后返回 base64。

## 四、可用的全局对象

内嵌的是 QuickJS，**没有** `window` / `document` / `fetch` / `XMLHttpRequest` / `localStorage`，也没有 npm 包。
能用的只有下面这些 + ES2020 标准库（`JSON`、`Math`、`RegExp`、`Array`、`String`、`TextEncoder` 等）。

### Network（同步 HTTP）

```js
Network.get(url, headers)              // -> 响应正文（字符串）
Network.getJson(url, headers)          // -> 解析好的 JSON
Network.post(url, body, headers)       // -> 响应正文
Network.postJson(url, body, headers)   // -> 解析好的 JSON
Network.getBase64(url, headers)        // -> 二进制内容的 base64
Network.request({method, url, headers, body, base64})  // -> {status, headers, body, base64}
```

- 默认带手机 Chrome UA，20 秒连接 / 30 秒读超时，跟随重定向；
- 状态码 ≥ 400 直接抛错（错误信息带状态码和 URL）；
- 需要 Cookie 就自己拼 `Cookie` 头；需要 Referer 就传 `{Referer: '...'}`。

### Html（轻量 DOM）

```js
var doc = Html.parse(html);        // 根节点
doc.findAll(selector)              // -> Node[]
doc.find(selector)                 // -> Node | null
doc.getText(selector)              // -> 文字
doc.get(selector[, attr])          // 取文字；`'a@href'` 或 ('a','href') 取属性
node.attr('href')                  // 属性值（不存在返回 ''）
node.text()                        // 自身及子节点的文字（已解 HTML 实体、压缩空白）
node.html()                        // 内部 HTML
node.find / findAll / getText / get
```

选择器支持：标签名、`#id`、`.class`、`[attr]`、`[attr=v]`、`[attr*=v]`、`[attr^=v]`、`[attr$=v]`、以及后代组合（`div.a span`）。
**不支持**：逗号分组、`>` / `+` / `~`、`:nth-child` 等伪类。需要复杂定位时，先用 `Text.between` / `Text.matchAll` 把 HTML 切段再解析。

### Text / Json / Tools / Crypto

```js
Text.between(text, start, end)   // 取两段之间的内容（end 省略取到结尾）
Text.after(text, start)
Text.match(text, pattern, flags) // 返回第一个匹配（优先第 1 个捕获组），没匹配返回 ''
Text.matchAll(text, pattern, flags)  // 返回捕获组数组
Text.trim(text)

Json.path(obj, 'data.list.0.title')

Tools.absUrl(base, url)          // 补全 //x、/x、相对路径
Tools.decodeEntities(text)

Crypto.md5(s) / Crypto.sha1(s) / Crypto.sha256(s)   // 十六进制小写
Crypto.hmacSha1(key, data) / Crypto.hmacSha256(key, data)
```

## 五、能力边界

| 想做的事 | 能不能 |
| --- | --- |
| GET / POST、自定义头、Cookie、Referer | ✅ |
| 解析 HTML、正则切文本 | ✅ |
| MD5 / SHA1 / SHA256 / HMAC 签名 | ✅ |
| 图片要 Referer / Cookie / 解密 / 切条还原 | ✅ 在 `fetchImage` 里做 |
| AES / RSA 等其它加密、Web Crypto | ❌ 目前没有，需要写 Python 插件 |
| 执行站点自己的 JS（混淆过的解密脚本） | ❌ 没有 DOM 和浏览器 API，只能手工翻译 |
| `async` / `await` / Promise | ❌ 必须同步 |
| 跨请求保存状态 | ❌ 每次调用都是新的一次调用；确实需要就每次重新请求 |
| 持久化缓存 | ❌ 未开放；别把状态寄托在全局变量上 |

## 六、和另外两种图源的关系

| 形式 | 适合 | 代价 |
| --- | --- | --- |
| `engine: rules` 的 JSON | 纯 SSR 站点，只发 GET，选择器就够 | 表达力最弱，不能登录/签名 |
| `.js` 脚本（本文） | 需要登录、签名、自定义头、图片特殊处理的站点 | 要自己写代码，没有 DOM |
| Python 插件 | 复杂协议、AES、浏览器渲染、要复用 `dumbcat.plugins.api` | 跟着 APK 发版，用户改不了 |

推荐顺序：能写 JSON 就写 JSON；写不了再写 `.js`；`.js` 也搞不定（比如要跑浏览器）才考虑 Python 插件并提 PR。

示例见 [`示例图源/jjmhw.js`](示例图源/jjmhw.js)：列表 / 搜索 / 详情 / 章节排序 / 图片五段齐全，可以照着改成别的站。
