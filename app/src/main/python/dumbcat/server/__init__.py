"""呆猫漫画 Web 服务端（FastAPI）。

模块划分：
- ``config``      设置与路径
- ``events``      SSE 事件总线
- ``normalize``   各图源数据 → 前端统一形状
- ``store``       作品/章节/页面内存仓库
- ``registry``    内置漫源 + 用户插件注册表
- ``imagecache``  图片磁盘缓存（带 Referer 代理）
- ``tasks``       下载任务队列
- ``app``         HTTP 接口
"""
