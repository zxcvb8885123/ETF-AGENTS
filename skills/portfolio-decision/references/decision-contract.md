# Portfolio Decision P0-P6 契約

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
- `bundle_sha256` 鎖定整份輸入內容；頂層與帳戶、規則、基準、行情物件使用嚴格欄位白名單，不能夾帶 peer packet 或執行指令。
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

## BuyIntentPacket 與 SellIntentPacket

共用 envelope：

```json
{
  "schema_version": "1.0",
  "packet_id": "unique-id",
  "role": "buy | sell",
  "bundle_id": "decision-input-001",
  "snapshot_id": "snapshot-id",
  "decision_cutoff": "2026-09-21T08:55:00+08:00",
  "bundle_hash": "DecisionInputBundle canonical SHA-256",
  "momentum_result_id": "momentum:id",
  "status": "completed | degraded | failed",
  "content_sha256": "排除自身欄位後的 artifact SHA-256",
  "dependencies": {
    "decision_bundle_id": "decision-input-001",
    "momentum_result_id": "momentum:id",
    "role_input_sha256": "deterministic role input SHA-256",
    "peer_packet_ids": []
  },
  "items": [],
  "errors": []
}
```

每個 item 都要有 `symbol`、`intent`、`rationale`、`status_reason`、`horizon`、`evidence_ids`、`risk_flags`、`invalidation_conditions` 與非空 `claims`。每個 claim 有全 packet 唯一的 `claim_id`、`text` 與 `evidence_ids`。

Buy intent：`buy`、`add`、`watch`、`exclude`。已持股只能用 `add`，未持股只能用 `buy`；`buy/add` 必須有可用 MomentumResult。

Sell intent：`hold`、`trim`、`exit`、`forced_exit`。另需 `thesis_status=intact|weakened|invalidated|unavailable`，而且必須剛好覆蓋全部目前持股。

主控以 `build-role-input` 建立兩份不含 peer packet 的輸入，並在分開的子 Agent 環境執行。兩個 packet 的 `role_input_sha256` 必須等於自己的確定性輸入，`peer_packet_ids` 必須是空陣列。這是可驗證的依賴約束；執行環境隔離仍由主控負責。

只有 `status=completed` 的 Buy／Sell packet 能進入裁決。所有 envelope、item、claim 與 dependencies 都採嚴格欄位白名單，未知欄位或配置／下單別名一律拒絕。

## TradeDebateBundle 與 TradeIntentResult

`TradeDebateBundle.packets` 必須剛好包含一個已驗證 Buy packet 與一個已驗證 Sell packet，兩者使用相同 bundle、hash、Snapshot、cutoff 與 MomentumResult。

Trade Adjudicator 對兩個 packet 的股票聯集逐檔產生結果：

```json
{
  "symbol": "2330.TW",
  "intent": "add",
  "rationale": "裁決理由",
  "status_reason": "交給後續配置層的狀態",
  "evidence_ids": ["existing-evidence-id"],
  "adopted_claim_ids": ["existing-claim-id"],
  "rejected_claim_ids": ["existing-claim-id"],
  "unresolved_questions": [],
  "invalidation_conditions": []
}
```

- 每個來源 claim 必須剛好被採納或否決，不得同時出現在兩邊。
- `buy/add/trim/exit/forced_exit` 必須採納至少一個相同方向的來源 claim，不能把全部支持 claim 否決後仍輸出可執行意圖。
- 結果不能增加股票、claim ID 或 evidence ID。
- 已持股不得裁決為 `buy`；未持股不得裁決為 `add/hold/trim/exit/forced_exit`。
- P0～P2 禁止任何權重、股數、費稅或訂單欄位。
- Buy／Sell packet、TradeDebateBundle 與 TradeIntentResult 都保存 `content_sha256`；內容被改寫但 hash 未更新時拒絕。

## SizingPlan 與 position_sizing

`DecisionPolicy.position_sizing` 為選配：`method=conviction_volatility_v1`、`conviction_multipliers`（剛好 `high`／`medium`／`low`，皆大於 0 且 high ≥ medium ≥ low）與 `volatility_floor`（0～1）。啟用時必須先以 Portfolio Risk 的 `SizingPlan` 對 `TradeIntentResult` 全部 buy／add 候選分級，`apply-sizing` 再把 `conviction_by_symbol`、`sizing_plan_id` 與 `sizing_plan_sha256` 綁入新版 policy（`policy_id` 加上 plan ID，重算 `content_sha256`）；未綁定就執行配置時停止。

`SizingPlan` 的 item 只有 `symbol`、`conviction`、`rationale`、`evidence_ids`，禁止權重／股數等欄位；證據必須屬於該股票。配置時候選依等級再依代號排序，受 `max_positions` 限制；原始分數為等級乘數 ÷ max(ATR14%, floor)，按比例分配 `1 − cash_buffer_rate − 非候選持股權重`，超過個股上限者固定在上限並把餘額重新分配。Proposal 另存 `position_sizing.target_weights` 供重算；未啟用 `position_sizing` 時沿用 `default_target_weight` 等權重。

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

`ProposalBundle` 同時保存 `allocation_proposal` 與 `order_proposal`。配置工具先執行退出／減碼，再依股票代號排序處理買進／加碼；以 Decimal 計算價格、費稅與現金，依一張 1,000 股向下取整。現金不足、未滿一張或持股檔數已滿時保存 `constraint_flags`，不能填補股數。內容必須可由原始帳戶、cutoff 行情、TradeIntentResult 與 DecisionPolicy 完整重算。

## ScenarioResult、GuardResult 與 RiskReview

`ScenarioResult` 明確標記情境假設，包含基準、價格下跌及流動性壓力。每個情境從 cutoff 帳戶重建：成交率以張為單位向下取整，滑價獨立套用，逐筆重算成交價、費稅、可用現金、總現金、持股、收盤估值、NAV 與未成交張數；不能從「假設全部成交」的配置直接乘跌幅。`GuardResult` 對提案及每個情境檢查交易池、明確可交易狀態、持股數、現金／買力、個股與產業權重、換手、強制退出流動性；若規則另有明確基準要求，再檢查每一份必備基準的 Active Share。

Portfolio Risk Agent 只輸出 `RiskReview`：

- `decision` 限定 `approve`、`revise`、`reject`。
- Guard 失敗是硬性失敗，RiskReview 只能 `reject`，不得 `approve` 或用修正繞過。
- `revise` 只允許 `remove_candidate`、`increase_cash_buffer`、`reduce_max_stock_weight`、`reduce_turnover_limit`，且不能提高風險。
- 修正序號必須連續且最多三次；每次重新建立 Proposal、Scenario、Guard 與 RiskReview。
- `evidence_ids` 必須來自共同輸入；未知欄位或權重／股數等手寫結果一律拒絕。

## RevisionHistory、DecisionResult 與保存

`RevisionHistory` 從 revision 0 保存每一版完整 `ProposalBundle`、`ScenarioResult`、`GuardResult` 與 `RiskReview`。Validator 由基礎 Policy 重建初版，再按前一版已驗證的 allowlist actions 重建下一版，檢查連續序號、parent proposal、單調降風險及最多三次修正。最終輸入必須等於 history 最後一版，最後一版不得為 `revise`。

完整 Validator 重建 Proposal、Scenario、Guard 與 RiskReview 後才產生 `DecisionResult`。Risk approve、Guard passed 且存在訂單時為 `approved`；通過相同檢查但沒有訂單時為 `no_trade`；其他情況為 `rejected`。拒絕結果的最終 `orders` 必須為空。

`DecisionRepository` 以安全白名單 run ID／artifact 名稱原子保存所有版本與 SHA-256 manifest。相同 run ID 重試前會重新讀取每個實際檔案並核對 manifest；缺檔、額外檔案、路徑穿越、內容竄改或相同 ID 不同內容均拒絕。這些結果只供回測與人工檢查，不代表已下單或正式送件。
