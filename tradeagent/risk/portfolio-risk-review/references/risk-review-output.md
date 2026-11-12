# 風險審查輸出（RiskReview）

模型只輸出 `decision`、`rationale`、`evidence_ids`、`risk_flags`、`unresolved_questions`、`revision_actions`；識別、版本與內容雜湊由程式補上。

- `decision`：`approve`／`revise`／`reject`。`GuardResult.passed=false` 只能 `reject`。已達修正上限（Policy `max_revisions`，比賽為 3）時只能 `approve` 或 `reject`，再要求修正會由程式降級為 `reject`。
- `evidence_ids`：只從主控給的可引用清單選（提案持股的行情證據）；proposal／scenario／guard 等 artifact ID 不是證據。
- `revision_actions`：`decision=revise` 時才有內容。每項四個字串欄位 `type`、`symbol`、`value`、`reason`（Codex strict JSON Schema 要求全部出現，不適用的填空字串）：

| type | symbol | value | 限制 |
| --- | --- | --- | --- |
| `remove_candidate` | 本提案的買進候選代號 | `""` | 只移除候選，不賣既有持股。 |
| `increase_cash_buffer` | `""` | 0～1 的小數字串 | ≥ 目前 `cash_buffer_rate`，且 ≤ `cash_weight_ceiling` − 0.03（競賽現金上限減 3 個百分點邊際，比賽為 0.22；整張取整會讓實際現金高於緩衝）。Policy 啟用 `cash_ratio_range` 時另 ≤ 該姿態今日可選的最高格點，且為兩位小數。 |
| `reduce_max_stock_weight` | `""` | 0～1 的小數字串 | ≤ 目前 `max_stock_weight`。Policy 有 `revision_bounds` 時另 ≥ `min_max_stock_weight`，且為 `step` 的整數倍（簡報會附數值）。 |
| `reduce_turnover_limit` | `""` | 0～1 的小數字串 | ≤ 目前 `max_turnover_rate`。Policy 有 `revision_bounds` 時另 ≥ `min_max_turnover_rate`，且為 `step` 的整數倍（簡報會附數值）。 |

`special_weight_limits` 內的股票（目前為 2330）不受 `reduce_max_stock_weight` 影響；要降低它的權重請用 `remove_candidate` 或 `increase_cash_buffer`。

每項只往降風險方向並附 `reason`；不適用的欄位填入非空內容、未知欄位、手寫權重或股數，都會被拒絕。

正例（`revise`）：

```json
[
  {"type": "remove_candidate", "symbol": "2330.TW", "value": "", "reason": "該筆委託佔 20 日均量比例被標 flagged，且同產業剩餘空間不足"},
  {"type": "increase_cash_buffer", "symbol": "", "value": "0.12", "reason": "價格下跌情境下買力接近用盡，先提高緩衝"}
]
```
