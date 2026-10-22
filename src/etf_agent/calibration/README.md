# calibration｜校準旁觀者

只讀已封存的 Decision run 與官方價量，回答兩個問題。**不屬於決策鏈**：`decision/`、`automation/` 與 `scripts/run_daily_pipeline.py` 都不匯入本套件（有測試鎖定），本套件也不回寫任何 Decision artifact。

1. 當時給的信心等級與多空強度，後來有沒有說中？（`snapshot` 加 `report`）
2. 決策用的前日收盤，與當日成交均價、開盤差多少？（`gap-audit`）

## 檔案清單

| 檔案 | 職責 | 主要公開項目 |
| --- | --- | --- |
| `contracts.py` | 常數與錯誤型別：horizon（1、5、20 日）、最少 20 個決策日、單日覆蓋率 80% | `CalibrationError`、`HORIZONS`、`MIN_DECISION_DAYS` |
| `tools.py` | 確定性統計（Decimal）：前瞻報酬、平均名次、Rank IC、分組平均、百分位 | `forward_return`、`rank_ic`、`average_ranks`、`group_summary` |
| `prices.py` | 官方成交均價查詢，只讀 SQLite；沿用帳本的 `OfficialCloseMarket` | `OfficialPriceBook` |
| `repository.py` | 觀測的不可變保存，獨立根目錄 `artifacts/calibration/` | `CalibrationRepository` |
| `service.py` | 記錄決策與對答案 | `build_observation`、`record_day`、`record_all`、`evaluate` |
| `gap_audit.py` | 決策價與成交均價的偏離、開盤漲跌停、缺均價清單 | `execution_gap_report` |

## 口徑

- **成交價**：官方成交金額 ÷ 成交股數（與帳本相同）。YAHOO 等非官方來源、成交量或金額為 0 的列不會產生均價。
- **進場日 T**：決策 cutoff 後第一個已有官方收盤的交易日（沿用 `OfficialCloseMarket.execution_date`）。**報酬 = T+h 日均價 ÷ T 日均價 − 1**；交易日只數資料庫裡的官方列（`calendar_basis=official_price_rows`），不使用其他日曆。
- **防前視**：只採用 `fetched_at` 不晚於 `as_of` 的列。價格還沒到齊的決策日標為 `pending`，不補值、不插值。
- **除權息**：窗口 (T, T+h] 內有 `ex_rights_marker` 的股票不計入。
- **超額報酬**：減去同一決策日、同一 horizon 內全池有效報酬的平均，避免只量到大盤。
- **同日重複決策**：同一個成交日只採用 cutoff 最晚的一份。
- **樣本門檻（寫在 Python）**：某 horizon 的「完整決策日」少於 20 天，只回報天數與筆數（`status=insufficient_sample`），不給任何平均或 Rank IC。
- **Rank IC**：只算 buy／add；訊號分別為信心等級（high 3、medium 2、low 1）與多空強度差（強度 strong 3、moderate 2、weak 1、none 0，多頭減空頭）；同分取平均名次；少於 3 筆或任一邊為常數時為 `None`。

## 限制

- 5 日與 20 日報酬在相鄰決策日高度重疊，等效獨立樣本遠少於筆數；不要對平均報酬做一般 t 檢定。
- 只有 buy／add 才有信心等級，比較的是「被選中的股票之間」，不是整體預測力。
- 結果是診斷，**不要在比賽期間用單一時段的結果改參數**。評分是否另計風險調整指標尚未確認，所以只報報酬與 IC。
- `gap-audit` 的相鄰資料日相隔超過 5 個日曆天（資料缺口或長假）、或任一天有除權息標記的配對一律略過。

## 命令

```bash
.venv/bin/python cli/calibration.py snapshot                    # 把 artifacts/decisions 裡的決策記成觀測，可重複執行
.venv/bin/python cli/calibration.py report                      # 對答案；價格未到齊顯示 pending
.venv/bin/python cli/calibration.py gap-audit --days 60         # 決策價與成交均價的偏離
```

`snapshot` 只讀已封存的決策，隨時可從封存補記，不會因為某天沒執行而永久漏掉。

每日流程（`scripts/run_daily_pipeline.py`）在決策封存、報告交付後，會以子程序呼叫 `cli/calibration.py snapshot`（不匯入本套件），結果寫入 `pipeline.json` 的 `calibration`。失敗只記錄，不影響決策、摘要狀態與結束碼。`report` 與 `gap-audit` 仍需手動執行。
