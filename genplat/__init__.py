"""物业备用发电机试运行与燃油补给平台。

仅使用 Python 标准库：sqlite3 持久化运行事件，http.server 提供 API 与页面，
threading 运行模拟适配器与到期/低油位提醒。
"""

__version__ = "1.0.0"
