# 变更记录

## 0.1.0 — 2026-11-22

首个公开版本。功能上对应内部迭代的 3.2.1，对外重新编号（`versionCode` 保持递增，见 README）。

- Java WebView 壳 + Chaquopy 内嵌 Python 3.10 后端 + FastAPI 本地 HTTP 服务（仅监听 127.0.0.1，一次性 token）
- 搜索、浏览、详情、阅读器、下载队列、书架、图源插件页
- 内置图源清单改为默认停用（`enabled: false`），只作为适配器与字段示例保留
- 签名配置移出仓库：口令改由 `local.properties` 或环境变量提供，仓库不再包含 keystore
- 补上 Gradle Wrapper；`tools/build.py` 去掉硬编码的本机 Gradle / JDK / Python 路径
- 内置前端资源版本号升到 12，老版本升级后会重新解压（否则仍沿用旧的图源启用状态）
