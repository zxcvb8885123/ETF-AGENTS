# 換股的組數、份量與賣誰：由 Agent 主導、人只設絕對硬上限（計畫）

狀態：**計畫，未實作、未開啟**（2026-11-11 撰寫）。本文件只寫設計，沒有改程式、設定、Skill 或其他文件。把本文件的行為當成現行行為是錯的。

與平行工作的關係：[`quality_dip_agent_sizing_plan.md`](quality_dip_agent_sizing_plan.md)（`agent_swap.quality_dip` v3，兩段式：交易 Agent 提名＋風險 Agent 只能同意／降一格／否決）**正在工作區實作中、尚未 commit**；本文件不改它，也不假設它已經是現行行為。兩者的關係與遷移路徑見第 7 節。

前置閱讀：[`agent_nominated_rotation_plan.md`](agent_nominated_rotation_plan.md)、[`agent_led_exposure_plan.md`](agent_led_exposure_plan.md)、[`decision-contracts.md`](../decision-contracts.md) 的「換股提名」「風險 Agent」節。

## 1. 使用者需求（原意，以此為準）

- 錯殺股（以及一般換股）「最多幾組」「單檔最高幾 %」不該由人填，應由**風險 Agent** 決定。
- 例：交易 Agent 說要買 5 組；風險 Agent 看過，認為其中 3 組最好、最有機會賺錢，再決定各買幾 %。
- 使用者的資產看起來可以每檔都買 5%；若錢不夠，能不能賣掉「報酬不如預期」的持股來換？
- 買幾組、買多重、錢不夠賣哪檔，整套都由 Agent 判斷；**人只設不可跨越的絕對硬上限**。

## 2. 現況事實（含實際數字）

以下數字以唯讀腳本讀封存檔重算，沒有驗 manifest 與雜湊；評價基準是各 run 的 `decision_input.json` 帳戶快照與最後一根收盤。只描述當時的狀態，不代表改了績效會更好。

### 2.1 帳戶與限制（2026-11-11 08:13 run）

出處：`artifacts/decisions/2026-11-11/decision-20261111T001308Z/`（`decision_input.json`、`policy.json`、`proposal.json`、`decision.json`、`team_inputs.json`、`intent.json`）。

| 項目 | 數值 | 出處 |
| --- | --- | --- |
| NAV | 953,014,209 | `decision_input.json` `account_snapshot.nav` |
| 現金（盤前） | 142,528,459，占 NAV **14.96%** | 同上 `cash` |
| 持股（盤前） | **30 檔／上限 30**，股票權重合計 85.0% | 同上 `positions`；`team_inputs.json` `portfolio_slots.free_slots = 0` |
| 現金姿態 | `defensive`／`lower`，對應現金格點 **15%**（`grid.defensive.lower`） | `policy.json` `position_sizing.cash_stance`、`cash_grade`、`cash_ratio_range.grid` |
| 高於現金格點的可用現金 | 142,528,459 − 0.15 × 953,014,209 ＝ **−423,672（−0.044% NAV）**，即沒有可用現金；當天因此是減碼日（`RISK_DRAWDOWN`） | 推算；旗標見 `proposal.json` `constraint_flags` |
| 單股上限 | 10%（2330 為 25%） | `policy.json` `max_stock_weight`、`special_weight_limits` |
| 產業上限 | 60% | `max_sector_weight` |
| 檔數 | 最少 20、最多 30；廣度 `diversified` 對 30 | `min_positions`、`max_positions`、`positions_by_breadth` |
| 單日換手上限 | 30%（買賣雙邊成交額合計 ÷ NAV，`allocation.py` 約 873 行） | `max_turnover_rate` |
| 現金上限 | 25% | `cash_weight_ceiling` |
| 高信心份額下限 | alpha 0.6 × 單股上限 10% ＝ **6%** | `limit_scaled_conviction.alpha` |
| 換股提名上限 | `agent_swap.max_swaps_per_run` 2；quality_dip v2 1 組、單檔 3% | `policy.json` `agent_swap` |
| 當天實際換手 | 4.19% | `proposal.json` `order_proposal.turnover_rate` |
| 盤後（決策結果） | 28 檔、現金 15.30%、最大單檔 7.19%（2330）、中位數 2.92% | `decision.json` `portfolio` |

最近 8 份 run 的走勢（`artifacts/decisions/2026-11-0*/…/decision.json` 與 `decision_input.json`）：

| run | 盤前現金 | 盤前檔數 | 盤後檔數 | 盤後現金 | 最大單檔 | 中位數 | 姿態 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 11-02 | 11.0% | 30 | 29 | 11.5% | 6.72% | 2.90% | defensive |
| 11-03 | 11.6% | 29 | 30 | 10.5% | 6.72% | 2.89% | neutral |
| 11-04 | 10.7% | 30 | 24 | 17.2% | 6.83% | 3.59% | defensive |
| 11-05 | 18.9% | 25 | 25 | 19.0% | 6.95% | 3.45% | defensive |
| 11-06 | 19.2% | 25 | 26 | 18.2% | 7.00% | 3.41% | defensive／middle |
| 11-09 | 18.6% | 26 | 27 | 18.2% | 7.23% | 2.90% | defensive／middle |
| 11-10 | 18.0% | 27 | 30 | 15.1% | 7.00% | 2.88% | defensive／lower |
| 11-11 | 15.0% | 30 | 28 | 15.3% | 7.19% | 2.92% | defensive／lower |

### 2.2 回答：「現在的資產能不能每檔買 5%」

5% NAV ≈ **47,650,710 元**。結論：**同時受名額與現金兩道限制，不能「每檔都 5%」；只能「少數新買進各 5%，錢來自賣掉別檔」。**

1. **全部持股都 5% 做不到**（算術）：N 檔 × 5% ≤ 1 − 現金比例。防守格點 15% 時 N ≤ 17；積極姿態最低格點 3% 時 N ≤ 19。兩者都低於 `min_positions` 20。所以在現行硬規則下，持股不可能全部 5%（現況平均約 85% ÷ 30 ≈ 2.83%）。
2. **新買進一檔 5% 需要先賣掉約 5% 的持股**：11-11 高於現金格點的可用現金是負的，持股 30／30。新股要名額（一換一），要錢（賣款）。
3. **換手上限讓一天最多約 3 組 5% 的換股**：一組 5% 換股的成交額約 10% NAV（賣 5%＋買 5%），30% 上限扣掉其他訂單後最多約 3 組。這與使用者舉例的「挑 3 組」剛好相符，但這是巧合，不是設計。
4. **賣款要夠才買得到 5%**：一換一時，買進金額不超過換出者的賣款（否則會動用現金格點以下的錢）。11-11 權重 ≥ 4% 的持股只有 9 檔：2412（7.00%，hold／high）、4904（5.35%，hold／medium）、2330（5.27%，add／high）、2886（4.86%，hold／high）、2885（4.63%，hold／medium）、2618（4.48%，hold／medium）、1402（4.03%，hold／low）、2880（4.03%，hold／high）、1216（4.00%，hold／low）。換出低權重持股（例如 8046 的 0.74%）最多只換得到約 0.74% 的新股。
5. **份量 5% 在現行份額之間**：high 新股的份額下限已是 6%；medium、low 的份額由剩餘預算依乘數（1.5／0.6／0.3）分配，現況中位數約 2.9%。

### 2.3 回答：「錢不夠時，哪些持股符合報酬不如預期」

「報酬不如預期」沒有唯一定義。兩種口徑的結果**不一致**，下表全部列出、不替人挑（出處：`team_inputs.json` `position_review.items`、`intent.json`、`policy.json` `exposure_rotation.held_since_by_symbol`）。

注意（事實）：`position_review.cost_basis_note` 寫明「比賽開帳或重建日的成本是當日結算收盤價，不是真實買價」。所以「未實現報酬」是從開帳日（多數為 10-27）起算的漲跌，不是真實損益。`loss_alert` 門檻 15%，**沒有任何一檔觸發**。

| 持股 | 第一輪 | 權重 | 未實現報酬 | 20 日相對交易池 | 5 日相對交易池 | 持有日曆天數（11-11） | 現行可否當換出者 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 3034 | hold／low | 1.60% | −10.4% | +5.9% | −11.0% | 15 | 11-11 已換出 |
| 3653 | hold／medium | 0.61% | −10.2% | **−3.2%** | −1.1% | 7 | 天數可 |
| 4904 | hold／medium | 5.35% | −7.5% | +15.5% | −3.3% | 15 | 天數可 |
| 8046 | hold／low | 0.74% | −6.9% | **−15.9%** | −3.1% | 15 | 天數可 |
| 1216 | hold／low | 4.00% | −6.7% | +15.8% | −4.0% | 15 | 天數可 |
| 2609 | hold／low | 2.25% | −6.6% | +13.4% | −3.3% | 15 | 天數可 |
| 2801 | hold／medium | 3.53% | −5.8% | +14.9% | −4.0% | 15 | 天數可 |
| 6446 | hold／high | 2.01% | −5.5% | +23.1% | +7.4% | 2 | **否**（未滿 5 天） |
| 3045 | hold／high | 3.95% | −5.2% | +19.2% | −0.8% | 15 | 天數可 |
| 3374 | hold／medium | 1.34% | −5.0% | +1.4% | +1.7% | 15 | 天數可 |
| 2603 | hold／low | 3.40% | −4.7% | +25.8% | −1.2% | 15 | 天數可 |
| 1402 | hold／low | 4.03% | −3.2% | +11.5% | −2.0% | 15 | 天數可 |
| 2327 | hold／low | 1.18% | −2.8% | **−18.5%** | −5.1% | 1 | **否**（未滿 5 天） |
| 2890 | hold／low | 0.67% | −2.4% | +11.5% | −2.2% | 15 | 11-11 已換出 |

（其餘 16 檔未實現報酬 > −2%，略；完整清單在同一份 `position_review`。持有天數是 `exposure.holding_days_by_symbol` 的日曆天數算法；未滿 `agent_swap.min_holding_days` 5 天的另有 2610、5880。）

- 以未實現報酬看，最差是 3034、3653、4904、8046、1216。
- 以 20 日相對交易池看，只有 3653、8046、2327 為負：交易池 20 日跌了約 16%，多數持股「跌得比較少」，相對報酬反而是正的。
- 推論：兩個口徑互相矛盾，正說明「哪檔報酬不如預期」是判斷，不該由程式依報酬排序挑人。交易 Agent 每檔都寫了 `invalidation_conditions`（例如「近 5 日與近 20 日報酬都明顯落後交易池」），由它對照自己的條件判斷，比程式選一個口徑更貼近使用者的意思。

### 2.4 11-11 的關鍵事例：換了，但新股沒買到

出處：`proposal.json` `constraint_flags`、`decision.json` `orders`、`team_inputs.json` `swap_nomination`。

- 交易 Agent 提名 2 組 thesis 換股：買 2408（high）賣 2890、買 6239 賣 3034。
- 結果：2890（152 張）與 3034（32 張）照賣；2408、6239 **都是 `LOT_SIZE`（目標金額不足一張）、沒有買單**，旗標 `AGENT_SWAP_PROCEEDS_TO_ADD`：賣款流向 2330 加碼（8 張、18,324,396 元）。當天實際成交 3 筆。
- 事實：換入者的買進金額**不是**由這一對的賣款決定，而是由全體信心預算的剩餘額度決定；當天現金在格點以下、預算被 high 加碼 2330 先用掉，所以換入者分到的金額不足一張。
- 推論：這就是使用者要求的缺口——「買幾組、每組多重」目前沒有任何 Agent 回答；交易 Agent 提了換股，程式卻把錢給了別檔。

### 2.5 現行哪些機制能「賣掉不如預期的持股來換」

| 機制 | 現況 | 能做到 | 做不到／缺口 |
| --- | --- | --- | --- |
| `agent_swap` v2 thesis 配對（已開啟） | 交易 Agent 在 5c 提名有序配對；換出依據可為本次已採納的空頭 claim **或持股檢視事實 `review:<代號>:position`** | 交易 Agent 已可用「報酬不如預期」當換出依據提名賣誰 | 對數由人設 2；換入者份量由信心預算決定（11-11 不足一張）；風險 Agent 不參與 |
| `agent_swap.exposure` v2（已開啟） | `purpose: exposure` 由 `exposure_view` 決定取用對數（`keep_defensive`＝0） | 組合層級理由讓位 | 份量同上；只回答曝險 |
| `agent_swap.quality_dip` v2（已開啟） | 1 組、單檔 3%、買進不超過換出賣款 | 錯殺股換股，買進以賣款為上限 | 組數與 % 由人設；v3（實作中）改為人設上限＋兩段式 |
| `agent_swap.proceeds_to_add` v1（已開啟） | 換入者無買單時賣款流向 high 加碼持股 | 賣了不會閒置 | 由程式決定錢給誰；與「每組各買幾 %」直接衝突 |
| `drawdown_derisk` v5＋`release_order`（已開啟） | 減碼日依交易 Agent 給的順序賣到現金回到格點 | Agent 決定先賣誰 | 只賣不買，用途是補現金，不能拿來買別檔 |
| `rotation.forced_swap`（已移除，與 `agent_swap` v2 互斥） | 曾由程式依論點檢視 broken 換股 | — | 現行不存在 |
| `exposure_rotation` v3 換股步驟 | `agent_swap` v2 開啟後停用（`EXPOSURE_ROTATION_SKIPPED:agent_swap`） | — | 現行不換股 |
| 一般「賣 A 籌錢加碼 B」（不換名額） | 只有 `proceeds_to_add` 與減碼日的 `release_order` 碰得到 | — | 沒有 Agent 能說「賣 A 是為了加碼 B」 |

缺口歸納（推論）：

1. 換股的「份量」沒有 Agent 回答，換入者常拿不到錢。
2. 「幾組」由人設的數字直接決定。
3. 風險 Agent 對換股完全沒有發言權（v3 只對 quality_dip）。
4. 賣款不足（換出者權重小）時，沒有正式的補款方式。

## 3. 責任劃分

| 誰 | 決定什麼 | 形式 |
| --- | --- | --- |
| 人 | 絕對硬上限：一天最多成交幾組、單檔換入權重上限、交易 Agent 最多可提名幾組；既有硬規則（現金格點、單股與產業上限、換手、檔數、持有天數、冷卻、CompetitionGuard）；閘門開不開 | Policy 數字，全部必填，程式不給預設（本文件留空） |
| 交易 Agent（5c 換股提名） | 提名哪幾組（買誰、賣誰）、先後、每組建議份量格；換出依據（空頭 claim 或 `review:<代號>:position` 等程式事實，例如「報酬不如預期」）；不提名的原因 | 代號、列舉、識別碼、文字 |
| 風險 Agent（新：配置裁決，6b） | 從交易 Agent 的 N 組挑出要執行的組、排定執行先後、決定每組最終份量格；不選的寫原因 | 列舉、識別碼、文字 |
| 程式 | 把份量格對成權重；依風險 Agent 的先後套硬上限與所有後段限制；縮到不足一張取消整組（換出者不賣）；記旗標；Finalizer 整份重算 | 不挑股、不排序、不補提名、不升級 |

**組數就是風險 Agent 選中清單的長度，不是 Agent 寫出的數字。** 方案 A 不需要對「Agent 不輸出數字」開例外。

## 4. 設計（方案 A：列舉份量格＋風險 Agent 挑選排序，程式對數字）

### 4.1 流程

```
交易 Agent 第一輪（逐檔意圖、信心）→ 入選排序（5b）
        │
換股提名（5c，交易 Agent）            ← 提名 N 組（N ≤ 人設提名上限），每組附建議 size
        │
風險 Agent 現金姿態（6，簡報不變）     ← level／cash_grade／breadth／turn_assessment／exposure_view
        │
程式前置檢查（沿用各 purpose 的硬性條件）
        │
風險 Agent 配置裁決（6b，獨立呼叫）    ← 挑哪幾組、先後、每組 size_grade；其餘寫不選原因
        │
程式配置（7）：份量格→權重→硬上限→賣款／現金→換手→單股／產業→不足一張取消整組
        │
Finalizer 整份重算、Guard
```

### 4.2 交易 Agent（5c）的變更

- 沿用 `agent_swap` 的有序配對清單與 `purpose`（`thesis`／`exposure`／`quality_dip`）。
- 每一組（不只 quality_dip）必填列舉 `size`：`small`／`medium`／`large`（意思：試單／一般／把握最高）。不輸出百分比。
- 換出依據規則不變：本次已採納的空頭 claim，或程式算的持股事實（`review:<代號>:position`；exposure 配對另可用 `review:<代號>:exposure`）。「報酬不如預期」走 `review:<代號>:position` 並在理由對照該股自己的 `invalidation_conditions`。
- 提名上限改為「最多可提名幾組」（人填），可以大於最後成交上限，讓風險 Agent 有得挑（使用者例子：提 5 組、挑 3 組）。
- 簡報多給：每組換出者的權重（讓 Agent 知道賣款大約夠買多少格，以「賣款約可支應的份量格」列舉呈現，不給百分比；呈現方式見第 10 節第 6 項）。

### 4.3 風險 Agent 配置裁決（6b）

把 v3 的 quality_dip 裁決步驟推廣到全部 purpose（同一個呼叫位置，不另開平行步驟）。

**簡報**（只有這些）：

- 每一組：`pair_id`（程式依交易 Agent 清單順序編 `pair-1…pair-N`）、`purpose`、買賣代號、交易 Agent 的 `size` 與理由、兩邊依據識別碼對應的 claim 文字、程式事實（換出者權重、未實現報酬、5／20 日相對交易池、持有天數；兩檔 beta、產業、ATR；換入者被拖累背景若有）。
- 風險 Agent 本次現金姿態輸出（`level`、`cash_grade`、`turn_assessment`、`exposure_view`）。
- 組合曝險、產業權重、轉向、加權指數事實（背景、不可引用）。
- **不附**：其他股票的意圖與信心統計、任何權重上限的百分比。

**輸出**：

| 欄位 | 內容 |
| --- | --- |
| `selected` | 有序清單＝執行先後。每項 `pair_id`、`size_grade`（`small`／`medium`／`large`）、`reason`（文字，必填） |
| `declined` | 每項 `pair_id`、`reason_kind`（`market_risk`／`portfolio_concentration`／`timing`／`weaker_case`）、`reason` |
| `evidence_ids` | 至少一個市場層級證據（同現金姿態的要求） |
| `defensive_reconciliation` | `level` 為 defensive 或 `exposure_view` 為 `keep_defensive`，且 `selected` 非空時必填 |

**能與不能**：

| 能 | 不能 |
| --- | --- |
| 從 N 組挑出要做的、排先後、決定份量格 | 新增配對、換掉買或賣的代號 |
| 全部不選（今天不換） | 新增事實、引用交易 Agent 沒採納的 claim |
| 依市場、組合集中度、兩邊論點強弱比較取捨 | 手寫權重或張數、改現金比例、覆寫 CompetitionGuard |
| 升級份量格（是否允許見第 10 節第 1 項） | 讓成交高於人設硬上限（程式會縮） |

**Validator**（只驗結構，不評理由好壞）：

- `selected` ∪ `declined` 剛好涵蓋每個 `pair_id` 各一次；不得有清單外的 `pair_id`。
- `selected` 長度不得超過人設的成交組數上限（超過就是退回重答，而不是程式默默截尾——讓「挑哪幾組」完全是 Agent 的選擇）。
- 同一檔換入者或換出者不得在 `selected` 出現兩次。
- `exposure` 配對：本次自己的 `exposure_view` 為 `keep_defensive` 時不得選（與現行 `keep_defensive` 取消 exposure 配對一致，避免紀錄互相矛盾）；選中的 exposure 配對數不得超過 `max_pairs_by_view` 對應值。
- `quality_dip` 配對：前置檢查不通過（回撤下限 defensive、`turn_assessment` 低於門檻）時程式不把它放進簡報。
- 若第 10 節第 1 項選「只能降不能升」：`size_grade` 不得高於交易 Agent 的 `size`。
- `declined` 缺 `reason_kind` 或 `reason`、`defensive_reconciliation` 該填未填或不該填而填、`evidence_ids` 無市場層級證據或引用不存在：拒絕。
- 文字出現前次 run 識別碼、背景鍵名、facts_id：拒絕。輸出含數字欄位或白名單外欄位：拒絕。
- 綁定本次提名識別碼與雜湊、現金姿態雜湊；不一致拒絕。

### 4.4 與風險 Agent 既有輸出的銜接（不重複、不矛盾）

| 既有輸出 | 回答的問題 | 與配置裁決的關係 |
| --- | --- | --- |
| `level`／`cash_grade` | 留多少現金 | 不變。裁決不改現金；換入買進只用該組賣款（與格點以上的可用現金，見第 10 節第 5 項），不動用格點以下的錢 |
| `breadth` | 持股檔數上限 | 不變。一換一不改檔數 |
| `turn_assessment` | 市場有沒有轉向 | 不變；仍是 quality_dip 的人設門檻，由程式在裁決前檢查 |
| `exposure_view` | 要不要以曝險理由讓低 beta 持股讓位 | 不變；決定 exposure 配對可不可選、最多幾組，裁決只在這個範圍內挑 |
| 配置裁決（新） | 交易 Agent 提的這些換股，今天做哪幾組、誰先、各多重 | 只管換股配對，不管一般買進與加碼 |

現金姿態那次呼叫在看到任何提名之前定案，簡報完全不變，維持「風險 Agent 的姿態簡報不附交易 Agent 的意圖與信心統計」。配置裁決是另一次獨立呼叫，對這條規則開一個窄例外（同 v3 已決定的第 2 項）。

### 4.5 程式配置

1. 只處理 `selected` 的組，依風險 Agent 的先後。
2. 份量格對成換入權重上限：`large`＝人設的單檔換入權重上限（purpose 別的上限見第 10 節第 3 項）、`medium`／`small` 為它的固定比例（沿用 v3 的程式常數 2/3、1/3，或由人填，見第 10 節第 4 項）。
3. 換入買進金額＝下列取最小，只縮不放：份量格權重 × NAV、該組換出者賣款淨額（含費稅）＋分到的格點以上可用現金、單股上限餘裕、產業上限餘裕。
4. 換入者的買進**先保留**，再跑一般的信心預算配置；換入者不再參與信心預算（避免 11-11 那種被 high 加碼先吃掉、只剩零頭的情況）。
5. 依先後累計換手，會超過換手上限的組從該組起取消。
6. 縮到不足一張：整組取消、換出者不賣（`AGENT_SIZING_CANCELLED:<原因>`）。**不會只賣不買。**
7. 本閘門開啟時，選中的組不適用 `proceeds_to_add`：換入者沒買到就整組取消，賣款不流向別檔。
8. 換出者賣單標 `exit`＋`AGENT_SWAP`；旗標說明寫交易 Agent 的 `size`、風險 Agent 的 `size_grade` 與名次、最後套用的上限。交易 Agent 的意圖不改寫。
9. 其後照舊受現金格點、Active Share、CompetitionGuard；任一硬規則仍不過，照舊整份拒絕。

### 4.6 錢不夠時賣誰（使用者的第三個問題）

- **賣誰由交易 Agent 提名、風險 Agent 挑選**：每一組本身就是「賣這檔來買那檔」。賣款不夠支應份量格時，程式只會把買進縮到賣款（第 4.5 節第 3 項），不會自行加賣別檔。
- 若要「賣款不夠時另外減碼一檔補錢」，需要新的配對形式（一組可附 `funding_symbols`：額外減碼或出場的持股，同樣須有換出依據、同樣由風險 Agent 一併挑選）。這會讓檔數可能減少（多換一），仍受 `min_positions` 20。是否做見第 10 節第 5 項；推薦第一階段先不做。
- 保證：買進金額 ≤ 本組（含補款持股）賣款淨額＋分到的格點以上現金；任何限制使買進不足一張即整組取消、全部賣單不賣。
- 與減碼日：`drawdown_derisk` v5 的 `release_order` 仍只負責把現金補回格點、只賣不買；配置裁決的換股在其後處理，`derisk_day_view` v1 的規則不變。兩者不共用賣款：減碼賣出的錢用來補現金，不得拿去支應換股。

### 4.7 契約、`team_inputs` 與重建

- 閘門：`position_sizing.agent_swap.sizing`（暫名）`{"version": "v1", "max_nominations": 【待人填】, "max_pairs_per_run": 【待人填】, "max_entry_weight": "【待人填】"}`；前提 `agent_swap` v2。只由人設定；`agent_swap` 前綴已在策略反思禁區。pipeline 綁定（人不得預填）：`ruling_ref`（裁決識別碼、雜湊、狀態）、`rulings`（選中組的有序清單與 `size_grade`）；`nominated_pairs` 每組多 `size`。
- Policy 驗證：`max_pairs_per_run` ≤ `max_nominations`；`max_entry_weight` 在 (0, 單股上限]；本閘門開啟時 `agent_swap.max_swaps_per_run` 的角色由 `max_pairs_per_run` 取代（只能有一個出處，實作時二擇一並寫進契約）。
- `team_inputs`：v3 已用掉 2.10（`quality_dip_ruling`）。推薦 **2.11 ＝ 2.10 的超集**，多必要鍵 `swap_ruling`；本閘門開啟時 `quality_dip_ruling` 封存為 `superseded`（quality_dip 配對改由 `swap_ruling` 裁決）。理由：裁決是會改變配置的 Agent 輸出，比照 2.9／2.10 升版、不走選配鍵（選配鍵目前只用於程式算的事實）。另一做法見第 10 節第 7 項。
- Finalizer：以同一個 Validator 重驗 `swap_ruling`，由 `team_inputs` 的提名與裁決重算 Policy 綁定的 `size`、`rulings`、`ruling_ref` 逐欄比對；裁決不是 `completed` 卻有依裁決成交的組，拒絕。配置股數與旗標照舊由 Policy 與 bundle 整份重算。
- 回放：隨 `agent_swap` 略過，notes 標 `agent_swap.sizing: ignored_in_replay`，`evidence_grade: replay_reference_only`。
- 舊版相容：沒有 `agent_swap.sizing` 的 Policy，簡報、Schema、`team_inputs` 版本、配置與封存 run 重建雜湊逐位元不變；舊提名帶 `size`（非 quality_dip v3）拒絕。

### 4.8 失敗降級（fail-closed）

| 情況 | 處理 |
| --- | --- |
| 裁決驗證失敗 | 帶錯誤回饋的修正循環，最多三次 |
| 修正用盡（`exhausted`）／暫時性錯誤重試仍失敗（`unavailable`） | 見第 10 節第 8 項：推薦**本次全部換股配對取消、換出者不賣**，其餘決策照常；不退回「程式自己決定份量」 |
| 提名本身 `exhausted`／`unavailable` | 照現行：不換股，不呼叫裁決 |
| 前置檢查使全部組不可選（例如只有 exposure 組而 `keep_defensive`） | 不呼叫裁決，狀態 `not_required` |
| 安全拒絕 | 不重試，比照換股提名現行處理（往上拋、整份停止） |
| 硬規則失敗 | 照舊整份拒絕 |

## 5. 方案比較：A 列舉格子 vs B 風險 Agent 直接寫比例

| 項目 | A：列舉份量格（推薦） | B：風險 Agent 直接寫百分比（需人明確同意破例） |
| --- | --- | --- |
| 內容 | 風險 Agent 選 `small`／`medium`／`large`，程式依人設上限對成權重 | 風險 Agent 對每組寫「買 4.5%」，程式只夾在硬上限內 |
| 與鐵律 | 相容：Agent 不輸出數字 | **違反** AGENTS.md「Agent 不輸出任何數字」 |
| 可重算 | 可：同一份 `team_inputs` 與 Policy 重算出相同股數 | 若把百分比存進 `team_inputs`，配置**仍可**確定性重建；破壞的不是「重算」本身 |
| 實際破壞的保證 | 無 | ①「所有數值都能追溯到確定性工具與 `evidence_id`」：4.5% 沒有任何工具或證據來源；② Validator 只剩範圍檢查（連續值無法驗「合不合理」，只能驗「在不在上下限」）；③ 跨日可比與統計：連續值難以累積分布、難以回放比較；④ 提示注入面變大：外部文字可誘導一個具體數字；⑤ 多個既有閘門（`cash_grade`、`exposure_view`、`size`）都是「列舉→人設數字」，B 會是唯一例外 |
| 表達力 | 三格（或人決定的格數） | 連續 |
| 使用者原話 | 「決定各買幾 %」以格子表達 | 字面最接近 |

推薦 A。理由：使用者要的是「Agent 判斷多重」，三格已能表達輕重；B 換到的連續表達力，代價是鐵律與 Validator 的實質失效。若人選 B，至少要求：百分比必須存進 `team_inputs`、只能縮不能超過硬上限、Validator 驗小數位數與範圍、AGENTS.md 明文記載破例。

## 6. 不變的邊界

- Agent 不輸出數字（方案 A）；組數是清單長度。
- 現金姿態簡報不變；風險理由出現前次 run 識別碼即拒絕；風險 Agent 不手寫權重、不覆寫 CompetitionGuard、不處理停損停利。
- 程式不改寫交易 Agent 的意圖，只留旗標；不挑股、不排序、不補提名。
- 一換一：不足一張整組取消、換出者不賣，不會只賣不買；買進不超過本組賣款加分到的格點以上現金。
- 現金格點、換手、檔數、單股與產業上限、Active Share、持有天數、再買冷卻一律不放寬。
- `exposure_view` 仍只管 exposure 配對；quality_dip 的回撤下限與轉向門檻仍由程式先檢查。
- 背景不是證據；所有版本閘門只由人設定、策略反思不得改。

## 7. 與 quality_dip v3（實作中）的關係與遷移

事實：v3 在工作區實作中、未 commit；它的裁決步驟（6b，`decision/quality_dip_ruling.py`）只裁 quality_dip，風險 Agent 只能同意／降一格／否決，組數與單檔上限由人填。

| 做法 | 說明 | 評價 |
| --- | --- | --- |
| 取代 | 不做 v3，直接做本計畫 | 浪費正在進行的工作，也會與另一個 agent 衝突 |
| **擴充（推薦）** | v3 照原計畫完成並由人決定是否 commit；本計畫做成 `agent_swap.sizing`，重用 v3 的裁決呼叫位置、修正循環、封存與綁定框架，把裁決對象從 quality_dip 推廣到全部 purpose，並加入「挑選與排序」 | 不打斷 v3；共用程式碼；quality_dip 的人設門檻（`min_turn_assessment` 等）原樣保留 |
| 並存 | v3 裁 quality_dip、另一個步驟裁其餘配對 | 兩次風險 Agent 呼叫、兩套組數上限互相搶換手額度，不推薦 |

遷移路徑（推薦）：

1. 等 v3 完成；人決定 v3 是否開啟（其硬上限仍待人填）。
2. 實作 `agent_swap.sizing` v1，前提 `agent_swap` v2；與 quality_dip v3 同時開啟時，quality_dip 配對改由 `swap_ruling` 裁決（`quality_dip_ruling` 封存 `superseded`），v3 的 `max_entry_weight` 當 quality_dip 配對的份量上限。
3. 回滾：刪掉 `position_sizing.agent_swap.sizing`，`team_inputs` 回到 2.10（或 2.9），行為回到 v3（或 v2）。開啟前先存樣板 `config/policy_versions/【開啟日期】-before-agent-sizing.json`。
4. 比賽 11-27 結束（使用者 11-11 更正，原記 11-24），11-12 起剩約 12 個交易日；實作與測試時間無法在本文件保證，若趕不上，v3 仍是可用的較小版本。

## 8. 實作順序與測試清單

### 8.1 實作順序

1. `decision/swap_nomination.py`：全 purpose 的 `size`、`max_nominations`、簡報的換出者賣款份量提示。
2. 新檔 `decision/swap_ruling.py`（或在 v3 `quality_dip_ruling.py` 上推廣）：契約、簡報、Validator、封存、Policy 綁定、`team_inputs` 重建。
3. `decision/sizing.py`：`agent_swap.sizing` 閘門驗證、綁定欄位形狀。
4. `automation/agent_schemas.py`、`risk_stages.py`、`daily_pipeline.py`：6b 推廣、前置檢查、修正循環、降級、`agent_calls`。
5. `decision/allocation.py`（只讀需要的行段）：`_agent_swap` 新分支、換入者保留預算、依裁決先後的換手累計、本閘門下停用 `proceeds_to_add`。
6. `decision/finalization.py`：2.11、重算比對。
7. Skill：`tradeagent/trading/trader/references/swap-nomination.md`、`tradeagent/risk/portfolio-risk-review/`（新 reference；確認 `agents/openai.yaml` 描述一致）。
8. 文件：`AGENTS.md`、`README.md`、`docs/status.md`、`docs/decision-contracts.md`、`docs/architecture.md`（2.11）、`docs/glossary.md`、`docs/operations.md`。

### 8.2 測試清單（`unittest.TestCase` 風格）

舊版相容：

- 沒有 `agent_swap.sizing`：簡報、Schema、`team_inputs` 版本、配置、封存 run 重建雜湊逐位元不變。

閘門：

- 缺任一必填數字、`max_pairs_per_run` > `max_nominations`、`max_entry_weight` 超過單股上限、`agent_swap` 不是 v2：拒絕。
- 人手預填 `rulings`／`ruling_ref`：拒絕。

提名：

- 任一組缺 `size`、值域外、提名超過 `max_nominations`、含數字欄位：拒絕。

裁決：

- `selected`∪`declined` 漏組、重複、清單外 `pair_id`；`selected` 超過 `max_pairs_per_run`；同一檔出現兩次：拒絕。
- `keep_defensive` 下選 exposure 組、超過 `max_pairs_by_view`：拒絕。
- （若選只降不升）`size_grade` 高於 `size`：拒絕。
- `declined` 缺原因、`defensive_reconciliation` 該填未填或不該填而填、無市場層級證據、引用不存在、文字含前次識別碼或背景鍵名、含數字欄位：拒絕。
- 綁定雜湊不符：拒絕。現金姿態簡報與舊版逐鍵相同；裁決簡報不含其他股票的意圖與信心統計。

配置：

- 依風險 Agent 的先後成交；`declined` 的組換出者不賣。
- 份量格對應權重不高於人設上限；買進不高於本組賣款淨額＋分到的格點以上現金（逐筆比對 `net_cash_impact`）。
- 換入者買進先保留、不被 high 加碼吃掉（以 11-11 的情境做回歸：2408 不再 `LOT_SIZE`，或在賣款不足一張時整組取消、2890 不賣）。
- 換手累計超過上限時從該組起取消；單股、產業上限縮減；不足一張整組取消、換出者不賣。
- 本閘門下選中組不出現 `AGENT_SWAP_PROCEEDS_TO_ADD`。
- 交易 Agent 意圖不被改寫；旗標含 `size`、`size_grade`、名次與套用上限。
- 減碼日：`release_order` 的賣款不支應換股。

降級：

- 裁決 `exhausted`／`unavailable`：全部換股取消、換出者不賣、其餘訂單照常。
- 只有不可選的組：`not_required`、不呼叫。
- 裁決不是 `completed` 卻有依裁決成交：Finalizer 拒絕。

重建：

- 2.11 缺 `swap_ruling` 拒絕；2.10／2.9 帶它拒絕；竄改 `size`、`rulings`、`ruling_ref` 任一欄拒絕。

修改後照 `AGENTS.md` 執行完整 pytest、`compileall` 與 `git diff --check`。

## 9. 風險

| 風險 | 說明 | 擋法 |
| --- | --- | --- |
| 風險 Agent 變成第二個選股者 | 「哪組最有機會賺錢」是個股判斷，與風險 Agent「不做個股判斷」的現行邊界衝突 | 只能讀交易 Agent 已採納的 claim 與程式事實、不得新增事實；是否允許見待人決定第 1 項 |
| 每次都選 `large` | 列舉變成變相最大值 | `large` 就是人設上限；買進受賣款限制；紀錄選擇分布 |
| 換出低權重持股、份量格形同虛設 | 11-11 的 2890、3034 權重 0.67%、1.60%，就算 `large` 也只買得到賣款那麼多 | 簡報提示「賣款約可支應的份量格」；是否允許補款賣出見第 10 節第 5 項 |
| 被交易 Agent 的理由帶著走 | 風險 Agent 看到提名後失去獨立性 | 現金姿態先定案；裁決不能新增配對 |
| 多一次呼叫的時間與失敗點 | 每天多一次 Agent 呼叫 | 只有有可選組才呼叫；失敗只取消換股 |
| 時程 | 比賽剩約 9 個交易日 | 分階段；v3 仍可先用 |
| 沒有前瞻證據 | 舊 run 沒有這些輸出 | 回放只是 `replay_reference_only`；只有比賽期間即時紀錄算證據 |

## 10. 待人決定（每項最多兩個選項；硬上限數值留空）

| 項 | 問題 | 選項 | 推薦與理由 |
| --- | --- | --- | --- |
| 1 | 風險 Agent 的權限 | (a) 從 N 組挑選、排先後、決定份量格，**可以升級**到人設上限；只能用交易 Agent 已採納的 claim 與程式事實 (b) 沿用 v3：只能同意／降一格／否決，不能升級 | **(a)**。使用者原話「挑其中 3 組、決定各買幾 %」指向挑選與決定份量，不只是否決。代價：風險 Agent 的邊界擴大到比較個股論點強弱，與現行「不做個股判斷」衝突，須在 AGENTS.md 明文改寫 |
| 2 | 份量的表達方式 | (A) 列舉份量格、程式對數字 (B) 風險 Agent 直接寫百分比（破例） | **A**。見第 5 節：B 破壞「數值追溯到工具與證據」「Agent 不輸出數字」與 Validator 的實質檢查 |
| 3 | 絕對硬上限數值 | 一天最多成交幾組 `max_pairs_per_run`：【待人填】；交易 Agent 最多提名幾組 `max_nominations`：【待人填】；單檔換入權重上限 `max_entry_weight`：【待人填】；purpose 別是否共用同一上限：(a) 共用 (b) quality_dip 沿用 v3 自己的上限 | 數值不代填。事實供參考：換手 30% 時，5% 的換股一天最多約 3 組；買進還受換出者賣款限制。purpose 別推薦 (b)，與 v3 相容 |
| 4 | 份量格對應比例 | (a) 沿用 v3 的程式常數（large＝上限、medium 2/3、small 1/3） (b) 由人填三個比例 | **(a)**。與 v3 一致、少一組人填數字 |
| 5 | 賣款不夠時 | (a) 買進縮到本組賣款（加分到的格點以上現金）為止，不足一張整組取消 (b) 允許每組附 `funding_symbols` 額外減碼或出場補款（由風險 Agent 一併挑選；數量上限【待人填】） | **(a) 先做**。簡單、保證明確；(b) 會讓檔數變動、驗證面變大。11-11 的事實顯示 (a) 下要買到 5%，換出者本身要有約 5% 權重（當天只有 9 檔 ≥ 4%） |
| 6 | 交易 Agent 的簡報要不要顯示「賣款約可支應的份量格」 | (a) 顯示（列舉，不給百分比） (b) 不顯示 | **(a)**。否則交易 Agent 會對 0.7% 的換出者提 `large`，到配置才被縮 |
| 7 | `team_inputs` 版本 | (a) 2.11＝2.10 超集，多必要鍵 `swap_ruling`，開啟時 `quality_dip_ruling` 標 `superseded` (b) 2.11＝2.9 加 `swap_ruling`，與 2.10 互斥（版本表不再是單一直線） | **(a)**。維持版本表的單一直線，舊 run 重建規則簡單 |
| 8 | 裁決失敗時 | (a) 本次全部換股取消、換出者不賣，其餘照常 (b) thesis／exposure 配對退回現行 v2 程式份量照做，只取消 quality_dip | **(a)**。開了閘門就表示份量要由 Agent 決定；失敗時退回程式份量等於程式替 Agent 決定，也會重現 11-11 的零頭問題 |
| 9 | 上線方式 | (a) 直接正式（先存回滾樣板） (b) 先影子（只封存、配置不讀） | 依時程推薦 **(a)**：比賽剩約 9 個交易日，影子期幾乎沒有樣本；但 (b) 較安全，若人重視風險可選 (b) |
| 10 | 一般加碼的籌錢（不換名額的「賣 A 加碼 B」） | (a) 本計畫不做，只處理換股 (b) 一併納入配置裁決 | **(a)**。先把換股做對；加碼籌錢牽涉信心預算整體，範圍大 |

以上全部是計畫。**沒有任何程式、設定或 Skill 被修改；本文件描述的閘門未實作、未開啟。**
