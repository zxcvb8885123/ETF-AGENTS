# 台股恐懼貪婪指標來源核對（2026-10-23）

核對時間：2026-10-23 00:56（Asia/Taipei）。核准人：Apollo。

| 來源代碼 | 資料 | 取得方式 | 使用依據 | 結論 |
| --- | --- | --- | --- | --- |
| `TAIFEX_PCR` | 臺指選擇權買賣權成交量比率% | `https://www.taifex.com.tw/data_gov/taifex_open_data.asp?data_name=PutCallRatio`（約最近 20 個交易日） | [政府資料開放平臺 11322](https://data.gov.tw/dataset/11322)：政府資料開放授權條款-第1版 | approved |
| `FINMIND_MARGIN` | 集中市場融資金額（`MarginPurchaseMoney`） | FinMind `TaiwanStockTotalMarginPurchaseShortSale` | 原始資料為證交所融資融券餘額，[政府資料開放平臺 11680](https://data.gov.tw/dataset/11680)：政府資料開放授權條款-第1版；FinMind 服務條款見 [Disclaimer](https://finmind.github.io/en/Disclaimer/) | approved |
| `TAIFEX_VIX` | 臺指選擇權波動率指數收盤值 | 期交所網站月檔 `.../vix/log2data/YYYYMMnew.txt`（只保留約最近 4 個月） | 見下方 | approved（使用者決定） |

## VIX 的判斷

- [期交所網站使用條款](https://www.taifex.com.tw/cht/edu/userTerms)第三點：網站內容的引用或轉載需事前書面同意，但已授權政府資料開放平臺者不在此限。
- 查詢時未在政府資料開放平臺或期交所 OpenAPI（`openapi.taifex.com.tw`）找到 VIX 資料集。
- 條款沒有列出「非商業用途」例外。
- 使用者於 2026-10-23 決定：本專案僅作 AI CUP 比賽研究、非商業、不轉售或公開散布原始資料，核准納入正式計算。這是使用者自行承擔的判斷，不是期交所的書面同意。
- 若日後取得書面同意或要撤回，修改 `config/market_fear_greed_sources.json` 的 `TAIFEX_VIX` 紀錄（新增核准時間），舊決策仍以當時截止時間前的紀錄重建。

## 不採用的來源

- 證交所網站 `rwd/zh/marginTrading/MI_MARGN`：網站使用條款禁止未經同意的自動化擷取，改用 FinMind。
- 證交所 OpenAPI `MI_MARGN`：只有逐股資料且只有當日，不足以計算變化率。
- CNN Fear & Greed Index：反映美股，不能代表台股。
