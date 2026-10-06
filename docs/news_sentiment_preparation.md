# 新聞候選核對與標題情緒測試

2026-10-03 已處理資料庫台達電與富邦金全部 85 筆候選、40 個不同連結。公開原頁讀取成功 37 個、HTTP 403 三個；原頁版本（包括失敗原因）存入 `news_page_captures`，原候選不改寫。26 個連結解析出明確帶時區的發布時間，其他時間仍未確認；新取得原文的可得時間為本次觀測時間，不能回填 9 月 29 日。

`perception/news_preparation` 解析 JSON-LD 與公開 metadata，分開保存標題、摘要、全文及發布時間原字串。不把網站樣板摘要當成全文，不推測時區，不自動核准使用權。`news_diagnostic` 要求全部不同連結均有股票匹配的模型標籤，保留新聞、社群、廣編及事件線索；重複版本及可識別的同內容轉載在計數前去重。同內容轉載若標註衝突，拒絕並要求核對。

本次 Codex 全量標註只依候選標題，原頁內容補抓另作來源核對。結果為 `news-diagnostic-1.0 / diagnostic_only`，不產生正式 MarketPerceptionResult，也不改寫情緒 validator 的 approved 要求。正式來源仍未接通；此工作完成的是資料準備、版本保存、標題判讀測試及可重跑入口。真實原頁成功讀取不代表已取得使用權。

重新驗證與封存：

```bash
.venv/bin/python cli/news_sentiment.py \
  --database var/etf_agent.db \
  --captures artifacts/news_sentiment_20261003/captures.json \
  --labels artifacts/news_sentiment_20261003/labels.json \
  --output-root artifacts/news_sentiment_20261003/runs
```

CLI 會比對候選與原資料庫版本，檢查 cutoff、原頁雜湊、全量覆蓋與標籤歸屬，保存不可變輸入／標籤／診斷。省略 labels 時只準備資料。公開連結抓取由本次保存的 capture.py 執行，未接每日自動擷取全文；沒有繞過拒絕存取、付費牆或登入。
