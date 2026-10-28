# 配置與輪動 v2 計畫（低波動偏好與滿檔凍結）

狀態：計畫，尚未實作。本文件只描述問題診斷與設計，不改變任何既有行為。

目標：移除「低波動優先」造成的防守股偏重，並讓投資組合在滿檔後仍能輪動，以爭取超額報酬。既有金融股不由程式強制賣出。

## 一、問題診斷（以 2026-10-27／10-28 決策 run 為證據）

### 1.1 權重與波動度成反比

`position_sizing.method = conviction_volatility_v1`，權重公式見 `src/etf_agent/decision/sizing.py:204`：

```
raw = conviction_multiplier / max(ATR14%, volatility_floor)
```

2026-10-27 run（`decision-20261027T000350Z`）實際結果，取同一信心等級的兩端：

| 股票 | 信心 | 權重 | ATR14% |
| --- | --- | --- | --- |
| 2412.TW | high | 6.61% | 1.70% |
| 3374.TWO | high | 1.42% | 7.89% |
| 2330.TW | medium | 4.50% | 1.66% |
| 1815.TWO | medium | 0.95% | 7.91% |

同一等級內，低波動股權重約為高波動股的 **4.6～4.8 倍**，純粹由波動度決定。交易池 ATR14% 從 0.62% 到 9.30%；受 `volatility_floor = 0.01` 影響，理論最大倍數約 9 倍。

### 1.2 但只改權重公式，效果有限

以同一 run 的真實信心等級重算（直接呼叫 `conviction_weights`，只改波動度指數）：

| 波動度指數 | 金融（industry:17） | 本文分組「防守類」 | 最大持股 |
| --- | --- | --- | --- |
| 1.0（現狀） | 26.0% | 49.2% | 2412.TW 6.61% |
| 0.5 | 24.5% | 43.9% | 2412.TW 5.19% |
| 0.0 | 21.9% | 37.3% | 四檔並列 3.86% |

「防守類」是本文件自訂分組＝金融 7 檔（2880、2884、2885、2886、2890、2891、2801）＋2412、4904、1216、1101、1402，非官方產業分類；1101、1402 屬水泥鋼鐵，只是波動相對低。

結論：金融從 26.0% 只降到 21.9%。主因是**選股結果**本身有 7 檔金融（占 30 檔的 23%），不只是權重放大。權重公式是放大器，不是主因。

### 1.3 入選的 ATR tie-break：本次只決定最後一個名額

`src/etf_agent/decision/allocation.py:658` 的排序鍵是「信心等級 → 支持數（降冪）→ ATR 低者優先 → 代號」。

2026-10-27 的 buy 候選 52 檔（high 10、medium 24、low 18），持股廣度目標 30 檔，因此 high 10 檔全入選、medium 24 檔只能進 20 檔。實際決定「哪 20 檔」的是**支持數**，不是 ATR：

| 支持數 | medium 檔數 | 入選 |
| --- | --- | --- |
| 3 | 2 | 全入選 |
| 2 | 13 | 全入選（含 1815.TWO ATR 7.91%、3443.TW ATR 7.30%） |
| 1 | 4 | 全入選 |
| 0 | 5 | 只剩 1 個名額 |

ATR 只在最後一個名額（5 檔支持數 0 的 medium 搶 1 個位子）生效，結果 ATR 最低的 2618.TW（1.91%）入選，6446.TW（4.19%）、4958.TW（6.50%）、6510.TWO（6.83%）、6187.TWO（8.53%）被擠掉。

因此本次 ATR tie-break 的實測影響只有 1／30 檔。它是**潛在且方向固定**的偏誤（同條件一律選低波動），在候選數遠多於名額時影響會放大，但不是 10-27 偏重防守股的主因。Part B 修的是這個潛在偏誤。

### 1.4 滿檔凍結（影響最大，且與 ATR 無關）

2026-10-28 run（`decision-20261027T231239Z`）：

- 意圖：`no_trade` 109、`hold` 28、`buy` 11、`add` 2，**沒有任何 exit 或 trim**。
- 持股 30 檔＝持股廣度上限 30 檔，`free_for_new = max_positions - len(active_symbols) = 0`。
- 13 檔 buy／add 候選全部掛上 `MAX_POSITIONS` 旗標，最終**委託數為 0**。

注意：此 run 的程式版本是 `git_c4bc2f37a776-dirty`，早於 `#107`（9601225「持股滿檔時新股不擠掉已持有的加碼」）。`#107` 已修正「既有持股的 add 被新股擠掉」，所以 3034.TW、3533.TW 的 add 在目前 HEAD 應該會成單。**但新股仍然進不來**：`free_for_new` 為 0 的限制不變。

結論仍然成立：只要交易 Agent 不輸出 exit／trim，就沒有新名字能進入投資組合，26% 金融會一直留著。此時改權重公式與 tie-break 都不會為新股產生任何委託。

### 1.5 交易 Agent 不知道自己已經滿檔

`team_inputs.json` 只有 `analyst_reports`、`cash_stance`、`prior_decision_context`，完全沒有 `max_positions`、持股檔數或剩餘名額資訊（實測計數為 0）。

所以交易 Agent 在不知道「買新股必須先賣一檔」的情況下，對 30 檔全給 `hold`、又對 11 檔新股給 `buy`，然後程式靜默丟掉全部 11 檔。它不是判斷不換股，是不知道要做這個取捨。

### 1.6 風險 Agent 目前無法要求賣出

`src/etf_agent/decision/risk.py:29` 的 `REVISION_TYPES` 只有四種，全部是「降低風險」且只作用於本次提案：

- `remove_candidate`：只能移除本提案的**買進候選**（`risk.py:673` 檢查 symbol 必須在本次 buy 單內）。
- `increase_cash_buffer`、`reduce_max_stock_weight`、`reduce_turnover_limit`：只能調緊政策數值。

沒有任何動作可以對**既有持股**要求出場或減碼。因此「風險 Agent 判斷風險後才賣金融股」目前做不到。

另外 2026-10-27 的 `risk_review.json` 已記錄 `REVISE_SCHEMA_CONFLICT`：輸出 schema 要求 `revision_actions` 的 `symbol` 與 `value` 至少 1 字元，但 Skill 指示要求 `remove_candidate` 的 `value` 為空字串，導致任何 revise 動作都無法合法輸出。revise 通道實際上是壞的。另也注意風險 Agent 兩天都是 `approve`，只掛 WATCH 旗標；它的職責是降低風險，不會為了「換到更好的標的」而要求出場。

## 二、設計

A、B 兩項是共同基礎；C1 與 C2 是「誰來觸發輪動」的二選一（也可都做）。既有持股一律不由程式強制賣出。

### A. 權重公式新增波動度指數（新方法，不改 v1）

新增 `position_sizing.method = "conviction_volatility_v2"`，新增欄位 `volatility_exponent`（Decimal，0～1）：

```
raw = conviction_multiplier / max(ATR14%, volatility_floor) ** volatility_exponent
```

- `volatility_exponent = 1` 與 v1 數值完全相同；`0` 為純信心等級（等級內等權）。
- `conviction_volatility_v1` 的語意與程式路徑完全不動，舊 run 仍能原樣重建驗證。
- 建議值 `0`。若要保留少量風險平價性質可用 `0.25`。

### B. 入選排序移除 ATR 偏好

新增 `position_sizing.selection_tiebreak`，允許值：

- `"atr_low"`：現狀（信心 → 支持數 → ATR 低 → 代號），舊 policy 預設值，維持相容。
- `"symbol"`：信心 → 支持數 → 代號。中性，不偏任一波動區間。
- `"momentum_high"`：信心 → 支持數 → `return_20d` 高者優先 → 代號。積極，主動偏向近期強勢股。

`allocation.py:317-336` 的 `RISK_MIN_POSITIONS`（持股不足時依 ATR 低者優先留住 `exit` 持股）**保持不變**：它只在出場後檔數不足時啟動，效果是留住既有持股，與「不強制賣金融股」一致。

### C. 解除滿檔凍結（本案關鍵，二選一）

#### C1：交易 Agent 看得到名額，自己決定換股（機會驅動）

- `team_inputs` 新增確定性欄位 `portfolio_slots`：`held_positions`、`max_positions`、`free_slots`，由程式計算。
- Skill 明確說明：名額為 0 時，要買新股必須對某檔既有持股輸出 `exit` 或 `trim`，並在理由中說明取捨。
- 交易 Agent 本來就是 exit／trim 的擁有者，也握有多空論點，由它做「換掉最弱的一檔」最自然。
- 代價：換手率上升，須觀察 `max_turnover_rate` 與費稅；也可能換錯。
- 輪動速度：快，由機會驅動。

#### C2：風險 Agent 新增 `exit_position`（風險驅動）

- 新增 revision type `exit_position`（建議同時新增 `trim_position`），只有風險 Agent 能輸出，必須附 `reason` 與 `evidence_ids`，`symbol` 必須是既有持股。
- 語意是「以風險為由要求出場／減碼」，屬於降低風險，符合風險 Agent 既有邊界；不是手寫權重。
- 賣出釋放的檔數進入同一次修正循環的 `free_for_new`，讓本次 buy 候選補上。
- 意圖標記沿用既有規則：仍有持股標 `trim`，減至零標 `exit`，留 `RISK_EXIT_POSITION` 旗標；不改寫交易 Agent 的原意圖。
- 代價：風險 Agent 只在偵測到風險時出場，低波動金融股正是它最不會賣的；輪動速度慢且可能長期不觸發。
- 輪動速度：慢，由風險驅動。

無論選哪個，都要修正 1.6 的 schema 衝突：`revision_actions` 的 `symbol`／`value` 改為依 `type` 條件必填（`remove_candidate`／`exit_position` 需 `symbol` 不需 `value`；三個政策類需 `value` 不需 `symbol`），並同步更新 `tradeagent/risk/portfolio-risk-review/` 的 Skill 與 `references/decision-contract.md`。

### D. 選股偏重（本計畫不處理，僅記錄）

7 檔金融是分析團隊與多空研究的結論，不是程式偏好；1.2 顯示這是 22% 的底盤。若 A～C 完成後金融仍長期偏重，需要檢討的是基本面分析師對「獲利年增＋營業現金流」的權重，以及產業上限 35% 是否該調低；這屬於策略層，不在本計畫。

## 三、影響範圍

- `src/etf_agent/decision/sizing.py`：v2 公式與 `volatility_exponent`、`selection_tiebreak` 的政策驗證。
- `src/etf_agent/decision/allocation.py`：`_conviction_targets` 排序鍵依 `selection_tiebreak` 分支；C2 的釋放檔數路徑。
- `src/etf_agent/decision/trader.py`、`finalization.py`：C1 的 `portfolio_slots` 建構與重建驗證。
- `src/etf_agent/decision/risk.py`：C2 的 `REVISION_TYPES` 新增項目、條件必填驗證、`apply` 路徑。
- `src/etf_agent/decision/validator.py` 與 Finalizer：重建驗證需支援 v2 與新欄位／新 revision type，並維持對 v1 舊 run 的重建。
- `config/decision_policy.json`：新 `policy_id` 與 `available_at`；policy_hash 改變。
- `src/etf_agent/strategy_reflection/`：`volatility_exponent` 是否納入可調白名單（建議納入，單次變幅上限 0.25）。
- 文件：`README.md`、`docs/architecture.md`、`src/etf_agent/decision/README.md`、`tradeagent/` 相關 Skill。

## 四、測試要求（unittest.TestCase 風格）

- v2 在 `volatility_exponent=1` 時與 v1 數值逐檔相同。
- `volatility_exponent=0` 時同等級等權，且單股上限與 redistribute 行為不變。
- `selection_tiebreak` 三種值各自的排序結果，含同等級同支持數的邊界（對應 1.3 的 5 檔支持數 0 搶 1 名額）。
- 舊 policy（無新欄位、method 為 v1）行為完全不變，舊 run 重建驗證通過。
- C1：`portfolio_slots` 由程式計算且 Finalizer 能重建；`free_slots=0` 時交易 Agent 的新股 buy 仍無委託（不得用 Skill 繞過硬規則）。
- C2：`exit_position` 的 symbol 不是既有持股時拒絕；缺 `reason`／`evidence_ids` 時拒絕；釋放檔數後 buy 候選確實補上；與 `forced_exit`、`RISK_MIN_POSITIONS` 的互動。
- `revision_actions` 條件必填：三個政策類缺 `value` 拒絕、`exit_position` 缺 `symbol` 拒絕、多餘欄位拒絕。
- 修正循環達 3 次仍不通過時由 Guard 拒絕（fail-closed）。
