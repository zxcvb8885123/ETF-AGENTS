# 換股改由交易 Agent 提名（階段 3）：詳細設計

狀態（2026-11-10，依程式與 `config/decision_policy.json` 核對）：**5.3 的步驟 1–6 已實作，步驟 7（文件同步）已做**。`position_sizing.agent_swap` **v1 影子期已於 2026-11-10 開啟**（`max_swaps_per_run 2`、`min_holding_days 5`；步驟 1–4：Policy 驗證、提名契約與 Validator、降級版 `agent_step_degradable`、5c 接入與 `team_inputs` 2.9）；**v2（步驟 6：`nominated_pairs`／`nomination_ref` 綁定、`portfolio_slots` 1.1、`AGENT_SWAP*` 與 `EXPOSURE_ROTATION_SKIPPED:agent_swap` 旗標）與影子報告 `cli/calibration.py shadow-swap`（步驟 5）已實作、尚未開啟**（正式設定是 v1，v2 的行為不是現行行為）。是否切 v2 由人在影子期累積 ≥ 10 個決策日後決定（第 6 節）。上層計畫見 [agent_autonomy_plan.md](agent_autonomy_plan.md) 階段 3；本檔取代該節的粗略設計，與它衝突處以本檔為準。下文各節保留當時的設計與推論；行號為 2026-11-10 工作區（分支 `145-`）。

**實作註記**：

- 提名失敗（`exhausted`／`unavailable`）只取消該動作，已依第 6 節的決定實作；硬規則與安全拒絕仍整份拒絕。
- `team_inputs` 2.9 是 2.8 的超集，所以 `agent_swap` 也要求 `exposure_rotation` v3 與 `rebound_participation` v2 同時存在；同階段的 `release_order`、`held_weight_facts` 因此改用不升版本的選配鍵。
- v2 的換入者仍沿用冷卻名單排除（候選理由含 `reentry_cooldown`），即使 `reentry_cooldown` 改為 v3 的提示。
- 影子報告的「若開 v2 會成交幾對」只是上限估計，動態取消（現金、權重、產業、換手）無法離線重現，標 `replay_reference_only`；無影子 run 時回報 `no_shadow_data`。
- Skill 檔不是逐 run 版本化的，第一輪說明用「若 brief 含某鍵則……」的條件寫法，已更新 `tradeagent/trading/trader/`（新增 `references/swap-nomination.md`）。

已決定（2026-11-10，使用者選擇，見上層計畫第 6 節第 2 項）：**硬規則不過整份拒絕；換股提名失敗只取消該動作並留旗標**，其餘訂單照常。

## 0. 證據與它能支持什麼

事實（`artifacts/decisions/` 11 份封存，目標交易日 2026-10-27～11-10，以 `calibration/intent_landing.py` 核對，該工具目前在工作區、尚未 commit）：

- buy／add 意圖 256 筆，有買單 40 筆；未落地 216 筆中 163 筆旗標為 `MAX_POSITIONS`。
- 每天交易 Agent 仍標 10～30 檔 buy／add，多數是 low／medium。
- 11 份中沒有任何 `RISK_FORCED_SWAP`；`exposure_rotation` 只有 11-10 一份帶閘門，兩種換股都是 `…_SKIPPED`。

本檔另以暫存目錄的唯讀腳本直接讀 11 份 `intent.json`、`proposal.json`、`decision_input.json`、`policy.json` 補充（事實；**此腳本沒有驗 manifest 與雜湊**，數字應以日後正式工具重核）：

- 163 筆依意圖／信心分：buy／high 9、buy／medium 78、buy／low 74、add／medium 2。其中 10-27 開帳日（持股 0 檔）占 22 筆（medium 4、low 18），那天沒有可換出的持股。
- 扣掉開帳日，維持 `rotation.min_new_conviction` medium 時理論上可被提名的換入者最多 83 筆（high 9＋medium 74），low 有 56 筆不能被提名。
- 開盤前持股數：10-28～11-04 為 29～30 檔，11-05～11-10 為 25～27 檔；11 份的廣度都是 diversified（目標 30）。所以 11-05 起不是「滿 30 檔」，而是新股候選多於剩下的空名額。

限制（事實）：`_conviction_targets` 在 `growth_left` 與 `swap_left` 都為 0 時就記 `MAX_POSITIONS`（`src/etf_agent/decision/allocation.py:1270-1271`），涵蓋「已達 30 檔」「已達廣度目標」「空名額已被排在前面的候選用完」等情況；163 筆沒有逐筆區分這幾種。

推論：名額由程式全域配對、交易 Agent 只拿到背景，所以「沒有名額」時新股意圖幾乎必然落空；既有的兩條程式換股路徑條件嚴、很少觸發，沒有填補這個缺口。本檔主張的是「誰換誰該由 Agent 回答」，**不主張改了績效會更好**。

## 1. 現況：哪些程式路徑代替 Agent 做換股決定

| 路徑 | 位置 | 行為（事實） |
| --- | --- | --- |
| 名額配對 `rotation` | `allocation.py:1184-1347` 的 `_conviction_targets`（名額邏輯 1241-1284） | 依（信心、名次、代號）排序候選。新股只能用「廣度目標內的空名額」（`growth_left`）或「本次實際出場釋出的名額」（`swap_left = min(released, rotation.max_swaps_per_run)`）；用換股名額的新股信心須不低於 `rotation.min_new_conviction`（現值 medium，否則 `ROTATION_CONVICTION`）。都沒有就記 `MAX_POSITIONS`（`_slot_flag`，1349-1372）。交易 Agent 只收到 `portfolio_slots` 背景（`decision/rotation.py:52-90`），其 note 明寫「由程式依信心等級配對」（`rotation.py:24-29`）。 |
| 強制換股 `rotation.forced_swap` | 呼叫 `allocation.py:609-634`；本體 `_forced_swap_victims` 800-904 | 滿檔又沒人出場時，信心達 `forced_swap.min_new_conviction`（現值 high）的新股，由程式換掉「交易 Agent 標 hold、論點檢視判定 broken、且（confirmation）本次採納該股空頭 claim」的持股，**依代號**挑；最多 2 檔；賣單標 `exit`、旗標 `RISK_FORCED_SWAP`。名單由 `thesis_verdicts.bind_forced_swap`（`decision/thesis_verdicts.py:108`）綁進 Policy。 |
| 曝險換股 `exposure_rotation` v3 | 呼叫 640-646；本體 `_exposure_rotation` 911-1086；復原迴圈 `_run_exposure` 421-448 | 姿態停止線非 null 且含現金組合 beta 低於目標時，把被名額擋下的高 beta 新股換掉「hold、hold 信心非 high、持有 ≥ 5 日、beta 低於組合」的持股（依 `exposure.exposure_rotation_plan`）；最多 2 對；`RISK_EXPOSURE_ROTATION`。防守姿態目標為 null，不觸發。 |
| 信心差換股 `conviction_gap_swap` | 同上（`kind != "exposure"`） | 任何姿態，以 high 新股換 hold 信心 low 的持股，與曝險換股共用檔數上限，曝險先跑。旗標 `RISK_CONVICTION_GAP_SWAP`。 |
| 反彈參與 `rebound_participation` v2 | `apply_rebound`（呼叫於 976） | 風險 Agent 的 `turn_assessment` 決定是否放寬曝險換股的防守停止線、檔數與持有天數；另有條件式現金放寬與 confirmed 換手放寬（`docs/decision-contracts.md` 反彈參與節）。 |
| 減碼日 `drawdown_derisk` v4 | 556-560 | 減碼日曝險換股與信心差換股整個略過（965-969）；`forced_swap` 只看 `derisk_day` 決定現金下限。 |

共通點（事實）：換出者全部由程式依代號、beta 或信心差挑，交易 Agent 不被問「要換掉誰」。交易 Agent 分批執行（`automation/team_stages.py:310` 起），評持股時看不到別批的新股；Skill 也這樣告訴它（`tradeagent/trading/trader/SKILL.md:38`、`:43`、`:45`，`references/data-rules.md:33`）。

時序（事實，`automation/daily_pipeline.py:345-376`）：交易 Agent 分批（5）→ 第二輪入選排序（5b，`team_stages.py:432-474`）→ 風險 Agent 給姿態、廣度、`cash_grade`（6）→ 配置與修正循環（7）。**第二輪時還不知道持股廣度目標與現金緩衝。**

## 2. 新設計

### 2.1 一句話

各批合併、入選排序之後，交易 Agent 再做一次「換股提名」（5c）：看到全部被名額卡住的新股與全部可換出的持股，輸出一份**有先後的配對清單**「用哪檔新股換掉哪檔持股、引用哪些已採納的 claim、為什麼」。程式不再自己挑換出者，只檢查可行性；配置時依清單先後取用需要的對數。

### 2.2 提名的語意

- 清單是「若需要名額，依序使用這些配對」，不是一定要成交的訂單。程式在廣度與現金確定後（步驟 7）才知道需要幾對，只取用需要的、可行的。
- 換入者在不換股的情況下已拿到名額（例如廣度目標或 Agent 自己的 exit 釋出了名額）時，該對**不使用**、換出者**不賣**（`AGENT_SWAP_UNNEEDED`）。這保證不會為了一檔本來就買得到的新股多賣一檔。
- 一換一：換出者與換入者成對成交或成對取消，**不只賣不買，也不只買不賣**（後者會讓檔數超過上限）。
- Agent 沒有提名就不換，程式不補挑。

### 2.3 Agent 輸出（只有列舉、代號與文字，不輸出任何數字）

```json
{
  "nominations": [
    {
      "buy_symbol": "XXXX.TW",
      "sell_symbol": "YYYY.TW",
      "buy_basis_ids": ["bull-…"],
      "sell_basis_ids": ["bear-…", "review:YYYY.TW:position"],
      "reason": "白話理由：為什麼新股比這檔持股更值得佔位置"
    }
  ],
  "thesis_hint_responses": [
    {"symbol": "YYYY.TW", "response": "nominated", "reason": "…"}
  ],
  "no_nomination_reason": "（nominations 為空時必填，否則不得出現）"
}
```

- 先後即陣列順序；`response` 列舉為 `nominated`／`kept`。
- 每個 `buy_symbol`、`sell_symbol` 各最多出現一次；兩者不得相同；總對數不得超過上限（2.6）。
- 程式封存時補信封（`schema_version`、`nomination_id`、bundle／snapshot／cutoff、`trade_decision_id`＋雜湊、`selection_ranking_id`＋雜湊、`status`、`errors`、`content_sha256`），做法同 `selection_ranking.seal_selection_ranking`。`status` ∈ `completed`／`not_needed`／`exhausted`。

### 2.4 每筆提名必須引用的依據（不得新增事實或 claim）

> 已實作、尚未開啟的例外（[曝險計畫](agent_led_exposure_plan.md) 第 4.3 節）：`agent_swap.exposure` 啟用時，`purpose: exposure` 的配對換出依據改為持股自己的曝險事實 `review:<代號>:exposure`，不要求第一輪已採納的空頭論點；`thesis` 配對維持本節規則。

| 欄位 | 可引用的範圍 | 規則 |
| --- | --- | --- |
| `buy_basis_ids` | 該新股在本次交易決策中**已採納**的多頭 claim | 至少一個；沿用 buy 本來就要求的多頭依據 |
| `sell_basis_ids` | 該持股在本次交易決策中**已採納**的空頭 claim、該股的持股檢視警示 `review:<代號>:<旗標>`、該股的持股事實 `review:<代號>:position` | 至少一個；**不得引用第一輪已否決的 claim**（與第一輪矛盾）。這延續 `data-rules.md:117`「不得純粹為了買別檔而賣，出場要有該股自己的依據」 |
| `reason`、`no_nomination_reason`、`thesis_hint_responses[].reason` | 文字 | 出現前次決策識別碼即拒絕（沿用 `prior_reference_tokens`）；不得引用論點檢視、`portfolio_slots`、策略教訓（它們沒有識別碼，靠 Skill 規則與 2.5 的 `citable=false` 標示） |

推論：「已採納的空頭依據」這一條會讓部分持股無法被提名（第一輪標 hold 又否決了全部空頭 claim 的持股）。這是刻意的：要換掉它，第一輪就該採納它的空頭論點或持股事實；第二輪不改第一輪的判斷。

### 2.5 簡報內容（全部由程式算，Agent 不輸出數字）

可放進 `buy_basis_ids`／`sell_basis_ids` 的 ID（只有這些）：

- 候選新股已採納的多頭 claim（ID＋原文）。
- 持股已採納的空頭 claim（ID＋原文）、持股檢視警示 `review:<代號>:<旗標>` 與持股事實 `review:<代號>:position`（含帳面報酬、近 5／20 日高點回落、相對交易池報酬，取自 `position_review`）。

沒有 ID 的程式事實（可讀、不可放進 basis）：

- 候選新股（`buy` 且未持有、信心不低於門檻）：代號、第一輪信心與理由、入選排序名次、是否可提名與原因（不可交易、在 `reentry_cooldown` 名單、缺行情、信心低於門檻）。
- 可換出的持股（第一輪 `hold`）：代號、hold 信心、目前權重、持有天數（帳本，`held_since_by_symbol`）、是否可提名與原因（不可交易、持有天數不足、沒有已採納的空頭依據）。
- 本次可提名的對數上限（2.6 A 段第 1 條的算法）。

v1（影子）與 v2（正式）的 5c 簡報與 Skill **完全相同**，不告訴 Agent 目前是影子期，否則影子資料不能代表正式行為。

背景（`citable=false`、沒有 evidence_id，只能參考，文字欄位不得寫其識別碼）：

- 名額事實：目前持股數、競賽上限與下限、`positions_by_breadth` 對照表（**註明今天的廣度由風險 Agent 稍後決定**）、本次第一輪標 exit 的檔數、候選新股數、本次提名上限。
- 保守的一張成本與現金估計：每檔候選新股一張的含費成本，以及「以今日可選最高現金格點估計」能負擔的一張數（只是提示；最後以配置為準）。
- 每檔持股的 beta 與 up／down capture（取自 `exposure_review`，沿用 Skill 現行規則：參與度低本身不是賣出理由）。
- 論點檢視背景（每週結論、可能已過時）：判定 `broken` 且第一輪標 hold 的持股列為「必須回應的提示」，Agent 在 `thesis_hint_responses` 對每檔剛好回應一次（`nominated` 時該股必須是某對的 `sell_symbol`，`kept` 時不得是）。回應是列舉，不是引用。

不附：風險 Agent 的姿態與現金（此時尚未產生）、其他前次 run 識別碼。

### 2.6 可行性檢查分兩段

**A. 5c 驗證（靜態，失敗走修正循環）**：只用 5c 時已知的事實。

1. 結構：欄位白名單、列舉值域、無數字欄位、重複、總對數 ≤ `min(agent_swap.max_swaps_per_run, max(0, rotation.max_swaps_per_run − 第一輪標 exit 且可交易的持股數))`。扣掉第一輪 exit 是因為配置時 `swap_left = min(released, rotation.max_swaps_per_run)`，exit 釋出的名額與提名換出共用同一個上限（`allocation.py:1249-1250`）；不扣的話多出的配對在配置時必然以 `swap_cap` 取消。
2. `buy_symbol`：在交易池、第一輪 `buy`、未持有、可交易、有行情、信心 ≥ `rotation.min_new_conviction`、不在 `reentry_cooldown` 名單（名單在 5c 前已綁進 Policy，`allocation.py:586-600`）。
3. `sell_symbol`：持有、第一輪 `hold`、可交易、持有天數 ≥ `agent_swap.min_holding_days`。
4. 依據：2.4 的引用規則；`thesis_hint_responses` 剛好覆蓋提示名單。
5. 保守現金：以「今日可選最高現金格點」當緩衝、賣款依 `reuse_sell_proceeds`，依清單先後累計每對「賣出淨額 − 新股一張含費成本」，任何一步不夠即回饋。這是對任何姿態都成立的下界檢查；通過不代表配置一定買得到。
6. 換手下界：第一輪賣單＋提名賣單＋新股各一張的成交額 ÷ NAV ≤ `max_turnover_rate`。

回饋用中性措辭，並列修法：「改提名其他持股或新股（補依據）」或「刪除該對（降級）」，比照 `typed_evidence_v1` 的回饋規則；差異記入 `agent_calls.revision_diff`。

修正循環**最多三次嘗試**（第一次＋兩次帶回饋的重試），明定為 5c 自己的上限。注意（事實）：現行 `agent_step` 的嘗試次數是 `PipelineBase.max_attempts`，預設 2（`automation/pipeline_base.py:155`、`338`）；`decision-contracts.md` 說的「修正循環最多三次」是配置後風險審查的 `max_revisions`，兩者不同，5c 不得靜默沿用 2。

**降級而不是整天失敗**（新程式路徑）：

- 驗證失敗用盡：程式封存 `status: exhausted`、`nominations: []`，配置留 `AGENT_SWAP_SKIPPED:nomination_exhausted`（symbol 為 `PORTFOLIO`）。
- 呼叫本身失敗（`TransientAgentError`：逾時、非零結束、輸出不是 JSON）在現行一次暫時性重試後仍失敗：封存 `status: unavailable`、`nominations: []`，留 `AGENT_SWAP_SKIPPED:nomination_unavailable`。
- 安全拒絕（秘密外洩、未核准工具）**仍整個停止**，不降級。
- 其餘決策照常。現行 `agent_step` 用盡即 `raise DailyPipelineError`（`pipeline_base.py:364`），`_call_agent` 的暫時性錯誤重試後也往上拋；需要另寫一個「回傳確定性降級結果」的版本，續跑（resume）必須重現同一份 artifact。
- 已知限制：Finalizer 無法證明 Agent 真的失敗，只能核對 exhausted／unavailable 的形狀與「配置沒有使用任何提名」；嘗試紀錄留在 `agent_calls` 與 raw 輸出供稽核。

**B. 配置時（動態，失敗只取消該對並留旗標，不再問 Agent）**：依清單先後處理。

1. 減碼日（`drawdown_derisk` 觸發）整個略過：`AGENT_SWAP_SKIPPED:derisk_day`。防守補現金當天不換股買進。
2. 先跑一次不含提名的名額配對；換入者已拿到名額的對標 `AGENT_SWAP_UNNEEDED`，換出者不賣。
3. 其餘配對依序試算：換出者目標設 0，以既有的 `allowed_new`（`allocation.py:1208`、`1266-1268`）把釋出的名額只給本對的換入者，避免名額被其他新股拿走；試算失敗時從清單**尾端**減少對數（沿用曝險換股的 1040-1052 寫法）。被捨棄的對標 `AGENT_SWAP_CANCELLED:swap_cap`。
4. 復原迴圈：換入者經現金、單股權重、產業、換手配置後沒有買單，就排除該對重跑（沿用 `_run_exposure` 421-448 的收斂寫法），換出者因此不賣：`AGENT_SWAP_CANCELLED:<cash_fit|stock_weight|sector_fit|turnover>`。
5. 換出者被 `RISK_CASH_CEILING`／`RISK_TURNOVER`／`RISK_MIN_POSITIONS` 留住時，換入者同步取消（否則檔數會超過上限）：`AGENT_SWAP_CANCELLED:victim_kept`。現行 `_run` 只對 forced_swap／exposure 的 hold 持股開放留住（508-512），需加上本閘門。
6. Active Share 迴圈（`_active_share_drop`，取消重疊最多的候選後重跑）若取消了某對的換入者，該對同步取消：`AGENT_SWAP_CANCELLED:active_share`。
7. 配置後風險審查的修正循環（`increase_cash_buffer`、`reduce_turnover_limit` 等 override）重跑配置時，同一套規則確定性重算，可行的對可能因此變少（`AGENT_SWAP_CANCELLED:<原因>`），**不再問交易 Agent**。
8. 成交的對：換出者賣單由程式標 `exit`、旗標 `AGENT_SWAP`（detail 寫「交易 Agent 第二輪提名換出，換入 X」）；換入者旗標 `AGENT_SWAP_FILL`。交易 Agent 第一輪的意圖欄位不改寫。

程式在 B 段**只會減少成交的對數、依 Agent 的先後從尾端減**，不會換成別的持股或別的新股。

### 2.7 何時呼叫 Agent

5c 只在「有需要也有可能」時呼叫，否則程式直接封存 `status: not_needed`（比照 `selection_ranking.program_ranking`），Finalizer 以同一規則重算：

- 可提名的新股數 > 保守空名額 ＋ 第一輪標 exit 且可交易的持股數，其中保守空名額 ＝ max(0, min(`positions_by_breadth` 的最小值, 競賽上限) 夾在 `min_positions` 以上 − 目前持股數)。5c 時廣度未知，用最窄的廣度估計，才不會在風險 Agent 選窄廣度時漏呼叫（例如持股 26 檔、之後選 concentrated 22 檔，實際空名額為 0）；且
- 至少有一檔可提名的持股；且
- 2.6 A 段第 1 條的對數上限大於 0。

推論：依第 0 節（11-05 起持股 25～27 檔、每天仍有 4～12 筆 `MAX_POSITIONS`），這個條件多數交易日會成立，實際上幾乎每天多一次 Agent 呼叫。

## 3. 版本閘門

### 3.1 新欄位 `position_sizing.agent_swap`

| 版本 | 欄位 | 行為 |
| --- | --- | --- |
| `v1`（影子） | `version`、`max_swaps_per_run`、`min_holding_days`；流程綁入、不得預填的 `thesis_hint_symbols`＋`verdicts_ref` | 跑 5c、封存提名、Finalizer 重建驗證；**配置完全不讀提名**，訂單與旗標與沒有此欄位時相同（policy 雜湊不同，所以 `proposal_id` 不同）。可與 `forced_swap`、`exposure_rotation`（含 `conviction_gap_swap`）、`rebound_participation` 共存。 |
| `v2`（正式） | 同 v1 | 配置依 2.6 B 段使用提名。互斥與取代見 3.2。 |

- 前提（缺一 fail-closed 拒絕）：`selection_ranking`、頂層 `position_review`、`rotation`＋`held_add_priority`、`reentry_cooldown`、`turnover_fit`、`cash_buffer_floor`、`exposure_rotation` v3（提供 hold 信心、`held_since_by_symbol` 帳本綁定與 `exposure_review`）、`rebound_participation` v2（理由見 3.3）。
- `thesis_hint_symbols` 用 `bind_forced_swap` 同一套讀法綁定（建立時間早於 cutoff、不超過 8 天），重建只讀 Policy。
- 只由人設定；`agent_swap` 前綴加入 `strategy_reflection/contracts.py:136` 的 `FORBIDDEN_PREFIXES`。注意：實際上限取 `min(agent_swap.max_swaps_per_run, rotation.max_swaps_per_run)`，而 `rotation.max_swaps_per_run` 是反思可調旋鈕（KNOBS，範圍 0～10）；反思只能把它往下調時才會影響本閘門。
- 沒有此欄位的舊 Policy／舊 run 逐位元維持原重建行為。

### 3.2 與其他閘門的關係（v2）

| 閘門 | 關係 | 理由 |
| --- | --- | --- |
| `rotation.forced_swap` | **互斥**：同時出現即拒絕 | 論點被推翻改為 5c 必須回應的提示；誰讓位由 Agent 回答 |
| `exposure_rotation` v3 | **保留但取代其換股步驟**：v2 存在時 `_exposure_rotation` 不配對，留 `EXPOSURE_ROTATION_SKIPPED:agent_swap`；`conviction_gap_swap` 必須省略（出現即拒絕） | 它同時是 hold 信心、持有天數帳本綁定與 `exposure_review` 的宿主；拿掉會連帶拿掉這三者與 `rebound_participation` |
| `rebound_participation` v2 | **保留**：條件式現金放寬與 confirmed 換手放寬照舊；它對曝險換股的放寬（`by_state` 的 `defensive_target`／`neutral_target`／`max_swaps_per_run`／`min_holding_days`）在 v2 下沒有作用 | 上層計畫第 6 節第 5 項：rebound v2 11-10 才開，先觀察滿 10 個決策日；本設計不在同一段時間拿掉它 |
| `drawdown_derisk` v4 | **減碼日優先**：整個略過換股 | 與曝險換股同；防守補現金當天不買進 |
| `reentry_cooldown` v1 | 並存：換入者在名單內不可提名；換出者賣出後同樣進名單（3 日內不得買回） | 防反覆來回。若日後開上層計畫的 v2（只計 Agent 自己的出場），`AGENT_SWAP` 必須算「Agent 自己的出場」 |
| `slot_recycle`（未開啟） | 無關；不在本計畫開啟 | |

### 3.3 `team_inputs` 2.9 與 Finalizer

- `team_inputs` 2.9 ＝ 2.8 ∪ {`swap_nomination`}，與 `position_sizing.agent_swap`（v1 或 v2）**雙向**同時存在；因為是 2.8 的超集，也必須有 `rebound_participation` 與 `exposure_rotation`。這就是 3.1 把 rebound 列為前提的原因。日後若要在沒有 rebound 的設定下開 `agent_swap`，必須另立一個版本號，不得讓 2.9 帶兩種欄位集合（`exposure_rotation_v2_plan.md` 第 6 節否決過這種做法）。
- 影子 v1 與正式 v2 的 `team_inputs` 欄位集合相同，差別只在 Policy 的閘門版本。
- Finalizer 重算或比對：
  1. 5c 簡報的可引用事實與 `not_needed` 判定，以同一份 bundle、交易決策、排序、`position_review`、`exposure_review`、帳本持有天數整份重算。
  2. `swap_nomination` 的信封、雜湊、2.6 A 段全部靜態檢查（用同一個 Validator）。
  3. `thesis_hint_symbols`／`verdicts_ref` 與 Policy 綁定一致。
  4. v2：配置重跑後旗標（`AGENT_SWAP`、`AGENT_SWAP_FILL`、`AGENT_SWAP_UNNEEDED`、`AGENT_SWAP_CANCELLED:*`、`AGENT_SWAP_SKIPPED:*`）與訂單逐位相同（沿用現有整份重建）。
- `portfolio_slots` 的 note（`rotation.py:24-29`）寫「由程式依信心等級配對」「不得為了騰出名額而出場」，與 v2 衝突。v2 下需改用另一段 note 並把 `PORTFOLIO_SLOTS_VERSION` 升為 1.1（依 Policy 決定，Finalizer 照樣重算）；1.0 文字不動，舊 run 才能重建。v1 影子期維持 1.0。

## 4. 風險與上線方式

| 風險 | 說明 | 怎麼降低 |
| --- | --- | --- |
| Agent 過度換股 | 開帳日以外每天 4～27 筆 buy 被名額擋下（第 0 節的腳本），Agent 可能每天都提滿 | 對數上限 `min(agent_swap.max_swaps_per_run, rotation.max_swaps_per_run)`（建議 2，與現行曝險換股相同）；換出者必須有自己已採納的空頭依據；`rotation.min_new_conviction` 仍擋 low（見第 6 節第 3 項） |
| 手續費與換手 | 一對約等於兩倍單股權重的成交額 | 不新增數字：沿用 `turnover_fit`＋`max_turnover_rate` 0.3（rebound confirmed 時依現行規則放寬），超過時復原迴圈取消該對；5c 另做換手下界檢查 |
| 反覆來回 | 今天換出、明天買回；或剛換入就被換出 | `reentry_cooldown` 擋 3 日內買回；`min_holding_days`（建議 5）擋剛買的持股被提名；即時紀錄另統計「換出後 10 日內再提名買回」次數 |
| Agent 不熟悉新輸出 | 第一次用、引用錯誤、修正循環用盡 | 用盡只取消換股、不拖垮整天決策；影子期先觀察驗證失敗率與用盡次數 |
| 交件時間 | 多一次 Agent 呼叫與最多 3 次修正 | 只在 2.7 條件成立時呼叫；逾時規則與交件期限不變；影子期記錄 5c 耗時 |
| 兩階段資訊落差 | 5c 不知道姿態與廣度，Agent 提的對可能在配置時被取消 | 語意定為「有序候選清單」；取消一律留旗標，隔天簡報可看到（只當背景） |
| 驗證薄弱 | 舊封存沒有提名輸出，無法回放 v2 | 見下方「驗證」；只有比賽期間的即時紀錄算證據 |

**比賽進行中的上線方式（建議）**

1. 程式與測試完成後，先開 `agent_swap: v1`（影子）至少 10 個決策日（`calibration.MIN_DECISION_DAYS`）。期間配置不變、帳本不受影響。
2. 影子期每天以 `calibration/` 下的唯讀工具（不被 `decision/`、`automation/` 匯入）讀封存的提名，用 v2 規則離線算「如果開了會成交哪幾對」，與 `intent_landing` 一起輸出：提名對數、用盡次數、可能成交對數、估計換手、與當天實際 `MAX_POSITIONS` 筆數。報告標 `evidence_grade: replay_reference_only`。
3. 由人看影子結果決定是否切 v2；切換前把現行樣板存到 `config/policy_versions/`，`policy_id`／`available_at` 用真實切換時間、不回填。
4. 回滾：把 `agent_swap.version` 改回 `v1`（或移除欄位並恢復 `forced_swap`、`conviction_gap_swap`，用存好的樣板）。

**驗證的上限（事實）**：回放工具已略過 `exposure_rotation` 與 `rebound_participation`（`src/etf_agent/evaluation/replay.py:248-258`、`289-291`），`agent_swap` 也會被略過；舊封存沒有提名，只能做等價測試（第 5 節），不能用回放證明它有效。

## 5. 測試、文件與實作步驟

### 5.1 測試清單（`unittest.TestCase` 風格）

正常：

- 有提名、換入者被名額卡住、現金與換手可行：成交一對，換出者賣單 `exit`＋`AGENT_SWAP`，換入者 `AGENT_SWAP_FILL`，第一輪意圖不變。
- 換入者本來就拿得到名額：`AGENT_SWAP_UNNEEDED`，換出者不賣。
- 提名多於可用名額：依清單先後取用，尾端標 `AGENT_SWAP_CANCELLED:swap_cap`。
- 不需要時程式封存 `not_needed`、不呼叫 Agent；Finalizer 重算一致。
- 論點檢視提示全部回應；`kept` 的持股不被賣。
- 影子 v1：配置的訂單與旗標與沒有閘門時逐位相同。

fail-closed（修正循環或拒絕）：

- 提名未持有／非 hold／不可交易／持有天數不足的換出者；已持有／非 buy／信心低於門檻／冷卻中／缺行情的換入者；同一代號重複；換出換入相同；超過對數上限。
- `buy_basis_ids` 不是該股已採納的多頭 claim；`sell_basis_ids` 引用已否決的 claim、別檔的 claim、或為空。
- 文字含前次決策識別碼；輸出含數字欄位或未知欄位。
- 提示漏回應、重複回應、`nominated` 卻不在配對裡。
- 保守現金或換手下界不過。
- 修正循環三次用盡：封存 `exhausted`、配置留 `AGENT_SWAP_SKIPPED:nomination_exhausted`、其餘訂單照常；續跑重現同一份 artifact；第二次嘗試就通過時不封存 exhausted。
- 5c 呼叫逾時／非零結束／非 JSON 且暫時性重試後仍失敗：封存 `unavailable`、留 `AGENT_SWAP_SKIPPED:nomination_unavailable`、其餘訂單照常。
- 安全拒絕（秘密外洩）仍整個停止，不走降級。
- `not_needed` 判定：持股 26 檔、最窄廣度 22 時仍會呼叫；對數上限扣掉第一輪 exit 數後為 0 時不呼叫。

配置時：

- 換入者因現金／單股／產業／換手沒有買單：該對取消、換出者不賣，迴圈收斂。
- 換出者被留住（現金上限、換手、最低檔數）：換入者同步取消，最終檔數不超過上限。
- 減碼日整個略過；`stance_floor` 為 defensive 且觸發減碼時同。
- Active Share 迴圈取消換入者：該對取消（`active_share`），換出者不賣。
- 風險審查修正循環以 override 重跑：提名換股依同一規則確定性重算，不呼叫交易 Agent；Finalizer 以最後一版重建一致。

Policy 與重建：

- `agent_swap` 缺前提、版本不在值域、`thesis_hint_symbols` 預填或與重讀結論不符：拒絕。
- v2 與 `forced_swap` 或 `conviction_gap_swap` 同時出現：拒絕。
- `team_inputs` 2.9 缺 `swap_nomination`、或有提名但 Policy 沒閘門（雙向）：拒絕。
- 提名、旗標或訂單被改寫：Finalizer 拒絕。
- 相容：沒有 `agent_swap` 的 Policy 與既有封存 run 重建雜湊不變（`tests/decision/test_archived_runs_replay.py`）；`portfolio_slots` 1.0 文字不變。

等價測試（`replay_reference_only`）：給定「與 forced_swap／曝險換股會選的人相同」的提名時，v2 的訂單與舊路徑相同（旗標代碼不同）。

### 5.2 實作時要同步更新的文件（本次只列出，不修改）

| 文件 | 要改的地方 |
| --- | --- |
| `AGENTS.md` | 「每日決策鏈的 Agent 邊界」（交易 Agent 5c 提名、提示回應）、「程式配置與風控」（換股改由 Agent 提名）、`team_inputs` 版本、閘門摘要 |
| `README.md` | 架構摘要與閘門清單 |
| `docs/status.md` | 版本閘門啟用狀態（唯一出處）與已知限制（exhausted 無法由 Finalizer 證明） |
| `docs/decision-contracts.md` | 交易 Agent、`team_inputs` 版本表、滿檔換股、強制換股、曝險換股、反彈參與各節 |
| `docs/architecture.md` | 「一天怎麼跑」加 5c、`team_inputs` 版本表 |
| `docs/decision-details.md`、`docs/glossary.md` | 白話說明與 `AGENT_SWAP*` 旗標 |
| `docs/README.md` | 索引與變更紀錄加入本檔 |
| `docs/agent_autonomy_plan.md` | 階段 3 指向本檔 |
| `docs/exposure_rotation_plan.md`、`docs/exposure_rotation_v2_plan.md`、`docs/rebound_participation_v2_plan.md` | 加註 v2 開啟時換股步驟被取代 |
| `docs/operations.md` | 每日檢視的新旗標、影子報告 |
| `tradeagent/trading/trader/SKILL.md`（38、43、45 行）、`references/data-rules.md`（33、117、126、132 行）、新增 `references/swap-nomination.md` | 第一輪說明改為「名額卡住時第二輪會請你提名」；新輸出與引用規則 |
| `config/policy_versions/README.md` | 切換時的樣板快照 |
| `src/etf_agent/strategy_reflection/README.md` | 禁區新增 `agent_swap` |
| `src/etf_agent/evaluation/README.md` | 回放略過 `agent_swap` |

### 5.3 實作拆分（每步可獨立驗收）

1. **Policy 驗證**：`decision/sizing.py` 加 `agent_swap` 白名單、前提、v1／v2 值域、與 `forced_swap`／`conviction_gap_swap` 的互斥；加入 `FORBIDDEN_PREFIXES`。驗收：Policy 測試；舊 Policy 不變。
2. **提名契約與 Validator**：新增 `decision/swap_nomination.py`（信封、封存、`not_needed` 判定、簡報建構、2.6 A 段靜態檢查、論點提示綁定）。驗收：純單元測試，不碰 pipeline。
3. **降級版 agent_step**：可指定最多 3 次嘗試；驗證用盡回傳 `exhausted`、暫時性錯誤重試後仍失敗回傳 `unavailable`，續跑可重現；安全拒絕仍拋錯。驗收：pipeline_base 測試。
4. **5c 接入 pipeline 與 `team_inputs` 2.9**：`team_stages.run_swap_nomination`、`daily_pipeline` 呼叫順序（5b 之後、6 之前）、`build_team_inputs` 與 Finalizer 雙向檢查、Skill `references/swap-nomination.md`（v1／v2 共用、不提影子期）。此時只支援 v1（影子）。驗收：e2e 測試中配置逐位不變；影子封存可被 Finalizer 重建。
5. **影子報告工具**：`calibration/` 唯讀工具，用 v2 規則離線算可能成交的對，標 `replay_reference_only`。驗收：對測試封存輸出固定數字；缺檔或雜湊不符整批拒絕。
6. **配置 v2**：`allocation.py` 新增提名換股步驟（取代 forced_swap 呼叫位置、在曝險換股之前）、`allowed_new` 試算、復原迴圈、換出者留住時同步取消、`EXPOSURE_ROTATION_SKIPPED:agent_swap`、`portfolio_slots` 1.1 note。Skill 檔不是逐 run 版本化的，第一輪的說明改用現行寫法「若 brief 含某鍵（例如 v2 才出現的 `swap_nomination_background`）則……」做條件說明，不靠改 Skill 切換行為。驗收：5.1 的配置與等價測試；舊封存重建不變。
7. **文件同步**（5.2）與人決定是否切 v2。

每步都跑 `AGENTS.md`「測試與驗證」的三個指令；新增 Skill 時另跑 `quick_validate.py`。

## 6. 需人決定事項

1. **提名放在哪一步。** (a) 5b 之後另一次呼叫（本檔設計：靜態檢查走修正循環，現金與換手等動態不可行只在配置時從清單尾端取消）；(b) 移到風險 Agent 之後，配置前再問一次（能看到真實廣度與現金，但多一個階段，且配置後的取消仍無法再問）。**建議 (a)**：不改變「交易 → 風險 → 配置」的順序，動態取消有旗標可查。
2. **上線節奏。** 建議先開 v1 影子 ≥ 10 個決策日，看提名率、用盡次數、估計換手與可能成交對數後再由人決定是否切 v2；不建議直接開 v2。
3. **換入者的最低信心。** 選項：沿用 `rotation.min_new_conviction`（medium）或允許 low。事實（第 0 節，未驗雜湊）：扣掉開帳日，163 筆中 high／medium 的 buy 有 83 筆、low 有 56 筆。**建議沿用 medium**：可提名池已遠大於每天 2 對的上限，放進 low 只增加候選、不增加成交對數，卻讓 Agent 有理由用弱候選換掉持股；影子期另記錄「被 medium 門檻擋掉的 low」數量，之後再議。
4. **v2 時舊換股路徑的處置。** 本檔設計：`forced_swap` 與 `conviction_gap_swap` 必須移除（互斥）；`exposure_rotation` 保留但換股步驟停用；`rebound_participation` 保留（現金與換手放寬照舊、對換股的放寬失效）。另一選項是三者全部移除，但會一併失去 hold 信心、持有天數與現金放寬。**建議採本檔設計**，等 rebound v2 觀察滿 10 個決策日後另案決定它的去留。

## 7. 證據等級

第 0 節是封存決策的事後統計（事實）；第 1 節是程式現況（事實，行號可核對）；第 2～4 節是設計與推論。影子報告、等價測試與任何回放都只算 `replay_reference_only`；只有比賽期間的即時紀錄算證據。
