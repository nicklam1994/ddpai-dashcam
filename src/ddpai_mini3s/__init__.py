"""DDPAI 行車紀錄器本機 Wi-Fi 工具組。

整合自兩個逆向工程專案：
  - awexandrr/ddpai-api   （POST + sessionid 握手、檔案下載）
  - pwrtux/visiondash     （GET + `doi-lel` header、CLI 與 Web 儀表板）

目標機型：DDPAI Mini3S（單前鏡、較舊韌體），但所有 API 互動都走
「多策略自動探測」，因此 N5 / Z40 / Mini 系列應可通用。
"""

from .client import Camera, CameraError, CameraUnreachable
from .discovery import candidate_hosts, discover, probe_host
from .models import CameraEvent, CameraFile, DeviceInfo, StorageInfo

__version__ = "0.1.0"

__all__ = [
    "Camera",
    "CameraError",
    "CameraUnreachable",
    "CameraFile",
    "CameraEvent",
    "DeviceInfo",
    "StorageInfo",
    "candidate_hosts",
    "discover",
    "probe_host",
    "__version__",
]
