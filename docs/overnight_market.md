# 隔夜市場背景

## 用途與範圍

替風險 Agent 的「配置前現金姿態」與最終報告提供一個由程式計算的隔夜背景：美股三大指數、在美國掛牌的台股代理（1.1 另含日韓指數、VIX 與台指期夜盤），在台股上次收盤到本次決策截止之間完成的時段表現。

- 只作背景。**不併入**全市場情緒（`market_sentiment`），不進分析師、多空研究員或交易 Agent，不產生候選、權重或訂單，也不預測今日台股指數方向。
- 資料包 `overnight-market-1.1`（結果 `overnight-v1.1`）加入日韓指數、VIX 與台指期夜盤；舊封存的 `overnight-market-1.0`（結果 `overnight-v1`）維持原重建行為，`night_session` 為 `null`。見下方「1.1 擴充」。
- 程式在 `src/etf_agent/perception/overnight_market/`，CLI 為 `cli/overnight_market.py`，來源核准在 `config/overnight_market_sources.json`。
- `team_inputs` 不升版；沒有任何抓取時決策輸入不附欄位，既有 fixture 與舊 run 重建逐位元不變。

## 授權狀態（需要使用者決定）

> 1.1：`YAHOO_FINANCE` 與 `TAIFEX_MIS`（夜盤）都已由使用者核准。`TAIFEX_MIS` 先前是 `unverified`（核准的是 OpenAPI、實際用官網即時行情），使用者 2026-10-30 在被告知端點不同後明確改為 `approved`；格式仍待 10/31 05:00 後實抓驗證，不符時夜盤維持 `unavailable`。核准時間晚於決策截止時間的來源在 1.1 視同未核准並寫入 `data_gaps`（1.0 維持直接拒絕）。

`config/overnight_market_sources.json` 目前的 `YAHOO_FINANCE` 已由使用者 apollo 於 2026-10-30 核准（`license_status: "approved"`）。若改回未核准，整體結果固定 `unavailable`、`outlook=unknown`，逐標的數字只在診斷輸出（`cli/overnight_market.py show`）可見，不進現金姿態 brief，也不註冊 evidence_id。

授權核准是使用者的決定：確認 Yahoo Finance 使用條款可用於非商業比賽研究後，由使用者把該列改成 `approved`，並填入 `approved_by`、`evidence`、`usage_scope=competition_research` 與不晚於決策截止時間的 `reviewed_at`，隔夜背景才會進正式結果。

## 運作方式

1. `./start.sh daily` 在恐懼貪婪抓取之後、固定 cutoff 之前執行 `cli/overnight_market.py capture`（yfinance 日線：`^GSPC`、`^IXIC`、`^SOX`、`EWT`、`TSM`；1.1 另有 `^N225`、`^KS11`、`^VIX` 與期交所官網夜盤報價），不可變地保存到 `artifacts/data/overnight_market/`。失敗只降級，不中止每日流程。
2. 決策輸入建立時選出所有 `available_at` 都不晚於 cutoff 的最後一次抓取，連同來源核准與台股最近交易日組成資料包，由程式算出結果（`overnight_market_input` = `{bundle, result}`）。
3. `DecisionInputValidator` 與 `overnight_market_channel` 以原始回應重算，數字被改、原始回應雜湊不符、網址或主機不在白名單、抓取晚於 cutoff、缺時區都拒絕。

## 時間規則

- `available_at` = 抓取時間（UTC，含時區），不回填到過去。
- 美股每個日線的 `session_end` = 該日 16:00 America/New_York 轉 UTC（夏令／冬令自動換算；提早收盤日取 16:00，偏保守）。`session_end` 晚於該次抓取時間的日線丟棄，不使用盤中價。
- 可用視窗：台股最近交易日 13:30（台北）< `session_end` ≤ `decision_cutoff`。多個時段以 (1+r) 累乘（台股連假、週一納入週五收盤）。
- 視窗內沒有完成時段（例如平日 21:00 的 run，美股當日尚未收盤）時，該標的與整體 `unavailable`、`outlook=unknown`，這是正常結果。

## 指標與判讀（`overnight-v1`；1.1 的 `outlook` 規則相同）

- 每個標的：`change_pct`（最新時段漲跌幅）、`cumulative_change_pct`（視窗內累乘）、`percentile_20d`（最新單日報酬在近 20 個單日報酬的百分位，樣本不足為 `null`）。
- 主訊號 = `^SOX` 與 `^GSPC` 的 `cumulative_change_pct` 平均，兩者都必須可用，否則整體不可用。≤ −1.5% 為 `negative`，≥ +1.0% 為 `positive`，其間 `neutral`。
- 台股代理（`EWT`、`TSM` 可用者的累計漲跌幅平均）與主訊號方向相反且兩者幅度皆 > 0.75% 時為 `mixed`。`^IXIC`、代理單項失敗只降級（列入 `data_gaps`）。
- evidence_id 格式 `overnight:<來源>:<代號>:<雜湊前 20 碼>`，只給已核准且可用的標的；只有現金姿態可引用，個股層級的 claim／finding 引用一律拒絕。
- `limitations` 固定三條：代理非因果、只代表隔夜一段、不預測指數方向。

## 接線

`start.sh` 抓取 → `scripts/run_daily_pipeline.py` 與 `automation/daily_pipeline.py` 透傳 `overnight_root`／`overnight_approvals_path` → `decision/service.py::build_input_file` → `decision/input_builder.py`（可選欄位 `overnight_market_input`）→ `decision/input_validator.py`／`input_context.py` → `automation/risk_stages.py::run_cash_stance`（brief 的 `overnight_market` 與 `citable_evidence_ids`）→ `decision_delivery/builder.py`／`renderer.py`（報告「隔夜市場背景」一行與引用清單；`renderer_v1.py` 不動）。

## 限制

- Yahoo 資料經 yfinance 取得，無 SLA；`url` 欄位是該標的 Yahoo 圖表端點的識別，實際傳輸由 yfinance 負責。
- 盤前、美股期貨與匯率未納入；夜盤只有 1.1，且要夜盤結束後才有（見「1.1 擴充」）。
- 代理與指數只是相關背景，不是台股因果。

## 1.1 擴充（2026-10-30）

- **新增標的**：`^N225`、`^KS11`（`asia_index`，日韓正常盤 15:30 當地收盤，約台北 14:30，所以 21:00 的 run 已可用）、`^VIX`（`volatility`，ET 16:15 完成）。三者仍走 Yahoo 日線與同一份核准。
- **台指期夜盤（`TXF_NIGHT`，來源 `TAIFEX_MIS`）**：取期交所官網即時行情 `https://mis.taifex.com.tw/futures/api/getQuoteList` 的臺指期月合約，只保存 `SymbolID`、`CDate`、`CTime`、`CLastPrice`、`CRefPrice`、`CTotalVolume`。挑成交量最大的月合約，`change_pct` = 最後價 ÷ `CRefPrice`（前一日日盤結算價）− 1。
- **為什麼不用 OpenAPI／FinMind**：兩者的「盤後」列屬於「同一日日盤之前」那段夜盤（`Change` 以前一日結算價為基準），當日日盤收盤後才出現，07:00 的 run 只拿得到前一天的夜盤，不能當決策輸入（2026-10-30 以探測日誌與 FinMind 實測確認）。它們只適合事後校準。
- **完成條件（fail-closed）**：抓取時間在夜盤結束（報價日期當天台北 05:00）之後、報價時間不晚於 05:05、報價時間不晚於抓取時間，且夜盤結束時間落在 台股最近交易日 13:30 < 結束 ≤ cutoff。平日 21:00 的 run 夜盤進行中，一律 `unavailable`（`data_gaps` 說明），不影響整體狀態；未核准、抓取失敗、格式不符同樣只降級。
- **`agreement`**：把美股三指數、EWT、TSM、日韓指數、夜盤（VIX 不算方向）的漲跌幅以 ±0.30% 為持平帶分成上／下／持平；至少 3 個訊號且同向比例 ≥ 75%、沒有反向者為 `aligned_up`／`aligned_down`，不足 3 個為 `insufficient`，其餘 `mixed`。只描述同向程度，不是預測。
- **`outlook` 規則不變**：仍是 ^SOX 與 ^GSPC 的主訊號（與 `overnight-v1` 相同門檻）。新資料目前只增加背景與 `agreement`；要不要讓它們影響姿態下限，須先用專案自己的歷史資料離線驗證（尚未做）。
- **已知限制**：①夜盤端點的日期與時間格式（`CDate` 是否為日曆日、05:00 後 `CTime` 的表現）只用 18:11 的盤中報價與測試假資料驗證，**第一次真實驗證在 2026-10-31 台北 05:00 後的抓取**；不符時維持 `unavailable`。②主訊號（^SOX、^GSPC）不可用時整體 `unavailable`，所以平日 21:00 的 run（美股未收）日韓資料算出來也不會進正式結果。③`mis.taifex.com.tw` 不是公開 OpenAPI，條款適用由使用者另行確認；`config/overnight_market_sources.json` 的 `TAIFEX_MIS` 已由使用者明確核准（見「授權狀態」）。最後一筆成交在午夜前（零成交到 05:00）會被保守判為「尚未結束」，屬已知的 false negative。④找不到夜盤或費半對台股隔日開盤預測力的公開量化統計，因此只當背景。

## 各市場資料何時可抓（台北時間）

| 市場／資料 | 收盤或完成 | 正式流程能用的 run | 備註 |
| --- | --- | --- | --- |
| 台股現貨 | 13:30 | 20:00 資料抓取後的 21:00、隔日 07:00 | 官方價量 20:00 才抓 |
| 日經 `^N225`、KOSPI `^KS11` | 約 14:30（15:30 當地） | 21:00 與 07:00 | 已實測 18:16 抓得到當日日線；Yahoo 最終化延遲沒有量化 |
| 美股 `^GSPC`、`^IXIC`、`^SOX`、EWT、TSM | 美東 16:00 ＝ 04:00（夏令，至 11/1）／05:00（冬令，11/2 起） | 只有 07:00 | 2026-10-30 07:01 的抓取已含前一晚完整日線；21:00 時美股未開盤，視窗內無完成時段 |
| `^VIX` | 美東 16:15 ＝ 04:15／05:15 | 只有 07:00 | |
| 台指期夜盤 | 05:00 結束 | 只有 07:00（08:45 日盤開前） | 期交所 OpenAPI、FinMind 要等當日日盤收盤後才有，不能用；官網即時行情待 10/31 05:00 後實測 |

21:00 是備份 run：美股未收、夜盤進行中，背景整體 `unavailable` 是預期結果，不需要處理。每日排程：週一至週五 07:00、週日至週四 21:00（`scripts/launchd/com.etf-agents.daily.plist.template`）。
