# DDPAI 行車紀錄器 — 專案筆記

> 對象：DDPai Mini3Pro_V4 / 韌體 v7.1.0.48（機身標 Mini3S，韌體自報 Mini3Pro_V4）
> 相機 IP：`193.168.0.1`（Windows Wi-Fi 側；WSL 要用 powershell 讀閘道才看得到）
> 最後更新：2026-11-01

---

## 一、API 全貌（實測可用）

### 連線與握手

| 項目 | 值 |
| --- | --- |
| Base path | `/vcam/cmd.cgi?cmd=<CMD>` |
| 必要 header | `doi-lel: 1`（策略 `vcam-get-doilel`） |
| Session | `API_RequestSessionID` → 回 `acSessionId`，之後帶 `sessionid` header |
| 登入 | `POST ?cmd=API_RequestCertificate` + JSON body |
| 登入 body | `{"user":"admin","password":"admin","level":0,"uid":"<any>"}` |
| 預設帳密 | `admin` / `admin`（寫死在 APK `Device.java` 的 field initializer） |

**關鍵**：控制類命令（登入、抓拍、刪除、鎖檔）只認 **POST + JSON body**。同樣的 cmd 用 GET 加 query 參數，一律回 `WEB_E_ERR_WRONG_CGI_REQUEST`。這是整個專案最重要的一條經驗。

### 錯誤碼（來自 APK `ErrCode.java`，不是猜的）

| 十進位 | 常數 | 含義 |
| --- | --- | --- |
| `1996488773` | `LOGIN_SESSION_ERROR` | 未登入／session 失效 → **先登入再重試** |
| `1996488764` | `WEB_E_ERR_WRONG_CGI_REQUEST` | cmd 名或參數格式錯 |
| `1996488762` | `WEB_E_ERR_DECODE_JSON` | 需要 JSON body |
| `1996488765` | `NORMAL_SESSION_TIMEOUT` | session 逾時 |
| `8196` | `DEV_ONLY_SINGLE_USER_CONNECT` | 已有別的客戶端連著 |

一開始把這幾個都當成「不支援」，導致誤判「相機不存抓拍照片」。教訓：**先讀錯誤碼表，再下結論**。

### 讀取類

| 用途 | 命令 | 備註 |
| --- | --- | --- |
| 設備資訊 | `API_GetBaseInfo` | 含密碼、MAC、SD 容量（KB） |
| SD 容量 | `API_GetBaseInfo`（`API_GetStorageInfo` 本機不支援） | `sdcapacity` / `sdspare` 單位 KB |
| 錄影清單 | `APP_PlaybackListReq` | `{"num":N,"file":[...]}`，`size` 單位 **KB** |
| 第一儲存清單 | `APP_FirstStorageFileListReq` | 結果同上 |
| 事件清單 | `APP_EventListReq` | 含 `imgname`（事件縮圖） |
| 停車監控 | `APP_ParkingEventListReq` | 本機回空 |
| 鎖定清單 | `APP_LockedListReq` | — |
| GPS 清單 | `API_GpsFileListReq` | 只有 `.git` / `.gpx` |
| Mailbox | `API_GetMailboxData` | 新檔案／刪除通知；**讀完即清** |
| 分頁狀態 | `API_Get_PageFileListStatus` | `filenum` 比清單多 1（正在寫入那段） |

### 控制類（需登入）

| 用途 | 命令 | Body |
| --- | --- | --- |
| 抓拍 | `API_CameraCapture` | 無 |
| 刪除檔案 | `APP_DeleteEvent` | `{"file":["name",...]}` |
| 鎖定影片 | `API_SetLockFile` | `{"switch":"lock"\|"unlock","filename":"..."}` |
| 加速下載 | `API_SuperDownload` | `{"switch":"on"\|"off"}` |
| 重啟 | `API_Reboot` | 無 |

### 串流

| 用途 | 位址 |
| --- | --- |
| 前鏡 RTSP | `rtsp://193.168.0.1:554/liveRTSP/av4`（1280×720 H.264 + pcm_alaw） |
| 後鏡 RTSP | `rtsp://.../av5`（本機無後鏡） |
| 檔案下載 | `http://IP/<name>`（支援 Range；不支援 HEAD） |

---

## 二、抓拍照片的真相

**相機會存抓拍照片**，檔名 `G_<YYYYMMDDHHMMSS>_<序號>_<時長>_X.jpg`，掛在**根目錄**，可直接 `GET http://IP/G_....jpg` 下載（實測 2560×1440、約 500 KB、帶 EXIF）。

抓拍後 mailbox 會吐出：

```
MSG_EventOccured  {"imgname":"G_20261101131646_050_0000_X.jpg","size":"3072"}
MSG_DeleteEvent   {"file":["G_20261031035114_009_0015_L.jpg"]}
```

也就是說：**照片真的會被刪**，但兇手是相機自己的 ring buffer，不一定是手機 App。

### 但沒有「列出所有抓拍」的 API

已徹底驗證：

- 24 支照片清單 cmd（`APP_PhotoListReq`、`API_GetPhotoList`、`APP_AlbumListReq`、`API_GetThumbList`…）全部 `WRONG_CGI_REQUEST`
- 列表類 cmd 加 `property=photo` / `type=photo` / `filetype=2`，GET 與 POST JSON body 都試過 → 無效
- APK 裡的 `DEV_PHOTO_LIST_REQ`（`Config.cgi?action=dir&property=photo`）只用在 **NPD 機型**；本機是自家 `bz` 協議，不走它
- 播放清單 166 筆全是 `.mp4`；猜 108 種 JPG 路徑全 404
- 官方 App 也是靠 mailbox 的 `MSG_EventOccured` 才知道有新照片

**因此**：抓拍必須由工具發起（`ddpai capture` 或網頁按鈕），當下拿檔名立即下載。離線時按遙控產生的照片，只能靠「時間窗 × 序號」暴力枚舉找回 —— 相機只有 12 req/s，27 秒窗口要 40 分鐘，實務上不划算。

---

## 三、四個容易踩的韌體特性

1. **`size` 單位是 KB**：清單回 `143360`，實際檔案 `146,800,640` bytes。用「Range 總長度 ÷ 清單值」判定（比值 ≈1024 即 KB），判定一次快取。
2. **不支援 `HEAD`**：回空 body。改用 `Range: bytes=0-0` 讀 `Content-Range` 總長度。
3. **時間戳是「本地時間當 UTC」**：OSD 顯示 12:47、epoch 也是 12:47 UTC，再套時區就多 8 小時。優先用檔名 `YYYYMMDDHHMMSS`；`end` 不可套檔名時間戳（否則每段長度變 0）。
4. **RTSP 只准一個 session**：舊 ffmpeg 沒收乾淨，新連線回 `DESCRIBE failed: 404`（看似路徑錯）。音頻是 `pcm_alaw`，mp4 不收，錄影要轉 aac。

### 單一客戶端限制

`DEV_ONLY_SINGLE_USER_CONNECT = 8196`：手機 App 連著時電腦連不上；跑 CLI 前要先停網頁服務。另外**不要用多線程猛打相機**（24 線程會把它的 httpd 打滿，連正常請求都 `RemoteDisconnected`，可能要好幾分鐘才恢復）。

---

## 四、工具能力現狀

```bash
ddpai doctor --matrix    # 連線診斷 + 9 組策略矩陣
ddpai info               # 型號／韌體／容量（size 自動換算 GB）
ddpai list [--rear]      # 錄影清單
ddpai events             # 事件清單
ddpai photos             # 相機照片（事件縮圖，唯一可列舉來源）
ddpai capture            # ★ 叫相機抓拍（需登入），立即下載到 photos/
ddpai mailbox            # 讀 MSG_EventOccured / MSG_DeleteEvent
ddpai download --latest N
ddpai lock <name> [--unlock]
ddpai delete <name> -y
ddpai rtsp / snapshot / reboot / raw
ddpai serve              # 網頁儀表板 8766
```

網頁（`:8766`）：即時畫面（MJPEG 直播／拍照／錄 10 秒）、相冊四來源（我的抓拍／相機照片／相機事件／相機錄影）、診斷頁。

---

## 五、待辦

- [ ] 遙控抓拍驗證：按遙控同時開 mailbox 輪詢，若 `MSG_EventOccured` 跳出即證明遙控有效（比枚舉快一萬倍）
- [ ] 若日後換支援照片清單 API 的機型，`Camera.photo_support()` 會自動啟用縮圖牆
- [ ] 錄影檔名目前用 WSL UTC 時間，與相機 OSD 差 8 小時（內容時間軸正確，僅檔名）
