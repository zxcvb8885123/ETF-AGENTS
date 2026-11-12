# 誰在做決定：Agent 與程式的分工盤點

撰寫時間：2026-11-10 22:40（Asia/Taipei），分支 `147-`（`b320dee`）。唯讀盤點：沒有改程式、設定、Skill 或封存；只新增本檔並在 `docs/README.md` 加一行索引。

要回答的問題（使用者原話）：「我現在好像都是程式在決定？但我是要讓 Agent 自己去判斷。」

**結論（先講）**

- 以 2026-11-10 20:10 之後的正式設定來看，**「買誰、賣誰、誰換誰、先賣誰、誰先拿錢、現金要保守還是積極」已經是 Agent 在決定**。「都是程式在決定」這個印象來自舊設定下的 11 份封存：256 筆買進意圖只有 40 筆有買單。
- **還不是 Agent 決定的是「多少」**：每檔買多少、防守時賣多少、現金留幾趴、持有幾檔、`trim` 賣多少，全部是 Agent 選一個列舉、人設的數字把它換成幅度。另外有 5 處程式仍會否決或替 Agent 選。
- **這套新設定還沒有任何一份真實 run。** 2026-11-10 一天內切換了 6 個會影響帳本的閘門版本（另開 1 個影子），封存裡最新一份仍是舊設定。現在的分工是「設定上如此」，不是「已經觀察到如此」。

證據等級：本檔所有統計數字都是 `evidence_grade: replay_reference_only`（封存決策的事後統計，不是績效，也不是對 LLM 判斷的前瞻驗證）。**本檔沒有任何數字來自現行設定下的即時紀錄。**閘門狀態的唯一出處仍是 `config/decision_policy.json` 與 [status.md](status.md#版本閘門啟用狀態)；本檔與它們不一致時以它們為準。

## 0. 依據與方法

### 0.1 讀了什麼（事實）

- 設定：`config/decision_policy.json`（`policy_id: strategy-2026-11-10-agent-swap-v2-1`，`available_at: 2026-11-10T20:10:00+08:00`）、`config/decision_rules.json`（`decision-rules-2026-11-03b`）。
- 程式：`src/etf_agent/decision/` 的 `allocation.py`、`sizing.py`、`swap_nomination.py`、`release_order.py`、`rebound.py`、`exposure.py`、`drawdown.py`、`cash_ratio.py`、`reentry_cooldown.py`、`risk.py`。下文行號是這個版本的 `allocation.py`。
- 文件：`AGENTS.md`、[status.md](status.md)、[agent_autonomy_plan.md](agent_autonomy_plan.md) 第 1～3 節、[agent_led_exposure_plan.md](agent_led_exposure_plan.md) 第 8 節、[follow_up_plan.md](follow_up_plan.md)。
- 封存：`artifacts/decisions/` 共 11 份 run（目標交易日 2026-10-27～2026-11-10），以 `.venv/bin/python cli/calibration.py intent-landing` 唯讀統計（22:3x 執行，結束碼 0）。截至撰寫時沒有目標交易日 2026-11-11 的封存。

### 0.2 分類方法

計數單位是「決定點」：從資料到訂單的路上，一個會改變結果的閘門或規則算一列；同一個閘門裡有兩種性質（例如「誰換誰」與「一次最多幾對」）就拆成兩列。每一列只問兩個問題：

1. **這一步有沒有在選對象、選方向或選幅度？** 沒有（只是計算、核對、守比賽規則）就是界線。
2. **有的話，是誰選的？**

據此分成七類：

| 分類 | 定義 | 判讀 |
| --- | --- | --- |
| 硬界線 | 比賽規則、結算口徑、資料時間、證據、重算驗證、避免整批作廢 | 應留程式 |
| 自設上限 | 不是比賽規則，是人設的數字；只限制「最多多少、最快多快」，不選對象 | 留在程式，數字由人決定 |
| Agent | Agent 選對象、方向或先後；程式只照做或檢查可不可行 | 已交給 Agent |
| 對映 | Agent 選一個列舉，人設的數字把它換成幅度（方向是 Agent 的，幅度是人設的） | 判斷的一半仍在數字裡 |
| 公式 | 程式用門檻或排序否決 Agent 的意思，或替 Agent 選對象 | 判斷被寫成公式 |
| 紀錄 | Agent 有填、程式有算，但配置不讀 | 不影響帳本 |
| 失效 | 設定還在，但目前不產生作用 | 不影響帳本 |

「對映」與「公式」的分界沿用 [agent_led_exposure_plan.md](agent_led_exposure_plan.md) 第 8 節的檢查法：出現「程式依 X 排序挑出一檔」或「列舉對映成一個目標值讓程式去追」，那一步就是把判斷寫進了程式。

這個分類是本檔的判讀（推論），不是可重算的量。[agent_autonomy_plan.md](agent_autonomy_plan.md) 第 1.2 節已指出「Agent 四成、程式六成」這類比例沒有可重算的定義；本檔的比例同樣只是「依上表定義數出來的列數」，換一種切法數字會不同。

## 1. 盤點表：從資料到訂單

### 1.1 逐點盤點

| # | 決定點 | 現在誰決定 | 位置（檔案／閘門） | 分類 |
| --- | --- | --- | --- | --- |
| 1 | 哪些股票可以交易（150 檔交易池、可交易狀態） | 程式（主辦方名單） | `config/competition_profile.json`、`contracts.untradable_symbols`、旗標 `RISK_NOT_TRADABLE` | 硬界線 |
| 2 | 哪些資料當時看得到、來源是否核准 | 程式 | `decision_cutoff`／`available_at`、`license_status` | 硬界線 |
| 3 | 技術指標、財務比率、beta、ATR 的數值 | 程式公式 | `decision/momentum.py`、`exposure.BetaModel`、`fundamental_metrics` | 硬界線 |
| 4 | 全市場情緒用新聞還是恐懼貪婪指標、何時算不可用 | 程式規則 | `perception/market_fear_greed`、`market_news_input` | 硬界線 |
| 5 | 論點引用的證據存不存在、種類對不對 | 程式 | bundle 的 `typed_evidence_v1` | 硬界線 |
| 6 | 每檔的技術、基本面、事件看法 | Agent（三位分析師） | `decision/analysts.py` | Agent |
| 7 | 每檔的多頭與空頭論點強度 | Agent（多空研究員） | `decision/stance.py` | Agent |
| 8 | 每檔要買、加碼、續抱、減碼還是出場 | Agent（交易） | `decision/trader.py` | Agent |
| 9 | 買進與續抱的信心（高／中／低） | Agent（交易） | `trader.py`；`exposure_rotation.hold_conviction` | Agent |
| 10 | 虧損達 15% 的持股要不要處理 | 人設門檻，Agent 決定怎麼處理（必須回應、可以續抱） | `position_review.loss_alert`、`decision/position_review.py` | Agent |
| 11 | 同信心等級內誰排前面 | Agent（交易第二輪） | `selection_ranking`、`decision/selection_ranking.py` | Agent |
| 12 | 需要賣股補現金時先放掉誰 | Agent（交易第二輪） | `drawdown_derisk` v5、`decision/release_order.py` | Agent |
| 13 | 滿檔時用哪檔新股換掉哪檔持股 | Agent（交易第二輪提名） | `agent_swap` v2、`swap_nomination.py`、`allocation._agent_swap`（1002 行起） | Agent |
| 14 | 一次最多換 2 對、持有滿 5 天才能被換出 | 人設數字 | `agent_swap.max_swaps_per_run`／`min_holding_days` | 自設上限 |
| 15 | 能不能因為「整個組合太防守」而換掉某檔 | **不能成交**：換出者要有這檔自己的空頭依據；以曝險為由的配對只封存 | `swap_nomination.VICTIM_REASONS` 的 `no_bear_basis`、`agent_swap.exposure` v1（`_agent_swap` 1070 行略過）、交易 Skill 的「市場參與度低本身不是賣出理由」 | 公式 |
| 16 | 低信心的新股能不能被提名、能不能用出場釋出的名額 | **不能**（門檻為中信心） | `rotation.min_new_conviction`；`swap_nomination` 的 `conviction_below_threshold`、旗標 `ROTATION_CONVICTION` | 公式 |
| 17 | `trim` 要賣多少 | 人設數字：一律一半 | `trim_fraction 0.5`、`allocation._run`（584 行） | 對映 |
| 18 | 現金姿態（進攻／中性／防守）與子等級 | Agent（風險） | `cash_stance`、`cash_grade` | Agent |
| 19 | 姿態與子等級等於幾趴現金 | 人設數字：9 個格點，3%～21% | `cash_ratio_range.grid` | 對映 |
| 20 | 持股要集中、平衡還是分散 | Agent（風險） | `position_breadth` | Agent |
| 21 | 集中／平衡／分散等於幾檔 | 人設數字：22／26／30 | `positions_by_breadth` | 對映 |
| 22 | 回撤 5% 起不得進攻、10% 起必須防守 | 人設數字，程式強制 | `drawdown_control`、`decision/drawdown.py` | 自設上限 |
| 23 | 現金一天最多拉高 3 個百分點 | 人設數字 | `cash_ratio_range.max_daily_raise` | 自設上限 |
| 24 | 全市場情緒為負面時不得選進攻 | 程式規則 | `sizing.validate_cash_stance`（486 行說明） | 自設上限 |
| 25 | 風險審查後要不要再加現金、降單股上限或降換手 | Agent（風險），只能往降風險方向 | `risk.RiskReviewValidator` | Agent |
| 26 | 市場轉向時放寬曝險換股 | 設定仍在，目前不作用（見 1.3） | `rebound_participation` v2、`exposure_rotation` v3 的換股步驟 | 失效 |
| 27 | 要不要開始回補、是不是被大盤拖累等列舉 | Agent 有填，配置不讀 | 8 個紀錄閘門（見 1.3） | 紀錄 |
| 28 | 候選誰先拿名額、誰先拿現金 | Agent 的信心與名次（同名次才依代號） | `_ordered_conviction_candidates`（1414 行）、`cash_fit_order` v2、`high_conviction_cash_priority` v2 | Agent |
| 29 | 沒有持有的新股只能用空名額或出場釋出的名額（一次最多 5 檔） | 程式規則＋人設數字 | `held_add_priority`、`rotation.max_swaps_per_run`、旗標 `MAX_POSITIONS` | 自設上限 |
| 30 | 每檔買多少 | 人設數字：高 1.5／中 0.6／低 0.3 的相對份額 | `conviction_volatility_v2` 的 `conviction_multipliers` | 對映 |
| 31 | 高信心至少買到單股上限的六成（一般股 6%、2330 為 15%） | 人設數字 | `limit_scaled_conviction.alpha` | 對映 |
| 32 | 標了 `add` 但現有權重已高於份額時不加碼 | 程式公式 | `held_weight_budget` v2、旗標 `RISK_HELD_WEIGHT` | 公式 |
| 33 | 防守姿態下現金不足時要賣多少股票 | 程式公式：補到第 19 列的格點 | `drawdown_derisk`、`allocation._drawdown_derisk`（2008 行） | 對映 |
| 34 | 一次減碼最多賣 NAV 的 25%，且不超過換手餘額 | 人設數字 | `drawdown_derisk.max_derisk_fraction` | 自設上限 |
| 35 | 釋出順序驗證失敗時改依 ATR 高到低賣 | 程式公式（降級路徑） | `_drawdown_derisk`、旗標 `DERISK_ORDER_SKIPPED:order_*` | 公式 |
| 36 | Agent 自己出場的股票 3 天內不得買回，也不能被提名換入 | 人設數字，程式擋單 | `reentry_cooldown` v2、旗標 `RISK_REENTRY_COOLDOWN` | 公式 |
| 37 | 現金已低於緩衝時，買單不得動用原有現金（賣款可買替補） | 程式照風險 Agent 選的現金比例執行 | `cash_buffer_floor`、`allocation._cash_floor`（2171 行） | Agent |
| 38 | 跳空預留 3%、買進時單股上限留 3% 空間、資金不足整批作廢的口徑 | 程式 | `buy_gap_reserve`、`stock_limit_headroom`、`insufficient_funds_policy` | 硬界線 |
| 39 | 單股不超過 10%（2330 為 25%） | 比賽規則 | `decision_rules.max_stock_weight`、`_fit_stock_weights` | 硬界線 |
| 40 | 單一產業不超過 60%，超過時先減哪張買單 | 人設數字；程式先減信心低的買單，不賣既有持股 | `sector_fit`、`max_sector_weight`、`_fit_sector_weights` | 自設上限 |
| 41 | 總換手不超過 30%，超過時先減哪張買單 | 人設數字；程式先減信心低、ATR 高的買單 | `turnover_fit`、`max_turnover_rate`、`_fit_turnover` | 自設上限 |
| 42 | 持股 20～30 檔、現金低於 25%；不足時留住哪檔出場 | 比賽規則；留誰由程式依 ATR 低到高 | `_run_min_positions`（370 行）、`cash_ceiling_fix` | 硬界線 |
| 43 | Active Share 不低於 15%；不足時取消哪檔買進 | 比賽規則；取消誰由程式挑重疊最多的 | `_active_share_drop`（328 行） | 硬界線 |
| 44 | 整張、手續費、證交稅、滑價、賣款同日可買 | 程式 | `decision_rules.json`、`allocation._order` | 硬界線 |
| 45 | 壓力情境、最終檢查、整份重算、不可變封存 | 程式 | `risk.ScenarioEngine`、`CompetitionGuardV2`、`finalization.py` | 硬界線 |
| 46 | 產生上傳檔、備援檔、實際上傳 | 程式產生，人上傳 | `competition/`、`competition/fallback.py` | 硬界線 |

### 1.2 整體比例

依上表數出來（推論，定義見 0.2）：

| 分類 | 列數 | 占 46 列 | 列號 |
| --- | --- | --- | --- |
| 硬界線 | 12 | 26% | 1–5、38、39、42–46 |
| 自設上限 | 8 | 17% | 14、22、23、24、29、34、40、41 |
| Agent | 13 | 28% | 6–13、18、20、25、28、37 |
| 對映 | 6 | 13% | 17、19、21、30、31、33 |
| 公式 | 5 | 11% | 15、16、32、35、36 |
| 紀錄 | 1 | 2% | 27 |
| 失效 | 1 | 2% | 26 |

只看會影響訂單的 44 列：

- **界線 20 列（45%）**：硬界線 12、自設上限 8。
- **已交給 Agent 13 列（30%）**。
- **判斷還在程式或數字裡 11 列（25%）**：其中「對映」6 列（方向是 Agent 的、幅度是人設的），「公式」5 列（程式否決或代選）。

怎麼讀這個比例：

- 13 列 Agent 涵蓋了所有「選對象」的決定：買賣哪一檔、信心、排序、換誰、先賣誰、現金方向、廣度。
- 6 列對映涵蓋了所有「選幅度」的決定：買多少、賣多少、現金幾趴、幾檔。`AGENTS.md` 的總則規定 Agent 不輸出數字，所以幅度只能走「列舉換成人設數字」這條路；這是原則的直接結果，不是近期閘門造成的。
- 列數不等於影響大小。對真實帳本的影響排序見第 2 節。

### 1.3 兩個容易誤讀的地方（事實）

**失效的閘門。** 以下設定還在 `config/decision_policy.json`，但目前不改變訂單：

- `exposure_rotation` v3 的換股步驟：`agent_swap` v2 開啟後，`allocation._exposure_rotation` 在 1292–1295 行直接留下 `EXPOSURE_ROTATION_SKIPPED:agent_swap` 後返回。它還在提供續抱信心、持有天數與 beta 事實。
- `rebound_participation` v2：它只放寬上一項的換股參數（`rebound.apply_rebound` 在那個返回點之後才被呼叫），而且沒有填選填的現金與換手放寬欄位。風險 Agent 仍要填 `turn_assessment`，但這個列舉現在不改變任何訂單。
- `beta_target_by_stance`（進攻 0.90、中性 0.55）與 `by_state` 的 0.30／0.55：因上兩項而不作用。
- 頂層 `cash_buffer_rate 0.05`、`cash_buffer_by_stance`：被 `cash_ratio_range` 的格點取代。`default_target_weight 0.04`：有 `position_sizing` 時不使用。`volatility_exponent 0`：權重不看波動度。

**紀錄閘門（8 個，合併為第 27 列）。** `cash_stance_recency`、`cash_stance_turn_facts`、`market_index_facts`、`exposure_view`、`quality_dip_facts`、`quality_dip_assessment`、`event_direction`、`agent_swap.exposure` v1。Agent 在這些地方做了判斷，但配置不讀。其中 `exposure_view`（要不要開始回補）與 `purpose: exposure` 的提名，正是使用者想要 Agent 做的判斷，目前只留紀錄。

### 1.4 為什麼會覺得「都是程式在決定」（事實與推論）

事實（11 份封存，`replay_reference_only`）：

| 項目 | 數字 |
| --- | --- |
| 買進／加碼意圖 | 256 筆 |
| 有買單 | 40 筆（15.6%）；扣掉開帳日是 204 筆中 10 筆 |
| 依信心：高 | 30 筆中 12 筆有買單 |
| 依信心：中 | 151 筆中 28 筆 |
| 依信心：低 | 75 筆中 0 筆 |
| 沒買到的原因 | `MAX_POSITIONS` 163、沒有旗標的 `add` 25、`CASH_BUFFER` 14（另 1 筆同時 `RISK_DRAWDOWN`）、`RISK_HELD_WEIGHT` 4、`RISK_REENTRY_COOLDOWN` 3、`LOT_SIZE` 3、`RISK_CASH_FIT` 2、`RISK_DRAWDOWN` 1 |
| Agent 標續抱或加碼卻被程式賣出 | 只有 11-04 一天：48,450,690 元，占全部 11 份賣單總額 113,166,530 元的 43%（占當日賣單 76.85%） |
| 現行設定下的封存 run | 0 份 |

最後一份封存（目標交易日 2026-11-10，舊設定）的狀態：持股 30 檔（比賽上限）、現金 15.1%、姿態防守／子等級 lower、回撤 5.07%（回撤下限為中性）；Agent 有 10 筆買進意圖，3 筆有買單，7 筆是 `MAX_POSITIONS`。

推論：

- 沒買到的主因是**名額**。持股已在比賽上限 30 檔，新股只能靠出場或換股進來。這條上限是比賽規則，不是程式替 Agent 判斷。
- 在舊設定下，滿檔時「換掉誰」沒有人問過 Agent，所以意圖幾乎都落空。這一點在 2026-11-10 20:10 已改成 Agent 提名（第 13 列），但一次最多 2 對（第 14 列）。
- 真正違反 Agent 明確意思的事件只有一次（11-04 的防守減碼）。「先賣誰」已改由 Agent 給順序（第 12 列）；「賣多少」仍是公式（第 33 列）。

## 2. 仍由程式替 Agent 判斷的清單

範圍：現在已開啟的閘門（不含已實作未開啟者）。依「對真實帳本的影響」由大到小排序；排序依據是封存紀錄中動用的金額與發生次數（`replay_reference_only`），沒有紀錄的項目依它能動用的金額上限排（推論）。

### 2.1 防守時賣多少、現金留幾趴（第 19、33 列）

- **現在怎麼決定**：風險 Agent 選姿態與子等級；`cash_ratio_range.grid` 把它換成現金比例（防守為 15／18／21%）。現金低於這個比例時，`_drawdown_derisk` 自動賣股補足，一次最多 NAV 的 25%。交易 Agent 只能決定先賣誰，不能說「這次不賣」或「少賣一點」。
- **影響**：封存中唯一一次大額覆寫就在這裡（11-04，48,450,690 元）。以最後一份封存的 NAV 約 9.49 億元估算，格點每差 3 個百分點約 2,850 萬元（推論）。
- **為何這是判斷**：「現在該保守」是 Agent 的判斷；「保守等於 15% 還是 21%」「今天就要補足還是分幾天」是幅度與速度的判斷，目前由 9 個人設數字決定。
- **改成 Agent 判斷後程式只守**：現金低於 25%（比賽規則）、回撤下限（第 22 列）、單次減碼上限（第 34 列）、換手上限、持股不低於 20 檔。
- **風險**：兩個 Agent 的衝突（風險 Agent 要現金、交易 Agent 要續抱）不會消失，只是換人裁決。使用者已在 [agent_autonomy_plan.md](agent_autonomy_plan.md) 第 6 節第 4 項決定「量聽風險 Agent、對象聽交易 Agent」；讓交易 Agent 可以拒絕減碼，等於推翻這個決定，防守姿態可能補不到現金。
- **回滾**：格點是設定值，換回 `config/policy_versions/` 的樣板即可。若新增 Agent 輸出欄位則要改契約與 Skill，回滾是刪掉新閘門欄位。

### 2.2 每檔買多少（第 30、31 列）

- **現在怎麼決定**：交易 Agent 只給高／中／低。程式用乘數 1.5／0.6／0.3 分配預算，並保證高信心至少拿到單股上限的六成（一般股 6%、2330 為 15%）。`volatility_exponent` 為 0，所以不看波動度。
- **影響**：每一張買單的金額都由這兩組數字決定。一檔高信心新股至少約 5,700 萬元（NAV 9.49 億 × 6%，推論）；[agent_autonomy_plan.md](agent_autonomy_plan.md) 第 3.3 節記錄過 6446 一檔占去大半可用現金。
- **為何這是判斷**：兩檔同為高信心時，Agent 無法表達「這檔想買滿、那檔先試單」。部位大小是基金經理人的核心判斷之一。
- **改成 Agent 判斷後程式只守**：單股上限、現金下限、整張、產業與換手上限。Agent 仍不輸出數字，只能多一個列舉（例如試單／標準／滿額），對映的數字仍由人設。
- **風險**：列舉變多，驗證失敗機率上升；Agent 可能全部選最大。高信心份額變小會讓更多候選成交，持股更分散。
- **回滾**：乘數與 `alpha` 是設定值，換回樣板。新增列舉則是新閘門欄位，刪掉即回滾。

### 2.3 滿檔時能換幾檔、誰有資格被換（第 14、15、16 列）

- **現在怎麼決定**：誰換誰由 Agent 提名（已交給 Agent）。但程式另有三道資格與上限：
  - 一次最多 2 對，換出者要持有滿 5 天。
  - 換出者必須有這檔自己的空頭依據或持股檢視事實。以「組合太防守」為由的配對（`purpose: exposure`）只封存、不成交。
  - 低信心新股不能被提名，也拿不到出場釋出的名額。
- **影響**：名額是沒買到的最大宗原因（163／216）。持股 30 檔時，每天能成交的新股數 = Agent 自己的出場數 + 最多 2 對換股。低信心買進意圖 75 筆中 0 筆有買單；旗標 `ROTATION_CONVICTION` 在 11 份封存出現 0 次，所以這 75 筆主要是被名額擋下，不是被這個門檻擋下（事實）。
- **為何這是判斷**：「一次 2 對」是速度上限，屬於人的決定（界線）。「組合太防守算不算換股理由」「低信心值不值得換進來」是策略取捨，目前由程式規則回答「不算」「不值得」。
- **改成 Agent 判斷後程式只守**：每次對數上限、持有天數、換手、現金下限、不低於 20 檔、不只賣不買。
- **風險**：反彈判斷錯誤時追高；放掉高信心金融股後反彈失敗。[agent_led_exposure_plan.md](agent_led_exposure_plan.md) 第 5 節已列出。
- **回滾**：`agent_swap.exposure` 改回 `v1` 或刪掉該欄位；`rotation.min_new_conviction` 改回 `medium`。
- **現況**：曝險理由的正式版（`agent_swap.exposure` v2）已實作、尚未開啟，開啟時需要人填 `max_pairs_by_view` 兩格數字。

### 2.4 自己出場的股票 3 天內不得買回（第 36 列）

- **現在怎麼決定**：冷卻名單只計交易 Agent 自己標的出場。3 天內 Agent 想買回，程式不建買單，也不讓它被提名換入。
- **影響**：封存中 3 筆（11-05 的 1815、3443、3533，當時是舊版、把程式賣出也算進去）。現行版只擋 Agent 自己的出場，預期次數更少（推論，沒有紀錄）。
- **為何這是判斷**：「昨天賣、今天想買回」可能是新證據出現，也可能是搖擺。程式用固定天數一律當成搖擺。
- **改成 Agent 判斷後程式只守**：換手上限、現金下限。做法已實作為 `reentry_cooldown` v3（改成提示，買回時必須回應，沒回應才取消）。
- **風險**：來回交易的費稅。
- **回滾**：`reentry_cooldown.version` 改回 `v2`。

### 2.5 `add` 在現有權重已高於份額時不成交（第 32 列）

- **現在怎麼決定**：Agent 標 `add`，但持股現有權重已高於信心份額時，程式不建買單。
- **影響**：封存中 `RISK_HELD_WEIGHT` 4 筆，另有 25 筆沒有旗標的 `add` 未成交，抽看多半屬同一類（[agent_autonomy_plan.md](agent_autonomy_plan.md) 第 1.2 節）。合計 29 筆，是僅次於名額的第二大未成交原因。
- **為何這是判斷**：Agent 說「加碼」，程式把它解讀成「目標權重等於信心份額」。這是第 2.2 項的延伸：幅度由公式決定，所以加碼會變成不動。
- **現況**：`held_weight_budget` v2 已把現有權重與份額放進交易 Agent 的簡報，Agent 至少知情。
- **改成 Agent 判斷後程式只守**：單股上限、現金下限。
- **風險**：已經偏重的持股再加碼，集中度上升。
- **回滾**：`held_weight_budget` 改回 `v1`（只少了簡報事實，算法相同）。

### 2.6 `trim` 一律賣一半（第 17 列）

- **現在怎麼決定**：`trim_fraction 0.5`。
- **影響**：每一筆減碼。最後一份封存有 1 筆（2890 賣 151 張）。
- **為何這是判斷**：Agent 無法表達「小幅減」或「大幅減」。
- **改成 Agent 判斷後程式只守**：整張、持股不低於 20 檔。需要新列舉、改契約與 Skill（[agent_autonomy_plan.md](agent_autonomy_plan.md) 列為「下一批」，尚未實作）。
- **回滾**：新閘門欄位刪掉即可。

### 2.7 持股檔數的對映（第 21 列）

- **現在怎麼決定**：集中／平衡／分散等於 22／26／30 檔。
- **影響**：決定還有幾個空名額。目前持股 30 檔，選哪一級都不會多出名額，所以近期影響小（推論）。
- **為何這是判斷**：Agent 只有三個選項。
- **回滾**：設定值。

### 2.8 現金一天最多拉高 3 個百分點（第 23 列）

- 這是自設上限，但它限制的是風險 Agent 的判斷速度：Agent 認為該從 8% 拉到 18% 時，要分好幾天。
- 它同時防止現金大幅來回。[agent_autonomy_plan.md](agent_autonomy_plan.md) 把它排在「下一批」，理由是風險較高。
- 是否放寬是人的決定，不是 Agent 的。

### 2.9 界線裡面的程式排序（第 35、40–43 列）

這幾處是「為了守界線，程式得挑一檔」：

- 釋出順序驗證失敗時依 ATR 賣。
- 產業或換手超限時，先減信心低的買單（信心是 Agent 給的；同信心才看 ATR 與代號）。
- 持股不足 20 檔、現金達 25% 或賣單本身超過換手時，依 ATR 低到高留住 Agent 要出場的持股。
- Active Share 不足時，取消與 ETF 重疊最多的買進。

事實：11 份封存中 `RISK_MIN_POSITIONS`、`RISK_CASH_CEILING`、`RISK_TURNOVER`、`RISK_ACTIVE_SHARE`、`RISK_SECTOR_WEIGHT`、`TURNOVER_LIMIT`、`DERISK_ORDER_SKIPPED` 都是 0 次。影響目前最小，排最後。

另一個程式讀到的細節（事實）：`cash_fit_order` v2 已不再因現金不足取消零頭，但 `_fit_turnover`（1961、1988–1992 行）在換手超限時，對沒有持有、非高信心的新股仍會在殘量低於目標一半時整筆取消。這條路徑在封存中沒有觸發過。

## 3. 反方檢視

### 3.1 看起來像程式替 Agent 判斷，其實必須留在程式

| 項目 | 為什麼不能交給 Agent |
| --- | --- |
| 持股 20～30 檔、現金低於 25%、單股 10%（2330 為 25%）、Active Share 不低於 15% | 比賽規則，違反即無效。沒買到的最大宗原因 `MAX_POSITIONS`，根源是 30 檔上限，不是程式的偏好 |
| 整張、費稅、滑價、賣款同日可買、資金不足整批作廢 | 對應主辦方結算口徑。算錯一張就可能整批作廢 |
| 跳空預留 3%、買進時單股上限留空間 | 決策用前一日收盤、成交用當日價。沒有預留，開高時會超限或資金不足 |
| 權重、張數、現金的數字 | LLM 算術不可靠，且每個數字要能由程式重算核對。Agent 輸出數字，Finalizer 就無法驗證「這個數字從哪來」 |
| 資料時間與來源授權 | 防止用到當時看不到的資料 |
| 證據引用與種類核對 | 防止 Agent 編造依據。這是「限制 Agent」，但限制的是事實，不是判斷 |
| 整份重算、不可變封存、最終檢查 | 帳本是真的，出錯要能追查 |
| 回撤下限（第 22 列） | 不是比賽規則，是帳戶層級的最後防線。它只限制姿態的下限，不挑股。**目前正在作用**：最後一份封存回撤 5.07%，下限為中性，風險 Agent 不能選進攻（事實） |
| 換手 30%、產業 60%、一次換 2 對、持有 5 天、單次減碼 25% | 不是比賽規則，是人設的上限。Agent 自由度變大後，這些是僅剩的數量上限。要放寬是人的決定，不該交給 Agent |
| 列舉換成數字（第 19、21、30、31 列）這個做法本身 | 只要維持「Agent 不輸出數字」，幅度就只能這樣表達。能改的是列舉的粒度與對映的數值，不是拿掉對映 |

### 3.2 全部交給 Agent，在 11/27 前的實際風險

前提（事實）：比賽預計 2026-11-27 結束（使用者口述，文件查無出處；2026-11-11 使用者更正，原記 11-24）。從 11-11 到 11-27 最多 13 個交易日（推論，未扣休市）。

1. **新設定一份真實 run 都還沒有。** 2026-11-10 一天內切換了 `reentry_cooldown` v2、`drawdown_derisk` v5、現金三個 v2、`agent_swap` v2（同時移除 `forced_swap` 與 `conviction_gap_swap`），另開影子 `agent_swap.exposure` v1（不影響訂單，但改變提名的輸出格式）。之後的結果無法分辨是哪一個造成的。再往上加，等於在沒有觀察的基礎上繼續改。
2. **無法事前驗證。** 回放只能固定舊的 Agent 意圖重算程式；新的 Agent 輸出（提名、釋出順序）舊封存裡沒有，連回放都做不到。是否有效只能看即時紀錄，而剩下的天數不到文件原訂的觀察門檻（10 個決策日）。
3. **每多一個 Agent 步驟，多一個失敗點。** 提名與釋出順序驗證失敗時只取消該動作；但任何硬規則失敗仍是整份拒絕，當天改用「全部續抱」的備援檔。Agent 輸出越多，交件期限（08:55）前用完修正次數的機會越高（推論）。
4. **帳戶已在回撤中。** 回撤 5.07%，離 10% 的強制防守還有約 5 個百分點。此時放寬幅度類的限制（部位大小、現金、換手），錯一次的代價比開賽時大。
5. **帳本是真的、不能重來。** 設定可以一行回滾，已成交的訂單不能。
6. **Agent 的典型失誤程式擋不住。** 每天都提滿 2 對、每次都選最積極的列舉、對必須交代的持股寫敷衍理由。這些只能由人看紀錄發現。

反方的結論（推論）：把「選對象」交給 Agent 這一步已經做完，而且是今天才做完。11/27 前再把「選幅度」也交出去，會在沒有任何觀察的情況下同時改兩層；真正缺的不是更多閘門，而是這批閘門的頭幾天紀錄。

## 4. 建議的下一步

最多 5 項，依優先排序。都是小步，不改寫架構。

| 順序 | 做什麼 | 11/27 前能完成嗎 | 需要使用者決定 |
| --- | --- | --- | --- |
| 1 | **先停 2～3 個決策日不再開新閘門**，每天看現行設定的真實紀錄：`cli/calibration.py intent-landing --since 2026-11-11`、`AGENT_SWAP`／`AGENT_SWAP_CANCELLED:*` 旗標、減碼日的 `DERISK_ORDER_SKIPPED`、提名狀態是否 `exhausted`／`unavailable` | 能（不改任何東西） | 是否接受暫停；看到什麼情況要回滾到 `config/policy_versions/2026-11-10-before-agent-swap-v2.json` |
| 2 | **決定要不要讓「組合太防守」成為可成交的換股理由**（`agent_swap.exposure` v2，已實作）。這是使用者原始疑慮最直接對應的一項：風險 Agent 的 `exposure_view` 與交易 Agent 的曝險提名都同意才換 | 能（只改設定）。[follow_up_plan.md](follow_up_plan.md) 建議 2026-11-13 看過約 3 個決策日的影子紀錄後決定 | 開或不開；`max_pairs_by_view` 兩格數字（不得高於 2） |
| 3 | **檢視三組人設數字**（只改設定、不改程式）：高信心至少六成（`alpha 0.6`）、換股一次 2 對、換入者至少中信心。它們決定 Agent 的選擇有多少能成交 | 能，但都是加風險或加換手的方向，建議排在第 1 項之後、一次只動一個 | 每個數字要不要動、動多少。本檔不代訂 |
| 4 | **冷卻改成提示**（`reentry_cooldown` v3，已實作）：Agent 買回時必須交代，沒交代才取消 | 能（只改設定）。原訂觀察 10 個決策日已不可能，要縮短 | 是否接受縮短觀察期；是否接受來回交易的費稅風險 |
| 5 | **同步文件**：`AGENTS.md` 第 76–78 行仍把 `forced_swap`＋`confirmation`、`drawdown_derisk` v4 列為已開啟、把 `exposure_rotation` 寫成會動作；[status.md](status.md) 閘門表開頭仍是 `strategy-2026-11-10-cash-v2-1`，`agent_swap` 同時出現在「已開啟（v1 影子）」與「未開啟」兩張表。這些與現行設定不符（事實），本檔沒有修改 | 能 | 下令即可 |

11/27 前不建議做（都要新增 Agent 輸出、改契約與 Skill，沒有時間觀察）：部位大小列舉、`trim` 幅度列舉、讓 Agent 表達減碼的量或速度、改現金格點或單日拉高上限。這幾項是第 2.1、2.2、2.6 節指出的最大缺口，賽後再排。

## 5. 事實與推論的界線

- **事實**：第 0.1 節的讀取範圍；第 1.1 表的「現在誰決定」與「位置」；第 1.3 節；第 1.4 節的統計表與最後一份封存的狀態；第 2 節各項的「現在怎麼決定」與封存次數；第 4 節第 5 項的文件不一致。
- **推論**：第 1.1 表的「分類」欄與第 1.2 節的比例；第 2 節的排序、「為何這是判斷」、風險與金額估算；第 3.2 節；第 4 節的優先順序。
- **沒有做的事**：沒有重跑決策、沒有回放、沒有驗證現行設定下 Agent 實際會怎麼提名。本檔不宣稱任何一項改了之後績效會更好。
