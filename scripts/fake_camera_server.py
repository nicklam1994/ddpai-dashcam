"""在固定埠開一台假相機，用來在沒有實機的情況下試 CLI / 網頁。

    python scripts/fake_camera_server.py --port 8088 --mode plain

然後：
    DDPAI_HOST=127.0.0.1:8088 ddpai info
    DDPAI_HOST=127.0.0.1:8088 ddpai serve --port 8766
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from fake_camera import FakeCamera  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="啟動模擬 DDPAI 相機")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--mode", default="plain",
                        choices=["plain", "doilel", "session", "htmlwall"])
    args = parser.parse_args()

    camera = FakeCamera(mode=args.mode, port=args.port).start()
    print(f"模擬相機已啟動： http://{camera.host}  (mode={args.mode})", flush=True)
    print("按 Ctrl+C 結束。", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n關閉中…")
    finally:
        camera.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
