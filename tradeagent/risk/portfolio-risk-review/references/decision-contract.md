# 決策層契約（DecisionInputBundle 至 DecisionResult）

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

- `snapshot_sha256` 使用排序 key、無多餘空白的 canonical JSON 計算。
- `bundle_sha256` 鎖定整份輸入內容；頂層與帳戶、規則、基準、行情物件使用嚴格欄位白名單，不能夾帶未列入契約的欄位或執行指令。
- `account_snapshot` 必須有 `account_id`、`available_at`、`valuation_at`、`source_evidence_id`、`cash`、`settled_cash`、`unsettled_cash`、`nav` 與不重複的正股數持股。`cash=settled_cash+unsettled_cash`，NAV 必須在規則容許誤差內等於 cutoff 行情重算值；配置只能使用 settled cash。
- `rules` 必須保存來源 URL、發布／可得時間、不可放寬限制及排除自身欄位後的 `config_sha256`。`required_benchmark_ids` 可為空；只有明確啟用有來源的基準約束時才列 ID，所列基準缺漏即拒絕。
- `benchmarks` 可為空；若提供，每筆保存 `benchmark_id`、版本、可得時間及成分權重。空必備基準時 `minimum_active_share` 必須為 0。
- `price_series` 必須覆蓋 Snapshot 全交易池，每個序列以 `series_sha256` 鎖定並依日期嚴格遞增；最後交易日、收盤價與證據必須等於 Snapshot 最新行情。
- `research_results` 若提供，必須通過既有 ResearchResult 2.1 validator。
- `perception_inputs` 若提供，必須成對包含資料 bundle 與結果，兩者 cutoff 必須和主決策完全相同，並通過既有授權與重算 validator。
- Snapshot、帳戶與所有 Perception bundle 的 evidence ID 使用同一全域命名空間，碰撞時拒絕。

## MomentumResult

由 `MomentumEngine 1.0.0` 計算，不由 Agent 填值。每檔至少 61 筆才可標記 `available`，計算 5／20／60 日報酬、MA20／MA60、ATR14、下行波動、20 日平均量與成交值。

市場 regime 使用可用股票的 MA20 與 MA60 breadth：兩者皆不低於 60% 為 `bull`，皆不高於 40% 為 `bear`，其餘為 `neutral`。可用覆蓋低於 80% 時狀態是 `unavailable`、`regime=null`，不得補猜。

## ResearchDebateBundle、TradeDecision 與 position_sizing（schema 2.0）

每日決策鏈為分析團隊 → 多空研究員 → 交易 Agent → 風險 Agent，由 `DailyDecisionPipeline` 逐批執行並以下列 Validator 重建驗證：`AnalystReportValidator`、`StancePacketValidator`／`ResearchDebateBundleValidator`、`TradeDecisionValidator`。各角色欄位見對應 Skill（`technical-analyst`、`fundamental-analyst`、`event-analyst`、`bull-researcher`、`bear-researcher`、`trader`）。

Decision run 的 artifact 為 `momentum`、`debate`（`ResearchDebateBundle` 2.0，多頭與空頭 `StancePacket`）、`intent`（`TradeDecision` 2.0）、`policy`、`proposal`、`scenario`、`guard`、`risk_review`、`revision_history`、`decision` 與 `team_inputs`（新 2.1 為 `schema_version`、三份 `analyst_reports`、`cash_stance`；舊 2.0 保留三／四份分析報告及 `research_result`）。`DecisionFinalizer` 與 `DecisionResultValidator` 必須取得 `team_inputs`，重建多空辯論、交易決策與現金姿態，並檢查 policy `position_sizing` 的 `conviction_by_symbol`、`cash_stance`、`sizing_plan_id`／`sizing_plan_sha256` 與交易決策、現金姿態一致；`debate.schema_version` 不是 2.0 或缺少 `team_inputs` 一律拒絕。DecisionResult 含 `team_inputs_sha256`。舊版 1.0 的 Buy／Sell／Trade Adjudicator 鏈已移除，不再支援重建。

`DecisionPolicy.position_sizing`：`method=conviction_volatility_v1`、`conviction_multipliers`（剛好 `high`／`medium`／`low`，皆大於 0 且 high ≥ medium ≥ low）、`volatility_floor`（0～1）與 `cash_buffer_by_stance`（剛好 `aggressive`／`neutral`／`defensive`，皆 ≥0 且低於 `cash_weight_ceiling`，依序不遞減）。`apply_trade_decision` 把 `conviction_by_symbol`、`cash_stance`、`sizing_plan_id`（沿用欄位名，值為 `TradeDecision.decision_id`）與 `sizing_plan_sha256`（`TradeDecision.content_sha256`）綁入新版 policy，並將 `cash_buffer_rate` 設為該姿態對應值（Validator 檢查兩者一致；`policy_id` 加上 decision ID，重算 `content_sha256`）；未綁定就執行配置時停止。

`cash_stance` 只有 `level`、`rationale`、`evidence_ids`，證據須存在於共同輸入；Agent 不輸出任何百分比。配置時候選依等級再依代號排序，受 `max_positions` 限制；原始分數為等級乘數 ÷ max(ATR14%, floor)，按比例分配 `1 − cash_buffer_rate − 非候選持股權重`，超過個股上限者固定在上限並把餘額重新分配。分級配置封頂時以個股上限 × 0.995 計算，保留手續費造成成交後 NAV 變小的緩衝。Proposal 另存 `position_sizing.target_weights` 供重算。配置引擎只讀取 TradeDecision 的 symbol／intent。

## DecisionPolicy 與 ProposalBundle

`DecisionPolicy` 是獨立、版本化且在 cutoff 前可得的策略／執行設定。可調策略包含現金緩衝、預設目標權重、減碼比例、滑價、換手與壓力情境；手續費、交易稅、最低費用、交易單位、個股／產業／持股檔數／現金限制及賣款可否重用是硬規則；Active Share 僅在有來源規則明確要求時才成為額外約束，Policy 的重複欄位必須與 `DecisionInputBundle.rules` 完全一致，不能藉 Policy 放寬。所有比率必須在允許範圍，並以 `content_sha256` 綁定內容。

第一版 `lot_size` 必須固定為 `1000`，也就是一張。`OrderProposal` 同時輸出 `lots` 與 `shares=lots*1000`；不產生零股單。帳戶既有持股若不是 1,000 股的整數倍，配置流程停止並要求先提供明確的零股處理政策。

必要欄位如下；`content_sha256` 使用移除自身欄位後的 canonical JSON SHA-256：

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
  "liquidity_fill_rate": "0.50",
  "reuse_sell_proceeds": false,
  "max_revisions": 3,
  "content_sha256": "canonical SHA-256"
}
```

`ProposalBundle` 同時保存 `allocation_proposal` 與 `order_proposal`。配置工具先執行退出／減碼，再依股票代號排序處理買進／加碼；以 Decimal 計算價格、費稅與現金，依一張 1,000 股向下取整。現金不足、未滿一張或持股檔數已滿時保存 `constraint_flags`，不能填補股數。內容必須可由原始帳戶、cutoff 行情、TradeDecision 與 DecisionPolicy 完整重算。

## ScenarioResult、GuardResult 與 RiskReview

`ScenarioResult` 明確標記情境假設，包含基準、價格下跌及流動性壓力。每個情境從 cutoff 帳戶重建：成交率以張為單位向下取整，滑價獨立套用，逐筆重算成交價、費稅、可用現金、總現金、持股、收盤估值、NAV 與未成交張數；不能從「假設全部成交」的配置直接乘跌幅。`GuardResult` 對提案及每個情境檢查交易池、明確可交易狀態、持股數、現金／買力、個股與產業權重、換手、強制退出流動性；已持有、本次沒有 buy 訂單的股票若之後被限制交易（例如新公告處置），只能續抱、不能買賣，Guard 以 `HELD_NOT_TRADABLE`（`passed=true` 的警告）與情境的 `held_not_tradable` 記錄，不否決整份決策；買進或賣出受限股票仍由 `TRADABLE`／`SCENARIOS` 否決，且沒有續抱的受限持股時輸出與舊版完全相同；若規則另有明確基準要求，再檢查每一份必備基準的 Active Share。

風險 Agent 只輸出 `RiskReview`：

- `decision` 限定 `approve`、`revise`、`reject`。
- Guard 失敗是硬性失敗，RiskReview 只能 `reject`，不得 `approve` 或用修正繞過。
- `revise` 只允許 `remove_candidate`、`increase_cash_buffer`、`reduce_max_stock_weight`、`reduce_turnover_limit`，且不能提高風險。
- 修正序號必須連續且最多三次；每次重新建立 Proposal、Scenario、Guard 與 RiskReview。
- `evidence_ids` 必須來自共同輸入；未知欄位或權重／股數等手寫結果一律拒絕。

## RevisionHistory、DecisionResult 與保存

`RevisionHistory` 從 revision 0 保存每一版完整 `ProposalBundle`、`ScenarioResult`、`GuardResult` 與 `RiskReview`。Validator 由基礎 Policy 重建初版，再按前一版已驗證的 allowlist actions 重建下一版，檢查連續序號、parent proposal、單調降風險及最多三次修正。最終輸入必須等於 history 最後一版，最後一版不得為 `revise`。

完整 Validator 重建 Proposal、Scenario、Guard 與 RiskReview 後才產生 `DecisionResult`。Risk approve、Guard passed 且存在訂單時為 `approved`；通過相同檢查但沒有訂單時為 `no_trade`；其他情況為 `rejected`。拒絕結果的最終 `orders` 必須為空。

`DecisionRepository` 以安全白名單 run ID／artifact 名稱原子保存所有版本與 SHA-256 manifest。相同 run ID 重試前會重新讀取每個實際檔案並核對 manifest；缺檔、額外檔案、路徑穿越、內容竄改或相同 ID 不同內容均拒絕。這些結果只供回測與人工檢查，不代表已下單或正式送件。

## CLI（`cli/portfolio_decision.py`）

每日流程由 `DailyDecisionPipeline` 直接呼叫 Python；CLI 提供同一批確定性工具供人工重建與除錯：`build-input`、`validate-input`、`compute-momentum`、`validate-momentum`、`seal-artifact`、`build-policy`、`validate-intent`（驗證 `TradeDecision`）、`compute-proposal`、`validate-proposal`、`compute-scenarios`、`compute-guard`、`validate-risk`、`revise-proposal`、`build-history`／`append-history`、`validate-history`、`finalize`、`validate-decision` 與 `save-run`。`finalize`、`validate-decision` 與 `save-run` 必須附 `--team-inputs`。Exit code `0` 表示成功或驗證通過，`2` 表示 artifact 已讀取但驗證失敗，`1` 表示檔案或工具錯誤。
