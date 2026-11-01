#!/usr/bin/env python3
"""掃描相機上的抓拍照片（暴力枚舉檔名）。

背景：這款韌體（Mini3 Pro v7.1.0.48）**沒有照片清單 API** —— 照片以
`G_<YYYYMMDDHHMMSS>_<序號>_<時長>_X.jpg` 直接掛在根目錄。官方 App 是從
mailbox 的 `MSG_EventOccured` 拿到檔名才知道有新照片，所以離線抓拍的照片
只能靠時間窗 + 序號枚舉找回來。

用法：
    python scripts/scan_photos.py 130630 130700            # HHMMSS 區間
    python scripts/scan_photos.py 130630 130700 --suffix X --dur 0000
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import requests

HOST = "193.168.0.1"
DATE = datetime.now().strftime("%Y%m%d")
HEADERS = {"doi-lel": "1", "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 13)"}


_local = __import__("threading").local()


def _thread_session() -> requests.Session:
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        _local.session = session
    return session


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def exists(name: str) -> tuple:
    """用 Range 0-0 探測（相機不支援 HEAD）。回傳 (name, size)。"""
    session = _thread_session()
    url = f"http://{HOST}/{name}"
    try:
        resp = session.get(url, headers={**HEADERS, "Range": "bytes=0-0"},
                           stream=True, timeout=5)
        try:
            if resp.status_code in (200, 206):
                total = None
                cr = resp.headers.get("Content-Range", "")
                if "/" in cr:
                    total = cr.rsplit("/", 1)[-1]
                elif resp.headers.get("Content-Length"):
                    total = resp.headers["Content-Length"]
                return (name, total)
        finally:
            resp.close()
    except requests.RequestException:
        pass
    return (name, None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("start", help="開始時間 HHMMSS（相機時鐘）")
    ap.add_argument("end", help="結束時間 HHMMSS")
    ap.add_argument("--date", default=DATE, help=f"日期 YYYYMMDD（預設 {DATE}）")
    ap.add_argument("--suffix", default="X", help="檔名結尾字母（預設 X）")
    ap.add_argument("--dur", default="0000", help="時長欄位（預設 0000）")
    ap.add_argument("--seq-max", type=int, default=1000)
    ap.add_argument("--threads", type=int, default=24)
    ap.add_argument("--download", action="store_true", help="找到就下載")
    ap.add_argument("-o", "--output", default="photos")
    args = ap.parse_args()

    def to_secs(hhmmss: str) -> int:
        return int(hhmmss[0:2]) * 3600 + int(hhmmss[2:4]) * 60 + int(hhmmss[4:6])

    start, end = to_secs(args.start), to_secs(args.end)
    if end <= start:
        print("✗ 結束時間必須大於開始")
        return 2

    stamps = []
    for sec in range(start, end + 1):
        stamps.append(f"{sec // 3600:02d}{sec % 3600 // 60:02d}{sec % 60:02d}")

    total = len(stamps) * args.seq_max
    print(f"掃描 {args.date} {args.start}–{args.end}（{len(stamps)} 秒 × {args.seq_max}）= {total} 個候選")
    print(f"執行緒 {args.threads}，這需要一點時間…")

    hits = []
    done = 0
    t0 = time.monotonic()
    chunk = max(1, args.threads * 8)

    def worker(batch):
        session = _session()
        return [exists(session, n) for n in batch]

    candidates = [f"{args.date}{stamp}_{seq:03d}_{args.dur}_{args.suffix}.jpg"
                  for stamp in stamps for seq in range(args.seq_max)]

    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        for result in pool.map(exists, candidates, chunksize=chunk):
            name, size = result
            done += 1
            if size is not None:
                hits.append((name, size))
                print(f"  ★ 找到 {name}  ({size} bytes)")
            if done % 5000 == 0:
                rate = done / max(time.monotonic() - t0, 0.001)
                print(f"    …{done}/{total}  ({rate:.0f}/s)")

    elapsed = time.monotonic() - t0
    print(f"\n完成：{done} 個請求，{elapsed:.1f} 秒，命中 {len(hits)} 張")
    for name, _ in hits:
        print(f"  http://{HOST}/{name}")

    if args.download and hits:
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        session = _session()
        for name, _ in hits:
            resp = session.get(f"http://{HOST}/{name}", headers=HEADERS, timeout=20)
            if resp.status_code == 200:
                (out / name).write_bytes(resp.content)
                print(f"  ↓ {out / name}")
    return 0 if hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
