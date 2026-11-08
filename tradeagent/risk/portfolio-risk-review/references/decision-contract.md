# 決策層契約摘要（DecisionInputBundle 至 DecisionResult）

這份是決策層程式契約的摘要，供人與開發對照；風險 Agent 審查時只需 [主指令](../SKILL.md)、[現金姿態規則](cash-stance.md) 與 [審查輸出](risk-review-output.md)。完整契約以 `src/etf_agent/decision/` 的 schema 與 Validator 為準，流程與版本閘門見專案 `AGENTS.md`、`docs/architecture.md` 與 `docs/decision-details.md`。

## DecisionInputBundle

所有子 Agent 只讀同一份 bundle。頂層必要欄位：

```json
{
  "schema_version": "1.0",
  "bundle_id": "decision-input-001",
  "snapshot_id": "snapshot-id",
  "decision_cutoff": "2026-09-21T08:55:00+08:00",
  "bundle_sha256": "排除自身欄位後的 canonical JSON SHA-256",
  "snapshot_sha256": "canonical JSON SHA-256",
  "snapshot": {},
  "account_snapshot": {},
  "rules": {},
  "benchmarks": [],
  "price_series": [],
  "research_results": [],
  "perception_inputs": []
}
```

- `bundle_sha256` 鎖定整份輸入；頂層與帳戶、規則、基準、行情物件使用嚴格欄位白名單。
- `account_snapshot` 必須有 `account_id`、`available_at`、`valuation_at`、`source_evidence_id`、`cash`、`settled_cash`、`unsettled_cash`、`nav` 與不重複的正股數持股；`cash=settled_cash+unsettled_cash`，NAV 必須在規則容許誤差內等於 cutoff 行情重算值，配置只能使用 settled cash。
- `rules` 保存來源 URL、發布／可得時間、不可放寬限制及 `config_sha256`；`benchmarks` 可為空，空必備基準時 `minimum_active_share` 必須為 0。
- `price_series` 覆蓋 Snapshot 全交易池，每個序列以 `series_sha256` 鎖定並依日期嚴格遞增。
- `research_results`、`perception_inputs` 若提供，必須通過既有 Validator 且 cutoff 與主決策相同；所有 evidence ID 使用同一全域命名空間，碰撞時拒絕。

## MomentumResult

由 `MomentumEngine 1.0.0` 計算，不由 Agent 填值。每檔至少 61 筆才可標記 `available`，計算 5／20／60 日報酬、MA20／MA60、ATR14、下行波動、20 日平均量與成交值。

市場 regime 使用可用股票的 MA20 與 MA60 breadth：兩者皆不低於 60% 為 `bull`，皆不高於 40% 為 `bear`，其餘為 `neutral`。可用覆蓋低於 80% 時狀態是 `unavailable`、`regime=null`，不補猜。

## 每日鏈 artifact 與 position_sizing（schema 2.0）

每日決策鏈為分析團隊 → 多頭／空頭研究員 → 交易 Agent → 風險 Agent，由 `DailyDecisionPipeline` 逐批執行，各角色欄位見對應 Skill。Decision run 的 artifact 為 `momentum`、`debate`（`ResearchDebateBundle` 2.0）、`intent`（`TradeDecision` 2.0）、`policy`、`proposal`、`scenario`、`guard`、`risk_review`、`revision_history`、`decision` 與 `team_inputs`（版本沿革與各版內容見 `AGENTS.md`）；`DecisionFinalizer` 與 `DecisionResultValidator` 必須取得 `team_inputs`，重建多空辯論、交易決策與現金姿態並核對 policy `position_sizing` 綁定的 `conviction_by_symbol`、`cash_stance`、`position_breadth`、`sizing_plan_id`／`sizing_plan_sha256`；缺少即拒絕。舊版 1.0 鏈已移除，不再支援重建。

`apply_trade_decision` 把交易決策與現金姿態綁入 policy 並設定 `cash_buffer_rate`（`cash_buffer_by_stance` 對應值；啟用 `cash_ratio_range` 時改為 `grid[cash_stance][cash_grade]`，Validator 重算今日可選格點並核對）。`cash_stance` 有 `level`、`breadth`、`rationale`、`evidence_ids`（閘門開啟時另有 `cash_grade`），證據須存在於共同輸入；`typed_evidence_v1` 下至少一個市場層級證據，全市場情緒 `negative` 時不得 `aggressive`。配置時候選依信心等級、名次（或支持數）、`selection_tiebreak`、代號排序，取到該廣度的檔數；原始分數為等級乘數 ÷ max(ATR14%, floor)（`conviction_volatility_v2` 改為 ÷ max(ATR14%, floor) 的 `volatility_exponent` 次方），按比例分配 `1 − cash_buffer_rate − 非候選持股權重`，超過個股上限者固定在上限並重新分配餘額。配置引擎只讀取 TradeDecision 的 symbol／intent。

## DecisionPolicy 與 ProposalBundle

`DecisionPolicy` 是獨立、版本化且在 cutoff 前可得的策略／執行設定。可調策略包含現金緩衝、預設目標權重、減碼比例、滑價、換手與壓力情境；手續費、交易稅、最低費用、交易單位、個股／產業／持股檔數／現金限制及賣款可否重用是硬規則，Policy 的重複欄位必須與 `DecisionInputBundle.rules` 完全一致。`lot_size` 固定為 `1000`（一張），`OrderProposal` 同時輸出 `lots` 與 `shares=lots*1000`，不產生零股單。必要欄位（`content_sha256` 使用移除自身欄位後的 canonical JSON SHA-256）：

```json
{
  "schema_version": "1.0",
  "policy_id": "policy-id",
  "available_at": "2026-09-21T08:00:00+08:00",
  "currency": "TWD",
  "cash_buffer_rate": "0.05",
  "default_target_weight": "0.04",
  "trim_fraction": "0.50",
  "commission_rate": "0.001425",
  "sell_tax_rate": "0.003",
  "minimum_commission": "20",
  "lot_size": 1000,
  "slippage_bps": "10",
  "max_turnover_rate": "0.30",
  "max_stock_weight": "0.10",
  "special_weight_limits": {"2330.TW": "0.25"},
  "max_sector_weight": "0.30",
  "sector_classification_version": "version-id",
  "sector_by_symbol": {"2330.TW": "半導體"},
  "min_positions": 20,
  "max_positions": 30,
  "cash_weight_ceiling": "0.25",
  "minimum_active_share": "0",
  "stress_price_decline_rate": "0.10",
  "stress_slippage_multiplier": "2",
  "liquidity_fill_rate": "1",
  "reuse_sell_proceeds": false,
  "max_revisions": 3,
  "content_sha256": "canonical SHA-256"
}
```

`ProposalBundle` 同時保存 `allocation_proposal` 與 `order_proposal`：先執行退出／減碼，再依代號處理買進／加碼；以 Decimal 計算價格、費稅與現金，依一張向下取整；現金不足、未滿一張或持股檔數已滿時保存 `constraint_flags`。程式風控（`RISK_STOCK_WEIGHT`、`RISK_SECTOR_WEIGHT`、`RISK_NOT_TRADABLE`、`RISK_MIN_POSITIONS`、`RISK_ACTIVE_SHARE` 等）保留交易 Agent 的原始意圖並記錄減碼或取消，細節見 `AGENTS.md`。內容必須可由原始帳戶、cutoff 行情、TradeDecision 與 DecisionPolicy 完整重算。

## ScenarioResult、GuardResult 與 RiskReview

`ScenarioResult` 明確標記情境假設（基準、價格下跌、流動性壓力），每個情境從 cutoff 帳戶重建：成交率以張為單位向下取整，滑價獨立套用，逐筆重算成交價、費稅、現金、持股、NAV 與未成交張數。`GuardResult` 對提案及每個情境檢查交易池、可交易狀態、持股數、現金／買力、個股與產業權重、換手、強制退出流動性與必備基準的 Active Share；已持有、本次沒有買單的股票若之後被限制交易，Guard 以 `HELD_NOT_TRADABLE`（`passed=true` 的警告）記錄，不否決整份決策。

`RiskReview` 的欄位、allowlist 與數值限制見 [審查輸出](risk-review-output.md)；`RiskReviewValidator` 以同一套規則重建驗證。

## RevisionHistory、DecisionResult 與保存

`RevisionHistory` 從 revision 0 保存每一版完整 `ProposalBundle`、`ScenarioResult`、`GuardResult` 與 `RiskReview`。Validator 由基礎 Policy 重建初版，再按前一版已驗證的 allowlist actions 重建下一版，檢查連續序號、parent proposal、單調降風險及最多三次修正；最後一版不得為 `revise`。

完整 Validator 重建後才產生 `DecisionResult`：Risk approve、Guard passed 且存在訂單時為 `approved`；通過相同檢查但沒有訂單時為 `no_trade`；其他為 `rejected`（最終 `orders` 必須為空）。`DecisionRepository` 以安全白名單 run ID／artifact 名稱原子保存所有版本與 SHA-256 manifest；缺檔、額外檔案、路徑穿越、內容竄改或相同 ID 不同內容均拒絕。這些結果只供回測與人工檢查，不代表已下單或正式送件。

## CLI（`cli/portfolio_decision.py`）

每日流程由 `DailyDecisionPipeline` 直接呼叫 Python；CLI 提供同一批確定性工具供人工重建與除錯（`build-input`、`validate-input`、`compute-momentum`、`build-policy`、`validate-intent`、`compute-proposal`、`compute-scenarios`、`compute-guard`、`validate-risk`、`revise-proposal`、`build-history`／`append-history`、`validate-history`、`finalize`、`validate-decision`、`save-run` 等）。`finalize`、`validate-decision` 與 `save-run` 必須附 `--team-inputs`。Exit code `0` 表示成功或驗證通過，`2` 表示 artifact 已讀取但驗證失敗，`1` 表示檔案或工具錯誤。
