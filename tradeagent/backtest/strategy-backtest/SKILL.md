---
name: strategy-backtest
description: 重播已保存的台股 ETF 決策，依歷史 cutoff 驗證資料、模擬整張成交與跨日帳務。用於 fixture 或已驗證歷史資料的回測，不產生即時交易建議或下單。
---

# 策略回測 Agent

使用版本化交易日曆與 `BacktestRequest` 重播保存的決策。Python 工具負責歷史時鐘、成交、交割、公司行動、帳務與重建；Agent 只可選擇核准的 fixture、要求診斷及解釋保存結果。

1. 先執行 `validate-request`。日曆、時區、資料模式、帳戶、規則或費稅假設不合法即停止。
2. 先用 `inspect-coverage` 檢查每個交易日是否有 cutoff 前的研究版本、未還原成交價與收盤價。
3. `replay` 只接受 cutoff 前有 `available_at` 的資料版本，並使用未還原 execution price。`analysis_close_price` 不能當成交價。
4. 每日保存 approved／rejected／no_trade 決策、成交或未成交、settled／unsettled cash、持股與 NAV。不可跳過失敗日或以現在資料補歷史資料。
5. `validate-run` 必須從初始帳戶完整重播；`build-report` 只產生帳務驗收 JSON／Markdown，不是績效宣稱或正式競賽報告。
6. fixture 或 exploratory 結果只驗證流程，不能宣稱策略有效或可直接交易。

詳細欄位見 [回測契約](references/backtest-contract.md)。

```bash
.venv/bin/python cli/backtest.py \
  --request artifacts/backtest_request.json validate-request

.venv/bin/python cli/backtest.py \
  --request artifacts/backtest_request.json \
  --daily-inputs artifacts/backtest_daily_inputs.json replay \
  --output artifacts/backtest_run.json

.venv/bin/python cli/backtest.py \
  --request artifacts/backtest_request.json \
  --daily-inputs artifacts/backtest_daily_inputs.json \
  --input artifacts/backtest_run.json validate-run
```
