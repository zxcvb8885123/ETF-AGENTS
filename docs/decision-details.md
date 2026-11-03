# 決策流程細節

概觀見[根目錄 README](../README.md#決策流程)，完整契約見[每日決策架構](architecture.md)。

## 原則

- **分工**：Agent 提出有證據的市場解讀與買賣意圖；指標、權重、股數、費稅及競賽限制由 Python 計算與驗證。
- **時間一致**：所有下游輸入必須與研究快照的截止時間、資料版本一致，並以當時可得時間防止未來資訊洩漏。
- **失敗處理**：資料不足或風控失敗時停止或明確降級。
- **前次決策只是背景**：交易 Agent 會看到上一個交易時段實際交出的決策對目前持股的意圖、信心與重估條件（目標時段與 cutoff 都必須更早，同一時段的晚間備案不算；跨 Snapshot 明確標為例外），用來檢查這次判斷是否一致、前次寫的重估條件是否出現；它不是證據，不能寫進理由，行動仍須由當天的多空論點支持；沒有前次決策時標為無資料。

## 對應參賽策略文件

交給主辦方的策略文件（`game_docs/ETF-AGENTS_AI_Agent_選股與決策策略_參賽格式.pdf`）寫的因子，都由程式計算後交給 Agent，細節見[架構文件](architecture.md#對應參賽策略文件的四類因子)：

- **基本面**：營收、獲利、負債，加上現金流（FinMind 現金流量表，稅前淨利與官方損益表相符才用）。
- **技術**：5／20／60 日變化、MA20／MA60 數值、ATR 與下行波動。
- **公司事件**：每則事件寫影響、反證（`counter_evidence`）與尚待確認事項（`pending_confirmations`）。
- **市場與風險**：風險 Agent 讀整體市場情緒、產業集中、每筆委託佔 20 日均量比例與壓力情境（比賽以成交均價結算，成交量只作提醒，不限制成交）。
- **持股檔數**：風險 Agent 和現金姿態一起選持股廣度：集中、平衡或分散，分別對應 22、26、30 檔（`config/decision_policy.json` 的 `positions_by_breadth`）。程式照交易 Agent 的信心高 → 中 → 低挑到該檔數；信心和支持數都一樣時，依 `selection_tiebreak` 決定（`atr_low` 波動低的優先，預設；`symbol` 不看波動；`momentum_high` 近 20 日漲多的優先）。Agent 不輸出檔數。
- **滿檔換股**：持股已達上限時，未持有的新股只能用本次出場釋出的名額，上限是 `rotation.max_swaps_per_run`，且信心不得低於 `rotation.min_new_conviction`。名額配對由程式全域計算；交易 Agent 只看到程式算好的名額狀態（`portfolio_slots`）當背景，出場必須有該股票的空頭論點或程式持股檢視警示支持，不得為了騰名額而賣。
- **持股檢視（停損／停利）**：程式依帳戶成本與行情算出每檔持股的帳面報酬與近 20 日高點回落，達 `position_review` 門檻時標警示，交易 Agent 必須正視並自行判斷賣、減碼或續抱（虧損警示的持股續抱須有多頭論點）。門檻只決定何時必須正視，不是固定百分比就賣；`team_inputs` 升為 2.4，尚未實跑。
- **入選排序**：交易 Agent 只能給高中低三級信心；同一等級內誰優先入選，改由交易 Agent 在各批合併後做第二輪（同一個 Skill、不另設 Agent），一次看全部候選判斷並附理由（不改信心、不輸出數字），程式照名次挑；沒有排序時沿用原規則。`team_inputs` 升為 2.5，尚未實跑。
- **低波動偏好強度**：`conviction_volatility_v2` 的 `volatility_exponent` 控制權重要多看波動度（1 與舊版相同、0 完全不看）。這個值與換股上限都可由策略反思依校準報告調整；版本開關只由人設定。
- **引用內容要支持結論**（`evidence_policy = typed_evidence_v1`，新 bundle 預設）：多空 claim 與分析師 finding 要宣告種類（price／financial／revenue／material_event／news），程式核對引用的證據種類對映，至少一個證據必須屬於該股票；市場層級與帳戶證據只能作補充。風險 Agent 的現金姿態至少要引用一個市場層級證據（全市場情緒、恐懼貪婪、隔夜或程式市場狀態），全市場情緒負面時不得積極；brief 不附交易 Agent 的意圖與信心統計。驗證退回的訊息並列「降級」與「補依據」兩種修法，重試前後的變化記在 `agent_calls.revision_diff`。舊 bundle／舊 run 維持原驗證。`team_inputs` 升為 2.6（再附程式算的基本面指標，Finalizer 重算比對；只在 bundle 有 `fundamental_period_policy` 時），尚未實跑。
- **控制回撤**：程式算帳戶峰值回撤，達 5% 時現金姿態至少 neutral、達 10% 時至少 defensive（`config/decision_policy.json` 的 `drawdown_control`）。綁定時另存帳本 NAV 歷史的 `history_ref`（run_id 鏈與 sha256），每日 pipeline 與事後重建都以帳本歷史重算並比對峰值；舊 run 照舊驗證。2026-11-03 起有解除門檻（進 neutral 後回撤要回到 3% 以下才解除、defensive 要回到 7% 以下才降回 neutral）與防守減碼（`position_sizing.drawdown_derisk`：defensive 時程式依 ATR 高→低把持股減碼到 20% 現金緩衝，一次最多 NAV 的 25%，`RISK_DRAWDOWN`），見下方「風控閘門（2026-11-03 晚間）」。

## 交割口徑與現金（2026-11-03）

- **同日賣款可買替補**：主辦方以淨額交割，`config/decision_rules.json` 的 `reuse_sell_proceeds` 改為 `true`（版本 `decision-rules-2026-11-03`）。規則封進每份 Decision run，舊 run 仍以當時封存的 `false` 重建。
- **買單順序與塵埃取消**（`position_sizing.cash_fit_order: "v1"`，比例 `cash_fit_min_fill_ratio` 預設 0.5）：現金不足時信心高、名次前的買單先拿錢；未持有的新股只買得到不到目標一半就整筆取消（`RISK_CASH_FIT`），不讓 1～4 張的零頭占名額；已持有的加碼只減量。
- **現金超過 25% 上限時**（`position_sizing.cash_ceiling_fix: "v1"`）：程式依 ATR14 由低到高留住交易 Agent 要出場的持股續抱（`RISK_CASH_CEILING`），直到現金比例低於上限，不再整份拒絕；沒有可留的出場時仍由 Guard 拒絕。
- **單股上限的空間**（`position_sizing.stock_limit_headroom: "v1"`）：當日有買進／加碼的股票封頂留 3%（10%×0.97、2330 為 25%×0.97），只續抱的仍留 0.5%；因為自己買上去超限當日就算違規，價格波動超限才有 5 日寬限。
- **跳空預留**（`position_sizing.buy_gap_reserve: "v1"`，比例 `buy_gap_reserve_rate` 預設 0.03）：決策用前日收盤、成交用當日均價，買單總額多算 3% 當現金需求，避免開高後被主辦方以資金不足整批作廢。
- **結算口徑**：主辦方 DataPack 的 `prev_day_orders.outcome` 為 `voided` 時，帳本把前一日決策結算為無成交；policy 帶 `insufficient_funds_policy: "void_batch"` 時，模擬成交現金不足也整批作廢（與主辦方相同），舊 run 維持逐張減少。
- 以上都是版本閘門，只由人設定、策略反思不得改；沒有該欄位的舊 Policy／舊 run 重建結果逐位元不變。

## 單股超出權重上限時

既有持股若因價格變動超出單股權重硬上限：

1. 配置程式在 Guard 前，按整張計算必要減碼。
2. 連同部分成交的壓力情境一併重算。
3. 原 Agent 意圖與程式風險減碼分開記錄；減碼賣單由程式標為 `trim`（仍有持股）或 `exit`（減至零）。
4. 無法滿足所有硬性限制時仍會拒絕。

## 單一產業超過 60% 時

`config/decision_rules.json` 的 `max_sector_weight` 自 `decision-rules-2026-11-03b` 起為 0.6（11/03 白天曾改為 1 關閉，之前為 0.35；舊 run 以封存的 rules 重建）。產業上限不是比賽硬規則，是本隊自設的風控。配置程式在 Guard 前檢查各壓力情境下的產業權重：

1. `position_sizing.sector_fit: "v1"`（目前啟用）：超標時只按整張減少當日買進／加碼的增量（同產業先減信心低、市值大者），**不賣既有持股**；買單減光仍超限（價格漂移造成）的產業由 Guard 容忍，條件是本次沒有買進該產業的任何股票（details 記 `carried`）；有買進的產業仍是硬性違規。
2. 沒有閘門的舊路徑：先減當日買進，再賣既有持股，Guard 嚴格判定。
3. 減碼紀錄為 `RISK_SECTOR_WEIGHT` 旗標，舊路徑的賣單意圖由程式標為 `trim`／`exit`，不改寫交易 Agent 的原意圖；減碼釋出的資金不再加碼其他股票。

## 風控閘門（2026-11-03 晚間）

- **換手上限 0.3**（`max_turnover_rate`，`position_sizing.turnover_fit: "v1"`）：換手 = 買賣 gross 合計 ÷ NAV。超過時程式依（信心低→高、ATR 高→低）縮減買單：加碼只減增量、新股殘量不到目標一半就整筆取消（`TURNOVER_LIMIT`）；賣單本身就超過時依 ATR 由低到高留住出場持股（`RISK_TURNOVER`），不整份拒絕。
- **出場冷卻**（`position_sizing.reentry_cooldown`，3 個日曆天）：最近出場（含程式強制換股）的股票未持有時不再買進（`RISK_REENTRY_COOLDOWN`）；名單由每日 pipeline 從決策庫綁進 Policy，重建只讀 Policy。
- **強制換股要有證據**（`rotation.forced_swap.confirmation: "v1"`）：被換出的持股必須有交易 Agent 本次採納的該股空頭 claim，否則不換、記 `RISK_FORCED_SWAP_UNCONFIRMED`；換出數不超過持股數 − `min_positions` 與現金能支應的新股數。
- 以上都是版本閘門，只由人設定、策略反思不得改；沒有該欄位的舊 Policy／舊 run 重建結果逐位元不變。
