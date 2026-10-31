"""資料模型：把各家韌體五花八門的欄位名統一成正規物件。

DDPAI 不同韌體對同一份資料的鍵名不一致（name / filename / file，
starttime / start / begintime……），所以所有解析都走「大小寫不敏感 +
候選鍵名」的方式，缺欄位就給 None，永不拋錯。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional


def _pick(raw: Dict[str, Any], *names: str) -> Any:
    """大小寫不敏感地取出第一個存在的鍵值。"""
    if not isinstance(raw, dict):
        return None
    lowered = {str(k).lower(): v for k, v in raw.items()}
    for name in names:
        if name.lower() in lowered:
            value = lowered[name.lower()]
            if value is not None and value != "":
                return value
    return None


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return None


def _as_ts(value: Any) -> Optional[datetime]:
    """時間戳可能是秒、毫秒，或已格式化的字串。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if len(text) >= 8 and not text.isdigit():
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.strptime(text, fmt)
                except ValueError:
                    continue
        value = _as_int(text)
    stamp = _as_int(value)
    if stamp is None:
        return None
    if stamp > 10_000_000_000:  # 毫秒
        stamp //= 1000
    if stamp < 1_000_000_000:  # 明顯不是 Unix 秒
        return None
    try:
        return datetime.fromtimestamp(stamp)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass
class CameraFile:
    """一段錄影（播放清單或事件清單的一列）。"""

    name: str
    size: Optional[int] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    index: Optional[int] = None
    kind: str = "playback"  # playback | event | parking
    camera: str = "front"  # front | rear
    thumbnail: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_raw(cls, raw: Dict[str, Any], kind: str = "playback", camera: str = "front") -> "CameraFile":
        name = _pick(raw, "name", "filename", "file", "path", "bvideoname", "videoname")
        start_raw = _pick(raw, "starttime", "start", "begintime", "bstarttime", "time")
        end_raw = _pick(raw, "endtime", "end", "bendtime", "endtime")
        return cls(
            name=str(name or "").strip(),
            size=_as_int(_pick(raw, "size", "filesize", "bvideosize", "len", "length")),
            start=_as_ts(start_raw),
            end=_as_ts(end_raw),
            index=_as_int(_pick(raw, "index", "idx", "id")),
            kind=kind,
            camera=camera,
            thumbnail=_pick(raw, "imgname", "thumb", "thumbnail", "img"),
            raw=raw if isinstance(raw, dict) else {},
        )

    @property
    def duration(self) -> Optional[float]:
        if self.start and self.end:
            return (self.end - self.start).total_seconds()
        return None

    @property
    def basename(self) -> str:
        return self.name.rstrip("/").rsplit("/", 1)[-1]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "basename": self.basename,
            "size": self.size,
            "start": self.start.isoformat(sep=" ", timespec="seconds") if self.start else None,
            "end": self.end.isoformat(sep=" ", timespec="seconds") if self.end else None,
            "duration": self.duration,
            "kind": self.kind,
            "camera": self.camera,
            "thumbnail": self.thumbnail,
        }


# 事件清單與播放清單結構相近，用別名保持可讀性。
CameraEvent = CameraFile


@dataclass
class DeviceInfo:
    """設備基本資訊（API_GetBaseInfo）。"""

    model: Optional[str] = None
    nickname: Optional[str] = None
    version: Optional[str] = None
    sn: Optional[str] = None
    mac: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "DeviceInfo":
        return cls(
            model=_pick(raw, "model", "devicetype", "dev_type", "product"),
            nickname=_pick(raw, "nickname", "devicename", "name"),
            version=_pick(raw, "version", "firmware", "swversion", "fw"),
            sn=_pick(raw, "sn", "serial", "chipsn"),
            mac=_pick(raw, "macaddr", "mac", "wifi_mac"),
            raw=dict(raw or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "nickname": self.nickname,
            "version": self.version,
            "sn": self.sn,
            "mac": self.mac,
            "raw": self.raw,
        }


@dataclass
class StorageInfo:
    """SD 卡容量。DDPAI 以 KB 回報，這裡統一換算成 bytes。"""

    total_bytes: Optional[int] = None
    free_bytes: Optional[int] = None
    brand: Optional[str] = None
    source: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def used_bytes(self) -> Optional[int]:
        if self.total_bytes is None or self.free_bytes is None:
            return None
        return max(self.total_bytes - self.free_bytes, 0)

    @property
    def used_percent(self) -> Optional[float]:
        if not self.total_bytes or self.used_bytes is None:
            return None
        return round(self.used_bytes / self.total_bytes * 100, 1)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "used_bytes": self.used_bytes,
            "used_percent": self.used_percent,
            "brand": self.brand,
            "source": self.source,
        }


_KEYS_TOTAL = ("sdcapacity", "sd_capacity", "capacity", "total", "sdsize", "ext_sdcapacity")
_KEYS_FREE = ("sdspare", "sd_spare", "spare", "free", "surplus", "sdfree", "ext_sdspare")


def parse_storage(raw: Dict[str, Any], source: str = "") -> Optional[StorageInfo]:
    """從任意回應中盡力挖出容量資訊。回傳 None 代表這份回應沒有容量欄位。"""
    if not isinstance(raw, dict):
        return None
    total = _as_int(_pick(raw, *_KEYS_TOTAL))
    free = _as_int(_pick(raw, *_KEYS_FREE))
    if total is None and free is None:
        return None

    def to_bytes(value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        # 100GB 的 bytes 值約 1.07e11，而 100GB 的 KB 值只有 1.05e8。
        # 超過這個量級才當作 bytes，其餘視為 KB（DDPAI 的慣例）。
        if value >= 100 * 1024 ** 3:
            return value
        return value * 1024

    return StorageInfo(
        total_bytes=to_bytes(total),
        free_bytes=to_bytes(free),
        brand=_pick(raw, "sdbrand", "brand", "sdcard"),
        source=source,
        raw=dict(raw),
    )


def human_size(num: Optional[int]) -> str:
    if num is None:
        return "N/A"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def human_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return "N/A"
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def flatten_files(items: Iterable[Any]) -> List[CameraFile]:
    return [item for item in items if isinstance(item, CameraFile)]
