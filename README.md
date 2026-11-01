# DDPAI Mini3S 工具組

直接從電腦透過 Wi-Fi 操作 DDPAI 行車紀錄器：不需要官方 App、不需要登入雲端、不需要拔記憶卡。

整合兩個逆向工程專案的做法，並補上一個關鍵缺口：**不同韌體的握手方式不一致**。

| 上游專案 | 做法 | 本專案如何吸收 |
| --- | --- | --- |
| [awexandrr/ddpai-api](https://github.com/awexandrr/ddpai-api) | `POST` + 先取 `sessionid` | 列為一組策略 |
| [pwrtux/visiondash](https://github.com/pwrtux/visiondash) | `GET` + `doi-lel: 1` header | 列為優先策略 |
| 本專案 | 9 組「路徑 × 方法 × header」策略自動探測 | 首次連線自動找出可用組合 |

目標機型是 **Mini3S**（單前鏡、較舊韌體），但因為走的是自動探測，N5 / Z40 / Mini 系列應該也能用。

---

## 快速開始

```bash
git clone https://github.com/nicklam1994/ddpai-dashcam.git
cd ddpai-dashcam
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e ".[dev,web]"
source .venv/bin/activate
```

### 1. 電腦連上相機的 Wi-Fi

相機開機後會開一個熱點，名稱通常是 `DDPAI_Mini3S_xxxx`。

- 連上後電腦會**失去互聯網**，這是正常的（相機熱點沒有對外網路）。
- WSL 使用者：Wi-Fi 是連在 Windows 側，WSL2 透過 NAT 一樣能連到相機，不用額外設定。

### 2. 診斷連線（開機後第一步）

```bash
ddpai doctor
```

它會做四件事：

1. 列出本機路由閘道（WSL 會額外去問 Windows 的 Wi-Fi 閘道）
2. 並行探測候選 IP（`193.168.0.1`、`192.168.1.254`、`192.168.100.1` …）
3. 命中後列出每個 API 命令是否可用
4. `--matrix` 會印出完整策略矩陣，看得出這台韌體吃哪一組 header

```bash
ddpai doctor --matrix
```

### 3. 日常操作

```bash
ddpai info                 # 型號、韌體、序號、SD 卡容量
ddpai list                 # 錄影清單（前鏡）
ddpai list --rear          # 後鏡（Mini3S 沒有後鏡，會是空清單）
ddpai events               # 碰撞／事件錄影
ddpai download --latest 3  # 下載最近 3 段到 ./downloads
ddpai download /DCIM/100DDPAI/FILE_xxx.MP4 -o ~/Videos
ddpai snapshot -o now.jpg  # 抓一張即時快照
ddpai rtsp                 # 印出 RTSP 直播網址（給 VLC）
ddpai raw APP_PlaybackListReq   # 直接送原始 cmd，看韌體回什麼
ddpai reboot -y            # 重啟相機
```

全域選項：`--host 193.168.0.1`、`--password`、`--json`、`--debug`、`--timeout`。

### 4. 網頁儀表板

```bash
./scripts/run.sh real             # 真機模式 → http://127.0.0.1:8766
./scripts/run.sh demo             # 示範模式（模擬相機，不需實機）
./scripts/run.sh stop             # 停止
./scripts/run.sh status           # 看狀態
```

也可以直接 `ddpai serve`。不用 build、不用 node —— 單一 HTML 檔加原生 JS。功能：

- 連線狀態、設備資訊、SD 卡用量條
- **即時畫面**：MJPEG 直播（瀏覽器直接看，不用 VLC）、一鍵拍照、錄 10 秒短片
- **相冊**（四個來源可切換）：
  - `我的抓拍` — 本機拍照／錄影，自動存到 `photos/`、`recordings/`，可放大檢視、下載、刪除
  - `相機照片` — 相機內建照片（**會自動偵測韌體支不支援**；本機實測不支援，見下節）
  - `相機事件` — 事件／碰撞錄影，可直接播放與下載
  - `相機錄影` — 循環錄影清單，可直接播放與下載
- 一鍵下載到電腦
- 診斷頁（等同 `ddpai doctor`）
- 可手動指定相機 IP 並重新連線

相關 API：

| 端點 | 用途 |
| --- | --- |
| `GET /api/live.mjpg` | MJPEG 直播串流（`<img src>` 即可） |
| `GET /api/snapshot.jpg` | 抓一張即時照片（回 JPEG，並存進相冊；`?save=false` 只回不存） |
| `POST /api/record` `{"seconds":10}` | 錄短片並存到 `recordings/` |
| `GET /api/captures` | 本機相冊：照片 + 錄影，時間倒序 |
| `GET /api/camera-album` | 相機端相簿：照片能力、事件、錄影一次給齊 |
| `GET /api/photos` · `GET/DELETE /api/photos/<檔名>` | 本機照片列表／檢視／刪除 |
| `GET/DELETE /api/records/<檔名>` | 本機錄影片段檢視／刪除 |
| `GET /api/camera-photo/<檔名>` | 代理相機上的照片（給支援照片 API 的韌體） |
| `GET /api/media/<檔名>` | 播放相機上的錄影（支援 Range） |
| `POST /api/download` `{"names":[...]}` | 把相機檔案下載到本機 `downloads/` |

相冊位置可用 `DDPAI_PHOTO_DIR` / `DDPAI_RECORD_DIR` 覆寫，預設在專案目錄下（已加進 `.gitignore`）。
直播需要 `ffmpeg`（`sudo apt install ffmpeg`）；沒有 ffmpeg 時其他功能照常。

---

## 實機驗證結果（DDPAI Mini3 Pro / v7.1.0.48）

以 `nicklam1994` 的實機（自稱 Mini3S，實際回報 **Mini3Pro_V4**）逐項實測：

| 項目 | 結果 |
| --- | --- |
| 相機 IP | `193.168.0.1`（在 Windows Wi-Fi 側，WSL 要靠 powershell 才看得到） |
| 握手策略 | `vcam-get-doilel` —— 需要 `doi-lel: 1`，與 visiondash 一致 |
| 裝置資訊 | `DDPai Mini3Pro_V4` / `v7.1.0.48` / MAC `b8:4d:43:…`，序號欄位為空 |
| 儲存空間 | 28.8 GB，已用 86–89%（來源 `API_GetBaseInfo`；`API_GetStorageInfo` 回不支援） |
| 錄影清單 | 160+ 段 1080p 60 秒循環錄影，`APP_PlaybackListReq` 正常 |
| 下載 | 140 MB / 段實測 45 秒（約 3.1 MB/s） |
| 直播 | `rtsp://193.168.0.1:554/liveRTSP/av4` → 1280×720 H.264 + pcm_alaw |
| 瀏覽器播放 | 正常，可拖時間軸，畫質 2560×1440 |
| 抓拍照片（SD 卡） | **存在**，需先登入才能觸發 —— 詳見下節 |

### 抓拍照片到底在哪（APK 反編譯 + 實機結論）

原以為相機不存抓拍照片。反編譯官方 APK `vv8.2.7.1021` 後發現是**我們用錯 API**：

1. **錯誤碼我之前判錯了**。APK 的 `ErrCode.java` 給出正確含義：
   - `1996488773` = `LOGIN_SESSION_ERROR`（**不是**「不支援」）
   - `1996488764` = `WEB_E_ERR_WRONG_CGI_REQUEST`（參數／格式錯）
   - `1996488762` = `WEB_E_ERR_DECODE_JSON`

2. **抓拍必須先登入**。`API_CameraCapture` 一直回 `LOGIN_SESSION_ERROR` 就是這個原因。
   APK 的登入序列是 `API_RequestSessionID` → `API_RequestCertificate`，而且 certificate
   要 **POST + JSON body**（用 GET 加 query 一定失敗）：

   ```json
   {"user":"admin","password":"admin","level":0,"uid":"<any>"}
   ```

   預設帳密 `admin`/`admin` 寫死在 `Device.java` 的 field initializer。

3. **照片真的存在，而且真的會被刪**。登入後 `API_CameraCapture` 回 `errcode: 0`，
   接著 mailbox 會吐出：

   ```
   MSG_EventOccured  {"imgname":"G_20261101131646_050_0000_X.jpg","size":"3072"}
   MSG_DeleteEvent   {"file":["G_20261031035114_009_0015_L.jpg"]}
   ```

   照片以 `G_<YYYYMMDDHHMMSS>_<序號>_<時長>_X.jpg` 掛在相機**根目錄**，
   `http://<ip>/G_....jpg` 直接下載得到（實測 2560×1440、510 KB、帶 EXIF）。
   相機用 ring buffer 管理，滿了就發 `MSG_DeleteEvent` 刪最舊的 ——
   所以「抓拍照片會消失」是真的，但不一定是手機 App 刪的，相機自己也會刪。

4. **沒有任何「列出所有抓拍照片」的 API**。這點也確認了：
   - 24 支照片清單 cmd 全部 `WRONG_CGI_REQUEST`
   - 列表類 cmd 加 `property=photo` / `type=photo` 等參數一樣不行（POST JSON body 也試過）
   - `DEV_PHOTO_LIST_REQ`（`Config.cgi?action=dir&property=photo`）只用在 NPD 機型，
     本機是自家 `bz` 協議，不走它
   - 播放清單 166 筆全是 `.mp4`；`API_GpsFileListReq` 只有 `.git` / `.gpx`

   官方 App 也是靠 mailbox 的 `MSG_EventOccured` 才知道有新照片（拿到檔名才下載），
   所以**離線時按遙控抓拍的照片，只能靠時間窗 + 序號暴力枚舉找回**（12 req/s，
   一小時約 4 萬個候選，實務上不划算）。

因此本工具的定位是：**由工具發起抓拍**（`POST /api/camera-capture` 或 `ddpai capture`），
拿到檔名後同時存在相機與本機相冊；相冊的「相機照片」分頁另外顯示可列舉的事件縮圖。

實測踩到並已修正的四個韌體特性：

1. **`size` 單位是 KB 不是 bytes**：清單回 `143360`，實際檔案 `146,800,640` bytes。程式用「Range 總長度 ÷ 清單值」自動判定（比值 ≈1024 → KB），判定一次後快取。
2. **不支援 `HEAD`**：回空 body。改用 `Range: bytes=0-0` 讀 `Content-Range` 的總長度。
3. **時間戳是「本地時間當 UTC」**：OSD 顯示 12:47 而 epoch 也是 12:47 UTC，若再套時區就會多出 8 小時。改為優先用檔名 `YYYYMMDDHHMMSS` 作時間（最權威），epoch 走 `utcfromtimestamp`。
4. **同時只接受一個 RTSP session**：上一個 ffmpeg 沒收乾淨時，新連線會回 `DESCRIBE failed: 404 File Not Found`（看起來像路徑錯）。已加 ffmpeg 進程登記 + 失敗時清理重試。另外音頻是 `pcm_alaw`，mp4 容器不收，錄影要轉 aac（失敗則丟音頻）。

---

## 為什麼需要「多策略探測」

DDPAI 各世代韌體對同一支 API 的要求不同：

| 策略 id | 請求方式 | 出現的韌體 |
| --- | --- | --- |
| `vcam-get-doilel` | `GET /vcam/cmd.cgi?cmd=X` + `doi-lel: 1` | visiondash 作者實測（N5 Dual） |
| `vcam-get-session` | `GET` + `sessionid` header | 需先 `API_RequestSessionID` |
| `vcam-post-session` | `POST` + form body + `sessionid` | ddpai-api 作者實測 |
| `cgi-get-*` | `/cmd.cgi`（沒有 `/vcam` 前綴） | 較舊的世代 |
| `vcam-get-plain` | `GET` 不帶任何特殊 header | 最寬鬆的韌體 |

`ddpai doctor --matrix` 會把 9 組策略 × 3 支命令的結果全列出來。第一次接上新機型時，看這張表最省時間。

若全部失敗，代表這台需要密碼或用了未收錄的路徑 —— 用 `DDPAI_PASSWORD=xxxx` 再試一次；仍失敗就用 `ddpai raw <cmd>` 對照官方 App 的請求（抓包）找出差異。

---

## 架構

```
src/ddpai_dashcam/
├── transport.py    策略矩陣、session 握手、單次請求、串流連線
├── discovery.py    候選 IP、WSL→Windows Wi-Fi 閘道、並行探測
├── parser.py       剝殼：data/file/list 嵌套、JSON-in-string、HTML 攔截頁
├── models.py       CameraFile / DeviceInfo / StorageInfo（大小寫不敏感取鍵）
├── client.py       Camera 高階 API：info / files / download / snapshot / doctor
├── cli.py          ddpai 命令列
└── web/
    ├── app.py      FastAPI：/api/*、Range 轉發、下載代理
    └── static/index.html   單頁儀表板（原生 JS）
```

設計取捨：

- **同步 `requests`**：這些請求都是「一次一支 cmd」，異步帶來的複雜度換不到吞吐量；FastAPI 的同步 handler 本來就跑在 threadpool。
- **不用前端框架**：整包沒有 build step，clone 下來裝完 Python 依賴就能跑，離線環境（連著相機沒有互聯網）也不會壞。
- **失敗不拋原始例外**：所有錯誤都翻成中文可讀訊息，因為使用者當下是「在車上連不到相機」，需要的是下一步動作而不是 traceback。

---

## 離線測試

沒有實機也能跑完整流程（假相機模擬三種韌體行為）：

```bash
.venv/bin/python -m pytest -q          # 32 passed

# 開一台假相機（plain / doilel / session / htmlwall）
.venv/bin/python scripts/fake_camera_server.py --port 8088 --mode session

DDPAI_HOST=127.0.0.1:8088 ddpai doctor
DDPAI_HOST=127.0.0.1:8088 ddpai serve --port 8766
```

`htmlwall` 模式模擬「一律回登入頁」，用來驗證錯誤訊息品質。

---

## 真機開機測試清單

第一次接 Mini3S，照這個順序做，出問題時比較好定位：

1. 相機開機，等指示燈穩定（約 20 秒）
2. 電腦 Wi-Fi 連上 `DDPAI_Mini3S_xxxx`
3. Windows 端 `ipconfig` 看無線網卡閘道 → 記下來（預期 `193.168.0.1`）
4. `ddpai doctor` → 應該看到 `✓ <IP> <策略名>`
5. `ddpai info` → 型號／韌體／SD 卡容量
6. `ddpai list` → 錄影清單（Mini3S 是前鏡）
7. `ddpai download --latest 1` → 下載一段，用播放器確認可播
8. `ddpai rtsp` → VLC 開直播
9. `ddpai serve` → 瀏覽器開 `http://127.0.0.1:8766`，試播放與下載

第 4 步失敗時，把 `ddpai doctor --matrix --json` 的輸出貼出來，就能看出是「連不上」、「被 403 擋」還是「回了 HTML 登入頁」。

---

## 已知限制

- **Mini3 系列無後鏡、無 GPS**：`--rear`、GPS 相關命令會回空清單，不是壞掉。
- **下載速度受限於相機 AP**：實測約 3.1 MB/s（140 MB / 45 秒）；`super download` 開關已實作（批次下載會自動開）。
- **相機只允許一個 RTSP session**：直播與錄影會互斥；官方 App 同時連著時，本機直播會失敗。程式已會清理殘留 ffmpeg 並重試一次。
- **同時只能有一個客戶端連相機**：手機 App 連著時電腦可能連不上，先關掉 App。
- **未實作雲端登入**：本專案只走本機 Wi-Fi，完全不碰 DDPAI 雲端帳號。

## 授權

MIT。上游 `visiondash` 為 MIT；`ddpai-api` 未附授權檔，本專案只重寫其 API 知識，未複製其程式碼。
