"""Web 子套件。`from ddpai_mini3s.web.app import app` 取得 FastAPI 實例。"""

__all__ = ["app"]


def __getattr__(name):  # 延遲載入，讓沒裝 fastapi 的環境也能 import 套件
    if name == "app":
        from .app import app as fastapi_app

        return fastapi_app
    raise AttributeError(name)
