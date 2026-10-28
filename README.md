# Mercari 商品監控

以關鍵字與價格篩選日本 Mercari 新上架商品，透過 LINE 通知。此版本不含個人商品、憑證或執行紀錄。

## 設定

1. 使用自己的專案副本；需要隱藏商品與價格時，使用私密專案。
2. 填寫 `watchlist.json`，完成後把 `enabled` 改為 `true`。追蹤多組商品時增加設定項目。
3. 在 Settings → Secrets and variables → Actions → Secrets 新增 `LINE_CHANNEL_ACCESS_TOKEN` 與 `LINE_USER_ID`，填入自己的 LINE 憑證與收件人 ID。
4. 到 Actions → Mercari monitor → Run workflow，手動執行。勾選 `test_line` 只會發送一則純文字連線測試。
5. 需要自動檢查時，在同頁的 Variables 新增 `MONITOR_ENABLED`，值設為 `true`；每 30 分鐘排程可能延遲，設回 `false` 可停止自動檢查。

| 商品欄位 | 填寫內容 |
| --- | --- |
| `id` | 每組不同且固定的代號；更換商品時使用新代號 |
| `name` | 通知顯示名稱 |
| `keyword` | 主要搜尋詞；必填 |
| `keywords` | 額外搜尋詞；空陣列時使用主要搜尋詞 |
| `price_min` / `price_max` | 日圓下限／上限；`null` 表示不限 |
| `must_match` / `must_not_match` | 標題須符合／不得符合的正規表示式；可留空 |
| `include_auctions` | 是否接受拍賣商品 |
| `review_note` | 通知中的人工核對提示；可留空 |
| `enabled` | 是否啟用這組商品 |

首次執行只建立現有商品基準；後續新出現的符合商品才通知。未填設定時不搜尋、不通知、不產生商品紀錄。公開專案中的設定與 `seen.json` 也會公開，金鑰請只放 Secrets。

僅依文字與價格篩選，不辨識圖片；搜尋只處理 API 本次回傳頁面，不保證即時或完整涵蓋。來源拒絕或搜尋失敗時會顯示失敗，不視為沒有商品；不使用登入、代理或驗證繞過。

## 本機

需要 Python 3.12 以上。安裝 `requirements.txt` 後，可執行：

```sh
python -m unittest -q
python mercari_watch.py --once --dry-run
```

正式通知另須自行設定環境變數 `LINE_CHANNEL_ACCESS_TOKEN` 與 `LINE_USER_ID`。

