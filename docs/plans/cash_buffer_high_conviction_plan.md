# 防守現金緩衝下的高信心買進：設計計劃

狀態：**已實作（2026-11-05，採第 6 節推薦案：只限 high、floor_ratio 0.75、帶狀減碼、減碼日不套用；不採 D2），正式設定尚未開啟**。本檔不改任何程式或設定；以下「事實」附出處，「推論／估算」另行標示。所有用封存輸入重算的數字都是 `evidence_grade: replay_reference_only`：只固定已封存的 Agent 意圖重算確定性程式，不是對 LLM 判斷的前瞻驗證。

姊妹計劃：[依單股上限放大的信心權重](limit_aware_conviction_weights_plan.md)（處理份額與單股上限）。本計劃只處理「現金緩衝擋住高信心買單」，份額公式不重複；兩者的交互見第 5.4 節。姊妹計劃第 12 節列為「尚未查證」的「現金為何掉到 20% 以下」，由本檔第 3 節回答（姊妹檔未改）。

## 1. 問題

防守姿態的現金緩衝（`cash_buffer_by_stance.defensive` 0.20）加上 `position_sizing.cash_buffer_floor: "v1"`，使「現金已低於緩衝時，原有現金完全不能動用，只有本次賣出收入可以買」。2026-11-05 07:00 的實例中，高信心、排名第 2 的未持有新股 6446.TW 被 `RISK_CASH_FIT` 整筆取消，排名較後的中信心 1802.TW 反而成交。使用者的期望：**最想買的（高信心）不應被緩衝擋住而買不到。**

近幾日姿態多為 defensive（第 3.1 節），所以「防守＝買進只能靠當日賣出」已經是常態，不是偶發。

## 2. 事實盤點：現金相關規則如何交互

### 2.1 哪些是比賽規則、哪些是本隊自設

| 規則 | 來源 | 性質 |
| --- | --- | --- |
| 現金比例必須 < 25% | `rules.cash_weight_must_be_below: "0.25"`（`decision_input.json` 的 `rules`）→ Policy `cash_weight_ceiling`，硬綁定見 `allocation.py:206-236`；Guard `CASH_WEIGHT` 在 `risk.py:361-363` | **比賽規則** |
| 現金不足整批作廢 | 主辦方結算；DataPack `prev_day_orders.outcome=voided`、`void_reasons` 含 `insufficient_funds`（`competition/datapack.py:37-39`）；本隊虛擬帳本以 `insufficient_funds_policy: "void_batch"` 模擬（`sizing.py:81-84`、`ledger/execution.py:18-22`） | **比賽規則**（本隊以閘門對齊） |
| 賣款當日可用 | `rules.reuse_sell_proceeds: true`（主辦方淨額交割） | **比賽規則** |
| 姿態現金緩衝 3%／10%／20% | `config/decision_policy.json` 的 `position_sizing.cash_buffer_by_stance`；`trader.py:248-257` 依風險 Agent 的姿態寫入 Policy 的 `cash_buffer_rate`；驗證 `sizing.py:408-419、444-447`（須 aggressive ≤ neutral ≤ defensive 且低於現金上限） | **Policy 自設**（策略反思可調旋鈕，`strategy_reflection/contracts.py:94-95`，上限 0.22、調高＝降風險） |
| `cash_buffer_floor` | `sizing.py:36-38`、`allocation.py:1491-1494` | Policy 自設版本閘門（禁區，`contracts.py:144`） |
| `cash_fit_order`、`cash_fit_min_fill_ratio` | `sizing.py:66-70`、`allocation.py:1502-1562` | Policy 自設版本閘門 |
| `buy_gap_reserve`（3%） | `sizing.py:77-80`、`allocation.py:1533-1536` | Policy 自設（為避免主辦方整批作廢） |
| `cash_ceiling_fix` | `sizing.py:71-74`、`allocation.py:331-353` | Policy 自設（為守比賽現金上限） |
| `drawdown_derisk` v3 | `sizing.py:94-104`、`allocation.py:1338-1424` | Policy 自設 |
| `turnover_fit`、`max_turnover_rate` 0.3 | `sizing.py:89-92`、`allocation.py:1275-1336`；Guard `TURNOVER` | 換手上限屬 rules 綁定之外的 Policy 參數（`max_turnover_rate` 在反思禁區） |

結論（事實）：**「防守時原有現金不能動」完全是本隊 Policy 的設計，不是比賽規則。** 比賽只要求現金 < 25% 與交割時現金足夠。

### 2.2 配置流程中的現金路徑（`AllocationOrderEngine._run`，`allocation.py:398-719`）

1. 出場／減碼先算（445-493 行）。
2. `drawdown_derisk`（497-503、1338-1424 行）：只在 `cash_stance == defensive` 時動作；把**交易 Agent 的賣單收入算進現金**後，若仍低於 `NAV × cash_buffer_rate`（1370 行的 `shortfall`），依 ATR 高→低、信心低→高減碼，一次最多 NAV × 25%。v3 另把被減碼的持股排除在加碼候選外（512-521 行）。
3. 名額與份額：`_conviction_targets`（905-1007 行），預算 `budget = 1 − cash_buffer_rate − held_outside`（989 行）；份額細節見姊妹計劃第 2 節。已持有者 `desired = max(current, desired)`（606-607 行）。
4. `_fit_cash`（1461-1594 行，呼叫在 621-625 行，`floor_disabled=bool(derisk_flags)`）：
   - `available = starting_cash（settled）+ 賣單淨收入`（1487-1489 行）。
   - `buffer = NAV × cash_buffer_rate`；`cash_buffer_floor: v1` 且當日**沒有**觸發減碼時，`buffer = min(buffer, starting_cash)`（1490-1494 行）。這就是「現金已低於緩衝時只剩賣款可用」的來源；當日有減碼則緩衝回到完整的 `NAV × rate`（減碼收入不得拿去買替補，1473 行 docstring）。
   - `cash_fit_order: v1`：買單依（信心、名次、代號）取得現金（1506-1519 行）。
   - 逐張減少直到 `available − cost − (累計買額 + cost) × 0.03 ≥ buffer`（1530-1548 行，含跳空預留）。
   - 未持有的新股若被現金縮到低於目標 × `cash_fit_min_fill_ratio`（0.5）就整筆取消並記 `RISK_CASH_FIT`，現金留給後面的買單；已持有的 add 只減量（1549-1562 行）。
5. 之後依序 `_fit_stock_weights`、`_fit_sector_weights`、`_fit_turnover`（627-636 行）；`turnover_fit` 啟用時 `_fit_cash` 不擋換手（1495-1497 行），改由 `_fit_turnover` 依信心低→高、ATR 高→低縮減。
6. `_run_min_positions`（318-357 行）：持股數不足時留住 exit；`cash_ceiling_fix` 時 base 情境現金達 25% 再多留 exit（`RISK_CASH_CEILING`）。
7. `run()` 的 Active Share 迴圈（253-274 行）。
8. 另一處重複的緩衝邏輯：`_forced_swap_victims` 的現金可行性檢查（789-794 行）同樣用 `min(NAV × rate, starting_cash)`。

### 2.3 一個既有的不一致（事實＋推論）

`drawdown_derisk` 把交易 Agent 的賣款算成「已補到緩衝」（1363-1371 行），但 `_fit_cash` 的 `cash_buffer_floor` 又允許買單把同一筆賣款花掉。11-05 07:00：2890 減碼 12.96M 使買前現金估為 20.26% ≥ 20%，所以沒有減碼；接著 1802 用掉這筆賣款，最終現金 18.97% < 20%（`proposal.json` 的 `allocation_proposal.cash_weight`）。推論：現行設計下，防守姿態的實際落點取決於「當天交易 Agent 有沒有自己賣」，並不穩定。

## 3. 現金為什麼會掉到緩衝以下（查證結果）

### 3.1 逐日姿態與現金（事實，`artifacts/daily/*/proposal.json`、`policy.json`、`var/datapack/DataPack_62_*.json`）

| 決策 run | 姿態 | 回撤／`stance_floor` | 起始現金 | 預計現金 |
| --- | --- | --- | --- | --- |
| 10-30 07:00 | neutral | 0.34%／aggressive | 10.02% | 11.03% |
| 11-02 07:00 | defensive | 0.41%／aggressive | 11.01% | 11.51% |
| 11-03 07:00 | neutral | 0.79%／aggressive | 11.55% | 10.50% |
| 11-04 03:34 | defensive | 2.63%／aggressive | 10.72% | 18.89% |
| 11-04 07:12 | defensive | 2.63%／aggressive | 10.72% | 17.17% |
| 11-04 21:56（11-05 前晚） | defensive | 2.79%／aggressive | 18.93% | 20.00% |
| 11-05 07:00 | defensive | 2.79%／aggressive | 18.93% | 18.97% |

- 主辦方 DataPack：`cash_ratio_tradable` 11-04 為 0.107136（30 檔），11-05 為 0.189264（25 檔），兩日 `prev_day_orders.outcome` 皆 `executed`。
- **防守是風險 Agent 依市場風險主動選的，不是回撤強制**：三份 `cash_stance.json` 的理由都寫明「stance_floor 為 aggressive」，回撤 2.6%～2.8%，未達 `neutral_at` 5%。理由文字也都預先寫出代價：「用原有現金的買單會被緩衝擋掉，只剩賣股收入可買替補」（來自 `tradeagent/risk/portfolio-risk-review/SKILL.md:19` 的說明）。

### 3.2 11-04 現金由 10.7% 升到 18.9% 的原因（事實）

- 11-04 兩個 run 都是 `drawdown_derisk.version: "v1"`（`policy.json`），由 `RISK_DRAWDOWN` 減碼 1815、3443、1802、3653、6239、3533、2615、2379，加上交易 Agent 自己的 3189 出場、8046 減碼。
- **主辦方實際執行的是 03:34 那一版**：DataPack 11-05 的 2615.TW 為 14,000 股、3653.TW 仍持有 1,000 股、現金 183,975,141，與 03:34 proposal（2615 減到 14,000、預計現金 183,853,582）一致，與 07:12（2615 未減、3653 出場、預計現金 167,111,724）不一致。目前 `artifacts/competition_d_plan/D-Plan_TEAM_11067_2026-11-04.json` 的 `agent_metadata.run_started_at` 是 03:34，`.bak` 是 07:12。本機帳本 `close-2026-11-04` 綁的是 07:12 的 `decision-20261103T231233Z`，之後有 `rebase-prepare-20261104T135609Z-datapack` 對齊主辦方。上傳流程本身**無法從封存完全判斷**，但持股與現金只與 03:34 一致。

### 3.3 為什麼停在約 18.9% 而不是 20%（推論，附估算）

03:34 run 的 1802.TW（83,000 股）與 3653.TW（1,000 股）有 `RISK_DRAWDOWN` 旗標卻沒有賣單：交易 Agent 對兩檔標 add，v1 的 `desired = max(current, desired)` 把減碼加回（即 v3 修掉的問題，`allocation.py:509-511` 註解、`docs/decision-details.md:70`）。兩檔以 11-04 決策價約 5.62M＋6.04M ≈ NAV 的 1.2%；18.89% + 1.2% ≈ 20.1%，與「v1 加回造成缺口」相符。

### 3.4 結構性結論

- 即使現金剛好補到 20%（11-04 21:56 的 v3 結果），只要姿態維持 defensive，`cash_buffer_floor` 讓現金**只能升不能降**：原有現金永遠凍結，買進只能靠當日賣出。
- 11-05 07:00 沒有觸發減碼，是因為交易 Agent 自己減碼 2890（第 2.3 節），不是因為現金已足夠。

## 4. 2026-11-05 07:00 的實際配置（事實）

- 起始現金 183,975,141，以決策價重算的 NAV 972,055,541（`account_snapshot.nav`、DataPack `nav_tradable`），現金 18.93%；緩衝 `min(194.4M, 184.0M) = 184.0M`，可用＝2890.TW 賣款 12,961,609。（交易後的 `allocation_proposal.nav` 為 971,954,802。）
- 買單取得現金的順序（`rank_by_symbol`）：high：2330（1）→ 6446（2）→ 1815（3）；medium：4904（1）→ 1802（2）→ …
  - 2330.TW 已持有 5.12% > 目標 4.98%，**沒有買單，沒有搶走現金**（姊妹計劃第 1 節）。
  - 6446.TW 是第一個拿現金的買單：目標 38,000 股（約 47.5M），現金只夠 10,000 股（26%）< 50% → `RISK_CASH_FIT` 整筆取消。
  - 1815.TWO `RISK_REENTRY_COOLDOWN`；4904.TW 已超過目標沒有買單。
  - 1802.TW（medium 第 2、已持有）拿到釋出的現金：207,000 → 188,000 股。
  - 1303、1590、2615、3653、5876、8415 被 `CASH_BUFFER` 縮為 0。
- 所以候選 (e)「讓高信心先用賣款」**現在已經如此**，排序不是原因；原因是「可用現金只有賣款」加上「新股殘量 < 50% 整筆取消」。

## 5. 候選方案與 11-05 重放

方法：以 `decision_input.json`、`policy.json`、`intent.json` 重跑 `AllocationOrderEngine`，原版重算的 `content_sha256` 與 `proposal.json` 相同（11-05 07:00 與 11-04 21:56 兩個 run 都相符）；各方案以子類別覆寫 `_fit_cash`／`_drawdown_derisk` 的暫存腳本計算（不在專案內），再跑 `ScenarioEngine` 與 `CompetitionGuardV2`。全部為 `replay_reference_only`。

符號：`B` = 當日姿態緩衝（`cash_buffer_rate`），`S` = 起始已交割現金，`N` = NAV。現行 v1 下限 `F = min(N × B, S)`。

### 5.1 方案定義

- **A：高信心軟下限**。high 的買單可把現金用到 `F_high = min(N × B × r, S)`（`r` < 1，例如 0.75 → 防守 15%）；medium／low 維持 `F`。這是一個**水位**，不是每日額度：現金已低於 `N × B × r` 時不再往下（多日也不會一路下滑）。當日觸發減碼時（`floor_disabled`）不套用，維持減碼收入只補緩衝的原意。
- **A＋帶狀減碼**：A 再把 `drawdown_derisk` 的觸發與目標都改為 `N × B × r`。防守變成帶狀：20% 以上全部自由；15%～20% 只有 high 能動用原有現金；低於 15% 由程式減碼補到 15%。
- **B：全等級軟下限**。所有等級都用 `F_high`。等同只在 `_fit_cash` 把防守緩衝降為 15%（不改份額預算）。
- **(b) 的字面版「買進後不低於 max(現行現金, 緩衝 − δ)」不可行**：`max(S, N(B−δ))` 在 `S` 介於 `N(B−δ)` 與 `N × B` 時等於 `S`（與 v1 相同），其他情況比 v1 更嚴；它永遠不會比 v1 寬鬆，**無法解開 6446**。有意義的形式是上面的 `min(S, N(B−δ))`，即 A／B。
- **C：防守時 high 不受緩衝限制**（用 aggressive 3% 或 0 作下限），防守只靠 `drawdown_derisk` 與擋中低信心。
- **D：調整最低成交比例**。D1：`cash_fit_min_fill_ratio` 全域 0.25；D2：只有 high 不做塵埃取消（任意整張）。
- **E：核實排序**。已於第 4 節確認：高信心本來就先拿賣款，不需改。

### 5.2 11-05 07:00 重放結果

| 方案 | 委託（除 2890 賣 302,000 外） | 6446.TW | 1802.TW | base 現金 | 跌 10% 情境現金 | 換手 | 最低 Active Share | Guard |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 現況 | 1802 買 188,000 | 取消（`RISK_CASH_FIT`） | +188,000 | 18.97% | 20.64% | 2.63% | 0.3704（00984A） | 通過 |
| A，下限 17%（r≈0.85） | 6446 買 24,000（30.0M） | 24,000（63%） | 0 | 17.17% | 18.72% | 4.43% | 0.3493 | 通過 |
| A，下限 15%（r=0.75） | 6446 買 38,000（47.5M） | 全額 | 0 | 15.36% | 16.78% | 6.23% | 0.3583 | 通過 |
| A，下限 10%（neutral） | 同上 | 全額 | 0 | 15.36% | 16.78% | 6.23% | 0.3583 | 通過 |
| C（下限 3%） | 同上 | 全額 | 0 | 15.36% | 16.78% | 6.23% | 0.3583 | 通過 |
| B，全等級 15% | 6446 買 38,000、1802 買 30,000 | 全額 | +30,000 | 15.16% | 16.56% | 6.44% | 0.3583 | 通過 |
| D1（0.25）／D2（high 不取消） | 6446 買 10,000（12.5M） | 10,000（26%） | 0 | 18.97% | 20.65% | 2.63% | 0.3704 | 通過；5876、8415 改記 `RISK_CASH_FIT` |

- A 的 1802 為 0：6446 用完賣款與部分原有現金後，medium 的下限仍是 `S`，沒有可用現金。
- **這筆資料分辨不出 r=0.75、0.5（10%）與 C**：6446 的目標在 15% 下限內就已全額滿足，下限再低也沒有其他 high 要買。
- 競賽現金上限（25%）：所有方案都只會讓現金更低，離上限更遠；`cash_ceiling_fix` 不受影響。
- 主辦方整批作廢：`buy_gap_reserve` 的 3% 預留仍在同一個不等式內（1536 行），A／B 不降低跳空保護。
- Active Share 下降約 0.012，距 0.15 門檻仍遠；`RISK_ACTIVE_SHARE` 未觸發。
- 情境壓力：跌 10% 情境下現金比例上升，Guard 的 `SCENARIOS` 沒有現金下限檢查（`risk.py:470-488`），A／B 不會造成新的拒絕。
- D-Plan 匯出：`market_view.posture.net_exposure_intent` 由委託淨流向推得（`competition/context.py:290-296`），C12 只檢查它與委託淨流向一致（`competition/dplan.py:618-620`）；`stance` 只檢查列舉值（`dplan.py:194-195`）。沒有「defensive 不得 increase」的檢查，A 使淨流向變為買超時會標 `increase`，不會被擋而落入備援 D-Plan。

### 5.2.1 從 neutral 掉進 defensive 的日子（重放）

6446.TW 在 11-02 07:00、11-03 21:26 也被 `CASH_BUFFER` 縮為 0；兩個 run 都是 defensive、現金約 11.5%（已交割現金 99,684,319，NAV 約 9.9 億）。這兩個 run 早於 11-03 的閘門組合（policy 沒有 `cash_buffer_floor`、`cash_fit_order`），重放時以 A 的下限直接套用：

| run | 現況 | A，下限 15% | A，下限 10% |
| --- | --- | --- | --- |
| 11-02 07:00（6446 目標 17,000） | 只賣 3264；6446 為 0 | 相同 | 相同 |
| 11-03 21:26（6446 目標 33,000） | 無委託；6446 為 0 | 相同 | 相同 |

原因（事實，代數）：A 的下限是 `min(N × B × r, S)`，現金已低於 `N × B × r` 時就等於 `S`，與現行 v1 相同。**推薦案只在現金已介於軟下限與緩衝之間（防守時 15%～20%）才有作用；從 neutral（約 10%～11%）剛轉 defensive 的日子，高信心仍只能用賣款。** 這兩天若要買到 6446，下限必須低於約 8%～9%，等於防守對 high 不設實質下限（接近 C）；D2 只能讓它用賣款買到部分（11-02 賣款約 5.06M，約 4 張）。

### 5.3 減碼日與隔日：A 單獨使用會來回交易（核心取捨）

11-04 21:56（當日觸發減碼）重放：

| 方案 | 委託 | base 現金 |
| --- | --- | --- |
| 現況／A（減碼日不套用） | 1802 賣 44,000（減碼）、8046 賣 7,000（減碼出場） | 20.00% |
| A（減碼日也套用） | 同上，再加 6446 買 30,000 | 16.14% |
| A＋帶狀減碼（15%） | 只有 6446 買 29,000，沒有減碼 | 15.19% |

- 「減碼日也套用」等於程式賣 ATR 高的持股去買高信心新股，與 1473 行 docstring「減碼收入不得拿去買替補」相反，**不採用**。
- 「A＋帶狀」在 21:56 也取消了原本的 8046 出場與 1802 減碼：這是防守被稀釋的具體樣子。

隔日推論（**推論**：同價、同快照，交易 Agent 全部 hold，重新雜湊 bundle 與 policy，只示意現行減碼的反應）：

- A 單獨（11-05 買 6446 後現金 15.36%）：若隔日仍 defensive，現行 `drawdown_derisk` 會減碼 8046 7,000、1802 83,000、3653 1,000、2615 14,000、3034 46,000，合計約 45.6M（NAV 4.7%），持股 26 → 22 檔，現金回到 20.04%。6446 的 ATR14 4.46%，在持股中排第 8 高，這次沒被賣，但不保證。估算賣出成本約 0.54%（證交稅 0.3%＋手續費 0.1425%＋滑價 0.1%），約 0.25M。
- A＋帶狀：隔日沒有任何委託，現金維持 15.36%。

| 方案 | 不來回交易 | 保留防守原意（20%） | 6446 買到 |
| --- | --- | --- | --- |
| 現況 | 是 | 是 | 否 |
| A 單獨 | 否（隔日減碼約 45.6M） | 隔日拉回 20% | 是 |
| A＋帶狀減碼 | 是 | 否（防守實質變成 15%～20% 的帶狀） | 是 |
| D | 是 | 是 | 部分（26%） |

每個方案都犧牲一欄，沒有三者兼得的版本。

### 5.4 與姊妹計劃的交互（推論，重放）

把 `conviction_weights` 換成姊妹計劃推薦的 C 公式（只放大 high，2330 為 2.5 倍）後再重放 11-05 07:00：

| 組合 | 委託（除 2890 外） | 6446.TW | base 現金 | 換手 |
| --- | --- | --- | --- | --- |
| 姊妹 C＋現行緩衝 | 2330 買 5,000、1802 買 18,000 | 取消；5876、8415 也取消 | 18.97% | 2.63% |
| 姊妹 C＋A（15%） | 2330 買 21,000（47.5M） | **仍取消**（32,000 → 1,000） | 15.37% | 6.23% |
| 姊妹 C＋A（10%）或 C（3%） | 2330 買 22,000、6446 買 32,000 | 32,000 | 11.01% | 10.58% |

- 兩個計劃同時啟用、下限 15% 時，high 第 1 名的 2330 會先用掉高信心可動用的額度，6446 仍被取消。**這是硬相依**：要兩檔都買到，下限需降到約 10%，防守的現金會接近 neutral。
- 需要人決定「高信心額度」在 high 內部要不要讓未持有新股優先，或接受依名次（第 10 節）。

## 6. 推薦：A＋帶狀減碼（只限 high，r = 0.75，減碼日不套用）

由一個版本閘門啟用：

```json
"high_conviction_cash_floor": {"version": "v1", "floor_ratio": "0.75"}
```

v1 語意（全部由程式計算，Agent 不輸出數字）：

1. `_fit_cash`：tier 為 high 的買單，下限改為 `min(N × B × floor_ratio, S)`；medium／low 不變；當日有 `RISK_DRAWDOWN`（`floor_disabled`）時全部維持 `N × B`。
2. `_drawdown_derisk`：觸發與目標都改為 `N × B × floor_ratio`。
3. `_forced_swap_victims` 的現金可行性改用同一個共用函式（被換入的新股門檻是 high，`forced_swap.min_new_conviction`），避免換股判斷與實際配置不一致。
4. `_conviction_targets` 的預算（989 行）**不變**：份額仍以完整緩衝計，軟下限只決定目標能不能被現金支應，不放大目標。
5. high 買單實際動用到 `S` 以下的現金時，記資訊旗標 `RISK_CASH_SOFT_FLOOR`（例：「高信心買單動用姿態緩衝內原有現金 X 元，下限 15%；交易 Agent 的意圖未被改寫」），供報告與稽核。

6. 帶狀時 `RISK_DRAWDOWN` 的說明文字（`allocation.py:1419-1421` 目前格式化 `cash_buffer_rate`，會寫「低於 20% 的姿態緩衝」）必須改帶軟下限，否則旗標與實際行為不符。

理由：

- 回應期望（有前提）：現金已在 15%～20% 時，11-05 的 6446 全額買到（38,000 股），排名較後的 1802 不再先拿走現金。**現金低於 15%（例如從 neutral 約 11% 剛轉防守，第 5.2.1 節）時沒有作用**，高信心仍只能用賣款；這部分只能靠 D2（部分成交）或接受。
- 沒有隔日來回交易（第 5.3 節）：防守的兩個機制（擋買與主動減碼）用同一條下限，不互相抵銷。
- `floor_ratio` 是相對緩衝的比例，所以策略反思調 `cash_buffer_by_stance`、或修正循環的 `increase_cash_buffer`（`risk.py:721-760`）調高緩衝時，高信心下限會同向移動（調高仍是降風險）。各姿態的下限：defensive 15%、neutral 7.5%、aggressive 2.25%。
- 只限 high：medium／low 仍只能用賣款，防守對大部分買單的效果不變。

明確的代價（推論）：

- **防守被稀釋**：防守的有效下限從 20% 變成 15%；減碼日要補的量也變少。以 11-04 03:34 粗估：起始 10.72%，扣交易 Agent 自己的賣單後，減碼補到 20% 約需 NAV 6～7%，補到 15% 約需 2～3%。
- 與姊妹計劃同時啟用、r = 0.75 時，6446 在 11-05 仍會被取消（第 5.4 節）。
- 現金下降使跌 10% 情境的 NAV 損失增加（11-05 約多 3.6% NAV 的股票曝險），風險 Agent 的「防守」與實際曝險的距離變大，報告必須寫清楚。

不推薦但保留的選項：

- **D**（只改最低成交比例）：改動最小、不稀釋防守，修掉了名次倒置，但 6446 只買到 26%，5876、8415 仍被取消。若人尚不接受稀釋防守，可單獨採用 D2 作過渡。
- **B**：中信心也能動用，稀釋更多，11-05 只多買 1802 30,000 股。
- **C**：與 A 在 11-05 無差異，但沒有任何下限，與姊妹計劃同時啟用時現金會到 11%。

## 7. 契約變更

### 7.1 新欄位與驗證（`sizing.py`）

- 常數 `HIGH_CONVICTION_CASH_FLOOR = "v1"`、`HIGH_CONVICTION_CASH_FLOOR_FIELDS = {"version", "floor_ratio"}`。
- `validate_position_sizing` 的白名單（355-368 行）加入 `high_conviction_cash_floor`。
- `_validate_execution_gates`（176-250 行）新增檢查（fail-closed）：
  - 必須是物件；`reject_unknown_fields`；`version` 只允許 `"v1"`。
  - `floor_ratio` 可解析為 Decimal，`0 < floor_ratio < 1`（等於 1 沒有意義，拒絕）。
  - 前提：`cash_buffer_floor: "v1"` 與 `cash_fit_order: "v1"` 必須同時存在（語意建立在兩者之上：high 要先拿現金）；有 `drawdown_derisk` 時版本必須是 v3。
- 新函式 `high_conviction_floor_ratio(sizing) -> Optional[Decimal]`，沒有閘門回 None。

### 7.2 Policy 綁定、雜湊與 effective

- 樣板 `config/decision_policy.json` → `build_decision_policy` → `policy.json`；`decision_policy_sha256`（`allocation.py:53`）涵蓋整個 `position_sizing`，新欄位自動進入 `content_sha256` 與 proposal 的 `policy_hash`。
- `_effective_policy`（892-900 行）：與 `cash_buffer_floor` 相同，**只在 Policy 有此欄位時**加入 `high_conviction_cash_floor: "v1"` 與 `high_conviction_floor_ratio`，寫入 proposal 的 `effective_policy`；舊 run 的 effective 鍵逐一不變。
- 不加入修正循環可覆寫的鍵（`_effective_policy` 的 `keys`、`ProposalValidator` 1613-1620 行的 `override_keys`）：風險 Agent 不能調這個比例。

### 7.3 重建驗證

- `ProposalValidator`（1597 行起）以同一 policy 重跑 `AllocationOrderEngine` 並整份比對；Finalizer（`finalization.py:240`）、`DecisionResultValidator`、`evaluation/replay.py` 都走同一引擎，不需新增比對邏輯。
- 舊 Policy 沒有此欄位 → `high_conviction_floor_ratio` 為 None → `_fit_cash`、`_drawdown_derisk`、`_forced_swap_victims` 走原式，**逐位元不變**。實作時先抽出共用的下限函式，並以既有 fixture 的 proposal `content_sha256` 鎖定不變。

### 7.4 Agent 契約（與姊妹計劃不同，這裡有變）

- 風險 Agent 的 Skill（`tradeagent/risk/portfolio-risk-review/SKILL.md:19`）目前寫「選該姿態會使買單被緩衝擋掉（出場賣股收入買的替補除外）」；三份 `cash_stance.json` 理由都照此描述代價。閘門啟用後需補一句：高信心買單可動用到緩衝 × `floor_ratio`，程式減碼也只補到這條線。
- 簡報（`automation/risk_stages.py:102-104` 的 `cash_buffer_by_stance`、`cash_ratio_now`）在 Policy 有閘門時另附 `high_conviction_cash_floor_by_stance`（程式算好的各姿態下限），沒有閘門時不加欄位。實作時須確認簡報內容是否進任何重建雜湊（例如 `agent_calls` 或 team_inputs）；若有，新欄位只在閘門存在時出現即可維持舊 run 不變。
- 交易 Agent 不需知道這個閘門：它不影響信心等級與名次，只影響程式能不能支應。
- `team_inputs` 不升版：Agent 輸入結構不變。

### 7.5 策略反思白名單

- `strategy_reflection/contracts.py` 的 `FORBIDDEN_PREFIXES`（136-152 行）加入 `"high_conviction_cash_floor"`（含 `floor_ratio`），與 `cash_buffer_floor`、`cash_fit_order` 同列；不加入 `KNOBS`。
- `cash_buffer_by_stance.*` 仍是正式旋鈕（`contracts.py:94-95`），方向判斷不變（調高＝降風險），因為下限是緩衝的固定比例。

## 8. 測試計劃（unittest.TestCase 風格）

`tests/decision/test_cash_fit_and_limits.py` 新增 `HighConvictionCashFloorTests`（`setUp` 建 bundle 與 policy）：

- `test_without_gate_proposal_is_bitwise_unchanged`：同一輸入、無閘門，proposal `content_sha256` 與改動前固定值相同；`effective_policy` 鍵與 `LEGACY_EFFECTIVE_KEYS` 相同。
- `test_high_buy_may_use_cash_down_to_soft_floor`：起始現金 18.9%、防守 20%、`floor_ratio` 0.75，high 新股買到下限 15%（含跳空預留）。
- `test_medium_and_low_still_limited_to_starting_cash`：同條件下 medium／low 只能用賣款。
- `test_soft_floor_is_a_level_not_a_daily_allowance`：起始現金已低於 15% 時 high 也不能再往下（不會逐日下滑）。
- `test_soft_floor_never_above_full_buffer`：起始現金高於 20% 時，high 下限為 15%、其他為 20%。
- `test_high_buys_take_cash_before_medium`：`cash_fit_order` 下 high 先用到軟下限，medium 只拿剩餘賣款（11-05 情境縮小版：6446 全額、1802 為 0）。
- `test_soft_floor_not_applied_on_derisk_day`：當日有 `RISK_DRAWDOWN` 時 high 也以完整緩衝計（不套用軟下限）。注意：這不禁止買單；減碼整張進位後現金若超過完整緩衝，超出部分仍可買進（Codex 審查指出、人已決定不擋）。
- `test_derisk_targets_soft_floor`：防守、現金 16% → 不減碼；現金 12% → 減碼補到 15%（不是 20%），且 `RISK_DRAWDOWN` 說明文字寫 15%。
- `test_forced_swap_feasibility_uses_same_floor`：強制換股的現金檢查與 `_fit_cash` 的下限一致（換入的新股買得到就換，買不到就不換）。
- `test_budget_unchanged_by_gate`：`target_weights` 與無閘門相同。
- `test_soft_floor_flag_recorded`：動用緩衝時有 `RISK_CASH_SOFT_FLOOR`，未動用時沒有。
- `test_increase_cash_buffer_override_moves_soft_floor`：修正循環把緩衝調高到 0.22 時，下限變成 0.165。
- `test_proposal_validator_rejects_tampered_orders`：竄改 6446 股數被 `ProposalValidator` 拒絕。
- fail-closed：`test_policy_rejects_unknown_version`（`"v2"`、`true`、`1`）、`test_policy_rejects_floor_ratio_out_of_range`（0、1、1.2、-0.1、`"abc"`）、`test_policy_rejects_unknown_subfield`、`test_policy_rejects_gate_without_cash_buffer_floor`、`test_policy_rejects_gate_without_cash_fit_order`、`test_policy_rejects_gate_with_derisk_v1_or_v2`。
- 端到端：比照 `AllGatesEndToEndTests`（419 行起）新增有閘門版本，確認 Scenario、Guard、Finalizer 重建通過。

其他檔案：

- `ProductionConfigTests` 的 `ALL_GATES`（48 行）與 `test_gates_are_forbidden_for_strategy_reflection`（120 行）加入新欄位（只在人決定啟用後才改 production config 的期望值）。
- `tests/decision/test_risk_limit_gates.py` 的 `DrawdownDeriskTests`（754 行起）加帶狀目標的案例。
- `tests/strategy_reflection/test_design_guards.py:34-35` 的閘門迴圈加入 `"high_conviction_cash_floor"`。
- 風險 Agent 簡報：有閘門才出現新欄位、沒有閘門時簡報不變的測試（目前 `tests/` 內沒有直接測 `risk_stages` 簡報的測試，`grep -rln "risk_stages\|cash_ratio_now" tests` 無結果，需新增）。
- Skill 有改時執行 `quick_validate.py tradeagent/risk/portfolio-risk-review`。

## 9. 需同步更新的文件（已 grep 確認段落）

- `docs/architecture.md`：85 行（防守姿態減碼）、87 行（現金緩衝說明）、附錄閘門表 259-260 行附近加一列。
- `docs/decision-details.md`：70 行（防守減碼）補帶狀目標；附錄名稱對照 127-129 行附近加一列。
- `src/etf_agent/decision/README.md`：53 行 `sizing.py` 列、98 行（`drawdown_derisk`）、100 行（執行面閘門）。
- `AGENTS.md`：「現金緩衝（`position_sizing.cash_buffer_floor: "v1"`…）」那條與 2026-11-03 閘門清單各補一句（Agent 邊界與契約變更，規則要求同步）。
- `tradeagent/risk/portfolio-risk-review/SKILL.md:19`（第 7.4 節）。
- `README.md`：目前沒有現金緩衝細節段落（grep `緩衝`、`cash_buffer` 無結果），不需改。
- `docs/README.md` 索引加本檔（後續事項，本次不改）。

## 10. 實作步驟、驗證、回滾

步驟：

1. `sizing.py`：常數、驗證、`high_conviction_floor_ratio`。
2. `allocation.py`：抽出共用下限函式（先只重構，確認既有測試與 fixture 雜湊不變），再接 `_fit_cash`、`_drawdown_derisk`（含 1419-1421 行的旗標文字）、`_forced_swap_victims`，加 `RISK_CASH_SOFT_FLOOR` 與 effective 鍵。旗標代碼目前只在 `allocation.py` 與 `decision/README.md` 列舉（`grep -rn "RISK_CASH_FIT" src` 結果），新旗標不需改 `trader_report.py` 或 D-Plan 匯出，只補文件。
3. `strategy_reflection/contracts.py`：`FORBIDDEN_PREFIXES`。
4. 風險 Agent 簡報與 Skill 文字（第 7.4 節）。
5. 測試（第 8 節），先寫舊 policy 逐位元不變的測試。
6. 用 11-04 21:56、11-05 07:00 封存輸入重放（`replay_reference_only`），確認舊 policy 雜湊不變、新閘門結果與第 5 節一致。
7. 文件（第 9 節）。
8. 最後才由人在 `config/decision_policy.json` 加入閘門（第 11 節）。

驗證：

```bash
PYTHONPYCACHEPREFIX=/tmp/etf-agent-pycache \
  .venv/bin/python -m pytest

PYTHONPYCACHEPREFIX=/tmp/etf-agent-pycache \
  .venv/bin/python -m compileall -q src tradeagent tests

git diff --check
```

回滾：從 `config/decision_policy.json` 移除 `high_conviction_cash_floor` 即回到原行為。已封存的新 run 因 policy 內含欄位，仍以新式重建通過，不需改封存；程式路徑不可移除（移除會讓已封存的新 run 無法重建）。回滾當天若現金在 15%～20%，現行減碼會立即補回 20%（可能是一次較大的程式減碼），回滾前要先知道這一點。

## 11. 需要人決定的事項

1. **是否接受稀釋防守**：推薦案讓防守的有效下限從 20% 變成 15%（減碼也只補到 15%）。不接受時改採 D2（只讓 high 新股不做塵埃取消，不動用原有現金）。
2. **下限的形式與數值**：比例 `floor_ratio`（推薦 0.75 → 防守 15%／中性 7.5%／積極 2.25%）、固定差距 δ，或「低一階姿態的緩衝」（防守 → 10%）。11-05 分辨不出 0.75 與 0.5；11-02、11-03 晚上（現金約 11.5%）兩者都無效（第 5.2.1 節）。要在「剛轉防守」的日子也買到，下限需低於約 8%～9%，防守對 high 幾乎沒有下限，需人明確接受。
3. **適用等級**：只限 high（推薦），或 high＋medium（B 類）。
4. **減碼的配套**：帶狀（觸發與目標都用軟下限，推薦）、維持現行（接受隔日來回交易，11-05 推論約 45.6M）、或只改觸發點、目標仍補到 20%（下穿 15% 時會一次大額減碼）。
5. **減碼日是否套用**：推薦不套用軟下限（買單以完整緩衝計）。**不禁止買單**：減碼整張進位後現金超過完整緩衝的部分仍可買進，同日可能出現程式減碼賣 A、買單買 B；人已決定不擋（Codex 審查指出，結論為降風險目的已達成、擋買單反而會讓好標的買不到）。
6. **與姊妹計劃的先後**：兩者都啟用且 r = 0.75 時，2330 先用完高信心額度，6446 仍被取消。要嘛接受依名次，要嘛在 high 內讓未持有新股優先，要嘛把下限降到約 10%。
7. **啟用方式**：比賽進行中改防守語意，是否先只做影子重放觀察幾個交易日（只算 `replay_reference_only`，不改正式 Policy），再決定啟用日。
8. **D2 是否一併採用**：與推薦案不衝突；推薦案在現金足夠時用不到 D2，現金不足時 D2 讓 high 新股至少買到部分。缺點是可能留下小部位（例如姊妹計劃組合下 6446 只剩 1,000 股）。

## 附錄：名稱對照

| 白話說明 | 程式名稱 |
| --- | --- |
| 高信心可動用部分緩衝、減碼補到同一條線（本計劃，已實作、未開啟） | `position_sizing.high_conviction_cash_floor`、旗標 `RISK_CASH_SOFT_FLOOR` |
| 姿態現金緩衝 | `cash_buffer_by_stance`、`cash_buffer_rate` |
| 現金緩衝只放行賣款 | `cash_buffer_floor` |
| 現金不足依信心分錢、零頭取消 | `cash_fit_order`、`cash_fit_min_fill_ratio`、旗標 `RISK_CASH_FIT` |
| 買單跳空預留 | `buy_gap_reserve`、`buy_gap_reserve_rate` |
| 現金逼近上限留住出場 | `cash_ceiling_fix`、旗標 `RISK_CASH_CEILING` |
| 防守姿態主動減碼 | `drawdown_derisk`、旗標 `RISK_DRAWDOWN` |
| 換手上限縮減 | `turnover_fit`、`max_turnover_rate`、旗標 `TURNOVER_LIMIT` |
| 虛擬帳本現金不足整批作廢 | `insufficient_funds_policy: "void_batch"` |

## 實作紀錄（2026-11-05）

- 照第 6 節實作：`{"version": "v1", "floor_ratio": "0.75"}`，`floor_ratio` 必填且 0 < r < 1。前提是 `cash_buffer_floor`、`cash_fit_order`，有 `drawdown_derisk` 時必須是 v3。下限函式是 `AllocationOrderEngine._cash_floor`，由 `_fit_cash` 與 `_forced_swap_victims` 共用；強制換股的現金檢查只在閘門開啟時才看減碼日，舊路徑不變。
- `RISK_CASH_SOFT_FLOOR` 的動用量以一般下限 `min(N × B, S)` 為準，不是 `S`：現金高於完整緩衝時，只計動用到 20% 以下的部分。
- 第 5.4 節的相依由人決定維持「依名次」：名次第一的 high 先用完高信心可動用的現金。以 `test_rank_first_high_add_takes_the_soft_floor_before_new_high` 鎖定。
- 用封存輸入重放，結果為 `replay_reference_only`：
  - 未加閘門時，11-05 07:00 與 11-04 21:56 的 proposal `content_sha256` 與封存檔相同。
  - 只開本閘門時，11-05 07:00 的 6446 買 38,000 股、base 現金 15.36%；11-04 21:56 只有 6446 買 29,000 股、現金 15.19%，沒有減碼。兩者與第 5.2、5.3 節一致。
