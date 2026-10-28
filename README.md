# Mercari 商品監控

監聽日本站與台灣站的新上架商品，透過 Discord、LINE 或兩者同時通知。搜尋固定使用最新排序與販賣中篩選，依原始商品編號合併結果。

- Discord 通知包含商品名稱、商品大圖、可點擊的商品連結與來源幣別價格。
- 同件商品跨關鍵字、跨站與跨輪次去重；不同賣家或不同刊登編號視為不同商品。
- 台灣站獨立讀取搜尋頁，使用「顯示可購買的商品」及「新商品」，不以日本站結果代替。
- 私人搜尋條件從 `.env` 或 GitHub Secrets 載入，公開的 `watchlist.json` 保持空白範本。
- 已看紀錄加密保存，不提交商品編號、搜尋條件或執行紀錄到 Git。

## 本機設定

需要 Python 3.12 以上：

```sh
python -m venv .venv
# 啟用虛擬環境後：
python -m pip install -r requirements.txt
python -m playwright install chromium
```

將 `.env.example` 複製為 `.env`。環境變數優先於檔案；程式不會覆蓋已設定的變數。

| 環境變數 | 內容 |
| --- | --- |
| `WATCHLIST_JSON` | 私人監聽條件的 JSON 陣列；`[]` 停用監聽 |
| `DISCORD_WEBHOOK_URL` | Discord incoming Webhook 網址；使用 Discord 時設定 |
| `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_USER_ID` | 使用 LINE 時須同時設定 |
| `MONITOR_STATE_KEY` | Fernet 加密金鑰；正式保存紀錄必填 |
| `MONITOR_STATE_PATH` | 加密紀錄位置，預設 `.state/seen.enc` |

產生加密金鑰，放入自己的 `.env` 與同名 GitHub Secret，後續執行使用相同金鑰：

```sh
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`WATCHLIST_JSON` 的通用格式如下；實際搜尋詞僅填在自己的 `.env`。JSON 可放在單引號內，正規表示式的反斜線需符合 JSON 與 dotenv 的跳脫規則。

```dotenv
WATCHLIST_JSON='[{"id":"watch-1","name":"商品A","keyword":"範例商品","keywords":[],"sources":["jp","tw"],"source_keywords":{},"price_min":null,"price_max":null,"must_match":[],"must_not_match":[],"include_auctions":true,"review_note":"","enabled":true}]'
```

| 商品欄位 | 說明 |
| --- | --- |
| `id` | 不同條件使用不同且固定的代號 |
| `name` | 通知顯示名稱 |
| `keyword` | 主要搜尋詞，必填 |
| `keywords` | 搜尋詞陣列，逐一搜尋後合併；空陣列使用 `keyword` |
| `sources` | `["jp"]`、`["tw"]` 或兩者；省略時維持日本站 |
| `source_keywords` | 可為 `jp`、`tw` 分別指定搜尋詞陣列 |
| `price_min` / `price_max` | 日本站日圓篩選；使用台灣站的條件請留 `null`，避免將台幣與日圓混用 |
| `must_match` / `must_not_match` | 標題須符合／不得符合的正規表示式陣列 |
| `include_auctions` | 日本站是否接受拍賣商品 |
| `review_note` | 人工核對提示 |
| `enabled` | 是否啟用 |

首次成功搜尋每一組條件與來源時，只建立基準。之後出現的新商品才通知；單一來源失敗時保留該來源紀錄，其他來源仍會處理。未啟用條件時不搜尋、不推播、不寫入紀錄。

```sh
python mercari_watch.py --once --dry-run
python mercari_watch.py --test-discord
python mercari_watch.py --test-line
python mercari_watch.py --once
python -m unittest -q
```

測試通知只發送純文字，不搜尋商品或修改去重紀錄。試跑不發通知、不保存紀錄。

## Discord 設定

1. 在 Discord 建立文字頻道。
2. 伺服器設定 → 整合 → Webhook → 新增 Webhook，選擇頻道並複製網址。
3. 將網址填入 `.env` 的 `DISCORD_WEBHOOK_URL`，或 GitHub 同名 Secret。
4. 將接收頻道通知設為「所有訊息」，開啟 Discord 與手機系統的通知權限。

商品圖片由商品來源提供；缺少圖片時仍保留名稱與連結。兩站皆找到相同商品時合併連結。台灣站以新台幣顯示，日本站以日圓顯示，不自行推算匯率。圖片與商品內容需自行核對。

## GitHub Actions 排程

1. 使用自己的 repository。公開 fork 中只保留程式與範本。
2. 在 Settings → Secrets and variables → Actions → Secrets 設定 `WATCHLIST_JSON`、`MONITOR_STATE_KEY`，以及通知通道憑證。至少設定一種通知通道。
3. 到 Actions → Mercari monitor → Run workflow；勾選 `test_discord` 或 `test_line` 可先測試通知。
4. 在 Variables 設定 `MONITOR_ENABLED=true`，啟用每 30 分鐘檢查；設為 `false` 停止排程。GitHub 排程可能延遲。
5. 從執行結果確認各來源的成功／失敗狀態；被拒絕、驗證頁或無法確認篩選條件，都視為搜尋失敗。

去重紀錄只以 Fernet 加密檔存入 Actions Cache，金鑰保存在 Secrets；工作流程不再提交 `seen.json`。快取可能遭清除，缺少紀錄時會重新建立基準，不將現有商品整批推送。請保留加密金鑰，變更金鑰前需先清除舊加密紀錄。

所有已設定通道都成功，才記為已通知；任何通道失敗則下輪重送。雙通道部分成功、分批中途失敗或送達後連線中斷時，先前成功的訊息可能重複。Discord 限流依服務回傳時間做有限次重試。

Discord Webhook 基本功能不需 Nitro。公開專案使用標準 GitHub-hosted runner 免費；私人專案受帳號免費額度與超額計費規則限制。[Discord 說明](https://discord.com/safety/using-webhooks-and-embeds)、[GitHub 計費](https://docs.github.com/en/billing/concepts/product-billing/github-actions)。

## 資料與限制

搜尋只處理本次回傳／頁面已載入的商品，不保證即時或完整涵蓋。標題篩選不辨識圖片，也不保證商品真偽、內容、庫存或跨境購買資格。程式不登入 Mercari、不使用代理、不解驗證。

本機 `.env`、`.state/`、執行紀錄與真實商品資料不得提交。公開範例、測試、README、版本說明與標籤均不包含個人搜尋內容。不要將私人條件放入 GitHub Variables、issue、commit 或 workflow 輸入。

## 版本與授權

版本標籤採 `vMAJOR.MINOR.PATCH`；版本變更見 [CHANGELOG.md](CHANGELOG.md)。

本專案 fork 自 [1212k10969/mercari-watch-template](https://github.com/1212k10969/mercari-watch-template)，基準為 `5a19a755a758b36f5bce2d3e1dc3e20f146c7e44`。上游基準未附授權；本 fork 的 MIT 許可僅涵蓋本次新增檔案與修改部分，不代表可重新授權上游原始程式。授權範圍與來源詳見 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。

