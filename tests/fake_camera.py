"""假相機：在 localhost 模擬 DDPAI 韌體，讓整條鏈路可以離線測試。

三種模式對應三種真實韌體行為：
  plain    —— 直接回 JSON（最寬鬆）
  doilel   —— 必須帶 `doi-lel: 1`，否則 403（visiondash 遇到的那種）
  session  —— 必須先 API_RequestSessionID 拿到 token，之後帶 sessionid（ddpai-api 那種）
  htmlwall —— 一律回登入頁（模擬密碼未過）
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse

BASE_INFO = {
    "nickname": "DDPAI_Mini3S",
    "model": "mini3s",
    "version": "v6.4.0",
    "sn": "MINI3S000123",
    "macaddr": "AA:BB:CC:DD:EE:FF",
    "sdcapacity": str(64 * 1024 * 1024),  # KB → 64GB
    "sdspare": str(32 * 1024 * 1024),
    "sdbrand": "SanDisk",
}

FILES = [
    {"name": "/DCIM/100DDPAI/FILE_20260101120000_0001.MP4", "starttime": 1767240000,
     "endtime": 1767240060, "size": 31457280, "index": 1},
    {"name": "/DCIM/100DDPAI/FILE_20260101120100_0002.MP4", "starttime": 1767240060,
     "endtime": 1767240120, "size": 41943040, "index": 2},
    {"name": "/DCIM/100DDPAI/FILE_20260101120200_0003.MP4", "starttime": 1767240120,
     "endtime": 1767240180, "size": 52428800, "index": 3},
]

EVENTS = [
    {"name": "/DCIM/EVENT/EMERGENCY_20260101120500.MP4", "imgname": "/DCIM/EVENT/EMERGENCY_20260101120500.JPG",
     "starttime": 1767240300, "endtime": 1767240330, "size": 20971520, "index": 1},
]

MEDIA = {
    FILES[0]["name"]: b"\x00\x00\x00\x18ftypmp42" + b"A" * 4096,
    FILES[1]["name"]: b"\x00\x00\x00\x18ftypmp42" + b"B" * 8192,
    FILES[2]["name"]: b"\x00\x00\x00\x18ftypmp42" + b"C" * 2048,
    EVENTS[0]["name"]: b"\x00\x00\x00\x18ftypmp42" + b"E" * 1024,
}

SESSION_TOKEN = "TESTTOKEN1234"


class FakeCamera:
    """可程式化的假相機伺服器（background thread）。"""

    def __init__(self, mode: str = "plain", port: int = 0) -> None:
        self.mode = mode
        self.requests: list = []
        handler = _make_handler(self)
        self.server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}"

    def start(self) -> "FakeCamera":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self) -> "FakeCamera":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()


def _make_handler(camera: FakeCamera):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:  # 靜音
            pass

        # ── 驗證 ────────────────────────────────────────────
        def _authorised(self) -> bool:
            if camera.mode == "htmlwall":
                return False
            if camera.mode == "doilel":
                return self.headers.get("doi-lel") == "1"
            if camera.mode == "session":
                return self.headers.get("sessionid") == SESSION_TOKEN
            return True

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            camera.requests.append(("GET", parsed.path, params, dict(self.headers)))

            if parsed.path in MEDIA:
                self._serve_media(MEDIA[parsed.path])
                return
            if parsed.path in ("/vcam/cmd.cgi", "/cmd.cgi"):
                self._serve_cmd(params)
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            camera.requests.append(("POST", parsed.path, params, dict(self.headers)))
            if parsed.path in ("/vcam/cmd.cgi", "/cmd.cgi"):
                self._serve_cmd(params, body)
                return
            self._send(404, b"not found", "text/plain")

        def do_HEAD(self) -> None:
            parsed = urlparse(self.path)
            data = MEDIA.get(parsed.path)
            if data is None:
                self._send(404, b"", "text/plain")
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()

        # ── 回應 ────────────────────────────────────────────
        def _serve_media(self, data: bytes) -> None:
            rng = self.headers.get("Range")
            if rng:
                match = re.match(r"bytes=(\d+)-(\d*)", rng)
                if match:
                    start = int(match.group(1))
                    end = int(match.group(2)) if match.group(2) else len(data) - 1
                    chunk = data[start:end + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(len(chunk)))
                    self.end_headers()
                    self.wfile.write(chunk)
                    return
            self._send(200, data, "video/mp4")

        def _serve_cmd(self, params: Dict[str, str], body: bytes = b"") -> None:
            cmd = params.get("cmd", "")
            if cmd == "API_RequestSessionID":
                if camera.mode == "htmlwall":
                    self._send(403, b"<html>login</html>", "text/html")
                    return
                self._json({"errcode": 0, "data": {"acSessionId": SESSION_TOKEN}})
                return

            if not self._authorised():
                self._send(403, b"<html><head>login</head></html>", "text/html")
                return

            if cmd == "API_GetBaseInfo":
                self._json({"errcode": 0, "data": BASE_INFO})
            elif cmd == "G_DEV_TYPE":
                self._json({"errcode": 0, "devtype": "mini3s"})
            elif cmd == "APP_PlaybackListReq":
                self._json({"errcode": 0, "file": FILES})
            elif cmd == "APP_PlaybackListReq_RearCam":
                self._json({"errcode": 0, "file": []})
            elif cmd == "APP_EventListReq":
                self._json({"errcode": 0, "event": EVENTS})
            elif cmd == "APP_EventListReq_RearCam":
                self._json({"errcode": 0, "event": []})
            elif cmd == "APP_ParkingEventListReq":
                self._json({"errcode": 0, "event": []})
            elif cmd == "API_GetStorageInfo":
                self._json({"errcode": 0, "data": json.dumps({
                    "sdcapacity": BASE_INFO["sdcapacity"], "sdspare": BASE_INFO["sdspare"]})})
            elif cmd == "API_SuperDownload":
                self._json({"errcode": 0})
            elif cmd == "API_CameraCapture":
                self._json({"errcode": 0, "data": {"path": "/DCIM/EVENT/EMERGENCY_20260101120500.JPG"}})
            elif cmd in ("API_Reboot", "API_SetLogonInfo", "API_GetMailboxData"):
                self._json({"errcode": 0, "data": []})
            else:
                self._json({"errcode": -1, "errmsg": "unknown cmd"})

        def _json(self, payload) -> None:
            self._send(200, json.dumps(payload).encode(), "application/json")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if body:
                self.wfile.write(body)

    return Handler
