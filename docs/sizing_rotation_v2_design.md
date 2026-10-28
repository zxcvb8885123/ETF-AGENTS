# 配置與輪動 v2 實作設計（解除滿檔凍結、修正 revise 通道）

狀態：設計，尚未實作。本文件承接 [docs/sizing_rotation_v2_plan.md](sizing_rotation_v2_plan.md) 的診斷，給出逐檔案的改法、相容保證、邊界守則、測試清單與實作順序。所有行號以 2026-10-28 的工作樹（含未提交的 `held_add_priority` 版本閘門）為準。

目標：讓投資組合在滿檔後仍能把名額輪動給更好的標的，以爭取超額報酬；既有金融股**不由程式強制賣出**，賣出仍須由 Agent 依證據決定。

## 0. 讀碼補充核對（三點會改變設計）

計畫文件的診斷全部成立；讀碼時另外發現三件事，直接決定下面的方案形狀：

1. **交易 Agent 分批平行執行，各批互不可見。** `automation/team_stages.py:300`（`universe_batches`，每批 50 檔）與 `:352–353`（`_run_jobs` 平行）。一批裡的交易 Agent看不到其他批的候選股，所以「賣 X 換 Y」這種配對判斷**無法交給 LLM**；誰出場、誰進場的全域配對必須由程式做。
2. **持股廣度在交易 Agent 之後才決定，且會凍結換股。** `daily_pipeline.py:244`（交易）早於 `:258`（風險 Agent 給 `breadth`）。`allocation.py:666` 把目標檔數改成 `min(max_positions, max(廣度, min_positions))`，`:667` `slots` 與 `:671` `free_for_new` 都用這個收窄後的目標；若持有 30 檔而廣度選 22，`slots = 22 − 28 < 0`，**加碼與新股一起被凍結**，交易 Agent 即使出場 1 檔也放不進新股。換股設計必須把這個互動一起處理，否則 C1 會被廣度悄悄抵銷。
3. **交易 brief 其實已有 `rules.max_positions` 與 `account.position_count`**（`team_stages.py:307–313`），但沒有算好的剩餘名額、沒有「滿檔時買新股必須有持股出場」的指引，也沒有廣度目標（當時還不存在），而且 brief 不進 `team_inputs`、不被 Finalizer 重建。計畫的結論（交易 Agent 不知道要做取捨）仍成立，修法是「給算好的名額＋全池換股脈絡＋Skill 指引＋封存可重建」，不是只補一個數字。

## 0.5 實作時的更正：rotation 不是「解凍」，是上限與門檻

實作 PR-2 時用測試證實（`tests/decision/test_position_sizing.py::RotationSlotTests`）：

- **舊路徑本來就會換股。** `free_for_new = max_positions - len(active_symbols)`，只要本次有任何持股出場，`active_symbols` 就會減少，未持有的新股立刻拿到名額（`test_old_path_already_rotates_on_exit`）。舊路徑也沒有信心門檻，`low` 信心的新股同樣拿得到（`test_old_path_allows_weak_new_symbol_into_released_slot`）。
- **所以 §2.2 的名額配對不是新增能力**，而是兩件事：
  1. **修一個隱藏缺陷**：廣度目標低於目前持股數時（例如持有 30、廣度 22），舊路徑的 `slots` 會變成 0 或負數，連既有持股的 `add` 都一起凍住（`test_breadth_below_held_count_freezes_adds_on_old_path`）；rotation 的 `cap` 以持股數為底修掉它（`test_rotation_keeps_adds_alive_when_breadth_narrows`）。
  2. **加上上限與門檻**：`max_swaps_per_run` 與 `min_new_conviction`。啟用 rotation 的換股**比舊路徑少、不是多**。
- **真正的凍結原因仍然是交易 Agent 從不輸出 exit**，那是 PR-3（§2.3）處理的。計畫文件 §1.4 的結論不變。
- 使用者決定不加煞車：`max_swaps_per_run = 10`、`min_new_conviction = "low"`（等於沒有信心門檻），保留缺陷修正與一個防暴衝的上限。
- 另外，`_run` 新增的 `over_max` 分支只是維持舊路徑語意，不是缺陷修正：啟用 rotation 時新股能入選就代表持股數還沒到硬上限（`new_cap` 保證）。

## 1. 選定方案與理由

**選 C1 為主（機會驅動，交易 Agent 決定出場、程式做全域名額配對），同時必做 revise schema 修正；C2（風險 Agent `exit_position`）做成受政策閘門控制的窄版本，列為第三階段、不作為解決金融股偏重的手段。**

### 1.1 正面回應「使用者偏好 C2，但 C2 幾乎不會賣金融股」

使用者原本的要求「由風險 Agent 判斷風險後才賣」其實混了兩件事：

| 要解決的事 | 性質 | 誰擁有資訊 | 對應方案 |
| --- | --- | --- | --- |
| 持股本身出現風險（事件惡化、產業集中、流動性） | 降風險 | 風險 Agent（看 proposal／情境／Guard／產業曝險／流動性） | C2 |
| 把名額從較弱的持股換給更好的候選 | 投資組合建構 | 交易 Agent（同時握有每檔的多空 claims 與持股狀態） | C1 |

金融股目前「權重 26%、7 檔」的狀態，從風險 Agent 的視角幾乎沒有可挑的毛病：低波動、產業 26% 未超過上限、流動性充足、兩天審查都 approve 只掛 WATCH。給它 `exit_position` 也不會賣，因為它的職責是降風險，而金融股正是最不需要降的風險。所以「只做 C2」在使用者真正的目標（超額報酬）上是空集合。

C1 把出場決定放回 AGENTS.md 原本就指定的擁有者：交易 Agent 擁有 `trim`／`exit` 意圖，且 `trader.py:182–183` 已要求出場必須採納至少一個空頭 claim。這也回答了「不強制賣金融股」：程式只告訴交易 Agent「名額為 0、全池有幾檔未持有且多頭強度強的候選」，**不指定賣誰**；如果空頭研究員對某檔金融股沒有可採納的 claim，它就留著。輪動發生的速度取決於研究結論，不是程式偏好。

C2 保留的理由：revise 通道本來就是壞的（§6），修好之後加一個 `exit_position` 的邊際成本不高；而且它能處理 C1 處理不到的情況（配置後才看得到的產業集中或情境失敗）。但它是**邊界變更**（風險 Agent 第一次能賣既有持股），要同步改文件，且 v1 不在同一次修正循環內把釋出的名額給新股（§2.5），避免 revise 鏈不再單調降風險。

### 1.2 不選「只改權重公式或 tie-break」的理由

計畫 §1.2 已實測：波動度項全拿掉金融也只從 26.0% 降到 21.9%；§1.3 實測 ATR tie-break 只影響 1／30 名額。這兩項（計畫的 A、B）仍值得做，但只是底盤，放在 C1 之後（§8）。

## 2. 逐檔案的具體改法

### 2.1 Policy 新欄位（`src/etf_agent/decision/sizing.py`）

新增常數與允許欄位：

```python
# sizing.py:28 之後
ROTATION_VERSION = "v1"
ROTATION_FIELDS = {"version", "max_swaps_per_run", "min_new_conviction"}
```

`position_sizing.rotation`（物件，選配；省略即舊行為）：

| 欄位 | 型別 | 規則 | 建議值 |
| --- | --- | --- | --- |
| `version` | str | 必須等於 `ROTATION_VERSION` | `"v1"` |
| `max_swaps_per_run` | int | 非負整數（非 bool）；本次出場釋出的名額最多有幾個可給未持有的新股 | `3` |
| `min_new_conviction` | str | `CONVICTION_LEVELS` 之一；只有信心不低於此等級的新股可用「換股名額」（目標之下的真正空名額不受此限） | `"medium"` |

`validate_position_sizing`（`sizing.py:63`）：

- `:70–77` 允許集合加 `"rotation"`。
- 在 `:85–86`（`held_add_priority` 檢查）之後加：`rotation` 必須是物件、`reject_unknown_fields(rotation, ROTATION_FIELDS, "position_sizing.rotation", errors)`、`version` 等於 `ROTATION_VERSION`、`max_swaps_per_run` 是非負 int、`min_new_conviction` 在 `CONVICTION_LEVELS`。
- `rotation` 存在時必須同時有 `held_add_priority`（否則「新股不佔滿檔名額」的前提不成立），缺少即錯誤。

Policy 的 `content_sha256` 會改變，`config/decision_policy.json` 需新 `policy_id`（例如 `strategy-2026-10-29`）與 `available_at`；這和 `held_add_priority` 的做法相同。

### 2.2 名額與換股的確定性配對（`src/etf_agent/decision/allocation.py`）

**`_run`（`allocation.py:355`）**：在 `:372–375` 取得 `current` 後記錄 `held_count = len(current)`（帳戶快照不允許零股與零持股，`:378–385`），並在 `:454–457` 呼叫 `_conviction_targets` 時多傳 `held_count`。

**`_conviction_targets`（`allocation.py:632`）**：簽名加 `held_count: int`，回傳值由 `(ordered, weights)` 改為 `(ordered, weights, skipped)`，`skipped: Dict[str, str]` 記錄「被名額規則擋下的新股 → 旗標代碼」；舊路徑（無 `rotation`）`skipped` 一律填 `MAX_POSITIONS`，確保舊 run 的 `constraint_flags` 逐字不變。

在 `:666`（目標檔數收窄）之後、`:667`（`slots`）之前分支：

```python
rotation = sizing.get("rotation")
if rotation is None:
    # 既有路徑，一字不改：slots / free_for_new 用收窄後的 max_positions
    ...
else:
    hard_max = int(self.policy["max_positions"])          # 競賽硬上限（與 rules 綁定）
    target = max_positions                                 # 廣度收窄後的目標
    released = held_count - len(active_symbols)            # 本次實際出場的檔數（被 RISK_MIN_POSITIONS 留住或不可交易者不算）
    cap = max(target, min(held_count, hard_max))           # 廣度收窄只停止「淨增加」，不凍結既有持股的加碼與換股
    slots = cap - len(active_symbols - set(ordered))
    growth = max(0, target - held_count)                   # 目標之下、出場前就存在的真正空名額
    swaps = min(released, int(rotation["max_swaps_per_run"]))
    free_for_new = min(growth + swaps, hard_max - len(active_symbols))
    min_tier = CONVICTION_LEVELS.index(rotation["min_new_conviction"])
```

`:672–682` 的挑選迴圈改為：未持有的新股先用 `growth`；`growth` 用完才用 `swaps`，且 `CONVICTION_LEVELS.index(tiers[symbol]) <= min_tier` 才可用，否則 `skipped[symbol] = "ROTATION_CONVICTION"`；兩種名額都沒有時 `skipped[symbol] = "MAX_POSITIONS"`。排序鍵（`:659–665`）不變，所以哪一檔新股先拿到名額仍由「信心 → 支持數 → ATR → 代號」決定（B 階段再加 `selection_tiebreak`）。

不變量（寫成 assert 與測試）：配置後檔數 `len(active_symbols) + 新股數 ≤ hard_max`；`released = 0` 且 `growth = 0` 時新股數為 0（Skill 不能繞過硬規則）；`rotation` 省略時 `slots`／`free_for_new` 與現行算式逐位相同。

**`_run` 的旗標（`allocation.py:464–472`）**：用 `skipped[symbol]` 決定 `code`；新增 detail 文案：

- `MAX_POSITIONS`（rotation 啟用時）：「持股已滿且本次沒有出場釋出名額（交易 Agent 的意圖未被改寫）」；舊路徑文案不變。
- `ROTATION_CONVICTION`：「出場釋出的名額只給信心不低於 %s 的新股」。

**Proposal 內容雜湊影響**：舊 policy 無 `rotation` → `constraint_flags` 與 `position_sizing.target_weights` 逐位不變；新 run 多出的旗標代碼進入 `proposal_id`／`content_sha256`，由 `ProposalValidator`（`allocation.py:1008`）以同一引擎重算即可，不需新欄位。

### 2.3 交易 Agent 看得到的名額脈絡（新模組 `src/etf_agent/decision/rotation.py`）

依 AGENTS.md 的分檔慣例新增 `rotation.py`，只放確定性工具與驗證：

```python
PORTFOLIO_SLOTS_VERSION = "1.0"
PORTFOLIO_SLOTS_FIELDS = {
    "schema_version", "held_positions", "max_positions", "free_slots", "positions_by_breadth",
    "rotation", "unheld_by_bull_strength", "held_by_bear_strength", "note",
}

def build_portfolio_slots(bundle, policy, debate) -> Dict[str, object]: ...
def validate_portfolio_slots(bundle, policy, debate, slots, errors) -> None: ...  # 重算後整份相等
```

內容全部由程式算，沒有任何 Agent 可填的欄位：

| 欄位 | 來源 |
| --- | --- |
| `held_positions` | `len(account_snapshot.positions)` |
| `max_positions` | `policy["max_positions"]`（與 `rules.max_positions` 綁定，`allocation.py:197–210`） |
| `free_slots` | `max(0, max_positions − held_positions)` |
| `positions_by_breadth` | `policy.position_sizing.positions_by_breadth` 或 `null`；註明目標由風險 Agent 稍後決定 |
| `rotation` | `{"enabled": false}` 或 `{"enabled": true, "max_swaps_per_run": …, "min_new_conviction": …}` |
| `unheld_by_bull_strength` | 由已封存的 `debate` 計算：未持有股票依多頭 `strength`（`strong`／`moderate`／`weak`／`none`，`stance.py:27`）各有幾檔 |
| `held_by_bear_strength` | 持股依空頭 `strength` 各有幾檔 |
| `note` | 固定文字：名額為 0 時新股只能用本次出場釋出的名額；出場仍須採納空頭 claim；不得為了騰名額而出場、不得為了填名額而買進 |

兩個計數欄位是交易 Agent跨批看不到的全池資訊，但只是對**共同輸入**（`debate`）的統計，不是任何一批的輸出，符合 `docs/architecture.md` 平行段落「各批只讀共同輸入」的規則；它們也不是證據（沒有 `evidence_id`），只當背景。

**`automation/team_stages.py:272 run_trader`**：在 `:285` 之後呼叫 `build_portfolio_slots(bundle, base_policy, debate)`，brief（`:303–330`）加 `"portfolio_slots": slots`；`daily_pipeline.py:244` 的 `run_trader` 呼叫需把 `base_policy` 提前到 `:248–255` 的位置建立（只是把 `build_decision_policy`／`bind_drawdown` 往上移到交易階段之前，policy 內容不變）。任務提示（`:332–347`）加一段 `_ROTATION_RULES`：「`portfolio_slots.free_slots` 為 0 時，本批 buy 只有在全池有持股被你或其他批標為 exit 時才可能成交，且由程式依信心排序配對；持股若空頭 claim 成立、多頭理由已不成立，應給 exit 而不是慣性 hold；不得只為騰名額而出場。」

**`finalization.py`**：

- `:33–39` 新增 `ROTATION_TEAM_VERSION = "2.3"`、`ROTATION_TEAM_FIELDS = MEMORY_TEAM_FIELDS | {"portfolio_slots"}`，登記進 `TEAM_FIELDS_BY_VERSION`。
- `build_team_inputs`（`:42`）加參數 `portfolio_slots: Optional[Mapping] = None`；給了就是 2.3，且必須同時有 `prior_decision_context`（2.3 是 2.2 的超集）。
- `_validate_team_chain`（`:178`）：`:187–191` 的版本訊息補 2.3；版本為 2.3 時以 `validate_portfolio_slots(self.bundle, self.policy, self.debate, team["portfolio_slots"], errors)` 重算比對。
- `daily_pipeline.py:300` 傳入 `slots`。

`team_inputs_sha256` 只在新 run 改變；2.0／2.1／2.2 的欄位集合與驗證路徑不動。

### 2.4 revise schema 衝突修正（詳見 §6）

### 2.5 C2：`exit_position`（第三階段，政策閘門 `position_sizing.risk_exit`）

Policy：`position_sizing.risk_exit = {"version": "v1", "max_per_run": 2}`；`validate_position_sizing` 同 §2.1 方式驗證。沒有此欄位的 policy（含所有舊 run）：`REVISION_TYPES` 仍只有四種，`exit_position` 一律拒絕。

**`risk.py`**：

- `:29–34` `REVISION_TYPES` 加 `"exit_position"`；`RiskReviewValidator._validate_action`（`:653`）在 `:666` 之後：type 為 `exit_position` 而 policy 沒有 `risk_exit` → 錯誤「本 policy 未啟用風險出場」。
- `exit_position` 驗證：`symbol` 必須同時在 `account_snapshot.positions` 與 `proposal.allocation_proposal.positions`；不得是 `trading_status_bundle` 非 `allowed` 的標的（賣不掉，Guard 會否決）；不得是交易 Agent 已標 `exit`／`forced_exit` 的股票（無意義）；同一 review 的 `exit_position` 數 ≤ `max_per_run`；`len(positions) − 出場數 ≥ min_positions`（否則 Guard 必失敗，直接拒絕 review）；`value` 必須缺席或空字串。
- `revision_effects`（`:703`）回傳多一項 `"risk_exit_symbols"`：前一版 proposal 的 `risk_exit_symbols` ∪ 本次 actions。

**`allocation.py`**：

- `run`／`_run_min_positions`／`_run` 加 `risk_exits: Optional[Set[str]] = None`。`_run` 的出場迴圈（`:401–434`）：`symbol in risk_exits` 且意圖不是 `exit`／`forced_exit` 時，不可交易則記 `RISK_NOT_TRADABLE`，否則 `target_shares[symbol] = 0` 並記 `RISK_EXIT_POSITION`（detail：「風險 Agent 以風險為由要求出場（交易 Agent 的意圖未被改寫）」）。`_build_orders`（`:725`）用的意圖對照表複製一份，把這些股票標為 `exit`，與 `_fit_stock_weights`（`:783–786`）「程式產生的賣單標 trim／exit」的既有慣例一致。
- Proposal body（`:529–571`）**只在 policy 有 `risk_exit` 時**加 `"risk_exit_symbols": sorted(...)`（與 `excluded_symbols` 並列）。舊 run 的 body 不出現此鍵，`ProposalValidator` 的 `dict(proposal) != expected` 才能維持通過。
- `ProposalValidator.validate`（`:1020–1048`）與 `RevisionHistoryValidator`（`revision.py:174–182`）重播時把 `risk_exit_symbols` 傳回引擎。
- **v1 不在同一次修正循環內把釋出的名額給新股**：`_conviction_targets` 的 `released` 只算交易 Agent 的出場（`held_count − len(active_symbols)` 要先扣掉 `risk_exits` 造成的出場）。理由：`RevisionHistoryValidator` 的契約是「每一版只能降風險」（`revision.py:98` docstring、`decision-contract.md:108`），讓 revise 新增買單會破壞單調性，而且風險 Agent 沒看過那些新買單。釋出的名額在下一個交易日成為 `free_slots ≥ 1`，由當天的交易 Agent 填。若日後要在同次循環補買，另開 `risk_exit.version = "v2"`。
- 注意：出場後現金上升，可能逼近 `cash_weight_ceiling`；Guard `CASH_WEIGHT` 失敗即 reject，屬 fail-closed，Validator 不替它預先放寬。

**`automation/agent_schemas.py:87`** enum 加 `exit_position`（只在 policy 啟用時加入，否則 Agent 會輸出被拒的動作而耗掉重試）；**`risk_stages.py:171–185`** 提示文字同步。**`decision_delivery/renderer.py`／`renderer_v1.py`** 呈現新動作類型。

**邊界變更聲明**：這是風險 Agent 第一次能造成既有持股的賣單。它仍不手寫權重、不覆寫 Guard、不新增候選，符合「結構化風險修正」，但 AGENTS.md「後續 Agent」段、`README.md`、`docs/architecture.md` 的風險列、`src/etf_agent/decision/README.md` 規則段、`tradeagent/risk/portfolio-risk-review/SKILL.md` 與 `references/decision-contract.md` 都必須同步寫明：`exit_position` 只能以**配置後才看得到的風險**（產業集中、情境、流動性、共同事件）為由，附可引用清單中該股票的 `evidence_id`，不是換股工具。

### 2.6 A／B（計畫原有的權重指數與入選 tie-break）

- `sizing.py:23` 改為 `SIZING_METHODS = ("conviction_volatility_v1", "conviction_volatility_v2")`，`SIZING_METHOD` 保留給舊匯入；`validate_position_sizing:81–82` 改為 `in SIZING_METHODS`，v2 必須有 `volatility_exponent`（Decimal，0～1），v1 不得有。
- `conviction_weights`（`sizing.py:201`）加 `volatility_exponent: Optional[Decimal] = None`：`None` 走 `:211–214` 原式一字不改；給值時 `raw = multiplier / (max(vol, floor) ** exponent)`。v1 路徑不經過 `**`，保證舊 run 逐位重建。
- `selection_tiebreak`（`atr_low`／`symbol`／`momentum_high`）在 `_conviction_targets:659–665` 分支；`momentum_high` 用 `MomentumEngine` 的 `return_20d`（`momentum.py:131`），缺值者排最後。省略欄位即現行 `atr_low`。
- `strategy_reflection/contracts.py:36–55` 可選擇把 `volatility_exponent`（step 0.25，0～1）與 `rotation.max_swaps_per_run`（step 1，0～5）加進 `KNOBS`，`SIZING_GROUPS`（`:60`）加對應群組；`method`、`version` 類欄位保持在 `FORBIDDEN_PREFIXES`。

## 3. 向後相容：舊 run 如何保證完全不變

| 保護點 | 機制 |
| --- | --- |
| 權重公式 | v1 的 `conviction_weights` 程式路徑不碰 `**`；只有 `method = conviction_volatility_v2` 才進新式 |
| 名額計算 | `rotation` 省略 → `_conviction_targets` 走原分支，`slots`／`free_for_new`／`skipped` 文案逐位相同；`held_add_priority` 的既有閘門照舊 |
| Proposal 欄位 | `risk_exit_symbols` 只在 policy 有 `risk_exit` 時進 body；舊 proposal 的 dict 等式重算不受影響 |
| team_inputs | 2.0／2.1／2.2 的欄位集合與驗證分支不動；2.3 只是新增 |
| RiskReview | `revision_actions` 的 N/A 欄位「缺席或空字串」皆接受；舊封存（只有 approve／reject 或測試中缺席欄位）照舊通過；`exit_position` 在無 `risk_exit` 的 policy 下一律拒絕 |
| Policy 驗證 | 新欄位全是選配且版本固定（`v1`），未知版本拒絕（與 `stock_weight_fit`／`held_add_priority` 同一模式） |

驗收方式：`tests/decision/test_position_sizing.py:312`（`test_policy_without_held_add_priority_keeps_old_slot_order`）的同型測試，對 `rotation`／`risk_exit`／`volatility_exponent` 各寫一支「省略欄位 → 與舊結果逐位相同」；另以 `artifacts/decisions/2026-10-27` 與 `2026-10-28` 兩個真實 run 跑 `cli/portfolio_decision.py validate-decision --team-inputs` 作回歸（不改封存）。

## 4. 邊界守則

仍由確定性程式計算、Agent 不得輸出：

- 名額：`held_positions`、`free_slots`、`released`、`growth`、`swaps`、`cap`、廣度對應檔數。
- 誰進場：排序鍵（信心 → 支持數 → tie-break → 代號）與名額配對。
- 權重、張數、費稅、現金、換手、情境、Active Share、Guard。
- 風險出場後的訂單意圖標記（`exit`）、旗標與釋出名額的時點。

Agent 只能輸出：

- 交易 Agent：每檔 `intent`、buy／add 的 `conviction`、理由與 claim 取捨（同現行 `TradeDecisionValidator.ITEM`，`trader.py:69–72` 不加欄位）。出場仍須採納空頭 claim（`:182–183`）。
- 風險 Agent：現金姿態與廣度（不變）；revise 時的結構化動作，C2 下多一種 `exit_position`，仍只有 `type`／`symbol`／`value`／`reason`。
- 兩者都不得出現 `FORBIDDEN_DECISION_KEYS`（`fields.py:84–109`）；`portfolio_slots` 雖含數字，但它是程式產出的輸入，不是 Agent 輸出。

為什麼不違反 AGENTS.md：交易 Agent 的職責本來就包含 `exit`／`trim`（「空頭論點代表不宜買進或應減碼／出場」），C1 只是把程式已知的名額狀態告訴它，並把配對留在程式；「不改寫交易 Agent 的意圖」維持（新旗標與 `RISK_STOCK_WEIGHT` 同一慣例）；LLM 仍不輸出任何數字。C2 的擴張另在 §2.5 明示。

## 5. 換手與成本風險

- `max_turnover_rate` 目前為 `1`（`config/decision_policy.json`），Guard `TURNOVER`（`risk.py:389–391`）與 `_fit_cash` 的 `TURNOVER_LIMIT`（`allocation.py:952–1000`）實際上不會約束換股。C1 的防無謂換股因此**不能依賴**換手上限，必須在名額規則內：
  1. `max_swaps_per_run`（建議 3）：一天最多 3 檔新名字經換股進場；超過的出場只變成現金（下一天再補）。
  2. `min_new_conviction = medium`：`low` 信心的新股不能用換股名額，避免「出場一檔金融股換進一檔勉強的 low」。
  3. 出場仍須採納空頭 claim，且 Skill 明訂不得以「騰名額」為理由；Validator 現有規則已擋沒有空頭 claim 的 exit。
  4. 交易 Agent 的 `prior_decision_context` 已讓它看到前次對持股的意圖與重估條件（`trader/SKILL.md` 末段），Skill 加一句：前次才買進、今天就出場，必須由今天的空頭 claim 明確支持前次寫的重估條件已出現。程式不對此另設硬規則（持股天數不在 bundle 內，不發明欄位）。
- 費稅：每次換股約付 0.1425%×2 ＋ 0.3% 賣稅 ≈ 0.585% 的該檔金額；3 檔各 3.3% 權重 ≈ 0.06% NAV／日，可接受；仍由 `_order`（`allocation.py:903`）照舊計算並進情境。
- 滑價：`slippage_bps` 與 `stress_slippage_multiplier` 不變；換股增加的是賣單數量，`liquidity_exposure`（`risk.py:79`）會把大額賣單標 `flagged` 供風險 Agent 審查，必要時 `reduce_turnover_limit` 或 `remove_candidate`。
- 若日後要把 `max_turnover_rate` 調回有約束的值（例如 0.3），`_fit_cash` 只裁買單、不裁賣單（`:946–957`），出場仍會成交、新股被裁 → 現金上升；這是既有行為，不在本案改。
- 觀察指標（放進校準報告，供策略反思）：每日換股數、換進股 20 日後相對換出股的報酬差、`ROTATION_CONVICTION` 旗標數。

## 6. `revision_actions` schema 衝突的修法

現況：`automation/agent_schemas.py:88–89` 把 `symbol`／`value` 設為 `_TEXT`（`agent_runner.py:40`，`minLength: 1`）且四欄位全部 required（Codex strict），而 `tradeagent/risk/portfolio-risk-review/SKILL.md:22`、`references/decision-contract.md:102` 與 `risk_stages.py:175–177` 都要求不適用的欄位填空字串 → 任何 revise 都不可能通過 schema，2026-10-27 的 `risk_review.json` 因此留下 `REVISE_SCHEMA_CONFLICT`。

修法（條件必填，schema 放寬、Validator 收緊）：

1. `agent_schemas.py:88–89`：`"symbol": {"type": "string"}`、`"value": {"type": "string"}`（拿掉 `minLength`，仍列在 required 以符合 strict）。
2. `risk.py:653 _validate_action`：
   - `remove_candidate`／`exit_position`：`symbol` 必須非空且大寫；`value` 必須缺席或空字串，否則錯誤「此動作不接受 value」。
   - `increase_cash_buffer`／`reduce_max_stock_weight`／`reduce_turnover_limit`：`value` 必須可解析為 0～1 的 Decimal（既有 `:684–686`）；`symbol` 必須缺席或空字串。
   - `reason` 仍必填非空（`:669–672`）。
3. `revision_effects`（`:703`）不讀 N/A 欄位，維持現狀。
4. 同步修改的檔案：`tradeagent/risk/portfolio-risk-review/SKILL.md:22`（改寫為「不適用欄位填空字串，程式會檢查適用欄位」）、`tradeagent/risk/portfolio-risk-review/references/decision-contract.md:102`（同上，並列出條件必填表）、`tradeagent/risk/portfolio-risk-review/agents/openai.yaml`（描述若提到四欄位規則則對齊）、`risk_stages.py:175–177` 提示文字。
5. 驗收：以 2026-10-27 的 proposal／scenario／guard 用 `cli/portfolio_decision.py validate-risk` 餵一份含 `remove_candidate`（`value: ""`）與 `increase_cash_buffer`（`symbol: ""`）的 revise，必須通過；`REVISE_SCHEMA_CONFLICT` 不再出現在新 run。

## 7. 測試清單（`unittest.TestCase` 風格）

### `tests/decision/test_position_sizing.py`

- `RotationSlotTests.test_exit_releases_slot_for_new_buy_when_full`：持股滿檔、交易 Agent 對一檔 exit、一檔新股 buy（medium）→ 新股有 `target_weights`、出場有賣單、檔數不超過 `max_positions`。
- `test_no_exit_means_no_new_buy_when_full`：同上但無 exit → 新股掛 `MAX_POSITIONS`（新文案），委託數不因 Skill 而增加。
- `test_low_conviction_cannot_use_swap_slot`：exit 1 檔、新股 `low` → `ROTATION_CONVICTION`，不建單；`growth` 名額存在時同一檔 `low` 可入選。
- `test_swaps_capped_by_max_swaps_per_run`：exit 4 檔、`max_swaps_per_run = 3`、5 檔 medium 新股 → 剛好 3 檔進場，順序依信心／支持數／ATR／代號。
- `test_breadth_narrowing_stops_growth_but_not_add_or_swap`：持有 30、廣度 22、1 exit、1 add、1 buy → add 與 buy 都成單，檔數 30；無 exit 時 buy 不成單但 add 仍成單。
- `test_min_positions_kept_exit_does_not_release_slot`：被 `RISK_MIN_POSITIONS` 留住的 exit 不算 `released`。
- `test_untradable_exit_does_not_release_slot`：非 `allowed` 持股的 exit 不算 `released`。
- `test_policy_without_rotation_keeps_old_slot_order`：省略 `rotation` → proposal 與現行結果逐位相同（含旗標文案）。
- `test_rotation_requires_held_add_priority_and_known_version`：缺 `held_add_priority`、`version = "v2"`、`max_swaps_per_run = True`、`min_new_conviction = "強"` 各自被 `validate_position_sizing` 拒絕。
- `test_policy_template_enables_rotation`：正式樣板含 `rotation`（與 `test_policy_template_enables_held_add_priority` 同型）。
- `test_v2_with_exponent_one_matches_v1`、`test_v2_with_exponent_zero_is_equal_within_tier`、`test_v1_policy_rejects_volatility_exponent`、`test_selection_tiebreak_variants`（三種值在同等級同支持數的 5 檔搶 1 名額）。

### `tests/decision/test_team_chain.py`／新 `tests/decision/test_rotation.py`

- `PortfolioSlotsTests.test_slots_are_computed_from_bundle_policy_and_debate`：欄位值逐一等於手算；`unheld_by_bull_strength` 的四個 key 齊全。
- `test_finalizer_rebuilds_team_inputs_2_3`：`build_team_inputs(..., portfolio_slots=slots)` → `DecisionFinalizer` 通過；竄改 `free_slots` 或 `held_by_bear_strength` 一個數 → 拒絕；2.3 缺 `prior_decision_context` → 拒絕。
- `test_old_team_inputs_versions_still_validate`：2.1／2.2 的 fixture 不變仍通過。
- `test_trader_text_may_mention_slots_but_not_prior_tokens`：確認 `portfolio_slots` 不引入新的禁用字串（現行規則不變）。

### `tests/decision/test_portfolio_risk_decision.py`

- `test_revision_actions_conditional_fields`：`remove_candidate` 配 `value: ""` 通過、配 `value: "0.1"` 拒絕；`increase_cash_buffer` 配 `symbol: ""` 通過、配 `symbol: "2330.TW"` 拒絕；多餘欄位拒絕；缺 `reason` 拒絕。
- `test_review_schema_accepts_empty_strings`：直接用 `jsonschema`（若專案有）或 `_object` 結構斷言 `symbol`／`value` 沒有 `minLength`。
- C2：`test_exit_position_rejected_without_risk_exit_policy`、`test_exit_position_requires_held_tradable_symbol`（未持有、不可交易、交易 Agent 已 exit 各拒絕）、`test_exit_position_cannot_breach_min_positions`、`test_exit_position_sells_whole_position_with_program_intent`（賣單意圖 `exit`、旗標 `RISK_EXIT_POSITION`、`risk_exit_symbols` 入 body）、`test_exit_position_does_not_admit_new_buys_in_same_run`、`test_revision_history_replays_risk_exits_and_rejects_tampering`、`test_old_proposal_without_risk_exit_symbols_still_rebuilds`。
- `test_fourth_revision_still_fails`：現有 `test_illegal_revision_and_fourth_revision_fail` 延伸到含 `exit_position` 的鏈。

### `tests/automation/test_daily_pipeline.py`

- `test_trader_brief_contains_portfolio_slots_and_prompt_rotation_rules`：brief 檔含 `portfolio_slots`，提示含換股規則字句；`team_inputs` 為 2.3。
- `test_review_prompt_lists_exit_position_only_when_policy_enables_it`。

## 8. 實作順序（每步可獨立驗證、可獨立 commit）

| 步驟 | 內容 | 驗證 |
| --- | --- | --- |
| PR-1 revise 通道修復 | §6：schema 放寬、`_validate_action` 條件必填、Skill／契約／提示同步 | 新增條件必填測試；以 10-27 封存重跑 `validate-risk` |
| PR-2 名額配對（C1 程式端） | §2.1 `rotation` 欄位與驗證、§2.2 `_conviction_targets`／旗標、`config/decision_policy.json` 新 policy_id、`docs/architecture.md:28–41` 與 `README.md:118` | `RotationSlotTests` 全綠；10-27／10-28 封存 `validate-decision` 回歸通過 |
| PR-3 交易 Agent 脈絡（C1 Agent 端） | §2.3 `rotation.py`、brief、提示、`team_inputs` 2.3、Finalizer 重建、`trader/SKILL.md` 與 `data-rules.md` 新段落、`decision-contract.md:45` 版本說明 | `PortfolioSlotsTests`、pipeline 測試；`quick_validate.py tradeagent/trading/trader` |
| PR-4 A／B 底盤 | §2.6 v2 權重指數、`selection_tiebreak`、反思旋鈕 | 等價與排序測試；回放腳本比較 10-27 run 在 `exponent = 0`／`symbol` 下的權重分布 |
| PR-5（選配）C2 | §2.5 `risk_exit` 閘門、`exit_position`、engine／replay、renderer、邊界文件（AGENTS.md、README、architecture、decision/README、風險 Skill 與契約） | C2 測試群；確認未啟用時所有舊 run 與 PR-2～4 測試不變 |

PR-2 與 PR-3 合併後實跑一天，觀察 `constraint_flags` 中 `MAX_POSITIONS`／`ROTATION_CONVICTION` 數與實際換股數，再決定是否調 `max_swaps_per_run` 或做 C1b（§9）。

## 9. 已知限制與後續選項

- **C1 的配對是程式的，不是 LLM 的。** 交易 Agent 每批只能就本批持股的空頭 claim 判斷是否出場；它知道「全池有幾檔強多頭未持有」，但不知道是哪幾檔。若實跑後發現出場太少，可加 **C1b**：在所有批次之後再跑一次只讀「全池意圖摘要（代號、意圖、信心、多空強度）」的輪動回合，只允許把持股的 `hold` 改成 `exit`／`trim`（最多 `max_swaps_per_run` 檔、仍須採納空頭 claim），產生新的 `RotationDecision` artifact 與 Validator，由程式合併進 `TradeDecision` 後才封存。這會增加一個 Agent 呼叫與一份契約，本設計先不做。
- **廣度收窄不再凍結加碼與換股**（§2.2 的 `cap`）。這是 rotation v1 的刻意語意：廣度決定「不淨增加」，檔數只經交易 Agent 的出場下降；省略 `rotation` 的 policy 維持原凍結行為。
- **C2 不會解決金融股偏重**；它處理的是配置後才可見的風險。若 PR-2～4 跑一到兩週後金融仍長期偏重，該檢討的是計畫 §D：基本面分析師對獲利年增與營業現金流的權重，以及產業上限，屬策略層。
- 持股天數不在 `DecisionInputBundle` 內，所以「最短持有期」只能靠 Skill 與 `prior_decision_context` 的自我檢查，沒有程式硬規則。

## 10. 需同步更新的文件清單

- `AGENTS.md`「後續 Agent」：交易 Agent 可看到程式算的名額與全池多空強度統計（只當背景）；換股名額規則；C2 啟用時加風險出場一條。
- `README.md:118` 持股檔數段：補「滿檔時新股只能用出場釋出的名額、`max_swaps_per_run`、`min_new_conviction`」。
- `docs/architecture.md:28–41`：名額配對公式、廣度收窄的新語意、`team_inputs` 2.3。
- `src/etf_agent/decision/README.md`：檔案清單加 `rotation.py`；規則段加換股與（C2）風險出場。
- `tradeagent/trading/trader/SKILL.md`、`references/data-rules.md`：§1 輸入表加 `portfolio_slots`，§4 行動表補「滿檔時 buy 的成交條件」。
- `tradeagent/risk/portfolio-risk-review/SKILL.md`、`references/decision-contract.md`：§6 的條件必填；C2 時加 `exit_position` 的適用範圍與證據要求。
