# 变更记录

## 0.2.0 — 2026-11-23

- 新增「我的 → 图源管理」：图源分「已启用 / 已关闭」两组列出，开关直接写盘、点完立刻生效；
  关掉的图源仍留在列表里，随时能再打开。
- 支持在 App 里导入单个 `.json` 规则图源，导入的图源可以删除。
- 图源开关状态存在 `config/sources-enabled.json`，升级重新解压 appdata 不会冲掉用户的选择。
- 安卓原生启动页换成网页同款加载图（`loading.webp`），底色统一为 `#F7FAFF`，
  WebView 首屏不再跳色。
- 应用内显示的版本号与 `versionName` 对齐；`APPDATA_VERSION` 升到 13，已装 0.1.0 的设备
  会重新解压前端资源，否则看不到新的图源管理页。

## 0.1.0 — 2026-11-22

首个公开版本。功能上对应内部迭代的 3.2.1，对外重新编号（`versionCode` 保持递增，见 README）。

- Java WebView 壳 + Chaquopy 内嵌 Python 3.10 后端 + FastAPI 本地 HTTP 服务（仅监听 127.0.0.1，一次性 token）
- 搜索、浏览、详情、阅读器、下载队列、书架、图源插件页
- 内置图源清单改为默认停用（`enabled: false`），只作为适配器与字段示例保留
- 签名配置移出仓库：口令改由 `local.properties` 或环境变量提供，仓库不再包含 keystore
- 补上 Gradle Wrapper；`tools/build.py` 去掉硬编码的本机 Gradle / JDK / Python 路径
- 内置前端资源版本号升到 12，老版本升级后会重新解压（否则仍沿用旧的图源启用状态）
