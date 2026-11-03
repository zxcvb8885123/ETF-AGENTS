# 融資水位訊號來源核對（2026-11-04）

核對日期：2026-11-04（Asia/Taipei）。核准人：Apollo（使用者於 2026-11-04 在對話中口頭核准做法 A'，用途限 AI CUP 比賽研究、非商業、不轉售或公開散布原始資料）。

| 來源代碼（暫名） | 資料 | 取得方式 | 使用依據 | 結論 |
| --- | --- | --- | --- | --- |
| `FINMIND_MARGIN`（沿用） | 集中市場融資金額 `MarginPurchaseMoney` | FinMind `TaiwanStockTotalMarginPurchaseShortSale` | 已核准，見[前次核對](2026-10-23_market_fear_greed_sources.md) | approved（沿用） |
| `FINMIND_TAIEX`（新增） | 加權指數收盤價 | FinMind `TaiwanStockPrice`，`data_id=TAIEX` | 原始資料為證交所；經 FinMind 取得，條款見 [Disclaimer](https://finmind.github.io/en/Disclaimer/) | 使用者核准；寫入 config 時才生效 |

## 實測結果（2026-11-04）

- 免金鑰與現有金鑰（等級 register）都能取得 `TaiwanStockPrice` 的 TAIEX 與融資金額。
- 2025-01-02 至 2026-11-03 兩者各 443 個交易日，足夠 250 日百分位。
- 融資維持率 `TaiwanTotalExchangeMarginMaintenance` 在 free／register 等級被擋（回 400），不採用。

## 不採用的來源

- 證交所網站 `rwd/zh/afterTrading/FMTQIK`：與前次核對相同，網站條款禁止未經同意的自動化擷取。
- 證交所 OpenAPI `FMTQIK`、`MI_5MINS_HIST`：只回最新一筆，無法算 250 日歷史。
- FinMind `TaiwanStockMarketValue`：free 等級被擋。

## 已知限制

- 加權指數只是市值的代理，不含新上市與增資，長期有偏差；一年窗口內影響小。
- 使用者口頭核准不等於來源方書面同意；已於 2026-11-04 經使用者確認並寫入 `config/market_fear_greed_sources.json`。
