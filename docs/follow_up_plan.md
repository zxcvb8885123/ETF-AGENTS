# 後續優化與上線計畫（Agent 自主權線）

撰寫時間：2026-11-10 19:24（Asia/Taipei），分支 `147-`。本檔是唯讀調查的結果：撰寫時沒有改程式、設定、Skill 或其他文件（除了在 `docs/README.md` 加一行索引）。

方向只有一句話：**讓 Agent 自己判斷，程式只守硬性界線**。本檔回答四件事：現在到哪裡、接下來做什麼、此刻哪些閘門可以開、工作怎麼切。

證據等級：本檔所有數字都是封存決策的事後統計或離線重算，一律是 `evidence_grade: replay_reference_only`——只說明「當時發生了什麼」，不是績效，也不是對 LLM 判斷的前瞻驗證。**本檔沒有任何一個數字屬於「新閘門開啟後的比賽期間即時紀錄」**，因為 2026-11-10 下午開的閘門到撰寫時還沒有任何一份封存 run（見第 1.2 節）。

閘門啟用狀態的唯一出處仍是 [status.md](status.md#版本閘門啟用狀態) 與 `config/decision_policy.json`；本檔與它們衝突時以它們為準。

## 1. 目前狀態

### 1.1 正式設定（事實）

出處：`config/decision_policy.json`，2026-11-10 19:24 唯讀讀取。

- `policy_id: strategy-2026-11-10-cash-v2-1`，`available_at: 2026-11-10T17:58:00+08:00`。
- 與本計畫有關的閘門值：

| 閘門 | 目前值 | 開啟時間（出處） |
| --- | --- | --- |
| `agent_swap` | `v1`（影子）、`max_swaps_per_run 2`、`min_holding_days 5`；沒有 `exposure` 子區塊 | 2026-11-10 15:31（status.md） |
| `reentry_cooldown` | `v2`、`days 3` | 2026-11-10 17:40（AGENTS.md、agent_autonomy_plan.md） |
| `drawdown_derisk` | `v5`、`max_derisk_fraction 0.25` | 2026-11-10 17:40（同上） |
| `cash_fit_order`、`high_conviction_cash_priority`、`held_weight_budget` | 皆 `v2` | 2026-11-10 17:58（status.md） |
| `exposure_view` | `v1`（純紀錄） | 2026-11-10（agent_led_exposure_plan.md 第 10 節） |
| `quality_dip_facts`、`quality_dip_assessment`、`event_direction` | 皆 `v1`（純紀錄） | 2026-11-10（status.md） |
| `exposure_rotation` | `v3`，防守目標 `null`，含 `conviction_gap_swap v1` | 2026-11-09 23:45 |
| `rebound_participation` | `v2`（candidate 0.30／confirmed 0.55） | 2026-11-10 01:37 |
| `rotation.forced_swap` | `v1`、`confirmation v1` | 既有 |
| `slot_recycle`、`high_conviction_cash_floor` | 設定裡沒有這個欄位 | 未開啟 |

### 1.2 封存決策與新閘門的即時紀錄天數（事實）

出處：`artifacts/decisions/`，逐份讀 `policy.json`。

- 共 11 份封存 run，目標交易日 2026-10-27～2026-11-10。最後一份是 `decision-20261109T230435Z`（目標交易日 2026-11-10），它的 Policy 是 `strategy-2026-11-10-rebound-v2-1`、`team_inputs` 2.8。
- 這份 run **不含** `agent_swap`、`exposure_view`、`quality_dip_facts`；當時 `reentry_cooldown v1`、`drawdown_derisk v4`、`cash_fit_order v1`。
- 所以到撰寫時，各新閘門已有的封存 run 數是：

| 閘門 | 已有的封存 run 數 |
| --- | --- |
| `agent_swap` v1 影子 | **0** |
| `reentry_cooldown` v2、`drawdown_derisk` v5 | **0** |
| `cash_fit_order` v2、`high_conviction_cash_priority` v2、`held_weight_budget` v2 | **0** |
| `exposure_view` v1 | **0** |
| `quality_dip_facts`、`quality_dip_assessment`、`event_direction` | **0** |
| `rebound_participation` v2、`exposure_rotation` v3 | 1（11-10 那份） |

### 1.3 兩個唯讀工具的輸出（`replay_reference_only`）

指令：`.venv/bin/python cli/calibration.py intent-landing`、`.venv/bin/python cli/calibration.py shadow-swap`，2026-11-10 19:2x 執行，兩者結束碼皆為 0。

**換股影子報告**：`status: no_shadow_data`、`shadow_run_count: 0`、`skipped_non_shadow_runs: 11`。沒有任何提名可統計。

**意圖落地率**（11 份 run）：

| 範圍 | buy／add 意圖 | 有買單 | 落地率 |
| --- | --- | --- | --- |
| 全部 11 份 | 256 | 40 | 15.6% |
| 扣掉開帳日 10-27（52／30） | 204 | 10 | 4.9% |
| 11-04～11-10 五份 | 89 | 6 | 6.7% |

- 未落地 216 筆的原因：`MAX_POSITIONS` 163、沒有旗標的 `add` 25、`CASH_BUFFER` 14（另 1 筆同時 `RISK_DRAWDOWN`）、`RISK_HELD_WEIGHT` 4、`RISK_REENTRY_COOLDOWN` 3、`LOT_SIZE` 3、`RISK_CASH_FIT` 2、`RISK_DRAWDOWN` 1。
- 「Agent 標 hold／add 卻被程式賣出」只發生在 11-04：金額 48,450,690 元，占當日賣單 76.85%，旗標全是 `RISK_DRAWDOWN`。其餘 10 份為 0。
- 11 份都沒有 `RISK_FORCED_SWAP`；`exposure_rotation` 與信心差換股只在 11-10 出現，兩者都是 `…_SKIPPED`。
- 這些數字與 [agent_autonomy_plan.md](agent_autonomy_plan.md) 第 1.2 節一致。

**估值 V1 收集**（出處：`artifacts/valuation_collection/2026-11-10/` 的 `audit.json`）：只有一個資料日（2026-11-09）、兩個 run。

| run | 要求 | 相符 | 拒絕 | 原因 |
| --- | --- | --- | --- | --- |
| `valuation-20261110T092504Z` | 100 | 0 | 100 | `FINMIND_UNAVAILABLE` |
| `valuation-20261110T095135Z` | 100 | 96 | 4 | `VALUE_MISMATCH` |

「全部相符的資料日」目前是 0 天。

### 1.4 已開啟／已實作未開啟／未寫

| 項目 | 狀態 | 是否影響真實帳本 | 出處 |
| --- | --- | --- | --- |
| `intent-landing`、`shadow-swap` 統計工具 | 可用（唯讀） | 否 | `src/etf_agent/calibration/README.md` |
| `agent_swap` v1 影子 | 已開啟，尚無影子 run | 否（配置不讀提名） | status.md |
| `reentry_cooldown` v2 | 已開啟，尚無 run | 是 | agent_autonomy_plan.md |
| `drawdown_derisk` v5 | 已開啟，尚無 run | 是（只在減碼日） | 同上 |
| 現金三個 v2 | 已開啟，尚無 run | 是 | status.md |
| `exposure_view` v1 | 已開啟，尚無 run | 否（純紀錄） | agent_led_exposure_plan.md 第 10 節 |
| 優質股第 1、2 階段三個閘門 | 已開啟，尚無 run | 否（純紀錄） | quality_dip_plan.md 第 10 章 |
| 估值 V1 | 已掛資料排程，只收集與稽核 | 否 | quality_dip_plan.md 第 10.2 節 |
| `agent_swap` v2 | 已實作、未開啟 | 開了會 | agent_nominated_rotation_plan.md |
| `agent_swap.exposure` v1（影子）／v2（正式） | 已實作、未開啟 | v1 否、v2 會 | agent_led_exposure_plan.md 第 11 節 |
| `reentry_cooldown` v3 | 已實作、未開啟 | 開了會 | agent_autonomy_plan.md 階段 4 |
| `slot_recycle` v1 | 已實作、未開啟 | 開了會 | decision-contracts.md |
| 影子報告的 `purpose`／`kept` 統計 | **未寫** | 否 | agent_led_exposure_plan.md 第 11 節「尚未做」 |
| 優質股第 3 階段（走換股提名） | **未寫**（只有設計） | 寫完開了會 | quality_dip_plan.md 第 10.3 節 |
| 估值 V2（位置事實進簡報） | **未寫** | 否（背景） | quality_dip_plan.md 第 2.6 節 |
| 估值 V3（可引用） | **未寫、未定案** | 間接 | 同上 |
| 意圖落地率目標 | **文件裡沒有訂過** | — | agent_autonomy_plan.md 只說「同一把尺」 |

### 1.5 文件與設定不一致之處（事實，撰寫時未修改）

- `docs/status.md` 的「版本閘門啟用狀態」開頭仍寫 2026-11-09 核對、`policy_id: strategy-2026-11-09-exposure-v2-1`；「已開啟」表仍列 `reentry_cooldown {version v1}` 與 `drawdown_derisk {version v4}`，同頁下方才寫它們已切到 v2／v5。
- `docs/status.md` 營運表寫 `shadow-swap`「尚未累積影子資料」——這句目前仍然正確。
- `docs/README.md` 索引寫 `reentry_cooldown` v2／v3、`drawdown_derisk` v5、階段 4 的 v2「已實作、尚未開啟」，且同一行對曝險階段 2、3 同時寫「已實作」與「尚未實作」。
- `docs/agent_autonomy_plan.md` 的三份計畫總表寫本計畫「只有 `agent_swap` v1 影子已開」、優質股「已實作未開」，與同檔上方的階段表及正式設定不符。
- `docs/agent_led_exposure_plan.md` 開頭狀態寫「階段 2、3 尚未實作」，與第 11 節不符。

這些由工作包 P1 處理。

## 2. 依證據排序的優化項目

排序原則：先補「看得到」的工具，再等即時紀錄，最後才動配置。排在前面的不改變帳本行為。

### 2.1 影子報告補 `purpose`／`kept` 統計

- **目的**：讓人在決定是否開 `agent_swap` v2 與 `agent_swap.exposure` 時，看得到 Agent 怎麼提名、怎麼拒絕。文件已點名要看的項目：論點提示的 `nominated`／`kept` 分布、提名失敗（`exhausted`／`unavailable`）次數、被 `rotation.min_new_conviction` 擋掉的 low 候選數、換出後 10 日內又被提名買回的次數；曝險影子開啟後另看 `purpose` 分布、提名高信心持股時 `lower_conviction_review` 的 `kept` 檔數與理由。
- **前提**：無。現在就能寫。`purpose` 與 `lower_conviction_review` 只有在 `agent_swap.exposure` 開啟後才會出現在封存裡，沒有時必須回報「未記錄」，不得補值。
- **風險**：統計寫錯會誤導開閘決定。擋法是維持現有的 fail-closed（缺檔、雜湊、snapshot／cutoff 不一致整批拒絕）與固定數字的測試。
- **驗收指標**：對測試封存輸出固定數字；沒有影子 run 時仍回報 `no_shadow_data`；報告帶 `evidence_grade: replay_reference_only`。
- **回滾**：還原該檔案。
- **是否改變真實帳本行為**：否。

### 2.2 意圖落地率：先有分段基準，再談目標

- **目的**：agent_autonomy_plan.md 要求「之後每階段是否有效，都用同一把尺看即時紀錄」。目前工具只有全期合計與逐日，沒有依 Policy 分段，也沒有依信心分層的落地數。
- **關於「目標」**：既有文件**沒有**訂過落地率目標，而且明寫「落地率低不等於程式做錯，也不代表績效差」「階段 1 不保證落地率上升」。所以本檔**不訂數字目標**。建議的做法是：
  1. 以 2026-11-11 起的 run 另立一段基準（`--since 2026-11-11`），與第 1.3 節的舊基準分開看。
  2. 先看「違反 Agent 明確意思」的指標，而不是落地率本身：hold／add 被程式賣出的金額（舊基準只有 11-04 一次）；`RISK_CASH_FIT`（`cash_fit_order` v2 下預期不再出現）；`RISK_REENTRY_COOLDOWN`（v2 下只該擋 Agent 自己出場的股票）；減碼日是否留下 `DERISK_ORDER_SKIPPED:order_*`。
  3. 落地率的數字目標等新基準滿 10 個決策日（`calibration.MIN_DECISION_DAYS`）後由人決定要不要訂、訂多少。
- **前提**：無。
- **風險**：把落地率當成要追的數字，會誘使放寬名額或現金界線。本檔明確不這樣做。
- **驗收指標**：工具能依 `policy_id` 分段、依信心分層；舊 11 份的合計仍是 256／40／216。
- **回滾**：還原該檔案。
- **是否改變真實帳本行為**：否。

### 2.3 `agent_swap.exposure` v1（曝險提名影子）

- **目的**：讓交易 Agent 在提名時可以用「提高市場參與度」當理由（`purpose: exposure`），並在提名高信心持股前逐檔交代中、低信心持股。影子期只封存。
- **前提**（agent_led_exposure_plan.md 第 6 節表、第 11 節；`GATE_PREREQUISITES`）：`agent_swap` 與 `exposure_view` 都已開啟（已滿足）；換股提名計畫與 Skill 的兩處同步修改（已做，第 11 節）；「換股提名 v1 影子已在跑」；第 11 節另寫「需人決定，先看第 1、2 階段即時紀錄」。
- **現況**：影子 run 0 份、`exposure_view` 的 run 0 份。所以「已在跑」與「先看即時紀錄」都還沒有任何可看的東西。
- **風險**：
  - 提名輸出變長（當天可提名的中、低信心持股有 19 檔），驗證失敗與用盡次數可能上升。
  - 它會改變提名的輸出格式。若在 `agent_swap` 影子期中途才開，前後兩段影子資料的格式不同；換股提名計畫要求影子與正式的簡報相同，影子資料才能代表正式行為（推論：越晚開，可用來判斷 v2 的同格式影子天數越少）。
  - Agent 可能對每檔都填 `kept` 加敷衍理由，程式擋不住，只能由人看紀錄。
- **驗收指標**：開啟後每份 run 的提名封存可被 Finalizer 重建；訂單與旗標和沒開時相同；`exhausted`／`unavailable` 次數由 2.1 的報告列出。
- **回滾**：刪掉 `position_sizing.agent_swap.exposure`。
- **是否改變真實帳本行為**：否（v1 影子，配置略過 exposure 配對）。

### 2.4 `agent_swap` v2（依提名換股）

- **目的**：滿檔時「誰換誰」由交易 Agent 的有序提名決定，程式只檢查可行性、只會從清單尾端減少對數。對應舊基準最大宗的未落地原因 `MAX_POSITIONS`（163 筆）。
- **前提**（agent_nominated_rotation_plan.md 第 4、6 節；`_validate_agent_swap`）：
  - 影子期至少 10 個決策日，人看過提名率、用盡次數、估計換手與可能成交對數後決定。
  - 必須同時移除 `rotation.forced_swap` 與 `exposure_rotation.conviction_gap_swap`（互斥，同時出現即拒絕）。
  - `exposure_rotation` v3 與 `rebound_participation` v2 必須保留（`team_inputs` 2.9 的宿主）；`rebound_participation` v2 對換股的放寬在 v2 下失效，它的去留要等它自己觀察滿 10 個決策日後另案決定（目前 1 份 run）。
- **風險**：Agent 每天都提滿、費稅上升、今天換出明天想買回、提名在配置時被取消。上限仍是 `max_swaps_per_run`、`turnover_fit`、`reentry_cooldown`、`min_holding_days`。影子報告的「若開 v2 會成交幾對」只是上限估計，動態取消無法離線重現。
- **驗收指標**（開啟後看即時紀錄）：每日 `AGENT_SWAP`／`AGENT_SWAP_FILL` 對數不超過上限；`AGENT_SWAP_CANCELLED:*` 的原因分布；沒有「只賣不買」；換手不超過 `max_turnover_rate`；Finalizer 重建一致；Guard 沒有因換股而拒絕。
- **回滾**：把 `agent_swap.version` 改回 `v1`，並用切換前存好的樣板恢復 `forced_swap` 與 `conviction_gap_swap`。
- **是否改變真實帳本行為**：**是**。

### 2.5 `agent_swap.exposure` v2（依風險 Agent 的回補意見取用曝險配對）

- **目的**：兩個 Agent 都同意才做曝險換股——風險 Agent 的 `exposure_view` 不是 `keep_defensive`，且交易 Agent 有提名 `purpose: exposure` 的配對。
- **前提**（agent_led_exposure_plan.md 第 6 節、第 11 節）：`agent_swap` v2 已開；第 1、2 階段有即時紀錄且人看過；`max_pairs_by_view` 的兩格由人填，且 `0 ≤ start_rebuild ≤ rebuild ≤ max_swaps_per_run`。
- **風險**：假反彈追高；放掉高信心金融股後反彈失敗；`exposure_view` 每天都選最高級。擋法都是既有硬性界線（對數、換手、現金下限、減碼日略過），不放寬。
- **驗收指標**：`keep_defensive` 的日子 exposure 配對一對都不成交且換出者不賣；超過對映上限時從清單尾端取消（`exposure_cap`）；`exposure_view` 的選擇分布與提名的 `purpose` 分布由報告列出。
- **回滾**：把 `agent_swap.exposure.version` 改回 `v1`，或刪掉 `agent_swap.exposure`。
- **是否改變真實帳本行為**：**是**。

### 2.6 `reentry_cooldown` v3（冷卻改成提示）

- **目的**：冷卻名單不再擋買單，改成交易 Agent 簡報裡的 `cooldown:<代號>` 提示；買回時必須回應，沒回應的買單由配置取消。
- **前提**：agent_autonomy_plan.md 把它排在 v2 之後（「先收窄、之後整個改成提示」），第 5 節要求「一次只開一個階段」「每階段至少觀察 10 個決策日再下結論」。v2 於 11-10 17:40 才開，目前 0 份 run。另注意：`agent_swap` 的換入候選仍沿用冷卻名單排除，即使改成 v3。
- **風險**：今天賣、明天買回的來回交易；上限只剩現金緩衝與 `max_turnover_rate`。
- **驗收指標**：v2 觀察期內 `RISK_REENTRY_COOLDOWN` 只出現在 Agent 自己出場的股票；v3 開啟後看「3 日內買回」的次數與換手。
- **回滾**：`reentry_cooldown.version` 改回 `v2`。
- **是否改變真實帳本行為**：**是**。

### 2.7 `slot_recycle` v1（名額回收）

- **目的**：排在前面的新股因預算為零或不足一張（`LOT_SIZE`）而不會成交時，不再占著名額，讓後面的候選補位。
- **證據**：舊基準 `LOT_SIZE` 未落地只有 3 筆（都在 11-09），樣本很小。
- **前提**：沒有程式層的前提或互斥。文件上它屬於階段 4「一併評估、一併決定」，受同一條「一次只開一個階段、觀察 10 個決策日」約束；同階段的三個現金 v2 於 11-10 17:58 才開，目前 0 份 run。它與 `cash_fit_order` v2（零頭不再整筆取消）都會增加成交的新股檔數，兩者同時變動時無法分辨是誰造成的。
- **風險**：持股檔數與塵埃持股變多。
- **驗收指標**：開啟後 `MAX_POSITIONS` 筆數、持股檔數（不得超過 30）、一張以下的小部位數、換手。
- **回滾**：刪掉 `position_sizing.slot_recycle`。
- **是否改變真實帳本行為**：**是**。

### 2.8 優質股第 3 階段（走換股提名）

- **目的**：交易 Agent 判為「被大盤拖累」的買進候選，可以經由換股提名取得名額；不另開程式配對路徑。
- **前提**（quality_dip_plan.md 第 10.3 節）：曝險階段 2 落地且 `purpose` 列舉定案（程式已落地、閘門未開，列舉目前是 `thesis`／`exposure`，要不要加 `quality_dip` 尚未決定）；第 1、2 階段累積「數個決策日」的紀錄（天數由人定，文件沒有給數字）；人下令開工。要真的影響配置，還要 `agent_swap` v2 已開。
- **現況**：未寫；第 1、2 階段 0 份 run。
- **風險**：接刀。既定擋法：無新壞事、至少一個非價格的多頭論點、`turn_assessment` 至少 `turn_unconfirmed`、超跌候選預設排除、買進金額不超過賣款；現金下限、換手、檔數一律不放寬。
- **驗收指標**：先以影子（只封存、配置不讀）上線；Validator 對上述結構條件逐條有測試；舊封存重建不變。
- **回滾**：刪掉屆時新增的閘門欄位。
- **是否改變真實帳本行為**：寫程式本身不會；影子期不會；正式開啟**會**。

### 2.9 估值 V2／V3

- **目的**：V2 讓基本面分析師與交易 Agent 看到「PER／PBR 在自身歷史區間的位置」（背景、不可引用，三個視窗並列）；V3 才讓它可被引用。
- **前提**（quality_dip_plan.md 第 2.6 節 (5)、第 10.2 節）：V1 連續核對相符達**人指定的天數**（人還沒指定）；EPS 基準與殖利率基準釐清；上櫃端點確認（確認前上櫃一律 `unavailable`）；`license_status` 登記。V3 另需 V2 有即時紀錄與人決定，且要出新的證據政策版本。
- **現況**：一個資料日、最好的一次 96／100 相符、4 檔 `VALUE_MISMATCH`；全部相符 0 天。口徑待確認項目都未釐清。
- **風險**：單看位置下「便宜」結論（2308 的例子：近一年最低 1%、2019 年以來最高 16%）。擋法：不設門檻、Agent 不輸出數字、Skill 不得寫便宜／低估。
- **驗收指標**：V1 每個資料日的相符檔數與拒絕原因有彙總；4 檔不符的原因查明；V2 開啟後舊 run 逐位元不變。
- **回滾**：V2＝刪掉 `position_sizing.valuation_facts`；V1＝`VALUATION_COLLECT=0`。
- **是否改變真實帳本行為**：V1、V2 否；V3 間接會（改變可引用的證據）。

## 3. 現在可以開的清單

**沒有。此刻沒有任何一個未開啟的閘門滿足文件既有的開啟條件。**

共同原因：2026-11-10 下午開的閘門到撰寫時都還沒有任何一份封存 run，影子 run 是 0 份；所有「看過即時紀錄再決定」「觀察滿 10 個決策日」的條件都還沒開始計數。

逐項說明何時、看什麼才開（門檻都是文件原有的，本檔沒有放寬）：

| 項目 | 現在缺什麼 | 何時、看什麼指標才開 |
| --- | --- | --- |
| `agent_swap.exposure` v1（影子） | 影子 run 0 份；`exposure_view` 紀錄 0 份；人尚未決定 | 至少有一份帶 `agent_swap` v1 的 run 封存且 Finalizer 重建通過（「影子已在跑」）、`exposure_view` 有即時紀錄可看之後，由人決定。文件沒有給這一步的天數，本檔不代訂。看：`shadow-swap` 的 `shadow_run_count ≥ 1`、提名狀態、`exposure_view` 的實際輸出 |
| `agent_swap` v2 | 影子 0／10 | `shadow-swap` 的 `shadow_run_count ≥ 10`（`calibration.MIN_DECISION_DAYS`），人看過提名對數、`exhausted`／`unavailable` 次數、估計成交對數與取消原因、同日 `MAX_POSITIONS` 筆數後決定。開啟時必須同時移除 `forced_swap` 與 `conviction_gap_swap` |
| `agent_swap.exposure` v2 | `agent_swap` v2 未開；第 1、2 階段紀錄 0 份 | `agent_swap` v2 已開、曝險影子（v1）有即時紀錄、人看過 `purpose` 與 `lower_conviction_review` 的 `kept` 紀錄後決定；`max_pairs_by_view` 由人填且不得高於 `max_swaps_per_run` |
| `reentry_cooldown` v3 | v2 的 run 0／10 | v2 累積 10 個決策日，`intent-landing --since 2026-11-11` 顯示 `RISK_REENTRY_COOLDOWN` 只擋 Agent 自己出場的股票、沒有異常來回交易，且同一時間沒有別的配置閘門在觀察期內切換，由人決定 |
| `slot_recycle` | 同階段的現金三個 v2 的 run 0／10 | 現金三個 v2 累積 10 個決策日，確認持股檔數、小部位數與換手沒有異常後，由人決定是否單獨開（不與其他配置閘門同日開） |
| 優質股第 3 階段 | 程式未寫；`purpose` 列舉未定案；第 1、2 階段紀錄 0 份；`agent_swap` v2 未開 | 人決定 `purpose` 是否加 `quality_dip` 並下令開工；第 1、2 階段累積人指定天數的紀錄；先影子、`agent_swap` v2 開啟後才談正式 |
| 估值 V2 | 程式未寫；人未指定連續相符天數；口徑未釐清；全部相符 0 天 | 人指定天數；V1 連續全部相符達該天數；EPS 與殖利率基準釐清；4 檔 `VALUE_MISMATCH` 查明 |
| 估值 V3 | V2 不存在 | V2 開啟後有即時紀錄，人決定 |
| `high_conviction_cash_floor` | 與已開啟的 `cash_ratio_range` 互斥 | 不在本計畫範圍；要開必須先關 `cash_ratio_range` |

日曆推算（**推論，不是門檻**）：若 2026-11-11 起每個平日都是交易日、每天都有一份帶 `agent_swap` v1 的 run 成功封存，第 10 份影子 run 的目標交易日最早是 2026-11-24。實際以 `shadow-swap` 回報的 `shadow_run_count` 為準；被拒絕的日子、休市日都不算。

不需要開閘門、現在就能做的只有唯讀工具與文件（工作包 P1～P4）。

## 4. 執行步驟：工作包

共 12 包。P1～P4 現在可做；P5～P12 有進入條件，**條件沒成立或使用者沒有明確下令時不得執行**。

### 4.0 所有工作包共用的規定

**測試指令**（AGENTS.md「測試與驗證」，每包完成後三個都要跑、都要通過）：

```bash
PYTHONPYCACHEPREFIX=/tmp/etf-agent-pycache \
  .venv/bin/python -m pytest

PYTHONPYCACHEPREFIX=/tmp/etf-agent-pycache \
  .venv/bin/python -m compileall -q src tradeagent tests

git diff --check
```

**禁止事項**：

- 不 commit、不 merge、不 push；修改保留為未提交狀態，只回報改了哪些檔案。
- 不改與該包無關的檔案；不改 `artifacts/`（帳本與封存是真實紀錄，只能讀）。
- 閘門只照第 3 節的清單開。第 3 節目前是「沒有」，所以 **P1～P4 一律不得修改 `config/decision_policy.json`**。
- 開任何閘門之前：先把當時的 `config/decision_policy.json` 原樣存到 `config/policy_versions/<日期>-before-<名稱>.json`，在 `config/policy_versions/README.md` 補一行說明；再改 `policy_id`，並把 `available_at` 設為**真實切換時間**（Asia/Taipei，含時區），不得回填、不得沿用舊值。
- 不放寬任何硬性界線（檔數、現金、單股、產業、換手、Active Share、Guard）；不替人決定文件裡寫「由人決定」的數字。
- 新測試維持 `unittest.TestCase` 風格；說明文字與錯誤訊息用繁體中文。
- `calibration/` 不得被 `decision/`、`automation/` 匯入。
- 報告類輸出一律帶 `evidence_grade: replay_reference_only`。

### P1 文件狀態同步（現在可做；只改文件）

- **輸入**：`config/decision_policy.json`（只讀）、本檔第 1.1、1.5 節。
- **要改的檔案**：`docs/status.md`（閘門表開頭的核對日期與 `policy_id`／`available_at`、`reentry_cooldown` 與 `drawdown_derisk` 兩列的目前值、「未開啟」表中已開啟的列）、`docs/README.md`（索引裡 agent_autonomy_plan、agent_led_exposure_plan 兩行的過期敘述，並在更新紀錄補一行）、`docs/agent_autonomy_plan.md`（三份計畫總表的進度欄）、`docs/agent_led_exposure_plan.md`（開頭狀態句）。
- **驗收條件**：每一個改過的閘門值都能在 `config/decision_policy.json` 找到相同的值；沒有把「已實作未開啟」寫成已開啟；三個測試指令通過。
- **禁止事項**：共用規定；不改任何 `.py`、`.json`、Skill；不改寫各計畫文件的設計與推論段落。

### P2 影子報告補統計（現在可做；唯讀工具）

- **輸入**：`src/etf_agent/calibration/shadow_swap_report.py`、`tests/calibration/test_shadow_swap_report.py`、封存的 `team_inputs.swap_nomination` 形狀（agent_nominated_rotation_plan.md 第 2.3 節、agent_led_exposure_plan.md 第 11 節）。
- **要改的檔案**：上述兩檔、`src/etf_agent/calibration/README.md`；報告版本字串升一版。
- **內容**：每日與合計多輸出——論點提示的 `nominated`／`kept` 數；提名的 `purpose` 分布；提名高信心持股的對數，與當次 `lower_conviction_review` 的 `nominated`／`kept` 檔數及 `kept` 理由原文；被 `rotation.min_new_conviction` 擋掉的 low 候選數；換出後 10 個決策日內又被提名為換入的次數。封存裡沒有的欄位回報「未記錄」，不補值、不推測。
- **驗收條件**：測試封存（含有與沒有 `purpose` 的兩種）輸出固定數字；缺檔、雜湊或 snapshot／cutoff 不一致仍整批拒絕；對目前真實封存執行仍回報 `no_shadow_data`；三個測試指令通過。
- **禁止事項**：共用規定；不改 `decision/`、`automation/`；不讀寫帳本。

### P3 意圖落地率分段與分層（現在可做；唯讀工具）

- **輸入**：`src/etf_agent/calibration/intent_landing.py`、`tests/calibration/test_intent_landing.py`。
- **要改的檔案**：上述兩檔、`src/etf_agent/calibration/README.md`、`docs/operations.md`（每日檢視加一段：每個決策日跑 `intent-landing --since 2026-11-11` 與 `shadow-swap`，看本檔第 2.2 節列的四個指標）。
- **內容**：合計之外多一份依 `policy_id` 分段的彙總；落地與未落地各依信心（high／medium／low）分層；每段列出 hold／add 被程式賣出的金額。不輸出任何「目標」或「達標與否」。
- **驗收條件**：對目前 11 份封存，全期合計仍是 256／40／216、被賣出金額 48,450,690；分段加總等於合計；三個測試指令通過。
- **禁止事項**：共用規定；不新增門檻或評分。

### P4 估值 V1 稽核彙總（現在可做；唯讀工具）

- **輸入**：`artifacts/valuation_collection/`（只讀）、`cli/valuation_collect.py`、`src/etf_agent/data/valuation` 套件。
- **要改的檔案**：`cli/valuation_collect.py`（加一個唯讀子命令）、估值套件內的彙總函式與對應測試、`docs/operations.md` 的估值小節。
- **內容**：逐資料日列出採用的 run、要求／相符／拒絕檔數、拒絕原因與代號，並算「連續全部相符的資料日數」。只彙總已保存的稽核結果，不重新抓取。
- **驗收條件**：對目前封存輸出——資料日 2026-11-09、最好一次 96／100、`VALUE_MISMATCH` 4 檔、連續全部相符 0 天；manifest 或雜湊不符時拒絕；三個測試指令通過。
- **禁止事項**：共用規定；不連外部端點；不把估值接進決策、簡報或報告。

### P5 開閘前檢查報告（進入條件：`shadow_run_count ≥ 10`）

- **輸入**：P2、P3 的工具輸出（`--since 2026-11-11`）。
- **要改的檔案**：無程式修改。產出一份給人看的摘要（由執行者回報，不寫進 repo，除非使用者指定位置）。
- **內容**：影子天數、提名狀態分布、估計成交對數與取消原因、同日 `MAX_POSITIONS`、新基準的落地分層、hold／add 被賣金額、`RISK_CASH_FIT`／`RISK_REENTRY_COOLDOWN`／`DERISK_ORDER_SKIPPED` 次數；逐項對照第 3 節的條件寫「已滿足／未滿足」。
- **驗收條件**：每個數字都附指令與執行時間，標 `replay_reference_only` 或「比賽期間即時紀錄」；不下「可以開」的結論，只列條件是否滿足。
- **禁止事項**：共用規定；不改設定。

### P6 開 `agent_swap.exposure` v1 影子（進入條件：第 3 節該列成立且使用者明確下令）

- **輸入**：使用者的下令、當時的 `config/decision_policy.json`。
- **要改的檔案**：`config/policy_versions/`（新樣板與 README 一行）、`config/decision_policy.json`（加 `agent_swap.exposure: {"version": "v1"}`、新 `policy_id`、真實 `available_at`）、`docs/status.md`、`AGENTS.md` 閘門摘要、`README.md`、`docs/agent_led_exposure_plan.md`。
- **驗收條件**：Policy 驗證測試通過；讀正式樣板的整合測試通過；舊封存重建測試不變；三個測試指令通過。
- **禁止事項**：共用規定；不得寫成 `v2`；不得加 `max_pairs_by_view`。

### P7 切 `agent_swap` v2（進入條件：P5 完成、第 3 節該列成立且使用者明確下令）

- **輸入**：P5 的報告、使用者的下令。
- **要改的檔案**：`config/policy_versions/`（切換前樣板）、`config/decision_policy.json`（`agent_swap.version: v2`，同時移除 `rotation.forced_swap` 與 `exposure_rotation.conviction_gap_swap`，新 `policy_id` 與真實 `available_at`）、agent_nominated_rotation_plan.md 第 5.2 節列出的文件。
- **驗收條件**：`tests/decision/test_agent_swap_policy.py`、`test_agent_swap_allocation.py`、`test_gate_prerequisites.py`、`test_archived_runs_replay.py` 通過；三個測試指令通過；回報回滾方式（哪一份樣板）。
- **禁止事項**：共用規定；不同日再開其他配置閘門；不動 `max_swaps_per_run`、`min_holding_days` 的數字，除非使用者指定。

### P8 切 `agent_swap.exposure` v2（進入條件：P7 已上線、第 3 節該列成立且使用者明確下令並給出 `max_pairs_by_view`）

- **輸入**：使用者給的兩格數字、曝險影子的報告。
- **要改的檔案**：同 P6 的清單。
- **驗收條件**：`tests/decision/test_exposure_swap.py`、`test_exposure_swap_allocation.py`、`tests/automation/test_exposure_swap_pipeline.py` 通過；`max_pairs_by_view` 不高於 `max_swaps_per_run`；三個測試指令通過。
- **禁止事項**：共用規定；不自行填數字。

### P9 開 `reentry_cooldown` v3（進入條件：第 3 節該列成立且使用者明確下令）

- **要改的檔案**：`config/policy_versions/`、`config/decision_policy.json`（`reentry_cooldown.version: v3`、新 `policy_id`、真實 `available_at`）、`docs/status.md`、`AGENTS.md`、`README.md`、`docs/agent_autonomy_plan.md`。
- **驗收條件**：冷卻相關測試與舊封存重建測試通過；三個測試指令通過。
- **禁止事項**：共用規定；不改 `days`。

### P10 開 `slot_recycle`（進入條件：第 3 節該列成立且使用者明確下令）

- **要改的檔案**：`config/policy_versions/`、`config/decision_policy.json`（加 `slot_recycle: {"version": "v1"}`、新 `policy_id`、真實 `available_at`）、`docs/status.md`、`AGENTS.md`、`README.md`。
- **驗收條件**：`tests/decision/test_slot_recycle.py` 與舊封存重建測試通過；三個測試指令通過。
- **禁止事項**：共用規定；不與其他配置閘門同日開。

### P11 優質股第 3 階段實作（進入條件：使用者定案 `purpose` 列舉並下令開工）

- **輸入**：quality_dip_plan.md 第 4.2、10.3 節；agent_led_exposure_plan.md 第 11 節。
- **要改的檔案**（實作時確認）：`src/etf_agent/decision/swap_nomination.py`、`exposure_swap.py` 或新模組、`sizing.py`（閘門登記與前提）、`strategy_reflection/contracts.py`（禁區）、`allocation.py`、Skill `tradeagent/trading/trader/references/swap-nomination.md`、對應測試與文件。
- **驗收條件**：預設關閉；先只做影子（配置不讀）；結構條件逐條有 fail-closed 測試；沒有閘門的 Policy 與舊封存逐位元不變；三個測試指令通過；新增或大改 Skill 時另跑 `quick_validate.py`。
- **禁止事項**：共用規定；不開閘門；不新增程式挑股或排序的路徑；不放寬現金、換手、檔數。

### P12 估值 V2 實作（進入條件：使用者指定連續相符天數且達標、口徑釐清、下令開工）

- **輸入**：P4 的彙總、quality_dip_plan.md 第 2.6 節。
- **要改的檔案**（實作時確認）：估值位置事實的新模組、`sizing.py`（`valuation_facts` 閘門）、`strategy_reflection/contracts.py`、分析師與交易 Agent 簡報接線、Finalizer、Skill `fundamental-analyst` 與 `trader`、測試與文件。
- **驗收條件**：預設關閉；`citable=false`；三個視窗並列；`available_at` 用實際抓取時間、早於抓取時間的 cutoff 一律 `unavailable`；舊 run 逐位元不變；三個測試指令通過。
- **禁止事項**：共用規定；不開閘門；不設「便宜」門檻；不做 V3。

## 5. 需要人決定的事項

1. `agent_swap.exposure` v1 影子要不要在 `agent_swap` 影子期一開始就併開（第 2.3 節的取捨）。
2. 意圖落地率要不要訂數字目標；本檔建議滿 10 個決策日的新基準出來後再談。
3. 優質股第 3 階段的 `purpose` 列舉是否新增 `quality_dip`，以及第 1、2 階段要觀察幾個決策日。
4. 估值 V1「連續核對相符」要幾天才算數。
5. `rebound_participation` v2 在 `agent_swap` v2 之後的去留（它自己也要先滿 10 個決策日）。

## 6. 證據等級

第 1 節是設定檔、封存決策與唯讀工具輸出的事實；第 2 節的風險與第 3 節的日曆推算是推論。所有數字是 `replay_reference_only`。本檔不宣稱任何一項開了之後績效會更好；是否有效只有比賽期間的即時紀錄能回答。
