# Mercari 商品監控

監聽 Mercari 日本站、台灣入口、Yahoo!フリマ與駿河屋日本站的一般販售商品，透過 Discord、LINE 或兩者同時通知。搜尋使用最新排序與販賣中篩選，依來源商品編號合併結果。拍賣網站已移除，Mercari 拍賣商品預設排除。

- Discord 通知包含商品名稱、商品大圖、可點擊的商品連結與來源幣別價格。
- 同件商品跨關鍵字、跨站與跨輪次去重；每個通知目的地獨立記錄。不同賣家或不同刊登編號視為不同商品。
- 台灣站獨立讀取搜尋頁，使用「顯示可購買的商品」及「新商品」，不以日本站結果代替。
- Yahoo!フリマ只監聽固定價刊登，驗證服務端的最新排序及販售中條件；不搜尋 Yahoo!オークション。
- 駿河屋只使用 `www.suruga-ya.jp`，選「更新の新しい順」及「品切れ OFF」，確認本店售價後才通知；不同品況分開記錄，價格區間標示為起價。
- 私人搜尋條件從 `.env` 或 GitHub Secrets 載入，公開的 `watchlist.json` 保持空白範本。
- 已看紀錄加密保存，不提交商品編號、搜尋條件或執行紀錄到 Git。

## 本機設定

需要 Python 3.12 以上：

```sh
python -m venv .venv
# 啟用虛擬環境後：
python -m pip install -r requirements.txt
python -m playwright install chromium
# 使用駿河屋且沒有安裝 Edge / Chrome 時：
python -m patchright install chrome
```

將 `.env.example` 複製為 `.env`。環境變數優先於檔案；程式不會覆蓋已設定的變數。

| 環境變數 | 內容 |
| --- | --- |
| `WATCHLIST_JSON` | 私人監聽條件的 JSON 陣列；`[]` 停用監聽 |
| `DISCORD_WEBHOOK_URL` | Discord incoming Webhook 網址；使用 Discord 時設定 |
| `DISCORD_WEBHOOK_URL_SECONDARY` | 可選的獨立 Discord 通道；只接收指定給它的條件 |
| `DISCORD_WEBHOOK_URL_TERTIARY` | 可選的第三個獨立 Discord 通道；只接收指定給它的條件 |
| `MERCARI_LINK_MODE` | `original`（預設，標題連到商品原站）或 `buyee`（明確選擇以 Buyee 代購頁作主連結） |
| `SURUGAYA_BROWSER_CHANNEL` | 選用；Windows 預設 `msedge`、Linux 預設 `chrome`。駿河屋使用獨立的有頭瀏覽器，Linux 需桌面或 Xvfb |
| `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_USER_ID` | 使用 LINE 時須同時設定 |
| `MONITOR_STATE_KEY` | Fernet 加密金鑰；正式保存紀錄必填 |
| `MONITOR_STATE_PATH` | 加密紀錄位置，預設 `.state/seen.enc` |

產生加密金鑰，放入自己的 `.env` 與同名 GitHub Secret，後續執行使用相同金鑰：

```sh
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`WATCHLIST_JSON` 的通用格式如下；實際搜尋詞僅填在自己的 `.env`。JSON 可放在單引號內，正規表示式的反斜線需符合 JSON 與 dotenv 的跳脫規則。

```dotenv
WATCHLIST_JSON='[{"id":"watch-1","name":"商品A","keyword":"範例商品","keywords":[],"sources":["jp","tw"],"source_keywords":{},"price_min":null,"price_max":null,"must_match":[],"must_not_match":[],"include_auctions":false,"review_note":"","enabled":true}]'
```

| 商品欄位 | 說明 |
| --- | --- |
| `id` | 不同條件使用不同且固定的代號 |
| `name` | 通知顯示名稱 |
| `keyword` | 主要搜尋詞，必填 |
| `keywords` | 搜尋詞陣列，逐一搜尋後合併；空陣列使用 `keyword` |
| `sources` | 可選 `jp`、`tw`、`yahoo_fleamarket`、`surugaya`；省略時維持 Mercari 日本站；不接受拍賣網站 |
| `source_keywords` | 為啟用的來源分別指定搜尋詞陣列，避免每個網站都搜尋全部同義詞 |
| `discord_webhook_env` | Discord 目的地環境變數名稱；省略使用 `DISCORD_WEBHOOK_URL` |
| `price_min` / `price_max` | 日圓價格篩選；使用 `tw` 的條件請留 `null`，避免將台幣與日圓混用 |
| `must_match` / `must_not_match` | 標題須符合／不得符合的正規表示式陣列 |
| `include_auctions` | 日本站是否接受拍賣商品，省略與範本均為 `false`；一般販售監聽請保持關閉 |
| `review_note` | 人工核對提示 |
| `enabled` | 是否啟用 |

首次成功搜尋每一組條件與來源時，只建立基準。之後出現的新商品才通知；單一來源失敗時保留該來源紀錄，其他來源仍會處理。未啟用條件時不搜尋、不推播、不寫入紀錄。

不同規則可以指定不同 Discord 通道。例如第二組在私人 JSON 中加上 `"discord_webhook_env":"DISCORD_WEBHOOK_URL_SECONDARY"`，並在 `.env`／GitHub Secret 填入同名 Webhook。只接受 `DISCORD_WEBHOOK_URL` 或 `DISCORD_WEBHOOK_URL_` 後接大寫英數與底線的名稱；增加其他名稱時，也須同步加入 workflow 的環境變數。

自訂 Discord 目的地不會轉送到預設 Discord 或 LINE。目的地未設定時回報失敗，不會改送其他頻道。原有單通道去重紀錄會延續到預設通道，新通道首次建立自己的基準；同件商品若符合不同通道條件，可在各通道各收到一次。

第三個目的地使用 `DISCORD_WEBHOOK_URL_TERTIARY`，並在私人條件的 `discord_webhook_env` 指定同名變數；雲端來源工作、連線測試與計時異常警告均會讀取這個可選目的地。實際分流條件與通道憑證僅放在 `.env`／Secrets。

```sh
python mercari_watch.py --once --dry-run
python mercari_watch.py --test-discord
python mercari_watch.py --test-line
python mercari_watch.py --once
# 只執行某來源，私人條件仍從 .env 載入：
python mercari_watch.py --once --source surugaya
python -m unittest -q
```

測試通知以「【測試】」開頭，只發送純文字，不搜尋商品或修改去重紀錄；`--test-discord` 會測試所有已設定的 Discord 通道。試跑不發通知、不保存紀錄。

## Discord 設定

1. 在 Discord 建立文字頻道。
2. 伺服器設定 → 整合 → Webhook → 新增 Webhook，選擇頻道並複製網址。
3. 將網址填入 `.env` 的 `DISCORD_WEBHOOK_URL`，或 GitHub 同名 Secret。
4. 將接收頻道通知設為「所有訊息」，開啟 Discord 與手機系統的通知權限。

商品圖片由商品來源提供；缺少圖片時仍保留名稱與連結。兩站皆找到相同商品時合併連結。台灣站以新台幣顯示，日本站以日圓顯示，不自行推算匯率。圖片與商品內容需自行核對。

`MERCARI_LINK_MODE=original` 保留商品原站作為 Discord 標題及 LINE 查看按鈕的主連結。Discord 另外標示商品來源，店家刊登標為「Mercari Shops 日本站」；可核對商品編號時，另附「Buyee 代購入口（非原站）」。Buyee 是代購服務，並非商品來源，也不代表監聽新增了 Buyee 網站。

Mercari 原站在使用者裝置上可能轉往當地入口。監聽程式無法只靠通知網址控制手機的網站設定或 App 開啟行為，目前沒有已驗證能在所有裝置強制留在日本站的連結。只有明確希望以 Buyee 作主入口時，才在 `.env` 與 GitHub 同名 Secret 設定 `MERCARI_LINK_MODE=buyee`；這會更換開啟的網站，並不修復 Mercari 原站轉區。該模式在 Discord 將原站網址放在可複製欄位，保留來源標示。一般商品及具有匹配日本原始網址的 22 字元 Mercari Shops 商品編號可轉換；無法核對時不猜測。商品原始資料與去重紀錄保留，其他網站維持原連結。

`tw.mercari.com` 是跨境商品入口，並非台灣本地現貨市場；其台幣標價不能視為已包含全部跨境費用的到手價。不同賣家刊登同款商品仍分別通知，不以名稱相似判定同一件貨品。

Yahoo 新增來源同樣首次只建立基準，不重播既有商品。搜尋被拒絕、驗證頁、無法確認排序／庫存或資料不完整都會回報失敗。Yahoo 部分有效搜尋會回傳 HTTP 404，僅在服務端搜尋資料與篩選皆驗證成功時接受；缺少搜尋資料的 404 仍失敗。程式不把來源失敗解讀為零商品，也不使用代理或解驗證服務。

## 台灣比價評估

目前只有監聽、標題條件篩選與相同刊登 ID 去重，沒有實作跨市場商品配對、台灣價格擷取、圖片辨識或到手價計算。監聽不呼叫模型，不能據此推論收藏品比價可可靠地以零 token 完成。比價需要核對系列、版別、品況、數量與配件；圖文 AI 也須以真實案例驗證，資料不足時保留未知。現有測試未驗證跨市場配對品質；修正後的方案與實作狀態見 [比價評估](PRICE_COMPARISON.md)。

## 駿河屋日本站

`surugaya` 使用 [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python) 與獨立瀏覽器 profile，已在本機與 GitHub Windows／Edge runner 驗證原生搜尋與公開商品圖片。一般 HTTP、普通無頭瀏覽器與本次 Linux runner 未取得可確認的搜尋頁；首頁能開啟不代表搜尋、排序、庫存與圖片皆可用。Check workflow 的 `surugaya_probe` 可獨立檢查 Windows runner，不讀私人條件或通知憑證；網站仍可能變更驗證要求。

監聽「更新的商品」，不等同首次上市；只處理本次載入的搜尋頁。網站的「品切れ OFF」仍可能包含本店缺貨但其他賣家有貨的商品，因此程式另外確認本店新品／中古售價，略過第三方獨有商品與預約品。不將定價、運費門檻、手續費或第三方最低價當作本店售價。區間價標示「起」，不同分支品況與新品／中古使用不同 ID；它們仍需人工核對內容。尚未驗證的限時特價版型會回報來源失敗，保留既有紀錄。

圖片入口可能受驗證保護；只在商品需要通知時，沿網站實際圖片重新導向取得公開 CDN 網址，不猜圖片路徑。無法確認圖片時保留名稱與連結。瀏覽器 profile 保存在 `.state/surugaya-browser/`，包含瀏覽紀錄與網站 cookie，僅留本機，不提交或上傳 Actions Cache。雲端只保存原有加密去重檔；每次新 runner 都必須重新通過網站檢查。來源失敗不會變成零商品，也不建立假的基準；不保證永遠能通過驗證。不需模型 API、付費代理或解驗證服務。

工作流程將 Mercari 日本站、台灣入口與 Yahoo!フリマ留在 Linux，駿河屋由獨立 Windows job 執行 `--source surugaya`。兩邊分別快取 `.state/seen.enc` 與 `.state/surugaya-seen.enc`，共用私人規則與加密金鑰，各自首次建立基準；既有 Mercari／Yahoo 去重紀錄不需重置，停用來源的歷史也保留。只執行指定來源時，程式不搜尋其他來源，也保留同一狀態檔中其他來源的紀錄與健康狀態。

## GitHub 雲端排程

1. 使用自己的 repository。公開 fork 中只保留程式與範本。
2. 在 Settings → Secrets and variables → Actions → Secrets 設定 `WATCHLIST_JSON`、`MONITOR_STATE_KEY`，以及通知通道憑證。至少設定一種通知通道。
3. 在 Settings → Environments 建立 `monitor-clock-30m`，設定 Wait timer 為 **30 分鐘**；再建立 `monitor-clock-bootstrap`，Wait timer 為 **1 分鐘**。兩者均不設定 required reviewers。這些名稱只表示計時間隔，不包含私人條件。
4. 在 Variables 設定 `MONITOR_ENABLED=true`，到 Actions → **Monitor clock** → Run workflow；正常啟動保持 `bootstrap=false`。雲端計時器每輪自動啟動監聽，再接續下一輪，間隔約 30 分鐘，仍可能受 GitHub 排隊時間影響。首次驗收可用 `bootstrap=true`，先以一分鐘計時自動交接；下一輪固定使用正式 30 分鐘計時。
5. 看 [雲端計時紀錄](https://github.com/danny0926/mercari-watch-template/actions/workflows/monitor-clock.yml) 的等待狀態與下一輪，以及 [監聽執行紀錄](https://github.com/danny0926/mercari-watch-template/actions/workflows/market-monitor.yml)。自動監聽標題為 `Automatic monitor from clock …`，對應父計時 run。`workflow_dispatch` 可由使用者或雲端程式呼叫；事件名稱本身不能區分手動與自動，應連同父計時紀錄、觸發者及時間查看。
6. 單次檢查或通知測試仍使用 **Mercari monitor** → Run workflow；勾選 `test_discord` 或 `test_line` 可測試通知。`source` 可限制單一來源，搜尋詞仍只從 Secrets 讀取。來源失敗會保留其紀錄，其他來源繼續處理。

原生 cron 在本次實際觀察期間沒有產生 `schedule` 事件，因此正式流程已改用 GitHub environment wait timer，不將「設定了 cron」當作排程成功。此方案使用公開 repo 可用的等待計時功能；等待由 GitHub 處理，無須本機保持開機，也不呼叫模型。等待期間不計入執行計費時間，見 [GitHub 官方說明](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)。計時流程只使用目前 repo 的 `GITHUB_TOKEN`，不需要額外 PAT。請先設定 environment；程式會核對等候時間，缺少或不符時停止並告警，避免連續快速啟動。

停止時將 `MONITOR_ENABLED=false`；若要立即終止等待，也可取消 **Monitor clock** 正在等待的 run。已開始的來源工作可自行完成。重啟時設回 `true`，再執行一次 **Monitor clock**。工作被取消、workflow 被停用或持續 API 故障可能中斷接力；計時設定或交接失敗會向已設定的 Discord 通道發送警告，但故意取消不當作故障。來源健康警報只在程式啟動後運作，仍應核對最近的計時與監聽時間。

去重紀錄只以 Fernet 加密檔存入 Actions Cache，金鑰保存在 Secrets；工作流程不再提交 `seen.json`。快取可能遭清除，缺少紀錄時會重新建立基準，不將現有商品整批推送。請保留加密金鑰，變更金鑰前需先清除舊加密紀錄。

各目的地獨立保存發送結果，失敗只重試該目的地，成功目的地不受影響。預設目的地若同時設定 LINE 與 Discord，仍須兩者都成功才推進該目的地紀錄；其中一邊成功、分批中途失敗或送達後連線中斷時，該目的地先前成功的訊息可能重複。來源異常與恢復訊息也會送往對應目的地。Discord 限流依服務回傳時間做有限次重試。

Discord Webhook 基本功能不需 Nitro。公開專案使用標準 GitHub-hosted runner 免費；私人專案受帳號免費額度與超額計費規則限制。[Discord 說明](https://discord.com/safety/using-webhooks-and-embeds)、[GitHub 計費](https://docs.github.com/en/billing/concepts/product-billing/github-actions)。

## 資料與限制

搜尋只處理本次回傳／頁面已載入的商品，不保證即時或完整涵蓋。標題篩選不辨識圖片，也不保證商品真偽、內容、庫存或跨境購買資格。程式不登入商品平台，不使用付費代理或第三方解驗證服務。

本機 `.env`、`.state/`、執行紀錄與真實商品資料不得提交。公開範例、測試、README、版本說明與標籤均不包含個人搜尋內容。不要將私人條件放入 GitHub Variables、issue、commit 或 workflow 輸入。

## 版本與授權

版本標籤採 `vMAJOR.MINOR.PATCH`；版本變更見 [CHANGELOG.md](CHANGELOG.md)。

本專案 fork 自 [1212k10969/mercari-watch-template](https://github.com/1212k10969/mercari-watch-template)，基準為 `5a19a755a758b36f5bce2d3e1dc3e20f146c7e44`。上游基準未附授權；本 fork 的 MIT 許可僅涵蓋本次新增檔案與修改部分，不代表可重新授權上游原始程式。授權範圍與來源詳見 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。

