# 呆猫漫画（Android）

一个本地优先的漫画阅读器：所有请求都在手机本地完成，没有账号体系、没有云端同步，
后端跑在应用进程里，界面是一套网页。

> **当前版本 0.2.0**。这个仓库只包含阅读器本身，**不附带任何漫画内容**，也不提供
> 内容来源。内置的图源清单全部默认停用，仅作为适配器写法示例保留，详见
> [图源说明](app/src/main/python/dumbcat/sources/json/README.md)。

## 这是什么

- **Java 壳 + 内嵌 Python**：`MainActivity` 启动一个 WebView，同时用
  [Chaquopy](https://chaquo.com/chaquopy/) 在应用进程内跑一个 Python 3.10 后端。
- **本地 HTTP 服务**：后端是 FastAPI，只监听 `127.0.0.1`，端口随机，启动时生成一次性
  token；页面拿 token 调接口，别的程序即使扫到端口也进不来。
- **网页前端**：搜索、浏览、详情、阅读器、下载队列、书架、图源插件页，都在
  `assets/appdata/web/` 里，和后端通过 JSON API 通信。
- **图源可插拔**：每个图源是一个 JSON 清单文件，`engine` 决定用现成的 Python 适配器
  还是纯 CSS/JSON 选择器规则。加图源不用改代码。

## 功能

| 模块 | 说明 |
| --- | --- |
| 搜索 / 浏览 | 按图源搜索、看分类与榜单，支持分页懒加载 |
| 详情 | 封面、简介、章节列表、收藏到书架 |
| 阅读器 | 竖向长条（只此一种）、双指缩放与拖动、双击放大、预加载、断点续读；图片 100% 宽度、无缝拼接 |
| 下载 | 章节批量下载、并发控制、进度事件推送、失败重试；保存文件夹由用户自选（安卓走「所有文件访问权限」+ App 内目录浏览）；手机版可一键「导出到手机」到系统下载目录 |
| 书架 | 书架 / 历史 / 下载缓存 三个标签；本地书架、作品与章节快照（图源连不上也能打开）、阅读进度、旧版下载记录迁移；图源收藏由用户自己挑着加入 |
| 图源管理 | 在 App 里直接开/关图源、导入 `.json` 规则图源与 `.js` 脚本图源、删除导入的图源、管理登录账号 |
| 图源插件 | 热重载、启用/停用、登录态管理、18+ 标记；`.js` 图源跑在内嵌 QuickJS 里，出错会在页面上列出来 |

## 构建

需要：

- JDK 17+（`JAVA_HOME` 指向 JDK）
- Android SDK 35（在 `local.properties` 里写 `sdk.dir=...`）
- Python 3.10（Chaquopy 用它编译内嵌解释器；默认从 `PATH` 找，也可以在
  `local.properties` 里写 `chaquopy.python=...`）

```bash
# 命令行
./gradlew assembleRelease        # Windows: gradlew.bat assembleRelease

# 或用脚本：构建 + 把 APK 复制到 dist/
python tools/build.py
```

产物在 `app/build/outputs/apk/release/`，脚本模式下会复制一份到 `dist/`。

**关于签名**：仓库里**没有** keystore，签名口令也**不**写在代码里。要构建可分发
的版本，在本机 `local.properties` 里加：

```properties
dumbcat.keystore=dumbcat-release.jks          # 相对 app/ 目录，或写绝对路径
dumbcat.storePassword=...
dumbcat.keyAlias=...
dumbcat.keyPassword=...
```

也支持环境变量 `DUMBCAT_KEYSTORE` / `DUMBCAT_STORE_PASSWORD` / `DUMBCAT_KEY_ALIAS` /
`DUMBCAT_KEY_PASSWORD`（CI 用）。没有配置时构建照常进行，只是 APK 未签名。

> 自己生成密钥：`keytool -genkeypair -keystore dumbcat-release.jks -alias dumbcat -keyalg RSA -keysize 2048 -validity 10000`
> 生成后**不要提交**，并自己备份——丢了就再也发不了同签名的更新。

支持 ABI：`arm64-v8a`、`x86_64`（模拟器）。最低 Android 7.0（API 24），目标 API 35。

## 开发与测试

后端是纯 Python，桌面端就能跑起来调试（不需要 Android 设备）：

```bash
# 冒烟起一个本地服务（桌面版没有安卓导出/浏览器源）
set DUMBCAT_APPDATA=.scratch/appdata        # Windows；bash 用 export
python -c "from dumbcat.server import app"  # 需要 PYTHONPATH=app/src/main/python
```

测试与静态检查：

```bash
pip install -r tests/requirements-dev.txt
python -m pytest tests -q          # 26 个用例：SQLite 落盘、书架快照、离线接口、下载重试、JS 图源
python tools/check_js.py           # 前端 19 个 ES module + prelude.js 逐个 node --check
python -m py_compile $(find app/src/main/python -name "*.py")
```

Windows 上如果 `pip install` 被系统权限挡住，可以装进仓库：

```bash
pip install --target .scratch/pylibs pytest httpx
PYTHONPATH=.scratch/pylibs python -m pytest tests -q
```

CI 在 `.github/workflows/ci.yml`：push/PR 跑 Python 测试与前端语法检查，
安卓打包（Chaquopy + SDK 35）只在手动 `workflow_dispatch` 时跑。

写图源看 [插件开发指南](插件开发指南.md)；`.js` 脚本图源的 API 与示例看
[JS 图源开发指南](docs/JS图源开发指南.md) 和 [jjmhw.js](docs/示例图源/jjmhw.js)。

## 目录结构

```
app/src/main/java/com/dumbcat/manga/MainActivity.java   WebView 壳、启动 Python 服务
app/src/main/python/android_main.py                     解压前端资源、设置目录、拉起 uvicorn
app/src/main/python/dumbcat/                            后端：server / sources / plugins / browser
app/src/main/assets/appdata/web/                        网页前端（HTML/CSS/JS）
app/src/main/assets/appdata/json/                       图源清单（运行时读取）
app/src/main/python/dumbcat/sources/json/               图源清单（源码侧副本）
tools/build.py                                          一键打包
tools/check_js.py                                       前端语法检查
tests/                                                  pytest 用例
.github/workflows/ci.yml                                CI
tools/sync_app.py                                       从桌面版同步代码（可选）
```

## 图源

内置清单全部 `enabled: false`。要用哪个，把对应 JSON 的 `enabled` 改成 `true`，
在应用「图源插件」页点「↻ 热重载」即可。字段含义和自定义规则型图源的写法见
[图源说明](app/src/main/python/dumbcat/sources/json/README.md)。

规则写不出来的站点（要登录、要签名、图片要特殊处理）可以写 `.js` 脚本图源：
一段 `registerSource({...})`，跑在安卓版内嵌的 QuickJS 里，能自己发请求、解析 HTML、
算签名、给图片加请求头。写法见 [JS 图源开发指南](docs/JS图源开发指南.md)，
完整示例见 [jjmhw.js](docs/示例图源/jjmhw.js)。

请自行确认你所在地区的法律以及目标站点的服务条款；使用第三方图源的风险由使用者承担。

## 版本说明

| 对外版本 | versionCode | 说明 |
| --- | --- | --- |
| 0.2.1 | 11 | 阅读器删掉单页模式、条漫无缝铺满并恢复缩放手势；下载并入书架「下载缓存」标签，保存文件夹由用户自选，修掉手机端点下载没反应、历史为空、权限缺失等根因 |
| 0.2.0 | 10 | 阅读器支持双指缩放与拖动、断点续读更准；下载可导出到系统下载目录、任务队列落盘可续传；书架/历史存作品与章节快照，图源连不上也能打开 |
| 0.2.0 初版 | 8 | 图源管理页（开关 / 导入 / 登录）、下载设置页（子目录、每章图片上限）、书架改为自选收藏、WEBTOON 发现页可连续翻页、原生启动页换成网页同款加载图 |
| 0.1.0 | 6 | 首个公开版本。功能上对应内部迭代的 3.2.1，对外重新编号为 0.1.0 |

`versionCode` 没有跟着 `versionName` 回到 1，是为了让已经装了内部 3.2.x
（versionCode 5）的设备仍然能覆盖升级。

## 发布

每个版本打一个 tag：

```bash
git tag -a v0.2.0 -m "0.2.0 — 图源管理"
git push -u origin main --tags
```

然后在 GitHub 仓库页 **Releases → Draft a new release**：选对应的 tag，
把 [CHANGELOG](CHANGELOG.md) 里对应条目粘进说明，附件上传
`app/build/outputs/apk/release/app-release.apk`。以后每个版本重复这一步。

仓库不含 keystore，签名在各自机器上完成，所以每个 Release 的 APK 都由发布者的
密钥签名——请固定用同一个 keystore，否则老用户无法覆盖升级。

## 许可

[MIT](LICENSE)。内置图源适配器仅用于演示接口写法。

## 免责声明

本项目是阅读器框架，不托管、不分发任何漫画内容，与任何漫画站点无隶属关系。
仓库里的图源适配器仅用于演示如何对接公开接口。使用者应自行承担因使用第三方图源
产生的全部责任。
问题反馈请开 Issue。
