# Yahoo股市 全市場新聞來源核對（2026-11-16）

核准人：Apollo（2026-11-16 明確同意）。核准紀錄由 `cli/market_news.py review` 寫入 `var/etf_agent.db` 的 `market_news_source_reviews`，並保存規範頁原文與 SHA-256；只對之後的決策截止時間生效，不回溯舊決策。

| 來源代碼 | 內容 | 規範頁 | 規範內容 | 結論 |
| --- | --- | --- | --- | --- |
| `YAHOO_TW_MARKET` | Yahoo股市 `rss?category=tw-market` | [Yahoo股市 RSS 說明](https://tw.stock.yahoo.com/rss-help) | 「個人和非營利組織可以在非商業用途內免費使用這些資訊。每則訊息的出處必須被保留」；文字標示出處用「Yahoo股市」 | approved |
| `YAHOO_INTL_MARKETS` | Yahoo股市 `rss?category=intl-markets`（美股與國際市場） | 同上 | 同上 | approved |

## 判斷與限制

- 本專案只作 AI CUP 比賽研究，非商業、不轉售、不公開散布新聞原文；報告引用時標示「Yahoo股市」。
- 事實：Yahoo RSS 只保留約 4～5 小時、約 50 則，所以排程改為每 2 小時抓一次（`com.etf-agents.market-news-capture`），否則週末與夜間新聞會永久掉出。
- 事實：RSS 偶有無標題項目，程式只對 Yahoo 來源略過這種項目，其他來源仍整份拒絕。
- 推論：Yahoo 會轉載中央社等媒體稿，與中央社來源可能出現同內容；程式只依標題與日期去重，無法保證完全不重複計入「兩家媒體」。
- 撤回時用 `cli/market_news.py review --status restricted` 寫入新紀錄。
