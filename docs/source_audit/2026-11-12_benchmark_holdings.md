# 2026-11-12 0050 成分權重來源稽核

## 用途

`position_sizing.benchmark_facts`（已實作、尚未開啟）的 `active_weights` 區塊：風險 Agent 簡報的背景事實，顯示 0050 前幾大成分權重與組合起始權重的差額。紀錄用、不可引用、不改配置。

## 來源

| 項目 | 內容 |
| --- | --- |
| 來源 | 元大投信官網「持股權重」頁：`https://www.yuantaetfs.com/product/detail/0050/ratio` |
| 資料日 | 2026-11-12（頁面標示） |
| 抓取方式 | 2026-11-12 16:2x（Asia/Taipei）以網頁擷取工具讀取，人工抄錄進 `data/benchmark_holdings.csv` |
| 讀到的內容 | 16:2x 只讀到前 5 大；21:36 以瀏覽器點「展開」讀到完整清單，補進前 10 大：台積電 58.92%、聯發科 5.83%、鴻海 3.02%、台達電 2.46%、日月光投控 2.00%、南亞 1.49%、富邦金 1.37%、中信金 1.37%、台光電 1.23%、聯電 1.21%（資料日同為 2026-11-12，整份 `available_at` 統一記 21:36） |
| 授權 | **待確認**：頁面的使用條款／基金警語正文未讀到。 |
| 核准 | 使用者（Apollo）2026-11-12 決定以風險 Agent 背景事實用途使用，由使用者自行承擔使用條款風險（比照 [MoneyDJ 先例](2026-10-25_active_etf_holdings.md)）；建議使用者開頁核對數字與條款 |
| 保存 | `data/benchmark_holdings.csv`（欄位同 `data/active_etf_holdings.csv`，權重為占淨值比例＝百分比 ÷ 100）；核准檔 `config/benchmark_holdings_sources.json` |

## 限制

- 只有前 10 大（合計約 77.4%），其餘成分未知；`active_weights` 只對這 10 檔有意義，簡報須標明前 N 大合計。
- `available_at` 記為抓取時間，不回填到資料日；更早截止的決策不得使用。
- 權重隨股價每日漂移：預定每週一、四更新，`max_holdings_age_days`（人填，預定 5）以交易日計，超過即 `unavailable`。
- 補到前 10 大時只需更新 CSV（rank 連續 1 起、至多 10 列），不需改程式。
