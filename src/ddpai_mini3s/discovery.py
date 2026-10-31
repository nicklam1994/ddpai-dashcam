"""自動發現相機。

DDPAI 各世代的預設 IP 不統一，而且 WSL / 虛擬網卡會讓「抓本機預設閘道」
這招失效（WSL2 的閘道是 172.x 的虛擬交換器）。所以這裡做三件事：

  1. 常用候選 IP 清單（193.168.0.1 是 DDPAI 最常見的）
  2. 本機路由 / Windows Wi-Fi 介面的預設閘道（真的接上相機 AP 時就是相機）
  3. 並行 TCP + HTTP 探測，只接受「能回 DDPAI JSON」的主機
"""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, List, Optional, Sequence

log = logging.getLogger(__name__)

# 依出現頻率排序；Mini3S 世代最常見的是 193.168.0.1
KNOWN_HOSTS: Sequence[str] = (
    "193.168.0.1",
    "192.168.1.254",
    "192.168.100.1",
    "192.168.0.1",
    "192.168.4.1",
    "192.168.8.1",
    "10.0.0.1",
    "192.168.1.1",
)


def _run(cmd: List[str], timeout: float = 3.0) -> str:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return out.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version", "r", encoding="utf-8") as handle:
            return "microsoft" in handle.read().lower()
    except OSError:
        return False


def route_gateways() -> List[str]:
    """Linux 側的預設閘道。"""
    hosts: List[str] = []
    for line in _run(["ip", "route", "show", "default"]).splitlines():
        match = re.search(r"via\s+(\d+\.\d+\.\d+\.\d+)", line)
        if match:
            hosts.append(match.group(1))
    return hosts


def windows_wifi_gateways() -> List[str]:
    """WSL 專用：透過 powershell 讀 Windows 端 Wi-Fi 介面的閘道。

    在 WSL 裡「電腦連上相機 Wi-Fi」這件事發生在 Windows 側，
    WSL 自己的路由表看不到，只能問 Windows。
    """
    if not is_wsl():
        return []
    script = (
        "Get-NetIPConfiguration | "
        "Where-Object { $_.IPv4DefaultGateway -ne $null } | "
        "ForEach-Object { $_.InterfaceAlias + '=' + $_.IPv4DefaultGateway.NextHop }"
    )
    output = _run(["powershell.exe", "-NoProfile", "-Command", script], timeout=8.0)
    hosts: List[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        alias, _, hop = line.partition("=")
        hop = hop.strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", hop):
            continue
        # Wi-Fi 介面的閘道優先
        if "wi-fi" in alias.lower() or "wlan" in alias.lower() or "無線" in alias:
            hosts.insert(0, hop)
        else:
            hosts.append(hop)
    return hosts


def candidate_hosts(extra: Iterable[str] = ()) -> List[str]:
    """組出候選清單（去重、保持優先順序）。"""
    ordered: List[str] = []
    for host in list(extra) + windows_wifi_gateways() + route_gateways() + list(KNOWN_HOSTS):
        host = (host or "").strip()
        if host and host not in ordered:
            ordered.append(host)
    return ordered


def _split_host_port(host: str, default_port: int = 80) -> tuple:
    """接受 `1.2.3.4`、`1.2.3.4:8080`、`http://1.2.3.4:8080`。"""
    value = (host or "").strip()
    for prefix in ("http://", "https://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    value = value.split("/")[0]
    if ":" in value:
        name, _, port = value.rpartition(":")
        if port.isdigit():
            return name, int(port)
    return value, default_port


def tcp_open(host: str, port: int = 80, timeout: float = 0.8) -> bool:
    name, resolved_port = _split_host_port(host, port)
    try:
        with socket.create_connection((name, resolved_port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_host(host: str, timeout: float = 3.0, password: str = "") -> Dict[str, object]:
    """探測單一主機是否為 DDPAI 相機。"""
    from .transport import Transport, TransportError

    result: Dict[str, object] = {
        "host": host,
        "tcp": False,
        "api": False,
        "strategy": None,
        "error": "",
    }
    if not tcp_open(host, timeout=min(timeout, 1.5)):
        result["error"] = "TCP 80 不通"
        return result
    result["tcp"] = True

    transport = Transport(host=host, timeout=timeout, password=password)
    try:
        strategy = transport.detect()
        result["api"] = True
        result["strategy"] = strategy.id
    except TransportError as exc:
        result["error"] = str(exc)[:200]
    finally:
        transport.close()
    return result


def discover(
    hosts: Optional[Iterable[str]] = None,
    timeout: float = 3.0,
    password: str = "",
    max_workers: int = 8,
) -> Optional[str]:
    """回傳第一台確認可用的相機 IP，找不到回 None。"""
    targets = list(hosts) if hosts else candidate_hosts()
    if not targets:
        return None

    log.debug("並行探測 %d 個候選主機：%s", len(targets), targets)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(lambda h: probe_host(h, timeout, password), targets))

    for host in targets:  # 依原始優先順序取第一個成功的
        match = next((r for r in results if r["host"] == host and r["api"]), None)
        if match:
            log.debug("找到相機：%s（策略 %s）", host, match["strategy"])
            return host
    return None


def discover_all(
    hosts: Optional[Iterable[str]] = None, timeout: float = 2.0, password: str = ""
) -> List[Dict[str, object]]:
    """回傳所有候選主機的探測結果（doctor 用）。"""
    targets = list(hosts) if hosts else candidate_hosts()
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda h: probe_host(h, timeout, password), targets))
