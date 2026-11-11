# 把策略裁量還給 Agent：設計計畫

狀態（2026-11-10，依程式與 `config/decision_policy.json` 核對）：各階段已實作，**只有階段 0 的統計工具與階段 3 的 `agent_swap` v1 影子期已開啟／可用**，其餘為「已實作、尚未開啟」（正式設定沒有這些欄位，不可當成現行行為）：

| 階段 | 實作 | 開啟 |
| --- | --- | --- |
| 0 意圖落地率 | `calibration/intent_landing.py`、`cli/calibration.py intent-landing` | 唯讀工具，已可用 |
| 1 `reentry_cooldown` v2 | 已實作 | **已於 2026-11-10 17:40 開啟** |
| 2 `drawdown_derisk` v5（`release_order`） | 已實作（`decision/release_order.py`、Skill `references/release-order.md`） | **已於 2026-11-10 17:40 開啟** |
| 3 `agent_swap`（步驟 1–6，細節見[換股提名計畫](agent_nominated_rotation_plan.md)） | v1 影子（提名、封存、`team_inputs` 2.9、降級版 `agent_step_degradable`）、v2 配置與 `shadow-swap` 報告皆已實作 | **v1 已於 2026-11-10 開啟**（`max_swaps_per_run 2`、`min_holding_days 5`）；v2 未開啟 |
| 4 現金與冷卻 | `cash_fit_order` v2、`high_conviction_cash_priority` v2、`held_weight_budget` v2（`held_weight_facts`）、`reentry_cooldown` v3 皆已實作 | 三個 v2 已於 2026-11-10 17:58 開啟；`reentry_cooldown` v3、`slot_recycle` 未開啟 |

### 三份相關計畫的總表（2026-11-10）

三份計畫都在把「換誰、買賣多少」的裁量還給 Agent，但範圍、閘門與回滾各自獨立，所以不合併，只在此對照順序與依賴。

| 計畫 | 做什麼 | 閘門 | 目前進度 | 依賴 |
| --- | --- | --- | --- | --- |
| 本計畫（階段 0～4） | 取消程式挑股，改成 Agent 提名與給順序 | `agent_swap`、`reentry_cooldown` v2／v3、`drawdown_derisk` v5 等 | 全部已實作；已開啟：`agent_swap` v1 影子、`reentry_cooldown` v2、`drawdown_derisk` v5（2026-11-10 17:40）與現金三個 v2（17:58）；未開：`agent_swap` v2、`reentry_cooldown` v3 | 階段 3 影子跑滿 10 個決策日（`calibration.MIN_DECISION_DAYS`）才考慮 v2 |
| [換股提名](agent_nominated_rotation_plan.md) | 本計畫階段 3 的細部設計 | `agent_swap` v1／v2 | v1 影子已開、v2 已實作未開 | 由 `shadow-swap` 報告決定是否切 v2 |
| [曝險主導](agent_led_exposure_plan.md) | 風險 Agent 看組合曝險，曝險理由併入換股提名 | `exposure_view` | 階段 1 已開（純紀錄）；階段 2（提名多 `purpose`）、階段 3（配置讀取）已實作、尚未開啟（`agent_swap.exposure`） | 階段 3 要先開 `agent_swap` v2 |
| [優質股](quality_dip_plan.md) | 找出被大盤拖累的優質股 | `quality_dip_facts` | 階段 1、階段 2（`quality_dip_assessment`、`event_direction`）三個閘門皆已於 2026-11-10 開啟（純紀錄）；估值 V1（收集與稽核）已掛進資料排程；階段 3 尚未實作（等曝險階段 2 的 `purpose` 列舉）；估值 V2／V3 未做 | 階段 3 的換股要走換股提名，不另開路徑 |

建議順序：換股提名影子期 → 決定 v2 → 曝險階段 2 → 曝險階段 3；優質股階段 1 的事實紀錄可隨時由人決定是否開，不擋其他項。

**實作註記（與下文設計不符或補充之處，下文各節保留當時的設計與推論）**：

- 階段 1：解除冷卻後，1815、3443、3533 在重算時仍被現金不足與名額擋住，所以 v2 如第 4 節所預期，只保證不再二次封鎖、不保證落地率上升。
- 階段 2：順序驗證用盡或呼叫失敗時，配置不整份拒絕，而是退回 v4 的 ATR 順序並留 `DERISK_ORDER_SKIPPED:order_*`（降級）；`release_order` 以 `team_inputs` 選配鍵存放、**不升版本**（2.9 已被換股提名占用，前提只有 `selection_ranking`）。
- 階段 3：換股提名是獨立的第二輪（5c），不是擴充階段 2 的同一份輸出；閘門在 `position_sizing.agent_swap`（不是 `rotation.agent_swap`），`team_inputs` 升為 2.9；v1 影子期配置不讀提名。
- 階段 4：`held_weight_facts` 只給現有權重、單股上限與 high 份額下限，**不含 medium／low 份額**（簡報時現金與廣度尚未確定）；`reentry_cooldown` v3 沒回應提示是「配置取消該買單」、不是驗證錯誤；`agent_swap` 的換入候選仍沿用冷卻名單排除。
- 影子報告（`shadow-swap`）的「若開 v2 會成交幾對」是離線估算，**只是上限**（動態取消無法重現），只算 `replay_reference_only`。

是否開啟任何一項、何時切換由人決定。

本檔的數字來自 `artifacts/decisions/` 的 11 份封存決策（目標交易日 2026-10-27～11-10），是「當時實際發生了什麼」的事後統計，不是績效，也不是對 LLM 判斷的前瞻驗證。行號為 2026-11-10 工作區（分支 `145-`）。

## 1. 問題與證據

作者的初衷是「由 LLM Agent 決定買賣」。現行設計是 Agent 給意圖與信心，程式用一連串版本閘門決定哪些意圖成交、另外賣哪些持股。程式確實「不改寫交易 Agent 的意圖欄位」，但最終訂單和意圖常常不同；被犧牲的是哪一檔，多半由程式用 ATR、代號或 beta 決定。

### 1.1 核對方法

對每份 run 讀 `intent.json`（`items[].intent`／`conviction`／`rationale`）與 `proposal.json`（`order_proposal.orders`、`constraint_flags`、`allocation_proposal`），另讀 `policy.json` 看當時開了哪些閘門。「落地」的定義：Agent 標 `buy`／`add` 的股票，最終有買單（不論張數是否被縮減）。

### 1.2 核對結果（事實）

| 審查者的說法 | 核對 | 結果 |
| --- | --- | --- |
| 11 份決策 buy／add 意圖 256 筆，最終有買單 40 筆 | 重現 | 256／40。其中 10-27 開帳日占 52／30；扣掉開帳日是 204／10 |
| 11-04 起 5 份為 89 筆中 6 筆 | 重現 | 89／6（11-04：21／0、11-05：30／1、11-06：12／1、11-09：16／1、11-10：10／3） |
| 11-04 有 6 檔 `hold` 被賣，約 4,845 萬、占當日賣單 76.9% | 重現 | 1815、2379、3443、3533、3653、6239，合計 48,450,690 元，占賣單總額 63,046,070 元的 76.85% |
| 1815 的 Agent 明說風險不足以賣出仍被全賣 | 重現 | 理由原文：「這些風險不足以賣出，但足以不加碼」；140 張全賣，旗標 `RISK_DRAWDOWN` |
| 11-05 的 6446 因買不到半份整筆取消 | 重現 | `RISK_CASH_FIT`：目標 38,000 股只買得到 10,000 股。11-06 又發生一次（48,000／8,000）；11-04 是被 `CASH_BUFFER` 降為 0 |
| 11-09 台積電因 `held_weight_budget` 不准加碼（現權重 4.97%） | 重現 | `RISK_HELD_WEIGHT`：現有 4.97% 高於等級份額 1.65%。同日另有 2880、2886、4904 |
| `reentry_cooldown` 擋掉 1815、3443、3533 | 重現 | 11-05，Agent 意圖分別是 buy／high、buy／low、buy／medium |
| 裁量約 Agent 四成、程式六成 | **未重現** | 這是架構判讀，沒有可重算的定義，本檔不採用這個比例 |
| `forced_swap` 自動換出 hold（列為第一優先） | **未重現** | 11 份決策沒有任何 `RISK_FORCED_SWAP` 旗標。11-04 的 hold 被賣全部來自 `drawdown_derisk` |
| `exposure_rotation`／信心差換股覆寫意圖 | **未重現** | 只有 11-10 一份 run 帶這個閘門，兩者都是 `…_SKIPPED`，沒有產生賣單 |

沒落地的 216 筆 buy／add，依旗標分（事實）：

| 原因 | 筆數 | 說明 |
| --- | --- | --- |
| `MAX_POSITIONS`（沒有名額） | 163 | 最大宗。細分原因（真的 30 檔滿、廣度目標、換股名額用完、信心低於 `rotation.min_new_conviction`）沒有逐筆查證 |
| `add` 但沒有任何旗標、也沒有買單 | 25 | 全部是 `add`。抽看 11-04、11-05 的 13 筆，現有權重都已高於信心份額，所以「加碼」等於不動 |
| `CASH_BUFFER` 降為 0 | 14（另 1 筆同時有 `RISK_DRAWDOWN`） | 姿態現金緩衝 |
| `RISK_HELD_WEIGHT` | 4 | 同上一類，11-08 之後才有旗標 |
| `RISK_REENTRY_COOLDOWN` | 3 | |
| `LOT_SIZE` | 3 | 目標金額不足一張 |
| `RISK_CASH_FIT` | 2 | 都是 6446（high） |
| `RISK_DRAWDOWN`（減碼後不再加碼） | 1 | |

### 1.3 從證據得到的判讀（推論）

1. **意圖落地率低，主因是名額與現金，不是個別小閘門。** 名額與現金本來就會淘汰候選；落地率低不等於程式做錯，也不代表績效差。
2. **真正「違反 Agent 明確意思」的案例集中在 `drawdown_derisk`。** 11-04 風險 Agent 選防守（當時回撤 2.6%，回撤下限仍是 aggressive，所以防守是風險 Agent 自己的判斷），現金要補到 20%，程式依 ATR 由高到低挑股賣（`src/etf_agent/decision/allocation.py:1686` 的 `_drawdown_derisk`，排序鍵在 1745–1756 行）。這其實是兩個 Agent 的判斷互相衝突（風險 Agent 要現金、交易 Agent 說這幾檔續抱），目前由程式用 ATR 裁決。
3. **連鎖覆寫。** 11-04 被程式賣掉的 1815、3443、3533，隔天 Agent 想買回，又被冷卻名單擋下。冷卻名單把「程式賣的」也算成出場（`decision/reentry_cooldown.py:30` 的 `recent_exit_symbols`，檔頭註明含程式強制換股與風控減至零）。
4. **`add` 的語意和 Agent 以為的不同。** 程式把 `add` 當成「目標權重＝信心份額」，持股本來就比份額重時什麼都不做（25＋4 筆）。Agent 不知道這件事。
5. **交易 Agent 沒被問過「要換掉誰」。** 交易 Agent 分批執行，看不到整體持股；名額配對由程式全域計算（`docs/decision-contracts.md` 的「滿檔換股」）。所以滿檔時 Agent 的新股意圖幾乎必然落空，除非程式的換股閘門替它挑人。
6. 審查者建議的優先順序把 `forced_swap` 與 `exposure_rotation` 排最前面；依封存紀錄，這兩者到目前為止沒有實際賣過任何持股（閘門存在、有能力賣，但沒觸發）。本檔的順序改以「已發生的覆寫」為先。

## 2. 原則

1. **硬規則歸程式，策略取捨歸 Agent。** 比賽規則、資料時間、證據、對帳、重算、封存一律由程式強制，Agent 不能放寬。「買誰、賣誰、先犧牲誰」是策略取捨，由 Agent 回答。
2. **程式只算數字與檢查可行性，不替 Agent 挑犧牲哪一檔。** 需要賣多少、能買幾張、還有幾個名額由程式算；「賣哪幾檔來湊」由 Agent 給先後順序。
3. **Agent 仍然不輸出數字。** 新增的 Agent 輸出只能是列舉、股票代號與先後順序（與 `selection_ranking` 相同做法），權重、張數、金額仍由程式算並由 Finalizer 重算。
4. **只提示、不強制**（設計法，以下各階段共用）：
   - 程式把事實與警告放進 Agent 的簡報（例如「現金低於所選姿態的緩衝」「這檔 3 日內才出場」「這檔論點檢視判定被推翻」）。
   - Agent 必須對每個警告明確回應一次（採納並行動，或說明為何不動作），做法比照現有的持股檢視警示（`review:<代號>:<旗標>` 必須在 `adopted_claim_ids` 剛好處理一次）。
   - 程式檢查回應的可行性（硬規則、現金、整張、換手）。不可行時帶中性措辭的錯誤回饋讓 Agent 重選，次數沿用現有修正循環上限。
   - 用盡仍不可行就拒絕，不由程式代選、不夾到邊界。「拒絕」的範圍（整份決策或只是該動作）見第 6 節第 2 項。
5. **背景不是證據的規定不變。** 新增的提示若來自前次 run、論點檢視或程式計算的組合狀態，仍是 `citable=false`；Agent 的行動仍要由本次 claim 或持股檢視事實支持。
6. **版本閘門總則不變。** 每一項都以新版本或新欄位加入，舊 Policy／舊 run 逐位元維持原重建行為；只由人設定，策略反思不得改。

## 3. 閘門分類與處置

分類：**硬規則**（比賽或資料完整性要求，違反即無效）、**合理護欄**（不是比賽規則，但防止整批作廢、無法結算或明顯的操作失誤）、**策略性限制**（替 Agent 做策略取捨）。

### 3.1 硬規則：全部保留

| 項目 | 處置 | 理由 |
| --- | --- | --- |
| 交易池、可交易狀態（`RISK_NOT_TRADABLE`）、只能做多 | 保留 | 比賽規則 |
| 持股 20～30 檔、現金低於 25%、單股 10%（2330 為 25%）、Active Share ≥ 15% | 保留 | 比賽規則（`config/competition_rules.json`、`config/decision_rules.json`） |
| 整張、費稅、滑價、淨額交割（`reuse_sell_proceeds`）、`insufficient_funds_policy: void_batch` | 保留 | 對應主辦方結算口徑 |
| 來源授權、`decision_cutoff`／`available_at`、證據引用與種類（`typed_evidence_v1`） | 保留 | 資料時間一致與可稽核 |
| 主辦方資料包對帳、帳本結算、`history_ref` 重算 | 保留 | 帳戶為真實紀錄 |
| Finalizer／Validator 整份重算、不可變封存、無法驗證就拒絕 | 保留 | fail-closed |
| CompetitionGuard、備援 D-Plan 絕不自動上傳 | 保留 | 最後一道檢查 |

### 3.2 合理護欄

| 項目 | 處置 | 理由 |
| --- | --- | --- |
| `stock_limit_headroom`、`buy_gap_reserve`、`cash_ceiling_fix` | 保留 | 防止漲價或開高後違反硬規則、整批作廢 |
| `turnover_fit`＋`max_turnover_rate` 0.3 | 保留 | 不是比賽規則（契約文件已註明），但它是「Agent 自由度變大後可能多交易」的主要上限；放寬與否另案討論 |
| `sector_fit`＋`max_sector_weight` 0.6 | 保留 | 只在 `config/decision_rules.json`，`competition_rules.json` 沒有這一條；是否為主辦方要求本檔沒有查證，先當護欄 |
| `drawdown_control`（回撤 5%／10% 的姿態下限與解除門檻） | 保留 | 帳戶層級的最後防線，只約束風險 Agent 的姿態，不挑股 |
| 風險 Agent 的現金姿態、`cash_grade`、持股廣度對應 Policy 數字 | 保留 | 這本來就是 Agent 的判斷，程式只做列舉到數字的對映 |
| `cash_buffer_floor` | 保留 | 現金緩衝是風險 Agent 的決定，程式只執行 |
| `RISK_MIN_POSITIONS`（依 ATR 留住出場持股）、`RISK_ACTIVE_SHARE`（取消重疊最多的候選） | 先保留，下一批再評估 | 為了滿足硬規則而由程式挑股。11 份決策都沒有觸發；等前幾階段的 Agent 順序上線後，可改讀同一份順序 |
| `selection_ranking`、`position_review`、`hold_conviction` | 保留 | 已經是「Agent 判斷、程式驗證」的做法 |
| `conviction_volatility_v2` 等級乘數、`limit_scaled_conviction` alpha、`positions_by_breadth` | 保留 | 列舉到數字的對映，由人設定；alpha 是否偏高見下一批 |

### 3.3 策略性限制

| 項目 | 現行行為 | 處置 | 理由 |
| --- | --- | --- | --- |
| `drawdown_derisk` 的挑股順序 | 依 ATR 高到低賣到緩衝 | **改成提示**：要補多少由程式算，賣的先後由交易 Agent 給（階段 2） | 已發生的最大覆寫（11-04） |
| `reentry_cooldown` 把程式賣出也算出場 | 3 日內不得買回 | **先收窄**：只計 Agent 自己標的出場（階段 1）；之後整個**改成提示**（階段 4） | 連鎖覆寫（11-05） |
| `rotation.forced_swap` | 程式依代號換出「hold 且論點被推翻且有採納空頭 claim」的持股 | **改成提示**：論點被推翻是提示，是否讓出名額由 Agent 回答（階段 3） | 沒有觸發紀錄，但有能力賣 Agent 標 hold 的持股 |
| `exposure_rotation`、`conviction_gap_swap`、`rebound_participation` 的換股 | 程式依 beta、持有天數、信心差挑換出者 | **改成提示**：組合 beta、轉向事實放進簡報；誰換誰由 Agent 回答（階段 3）。`turn_assessment` 回到純紀錄 | 同上；這條鏈的參數最多、互動最難預測（見 [exposure_rotation_v2_plan.md](exposure_rotation_v2_plan.md) 第 4 節） |
| `cash_fit_min_fill_ratio`（新股買不到半份整筆取消） | `RISK_CASH_FIT` | **移除**（新版本不取消，買得到幾張買幾張；階段 4） | 現金分配已經照 Agent 的信心與名次，取消零頭是程式的偏好。11-06 起 high 已不取消 |
| `high_conviction_cash_priority` 的封鎖（high 被縮減後擋掉後面未持有的中低信心新股） | `CASH_BUFFER` 降為 0 | **移除封鎖**，保留「現金先給 high」（階段 4） | 先後順序已由名次決定，不需要再整批擋 |
| `held_weight_budget` 與 `add` 的語意 | 現有權重高於份額就不加碼 | **改成提示**：簡報列出現有權重與信心份額，讓 Agent 知道這個 `add` 不會成交（階段 4） | 預算算法本身合理，問題是 Agent 不知情 |
| `rotation.min_new_conviction`、`rotation.max_swaps_per_run` | 換股的信心門檻與檔數上限 | 保留 `max_swaps_per_run`；信心門檻併入階段 3 重新檢視 | 檔數上限是控制換手的護欄 |
| `trim_fraction` 固定 0.5 | `trim` 一律賣一半 | 下一批：**改成列舉**（例如輕／半／重，對映由 Policy 設） | Agent 無法表達減碼幅度 |
| `cash_ratio_range.max_daily_raise` 0.03 | 現金每日最多拉高 3 個百分點 | 下一批：**改成提示**或由人放寬 | 限制的是風險 Agent 的判斷；但它同時防止現金大幅來回，風險較高，排後面 |
| `limit_scaled_conviction` alpha 0.6 | high 份額至少為單股上限的 60% | 下一批：由人檢視數值 | high 份額大，吃掉其他候選的現金（6446 一檔就占去大半可用現金） |
| `slot_recycle`（未開啟） | 買不到一張的新股釋出名額 | 維持未開啟，階段 4 一併評估 | 方向與本計畫一致（讓更多意圖落地） |

## 4. 分階段實作

共通規則：每階段都是新版本或新欄位；舊版本的程式路徑不動；先加測試、再由人改 `config/decision_policy.json`；切換前把現行樣板存到 `config/policy_versions/`；`policy_id`／`available_at` 用真實切換時間、不回填。每階段都要跑 `AGENTS.md`「測試與驗證」的三個指令。

### 階段 0：意圖落地率統計（不改行為）

- 內容：把第 1.1 節的統計寫成唯讀工具（建議放在 `src/etf_agent/calibration/` 或 `evaluation/`，不被 `decision/`、`automation/` 匯入），每份 run 輸出：buy／add 落地數、未落地原因分佈、「Agent 標 hold／add 卻被程式賣出」的清單與金額。
- 閘門：無。
- 驗證：對現有 11 份封存跑出的數字要等於第 1.2 節。
- 測試：正常流程；封存缺檔或雜湊不符時拒絕、不輸出部分結果。
- 回滾：刪除工具即可，不影響決策鏈。
- 目的：之後每階段是否有效，都用同一把尺看即時紀錄。

### 階段 1：`reentry_cooldown` v2，只計 Agent 自己的出場（最小、風險最低）

- 內容：冷卻名單只收「交易 Agent 的意圖是 `exit`／`forced_exit`」的出場；程式造成的出場（`RISK_DRAWDOWN`、`RISK_FORCED_SWAP`、`RISK_EXPOSURE_ROTATION`、`RISK_CONVICTION_GAP_SWAP`、`RISK_STOCK_WEIGHT` 減至零）不計。v2 需要多讀前次 run 的 `intent.json`，並照舊驗證 manifest。
- 為什麼最小：不改任何 Agent 契約、不升 `team_inputs`、不新增 Agent 呼叫；只縮短一份封鎖名單。硬規則、現金緩衝、換手上限、名額全部照舊。
- 改哪裡（實作時才動）：`decision/reentry_cooldown.py`（依 `version` 分派，v1 函式本體不動）、`decision/sizing.py` 的閘門值域、`allocation.py:586–600` 不需要改（只讀 Policy 綁好的名單）。
- 驗證（`replay_reference_only`）：以 11-05 的封存輸入重算，v2 名單應為 {3189、3264}（Agent 自己出場的兩檔），1815、3443、3533 回到候選。**它們當天是否真的買得到沒有試算**；防守姿態下很可能仍被現金緩衝擋下，所以這一階段不保證落地率上升，只保證不再因程式的賣出而二次封鎖。
- 測試：
  - 正常：Agent 出場的股票仍被擋；程式賣出的股票不被擋；同一檔兩種來源都有時仍被擋。
  - fail-closed：前次 run 的 `intent.json` 缺少、損壞或 manifest 不符一律拒絕（不得當成「沒有出場」而解除冷卻）；`version` 不在值域拒絕；Policy 預填名單與重算不符拒絕。
  - 相容：v1 的 Policy 與現有 11 份 run 重建雜湊不變（`tests/decision/test_archived_runs_replay.py`）。
- 回滾：把 `reentry_cooldown.version` 改回 `v1`。
- 已知風險：程式今天賣、Agent 明天買回的來回交易。上限是現金緩衝與 `max_turnover_rate`；階段 2 上線後程式不再自己挑股賣，這個風險會變小。

### 階段 2：`drawdown_derisk` v5，減碼先後由交易 Agent 決定

- 內容：
  - 交易 Agent 第二輪（現有 `selection_ranking` 那一步，`src/etf_agent/automation/team_stages.py:432`，各批合併後、能看到全部持股）多輸出一份「若需要降低持股，先放掉誰」的先後順序（暫名 `release_order`）：涵蓋全部可減碼的持股各一次，附理由，不輸出數字。
  - 這份順序每天都產生，只有在防守姿態且現金不足時才被程式使用。這樣不需要改動「交易 → 第二輪 → 風險 Agent 現金姿態 → 配置」的順序（`automation/daily_pipeline.py:345–376`）。
  - 程式照舊算缺口、單次上限（NAV 的 25%）、換手餘額（v4 的規則）、不低於 20 檔；只把排序鍵由 ATR 換成 Agent 的順序。賣單意圖仍由程式標 `trim`／`exit`、旗標仍是 `RISK_DRAWDOWN`，但說明文字改為「依交易 Agent 的順序」。
  - 簡報要附每檔的事實（現有權重、帳面報酬、ATR、Agent 自己本次的意圖與信心），不附風險 Agent 的姿態（此時還沒產生）。
- 閘門：`drawdown_derisk.version: v5`，前提 `selection_ranking`；`team_inputs` 需要升版（暫定 2.9）存這份順序，Finalizer 重建比對順序、id 與雜湊。v1～v4 路徑不動。
- 驗證（`replay_reference_only`，且有限制）：封存的舊 run 沒有這份順序，無法直接重放 v5。只能做兩件事：(a) 給定「與 ATR 相同的順序」時 v5 與 v4 的訂單逐位相同（等價測試）；(b) 用 11-04 的輸入、人工給一份順序，確認賣出總額與 v4 相同、只是股票不同。Agent 會排出什麼順序只有即時紀錄能回答。
- 測試：
  - 正常：依順序賣到目標；順序第一檔不夠時繼續往下；交易 Agent 已標 `exit`／`trim`、不可交易、被留住的出場不在可減碼集合內。
  - fail-closed：順序缺漏、重複、含未持有股票、含數字或前次 run 識別碼一律拒絕並走修正循環；用盡後依第 6 節第 2 項的決定處理；Policy 是 v5 但 `team_inputs` 沒有順序時拒絕；順序被改寫時 Finalizer 拒絕。
  - 相容：v1～v4 的 Policy 與舊 run 重建不變。
- 回滾：`drawdown_derisk.version` 改回 `v4`（順序仍會產生但不被使用，或同時移除前提）。

### 階段 3：換股由 Agent 提名，取代程式挑換出者

- 內容：
  - 擴充階段 2 的同一份輸出：交易 Agent 對每檔持股另給列舉「是否願意為新股讓出名額」，並對程式給的每個換股提示各回應一次。
  - 提示來源（都是事實或背景，不是指令）：論點檢視判定被推翻（原 `forced_swap` 的名單）、組合 beta 低於姿態目標與各檔 beta（原 `exposure_rotation`）、新股 high 對持股 low（原 `conviction_gap_swap`）、轉向事實（原 `rebound_participation`）。
  - 程式只配對「Agent 願意讓出的持股」與「沒有名額的新股」（依 Agent 的順序與名次），並檢查可行性：不低於 20 檔、現金買得起至少一張、換手上限、`max_swaps_per_run`、不只賣不買。不可行就帶回饋讓 Agent 重選。
  - Agent 沒有願意讓出的持股時就不換，不由程式補挑。
- 閘門：新增一個換股閘門版本（暫名 `rotation.agent_swap: {version: v1}`），與 `forced_swap`、`exposure_rotation`、`rebound_participation` 互斥（同時出現即拒絕）；後三者的程式路徑保留供舊 Policy 重建。`hold_conviction` 目前綁在 `exposure_rotation` 之下，需要一併決定去留。
- 驗證：等價測試只能做到「Agent 的選擇剛好等於舊規則會選的人」時訂單相同；其餘只能看即時紀錄。回放工具目前略過 `exposure_rotation` 與 `rebound_participation`（`src/etf_agent/evaluation/replay.py:285–291`），新閘門也會被略過，不能用回放證明它有效。
- 測試：
  - 正常：有提名且可行時成交；提名多於名額時依順序取用；提示逐一被回應。
  - fail-closed：提示漏回應或重複回應、提名未持有或已標 `exit` 的股票、互斥閘門同時存在、違反 20 檔下限、只賣不買，一律拒絕或取消該對並留旗標；論點檢視名單與 Policy 綁定不符拒絕。
  - 相容：帶 `forced_swap`／`exposure_rotation`／`rebound_participation` 的舊 Policy 與舊 run 重建不變。
- 回滾：換回 `config/policy_versions/` 的前一份樣板（恢復三個舊閘門、移除新閘門）。
- 這是改動最大的一階段，建議在階段 2 有至少 10 個決策日（`calibration.MIN_DECISION_DAYS`）的即時紀錄後再做。

### 階段 4：現金分配的小規則與冷卻改成提示

- 內容（各自獨立的小版本，可分開開啟）：
  - `cash_fit_order` v2：新股買不到半份不再整筆取消，買得到幾張買幾張（`allocation.py:1968–1982` 的取消分支）；`cash_fit_min_fill_ratio` 在 v2 不允許出現。
  - `high_conviction_cash_priority` v2：保留現金先給 high，移除「high 被縮減後擋掉後面未持有的中低信心新股」。
  - 交易 Agent 簡報附每檔持股的現有權重與信心份額（事實），讓 Agent 知道 `add` 會不會成交；不改預算算法。
  - `reentry_cooldown` v3：名單不再擋買單，改成簡報裡的提示，Agent 買回時必須回應該提示。
  - 是否同時開啟 `slot_recycle`（已實作、未開啟）一併決定。
- 驗證：前兩項不需要新的 Agent 輸出，可以用封存意圖重放（`replay_reference_only`），比較落地筆數、持股檔數、塵埃持股數與換手。
- 測試：正常分支之外，確認取消零頭後不會讓持股超過 30 檔、不會讓現金低於緩衝下限、不會超過換手上限；舊版本重建不變。
- 回滾：各欄位改回 `v1`。

### 下一批（先不排時程）

`trim` 幅度改列舉、`limit_scaled_conviction` alpha 檢視、`cash_ratio_range.max_daily_raise` 改提示、`RISK_MIN_POSITIONS`／`RISK_ACTIVE_SHARE` 改讀 Agent 的順序。各自另立計畫。

## 5. 風險與取捨

| 風險 | 說明 | 怎麼降低 |
| --- | --- | --- |
| 比賽進行中、帳戶是真實紀錄 | 任何行為改變都直接進帳本，錯了無法重來 | 一次只開一個階段；每階段先存樣板、可一行回滾；備援 D-Plan 與人工上傳不變 |
| Agent 多交易 | 換股與買回的自由變大，費稅上升 | `max_turnover_rate` 0.3、`max_swaps_per_run`、現金緩衝全部保留；階段 0 的統計每天看換手 |
| Agent 亂賣或不肯賣 | 減碼順序排得不好；或防守時該賣卻全部標不願意 | 減碼的「量」仍由風險 Agent 的姿態與程式決定，Agent 只決定先後；硬規則與回撤下限不變 |
| 交件時間 | 多一份輸出與可能的修正循環，耗時增加 | 併入既有第二輪、不新增階段；逾時規則與 08:55 期限不變；修正循環次數不加 |
| 驗證薄弱 | 回放只固定封存的 Agent 意圖重算程式，標 `replay_reference_only`；階段 2、3 需要 Agent 的新輸出，舊封存沒有，連回放都做不到 | 只用等價測試證明「不改變既有情況」；是否有效只看比賽期間的即時紀錄，每階段至少觀察 10 個決策日再下結論 |
| 樣本太少 | 目前證據只有 11 份決策、一次明確的覆寫事件（11-04） | 本檔不宣稱改了會比較好；主張的是「這些取捨該由誰做」，不是績效 |
| 契約變動面大 | 階段 2、3 會改交易 Agent 契約、`team_inputs` 版本、Skill 與 Finalizer | 分階段、每階段獨立版本；階段 1 與階段 4 的前兩項完全不動 Agent 契約 |
| 兩個 Agent 的衝突沒有消失 | 風險 Agent 要現金、交易 Agent 要續抱時，還是有人要讓步 | 本計畫的取捨：量聽風險 Agent、對象聽交易 Agent。這是設計選擇，不是唯一解 |

取捨的另一面（推論）：現行閘門很多是為了修正特定日子觀察到的問題而加的（塵埃持股、來回交易、防守時不降曝險）。拿掉它們，那些問題可能回來；本計畫的立場是讓 Agent 在知情下自己處理，並保留能量化的上限。

## 6. 需要人決定的事項（5 項）

1. **是否接受以「已發生的覆寫」排序**（先冷卻收窄、再減碼順序、最後才是換股鏈），而不是審查者建議的 `forced_swap` 優先。建議：接受。`forced_swap` 與 `exposure_rotation` 到目前沒有賣過任何持股。
2. **修正循環用盡時拒絕的範圍。** 選項：(a) 整份決策拒絕，走備援 D-Plan（全部續抱）；(b) 只取消該動作（不減碼、不換股），其餘訂單照常，留旗標。建議：硬規則不過維持 (a)；減碼順序與換股提名這類非硬規則的動作用 (b)，否則一份排序沒過就丟掉整天的決策。**已決定（2026-11-10，使用者選擇）：採上述建議。**硬規則整份拒絕；減碼順序與換股提名失敗只取消該動作並留旗標。
3. **階段 1 是否現在就開。** 建議：可以先做，改動最小；但要接受它可能不會提高落地率（第 4 節已說明）。
4. **階段 2 的衝突裁決方式**：量由風險 Agent 的姿態決定、對象由交易 Agent 決定。另一個選項是讓交易 Agent 可以拒絕減碼（現金補不到緩衝）。建議：採前者，保留風險 Agent 的防守權。
5. **階段 3 之後 `rebound_participation` v2 的去留。** 它 2026-11-10 才開啟，還沒有觀察期。建議：先觀察滿 10 個決策日再決定是否併入階段 3，不要在同一段時間同時改兩件事。

## 7. 要同步更新的文件（本次只列出，不修改）

實作並開啟任一階段時：

| 文件 | 要改的地方 |
| --- | --- |
| `AGENTS.md` | 「每日決策鏈的 Agent 邊界」（交易 Agent 第二輪的新輸出、提示必須回應）、「程式配置與風控」（哪些挑股改由 Agent 決定）、版本閘門啟用狀態摘要、`team_inputs` 版本 |
| `README.md` | 架構摘要與閘門清單 |
| `docs/status.md` | 「版本閘門啟用狀態」表（唯一出處）與已知限制 |
| `docs/decision-contracts.md` | 交易 Agent、`team_inputs` 版本表、各受影響閘門的小節、「程式風控不改寫意圖」 |
| `docs/architecture.md` | 「一天怎麼跑」、「風控與配置規則」表、`team_inputs` 版本表 |
| `docs/decision-details.md`、`docs/glossary.md` | 白話說明與新旗標、新名稱 |
| `docs/README.md` | 文件索引與變更紀錄加上本檔 |
| `docs/exposure_rotation_plan.md`、`docs/exposure_rotation_v2_plan.md`、`docs/rebound_participation_plan.md`、`docs/rebound_participation_v2_plan.md` | 階段 3 開啟時加註被取代的說明 |
| `docs/operations.md` | 每日檢視要看的新旗標與落地率統計 |
| `tradeagent/trading/trader/`（`SKILL.md` 與 `references/ranking.md`） | 第二輪的新輸出與提示回應規則 |
| `config/policy_versions/README.md` | 新增的樣板快照說明 |
| `src/etf_agent/strategy_reflection/contracts.py` 的 `FORBIDDEN_PREFIXES`（約 136 行）與其 README | 新閘門欄位加入禁區 |
| `src/etf_agent/evaluation/README.md` | 新閘門在回放中的處理（略過或重算） |

## 8. 證據等級

本檔第 1 節是封存決策的事後統計（事實）；第 1.3、3、5 節的判讀是推論。沒有任何一項是對「改了之後績效會更好」的驗證。之後各階段的回放與等價測試都只算 `replay_reference_only`；只有比賽期間的即時紀錄算證據。
