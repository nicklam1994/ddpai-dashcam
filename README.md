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
git clone https://github.com/nicklam1994/ddpai-mini3s.git
cd ddpai-mini3s
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
ddpai serve                       # http://127.0.0.1:8766
ddpai serve --bind 0.0.0.0        # 讓同網段其他裝置也能開
```

不用 build、不用 node —— 單一 HTML 檔加原生 JS。功能：

- 連線狀態、設備資訊、SD 卡用量條
- 錄影／事件清單，可直接在瀏覽器播放（後端做 Range 轉發，能拖時間軸）
- 一鍵下載到電腦
- 診斷頁（等同 `ddpai doctor`）
- 可手動指定相機 IP 並重新連線

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
src/ddpai_mini3s/
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

- **Mini3S 無後鏡、無 GPS**：`--rear`、GPS 相關命令會回空清單，不是壞掉。
- **下載速度受限於相機 AP**：實測值通常在 1–3 MB/s；`super download` 開關已實作（批次下載會自動開）。
- **播放 API 各家不同**：網頁版只做「直連檔案 URL + Range 轉發」。若某韌體要求先開 `API_EnterPlaybackMode`，播放會 404，但下載不受影響。
- **未實作雲端登入**：本專案只走本機 Wi-Fi，完全不碰 DDPAI 雲端帳號。

## 授權

MIT。上游 `visiondash` 為 MIT；`ddpai-api` 未附授權檔，本專案只重寫其 API 知識，未複製其程式碼。
