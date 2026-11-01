"""回應解析：把 DDPAI 各式各樣的回應外殼剝到剩下資料本體。

已知的回應形態（同一支 cmd 在不同韌體上都出現過）：
  1. {"errcode": 0, "data": {...}}
  2. {"errcode": 0, "data": "{\"sdcapacity\": 123}"}   ← data 是 JSON 字串
  3. {"file": [ {...}, {...} ]}                        ← 播放清單
  4. [ {...} ]                                         ← 直接回陣列
  5. "<html>...</html>"                                ← 未通過校驗時回登入頁
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

_LIST_KEYS = ("file", "files", "list", "event", "events", "data", "items", "records")
_CONTAINER_KEYS = ("data", "result", "body", "response", "content")


def maybe_json(value: Any) -> Any:
    """字串若是 JSON 就解析，否則原樣回傳。"""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    if text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return value


def unwrap(payload: Any) -> Any:
    """反覆剝殼，直到拿到真正的資料。"""
    current = maybe_json(payload)
    for _ in range(6):
        if not isinstance(current, dict):
            break
        # errcode/errcode 為非 0 時，答案可能在 message 裡，先不處理
        inner = None
        for key in _CONTAINER_KEYS:
            if key in current:
                candidate = maybe_json(current[key])
                if isinstance(candidate, (dict, list)):
                    inner = candidate
                    break
        if inner is None:
            break
        current = inner
    return current


def errcode(payload: Any) -> Optional[int]:
    """取出 errcode（不同韌體用 errcode / errCode / code）。"""
    if not isinstance(payload, dict):
        return None
    lowered = {str(k).lower(): v for k, v in payload.items()}
    for key in ("errcode", "err_code", "code", "ret"):
        if key in lowered:
            try:
                return int(str(lowered[key]).strip())
            except (TypeError, ValueError):
                return None
    return None


def find_list(payload: Any, keys: Iterable[str] = _LIST_KEYS) -> List[Dict[str, Any]]:
    """在任意深度的回應中找出第一份「字典陣列」。"""
    payload = maybe_json(payload)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    lowered = {str(k).lower(): v for k, v in payload.items()}
    for key in keys:
        if key in lowered:
            found = find_list(lowered[key], keys)
            if found:
                return found

    # 鍵名不合預期時，退一步找結構：值若是字典或陣列就往下挖
    for key, value in payload.items():
        if str(key).lower() in ("errcode", "errmsg", "message", "msg"):
            continue
        inner = maybe_json(value)
        if isinstance(inner, list) and inner and isinstance(inner[0], dict):
            return [item for item in inner if isinstance(item, dict)]
        if isinstance(inner, dict):
            found = find_list(inner, keys)
            if found:
                return found
    return []


def find_dict(payload: Any) -> Dict[str, Any]:
    """回傳最內層的字典（用於單筆資訊類回應）。"""
    payload = maybe_json(payload)
    if not isinstance(payload, dict):
        return {}
    inner = unwrap(payload)
    return inner if isinstance(inner, dict) else payload


def is_probably_html(body: str) -> bool:
    head = (body or "").lstrip()[:200].lower()
    return head.startswith("<!doctype") or head.startswith("<html") or "<head>" in head


def extract_string(payload: Any, *keys: str) -> Optional[str]:
    """在回應的任意深度找某個鍵的字串值（例如 acSessionId）。"""
    payload = maybe_json(payload)
    if isinstance(payload, dict):
        lowered = {str(k).lower(): v for k, v in payload.items()}
        for key in keys:
            if key.lower() in lowered:
                value = maybe_json(lowered[key.lower()])
                if isinstance(value, (str, int)):
                    return str(value)
        for value in payload.values():
            found = extract_string(value, *keys)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = extract_string(item, *keys)
            if found:
                return found
    return None
