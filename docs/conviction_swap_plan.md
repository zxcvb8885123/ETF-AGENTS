# 信心換股（`rotation.conviction_swap`）：設計計劃

狀態（2026-11-09）：**已併入 [市場轉向時的持股曝險調整](exposure_rotation_plan.md)**（該文含 hold 信心與 `conviction_gap_swap`），本文只保留當初的資料與推導。實作時放在新版本閘門後面，舊 Policy／舊 run 逐位元不變。「事實」來自封存資料與程式；「推論」另外標示。

## 1. 要解決的問題

**事實**（`artifacts/daily` 的 23 個 run，10-27～11-09，含同日備援 run）：

- 金融股（`industry:17`）持有約 7～8 檔，交易 Agent 標 `exit` 0 次、`trim` 2 次，實際賣單 1 筆（2890.TW）。其他持股多次被出場。
- 出場規則要求「該股有空頭論點或持股檢視警示」（`tradeagent/trading/trader/references/data-rules.md:117`）。11-09 持有的 8 檔金融股有 4 檔空頭強度為 `none`；其他 18 檔持股只有 3 檔是 `none`。
- 11-09 的 12 檔新股買進候選裡有 8 檔被 `MAX_POSITIONS` 擋下（目標 30 檔、持有 26 檔），同時現金也已到防守格點。
- 持股的 `hold` 沒有信心等級（`conviction` 只給 buy／add），系統無法比較「新股」與「持股」誰比較值得持有。

使用者的決定（2026-11-09）：新股的信心高於持股時，可以賣持股買新股。「不得為了騰名額而賣」的意思應該是「不能**純粹**因為想買別檔而賣」，不是完全禁止比較。

## 2. 為什麼不用「持續落後就賣」

**事實**：11-09 的 8 檔金融股，近 5 日相對交易池 +4.8～+12.8 個百分點，近 20 日 +14.5～+30.4 個百分點（交易池 20 日 −21.9%）。26 檔持股只有 3 檔兩段都落後（3374.TWO、3653.TW、8046.TW），沒有金融股。

**推論**：落後規則在下跌段正好賣不到金融股，和目標不符，所以不當觸發條件。

## 3. 規則

交易 Agent 只多輸出一個列舉，配對全部由程式做。

1. **持股也給信心**：閘門開啟時，交易 Agent 對 `hold` 也給 `conviction`（`high`／`medium`／`low`），意思是「以目前資訊，這檔還值得佔一個位置的程度」。交易 Agent 分批執行，評持股時看不到別批的新股，所以評分不是「為了買別檔」，這就是「不純為買進而賣」的保證。
2. **程式換股**（全部成立才換）：
   - 新股：未持有、信心 `high`、本次因 `MAX_POSITIONS` 或現金買不到。
   - 持股：交易 Agent 標 `hold` 且信心 `low`；可交易；不在被留住的出場名單；持有滿最短天數。
   - 兩者至少差兩級（`high` 對 `low`）。
   - 新股配對順序沿用入選排序；持股換出順序為：有空頭 claim 者優先，再依相對交易池 20 日由弱到強，最後依代號（確定性）。
3. **上限**：每次最多 `max_swaps_per_run`（建議 2）；與 `rotation.max_swaps_per_run`、換手上限、產業上限、`min_positions`、出場冷卻共用，全部照舊由 Guard 檢查。
4. **留痕**：賣單由程式標 `exit`、旗標 `RISK_CONVICTION_SWAP`，不改寫交易 Agent 的原意圖。
5. **與產業無關**：不寫「賣金融、買科技」。任何產業的持股都適用同一條。

## 4. 成本與風險

- **成本（事實）**：買賣手續費各 0.1425%、賣出交易稅 0.3%，一來一回約 0.585%，未含滑價。
- **波動（推論）**：通常是從低波動換到高波動，帳戶下跌時跌更多。現金比例不變，但持股風險變高。
- **最近的反例（事實）**：過去 20 日金融股明顯抗跌。
- **分批一致性（推論）**：不同批次對 `low` 的標準可能不同，所以只用差兩級，不用 `high` 對 `medium`。
- **換手**：持有最短天數防止剛買就換。
- **驗證限制（事實）**：封存的 `hold` 沒有信心等級，**無法回放**，也不得捏造。只能開啟後看即時紀錄，至少 10 個決策日（`calibration.MIN_DECISION_DAYS`）。

## 5. 要改的地方（實作時）

| 位置 | 改動 |
| --- | --- |
| `decision/sizing.py` | `ROTATION_FIELDS` 加 `conviction_swap`；`GATE_PREREQUISITES` 登記；形狀驗證 `{version, max_swaps_per_run, min_holding_days}` |
| `decision/trader.py` | 閘門開啟時 `hold` 必填 `conviction`；沒開時維持現行規則 |
| `automation/agent_schemas.py`、交易階段 | 閘門開啟才用新 Schema；舊 Schema 物件不動 |
| `decision/allocation.py` | 新增換出選擇，放在 `_forced_swap_victims` 之後；重建只讀 Policy 與 trade decision |
| 持有天數 | 由 pipeline 從帳本算出綁進 Policy（做法同 `reentry_cooldown`），重建只讀 Policy |
| `decision/rotation.py` | `portfolio_slots` 的說明文字改為閘門版本；舊版文字不動 |
| Skill：`tradeagent/trading/trader/`（`SKILL.md`、`references/data-rules.md`） | 定義持股信心；改寫「騰名額」條文 |
| `evaluation/replay.py` | 舊 run 沒有持股信心：回放時不換，notes 標示 |
| `strategy_reflection/contracts.py` | `FORBIDDEN_PREFIXES` 加 `rotation.conviction_swap` |
| 文件 | `AGENTS.md`（交易 Agent 邊界、配置規則）、`docs/decision-contracts.md`、`docs/status.md`、`docs/glossary.md`、`README.md` |

`team_inputs` 是否升版：實作前先確認。推論是不用，因為新欄位在 trade decision 內，已由既有雜湊涵蓋。

## 6. 測試（`unittest.TestCase` 風格）

- 沒有閘門：Schema、提示詞、配置結果逐位元不變；`hold` 帶 `conviction` 被拒。
- 有閘門：`hold` 缺信心被拒；只有 `high` 對 `low` 才換；`high` 對 `medium`、`medium` 對 `low` 不換。
- 新股本來買得到就不換；持股不可交易、持有天數不足、在冷卻名單就不換。
- 上限：每次檔數、換手、產業、`min_positions`、現金；只賣不買的情況不得出現。
- 竄改持有天數或信心，Finalizer 拒絕；回放舊 run 不換。

## 7. 需要人決定

1. **換股門檻**：只有 `high` 換 `low`（建議，較保守），或連 `high` 換 `medium` 也可以（會賣比較多，分批標準不一致的風險也較高）。
2. **持有最短天數**：建議 5 個交易日。
