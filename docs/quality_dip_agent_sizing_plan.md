# 優質股換股：組數與份量由交易 Agent 提出、風險 Agent 裁決（`agent_swap.quality_dip` v3 計畫）

狀態：**已實作、閘門預設不存在＝關閉、尚未開啟**（2026-11-11 第三版：使用者已決定第 2～10 項，程式與測試已完成；第 1 項硬上限數值仍待人填，`config/decision_policy.json` 沒有改，現行 v2 設定逐位元不變）。把本文件的行為當成現行行為是錯的。實作紀錄與照計畫做不到、自行取捨之處見[第 9 節](#9-實作紀錄2026-11-11第三版)。

第一版錯在哪裡（留作紀錄）：擅自把上限寫成「最多 3 組、單檔 5%」、要人填三格 `size_grid` 數字，而且只有交易 Agent 一方決定。使用者的原意是：**買幾組、每組買多重都由 Agent 判斷，先由交易 Agent 決定，再交給風險 Agent 決定；人只設不可跨越的硬上限。** 本版照此重寫，所有上限數值留空（`【待人填】`），由人決定。第三版起使用者已對第 2～10 項表態（第 7 項選 B，其餘照推薦 A），見第 8 節。

前置閱讀：[`quality_dip_plan.md`](quality_dip_plan.md) 第 10.4 節（v2 現況）、[`agent_nominated_rotation_plan.md`](agent_nominated_rotation_plan.md)、[`agent_led_exposure_plan.md`](agent_led_exposure_plan.md)。

## 1. 為什麼改

### 1.1 事實（2026-11-11 08:13 run，`artifacts/decisions/2026-11-11/decision-20261111T001308Z`，唯讀讀取封存檔）

- Policy：`agent_swap` v2、`max_swaps_per_run 2`；`quality_dip` v2、`max_swaps_per_run 1`、`max_entry_weight 0.03`、`min_new_conviction medium`、`min_turn_assessment turn_unconfirmed`。
- 持股 30／30，沒有空名額。
- 交易 Agent 標 `market_driven` 共 20 檔：`add`／high 1、`buy`／high 1、`buy`／medium 15、`buy`／low 3。另有 4 檔 `oversold_cause_unclear`。`hold`／low 持股 9 檔。
- 換股提名 2 組，都是 `purpose: thesis`（買 2408 賣 2890、買 6239 賣 3034），0 組 `quality_dip`。兩組都成立，換入者旗標為 `AGENT_SWAP_PROCEEDS_TO_ADD`。
- 風險 Agent：`defensive`／`lower`／`diversified`、`turn_assessment: turn_unconfirmed`、`exposure_view: keep_defensive`。回撤下限 `stance_floor: neutral`（回撤 4.70%），當天有 `RISK_DRAWDOWN`。
- 現行程式（`allocation._agent_swap`）：`keep_defensive` 只取消 `purpose: exposure` 的配對，**不影響 quality_dip**；quality_dip 只看 `turn_assessment` 是否達 `min_turn_assessment`、回撤下限是否為 defensive。當天這兩項都通過。
- 現行簡報不給交易 Agent 任何上限數字，交易 Agent 也沒有欄位可以表達「這組買多一點或少一點」。買進權重由程式依信心算出後被 `max_entry_weight` 蓋住。
- quality_dip 的組數受 `agent_swap.max_swaps_per_run`（2）封頂；當天 2 組 thesis 已用完總額度。**（v3 起已決定改為另計，見第 8 節第 7 項。）**

### 1.2 推論（未驗證，不可當成事實）

- 當天就算交易 Agent 多提名 quality_dip 配對並排在清單後面，也會因總上限 2 組被 `swap_cap` 取消（v2 的行為；v3 的 quality_dip 組數另計、不占總上限）。
- 交易 Agent 沒提名 quality_dip 的原因無法由封存檔得知。可能原因：quality_dip 每一條都比 thesis 嚴（第 10.4 節第 2 項已載明它不會多出名額來源），同一對用 thesis 提名比較容易通過。
- 16 檔 medium 以上的 `market_driven` 買進中，有幾檔在提名簡報標為 `quality_dip_nominable`，**本次沒有重算**。
- 所以本計畫**不保證**會出現 quality_dip 配對。它只做三件事：讓兩個 Agent 有欄位表達組數與份量、讓「不提名」留下原因、把人的角色縮成設硬上限。

### 1.3 現況與使用者原意的差距

| 項目 | 現況（v2） | 使用者要的 |
| --- | --- | --- |
| 買幾組 | 人設的 `max_swaps_per_run` 實際上就是答案（1） | Agent 判斷；人只設上限 |
| 每組買多重 | 程式依信心計算，再被人設的 3% 蓋住 | Agent 判斷；人只設上限 |
| 風險 Agent 的角色 | 只有 `turn_assessment` 間接當門檻，沒有對 quality_dip 表態 | 交易 Agent 先決定，再由風險 Agent 裁決 |

## 2. 責任劃分

| 誰 | 決定什麼 | 形式 |
| --- | --- | --- |
| 人 | 絕對硬上限：最多幾組、單檔買進權重上限（超跌另一組）；既有門檻 `min_new_conviction`、`min_holding_days`、`min_turn_assessment`；閘門開不開 | Policy 數字，全部必填、程式不給預設 |
| 交易 Agent（換股提名步驟） | 提名哪幾組（組數＝清單裡 quality_dip 配對的數目）、先後順序、每組的份量格；不提名時說明原因 | 列舉、代號、識別碼、文字 |
| 風險 Agent（新增的裁決步驟） | 對交易 Agent 提出的每一組做最後裁決：同意、降一格、否決 | 列舉、識別碼、文字 |
| 程式 | 把份量格對成權重上限；套硬上限與後段所有限制；縮到不足一張就取消該對；記錄旗標；整份重算驗證 | 不挑人、不排序、不補提名、不升級 |

Agent 不輸出任何數字。組數是清單長度，不是 Agent 寫出來的數字。**本計畫不需要對「Agent 不輸出數字」開例外。**

需要人明確同意的一個例外是另一條規則：現行「風險 Agent 的簡報不附交易 Agent 的意圖與信心統計」。本計畫的裁決步驟要讓風險 Agent 看到交易 Agent 提出的 quality_dip 配對，否則無法「交給風險 Agent 決定」。做法是把它放在**另一次獨立呼叫**，現金姿態那次的簡報完全不變（見第 3.3 節與第 8 節第 2 項）。

## 3. 兩段式流程

```
交易 Agent 第一輪（逐檔意圖、信心、dip_assessment）
        │
入選排序（第二輪）
        │
換股提名（第二輪，交易 Agent）        ← 第一段：提名 quality_dip 配對＋每組 size
        │                                 沒提名時填 quality_dip_decline
風險 Agent 現金姿態（簡報不變）        ← level／cash_grade／turn_assessment／exposure_view
        │
程式前置檢查（回撤下限、轉向門檻、提名狀態）
        │  不通過 → 不呼叫裁決，全部 quality_dip 配對取消
風險 Agent 裁決（新步驟）              ← 第二段：每組 approve／downsize／reject
        │
程式配置：份量格 → 權重上限 → 硬上限 → 後段限制 → 不足一張取消該對
        │
Finalizer 整份重算、Guard
```

### 3.1 第一段：交易 Agent 提名

沿用換股提名的有序配對清單，v3 多兩樣：

- 每個 `purpose: quality_dip` 配對必填列舉 `size`：`small`（試單）／`medium`／`large`（把握最高）。其他 `purpose` 出現 `size` 即拒絕。
- 清單外的頂層欄位 `quality_dip_decline`：只有在「簡報顯示至少一檔可提名的換入者與一檔可提名的換出者，而清單裡沒有任何 quality_dip 配對」時必填，內容是列舉 `reason_kind`（`no_suitable_victim`／`prefer_thesis_pairs`／`market_not_ready`／`candidates_not_convincing`）加一句文字。有 quality_dip 配對時不得出現。純紀錄，程式不依它做任何事。

不另設「組數意見」列舉：組數就是清單長度，再加一個列舉只會多一個可能互相矛盾的欄位。

簡報多給交易 Agent 的東西：三個份量格的名稱與意思、「最多可提名幾組」（是否顯示見第 8 節第 8 項）。**不給百分比**，交易 Agent 也不得輸出。`size` 與信心、`dip_assessment` 不設對應，程式不驗。

5c 的結構檢查全部沿用 v2（換入者與換出者資格、非價格多頭依據、`dip_assessment` 相符）。

### 3.2 程式前置檢查（裁決之前，沿用 v2 的硬性界線）

任一成立就不呼叫裁決步驟，全部 quality_dip 配對取消、換出者不賣：

- 提名狀態為 `exhausted`／`unavailable`。
- 回撤下限 `stance_floor` 為 defensive，或帳本回撤歷史不可用。
- 風險 Agent 的 `turn_assessment` 低於人設的 `min_turn_assessment`。
- 清單裡沒有 quality_dip 配對（裁決狀態記 `not_required`）。

這些是人設的界線，不是 Agent 的判斷，本計畫不改。

### 3.3 第二段：風險 Agent 裁決

新增一次風險 Agent 呼叫，排在現金姿態之後、配置之前。

**簡報內容**（只有這些）：

- 每一組 quality_dip 配對：換入與換出代號、交易 Agent 的 `size` 與理由、兩邊依據識別碼對應的論點文字、換入者的被拖累背景（候選標示、價格型態）、兩檔的 beta 與產業、換出者目前權重。
- 風險 Agent 自己本次的現金姿態輸出（`level`、`cash_grade`、`turn_assessment`、`exposure_view`）。
- 轉向事實、加權指數事實、組合曝險與產業權重事實（背景、不可引用）。
- **不附**：其他股票的意圖與信心統計、thesis 與 exposure 配對、任何上限百分比。

**輸出**：

| 欄位 | 內容 |
| --- | --- |
| `rulings` | 與交易 Agent 的 quality_dip 配對一一對應、順序相同；每項 `ruling` 列舉 `approve`（照交易 Agent 的份量格）／`downsize`（份量降一格）／`reject`（否決這一組） |
| 每項 `reason_kind` | `downsize`／`reject` 必填列舉：`market_risk`／`portfolio_concentration`／`timing` |
| 每項 `reason` | 文字，必填 |
| `evidence_ids` | 整份裁決至少一個市場層級證據（與現金姿態相同的要求） |
| `defensive_reconciliation` | 文字；本次 `level` 為 defensive 或 `exposure_view` 為 `keep_defensive`，而且有任何一組不是 `reject` 時必填，說明為什麼在防守看法下仍放行 |

**風險 Agent 能做與不能做的事**：

| 能 | 不能 |
| --- | --- |
| 同意、把份量降一格、否決某一組 | 把份量升級（列舉裡沒有這個選項） |
| 全部否決（等於今天不做 quality_dip） | 新增配對、換掉換入者或換出者、改順序 |
| 依市場與組合風險裁決 | 新增個股事實、重判個股論點的真假 |
|  | 手寫權重或張數、改現金比例、覆寫 CompetitionGuard |

**驗證規則**（Validator，結構檢查，不判斷理由好壞）：

- `rulings` 必須剛好涵蓋每一組 quality_dip 配對各一次、順序相同；多一組、少一組、代號不符都拒絕。
- `small` 的配對不能 `downsize`（只能 `approve` 或 `reject`）。
- `downsize`／`reject` 缺 `reason_kind`、任一項缺 `reason`，拒絕。
- `evidence_ids` 沒有市場層級證據、引用不存在的識別碼，拒絕。
- 該填 `defensive_reconciliation` 而沒填，或不該填而填，拒絕。
- 文字出現前次 run 識別碼、背景區塊鍵名或 facts_id，拒絕。
- 輸出含任何數字欄位或白名單外欄位，拒絕。
- 裁決綁定本次提名與現金姿態的識別碼與雜湊；不一致拒絕。

**失敗降級（fail-closed）**：

| 情況 | 處理 |
| --- | --- |
| 驗證失敗 | 帶錯誤回饋的修正循環，最多三次 |
| 修正用盡 | 裁決封存為 `exhausted`；全部 quality_dip 配對取消（`AGENT_SWAP_CANCELLED:quality_dip_ruling_exhausted`）、換出者不賣；thesis、exposure 配對與其餘決策照常 |
| CLI 逾時、非零結束、輸出不是 JSON | 交件期限內再試一次並記入 `agent_calls`；仍失敗封存為 `unavailable`，處理同上（`…:quality_dip_ruling_unavailable`） |
| 安全拒絕 | 不重試；比照換股提名步驟現行的處理（實作時查明並寫進契約） |
| 硬性規則失敗 | 照舊整份拒絕 |

沒有裁決就沒有 quality_dip 換股。任何失敗都**不會**退回 v2「不經裁決直接成交」的行為。

### 3.4 與風險 Agent 既有輸出的關係

| 既有輸出 | 它回答的問題 | 與裁決的關係 |
| --- | --- | --- |
| `level`／`cash_grade` | 留多少現金 | 裁決不改現金。quality_dip 買進金額不超過該對賣款，本來就不動用原有現金 |
| `turn_assessment` | 市場有沒有轉向 | 仍是人設門檻 `min_turn_assessment` 的輸入，在裁決之前由程式檢查，行為同 v2 |
| `exposure_view` | 組合要不要以**曝險理由**讓低 beta 持股讓位 | 仍只管 `purpose: exposure` 配對，不變。它**不**自動決定 quality_dip |
| 裁決 `rulings`（新） | 這幾組「個股被大盤拖累」的換股，今天各放行多少 | 只管 `purpose: quality_dip` 配對 |

防守看法怎麼影響 quality_dip：

- **不寫死對映**。程式不會因為 `keep_defensive` 或 `defensive` 就自動取消 quality_dip。這兩個欄位回答的是組合層級的問題；quality_dip 是個股層級、買賣金額對等、份量有上限的換股，兩者答案可以不同。寫死對映等於程式又替 Agent 判斷一次，也會與 `exposure_view` 重複。
- **要求交代**。風險 Agent 若在防守看法下仍放行任何一組，必須填 `defensive_reconciliation`。程式只驗有沒有填。
- **11/11 的情境**：`turn_unconfirmed` 通過人設門檻，所以裁決步驟會被呼叫（前提是交易 Agent 有提名）。風險 Agent 當天是 `defensive`＋`keep_defensive`，它可以全部 `reject`，也可以放行並寫明理由。它會怎麼選，無法由封存檔得知，本文件不推測。
- 人若希望「`keep_defensive` 時一律不做」，那是硬上限的一種，見第 8 節第 6 項。

### 3.5 程式配置

1. 有效份量格＝交易 Agent 的 `size`，`downsize` 則降一格；`reject` 的配對取消（`AGENT_SWAP_CANCELLED:quality_dip_ruling`）、換出者不賣。
2. 依清單順序取用，超過 quality_dip 自己的組數上限的從尾端取消（`quality_dip_cap`）。超跌配對另受 `oversold` 上限。**quality_dip 組數另計（已決定第 7 項 B）：不占用 `agent_swap.max_swaps_per_run`；總上限只約束其餘（thesis／exposure）配對**，超過的仍從清單尾端取消（`swap_cap`）。
3. 份量格對成買進權重上限：`large`＝人設的 `max_entry_weight`，`medium`、`small` 為它的固定比例（比例是版本內的程式常數還是由人填，見第 8 節第 5 項）。
4. 買進股數＝下列取最小，只會縮減：程式依信心算出的股數、份量格的權重上限、該對換出者賣款淨額（含費）。
5. 之後照舊受現金下限、換手上限、檔數、單股與產業上限、Active Share 限制。
6. 縮到不足一張：該對取消、換出者不賣。不會只賣不買。
7. 換出者賣單由程式標 `exit`＋`AGENT_SWAP`，旗標說明寫明交易 Agent 的份量格、風險 Agent 的裁決與最後套用的上限。交易 Agent 的意圖不改寫。

## 4. 契約與驗證變更

### 4.1 閘門版本：`agent_swap.quality_dip` v3

取捨：

| 做法 | 好處 | 代價 |
| --- | --- | --- |
| v3（建議） | 硬上限只有一個出處；`max_swaps_per_run`、`max_entry_weight` 的意義由「實際套用值」改為「絕對上限」，用版本切換最清楚 | `quality_dip_swap.py` 多一個版本分支 |
| 新閘門（例如 `agent_swap.quality_dip_ruling`） | 可以單獨開關裁決 | 兩個欄位互相依賴、上限分散兩處；`size` 屬於提名 Schema，仍要動 quality_dip |

v3 的形狀（數字全部由人填，程式不給預設）：

```json
"quality_dip": {
  "version": "v3",
  "max_swaps_per_run": 【待人填】,
  "max_entry_weight": "【待人填】",
  "min_new_conviction": "【沿用或待人填】",
  "min_holding_days": 【沿用或待人填】,
  "min_turn_assessment": "【沿用或待人填】",
  "oversold": {"max_swaps_per_run": 【待人填】, "max_entry_weight": "【待人填】"}
}
```

- `oversold` 仍是選填；沒寫＝超跌候選不走這條換股。
- Policy 驗證與 v2 的差異：v3 的組數另計（已決定第 7 項 B），所以 `max_swaps_per_run` **不再受 `agent_swap.max_swaps_per_run` 限制**，改為 0～10 的結構性合理範圍（實作常數 `QUALITY_DIP_V3_MAX_SWAPS`，人填的數字必須落在其中）；`max_entry_weight` 仍在 (0,1)、`oversold` 不得高於上一層。v3 另需 `agent_swap` v2。`nominated_pairs` 的對數檢查改為「扣除 quality_dip 後不超過 `agent_swap.max_swaps_per_run`、quality_dip 配對不超過 `quality_dip.max_swaps_per_run`」。
- pipeline 綁定欄位（人不填）：`turn_assessment`（同 v2）、`ruling_ref`（裁決識別碼、雜湊、狀態）、`rulings`（與 quality_dip 配對同序的裁決列舉）；`nominated_pairs` 的 quality_dip 配對多 `size`。
- 前提不變：`agent_swap`、`agent_swap.exposure`、`quality_dip_facts`、`quality_dip_assessment`、`cash_stance_turn_facts`。
- `agent_swap` 前綴已在策略反思禁區，不必改 `FORBIDDEN_PREFIXES`。

### 4.2 新欄位與列舉

| 位置 | 欄位 | 值 |
| --- | --- | --- |
| 換股提名（v3） | 配對的 `size` | `small`／`medium`／`large` |
| 換股提名（v3） | `quality_dip_decline.reason_kind` | `no_suitable_victim`／`prefer_thesis_pairs`／`market_not_ready`／`candidates_not_convincing` |
| 裁決 | `rulings[].ruling` | `approve`／`downsize`／`reject` |
| 裁決 | `rulings[].reason_kind` | `market_risk`／`portfolio_concentration`／`timing` |
| 裁決封存 | `status` | `completed`／`not_required`／`skipped_by_program`／`exhausted`／`unavailable` |
| 旗標 | 取消原因 | `quality_dip_ruling`、`quality_dip_ruling_exhausted`、`quality_dip_ruling_unavailable`（既有的 `quality_dip_cap`、`quality_dip_turn`、`quality_dip_stance_floor`、`quality_dip_entry` 等不變） |

Agent 輸出 Schema 各出新版（提名一份、裁決一份），舊 Schema 不動。

### 4.3 `team_inputs` 2.10

- 2.10 ＝ 2.9 加必要鍵 `quality_dip_ruling`（裁決封存，含狀態、綁定的提名與現金姿態識別碼與雜湊、`rulings`、`evidence_ids`、內容雜湊）。只有 Policy 為 quality_dip v3 時使用 2.10；其餘情況照舊是 2.9。
- 用升版而不是選配鍵的理由：裁決是 Agent 輸出、會改變配置，與 `swap_nomination`（2.9）性質相同；選配鍵目前只用在程式算的事實。
- Decision run 必須另存；Finalizer 與所有下游重建都要傳入，缺少即拒絕。版本史要補進 `docs/architecture.md`。

### 4.4 Finalizer 與下游重建

- Finalizer 以同一個 Validator 重驗裁決（涵蓋、順序、證據、綁定雜湊），並由 `team_inputs` 的提名與裁決**重算** Policy 綁定的 `size`、`rulings`、`ruling_ref`，逐欄比對；不一致拒絕。
- 裁決狀態不是 `completed` 而配置出現 quality_dip 成交，拒絕。
- 配置結果（股數、旗標）照舊由下游以 Policy 與 bundle 整份重算。
- 回放：隨 `agent_swap` 一起略過，notes 標 `agent_swap.quality_dip: ignored_in_replay`；證據等級 `replay_reference_only`。

### 4.5 舊版相容

- 沒有 quality_dip、或 v1／v2 的 Policy：不呼叫裁決步驟、提名不得有 `size` 與 `quality_dip_decline`、`team_inputs` 仍是 2.9、簡報逐鍵相同、封存 run 重建雜湊逐位元不變。
- v3 的提名缺 `size` 拒絕；v1／v2 的提名帶 `size` 拒絕。
- v2 的程式分支原樣保留，供舊 Policy 與舊 run 重建。

## 5. 不變的邊界

- Agent 不輸出數字；`size` 與 `ruling` 都是列舉。
- 風險 Agent 不得手寫權重、不得覆寫 CompetitionGuard；風險理由出現前次 run 識別碼即拒絕。
- 現金姿態那次的簡報不變，仍不附交易 Agent 的意圖與信心統計。
- 程式不改寫交易 Agent 的意圖，只在旗標留原因。
- 一換一：縮到不足一張取消該對、換出者不賣，不會只賣不買。
- 買進金額（含費）不超過該對換出者賣款淨額。
- 現金下限、換手上限、檔數、單股與產業上限、Active Share、再買冷卻、持有天數下限一律不放寬。
- 回撤強制防守時整個略過；`min_turn_assessment` 門檻照舊；減碼日的處理照 `derisk_day_view` 現行設定。
- `exposure_view` 仍只管 exposure 配對；thesis 配對不受本計畫影響。
- 背景不是證據：轉向、加權指數、組合曝險、被拖累背景都不可引用。
- 版本閘門只由人設定、策略反思不得改。
- 程式不補提名、不替 Agent 排序、不升級份量。

## 6. 實作順序、測試、文件、回滾

### 6.1 實作順序

1. `decision/quality_dip_swap.py`：v3 版本常數、閘門驗證、`size` 列舉、綁定（`size`、`rulings`、`ruling_ref`）。
2. `decision/swap_nomination.py`：v3 的 `size` 與 `quality_dip_decline` 檢查、簡報區塊、封存欄位。
3. 新檔 `decision/quality_dip_ruling.py`：裁決契約、簡報建構、Validator、封存。
4. `automation/risk_stages.py`、`agent_schemas.py`、`daily_pipeline.py`：前置檢查、裁決呼叫、修正循環、降級、`agent_calls` 紀錄。
5. `decision/trader.py`（`apply_trade_decision` 綁定）、`decision/finalization.py`（2.10、重算比對）。
6. `decision/allocation.py`：只改 `_agent_swap` 與 `_quality_dip_entry` 的 v3 分支（只讀需要的行段）。
7. Skill、文件、正式樣板回歸測試。
8. 實作時查明：安全拒絕在第二輪步驟的現行處理；quality_dip 配對與 `proceeds_to_add` 的互動（建議 quality_dip 配對不適用，沒有買單就取消）。

### 6.2 測試清單（`unittest.TestCase` 風格）

舊版相容：

- 沒有閘門、v1、v2 的 Policy：簡報、Schema、`team_inputs` 版本、配置結果與封存 run 重建雜湊逐位元不變。
- v1／v2 提名帶 `size` 或 `quality_dip_decline` 拒絕。

閘門：

- v3 缺任一必填數字、組數上限高於 `agent_swap.max_swaps_per_run`、`oversold` 高於上一層、`agent_swap` 不是 v2，拒絕。
- 人手填 `rulings`／`ruling_ref`（只能由 pipeline 綁定）拒絕。

第一段（提名）：

- quality_dip 配對缺 `size`、值域外、thesis／exposure 配對帶 `size`，拒絕。
- 有可提名對象而零配對時缺 `quality_dip_decline` 拒絕；有配對卻帶它拒絕；沒有可提名對象時帶它拒絕。
- 輸出含數字欄位拒絕。

第二段（裁決）：

- `rulings` 少一組、多一組、順序不同、代號不符，拒絕。
- `small` 配 `downsize` 拒絕。
- `downsize`／`reject` 缺 `reason_kind`、缺 `reason`，拒絕。
- 沒有市場層級證據、引用不存在的識別碼，拒絕。
- 防守看法下放行而缺 `defensive_reconciliation` 拒絕；全部 `reject` 時帶它拒絕。
- 文字含前次 run 識別碼、背景鍵名或 facts_id 拒絕。
- 裁決綁定的提名或現金姿態雜湊不符拒絕。
- 裁決簡報不含其他股票的意圖與信心統計；現金姿態簡報與 v2 逐鍵相同。

降級（fail-closed）：

- 修正用盡、暫時性錯誤重試仍失敗：quality_dip 配對全部取消、換出者不賣、thesis 配對與其餘訂單照常。
- 前置檢查不過（回撤下限防守、帳本歷史不可用、轉向不足、提名失敗）：不呼叫裁決、全部取消。
- 沒有 quality_dip 配對：狀態 `not_required`，不呼叫。
- 裁決不是 `completed` 而 Policy 綁了放行的 `rulings`，Finalizer 拒絕。

配置：

- `approve` 用交易 Agent 的份量格；`downsize` 降一格；`reject` 取消且換出者不賣。
- 三個份量格對應的權重上限都不高於 `max_entry_weight`；超跌用 `oversold` 的上限。
- 組數超過上限從清單尾端取消。
- 買進金額不超過賣款（逐筆比對 `net_cash_impact`）；不突破換手與現金下限。
- 縮到不足一張取消該對、換出者不賣。
- 交易 Agent 的意圖未被改寫；旗標說明含份量格與裁決。
- 風險 Agent 無法使成交量高於交易 Agent 的提名（同一份輸入下，任何裁決組合的買進股數都不高於全部 `approve`）。

重建：

- `team_inputs` 2.10 缺 `quality_dip_ruling` 拒絕；2.9 帶它拒絕。
- 竄改 `size`、`rulings`、`ruling_ref` 任一欄，Finalizer 拒絕。

修改後照 `AGENTS.md` 執行完整 pytest、`compileall` 與 `git diff --check`。

### 6.3 要同步更新的文件與 Skill

- `AGENTS.md`（風險 Agent 邊界、`team_inputs` 版本、閘門清單）、`README.md`。
- `docs/status.md`、`docs/decision-contracts.md`、`docs/architecture.md`、`docs/glossary.md`、`docs/README.md`、`docs/runtime-artifacts.md`、`docs/operations.md`（多一次 Agent 呼叫的時間）。
- `docs/quality_dip_plan.md`（第 10.4 節後補一節指向本文件）、`docs/agent_nominated_rotation_plan.md`、`docs/agent_led_exposure_plan.md`（說明 `exposure_view` 不管 quality_dip）。
- Skill：`tradeagent/trading/trader/references/swap-nomination.md`（`size` 與 `quality_dip_decline`）、`tradeagent/risk/portfolio-risk-review/`（新增裁決的 reference，並確認 `agents/openai.yaml` 描述與邊界一致）。

### 6.4 回滾

- 開啟前先存樣板：`config/policy_versions/【開啟日期】-before-quality-dip-v3.json`，並改 `policy_id` 與真實 `available_at`。
- 回滾＝把 `position_sizing.agent_swap.quality_dip` 換回樣板的區塊（v2）。`team_inputs` 隨之回到 2.9。

## 7. 風險與替代方案

### 7.1 風險

| 風險 | 說明 | 擋法 |
| --- | --- | --- |
| 仍然沒有人提名 | quality_dip 比 thesis 嚴的結構問題還在（第 10.4 節第 2 項） | `quality_dip_decline` 留下原因，一兩個決策日後由人判斷要不要處理（第 8 節第 10 項） |
| 交易 Agent 每次都選 `large` | 列舉變成變相的最大值 | `large` 就是人設的上限；風險 Agent 可降級；買進不超過賣款；紀錄選擇分布 |
| 風險 Agent 每次都全部否決 | 防守看法下最省事的答案 | 程式擋不住，也不該擋；紀錄裁決分布與 `reason_kind`，由人檢視 |
| 風險 Agent 被交易 Agent 的理由帶著走 | 看到提名後失去獨立性 | 現金姿態在看到提名之前已定案；裁決只能縮不能放；簡報只附 quality_dip 配對 |
| 多一次 Agent 呼叫 | 時間與失敗點增加 | 只有存在 quality_dip 配對且前置檢查通過才呼叫；失敗只取消 quality_dip |
| 沒有前瞻證據 | 舊 run 沒有這些輸出，回放只是 `replay_reference_only` | 只有比賽期間的即時紀錄算證據 |

### 7.2 風險 Agent 的權限：兩種做法比較

| 項目 | A：只能同意、降級、否決（建議） | B：風險 Agent 完全決定 |
| --- | --- | --- |
| 內容 | 交易 Agent 提出，風險 Agent 只能往小的方向改 | 風險 Agent 可以升級份量、改組數、改順序，甚至自己加配對 |
| 與現行邊界 | 相容：風險 Agent 不新增事實、不做個股判斷 | 衝突：風險 Agent 要讀全部個股論點，等於第二個交易 Agent |
| 簡報 | 只附 quality_dip 配對 | 要附全部買進候選與持股，推翻「不附意圖與信心統計」 |
| 最壞情況 | 成交量不高於交易 Agent 的提名，也不高於人設上限 | 成交量可高於交易 Agent 的提名（仍不高於人設上限） |
| 稽核 | 每組有兩個可追溯的決定 | 交易 Agent 的份量意見可被整個覆蓋，責任不清 |
| 符合使用者原話的程度 | 「交易 Agent 先決定，再交給風險 Agent 決定」：兩者都有決定權 | 交易 Agent 的決定只剩參考 |

建議 A。理由：兩個 Agent 的決定都保留且方向單一，風險 Agent 的角色仍是「風險」；B 需要改動風險 Agent 的根本邊界，比賽剩餘時間內不宜動。

### 7.3 其他沒採用的做法

- **風險 Agent 在現金姿態那次多填一個組合層級列舉**（不看提名）：少一次呼叫、不需要簡報例外，但風險 Agent 是盲選，不算「交給風險 Agent 決定」。列為第 8 節第 2 項的另一選項。
- **程式依 `exposure_view` 自動決定 quality_dip**：等於程式又替 Agent 判斷，且與 `exposure_view` 的職責重複。
- **另加「quality_dip 合計買進權重上限」**：合計已受組數上限乘單檔上限、換手與賣款限制，不另開欄位。

## 8. 待人決定與已決定（2026-11-11 使用者回覆）

第 2～10 項已決定：第 7 項選 **B**，其餘全部照下面標註的推薦（A）；第 9 項直接正式、不跑影子。**只有第 1 項（硬上限數值）使用者尚未給，仍待人填**，本文件與程式都不代填、`config/decision_policy.json` 沒有改。

| 項 | 決定 | 內容 |
| --- | --- | --- |
| 1 | **待人填** | 絕對硬上限：quality_dip 最多幾組（`max_swaps_per_run`）、單檔買進權重上限（`max_entry_weight`）、是否開 `oversold` 與它的兩個上限。事實供參考：硬上限若填現行 v2 的值，v3 的成交量恆不高於 v2。測試用的數值只在測試內，沒有寫進 config |
| 2 | 已決定 A | 風險 Agent 以獨立的裁決步驟決定，看得到 quality_dip 配對；對「風險 Agent 簡報不附交易 Agent 意圖」開這一個窄例外（現金姿態那次的簡報不變） |
| 3 | 已決定 A | 風險 Agent 只能同意、降一格、否決，不能升級、不能改組數 |
| 4 | 已決定 A | 版本用 `agent_swap.quality_dip` v3（硬上限單一出處） |
| 5 | 已決定 A | 份量格對成權重：版本內的程式常數（`large`＝硬上限、`medium`＝2/3、`small`＝1/3） |
| 6 | 已決定 A | 單一硬上限，不依 `exposure_view` 分級；風險 Agent 以裁決把關並填 `defensive_reconciliation` |
| 7 | 已決定 **B** | **quality_dip 組數另計，不占用 `agent_swap.max_swaps_per_run`**：quality_dip 有自己的 `max_swaps_per_run` 名額；thesis／exposure 配對照舊用總上限 |
| 8 | 已決定 A | 簡報顯示 quality_dip「最多可提名幾組」（`max_quality_dip_pairs`），不顯示任何權重百分比 |
| 9 | 已決定 A | 直接正式，不跑影子（v3 沒有影子版本；開啟前須先存回滾樣板） |
| 10 | 已決定 A | quality_dip 比 thesis 嚴的問題本次不動，先看 `quality_dip_decline` 的即時紀錄再決定 |

第 7 項選 B 讓計畫原文（以 A 為前提）的幾處必須分支，已在本文件與程式改為 v3 專屬：第 1.2 節、第 3.5 節第 2 項、第 4.1 節 Policy 驗證（見上）；程式端的對應改動見第 9 節。

以上全部是設計紀錄。**開啟閘門、填第 1 項數值仍需要人另行決定。**

## 9. 實作紀錄（2026-11-11，第三版）

狀態：已實作、閘門預設關閉。程式：`src/etf_agent/decision/quality_dip_swap.py`（v3 版本常數、閘門驗證、份量格與裁決列舉、`program_skip_reason`、綁定）、`swap_nomination.py`（`size`、`quality_dip_decline`、v3 的對數與事實）、新檔 `quality_dip_ruling.py`（裁決契約、簡報、Validator、封存、Policy 綁定、team_inputs 重建）、`allocation.py`（`_agent_swap`：v3 分支）、`finalization.py`（`team_inputs` 2.10、Finalizer 重算）、`sizing.py`（`nominated_pairs` 的 `size` 與對數檢查）、`automation/agent_schemas.py`、`team_stages.py`、`risk_stages.py`（`run_quality_dip_ruling`）、`daily_pipeline.py`（6b 步驟）。測試：`tests/decision/test_quality_dip_sizing.py`、`tests/automation/test_quality_dip_ruling_pipeline.py`。Skill：`tradeagent/trading/trader/references/swap-nomination.md`（v3 段）、`tradeagent/risk/portfolio-risk-review/references/quality-dip-ruling.md`（新）與 `SKILL.md`。

**契約要點**

- 閘門形狀與第 4.1 節相同；pipeline 綁定的 `ruling_ref`（`ruling_id`、`sha256`、`status`）與 `rulings`（與提名中 quality_dip 配對同序，只有 `completed` 才有值）人不得預填（`apply_swap_nomination` 與 `apply_quality_dip_ruling` 先清再綁）；`nominated_pairs` 的 quality_dip 配對另綁 `size`。
- 提名的 Schema 用 `SWAP_NOMINATION_QUALITY_DIP_SIZED_SCHEMA`：Codex strict schema 要求欄位一律輸出，所以非 quality_dip 配對的 `size` 填空字串、`quality_dip_decline` 不適用時 `reason_kind` 填 `not_applicable` 且 `reason` 填空字串，封存時還原成缺席（`approve` 的 `reason_kind` 填 `none`、`defensive_reconciliation` 填空字串，同理）。因此 Validator 的「不該填而填」指的是填了非空值。
- 前置檢查是純函式 `expected_program_status`：pipeline 用它決定要不要呼叫 Agent，Finalizer 用它重算狀態並拒絕任何不一致（做法同 `needs_nomination`↔`not_needed`）：提名沒有 quality_dip 配對＝`not_required`；回撤下限為 defensive、帳本歷史不可用、`turn_assessment` 低於 `min_turn_assessment`＝`skipped_by_program`（`errors` 恰為原因代碼）；其餘才呼叫 Agent。
- 裁決綁定提名（`nomination_id`、`nomination_sha256`）與現金姿態（`cash_stance_sha256`，因現金姿態沒有自己的雜湊，取 `team_inputs.cash_stance` 的標準雜湊）。
- 安全拒絕：比照換股提名，`agent_step_degradable` 仍往上拋、整個流程停止，不降級。
- 配置：v2 的程式分支原樣保留（供舊 Policy 與舊 run 重建）；v3 保留 v2 的全部前置條件（回撤下限、轉向門檻、賣款可再用）當第二道防線，裁決狀態不是 `completed` 時另取消全部 quality_dip 配對（旗標 `AGENT_SWAP_CANCELLED:quality_dip_ruling_exhausted`／`_unavailable`／`_skipped`），`reject` 取消該對（`quality_dip_ruling`，不占 quality_dip 組數）。有效份量格 `approve` 不變、`downsize` 降一格；權重上限＝（超跌用 `oversold.max_entry_weight`，否則 `max_entry_weight`）× 份量格比例，之後仍受「買進不超過換出賣款」與全部後段限制，所以任何裁決組合的買進股數都不高於全部 `approve`。換出者賣單旗標說明寫明份量格、裁決與套用份量格。

**照計畫做不到或自行取捨之處**

1. **組數另計與 `rotation.max_swaps_per_run`**：`pair_cap` 原本另扣 `rotation.max_swaps_per_run − 第一輪出場數`。v3 的 quality_dip 組數只受自己的 `max_swaps_per_run` 限制，**不受這個扣減牽動**（計畫只說不占 `agent_swap.max_swaps_per_run`，沒提這項）；成交仍受換手、現金下限、檔數、單股與產業上限。這是取捨，請人確認。
2. **`needs_nomination`**：v3 的呼叫條件改為「其餘 `pair_cap` ＋ quality_dip 組數 > 0」，否則 thesis 名額為 0 而 quality_dip 名額大於 0 的日子永遠不呼叫 Agent、選 B 等於沒做。v1／v2 逐位元不變。
3. **`proceeds_to_add` 與 quality_dip**：v3 的 quality_dip 配對**不適用** `proceeds_to_add`（換入者沒有買單就取消該對、換出者不賣，賣款不流向別檔）。計畫第 6.1 節第 8 項寫「建議如此」，這裡照做；v1／v2 的互動不變。
4. **`QUALITY_DIP_V3_MAX_SWAPS = 10`**：v3 的 `max_swaps_per_run` 不再有 `agent_swap.max_swaps_per_run` 當上限，需要一個結構性範圍；10 只是程式常數，不是人填的上限，實際值仍待人填（第 8 節第 1 項）。
5. **`quality_dip_decline` 與 `needs_nomination`**：只在 Agent 實際被呼叫（`completed` 的提名）時才可能需要交代；程式封存的 `not_needed`／`exhausted`／`unavailable` 一律不得有它。
6. **簡報**：裁決簡報的 `turn_facts`／`market_index_facts`／`turn_state` 都標 `citable=false`；`exposure_facts` 沿用風險 Agent 的 `risk_exposure_brief`（組合曝險、產業權重、上漲日），兩檔個別的 beta 與產業取自 `exposure_review` 與 `exposure_facts`。
7. **回放**：隨 `agent_swap` 一起略過（v3 在 `agent_swap` 之下），notes 標 `agent_swap.quality_dip: ignored_in_replay`，`evidence_grade: replay_reference_only`。

**開啟前需要人做的事**：填第 1 項的硬上限數值；先存樣板 `config/policy_versions/【開啟日期】-before-quality-dip-v3.json`（回滾＝把 `position_sizing.agent_swap.quality_dip` 換回樣板的 v2 區塊，`team_inputs` 隨之回到 2.9）、改 `policy_id` 與真實 `available_at`；第一個有 quality_dip 配對的決策日起檢視裁決分布與 `reason_kind`。
