"""命令列介面：`ddpai <子命令>`。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

from . import __version__, discovery
from .client import Camera, CameraError, CameraUnreachable
from .models import CameraFile, human_duration, human_size
from .transport import STRATEGIES

LOG = logging.getLogger("ddpai")


# ── 輸出工具 ───────────────────────────────────────────────────
class Palette:
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"
    RESET = "\033[0m"


def paint(text: str, colour: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"{colour}{text}{Palette.RESET}"


def header(title: str) -> None:
    print()
    print(paint(f"── {title} " + "─" * max(0, 46 - len(title)), Palette.BOLD))


def kv(key: str, value: Any, indent: int = 2) -> None:
    print(f"{' ' * indent}{paint(key + ':', Palette.DIM)} {value}")


def bar(done: int, total: Optional[int], width: int = 34) -> str:
    if not total:
        return f"  {human_size(done)}"
    ratio = min(done / total, 1.0)
    filled = int(ratio * width)
    return (
        f"  [{'█' * filled}{'·' * (width - filled)}] "
        f"{ratio * 100:5.1f}%  {human_size(done)} / {human_size(total)}"
    )


def progress_printer() -> Any:
    state = {"last": 0.0}

    def printer(done: int, total: Optional[int]) -> None:
        now = time.monotonic()
        finished = total is not None and done >= total
        if not finished and now - state["last"] < 0.2:
            return
        state["last"] = now
        sys.stdout.write("\r" + bar(done, total))
        sys.stdout.flush()
        if finished:
            sys.stdout.write("\n")

    return printer


# ── 子命令 ─────────────────────────────────────────────────────
def make_camera(args: argparse.Namespace) -> Camera:
    try:
        return Camera(
            host=args.host,
            password=args.password or "",
            timeout=args.timeout,
            auto_discover=not args.no_discover,
        )
    except CameraUnreachable as exc:
        print(paint(f"✗ {exc}", Palette.RED))
        print(paint("  提示：先連上相機 Wi-Fi（DDPAI_XXXX），再跑 `ddpai doctor` 看診斷。", Palette.DIM))
        raise SystemExit(2) from exc


def cmd_discover(args: argparse.Namespace) -> int:
    header("掃描相機")
    targets = discovery.candidate_hosts(args.extra_host or [])
    print(paint(f"  候選 {len(targets)} 個主機，並行探測中…", Palette.DIM))
    results = discovery.discover_all(targets, timeout=args.timeout, password=args.password or "")
    found = [r for r in results if r["api"]]
    for row in results:
        if row["api"]:
            print(f"  {paint('✓', Palette.GREEN)} {row['host']:<16} 策略={row['strategy']}")
        elif row["tcp"]:
            print(f"  {paint('△', Palette.YELLOW)} {row['host']:<16} 通但非 DDPAI API（{row['error']}）")
        else:
            print(f"  {paint('·', Palette.DIM)} {row['host']:<16} 不通")
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    if not found:
        print()
        print(paint("✗ 找不到相機。", Palette.RED))
        print(paint("  1) 電腦 Wi-Fi 是否連上 DDPAI_XXXX？", Palette.DIM))
        print(paint("  2) WSL 使用者請確認 Windows 側已連線，並可用 --extra-host 手動指定 IP。", Palette.DIM))
        return 1
    print()
    print(f"  相機位址：{paint(found[0]['host'], Palette.GREEN)}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    header("連線診斷")
    targets = discovery.candidate_hosts(args.extra_host or [])
    # 明確指定或環境變數給的位址要排第一
    for preferred in (args.host, os.environ.get("DDPAI_HOST")):
        if preferred:
            targets = [preferred] + [t for t in targets if t != preferred]
    print(paint(f"  介面檢查：WSL={discovery.is_wsl()}  路由閘道={discovery.route_gateways() or '無'}", Palette.DIM))
    if discovery.is_wsl():
        wifi = discovery.windows_wifi_gateways()
        print(paint(f"  Windows Wi-Fi 閘道：{wifi or '讀不到（powershell 不可用）'}", Palette.DIM))

    results = discovery.discover_all(targets[:8], timeout=min(args.timeout, 3.0), password=args.password or "")
    live = [r["host"] for r in results if r["api"]]
    for row in results:
        mark = paint("✓", Palette.GREEN) if row["api"] else paint("✗", Palette.DIM)
        print(f"  {mark} {row['host']:<16} {row['strategy'] or row['error']}")
    if not live:
        print()
        print(paint("✗ 沒有任何主機能回應 DDPAI API。", Palette.RED))
        print(paint("  檢查清單：", Palette.DIM))
        print(paint("   • Wi-Fi 必須連在相機的 AP（不是家用路由器）", Palette.DIM))
        print(paint("   • 相機有開機、指示燈正常", Palette.DIM))
        print(paint("   • Windows 端 `ipconfig` 看無線網卡閘道是否為 193.168.0.1", Palette.DIM))
        return 1

    cam = Camera(host=live[0], password=args.password or "", timeout=args.timeout)
    report = cam.doctor(full_matrix=args.matrix)
    cam.close()

    header("API 探測結果")
    for cmd, info in report["commands"].items():
        if info.get("ok"):
            print(f"  {paint('✓', Palette.GREEN)} {cmd:<26} errcode={info.get('errcode')} rows={info.get('rows')}")
        else:
            print(f"  {paint('✗', Palette.RED)} {cmd:<26} {info.get('error')}")

    if args.matrix and report.get("matrix"):
        header("策略矩陣")
        for row in report["matrix"]:
            mark = paint("✓", Palette.GREEN) if row["ok"] else paint("·", Palette.DIM)
            print(f"  {mark} {row['cmd']:<24} {row['strategy']:<24} {row['http_status']} {row['error']}")

    if report.get("notes"):
        header("備註")
        for note in report["notes"]:
            print(f"  • {note}")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    with make_camera(args) as cam:
        info = cam.info()
        storage = cam.storage()
    if args.json:
        print(json.dumps({"info": info.to_dict(), "storage": storage.to_dict() if storage else None},
                         ensure_ascii=False, indent=2))
        return 0
    header("設備資訊")
    kv("位址", cam.host)
    kv("型號", info.model or "—")
    kv("名稱", info.nickname or "—")
    kv("韌體", info.version or "—")
    kv("序號", info.sn or "—")
    kv("MAC", info.mac or "—")
    if storage:
        header("儲存空間")
        kv("總容量", human_size(storage.total_bytes))
        kv("可用", human_size(storage.free_bytes))
        kv("已用", f"{human_size(storage.used_bytes)} ({storage.used_percent}%)")
        kv("卡片", storage.brand or "—")
        kv("來源", storage.source)
    print()
    return 0


def _print_files(files: List[CameraFile], title: str) -> None:
    header(title)
    if not files:
        print(paint("  （沒有檔案）", Palette.DIM))
        return
    print(paint(f"  {'#':>3}  {'檔名':<38} {'開始時間':<20} {'長度':<8} {'大小':>9}", Palette.BOLD))
    for index, item in enumerate(files, 1):
        start = item.start.strftime("%Y-%m-%d %H:%M:%S") if item.start else "—"
        print(
            f"  {index:>3}  {item.basename[:38]:<38} {start:<20} "
            f"{human_duration(item.duration):<8} {human_size(item.size):>9}"
        )
    print()
    print(paint(f"  共 {len(files)} 個檔案", Palette.DIM))


def cmd_list(args: argparse.Namespace) -> int:
    with make_camera(args) as cam:
        files = cam.files(rear=args.rear)
    if args.json:
        print(json.dumps([f.to_dict() for f in files], ensure_ascii=False, indent=2))
        return 0
    _print_files(files, f"錄影清單（{'後鏡' if args.rear else '前鏡'}）")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    with make_camera(args) as cam:
        events = cam.events(rear=args.rear)
    if args.json:
        print(json.dumps([e.to_dict() for e in events], ensure_ascii=False, indent=2))
        return 0
    _print_files(events, f"事件錄影（{'後鏡' if args.rear else '前鏡'}）")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    with make_camera(args) as cam:
        if args.latest:
            files = sorted(cam.files(rear=args.rear),
                           key=lambda f: f.start.timestamp() if f.start else 0, reverse=True)
            targets = files[: args.latest]
            if not targets:
                print(paint("✗ 相機上沒有檔案", Palette.RED))
                return 1
            header(f"下載最近 {len(targets)} 段錄影")
            if args.dry_run:
                for item in targets:
                    print(f"  {item.basename}  {human_size(item.size)}")
                return 0
            cam.set_super_download(True)
            try:
                for index, item in enumerate(targets, 1):
                    print(f"  [{index}/{len(targets)}] {item.basename}")
                    path = cam.download(item.name, args.output, overwrite=args.overwrite,
                                        progress=progress_printer())
                    print(paint(f"  ✓ {path}", Palette.GREEN))
            finally:
                cam.set_super_download(False)
            return 0

        if not args.name:
            print(paint("✗ 需要檔名，或用 --latest N", Palette.RED))
            return 2
        if args.dry_run:
            print(f"  {args.name}")
            return 0
        path = cam.download(args.name, args.output, overwrite=args.overwrite,
                            progress=progress_printer())
    print(paint(f"✓ 已存到 {path}", Palette.GREEN))
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    with make_camera(args) as cam:
        path = cam.snapshot(args.output)
    if path:
        print(paint(f"✓ 已存到 {path}", Palette.GREEN))
        return 0
    print(paint("⚠ 相機沒有回傳快照路徑（部分韌體只存到 SD 卡）", Palette.YELLOW))
    return 1


def cmd_rtsp(args: argparse.Namespace) -> int:
    with make_camera(args) as cam:
        url = cam.rtsp_url(rear=args.rear)
    header("即時影像")
    print(f"  {paint(url, Palette.CYAN)}")
    print()
    print(paint("  播放：", Palette.DIM))
    print(f"    vlc {url}")
    print(f"    ffplay -rtsp_transport tcp {url}")
    print(paint("  錄存：", Palette.DIM))
    print(f"    ffmpeg -rtsp_transport tcp -i {url} -c copy out.mp4")
    return 0


def cmd_reboot(args: argparse.Namespace) -> int:
    if not args.yes:
        answer = input("  確定要重啟相機？[y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print(paint("  已取消", Palette.DIM))
            return 0
    with make_camera(args) as cam:
        ok = cam.reboot()
    print(paint("✓ 重啟指令已送出（Wi-Fi 會斷線數十秒）" if ok else "✗ 重啟失敗", Palette.GREEN if ok else Palette.RED))
    return 0 if ok else 1


def cmd_raw(args: argparse.Namespace) -> int:
    params = {}
    for pair in args.param or []:
        key, _, value = pair.partition("=")
        params[key] = value
    with make_camera(args) as cam:
        payload = cam.raw(args.cmd, params or None)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    with make_camera(args) as cam:
        if not cam.login(args.user, args.password or "admin"):
            print(paint("✗ 登入相機失敗（API_RequestCertificate 未通過）", Palette.RED))
            return 1
        photo = cam.capture()
        if photo is None:
            print(paint("✗ 相機沒有回報新照片（mailbox 沒有 MSG_EventOccured）", Palette.RED))
            return 1
        header("抓拍")
        kv("相機檔名", photo.name)
        kv("相機路徑", cam.photo_url(photo.name))
        if args.no_download:
            return 0
        path = cam.download_photo(photo.name, args.output, progress=progress_printer())
    print(paint(f"✓ 已存到 {path}", Palette.GREEN))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print(paint("✗ 需要 Web 依賴：pip install 'ddpai-dashcam[web]'", Palette.RED))
        return 2
    if args.host_ip:
        os.environ["DDPAI_HOST"] = args.host_ip
    if args.password:
        os.environ["DDPAI_PASSWORD"] = args.password
    print(paint(f"  Web 儀表板： http://{args.bind}:{args.port}", Palette.CYAN))
    uvicorn.run("ddpai_dashcam.web.app:app", host=args.bind, port=args.port,
                reload=args.reload, log_level="info")
    return 0


# ── 參數 ───────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ddpai",
        description="DDPAI 行車紀錄器本機 Wi-Fi 工具（Mini3S 優先）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用流程：\n"
            "  ddpai doctor              # 開機後第一步：確認連線與 API 是否通\n"
            "  ddpai info                # 設備與容量\n"
            "  ddpai list                # 錄影清單\n"
            "  ddpai download --latest 3 # 下載最近 3 段\n"
            "  ddpai serve               # 開網頁儀表板 http://127.0.0.1:8766\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"ddpai-dashcam {__version__}")
    parser.add_argument("--debug", action="store_true", help="顯示 debug log")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", help="相機 IP（預設自動探測）")
    common.add_argument("--password", help="相機密碼（預設讀 DDPAI_PASSWORD）")
    common.add_argument("--timeout", type=float, default=8.0, help="HTTP timeout 秒")
    common.add_argument("--no-discover", action="store_true", help="不做自動探測")
    common.add_argument("--json", action="store_true", help="輸出 JSON")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", parents=[common], help="掃描網段找相機")
    p.add_argument("--extra-host", action="append", help="額外候選 IP")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("doctor", parents=[common], help="連線與 API 診斷")
    p.add_argument("--matrix", action="store_true", help="輸出完整策略矩陣")
    p.add_argument("--extra-host", action="append", help="額外候選 IP")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("info", parents=[common], help="設備資訊與容量")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("storage", parents=[common], help="只看容量")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("list", parents=[common], help="錄影清單")
    p.add_argument("--rear", action="store_true", help="後鏡")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("events", parents=[common], help="事件錄影清單")
    p.add_argument("--rear", action="store_true")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("download", parents=[common], help="下載檔案")
    p.add_argument("name", nargs="?", help="相機上的檔名／路徑")
    p.add_argument("-o", "--output", default="downloads", help="本機目錄")
    p.add_argument("--latest", type=int, help="下載最近 N 段")
    p.add_argument("--rear", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("snapshot", parents=[common], help="抓一張即時快照")
    p.add_argument("-o", "--output", help="輸出路徑")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("rtsp", parents=[common], help="印出 RTSP 即時影像網址")
    p.add_argument("--rear", action="store_true")
    p.set_defaults(func=cmd_rtsp)

    p = sub.add_parser("reboot", parents=[common], help="重啟相機")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_reboot)

    p = sub.add_parser("raw", parents=[common], help="直接送原始 cmd")
    p.add_argument("cmd", help="例如 API_GetBaseInfo")
    p.add_argument("--param", action="append", help="KEY=VALUE")
    p.set_defaults(func=cmd_raw)

    p = sub.add_parser("capture", parents=[common], help="叫相機抓拍一張（存 SD 卡）")
    p.add_argument("-o", "--output", default="photos", help="本機目錄")
    p.add_argument("--user", default="admin", help="相機帳號（預設 admin）")
    p.add_argument("--no-download", action="store_true", help="只抓拍不下載")
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("serve", help="啟動網頁儀表板")
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--host-ip", help="相機 IP")
    p.add_argument("--password")
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve, host=None, json=False)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return int(args.func(args) or 0)
    except CameraError as exc:
        print(paint(f"✗ {exc}", Palette.RED))
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
