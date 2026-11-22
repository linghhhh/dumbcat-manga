"""Chromium 会话层。

- `runtime`：定位本机可用的 Chromium/Chrome/Edge 可执行文件。
- `hub`：全局共享的浏览器会话（单例）。整个程序只启动一个 Chromium，
  每个漫源在里面用**独立的 page**（各自独立的 Cookie/LocalStorage），
  互相隔离；界面上最多只有一个可见窗口，切换漫源不会堆出一堆窗口。
- `site`：站点通用页面控制器（搜索 / 浏览 / 分类 / 分页 / 章节 / 图片）。
- `comix`：Comix 站点专用控制器（scramble/canvas 阅读页需要点击翻页）。
"""
