# 隔夜市場背景（Phase 1）

## 用途與範圍

替風險 Agent 的「配置前現金姿態」與最終報告提供一個由程式計算的隔夜背景：美股三大指數與在美國掛牌的台股代理，在台股上次收盤到本次決策截止之間完成的時段表現。

- 只作背景。**不併入**全市場情緒（`market_sentiment`），不進分析師、多空研究員或交易 Agent，不產生候選、權重或訂單，也不預測今日台股指數方向。
- **台指期夜盤尚未接入**（Phase 2）：`night_session` 固定為 `null`，`data_gaps` 固定列出「台指期夜盤尚未接入」。
- 程式在 `src/etf_agent/perception/overnight_market/`，CLI 為 `cli/overnight_market.py`，來源核准在 `config/overnight_market_sources.json`。
- `team_inputs` 不升版；沒有任何抓取時決策輸入不附欄位，既有 fixture 與舊 run 重建逐位元不變。

## 授權狀態（需要使用者決定）

`config/overnight_market_sources.json` 目前的 `YAHOO_FINANCE` 已由使用者 apollo 於 2026-10-30 核准（`license_status: "approved"`）。若改回未核准，整體結果固定 `unavailable`、`outlook=unknown`，逐標的數字只在診斷輸出（`cli/overnight_market.py show`）可見，不進現金姿態 brief，也不註冊 evidence_id。

授權核准是使用者的決定：確認 Yahoo Finance 使用條款可用於非商業比賽研究後，由使用者把該列改成 `approved`，並填入 `approved_by`、`evidence`、`usage_scope=competition_research` 與不晚於決策截止時間的 `reviewed_at`，隔夜背景才會進正式結果。

## 運作方式

1. `./start.sh daily` 在恐懼貪婪抓取之後、固定 cutoff 之前執行 `cli/overnight_market.py capture`（yfinance 日線：`^GSPC`、`^IXIC`、`^SOX`、`EWT`、`TSM`），不可變地保存到 `artifacts/data/overnight_market/`。失敗只降級，不中止每日流程。
2. 決策輸入建立時選出所有 `available_at` 都不晚於 cutoff 的最後一次抓取，連同來源核准與台股最近交易日組成資料包，由程式算出結果（`overnight_market_input` = `{bundle, result}`）。
3. `DecisionInputValidator` 與 `overnight_market_channel` 以原始回應重算，數字被改、原始回應雜湊不符、網址或主機不在白名單、抓取晚於 cutoff、缺時區都拒絕。

## 時間規則

- `available_at` = 抓取時間（UTC，含時區），不回填到過去。
- 美股每個日線的 `session_end` = 該日 16:00 America/New_York 轉 UTC（夏令／冬令自動換算；提早收盤日取 16:00，偏保守）。`session_end` 晚於該次抓取時間的日線丟棄，不使用盤中價。
- 可用視窗：台股最近交易日 13:30（台北）< `session_end` ≤ `decision_cutoff`。多個時段以 (1+r) 累乘（台股連假、週一納入週五收盤）。
- 視窗內沒有完成時段（例如平日 21:00 的 run，美股當日尚未收盤）時，該標的與整體 `unavailable`、`outlook=unknown`，這是正常結果。

## 指標與判讀（`overnight-v1`）

- 每個標的：`change_pct`（最新時段漲跌幅）、`cumulative_change_pct`（視窗內累乘）、`percentile_20d`（最新單日報酬在近 20 個單日報酬的百分位，樣本不足為 `null`）。
- 主訊號 = `^SOX` 與 `^GSPC` 的 `cumulative_change_pct` 平均，兩者都必須可用，否則整體不可用。≤ −1.5% 為 `negative`，≥ +1.0% 為 `positive`，其間 `neutral`。
- 台股代理（`EWT`、`TSM` 可用者的累計漲跌幅平均）與主訊號方向相反且兩者幅度皆 > 0.75% 時為 `mixed`。`^IXIC`、代理單項失敗只降級（列入 `data_gaps`）。
- evidence_id 格式 `overnight:<來源>:<代號>:<雜湊前 20 碼>`，只給已核准且可用的標的；只有現金姿態可引用，個股層級的 claim／finding 引用一律拒絕。
- `limitations` 固定三條：代理非因果、只代表隔夜一段、不預測指數方向。

## 接線

`start.sh` 抓取 → `scripts/run_daily_pipeline.py` 與 `automation/daily_pipeline.py` 透傳 `overnight_root`／`overnight_approvals_path` → `decision/service.py::build_input_file` → `decision/input_builder.py`（可選欄位 `overnight_market_input`）→ `decision/input_validator.py`／`input_context.py` → `automation/risk_stages.py::run_cash_stance`（brief 的 `overnight_market` 與 `citable_evidence_ids`）→ `decision_delivery/builder.py`／`renderer.py`（報告「隔夜市場背景」一行與引用清單；`renderer_v1.py` 不動）。

## 限制

- Yahoo 資料經 yfinance 取得，無 SLA；`url` 欄位是該標的 Yahoo 圖表端點的識別，實際傳輸由 yfinance 負責。
- 夜盤、盤前、期貨與匯率都未納入。
- 代理與指數只是相關背景，不是台股因果。
