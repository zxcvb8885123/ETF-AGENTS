# 全市場新聞來源核對（2026-10-23）

核准人：Apollo。核准紀錄由 `cli/market_news.py review` 寫入 `var/etf_agent.db` 的 `market_news_source_reviews`，同時保存規範頁原文與 SHA-256；`reviewed_at` 為實際寫入時間，只對之後的決策截止時間生效，不回溯舊決策。

| 來源代碼 | 內容 | 規範頁 | 規範內容（2026-10-23 抓取） | 結論 |
| --- | --- | --- | --- | --- |
| `CNA_FINANCE` | 中央社財經 RSS | [中央社 RSS 服務](https://www.cna.com.tw/about/rss.aspx) | 「同意本社RSS新聞內容用於個人、非營利組織之非商業用途」，引用需標示「中央通訊社」 | approved |
| `LTN_BUSINESS` | 自由時報財經 RSS | [自由時報 RSS](https://service.ltn.com.tw/RSS) | 「非營利機構網站、個人及Blog在非商業用途前提下，可免費使用新聞訊息，但須標明新聞出處」 | approved |
| `CNA_ARCHIVE` | 中央社歷史原頁標題與摘要 | 同中央社 RSS 頁 | RSS 規範未涵蓋網站原頁 | approved（使用者決定） |
| `LTN_ARCHIVE` | 自由時報歷史原頁標題與摘要 | 同自由時報 RSS 頁 | RSS 規範未涵蓋網站原頁；網站頁尾「版權所有不得轉載」 | approved（使用者決定） |

## 判斷

- 本專案只作 AI CUP 比賽研究，非商業、不轉售、不公開散布新聞原文；報告引用時標示媒體名稱。RSS 兩個來源依上表條款核准。
- 歷史原頁只使用標題與摘要作內部研究、不轉載。這是使用者於 2026-10-23 自行承擔的判斷，不是媒體的書面同意。
- 要撤回時，以 `cli/market_news.py review --status restricted` 寫入新紀錄；較新的紀錄會取代較舊的核准，舊決策仍以當時截止時間前的紀錄重建。

## 核准後仍未改變的事

- 每日決策鏈（`./start.sh run`）目前沒有接入全市場新聞，只用恐懼貪婪指標；核准本身不會讓每日報告出現新聞情緒。
- 正式計算仍需同時滿足：至少 5 則獨立內容、2 家媒體、標籤覆蓋全部項目（見 [market_news_integration.md](../market_news_integration.md)）。
- 個股新聞（FinMind）未在此核准：每日決策鏈沒有讀取它，且公司新聞不能代表全市場情緒。
