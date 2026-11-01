"""即時影像：RTSP 直播、拍照、錄短片。

瀏覽器播不了 RTSP，所以在後端用 ffmpeg 轉成 MJPEG 串流給 `<img>`，
零外掛、零前端依賴，跟其他功能一樣 clone 完就能用。

實測 DDPAI Mini3 Pro（v7.1.0.48）：
  rtsp://<ip>:554/liveRTSP/av4  → 1280x720 H.264 + pcm_alaw

兩個實機踩到的坑（都寫進程式碼裡了）：
  1. **相機同時只接受一個 RTSP session**。上一個 ffmpeg 沒收乾淨時，新的
     連線會拿到 `DESCRIBE failed: 404 File Not Found`，看起來像路徑錯，
     其實是舊 session 還佔著。所以這裡登記所有 ffmpeg 進程，失敗時先清理再重試。
  2. **音頻是 pcm_alaw**，mp4 容器不收，`-c copy` 會直接失敗。錄影要轉 aac
     （或 `-an` 丟掉音頻）。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterator, Optional, Set

log = logging.getLogger(__name__)

FFMPEG = shutil.which("ffmpeg")
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"

# 相機只吃一個 RTSP session，所有拉流操作排隊
_session_lock = threading.Lock()
_procs: Set[subprocess.Popen] = set()
_procs_lock = threading.Lock()

BUSY_HINTS = ("404", "describe failed", "not found", "connection refused", "timed out")


class LiveError(RuntimeError):
    pass


def available() -> bool:
    return FFMPEG is not None


def _require_ffmpeg() -> str:
    if not FFMPEG:
        raise LiveError("找不到 ffmpeg，請先安裝：sudo apt install ffmpeg")
    return FFMPEG


def _register(proc: subprocess.Popen) -> subprocess.Popen:
    with _procs_lock:
        _procs.add(proc)
    return proc


def _unregister(proc: subprocess.Popen) -> None:
    with _procs_lock:
        _procs.discard(proc)


def kill_stale(grace: float = 0.5) -> int:
    """收掉本模組留下的 ffmpeg（前一個 client 斷線後沒收乾淨的那些）。"""
    with _procs_lock:
        alive = [p for p in _procs if p.poll() is None]
    for proc in alive:
        proc.terminate()
    deadline = time.monotonic() + grace
    for proc in alive:
        try:
            proc.wait(timeout=max(0.05, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
    for proc in alive:
        _unregister(proc)
    if alive:
        log.debug("清理 %d 個殘留 ffmpeg", len(alive))
    return len(alive)


def grab_frame(rtsp_url: str, width: int = 1280, timeout: float = 25.0) -> bytes:
    """抓一張 JPEG（等於「即時照片」）。"""
    def attempt() -> tuple:
        cmd = [
            _require_ffmpeg(), "-nostdin", "-v", "error",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-frames:v", "1",
            "-vf", f"scale={width}:-2",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "3",
            "pipe:1",
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        return (result.stdout or b""), (result.stderr or b"").decode("utf-8", "replace")

    with _session_lock:
        frame, err = attempt()
        if not frame.startswith(JPEG_SOI):
            # 不管是 session 被佔用、還是相機剛從舊連線恢復，一律清理後重試一次
            log.debug("抓圖失敗，清理 ffmpeg 後重試：%s", err.strip()[:120])
            kill_stale()
            time.sleep(1.0)
            frame, err = attempt()

    if not frame.startswith(JPEG_SOI):
        raise LiveError(f"抓不到畫面（{err.strip()[:160] or 'ffmpeg 無輸出'}）")
    return frame


def mjpeg_frames(
    rtsp_url: str,
    width: int = 960,
    fps: int = 6,
    quality: int = 6,
    max_seconds: Optional[float] = None,
) -> Iterator[bytes]:
    """持續吐 JPEG 影格，供 MJPEG（multipart/x-mixed-replace）使用。"""
    cmd = [
        _require_ffmpeg(), "-nostdin", "-v", "error",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-an",
        "-vf", f"fps={fps},scale={width}:-2",
        "-f", "mjpeg", "-q:v", str(quality),
        "pipe:1",
    ]
    kill_stale()
    proc = _register(subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, bufsize=0))
    started = time.monotonic()
    buffer = b""
    try:
        assert proc.stdout is not None
        while True:
            if max_seconds and time.monotonic() - started > max_seconds:
                break
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            buffer += chunk
            while True:
                start = buffer.find(JPEG_SOI)
                end = buffer.find(JPEG_EOI, start + 2) if start >= 0 else -1
                if start < 0 or end < 0:
                    if len(buffer) > 4 * 1024 * 1024:
                        buffer = buffer[-1024:]
                    break
                frame = buffer[start:end + 2]
                buffer = buffer[end + 2:]
                yield frame
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        _unregister(proc)


def record(rtsp_url: str, seconds: int = 10, timeout: Optional[float] = None,
           dest_dir: Optional[str] = None) -> Path:
    """錄一段直播存成 mp4（影像直通、音頻轉 aac，失敗就丟掉音頻）。"""
    seconds = max(1, min(int(seconds), 300))
    base = Path(dest_dir) if dest_dir else Path(tempfile.mkdtemp(prefix="ddpai-rec-"))
    base.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = base / f"live_{stamp}.mp4"
    suffix = 1
    while out.exists():
        out = base / f"live_{stamp}_{suffix}.mp4"
        suffix += 1

    def attempt(with_audio: bool) -> tuple:
        cmd = [
            _require_ffmpeg(), "-nostdin", "-v", "error", "-y",
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-t", str(seconds),
            "-c:v", "copy",
        ]
        cmd += ["-c:a", "aac", "-b:a", "64k"] if with_audio else ["-an"]
        cmd += ["-movflags", "+faststart", str(out)]
        result = subprocess.run(cmd, capture_output=True,
                                timeout=timeout or seconds + 40, check=False)
        return (result.stderr or b"").decode("utf-8", "replace")

    with _session_lock:
        err = attempt(True)
        ok = out.exists() and out.stat().st_size > 0
        if not ok and any(hint in err.lower() for hint in BUSY_HINTS):
            kill_stale()
            time.sleep(1.0)
            err = attempt(True)
            ok = out.exists() and out.stat().st_size > 0
        # pcm_alaw 之類的音頻無法轉 aac 時，退一步只留影像
        if not ok and "codec" in err.lower():
            log.debug("音頻不相容，改為純影像錄影：%s", err.strip()[:120])
            err = attempt(False)
            ok = out.exists() and out.stat().st_size > 0

    if not ok:
        raise LiveError(f"錄影失敗（{err.strip()[:160] or '無輸出'}）")
    return out
