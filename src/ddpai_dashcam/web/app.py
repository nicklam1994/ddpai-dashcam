"""FastAPI 後端 + 單頁網頁儀表板。

設計原則：
  * 不打包前端（沒有 node 也能跑）：單一 index.html + 原生 JS
  * 影片播放走 Range 轉發，可直接拖動時間軸
  * 所有 ddpai 錯誤都回 502 + 可讀訊息，前端顯示在 banner
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from ..client import Camera, CameraError, CameraUnreachable
from ..models import human_size
from .. import discovery, live
from ..transport import STRATEGIES

log = logging.getLogger("ddpai.web")

STATIC_DIR = Path(__file__).parent / "static"
DOWNLOAD_DIR = Path(os.environ.get("DDPAI_DOWNLOAD_DIR", "downloads")).expanduser()
RECORD_DIR = Path(os.environ.get("DDPAI_RECORD_DIR", "recordings")).expanduser()
PHOTO_DIR = Path(os.environ.get("DDPAI_PHOTO_DIR", "photos")).expanduser()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    reset_camera()


app = FastAPI(title="DDPAI 行車紀錄器", version="0.1.0", lifespan=lifespan)

_lock = threading.Lock()
_camera: Optional[Camera] = None


# ── 相機取得 ───────────────────────────────────────────────────
def get_camera(reconnect: bool = False) -> Camera:
    global _camera
    with _lock:
        if _camera is not None and not reconnect:
            return _camera
        if _camera is not None:
            _camera.close()
        _camera = Camera(
            host=os.environ.get("DDPAI_HOST") or None,
            password=os.environ.get("DDPAI_PASSWORD", ""),
            timeout=float(os.environ.get("DDPAI_TIMEOUT", "8")),
        )
        return _camera


def reset_camera() -> None:
    global _camera
    with _lock:
        if _camera is not None:
            _camera.close()
        _camera = None


def fail(exc: Exception, status: int = 502) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": str(exc)})


def ok(payload: Any) -> Dict[str, Any]:
    return {"ok": True, "data": payload}


# ── 頁面 ───────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    page = STATIC_DIR / "index.html"
    if not page.exists():
        return HTMLResponse("<h1>找不到前端檔案</h1>", status_code=500)
    return HTMLResponse(page.read_text(encoding="utf-8"))


# ── API ────────────────────────────────────────────────────────
@app.get("/api/health")
def health() -> Any:
    try:
        camera = get_camera()
        info = camera.info()
        return ok({
            "host": camera.host,
            "strategy": camera.transport.strategy.id if camera.transport.strategy else None,
            "model": info.model,
            "version": info.version,
        })
    except (CameraError, CameraUnreachable, Exception) as exc:  # noqa: BLE001
        return fail(exc, status=503)


@app.get("/api/info")
def api_info() -> Any:
    try:
        camera = get_camera()
        info = camera.info()
        storage = camera.storage()
        return ok({
            "info": info.to_dict(),
            "storage": storage.to_dict() if storage else None,
            "storage_human": {
                "total": human_size(storage.total_bytes) if storage else None,
                "free": human_size(storage.free_bytes) if storage else None,
                "used": human_size(storage.used_bytes) if storage else None,
            },
            "host": camera.host,
        })
    except Exception as exc:  # noqa: BLE001
        return fail(exc)


@app.get("/api/files")
def api_files(rear: bool = False) -> Any:
    try:
        files = get_camera().files(rear=rear)
        return ok([f.to_dict() for f in files])
    except Exception as exc:  # noqa: BLE001
        return fail(exc)


@app.get("/api/events")
def api_events(rear: bool = False) -> Any:
    try:
        events = get_camera().events(rear=rear)
        return ok([e.to_dict() for e in events])
    except Exception as exc:  # noqa: BLE001
        return fail(exc)


@app.get("/api/doctor")
def api_doctor(matrix: bool = False) -> Any:
    try:
        camera = get_camera()
        return ok(camera.doctor(full_matrix=matrix))
    except Exception as exc:  # noqa: BLE001
        return fail(exc)


@app.get("/api/hosts")
def api_hosts() -> Any:
    return ok({
        "candidates": discovery.candidate_hosts(),
        "wsl": discovery.is_wsl(),
        "windows_wifi": discovery.windows_wifi_gateways(),
        "current": (get_camera().host if _camera else None),
    })


@app.post("/api/host")
def api_set_host(payload: Dict[str, str] = Body(...)) -> Any:
    host = (payload or {}).get("host", "").strip()
    if not host:
        return fail(ValueError("host 不可為空"), status=400)
    os.environ["DDPAI_HOST"] = host
    reset_camera()
    try:
        camera = get_camera()
        camera.connect()
        return ok({"host": camera.host})
    except Exception as exc:  # noqa: BLE001
        reset_camera()
        return fail(exc)


@app.post("/api/reconnect")
def api_reconnect() -> Any:
    reset_camera()
    try:
        camera = get_camera()
        camera.connect()
        return ok({"host": camera.host})
    except Exception as exc:  # noqa: BLE001
        return fail(exc)


@app.get("/api/raw")
def api_raw(cmd: str, request: Request) -> Any:
    params = {k: v for k, v in request.query_params.items() if k != "cmd"}
    try:
        return ok(get_camera().raw(cmd, params or None))
    except Exception as exc:  # noqa: BLE001
        return fail(exc)


# ── 影片：播放（Range 轉發）─────────────────────────────────────
@app.get("/api/media/{name:path}")
def api_media(name: str, request: Request) -> Response:
    try:
        camera = get_camera()
        upstream = camera.transport.open_stream(name, range_header=request.headers.get("range"))
    except Exception as exc:  # noqa: BLE001
        return fail(exc)

    if upstream.status_code not in (200, 206):
        upstream.close()
        return JSONResponse(
            status_code=502,
            content={"ok": False, "error": f"相機回應 HTTP {upstream.status_code}"},
        )

    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    for key in ("Content-Length", "Content-Range"):
        if key in upstream.headers:
            headers[key] = upstream.headers[key]

    def iterfile():
        try:
            for chunk in upstream.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    media_type = "video/mp4" if name.lower().endswith((".mp4", ".mov")) else "application/octet-stream"
    return StreamingResponse(iterfile(), status_code=upstream.status_code,
                             media_type=media_type, headers=headers)


# ── 下載到本機 ─────────────────────────────────────────────────
@app.post("/api/download")
def api_download(payload: Dict[str, Any] = Body(...)) -> Any:
    names = payload.get("names") or ([payload["name"]] if payload.get("name") else [])
    if not names:
        return fail(ValueError("未指定檔名"), status=400)
    try:
        camera = get_camera()
        results = camera.download_many(names, dest_dir=DOWNLOAD_DIR)
        return ok(results)
    except Exception as exc:  # noqa: BLE001
        return fail(exc)


@app.get("/api/downloaded")
def api_downloaded() -> Any:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    entries = []
    for path in sorted(DOWNLOAD_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.is_file() and not path.name.endswith(".part"):
            entries.append({
                "name": path.name,
                "size": human_size(path.stat().st_size),
                "bytes": path.stat().st_size,
            })
    return ok({"dir": str(DOWNLOAD_DIR.resolve()), "files": entries})


@app.get("/api/local/{name:path}")
def api_local(name: str) -> Response:
    path = (DOWNLOAD_DIR / name).resolve()
    try:
        path.relative_to(DOWNLOAD_DIR.resolve())
    except ValueError:
        return fail(ValueError("非法路徑"), status=400)
    if not path.exists():
        return fail(FileNotFoundError(name), status=404)
    return FileResponse(path)


@app.get("/api/rtsp")
def api_rtsp(rear: bool = False) -> Any:
    try:
        return ok({"url": get_camera().rtsp_url(rear=rear), "ffmpeg": live.available()})
    except Exception as exc:  # noqa: BLE001
        return fail(exc)


# ── 即時影像：照片 / MJPEG 直播 / 錄短片 ───────────────────────
_live_slots = threading.Semaphore(2)


@app.get("/api/snapshot.jpg")
def api_snapshot(width: int = Query(1280, ge=320, le=2560),
                 save: bool = True) -> Response:
    """抓一張即時照片。預設同時存進本機相冊（photos/）。"""
    try:
        camera = get_camera()
        frame = live.grab_frame(camera.rtsp_url(), width=width)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"ok": False, "error": str(exc)})

    headers = {"Cache-Control": "no-store"}
    if save:
        try:
            name = save_photo(frame)
            headers["X-Photo-Name"] = name
        except OSError as exc:
            log.debug("照片存檔失敗：%s", exc)
    return Response(content=frame, media_type="image/jpeg", headers=headers)


def save_photo(frame: bytes, prefix: str = "ddpai") -> str:
    """把 JPEG 寫進相冊目錄，回傳檔名（同名自動加序號）。"""
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = PHOTO_DIR / f"{prefix}_{stamp}.jpg"
    suffix = 1
    while target.exists():
        target = PHOTO_DIR / f"{prefix}_{stamp}_{suffix}.jpg"
        suffix += 1
    target.write_bytes(frame)
    return target.name


def _safe_child(base: Path, name: str) -> Path:
    path = (base / name).resolve()
    path.relative_to(base.resolve())  # 路徑穿越會拋 ValueError
    return path


@app.get("/api/photos")
def api_photos() -> Any:
    """相冊清單（新的排前面）。"""
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for path in PHOTO_DIR.glob("*.jpg"):
        stat = path.stat()
        items.append({
            "name": path.name,
            "bytes": stat.st_size,
            "size": human_size(stat.st_size),
            "mtime": int(stat.st_mtime),
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
            "url": f"/api/photos/{path.name}",
        })
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return ok({"dir": str(PHOTO_DIR.resolve()), "photos": items})


@app.get("/api/photos/{name}")
def api_photo_file(name: str) -> Response:
    try:
        path = _safe_child(PHOTO_DIR, name)
    except ValueError:
        return fail(ValueError("非法路徑"), status=400)
    if not path.exists():
        return fail(FileNotFoundError(name), status=404)
    return FileResponse(path, media_type="image/jpeg")


@app.delete("/api/photos/{name}")
def api_photo_delete(name: str) -> Any:
    try:
        path = _safe_child(PHOTO_DIR, name)
    except ValueError:
        return fail(ValueError("非法路徑"), status=400)
    if not path.exists():
        return fail(FileNotFoundError(name), status=404)
    path.unlink()
    return ok({"deleted": name})


@app.get("/api/camera-album")
def api_camera_album(limit: int = Query(0, ge=0, le=2000)) -> Any:
    """相機端相簿：照片（若韌體支援）、事件、錄影，一次給齊。"""
    try:
        album = get_camera().camera_album(limit=limit)
        return ok(album)
    except Exception as exc:  # noqa: BLE001
        return fail(exc)


@app.get("/api/camera-photo/{name:path}")
def api_camera_photo(name: str) -> Response:
    """代理相機上的照片（給支援照片 API 的韌體用；本機韌體不會有圖）。"""
    try:
        camera = get_camera()
        upstream = camera.transport.open_stream(name)
    except Exception as exc:  # noqa: BLE001
        return fail(exc)
    if upstream.status_code not in (200, 206):
        upstream.close()
        return JSONResponse(status_code=404,
                            content={"ok": False, "error": f"相機沒有這個檔案（HTTP {upstream.status_code}）"})
    data = upstream.content
    upstream.close()
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "max-age=600"})


@app.post("/api/camera-capture")
def api_camera_capture(payload: Dict[str, Any] = Body(default={})) -> Any:
    """叫相機自己抓拍一張（存在 SD 卡），並下載到本機相冊。

    與 `/api/snapshot.jpg` 的分別：
      * snapshot = 從即時串流截圖（相機不留下檔案）
      * camera-capture = 相機真的寫一張 JPG 到 SD 卡（需要登入）
    """
    keep_on_camera = bool((payload or {}).get("keep_on_camera", True))
    try:
        camera = get_camera()
        if not camera.login():
            return fail(CameraError("登入相機失敗（API_RequestCertificate 未通過）"))
        photo = camera.capture()
        if photo is None:
            return fail(CameraError("相機沒有回報新照片（mailbox 沒有 MSG_EventOccured）"))
        target = camera.download_photo(photo.name, dest_dir=PHOTO_DIR / "camera")
    except Exception as exc:  # noqa: BLE001
        return fail(exc)

    return ok({
        "name": photo.name,
        "saved": str(target),
        "bytes": target.stat().st_size,
        "on_camera": keep_on_camera,
        "url": f"/api/camera-photo/{photo.name}",
        "local_url": f"/api/photos/camera/{photo.name}",
    })


@app.get("/api/photos/{folder}/{name}")
def api_photo_in_folder(folder: str, name: str) -> Response:
    """相冊子目錄（例如 photos/camera/ 存放從相機抓拍回來的照片）。"""
    try:
        path = _safe_child(PHOTO_DIR, f"{folder}/{name}")
    except ValueError:
        return fail(ValueError("非法路徑"), status=400)
    if not path.exists():
        return fail(FileNotFoundError(name), status=404)
    return FileResponse(path, media_type="image/jpeg")


@app.delete("/api/camera-media/{name:path}")
def api_camera_delete(name: str) -> Any:
    """刪除相機上的檔案（影片或照片）。"""
    try:
        camera = get_camera()
        result = camera.delete_files([name])
        return ok(result)
    except Exception as exc:  # noqa: BLE001
        return fail(exc)


@app.post("/api/lock")
def api_lock(payload: Dict[str, Any] = Body(...)) -> Any:
    """鎖定／解鎖影片（鎖住的片段不會被循環錄影覆蓋）。"""
    name = str((payload or {}).get("name", "")).strip()
    if not name:
        return fail(ValueError("未指定檔名"), status=400)
    locked = bool((payload or {}).get("locked", True))
    try:
        return ok(get_camera().lock_file(name, locked=locked))
    except Exception as exc:  # noqa: BLE001
        return fail(exc)


@app.get("/api/locked")
def api_locked() -> Any:
    try:
        return ok([f.to_dict() for f in get_camera().locked_files()])
    except Exception as exc:  # noqa: BLE001
        return fail(exc)


@app.get("/api/mailbox")
def api_mailbox() -> Any:
    try:
        return ok(get_camera().mailbox())
    except Exception as exc:  # noqa: BLE001
        return fail(exc)


@app.get("/api/captures")
def api_captures() -> Any:
    """相冊總覽：照片 + 錄影片段（同一個畫面用）。"""
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    RECORD_DIR.mkdir(parents=True, exist_ok=True)

    photos = []
    for path in PHOTO_DIR.glob("*.jpg"):
        stat = path.stat()
        photos.append({
            "kind": "photo",
            "name": path.name,
            "bytes": stat.st_size,
            "size": human_size(stat.st_size),
            "mtime": int(stat.st_mtime),
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
            "url": f"/api/photos/{path.name}",
        })

    records = []
    for path in RECORD_DIR.glob("*.mp4"):
        stat = path.stat()
        records.append({
            "kind": "video",
            "name": path.name,
            "bytes": stat.st_size,
            "size": human_size(stat.st_size),
            "mtime": int(stat.st_mtime),
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
            "url": f"/api/records/{path.name}",
        })

    items = sorted(photos + records, key=lambda item: item["mtime"], reverse=True)
    return ok({
        "photo_dir": str(PHOTO_DIR.resolve()),
        "record_dir": str(RECORD_DIR.resolve()),
        "items": items,
        "photo_count": len(photos),
        "video_count": len(records),
    })


@app.get("/api/live.mjpg")
def api_live(width: int = Query(960, ge=320, le=1920)) -> Response:
    """MJPEG 直播串流，瀏覽器用 <img> 就能看。"""
    if not _live_slots.acquire(blocking=False):
        return JSONResponse(status_code=429,
                            content={"ok": False, "error": "已有 2 個直播連線在跑，稍後再試"})
    try:
        camera = get_camera()
        url = camera.rtsp_url()
    except Exception as exc:  # noqa: BLE001
        _live_slots.release()
        return JSONResponse(status_code=502, content={"ok": False, "error": str(exc)})

    def stream():
        try:
            for frame in live.mjpeg_frames(url, width=width):
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
        except Exception as exc:  # noqa: BLE001
            log.debug("直播中斷：%s", exc)
        finally:
            _live_slots.release()

    return StreamingResponse(
        stream(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "Connection": "close"},
    )


@app.post("/api/record")
def api_record(payload: Dict[str, Any] = Body(default={})) -> Any:
    """錄一段直播存成 mp4（預設 10 秒），回傳可下載的路徑。"""
    seconds = int((payload or {}).get("seconds", 10))
    try:
        camera = get_camera()
        RECORD_DIR.mkdir(parents=True, exist_ok=True)
        path = live.record(camera.rtsp_url(), seconds=seconds, dest_dir=str(RECORD_DIR))
    except Exception as exc:  # noqa: BLE001
        return fail(exc)
    return ok({
        "name": path.name,
        "seconds": seconds,
        "bytes": path.stat().st_size,
        "url": f"/api/records/{path.name}",
    })


@app.get("/api/records")
def api_records() -> Any:
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for path in sorted(RECORD_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        items.append({"name": path.name, "bytes": path.stat().st_size,
                      "url": f"/api/records/{path.name}"})
    return ok(items)


@app.get("/api/records/{name}")
def api_record_file(name: str) -> Response:
    path = (RECORD_DIR / name).resolve()
    try:
        path.relative_to(RECORD_DIR.resolve())
    except ValueError:
        return fail(ValueError("非法路徑"), status=400)
    if not path.exists():
        return fail(FileNotFoundError(name), status=404)
    return FileResponse(path, media_type="video/mp4", filename=name)


@app.delete("/api/records/{name}")
def api_record_delete(name: str) -> Any:
    path = (RECORD_DIR / name).resolve()
    try:
        path.relative_to(RECORD_DIR.resolve())
    except ValueError:
        return fail(ValueError("非法路徑"), status=400)
    if not path.exists():
        return fail(FileNotFoundError(name), status=404)
    path.unlink()
    return ok({"deleted": name})
