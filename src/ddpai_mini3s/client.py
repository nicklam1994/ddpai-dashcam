"""高階相機物件：把 transport 的原始命令包成好用的方法。"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import discovery
from .models import CameraEvent, CameraFile, DeviceInfo, StorageInfo, human_size, parse_storage
from .parser import errcode, find_dict, find_list, maybe_json
from .transport import (
    PROBE_COMMANDS,
    STRATEGIES,
    Strategy,
    Transport,
    TransportError,
    strategy_from_id,
)

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, Optional[int]], None]

CMD_FILES = {False: "APP_PlaybackListReq", True: "APP_PlaybackListReq_RearCam"}
CMD_EVENTS = {False: "APP_EventListReq", True: "APP_EventListReq_RearCam"}
CMD_STORAGE = ("API_GetStorageInfo", "API_GetCamWifiCapacity", "API_GetBaseInfo")
CMD_BASE_INFO = ("API_GetBaseInfo", "G_DEV_TYPE")

DEFAULT_PASSWORD = os.environ.get("DDPAI_PASSWORD", "")
CHUNK = 64 * 1024


class CameraError(RuntimeError):
    """相機層級的錯誤（可讀訊息，已含建議動作）。"""


class CameraUnreachable(CameraError):
    """連不上相機。"""


class Camera:
    """一台 DDPAI 行車紀錄器。"""

    def __init__(
        self,
        host: Optional[str] = None,
        password: str = "",
        timeout: float = 8.0,
        transport: Optional[Transport] = None,
        auto_discover: bool = True,
    ) -> None:
        self.timeout = timeout
        self.password = password or DEFAULT_PASSWORD
        if transport is not None:
            self.transport = transport
        else:
            resolved = host or os.environ.get("DDPAI_HOST", "")
            if not resolved and auto_discover:
                resolved = discovery.discover(timeout=min(timeout, 3.0), password=self.password) or ""
            if not resolved:
                raise CameraUnreachable(
                    "找不到相機。請確認電腦已連上 DDPAI_XXXX 的 Wi-Fi，"
                    "或用 --host 193.168.0.1 指定 IP。"
                )
            self.transport = Transport(host=resolved, timeout=timeout, password=self.password)

    # ── 生命週期 ────────────────────────────────────────────────
    @property
    def host(self) -> str:
        return self.transport.host

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> "Camera":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ── 連線 ────────────────────────────────────────────────────
    def connect(self) -> Strategy:
        try:
            return self.transport.detect()
        except TransportError as exc:
            raise CameraUnreachable(str(exc)) from exc

    def is_connected(self) -> bool:
        return discovery.tcp_open(self.host, timeout=1.0)

    # ── 資訊 ────────────────────────────────────────────────────
    def raw(self, cmd: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.transport.request(cmd, params)

    def info(self) -> DeviceInfo:
        last: Optional[Exception] = None
        for cmd in CMD_BASE_INFO:
            try:
                payload = self.transport.request(cmd)
            except TransportError as exc:
                last = exc
                continue
            data = find_dict(payload)
            if data:
                return DeviceInfo.from_raw(data)
        raise CameraError(f"讀不到設備資訊：{last}")

    def storage(self) -> Optional[StorageInfo]:
        """多來源找容量：不同韌體把容量放在不同 cmd / 不同鍵。"""
        for cmd in CMD_STORAGE:
            try:
                payload = self.transport.request(cmd)
            except TransportError:
                continue
            candidates = [payload, find_dict(payload)]
            inner = find_dict(payload)
            if isinstance(inner, dict):
                candidates.append(inner)
            for candidate in candidates:
                if isinstance(candidate, dict):
                    parsed = parse_storage(candidate, source=cmd)
                    if parsed:
                        return parsed
            # 有些韌體把容量塞在字串化的 data 裡
            data = payload.get("data") if isinstance(payload, dict) else None
            decoded = maybe_json(data)
            if isinstance(decoded, dict):
                parsed = parse_storage(decoded, source=cmd)
                if parsed:
                    return parsed
        return None

    # ── 檔案清單 ────────────────────────────────────────────────
    def files(self, rear: bool = False) -> List[CameraFile]:
        payload = self.transport.request(CMD_FILES[bool(rear)])
        rows = find_list(payload, keys=("file", "files", "list", "data", "items"))
        camera = "rear" if rear else "front"
        files = [CameraFile.from_raw(row, camera=camera) for row in rows]
        return [f for f in files if f.name]

    def events(self, rear: bool = False) -> List[CameraFile]:
        payload = self.transport.request(CMD_EVENTS[bool(rear)])
        rows = find_list(payload, keys=("event", "events", "file", "list", "data"))
        camera = "rear" if rear else "front"
        events = [
            CameraFile.from_raw(row, kind="event", camera=camera)
            for row in rows
        ]
        return [e for e in events if e.name]

    def parking_events(self) -> List[CameraFile]:
        payload = self.transport.request("APP_ParkingEventListReq")
        rows = find_list(payload, keys=("event", "events", "file", "list", "data"))
        return [CameraFile.from_raw(row, kind="parking") for row in rows if row]

    # ── 下載 ────────────────────────────────────────────────────
    def download(
        self,
        name: str,
        dest_dir: str | Path = "downloads",
        overwrite: bool = False,
        progress: Optional[ProgressFn] = None,
        retries: int = 1,
    ) -> Path:
        """下載單一檔案，支援「已存在且大小相同就跳過」。"""
        name = name.strip()
        if not name:
            raise CameraError("檔名為空")

        dest = Path(dest_dir).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        local = dest / os.path.basename(name.lstrip("/"))

        remote_size = self.transport.head(name)
        if local.exists() and not overwrite and remote_size is not None:
            if local.stat().st_size == remote_size:
                if progress:
                    progress(remote_size, remote_size)
                log.debug("跳過（已下載）：%s", local.name)
                return local

        attempt = 0
        while True:
            attempt += 1
            try:
                self._stream_to_file(name, local, progress)
                return local
            except CameraError:
                if attempt > retries:
                    raise
                log.debug("下載失敗，重試 %d/%d：%s", attempt, retries, name)
                time.sleep(0.5)

    def _stream_to_file(self, name: str, local: Path, progress: Optional[ProgressFn]) -> None:
        try:
            resp = self.transport.open_stream(name)
        except Exception as exc:  # noqa: BLE001 - 網路層各種例外
            raise CameraError(f"連線失敗：{exc}") from exc

        if resp.status_code not in (200, 206):
            resp.close()
            raise CameraError(f"{name} 下載失敗：HTTP {resp.status_code}")

        total: Optional[int] = None
        if resp.headers.get("Content-Length"):
            try:
                total = int(resp.headers["Content-Length"])
            except ValueError:
                total = None

        downloaded = 0
        tmp = local.with_suffix(local.suffix + ".part")
        try:
            with tmp.open("wb") as handle:
                for chunk in resp.iter_content(chunk_size=CHUNK):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if progress:
                        progress(downloaded, total)
            tmp.replace(local)
        finally:
            resp.close()
        log.debug("完成 %s（%s）", local.name, human_size(downloaded))

    def download_many(
        self,
        files: Iterable[CameraFile | str],
        dest_dir: str | Path = "downloads",
        progress: Optional[ProgressFn] = None,
        super_download: bool = True,
    ) -> Dict[str, str]:
        """批次下載。預設會先開 super download（官方 App 也是這樣做）。"""
        names = [f.name if isinstance(f, CameraFile) else str(f) for f in files]
        results: Dict[str, str] = {}
        if super_download:
            self.set_super_download(True)
        try:
            for name in names:
                try:
                    path = self.download(name, dest_dir, progress=progress)
                    results[name] = str(path)
                except CameraError as exc:
                    results[name] = f"ERROR: {exc}"
        finally:
            if super_download:
                self.set_super_download(False)
        return results

    def download_latest(self, count: int = 1, **kwargs) -> Dict[str, str]:
        files = sorted(self.files(), key=lambda f: f.start.timestamp() if f.start else 0, reverse=True)
        return self.download_many(files[:count], **kwargs)

    def set_super_download(self, enabled: bool) -> bool:
        for cmd in ("API_SuperDownload", "APP_SuperDownload"):
            try:
                payload = self.transport.request(cmd, {"switch": "on" if enabled else "off"})
            except TransportError:
                continue
            code = errcode(payload)
            return code in (None, 0)
        return False

    # ── 其他 ────────────────────────────────────────────────────
    def snapshot(self, dest: Optional[str | Path] = None) -> Optional[Path]:
        """請相機拍一張，回傳本機路徑（有些韌體只存在 SD 卡上）。"""
        payload = self.transport.request("API_CameraCapture")
        data = find_dict(payload)
        remote = None
        for key in ("path", "file", "name", "imgname"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                remote = value.strip()
                break
        if not remote:
            return None
        target = Path(dest or f"snapshot_{int(time.time())}.jpg")
        resp = self.transport.open_stream(remote)
        try:
            if resp.status_code != 200:
                return None
            target.write_bytes(resp.content)
        finally:
            resp.close()
        return target

    def rtsp_url(self, rear: bool = False) -> str:
        host = self.host.replace("http://", "").replace("https://", "").split("/")[0]
        if ":" in host:  # 去掉自訂埠，RTSP 固定 554
            host = host.split(":")[0]
        return f"rtsp://{host}:554/liveRTSP/{'av5' if rear else 'av4'}"

    def reboot(self) -> bool:
        try:
            self.transport.request("API_Reboot")
        except TransportError:
            return False
        return True

    # ── 診斷 ────────────────────────────────────────────────────
    def doctor(self, full_matrix: bool = False) -> Dict[str, Any]:
        """回傳診斷報告：連線、命中策略、各 cmd 可用性、原始回應樣本。"""
        report: Dict[str, Any] = {
            "host": self.host,
            "tcp": self.is_connected(),
            "strategy": None,
            "session": bool(self.transport.session_token),
            "commands": {},
            "notes": [],
        }
        try:
            self.connect()
        except CameraUnreachable as exc:
            report["error"] = str(exc)
            report["notes"].append("連不上：先確認 Wi-Fi 已連上 DDPAI_XXXX，並可 ping 到相機 IP。")
            return report

        report["strategy"] = self.transport.strategy.id if self.transport.strategy else None
        report["session"] = bool(self.transport.session_token)

        for cmd in PROBE_COMMANDS + ("API_GetStorageInfo", "API_GetMailboxData"):
            try:
                payload = self.transport.request(cmd, timeout=min(self.timeout, 4.0))
                rows = find_list(payload)
                report["commands"][cmd] = {
                    "ok": True,
                    "errcode": errcode(payload),
                    "rows": len(rows),
                    "keys": sorted(payload.keys())[:12] if isinstance(payload, dict) else type(payload).__name__,
                }
            except (TransportError, CameraError) as exc:
                report["commands"][cmd] = {"ok": False, "error": str(exc)[:160]}

        if full_matrix:
            report["matrix"] = self.transport.probe_matrix()

        if report["strategy"] in ("vcam-get-doilel", "cgi-get-doilel"):
            report["notes"].append("此韌體需要 `doi-lel: 1` header（與 visiondash 一致）。")
        if not report["session"]:
            report["notes"].append("未取得 session token；多數唯讀操作仍可用，必要時設 DDPAI_PASSWORD。")
        return report
