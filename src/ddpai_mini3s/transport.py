"""傳輸層：處理 DDPAI 韌體之間最大的地雷 —— 握手方式不一致。

兩個上游專案各自找到一組「在某台機器上可用」的組合：

  ddpai-api      POST /vcam/cmd.cgi?cmd=X    header: sessionid（先 API_RequestSessionID）
  visiondash     GET  /vcam/cmd.cgi?cmd=X    header: doi-lel: 1

Mini3S 屬於較舊世代，可能兩者皆非。因此本層把「路徑 × 方法 × header 組合」
展開成策略矩陣，用一次 API_GetBaseInfo 探測，把可用組合快取起來。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

import requests

log = logging.getLogger(__name__)

UA = "Dalvik/2.1.0 (Linux; U; Android 13; SM-A127F Build/TP1A.220624.014)"
DEFAULT_TIMEOUT = 8.0
SESSION_CMD = "API_RequestSessionID"


@dataclass(frozen=True)
class Strategy:
    """一種請求組合。"""

    id: str
    path: str = "/vcam/cmd.cgi"
    method: str = "GET"
    doi_lel: bool = False     # 帶 `doi-lel: 1`
    session: bool = False     # 帶 `sessionid`
    form_body: bool = False   # POST 時把參數放進 form body

    def headers(self, session_token: str = "") -> Dict[str, str]:
        headers = {
            "User-Agent": UA,
            "Accept": "application/json, */*",
            "Connection": "close",
            "Accept-Encoding": "gzip",
        }
        if self.doi_lel:
            headers["doi-lel"] = "1"
        if self.session and session_token:
            headers["sessionid"] = session_token
            headers["Content-type"] = "application/x-www-form-urlencoded"
        return headers


# 探測順序：先試上游專案已驗證過的組合，再試退化版。
STRATEGIES: Tuple[Strategy, ...] = (
    Strategy("vcam-get-doilel", doi_lel=True),
    Strategy("vcam-get-plain"),
    Strategy("vcam-get-session", session=True),
    Strategy("vcam-get-doilel-session", doi_lel=True, session=True),
    Strategy("vcam-post-session", method="POST", session=True, form_body=True),
    Strategy("vcam-post-plain", method="POST", form_body=True),
    Strategy("cgi-get-doilel", path="/cmd.cgi", doi_lel=True),
    Strategy("cgi-get-plain", path="/cmd.cgi"),
    Strategy("cgi-post-session", path="/cmd.cgi", method="POST", session=True, form_body=True),
)

STRATEGY_BY_ID = {s.id: s for s in STRATEGIES}

# 探測用的命令：這些 cmd 在絕大多數 DDPAI 韌體都存在。
PROBE_COMMANDS = ("API_GetBaseInfo", "G_DEV_TYPE", "APP_PlaybackListReq")

# 進入「需要授權」狀態時的特徵碼
AUTH_HINTS = ("login", "logon", "auth", "password", "unauthorized", "permission", "not login")


class TransportError(RuntimeError):
    pass


@dataclass
class ProbeResult:
    strategy: Strategy
    status: Optional[int]
    ok: bool
    body: str = ""
    error: str = ""

    def summary(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy.id,
            "path": self.strategy.path,
            "method": self.strategy.method,
            "http_status": self.status,
            "ok": self.ok,
            "error": self.error,
            "body_preview": self.body[:200],
        }


@dataclass
class Transport:
    """對單一台相機的 HTTP 通道。"""

    host: str
    timeout: float = DEFAULT_TIMEOUT
    strategy: Optional[Strategy] = None
    session_token: str = ""
    password: str = ""
    probes: List[ProbeResult] = field(default_factory=list, repr=False)
    _session: Optional[requests.Session] = field(default=None, repr=False)

    # ── 基本屬性 ────────────────────────────────────────────────
    @property
    def base_url(self) -> str:
        host = self.host
        if host.startswith("http://") or host.startswith("https://"):
            return host.rstrip("/")
        return f"http://{host}"

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.trust_env = False  # 避免系統 proxy 干擾 193.168.0.x
        return self._session

    def headers(self) -> Dict[str, str]:
        strategy = self.strategy or STRATEGY_BY_ID["vcam-get-plain"]
        return strategy.headers(self.session_token)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    # ── 單次請求 ────────────────────────────────────────────────
    def _send(
        self,
        strategy: Strategy,
        cmd: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> ProbeResult:
        query = {"cmd": cmd}
        if params:
            query.update({k: v for k, v in params.items() if v is not None})
        url = self.base_url + strategy.path
        headers = strategy.headers(self.session_token)
        kwargs: Dict[str, Any] = {
            "headers": headers,
            "timeout": timeout or self.timeout,
            "allow_redirects": True,
        }
        try:
            if strategy.method == "POST":
                if strategy.form_body:
                    resp = self.session.post(
                        url, params={"cmd": cmd}, data=params or {}, **kwargs
                    )
                else:
                    resp = self.session.post(url, params=query, json=params or {}, **kwargs)
            else:
                resp = self.session.get(url, params=query, **kwargs)
        except requests.exceptions.Timeout:
            return ProbeResult(strategy, None, False, error="timeout")
        except requests.exceptions.RequestException as exc:
            return ProbeResult(strategy, None, False, error=str(exc)[:160])

        body = resp.text or ""
        return ProbeResult(
            strategy=strategy,
            status=resp.status_code,
            ok=_looks_like_json(resp),
            body=body,
            error="" if _looks_like_json(resp) else _reason(resp),
        )

    # ── 對外請求（自動選策略）────────────────────────────────────
    def request(
        self,
        cmd: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
        retries: int = 1,
    ) -> Dict[str, Any]:
        """發出指令並回傳解析後的 JSON。失敗會退回重新探測一次。"""
        if self.strategy is None:
            self.detect()

        attempt = 0
        while True:
            result = self._send(self.strategy, cmd, params, timeout)
            if result.ok:
                return _decode(result.body)
            # 策略失效（例如相機重開後換了模式）→ 重新探測一次
            if attempt < retries:
                attempt += 1
                log.debug("cmd %s 失敗（%s），重新探測策略", cmd, result.error)
                self.strategy = None
                self.detect(force=True)
                continue
            raise TransportError(f"{cmd} 失敗：{result.error or 'HTTP ' + str(result.status)}")

    # ── 探測與握手 ──────────────────────────────────────────────
    def detect(self, force: bool = False) -> Strategy:
        """依序嘗試策略矩陣，找出第一組能回傳 JSON 的組合。"""
        if self.strategy is not None and not force:
            return self.strategy

        self.probes = []
        connection_dead = False
        for index, strategy in enumerate(STRATEGIES):
            # 先綁定策略，讓 session 握手請求使用同一組 header 組合
            self.strategy = strategy
            # 有些韌體（ddpai-api 描述的那種）必須先拿 session token 才肯回應
            if not self.session_token:
                self._try_session()

            for cmd_index, cmd in enumerate(PROBE_COMMANDS[:2]):
                probe = self._send(strategy, cmd, timeout=min(self.timeout, 4.0))
                self.probes.append(probe)
                if probe.ok:
                    log.debug("策略命中：%s（cmd=%s）", strategy.id, cmd)
                    return strategy
                if probe.status is None:  # 連線層失敗
                    # 只有第一組策略的第一個命令就撞牆，才判定「整台不通」；
                    # 中途偶發斷線（廉價 AP 常見）應該換策略繼續試
                    if index == 0 and cmd_index == 0:
                        connection_dead = True
                    break
            if connection_dead:
                break

        self.strategy = None
        detail = "; ".join(
            f"{p.strategy.id}={p.error or p.status}" for p in self.probes[-4:]
        )
        raise TransportError(f"{self.host} 沒有可用的 API 策略（{detail}）")

    def _try_session(self) -> str:
        """嘗試取得 session token；失敗不影響可用性。"""
        if self.strategy is None:
            return ""
        probe = self._send(self.strategy, SESSION_CMD, timeout=min(self.timeout, 4.0))
        self.probes.append(probe)
        if probe.ok:
            from .parser import extract_string

            token = extract_string(_decode(probe.body), "acSessionId", "sessionid", "session_id")
            if token:
                self.session_token = token
                log.debug("取得 session token：%s…", token[:8])
                return token
        # 有些韌體要先用預設密碼登入才肯發 token
        if self.password:
            self._try_logon()
        return ""

    def _try_logon(self) -> None:
        for cmd in ("API_SetLogonInfo", "API_RequestCertificate"):
            probe = self._send(
                self.strategy,
                cmd,
                {"password": self.password, "pwd": self.password},
                timeout=min(self.timeout, 4.0),
            )
            self.probes.append(probe)
            if probe.ok:
                from .parser import extract_string

                token = extract_string(_decode(probe.body), "acSessionId", "sessionid", "certificate")
                if token:
                    self.session_token = token
                    return

    def probe_matrix(self, commands: Tuple[str, ...] = PROBE_COMMANDS) -> List[Dict[str, Any]]:
        """給 doctor 用的完整探測表。"""
        rows: List[Dict[str, Any]] = []
        for strategy in STRATEGIES:
            for cmd in commands:
                probe = self._send(strategy, cmd, timeout=min(self.timeout, 3.0))
                rows.append(probe.summary() | {"cmd": cmd})
        self.probes.extend([])  # 保持 probes 只記錄 detect 過程
        return rows

    # ── 檔案存取 ────────────────────────────────────────────────
    def file_url(self, name: str) -> str:
        return f"{self.base_url}/{name.lstrip('/')}"

    def head(self, name: str) -> Optional[int]:
        """檢查遠端檔案大小；不支援 HEAD 的韌體回 None。"""
        try:
            resp = self.session.head(self.file_url(name), headers=self.headers(), timeout=self.timeout)
            if resp.status_code == 200 and resp.headers.get("Content-Length"):
                return int(resp.headers["Content-Length"])
        except (requests.RequestException, ValueError):
            pass
        return None

    def open_stream(self, name: str, range_header: Optional[str] = None, timeout: float = 30.0):
        """開一個串流連線（供下載與網頁播放共用）。"""
        headers = self.headers()
        if range_header:
            headers["Range"] = range_header
        return self.session.get(
            self.file_url(name), headers=headers, stream=True, timeout=timeout
        )


# ── 工具函式 ───────────────────────────────────────────────────
def _looks_like_json(resp: requests.Response) -> bool:
    if resp.status_code != 200:
        return False
    body = (resp.text or "").lstrip()
    if not body or body[0] not in "[{":
        return False
    try:
        json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return False
    return True


def _reason(resp: requests.Response) -> str:
    body = (resp.text or "").lstrip()[:120]
    if resp.status_code == 403:
        return "HTTP 403（header 不被接受）"
    if resp.status_code == 404:
        return "HTTP 404（此韌體沒有這個路徑）"
    if body[:1] == "<":
        return "回傳 HTML（可能是登入頁，需密碼）"
    return f"HTTP {resp.status_code}"


def _decode(body: str) -> Any:
    from .parser import maybe_json

    parsed = maybe_json(body)
    if isinstance(parsed, str):
        raise TransportError("回應不是 JSON")
    return parsed


def strategy_from_id(strategy_id: str) -> Optional[Strategy]:
    return STRATEGY_BY_ID.get(strategy_id)


def env_password() -> str:
    return os.environ.get("DDPAI_PASSWORD", "")


def now() -> float:
    return time.time()
