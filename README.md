# 呆猫漫画（Android）

一个本地优先的漫画阅读器：所有请求都在手机本地完成，没有账号体系、没有云端同步，
后端跑在应用进程里，界面是一套网页。

> **首个公开版本 0.1.0**。这个仓库只包含阅读器本身，**不附带任何漫画内容**，也不提供
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
| 阅读器 | 竖向长条 / 左右翻页、双指缩放、预加载、断点续读 |
| 下载 | 章节批量下载、并发控制、进度事件推送、失败重试 |
| 书架 | 本地书架、阅读进度、旧版下载记录迁移 |
| 图源插件 | 热重载、启用/停用、登录态管理、18+ 标记 |

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

## 目录结构

```
app/src/main/java/com/dumbcat/manga/MainActivity.java   WebView 壳、启动 Python 服务
app/src/main/python/android_main.py                     解压前端资源、设置目录、拉起 uvicorn
app/src/main/python/dumbcat/                            后端：server / sources / plugins / browser
app/src/main/assets/appdata/web/                        网页前端（HTML/CSS/JS）
app/src/main/assets/appdata/json/                       图源清单（运行时读取）
app/src/main/python/dumbcat/sources/json/               图源清单（源码侧副本）
tools/build.py                                          一键打包
tools/sync_app.py                                       从桌面版同步代码（可选）
```

## 图源

内置清单全部 `enabled: false`。要用哪个，把对应 JSON 的 `enabled` 改成 `true`，
在应用「图源插件」页点「↻ 热重载」即可。字段含义和自定义规则型图源的写法见
[图源说明](app/src/main/python/dumbcat/sources/json/README.md)。

请自行确认你所在地区的法律以及目标站点的服务条款；使用第三方图源的风险由使用者承担。

## 版本说明

| 对外版本 | versionCode | 说明 |
| --- | --- | --- |
| 0.1.0 | 6 | 首个公开版本。功能上对应内部迭代的 3.2.1，对外重新编号为 0.1.0 |

`versionCode` 没有跟着 `versionName` 回到 1，是为了让已经装了内部 3.2.x
（versionCode 5）的设备仍然能覆盖升级。

## 发布

首个版本用 tag `v0.1.0`：

```bash
git tag -a v0.1.0 -m "0.1.0 — 首个公开版本"
git push -u origin main --tags
```

然后在 GitHub 仓库页 **Releases → Draft a new release**：选 `v0.1.0` 作为 tag，
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
