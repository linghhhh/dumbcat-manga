"""呆猫漫画源码包。

文件分层（详见 README「项目结构」）：

    dumbcat/
      app_config.py      站点地址、请求头、配置目录等共享常量（导入即设定进程临时目录）
      errors.py          错误文案与图片地址规整
      naming.py          文件名与页码规则
      html_parser.py     无第三方依赖的 HTML 解析器
      runtime_cleanup.py 运行时目录、缓存清理
      browser/           Chromium 共享会话（单例）与站点/Comix 页面控制器
      sources/           各漫源 provider 与漫源注册表
      loader.py          API 客户端（带退避重试）与图片下载管线
      ui/                PySide6 界面（主窗口 / 阅读器 / 后台线程调度）

仓库根目录仍保留 `manga_core.py` 与 `dumbcat_qt.py` 两个薄门面，分别转发到
`dumbcat.sources`/`dumbcat.loader` 与 `dumbcat.ui`：启动器 `启动呆猫漫画.bat`
和既有测试继续用原来的模块名即可运行。
"""
from __future__ import annotations

__all__ = ["APP_VERSION"]

APP_VERSION = "2.1.0"
