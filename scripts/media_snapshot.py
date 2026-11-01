#!/usr/bin/env python3
"""相機媒體指紋：抓拍照前後的差異，用來驗證「相機到底有沒有存照片」。

用法：
    python scripts/media_snapshot.py baseline     # 存基準
    python scripts/media_snapshot.py diff         # 與基準比對

指紋內容：全部檔案名 + 副檔名分佈 + 檔案數（相機自報 num 與實際筆數）+ SD 容量。
只看容量不夠 —— 循環錄影每分鐘會蓋掉一段 140MB，雜訊遠大於一張照片，
所以主要靠「檔名集合差異」判斷。
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ddpai_dashcam import Camera  # noqa: E402
from ddpai_dashcam.models import human_size  # noqa: E402
from ddpai_dashcam.parser import find_list  # noqa: E402

STATE = Path(__file__).resolve().parent.parent / ".run" / "media-baseline.json"
HOST = "193.168.0.1"


def fingerprint(host: str = HOST) -> dict:
    camera = Camera(host=host, timeout=10, auto_discover=False)
    try:
        camera.connect()

        raw = camera.transport.request("APP_PlaybackListReq")
        data = raw.get("data") if isinstance(raw, dict) else None
        reported = None
        if isinstance(data, str):
            try:
                reported = json.loads(data).get("num")
            except (json.JSONDecodeError, AttributeError):
                reported = None

        rows = find_list(raw)
        names = sorted(str(row.get("name", "")) for row in rows if row.get("name"))

        events_raw = camera.transport.request("APP_EventListReq")
        event_rows = find_list(events_raw, keys=("event", "events", "file", "list", "data"))
        event_names = sorted(
            str(row.get("bvideoname") or row.get("name") or "")
            for row in event_rows
        )

        storage = camera.storage()
        info = camera.info()

        return {
            "taken_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "host": host,
            "model": info.model,
            "firmware": info.version,
            "num_reported": reported,
            "file_count": len(names),
            "files": names,
            "extensions": dict(Counter(n.rsplit(".", 1)[-1].lower() for n in names if "." in n)),
            "event_count": len(event_names),
            "events": event_names,
            "storage_total": storage.total_bytes if storage else None,
            "storage_free": storage.free_bytes if storage else None,
        }
    finally:
        camera.close()


def print_fp(fp: dict) -> None:
    print(f"  時間       : {fp['taken_at']}")
    print(f"  機型/韌體  : {fp['model']} / {fp['firmware']}")
    print(f"  檔案數     : {fp['file_count']}（相機自報 num={fp['num_reported']}）")
    print(f"  副檔名     : {fp['extensions'] or '無'}")
    print(f"  事件數     : {fp['event_count']}")
    print(f"  SD 容量    : 總 {human_size(fp['storage_total'])} / 剩 {human_size(fp['storage_free'])}")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "diff"
    STATE.parent.mkdir(parents=True, exist_ok=True)

    print(f"→ 連 {HOST} …")
    current = fingerprint()
    print("\n【當前狀態】")
    print_fp(current)

    if mode == "baseline":
        STATE.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✓ 基準已存到 {STATE}")
        return 0

    if not STATE.exists():
        print(f"\n✗ 找不到基準 {STATE}，先跑 baseline")
        return 2

    base = json.loads(STATE.read_text(encoding="utf-8"))
    print("\n【基準狀態】")
    print_fp(base)

    before, after = set(base["files"]), set(current["files"])
    added = sorted(after - before)
    removed = sorted(before - after)

    print("\n【差異】")
    print(f"  新增 {len(added)} 個檔案")
    for name in added[:40]:
        print(f"    + {name}")
    if len(added) > 40:
        print(f"    … 另有 {len(added) - 40} 個")
    print(f"  消失 {len(removed)} 個檔案")
    for name in removed[:10]:
        print(f"    - {name}")
    if len(removed) > 10:
        print(f"    … 另有 {len(removed) - 10} 個（循環錄影覆蓋屬正常）")

    new_exts = Counter(n.rsplit(".", 1)[-1].lower() for n in added if "." in n)
    print(f"  新增檔案副檔名: {dict(new_exts) or '無'}")

    if current["storage_free"] is not None and base["storage_free"] is not None:
        delta = current["storage_free"] - base["storage_free"]
        print(f"  SD 剩餘變化    : {delta:+d} bytes ({human_size(abs(delta))} {'增加' if delta > 0 else '減少'})")

    print("\n【結論】")
    if new_exts.get("jpg") or new_exts.get("jpeg"):
        print("  ✓ 相機有寫入照片，抓拍存在相機上")
    elif added:
        print("  △ 有新檔案，但全是影片（這段時間的循環錄影），沒有照片")
    else:
        print("  ✗ 沒有任何新檔案 —— 抓拍沒有寫進相機儲存")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
