# Portfolio Decision P0-P2 契約

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
- `account_snapshot` 必須有 `account_id`、`available_at`、`source_evidence_id`、`cash`、`nav` 與不重複的正股數持股。
- `rules` 必須保存版本、可得時間及設定檔雜湊。
- `benchmarks` 至少一筆；每筆保存 `benchmark_id`、版本、可得時間及成分權重。
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
