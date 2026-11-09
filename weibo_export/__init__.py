"""weibo-export — 新浪微博博文导出工具（核心引擎 + 可复用本地界面）

品牌归属：极客港  jikegang.com
英文名：jikegang-weibo-export

本包同时承担两个角色：
1. 可复用的后端引擎（抓取 / 存储 / 导出），未来网页版复用同一套代码。
2. 通过 ``python -m weibo_export.server`` 启动本地 Web 服务，
   serve ``static/index.html`` 界面；桌面版由 Electron 壳打开同一界面。
"""

__version__ = "1.1.8"
__brand__ = "极客港"
__brand_en__ = "jikegang"
__brand_site__ = "jikegang.com"
__app_name_en__ = "jikegang-weibo-export"
