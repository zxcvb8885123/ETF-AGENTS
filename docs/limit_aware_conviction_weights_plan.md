# 依單股上限放大的信心權重：設計計劃

狀態：**已實作（2026-11-05，採文末「α 方案」而非第 5 節的 C），正式設定尚未開啟**。本檔不改任何程式或設定；以下「事實」附出處，「推論／估算」另行標示。所有用 2026-11-05 封存輸入重算的數字都是 `evidence_grade: replay_reference_only`：只固定已封存的 Agent 意圖重算確定性程式，不是對 LLM 判斷的前瞻驗證。

路徑說明：原請求為 `docs/plans/limit-aware-conviction-weights.md`，但 `docs/` 下沒有 `plans/` 子目錄，既有計劃檔慣例是 `docs/<snake_case>_plan.md`（例：`margin_level_signal_plan.md`、`fundamental_growth_signal_plan.md`），故改為本路徑。

## 1. 問題

比賽規定一般股單股上限 10%、台積電 25%（`policy.json` 的 `max_stock_weight: "0.1"`、`special_weight_limits: {"2330.TW": "0.25"}`，由 `bundle.rules` 綁定，見 `src/etf_agent/decision/policy_builder.py:11-24`）。但目前的份額只看信心乘數，單股上限只用來封頂，所以 2330 的上限從 10% 放寬到 25% 對配置沒有任何作用。

事實（`artifacts/daily/2026-11-05/daily-20261105-070000/`）：

- `policy.json`：`conviction_by_symbol` 中 2330.TW、6446.TW、1815.TWO 為 high；`rank_by_symbol` 2330.TW 在 high 排第 1；`cash_stance: defensive`（緩衝 0.20）；`volatility_exponent: "0"`；乘數 high 1.5／medium 0.6／low 0.3。
- `proposal.json` 的 `position_sizing.target_weights`：2330.TW `0.04984226`、6446.TW `0.04984226`，其餘 13 檔中信心各 `0.01993690`。
- 2330.TW 目前持有 22,000 股 × 2,260 = 49.72M，權重 5.115%（`allocation_proposal.positions`）> 目標 4.98%，所以沒有加碼委託。
- 實際委託只有 2890.TW 賣 302,000 股（trim）、1802.TW 買 188,000 股（add）；6446.TW 被 `RISK_CASH_FIT` 整筆取消。

Agent 只輸出 high／medium／low，不輸出數字，這點不變。

## 2. 現況演算法（事實）

### 2.1 `_conviction_targets`（`src/etf_agent/decision/allocation.py:905-1007`）

1. 依信心、名次排好候選（`_ordered_conviction_candidates`，931 行），依廣度目標、rotation 名額篩出 `eligible`（957-981 行）。
2. `held_outside`＝**本次出場／減碼後**仍持有、但不在 `eligible` 的股票權重合計（982-988 行，用 `target_shares`，所以已扣掉 trim）。
3. `budget = 1 − cash_buffer_rate − held_outside`（989 行）。
4. 呼叫 `conviction_weights`（990-1006 行），封頂 `limits[s] = _weight_limit(s, effective) × _headroom(effective, buying=True)`（1000 行）；`stock_limit_headroom: v1` 時 headroom 為 0.97（`sizing.py:66-67`、`allocation.py:1072-1079`）。
5. `_weight_limit`（1066-1070 行）：在 `special_weight_limits` 內的股票取特殊上限，其餘取 `effective["max_stock_weight"]`（修正循環可覆寫，`allocation.py:857-866、901-902`）。

### 2.2 `conviction_weights`（`src/etf_agent/decision/sizing.py:516-555`）

- `raw[s] = multiplier[tier[s]] / max(ATR%, floor) ** exponent`；`exponent=None` 走 v1 原式、`0` 直接回乘數（531-538 行）。
- 水位填充（water-filling）：`proposed[s] = left × raw[s] / Σraw(remaining)`；超過 `limits[s]` 的固定為上限、從 `left` 扣掉、移出 remaining，重算直到沒有超限（543-554 行）。
- 預算全部分完（除非全員封頂）；**上限只在 proposed 超過時才出現，不影響份額大小**。

### 2.3 份額之後（事實）

- `allocation.py:605-607`：`desired = 目標股數`，已持有者 `desired = max(current, desired)`。**加碼永遠不賣**：目前權重高於目標的入選持股保留原部位，超出的部分不會讓出預算。
- 接著 `_fit_cash`（1461 行起）：`cash_fit_order: v1` 時買單依（信心、名次、代號）取得現金；`cash_buffer_floor: v1` 時緩衝取 `min(NAV × 緩衝, starting_cash)`（1491-1494 行），現金已低於緩衝時只剩本次賣款可用；新股殘量低於 `cash_fit_min_fill_ratio` 整筆取消（1550-1562 行）。
- 之後依序 `_fit_stock_weights`、`_fit_sector_weights`、`_fit_turnover`（627-636 行），最後 `run()` 的 Active Share 迴圈（253-273 行）。

`conviction_weights` 只有 `allocation.py:990` 一處呼叫；`target_weights` 只寫在 proposal（`allocation.py:713`），其他地方（`reporting/contracts.py:23`）只把它列為研究報告禁止欄位（`grep -rn "conviction_weights\|target_weights" src cli` 的結果）。

## 3. 2026-11-05 的實際瓶頸：現金，不是目標權重（核心事實）

用封存的 `decision_input.json`、`policy.json`、`intent.json` 重跑 `AllocationOrderEngine`，原版重算的 `content_sha256` 與 `proposal.json` 相同（重放可信）。再把 `allocation.conviction_weights` 換成各候選公式重跑（`replay_reference_only`）：

| 數值（事實，重算） | 值 |
| --- | --- |
| NAV | 972,055,541 |
| 起始現金比例 | 18.93%（低於 defensive 緩衝 20%） |
| `held_outside`（扣 2890 trim 後） | 0.441136 |
| `budget` | 0.358864 |
| 15 檔入選股目前權重合計 | 約 0.3562 |
| 可用於買單的現金 | 約 12.96M（只有 2890.TW 的賣款） |

入選股中 2412.TW（6.9%）、4904.TW（5.7%）、2886.TW（4.8%）、3045.TW（4.1%）、2880.TW（3.9%）、2801.TW（3.7%）都已高於目標，但因 `max(current, desired)` 不賣，預算 0.359 幾乎被既有部位吃光。**任何只改份額的公式，在 11-05 只能改變「誰先拿到那 12.96M」**。

| 公式（見第 4 節） | 2330 目標 | 6446 目標 | 一般中信心 | 目標合計 | 實際委託 | 2330 最終權重 |
| --- | --- | --- | --- | --- | --- | --- |
| 現況 | 0.0498 | 0.0498 | 0.0199 | 0.358864 | 2890 賣 302k、1802 買 188k；6446 `RISK_CASH_FIT` | 5.12% |
| A（k=1，全等級） | 0.1031 | 0.0412 | 0.0165 | 0.358864 | 2890 賣 302k、2330 買 5,000（11.31M）、1802 買 18,000；6446、5876、8415 `RISK_CASH_FIT` | 6.28% |
| B（k=0.5，全等級） | 0.0729 | 0.0461 | 0.0184 | 0.358864 | 同 A | 6.28% |
| C（k=1，只放大 high） | 0.1031 | 0.0412 | 0.0165 | 0.358864 | 同 A | 6.28% |

- 三案在 11-05 的委託完全相同：2330 是 high 第 1 名且已持有（不受塵埃取消），先拿走賣款買 5 張；1802 從 188 張縮為 18 張。
- A 與 C 在 11-05 數字相同，**只因為唯一有特殊上限的 2330 剛好是 high**，這不是兩案的實測差異，只是這筆資料沒有區分力。
- 預算守恆：三案目標合計都等於 `budget`（Decimal 尾差 < 1e-7），封頂未觸發（2330 上限 0.25×0.97=0.2425）。

### 3.1 現金不受限時的估算（推論，非重放）

用同樣 2 high（2330、6446）＋13 medium，只改預算（`conviction_weights` 直接計算）：

| 預算 | 現況 2330／中信心 | A 或 C：2330／6446／中信心 | B：2330／6446／中信心 |
| --- | --- | --- | --- |
| 0.359（11-05 defensive） | 0.050／0.020 | 0.103／0.041／0.016 | 0.073／0.046／0.018 |
| 0.529（同持股、aggressive 緩衝 0.03） | 0.074／0.029 | 0.152／0.061／0.024 | 0.108／0.068／0.027 |
| 0.700 | 0.097／0.039 | 0.201／0.081／0.032 | 0.142／0.090／0.036 |

**比例型公式不會讓 2330 逼近 25%**：在 15 檔共享預算時，A／C 約 10%～20%。要真的逼近上限需第 4 節的 D 類公式，代價是中信心股目標明顯下降，列為需人決定（第 11 節）。

## 4. 公式候選

符號：`m(t)` 為信心乘數；`L_i` 為該股的**規則層級**上限（`special_weight_limits[i]`，否則 `policy["max_stock_weight"]`，**不用 effective**）；`L_base = policy["max_stock_weight"]`；`s_i = L_i / L_base`。封頂與水位填充維持不變。

- **A：乘數 × 上限比（k=1，全等級）**：`raw_i = m(t_i) × s_i`。份額與上限成正比，一般股 `s=1` 完全不變。
- **B：乘數 × 上限比的 k 次方**：`raw_i = m(t_i) × s_i^k`，0 ≤ k ≤ 1；k=0 等於現況、k=1 等於 A。
- **C：只放大 high（k=1）**：`raw_i = m(t_i) × (s_i if t_i == high else 1)`。
- **D：高信心先填到上限的比例（優先填充，參考）**：high 先取 `min(cap_i, α × L_i)`，剩餘預算再依乘數分給其他等級；若 high 合計超過預算再等比縮。11-05 估算（α=0.6，推論）：2330 0.150、6446 0.060，13 檔中信心各約 0.0115（現況 0.0199）。

### 4.1 跨信心等級排序是否被打破（判別條件）

- A：低信心的 2330 為 0.3×2.5=0.75，高於一般中信心 0.6；中信心的 2330 為 1.5，等於一般 high。**等級排序被打破**。
- B：排序維持的條件是 `2.5^k < min(相鄰等級乘數比)`；目前乘數比為 0.6/0.3=2、1.5/0.6=2.5，需 k < ln2/ln2.5 ≈ 0.756。但 `conviction_multipliers` 是策略反思可調的旋鈕（`strategy_reflection/contracts.py` 的 KNOBS），**安全的 k 會隨乘數漂移**。
- C：只把最高等級往上放大，不論乘數怎麼調，跨等級排序都不會被打破；medium／low 的 2330 行為與現況完全相同。
- D：排序維持，但改變了「份額依乘數比例分配」的整體語意，一般股會明顯縮水，不符合「一般股維持接近現況」。

## 5. 推薦：C（只放大 high，k=1，只放大不縮小）

`raw_i = m(t_i) × max(1, L_i / L_base)`（僅 `t_i == high`；其他等級 `raw_i = m(t_i)`），由版本閘門 `position_sizing.limit_scaled_conviction: "v1"` 啟用，k=1 與「只放大 high」固定在 v1 語意內，不另設數值參數。

理由：

1. 直接回應人的判斷：高信心的台積電份額跟著上限放大；在 11-05 的預算下，目標從 4.98% 提高到 10.31%。
2. 一般股只做水位填充的等比縮減（11-05：0.0199 → 0.0165），彼此相對比例不變。
3. 不論策略反思怎麼調乘數，跨信心等級排序不會被打破（4.1 節），不需要第二個可漂移的參數。
4. `max(1, ·)`：上限比一般股低的特例（目前不存在）不會被縮小，封頂本來就會處理。

風險與交互作用（推論，附 11-05 估算）：

- **集中度**：2330 目標可到約 10%～20%（3.1 節）。仍由 `_weight_limit × 0.97` 封頂、`_fit_stock_weights` 與 Guard 守硬上限，不會超過 24.25%。
- **Active Share**（`minimum_active_share` 0.15）：以 11-05 配置後權重估算，把 2330 調到 10%／15%／24.25% 時，30 檔必備主動 ETF 的最低 Active Share 約 0.37～0.47（00984A 最低，0.3704 → 0.3948／0.4198／0.4661；對 2330 權重高的 00404A 從 0.5185 降到 0.4357）。距門檻仍遠；若觸發，`_active_share_drop` 會取消與 ETF 重疊最多的候選，**最可能就是 2330 本身**，這是要記錄的失效模式。
- **產業上限**（`max_sector_weight` 0.6，只擋增量）：2330 屬 industry:24，11-05 配置後該產業 12.4%，空間充足。
- **換手上限**（`max_turnover_rate` 0.3，`turnover_fit` v1）：份額集中到 2330 後單筆買單較大（11-05 A 案目標 22,000 股約 49.7M ≈ NAV 5%），仍遠低於 30%；`_fit_turnover` 依信心縮減，2330 為 high 排前面。
- **現金緩衝與 cash_fit 的連動**：`cash_fit_order` 讓 high 第 1 名先拿現金，2330 已持有不會被塵埃取消，所以現金不足時 2330 會吃掉大部分可用現金，排在後面的新股（6446、5876、8415）被 `RISK_CASH_FIT` 取消的機率上升（11-05 重放即是如此）。這是「高信心先買」的預期結果，但會讓新股更難進場。
- **預算不會因此增加**：瓶頸若是現金低於緩衝或既有超重持股（第 3 節），新公式只重新排序資金去向，不會讓總曝險提高。
- **單一高價股的整張限制**：2330 一張約 2.26M（NAV 的 0.23%），整張取整誤差可忽略。

## 6. 資料契約變更

### 6.1 新欄位

- `config/decision_policy.json` → `position_sizing.limit_scaled_conviction: "v1"`。
- `sizing.py` 新增常數 `LIMIT_SCALED_CONVICTION = "v1"`，加入：
  - `validate_position_sizing` 的 `reject_unknown_fields` 白名單（`sizing.py:355-368`）。
  - `_validate_execution_gates` 的版本表（`sizing.py:180-187`）：值不是 `"v1"` 即拒絕。
  - 額外檢查（fail-closed）：啟用時 `method` 必須是 `conviction_volatility_v2`（縮小組合矩陣）；`special_weight_limits` 值必須可解析為正 Decimal（沿用 `decimal_value`，錯誤即拒絕）。
- `conviction_weights` 新增選配參數 `limit_scale: Optional[Mapping[str, Decimal]] = None`，沿用 `volatility_exponent` 的先例（`sizing.py:524-538`）：`None` 時完全不經過乘法，保證舊 run 逐位元相同。`limit_scale` 由 `_conviction_targets` 依 tier 與規則層級上限計算後傳入（`conviction_weights` 保持不知道 tier 語意）。
- 基準用 `policy["max_stock_weight"]`，**不用 effective**：修正循環可把 `max_stock_weight` 覆寫調低，若以 effective 當基準，2330 的倍數會反而變大。

### 6.2 policy 綁定與雜湊

- 樣板 → `build_decision_policy`（`policy_builder.py:28-58`）→ `policy_base.json` → 綁定交易決策 → `policy.json`；`content_sha256 = decision_policy_sha256(policy)` 涵蓋整個 `position_sizing`，新欄位自動進入雜湊；proposal 的 `policy_hash` 綁住它。
- 不加入 `allocation.py:893-896` 的 effective 鍵組：它不是執行面參數，也不能被修正循環覆寫，放進 effective 只會讓 proposal 多一個鍵。改為在 proposal 的 `position_sizing` 區塊（`allocation.py:708-716`）**只在 policy 有此欄位時**加 `"limit_scaled_conviction": "v1"`，供報告與稽核辨識；舊 run 的區塊不變。

### 6.3 重建驗證

- `ProposalValidator`（`allocation.py:1597-1641`）以同一 policy 重跑 `AllocationOrderEngine` 並整份比對，Finalizer、`DecisionResultValidator`、`evaluation/replay.py:283` 都走同一引擎，不需新增比對邏輯。
- 舊 policy 沒有此欄位 → `limit_scale=None` → 原式，逐位元不變。

### 6.4 `team_inputs` 與舊 run

- 不升 `team_inputs` 版本：權重是在全部 Agent 輸入完成後才由程式依 policy 計算，Agent 輸入沒有新增或改變任何欄位。`portfolio_slots`（`decision/rotation.py`）只算名額，不讀權重。
- 交易 Agent 與風險 Agent 的 Skill、brief 都不變；不需告知 Agent 這個閘門。

## 7. 策略反思白名單

- `strategy_reflection/contracts.py` 的 `FORBIDDEN_PREFIXES`（`contracts.py:136` 起）加入 `"limit_scaled_conviction"`，與 `stock_limit_headroom`、`cash_fit_order` 等執行面閘門同列；不加入 `KNOBS`。
- `special_weight_limits`、`max_stock_weight` 本來就在禁區（硬性規則）。
- 已知連動：`conviction_multipliers.high` 是正式旋鈕，調高會同時放大 2330 的份額（推論）。C 案的放大倍數固定為上限比，不會讓反思多一個自由度，但 `risk_reducing` 方向判斷在此閘門啟用後更敏感，建議週報加註（不在本計劃範圍）。

## 8. 測試計劃（unittest.TestCase 風格）

`tests/decision/test_position_sizing.py` 新增 `LimitScaledConvictionTests`：

- `test_without_gate_weights_are_bitwise_unchanged`：同一輸入，無閘門與 `limit_scale=None` 的 `conviction_weights` 結果相等；proposal `content_sha256` 與改動前固定值相等。
- `test_gate_scales_high_conviction_by_rule_limit_ratio`：2330 high、一般股 high，2330 raw 為 2.5 倍。
- `test_gate_does_not_scale_medium_or_low`：medium／low 的 2330 與一般股同權。
- `test_cross_tier_order_is_preserved_under_any_multipliers`：數組乘數（含反思邊界值）下，任何 high 份額 ≥ 任何 medium ≥ 任何 low。
- `test_scaled_weight_is_capped_at_limit_with_headroom`：預算大、候選少時 2330 封頂於 0.25×0.97，超額依比例重分配。
- `test_budget_is_conserved`：目標合計等於 budget（Decimal 容差 1e-25，沿用 `VolatilityExponentTests.assertClose`）。
- `test_single_candidate`：只有 2330 一檔時取 `min(budget, cap)`；只有一般股一檔時與無閘門相同。
- `test_all_high_conviction`：全 high 時一般股彼此等權，2330 為 2.5 倍（或封頂）。
- `test_empty_special_limits_matches_gate_off`：閘門開啟但 `special_weight_limits` 為空時 `target_weights` 與閘門關閉相同。
- `test_override_of_max_stock_weight_does_not_change_scale`：修正循環覆寫調低 `max_stock_weight` 時，2330 的放大倍數仍以 policy 層級計算。
- `test_special_limit_below_base_is_not_shrunk`：`max(1, ·)` 生效。
- `test_proposal_records_gate_only_when_present`：proposal `position_sizing` 區塊在有閘門時才有新鍵；`ProposalValidator` 重建通過、竄改 `target_weights` 被拒。
- fail-closed：`test_policy_rejects_unknown_limit_scaled_version`（`"v2"`、`true`、`1`）、`test_policy_rejects_gate_with_v1_method`、`test_policy_rejects_unparseable_special_limit`。

其他檔案：

- `tests/decision/test_cash_fit_and_limits.py`：`ProductionConfigTests` 的 `ALL_GATES`（約 50 行）與 `test_gates_are_forbidden_for_strategy_reflection`（120 行）加入新欄位；`LegacyEffectivePolicyTests` 不變並應繼續通過（證明 effective 鍵未增加）。
- `tests/strategy_reflection/test_design_guards.py:35` 的閘門迴圈加入 `"limit_scaled_conviction"`。
- 端到端：`test_sized_policy_replays_through_guard_history_and_finalizer`（`test_position_sizing.py:129`）比照新增有閘門版本，確認 Guard、Finalizer 重建通過。

## 9. 需同步更新的文件（已 grep 確認段落）

- `docs/decision-details.md`：「低波動偏好」節（約 43-45 行）補一段份額與單股上限的關係；附錄名稱對照（約 113-135 行）加一列。
- `docs/architecture.md`：附錄閘門表（約 251-275 行）加一列。
- `docs/sizing_rotation_v2_design.md`：「權重：可調的低波動偏好」節（19 行起）與「向後相容」節（64 行起）。
- `src/etf_agent/decision/README.md`：`sizing.py` 列（53 行）、權重說明（87 行）、執行面版本閘門段（100 行）。
- `AGENTS.md`：「權重由 `conviction_volatility_v1`…」那條與 2026-11-03 起的閘門清單各補一句（屬 Agent 邊界與契約變更，規則要求同步）。
- `README.md`：55 行「分工」只說權重由 Python 計算，不需改；如有權重公式說明再補。
- `docs/README.md`：索引加本檔（後續事項，本次不改）。
- Skill（`tradeagent/risk/strategy-reflection/SKILL.md` 24-25 行）不需改旋鈕清單；只在禁區說明有列舉時補上。

## 10. 實作步驟、驗證、回滾

步驟：

1. `sizing.py`：常數、`conviction_weights` 的 `limit_scale` 參數、驗證（白名單、版本、method 前提）。
2. `allocation.py`：`_conviction_targets` 依 tier 與規則層級上限算 `limit_scale`（只在閘門存在時）；proposal `position_sizing` 區塊條件式加鍵。
3. `strategy_reflection/contracts.py`：`FORBIDDEN_PREFIXES`。
4. 測試（第 8 節），先寫舊 policy 逐位元不變的測試。
5. 用 11-05 封存輸入重放（`replay_reference_only`）確認舊 policy 的 `content_sha256` 不變、新閘門結果與第 3 節一致。
6. 文件（第 9 節）。
7. 最後才在 `config/decision_policy.json` 加 `"limit_scaled_conviction": "v1"`（由人決定啟用時點，第 11 節）。

驗證：

```bash
PYTHONPYCACHEPREFIX=/tmp/etf-agent-pycache \
  .venv/bin/python -m pytest

PYTHONPYCACHEPREFIX=/tmp/etf-agent-pycache \
  .venv/bin/python -m compileall -q src tradeagent tests

git diff --check
```

回滾：從 `config/decision_policy.json` 移除該欄位即回到原行為；已封存的新 run 因 policy 內含欄位，仍以新式重建通過，不需改封存。程式路徑不可移除（移除會讓已封存的新 run 無法重建）。

## 11. 需要人決定的事項

1. **公式與範圍**：採 C（只放大 high）或 A／B（全等級）；B 的 k 值（若採 B，需 < 0.756 才在目前乘數下維持排序，且會隨反思調乘數漂移）。
2. **目標強度**：比例型公式在 15 檔共享預算下 2330 約 10%～20%，無法逼近 25%。若要逼近上限，需改採 D 類（high 先填到 α × 上限），並接受一般中信心股目標明顯下降（11-05 估算 0.0199 → 0.0115）；α 值需人定。
3. **low／medium 是否也放大**：C 案不放大；若人認為「上限大」本身就該多配，改採 A 並接受等級排序被打破。
4. **上限比低於基準時**是否 `max(1, ·)`（推薦）或照比例縮小。
5. **啟用時點**：比賽進行中改配置語意，是否先只在影子重放觀察、或直接在某個交易日起啟用。
6. **與現金瓶頸的先後**：第 3 節顯示 11-05 的限制來自現金低於緩衝與既有超重持股不讓出預算；若人期待 2330 實際權重明顯上升，可能要先處理第 12 節的問題，本計劃單獨上線的實際效果有限。

## 12. 相關但不在本計劃範圍的已知問題（只描述）

- **現金低於姿態緩衝時，高信心新股被整筆取消、排名較後的反而成交**：11-05 起始現金 18.93% 低於 defensive 緩衝 20%，`cash_buffer_floor: v1` 使緩衝只放行本次賣款（約 12.96M，`allocation.py:1491-1494`）。6446.TW（high、第 2 名，價約 1,250，目標 38,000 股約 47.5M）只買得到 10,000 股，低於 `cash_fit_min_fill_ratio` 0.5，被 `RISK_CASH_FIT` 整筆取消（`allocation.py:1550-1562`）；釋出的現金給後面的 1802.TW（medium、第 2 名，已持有，只減量不取消），結果 1802 買進 188,000 股。
- **現金為何掉到 20% 以下尚未查證**（可能與先前成交、價格變動或回撤減碼有關，需另查帳本）。
- **加碼永不賣造成的虛擬預算**：入選持股超過目標時保留原部位（`allocation.py:606-607`），使預算在帳面守恆、實際曝險卻高於預算；這同時削弱任何份額公式的效果（第 3 節）。

## 附錄：名稱對照

| 白話說明 | 程式名稱 |
| --- | --- |
| 高信心份額至少為 alpha × 單股上限（已實作、未開啟，見「α 方案」） | `position_sizing.limit_scaled_conviction: {"version": "v1", "alpha": …}`、`sizing.alpha_floor_weights` |
| 份額計算 | `sizing.conviction_weights`、`allocation._conviction_targets` |
| 單股上限 | `max_stock_weight`、`special_weight_limits`、`_weight_limit` |
| 有買單股票留 3% 上限空間 | `stock_limit_headroom`、`LIMIT_HEADROOM_BUY` |
| 現金不足依信心分錢、零頭取消 | `cash_fit_order`、`cash_fit_min_fill_ratio`、旗標 `RISK_CASH_FIT` |
| 現金緩衝只放行賣款 | `cash_buffer_floor` |
| Active Share 重算 | 旗標 `RISK_ACTIVE_SHARE` |

## α 方案（2026-11-05 人決定採用，已實作；正式設定尚未開啟）

人的決定：不採第 5 節的 C，改採第 4 節 D 類「高信心填到單股上限的 α 比例」；α 寫成 Policy 參數、由人設定；同等級內沿用 `selection_ranking` 名次；不新增信心等級、不改 Agent 輸出契約。以下是實作的精確定義（`sizing.alpha_floor_weights`，由 `allocation._conviction_targets` 呼叫）。

### 欄位與前提

```json
"limit_scaled_conviction": {"version": "v1", "alpha": "0.6"}
```

- `alpha` 為**必填**數字字串，0 < α ≤ 1；程式不提供預設值（0.6 只是建議值，寫在樣板時才生效）。
- 前提：`method` 為 `conviction_volatility_v2`、`selection_ranking: "v1"`（同等級依名次）。缺任一即 fail-closed。
- 版本閘門：只由人設定，策略反思禁區（`FORBIDDEN_PREFIXES` 含 `limit_scaled_conviction`）。不加入 effective policy、也不在修正循環可覆寫的鍵內；proposal 的 `position_sizing` 區塊只在開啟時多記 `{"version": "v1", "alpha": "0.60000000"}`，`content_sha256` 因此綁住 α。

### 公式

符號：`E` = 入選股（已依信心、名次、代號排序），`H ⊂ E` = high；`cap_i = L_i × headroom`（`_weight_limit × _headroom`，與原式相同的封頂）；下限 `f_i = min(α × L_i, cap_i)`（只對 `i ∈ H`）；`B` = 原本的預算（`1 − 緩衝 − held_outside`，不變）。

0. **原式與必要份額**：先以原式 `conviction_weights`（同乘數、同 `volatility_exponent`、同封頂）把 `B` 分給全部入選股，得 `base_i`；每檔 high 的必要份額 `r_i = max(base_i, f_i)`。
1. **α 不起作用**：沒有 high 被抬高（全部 `r_i = base_i`），且（`volatility_exponent = 0` 時）`base` 已滿足 high ≥ medium ≥ low：直接回傳 `base`，與原式逐位相同。
2. **預算不足**（`Σ_{i∈H} r_i ≥ B`）：依名次逐檔 `w_i = min(r_i, 剩餘)`，用完為止；後面的 high 與全部 medium／low 為 0。這就是「名次靠前先滿足、預算用完為止」。
3. **預算足夠**：每檔 high 固定為 `r_i`。被抬高的 high 拿到 `f_i`，其他 high 維持原式份額，不會因為別檔被抬高而縮小。剩餘 `R = B − Σ r_i` 照原式分給 medium／low（各自封頂）。
   - `volatility_exponent = 0` 時再加跨等級天花板：medium 不高於 `min_{i∈H} r_i`；low 不高於最小的 medium（沒有 medium 時不高於最小的 high）。low 先以 high 天花板分一次，若仍高於最小的 medium，就把 low 的天花板降到該值重算一次。調低 low 的天花板只會讓 medium 分到更多，所以一次就夠；重算後仍違反時程式丟 `DecisionToolError`（fail-closed）。
   - **分不完的預算留作現金**：只因這些天花板而分不出去的部分，不回流給 high，也不突破封頂。例：一般股 high 封頂 0.097、特殊上限股（如 2330）為 medium、`B = 0.30`、乘數 1.5／0.6；原式給 medium 0.203（高於 high），閘門下 high 0.097、medium 0.097，其餘 0.106 留作現金。
   - `volatility_exponent > 0` 時不加天花板，見下文「跨等級順序」。

**下限不是目標**：原式份額已高於 `f_i` 的 high 照原式，不會被壓回 α × 上限。在預算足夠（`Σ r_i < B`）時，每檔 high 都不少於原式份額；只有預算不足、依名次填時，名次在後的 high 才可能少於原式。例：預算 0.30，BIG（上限 25%）與 H2（10%）為 high、另 3 檔 medium，乘數 1.5／0.6、`volatility_exponent = 0`、α = 0.6。原式兩檔 high 各 0.09375；閘門下 BIG 0.15、H2 仍為 0.09375，medium 各 0.01875。

### 預算守恆與封頂

- 分支 1、2 與原式一樣把 `B` 分完，除非全部股票都已封頂；分支 3 在天花板不生效時也把 `B` 分完。天花板生效時，總和小於 `B`，差額留作現金（不超過預算）。現金因此變多，若觸發競賽現金上限，由 Guard 照舊拒絕（fail-closed），程式不另行補位。
- 任何股票都不超過 `cap_i`：`f_i` 本身先取 `min(α × L_i, cap_i)`，`base_i ≤ cap_i`，所以 `r_i ≤ cap_i`；medium／low 的天花板只會更低。之後照舊由 `_fit_stock_weights` 與 Guard 守硬上限。
- `L_i` 用 effective 的單股上限（`_weight_limit(symbol, effective)`；特殊上限如 2330 的 25% 不受覆寫影響）。這和原計劃 6.1 節「基準用 policy 層級」不同：α 方案是下限而不是倍數，修正循環調低 `max_stock_weight` 只會讓下限變小，方向是降風險。

### 跨等級順序

`volatility_exponent = 0`（目前正式設定）時，high ≥ medium ≥ low 由上述天花板保證，不再依賴推論；程式在回傳前也會檢查一次。Policy 已要求乘數 high ≥ medium ≥ low。原式只在特殊上限股拿到較高封頂時才會打破順序，例如 2330 為 medium 或 low，而一般股 high 已封頂。

`volatility_exponent > 0` 時，原式本來就可能因波動度讓高波動的 high 低於低波動的 medium。這時 α 方案不加天花板，非 high 的部分照原式分剩餘預算。保證的只有兩點：high 不低於原式份額，medium／low 不高於原式份額。medium 與 low 之間的相對順序不保證；例如 low 已封頂、medium 份額只略高於它時，剩餘預算變少會讓 medium 降到 low 以下（只在 low 的原始權重高於 medium 時發生，原因仍是波動度）。另外，特殊上限造成的倒掛（上例：一般股 high 已封頂、2330 為 medium）在 `volatility_exponent > 0` 時也照原式保留。這是範圍上的選擇：原本揭露的例外只涵蓋波動度造成的倒掛，這一項是新揭露的。若要在 `volatility_exponent > 0` 也強制順序，須另開閘門。測試 `test_cross_tier_order_is_preserved_under_any_multipliers`、`test_2330_as_medium_or_low_never_exceeds_the_highs` 以多組乘數、預算與特殊上限位置檢查；`test_positive_exponent_keeps_the_documented_exception` 鎖定 `volatility_exponent > 0` 的行為。

### 11-05 對照（`replay_reference_only`）

預算 0.358864，2330（上限 25%）與 6446（10%）為 high，13 檔 medium：2330 0.15、6446 0.06、medium 各 0.01145（與第 4 節 D 的估算一致）。實際委託仍受現金限制，見高信心現金下限計劃第 5.4 節。

### 與現金下限的相依

兩個閘門都開啟時，`cash_fit_order` 讓名次第一的 high 先拿現金。11-05 的 2330 已持有，不會被塵埃取消，它先用掉高信心可動用的現金，6446 因此被 `RISK_CASH_FIT` 取消。人決定維持「依名次」，不讓 high 內的未持有新股優先。這個行為以 `test_rank_first_high_add_takes_the_soft_floor_before_new_high` 鎖定。若要改，另開閘門。

### 與第 5 節推薦（C）的差異

- 一般高信心股在預算吃緊時也會被抬到 α × 10%（11-05：6446 從 0.0498 到 0.06）。C 只放大有特殊上限的股票。
- 中信心股在預算吃緊時明顯縮水（11-05：0.0199 → 0.0115），第 11 節第 2 項已列為代價。
- 不需要 `max(1, ·)`：下限永遠不超過封頂，也不會縮小原式份額。
