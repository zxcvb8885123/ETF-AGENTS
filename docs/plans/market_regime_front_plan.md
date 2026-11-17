# 大盤姿態前置並影響選股（`position_sizing.market_posture`）：設計計畫

狀態（2026-11-17 晚）：**設計草案，未實作、未開啟**。本檔只寫計畫，沒有改任何程式、設定或其他文件。文中「建議值」都是待人決定的起點，不是程式預設；所有數字最後由人填。

閱讀提醒：第 1 節的數字是事後整理。**決策當下無法知道隔天漲跌，不能拿 11/16、11/17 的結果當成「這樣設計一定對」的理由**；它們只用來說明現行機制有哪些缺口。

## 1. 問題與現況查證

### 1.1 使用者的問題

- 比賽帳戶 -8.5%（第一名 -2.04%，我們第 60 名）。11/16、11/17 兩天 0050 跌 8.2%，我們跌 6.7%。
- 希望分析團隊一開始就先判斷「今天大盤要防守還是進攻」，並讓它影響交易 Agent 選什麼股，而不只是調現金。

### 1.2 帳戶與行情事實（由封存檔重算）

來源：`artifacts/ledger/ai-cup-2026-final/runs/prepare-*/account_snapshot.json`、`inputs/close-*/close_market.json`、`artifacts/decisions/2026-11-1x/*/team_inputs.json`、`policy.json`、`proposal.json`。

| 收盤日 | 帳戶 NAV | 現金比例 | 組合 beta（含現金，對交易池等權，60 日） | 0050 當日 | 交易池等權當日 | 交易池跌停檔數（150 檔中） |
| --- | --- | --- | --- | --- | --- | --- |
| 11/10 | 953,014,209 | 15.0% | 0.13 | — | — | — |
| 11/11 | 972,614,993 | 14.9% | 0.16 | +3.5% | +3.0% | 0 |
| 11/12 | 963,964,768 | 13.7% | 0.33 | -2.2% | -2.8% | 5 |
| 11/13 | 981,240,865 | 12.6% | 0.51 | +1.5% | +1.8% | 0 |
| 11/16 | 938,638,008 | 17.6% | 0.58 | -5.9% | -6.3% | 43 |
| 11/17 | 915,079,825 | 18.1% | （今晚決策才算） | -2.5% | -3.2% | 16 |

- 兩天 NAV：981.24 → 915.08 百萬，-6.74%；0050 兩天 -8.29%。與使用者描述相符。
- 組合 beta 從 11/10 的 0.13 一路升到 11/16 的 0.58，是 11/12～11/16 三次決策主動換進高 beta 股造成的，不是原本就有。

### 1.3 高波動股是怎麼進來的

使用者點名的六檔，全部在 11/12～11/16 經「換股提名」進場（`policy.json` 的 `nominated_pairs`）：

| 代號 | 進場決策日 | 提名目的 | beta | ATR14 占股價 | 11/17 開盤前權重 |
| --- | --- | --- | --- | --- | --- |
| 3443 | 11/12 | thesis | 1.65 | 9.1% | 3.3% |
| 2368 | 11/12 | thesis | 1.95 | 10.5% | 3.0% |
| 1303 | 11/13 | exposure | 1.87 | 9.4% | 3.2% |
| 6510 | 11/13 | exposure | 1.77 | 9.0% | 3.8% |
| 1815 | 11/16 | thesis | 1.53 | 10.6% | 2.9% |
| 3044 | 11/16 | thesis | 1.37 | 8.5% | 3.7% |

- 六檔合計約 19.8%；beta ≥ 1 的持股共 13 檔、約 29.8%。
- 這幾天風險 Agent 的輸出（`team_inputs.cash_stance`）：11/12 `defensive`＋`probe`、11/13 `neutral`＋`probe`、11/16 `neutral`＋`probe`、11/17 `defensive`＋`keep_defensive`。程式的 `regime_assessment` 從 11/10 到 11/17 **每天都是 `bear`**（MA20 之上比例 5%～26%）。
- 兩天損失的歸因（依起始權重 × 收盤報酬的粗估，11/16 那天用下單前持股）：beta ≥ 1 的持股貢獻約 -4.7 個百分點（-2.39、-2.35），2330 約 -1.05，beta < 0.3 的持股約 -0.64。**兩天 6.7% 的跌幅，七成來自剛換進來的高 beta 股。**
- 交易池依 beta 分三等分（beta 以 11/16 收盤為止的資料算，對 11/16 當天有些微後見之明），11/13→11/17：低 beta 那組 -2.5%、中間 -9.6%、高 beta 那組 -15.2%。
- 靜態對照（把 11/10 收盤的持股原封不動放到 11/17，未計費稅與除權息，只是參考）：11/10→11/13 為 +3.36%（實際 +2.96%），11/10→11/17 為 +0.29%（實際 -3.98%）。也就是這一段裡，提高 beta 在上漲那三天沒有多賺，在下跌那兩天多賠約 4 個百分點。這是單一事件，不能推論成常態。

### 1.4 現行機制查證（以 `docs/status.md` 與 `config/decision_policy.json` 為準）

現行設定 `policy_id: strategy-2026-11-17-earmark-1`（`available_at 2026-11-17T16:30:00+08:00`）。

| 機制 | 現況 | 與本題的關係 |
| --- | --- | --- |
| 程式的市場狀態 `regime_assessment` | 一直存在；依交易池 MA20／MA60 之上比例分 bull／neutral／bear（門檻 60%／40%，寫死在 `MomentumEngine._regime`）；`market-regime:pool-breadth` 是可引用的市場層級證據 | **已經在鏈的最前面**：技術分析師、交易 Agent（每批簡報第一個鍵）、入選排序都讀得到。但它只是資訊，沒有任何約束力 |
| 風險 Agent 現金姿態 `level` | `aggressive`／`neutral`／`defensive`，排在交易 Agent 之後、配置之前 | 只對應現金比例與回撤下限。它的輸入**完全不含交易 Agent 的輸出**（`run_cash_stance` 只讀 bundle、momentum、分析師報告、Policy 欄位、前次決策背景與程式算的事實），所以順序上可以往前移 |
| `cash_ratio_range` v1 | 已開啟；防守 15／18／21%、中性 8／10／13%、進攻 3／5／8%，單日拉高上限 3 個百分點；比賽硬規則現金 < 25% | 防守最多也只有約兩成現金，其餘八成仍是股票。現金這個槓桿先天很短 |
| `market_index_facts` v1 | 已開啟，純紀錄；加權指數收盤、當日漲跌、5／20 日報酬、離高點回檔，只給風險 Agent 當背景，`citable=false` | 不給交易 Agent，不影響配置 |
| `exposure_view` v2 | 已開啟；風險 Agent 填 `keep_defensive`／`probe`／`start_rebuild`／`rebuild` | **使用者說「風險 Agent 完全不影響選股」不完全正確**：這個列舉決定換股提名裡 `purpose: exposure` 的配對能取用幾組。但它只有一個方向——**放行加曝險** |
| `offense` v1 | 已開啟；`probe` 為 3 組、份量格 medium、`beta_stop 0.55` | `beta_stop` 只停止「其後的 exposure 配對」，**不管 thesis、quality_dip 配對與一般買單**。六檔裡四檔是 thesis 進來的，完全不受這條線限制；11/17 開盤前組合 beta 0.58 已超過 0.55 |
| `exposure_rotation` v3 | 已開啟，但 `agent_swap` v2 下換股步驟停用（旗標 `EXPOSURE_ROTATION_SKIPPED:agent_swap`）；`beta_target_by_stance.defensive` 是 null | 即使沒停用，它也只會「beta 低於目標時換進高 beta」，**沒有往下降 beta 的動作** |
| `drawdown_control` | 回撤 5% 起至少中性、15% 起至少防守（11/11 由 10% 放寬） | 帳戶目前回撤約 8.5%，下限是中性，不強制防守 |
| `drawdown_derisk` v5 | 防守姿態且現金低於緩衝時，依交易 Agent 的釋出順序減碼 | 11/17 現金 17.6% 已高於防守最低格 15%，沒有觸發 |
| `position_review` v2 | 個股警示（虧損 12% 等），交易 Agent 必須處理、不是賣出指令；`stop_exit` 未開啟 | 個股層級，不看大盤 |

查證時另外發現：`docs/status.md` 開頭仍寫依 `strategy-2026-11-12-turnover-skip-1` 核對，與現行 `policy_id` 不一致（表內數值多數已更新）。本計畫不改它，只提醒。

### 1.5 真正的缺口

1. **不缺「大盤看法」，缺的是「看法對選股有約束力」**。程式每天都說 bear，交易 Agent 也看得到，但沒有任何規則把它接到「能買多少高波動股」。
2. **現有的選股槓桿全部是單向加風險**：`exposure_view`、`offense`、`rebound_participation`、`quality_dip` 都是「放行多買」。沒有任何一條是「防守日限制高波動股」。
3. **上限有漏洞**：`beta_stop` 只管 exposure 配對，thesis 配對與一般買單不受限。
4. **順序**：風險 Agent 的姿態在交易 Agent 給完意圖與信心之後才出來，交易 Agent 選股時不知道今天的姿態。

所以使用者要的東西可以拆成兩件事：(a) 一條**不靠預測**、對所有買進路徑都生效的高波動曝險上限；(b) 把姿態判斷移到交易 Agent 之前，讓它選股時就知道。

## 2. 設計方案與取捨

### 方案 A：純規則式高波動上限（不新增任何 Agent 判斷）

程式把每檔股票分成低／中／高風險桶（依 beta 與 ATR），再依程式的 `regime_assessment` 對應一列人填的上限（高風險桶總權重、每次新進檔數）。所有買進路徑（一般買單、三種目的的換股配對）都受限；只擋買、不強迫賣。

- 優點：完全確定性、可整份重算、可回放；不增加 LLM 呼叫；判斷錯的代價只有「少買」。
- 缺點：`regime` 是均線廣度，落後；交易 Agent 選股時仍不知道哪些會被擋，被擋掉的名額與現金可能閒置；沒有回應使用者「分析團隊先給看法」的要求。

### 方案 B：姿態前置＋雙鑰匙上限（重用風險 Agent 既有的三級姿態）

方案 A 的全部內容，再加兩件事：

1. 把風險 Agent 的現金姿態呼叫移到分析團隊之後、交易 Agent 之前（輸入不變，所以不需要新 Agent、不多一次呼叫）。
2. 交易 Agent 與換股提名的簡報多一塊背景：今日有效姿態、每檔的風險桶、高風險桶還剩多少空間。
3. 有效姿態＝程式姿態與風險 Agent 姿態**取較保守者**。Agent 只能讓它更保守，不能單獨放寬。

- 優點：同一個「進攻／中性／防守」判斷同時管現金與選股風格，不會出現兩個互相矛盾的大盤看法；交易 Agent 事前知道限制，會把信心放在可買的股票上；判斷錯的代價有程式上限封頂。
- 缺點：要改流程順序（續跑邏輯、簡報、Skill 都要動）；Agent 的姿態判斷有沒有價值，比賽結束前累積不到足夠樣本。

### 方案 C：新增第四位「大盤分析師」，輸出獨立的 `market_posture` 報告

分析團隊多一位只看市場層級資料的分析師，輸出 `defend`／`balanced`／`attack`；風險 Agent 的現金姿態照舊。

- 優點：字面上最貼近「分析團隊先給看法」。
- 缺點：每天多一次呼叫與一份新報告契約（版本、驗證、Skill、封存、重建全套）；它和風險 Agent 讀幾乎相同的市場資料，卻各給一個三級判斷，兩者不一致時要再定一套裁決規則；剩 8 個交易日做不完也驗不了。

### 取捨整理

| | 方案 A | 方案 B | 方案 C |
| --- | --- | --- | --- |
| 新增 Agent 輸出 | 無 | 無（沿用 `level`） | 一份新報告、一個新列舉 |
| 每日多幾次 LLM 呼叫 | 0 | 0（只換順序） | 至少 1 |
| 交易 Agent 選股時知道姿態 | 只知道 regime | 是 | 是 |
| 判斷錯時的損失上限 | 程式封頂 | 程式封頂 | 取決於另訂的裁決規則 |
| 與現金姿態矛盾的可能 | 無 | 無（同一個判斷） | 有 |
| 可用封存資料回放 | 可（確定性） | 規則部分可，前置部分不可 | 不可 |
| 估計工作量 | 約 2 天 | 方案 A 再加約 2 天 | 5 天以上 |

### 推薦：方案 B，分兩階段做，第一階段就是方案 A

- **階段 1（規則式上限）**：可以單獨上線，是「不靠預測的備案」本體。
- **階段 2（姿態前置）**：先以純紀錄方式實作與觀察，是否接上由人決定。

不推薦方案 C：它多出來的東西（一個獨立的市場判斷）正是最沒有證據、最容易和現有機制打架的部分。

## 3. 契約與資料流變更

### 3.1 新閘門 `position_sizing.market_posture`（形狀草案）

```json
"market_posture": {
  "version": "v1",
  "risk_bucket": {
    "high": {"beta_at_least": "…", "atr_pct_at_least": "…"},
    "low":  {"beta_below": "…",    "atr_pct_below": "…"}
  },
  "program_posture_by_regime": {"bull": "attack", "neutral": "balanced", "bear": "defend"},
  "by_posture": {
    "defend":   {"max_high_risk_weight": "…", "max_new_high_risk_entries": 0, "high_risk_conviction_cap": "low"},
    "balanced": {"max_high_risk_weight": "…", "max_new_high_risk_entries": 0, "high_risk_conviction_cap": "medium"},
    "attack":   {"max_high_risk_weight": "…", "max_new_high_risk_entries": 0, "high_risk_conviction_cap": "high"}
  },
  "shock_guard": {"pool_return_1d_at_most": "…", "limit_down_share_at_least": "…", "hold_trading_days": 0},
  "front": {"version": "v1", "mode": "record"}
}
```

- 全部數字必填、程式不給預設；`shock_guard` 與 `front` 是選配子欄位。
- 驗證：三列上限單調（防守 ≤ 中性 ≤ 進攻）；`risk_bucket` 的低門檻嚴格小於高門檻；`program_posture_by_regime` 只能填三個列舉值，`regime` 不可用時固定 `defend`、不可設定；未知欄位、缺欄位、版本錯一律拒絕整份 Policy。
- 流程綁入、樣板預填即拒絕：`program_posture`、`effective_posture`、`shock_state`。
- 前提（列入 `GATE_PREREQUISITES`）：`exposure_rotation`（共用 `beta_window_days` 與 `BetaModel`，單一出處）、`agent_swap` v2、`typed_evidence_v1`；`front` 另需 `cash_ratio_range`。
- 只由人設定，列入策略反思的 `FORBIDDEN_PREFIXES`。

### 3.2 誰輸出什麼

| 角色 | 階段 1 | 階段 2（`front`） |
| --- | --- | --- |
| 程式 | 算每檔 `risk_bucket`（低／中／高）、`program_posture`、`effective_posture`、高風險桶目前權重與剩餘空間，封存為 `team_inputs.posture_facts` | 同左，另把這些事實放進交易 Agent 與換股提名簡報 |
| 分析團隊 | 不變 | 不變（技術分析師本來就讀 `regime_assessment`） |
| 多空研究員 | 不變，**不讀姿態** | 不變，**不讀姿態**（維持隔離，避免兩方都順著姿態寫） |
| 風險 Agent | 不變（`level` 在配置時才參與取保守） | 呼叫時點移到交易 Agent 之前；輸出欄位不變、不多填任何東西 |
| 交易 Agent | 不變 | 簡報多一塊 `market_posture_background`；輸出欄位不變 |

Agent 全程不輸出數字，也沒有新增列舉。姿態仍是既有的 `level`。

### 3.3 風險桶（程式計算）

- 輸入：`BetaModel`（視窗取 `exposure_rotation.beta_window_days`，對交易池等權）與 `momentum` 的 `atr_14_pct`，都只用 bundle 的 `price_series`。
- 規則：beta 或 ATR 任一達到高門檻 → `high`；兩者都低於低門檻 → `low`；其餘 `mid`。
- **缺資料一律當 `high`**（事實欄位照實寫 `unknown`，配置時視為高風險），不補值。
- 任何 bar 晚於 `snapshot.latest_trade_date`、可得時間缺時區或晚於 cutoff 即拒絕（與 `exposure_facts` 相同檢查）。

### 3.4 有效姿態

```text
program_posture = 取最保守（
    program_posture_by_regime[regime]，regime 不可用時為 defend,
    shock_guard 生效時為 defend,
    drawdown_control.stance_floor 對映（defensive → defend、neutral → balanced））
effective_posture = 取較保守（program_posture, 風險 Agent 的 level 對映）
    aggressive → attack、neutral → balanced、defensive → defend
```

- Agent 不能單獨放寬：程式是 `defend` 時，風險 Agent 給 `aggressive` 也還是 `defend`。
- 想在 bear 期間放寬，只能由人改 `program_posture_by_regime.bear`，這是明確的加風險決定。
- `shock_guard`（規則式急跌保護，不靠預測）：最近一個交易日的交易池等權報酬不高於人填的值，或跌停檔數占比不低於人填的值，就把接下來 `hold_trading_days` 個交易日的程式姿態壓到 `defend`。由 `price_series` 逐日無記憶重算，不跨 run 存狀態。

### 3.5 程式如何限制選股與權重

對**所有買進路徑**生效，只縮不放、不改寫交易 Agent 的意圖與信心，只留旗標：

1. **換股提名**：依清單順序試算，換入者是 `high` 且（預估成交後高風險桶總權重超過 `max_high_risk_weight`，或當日新進高風險檔數已達 `max_new_high_risk_entries`）時，只取消該組，旗標 `AGENT_SWAP_CANCELLED:risk_bucket_cap`，換出者不賣。thesis、exposure、quality_dip 三種目的都適用，補上 `beta_stop` 只管 exposure 的漏洞。
2. **一般買進與加碼**：高風險桶的預算總和封頂在上限內，由優先序最低者（信心、名次、代號）先縮，向下取整張，不足一張取消，旗標 `RISK_BUCKET_CAP`。釋出的預算依現行順序重新分給非高風險候選（做法比照 `liquidity_fit` v2），分不完留作現金。
3. **信心上限**：高風險桶的 buy／add 在計算份額與入選順序時，信心取 `min(交易 Agent 的信心, high_risk_conviction_cap)`，旗標 `MARKET_POSTURE_CONVICTION_CAP`；封存的 `conviction_by_symbol` 不動。
4. **不賣**：既有的高風險持股即使超過上限也不由程式賣出（使用者已決定硬停損不開，這裡維持同樣立場）。超過上限的效果只有「不能再買、不能再加」。
5. **硬規則優先**：若封頂會讓現金達到 25% 上限、持股少於 20 檔或 Active Share 不足，依優先序把被縮的買單放回，直到硬規則滿足，旗標 `RISK_BUCKET_CAP_RELAXED:<原因>`。上限永遠不會讓 Guard 因它而拒絕。

### 3.6 階段 2 的流程與簡報

```text
資料 → 三位分析師 → 風險 Agent 現金姿態（提前，輸入與輸出不變）
     → 多空研究員（不讀姿態）
     → 交易 Agent（簡報含姿態背景與風險桶）→ 入選排序 → 換股提名（簡報含高風險桶剩餘空間）
     → 綁定 Policy → quality_dip 裁決 → 程式配置 → 風險審查
```

交易 Agent 簡報新增的 `market_posture_background`（`citable=false`、沒有 `evidence_id`）：

- `effective_posture`、`program_posture`（列舉）。
- 本批每檔的 `risk_bucket`，以及 beta、ATR 占比兩個事實數字。
- 高風險桶目前權重、今日上限、剩餘空間與還能新進幾檔（程式算的數字）。
- 固定說明文字：這是背景不是證據；買賣仍須由本次多空 claim 支持；防守日被上限擋掉的買單不會成交，請把信心放在買得到的股票上；市場狀態落後，僅依本次截止前可得的證據判斷。

`front.mode`：

- `record`：只改順序與封存，簡報不附背景、配置照階段 1。用來觀察「提前呼叫」本身穩不穩。
- `brief`：簡報附背景。配置規則仍是階段 1 那一套，不因 Agent 多看了背景而放寬任何東西。

### 3.7 與現有機制的關係（避免重複與矛盾）

| 現有機制 | 關係 |
| --- | --- |
| `regime_assessment` | 是程式姿態的來源；公式與門檻不動。新閘門只是把它接上約束力 |
| 風險 Agent `level`＋`cash_ratio_range` | 現金對應完全不變。`level` 多一個用途：參與有效姿態取保守。同一個判斷管兩件事，不會矛盾 |
| `market_index_facts`、`cash_stance_turn_facts`、`benchmark_facts` | 不變，仍只是風險 Agent 的背景。`shock_guard` 用的是交易池行情，不讀這些背景，避免把 `citable=false` 的東西變成規則輸入 |
| `exposure_view`／`offense` | 仍決定 exposure 配對取用幾組與份量格。新上限排在它**之後再收一次**：`offense` 放行的配對，若換入者是高風險且超過上限，照樣取消。取保守，兩者不會互相放寬 |
| `offense.beta_stop` | 保留。它管「組合整體 beta 不再加」，新上限管「高波動股的總權重」，口徑不同、可並存；任一條擋下就取消該組 |
| `exposure_rotation` | 步驟在 `agent_swap` v2 下本來就停用，不受影響；只共用 `beta_window_days` |
| `rebound_participation`、`quality_dip` | 它們放行的買進同樣受新上限限制。若人希望抄底不受限，要明確把 `by_posture.defend` 的上限設高，不另開例外 |
| `drawdown_control`／`drawdown_derisk` | 回撤下限併入程式姿態（只會更保守）；`drawdown_derisk` 的減碼不變 |
| `position_review` | 不變。新閘門不產生個股警示、不產生賣出 |

## 4. 版本閘門與舊 run 相容

- 沒有 `position_sizing.market_posture` 的舊 Policy／舊 bundle／舊 run：配置、簡報、流程順序、`team_inputs` **逐位元不變**。
- `team_inputs` 不升版（維持 2.10）：多一個選配鍵 `posture_facts`，與閘門雙向檢查（有閘門沒事實、有事實沒閘門都拒絕），Finalizer 以 bundle 與 Policy 整份重算比對。做法比照 `dip_facts`、`exposure_facts`。
- `effective_posture` 由 `apply_trade_decision` 綁進閘門；`sizing.validate_position_sizing` 以 `posture_facts` 與封存的 `cash_stance.level` 重算比對。
- 流程順序只在 `front` 子欄位存在時改變。封存檔的內容與雜湊不含順序，但續跑要記錄並比對：原 run 有 `front` 而續跑時沒有（或相反）一律拒絕續跑。
- 回放先比照其他閘門標 `market_posture: ignored_in_replay`，之後再做可回放版本（見第 6 節）。
- 每次開啟或調整前，把現行設定存成 `config/policy_versions/<日期>-before-market-posture.json`；回滾＝刪掉 `position_sizing.market_posture`（或只刪 `front`）。
- 實作完成後才需要同步更新 `README.md`、`docs/status.md`、`docs/decision-contracts.md`、`docs/glossary.md`、`docs/architecture.md` 與 `docs/plans/README.md`；本計畫階段不動它們。

## 5. 驗證與測試清單

測試維持 `unittest.TestCase` 風格，放在 `tests/decision/` 與 `tests/automation/`。

正常流程：

- 風險桶分類：高、中、低三種各一例；beta 高但 ATR 低仍為 `high`。
- 三種 regime 各自對到設定的程式姿態；`level` 較保守時有效姿態跟著 `level`；`level` 較積極時跟著程式。
- 換股配對超過上限只取消該組、換出者不賣、後面不超限的組照常成交。
- 一般買單封頂：由優先序最低者先縮、整張向下取整、釋出預算流向非高風險候選。
- 信心上限只影響份額與順序，封存的 `conviction_by_symbol` 與交易 Agent 意圖不變。
- 階段 2：交易 Agent 簡報含背景區塊；多空研究員的共同輸入與 `shared_input_sha256` 不含任何姿態欄位。

Fail-closed 分支：

- 任一檔缺 beta 或 ATR → 視為 `high`，事實欄位為 `unknown`，不補值。
- `regime` 為 `unavailable`（覆蓋不足 80%）→ 程式姿態 `defend`。
- bar 晚於 `snapshot.latest_trade_date`、可得時間缺時區或晚於 cutoff → 拒絕。
- Policy：缺欄位、多餘欄位、版本錯、上限不單調、高低門檻倒置、樣板預填 `effective_posture`／`program_posture`／`shock_state`、缺前提閘門 → 整份拒絕。
- `team_inputs.posture_facts` 與閘門只存在其一 → 拒絕；事實任一數值被改 → Finalizer 重算不符拒絕。
- 封存的 `effective_posture` 比重算結果積極 → 拒絕。
- 交易 Agent 或風險 Agent 的文字出現 `posture_facts` 的鍵名或識別碼 → 驗證失敗（比照 `exposure_facts`）。
- 封頂會違反現金上限、最少檔數或 Active Share → 依序放回並留 `RISK_BUCKET_CAP_RELAXED`，Guard 仍通過；放回後仍不過則照現行由 Guard 拒絕。
- 沒有閘門的舊 Policy 對同一份輸入，`proposal.json` 逐位元相同（回歸測試，至少取 11/12、11/13、11/16、11/17 四份封存 run）。
- 續跑：`front` 開關與原 run 不一致 → 拒絕。
- 策略反思提案碰到 `market_posture` 前綴 → 拒絕。

完成後依專案規定執行：全套 `pytest`、`compileall`、`git diff --check`；Skill 有改動時另跑 `quick_validate.py`。

## 6. 用封存資料做回放評估

以下全部是 `evidence_grade: replay_reference_only`：只固定已封存的 Agent 意圖重算程式，不是對 LLM 判斷的前瞻驗證，只有比賽期間的即時紀錄才算證據。

### 6.1 可用的資料

- `artifacts/decisions/` 有 2026-10-27 到 2026-11-17 共 16 個決策日的完整封存。
- 每份 `decision_input.json` 內含 150 檔、約 120 根日線，可在各自的 cutoff 內重算風險桶與 regime。

### 6.2 三種評估，各自的限制

1. **同輸入單日重算**（最可信）：對每份封存 run，用同一份輸入、同一份 Agent 輸出，只把 Policy 加上新閘門重跑配置。報告：哪些買單與配對被擋、現金與換手變化、Guard 是否通過。16 天全部要通過 Guard 才能考慮開啟。限制：每天都從真實的起始持股出發，看不到累積效果。
2. **多日滾動回放**（`cli/policy_replay.py`）：現行回放**略過 `agent_swap`**（`agent_swap: ignored_in_replay`），而高波動股正是經換股提名進場的，所以直接跑會看不到這個閘門最主要的作用。要先讓回放能讀封存的提名，或明講這份回放只涵蓋一般買單。另外，一旦持股與當時不同，後面幾天封存的意圖（對沒持有的股票給 hold／exit）就對不上，越往後越失真。
3. **階段 2 的反事實重跑**：要讓模型在舊 bundle 上重新跑交易 Agent，標 `evaluation_mode: counterfactual`。這不能證明什麼：Skill 與策略教訓是事後寫的，帶有後見之明。只用來檢查流程會不會壞，不用來估效果。

### 6.3 判讀時要一起看的數字

- 上漲日與下跌日分開統計與 0050、交易池等權的差額，不合成單一分數。
- 被擋掉的買單，之後 1／3／5 日的報酬（擋對了還是擋錯了），並列檔數。
- 現金比例是否因封頂而逼近 25%。

### 6.4 已經做的粗略診斷（不是回放，未計費稅）

用 11/17 決策的 `price_series`，逐日以前一日為止的資料算 regime 與 60 日 beta，再看隔天高 beta 那三分之一減低 beta 那三分之一的報酬差（共 58 個交易日，2026-08-20 到 2026-11-16）：

| 前一日 regime | 天數 | 隔日交易池平均 | 高減低：平均 | 高減低：中位數 | 高輸低的天數占比 | 最差一天 | 最好一天 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| bull | 15 | -0.45% | -1.15% | -0.65% | 67% | -7.1% | +2.9% |
| neutral | 32 | +0.26% | +0.29% | +0.62% | 44% | -5.2% | +5.5% |
| bear | 11 | -1.09% | -0.95% | +0.13% | 45% | -7.8% | +6.8% |

讀法：

- **regime 標籤對「隔天高波動股會不會輸」幾乎沒有預測力**。bear 那 11 天的中位數是正的，平均被 11/16 一天拉成負的；反而是 bull 標籤之後高波動股輸得最多（均線廣度是落後指標，高檔時才顯示 bull）。
- 確定的只有一件事：高波動股的單日差額落在 -7.8% 到 +6.8% 之間，**兩邊都很大**。限制它的權重會同時砍掉大賠與大賺。
- 前一日大跌（交易池 ≤ -2%，10 天）之後，隔天高減低的平均是 -0.06%、中位數 +0.13%：急跌後隔天沒有明顯方向，`shock_guard` 的價值也沒有被這份資料支持。
- 樣本只有 58 天、bear 只有一段，不能當成規律。

## 7. 風險、限制與上線時程

### 7.1 誠實的限制

1. **這個設計降低的是波動，不是提高期望報酬**。判斷對的那幾天少賠，判斷錯的那幾天少賺；第 6.4 節的資料看不出程式 regime 有預測力，Agent 的姿態有沒有預測力更是完全沒有紀錄可查。
2. **判斷錯時的損失怎麼封頂**：
   - 誤判為防守、結果大漲：持股仍在七成五以上，只有高風險桶被限制，既有持股不被強迫賣出。少賺的幅度大約是「被限制掉的權重 × 高低兩組的報酬差」。以 11/10→11/13 那段為例，兩組差約 1.4 個百分點，少放 20% 權重約少 0.3 個百分點。
   - 誤判為進攻、結果大跌：進攻列的上限仍是人填的數字，高風險桶最多就是那麼多。以 11/13→11/17 那段為例，兩組差約 12.7 個百分點，20% 權重約 2.5 個百分點。
   - 這兩個例子各只有一段行情，不是期望值；但它說明上限的作用是把「誤判為進攻」的尾端切掉。
3. **程式不賣**。這個閘門只能預防之後再買進，救不了已經在手上的高波動股。目前 beta ≥ 1 的持股約三成，開啟後第一天的效果只是「不再加」。要降下來仍靠交易 Agent 自己的 exit／trim。
4. **跌停賣不掉**。11/16 交易池有 43 檔跌停。事後才反應的規則（`shock_guard`、回撤下限、停損）遇到鎖跌停時可能根本成交不了；主辦方對跌停日的成交認定沒有查證過。
5. **急跌是一天發生的**。11/16 一天就跌 5.9%，任何「看到跌了才防守」的規則都只能保護第二天以後。
6. **與追名次的目標相衝突**。目前落後第一名約 6.5 個百分點、剩 8 個交易日。降低波動能避免名次再往下掉，但也等於放棄靠大反彈追回來的機會。要追名次需要承擔波動，這個計畫做的是相反的事。這是策略取捨，要由使用者決定，程式與 Agent 都不該替他決定。
7. **改寫轉述無法偵測**。交易 Agent 可以不寫出姿態的識別碼，卻順著姿態調整理由；這一點和現有的背景欄位一樣，只能靠「行動須由本次 claim 支持」把關。
8. **比賽進行中改配置引擎有風險**。`allocation.py` 很大，新增一條橫跨所有買進路徑的上限，出錯會讓當天決策被拒絕，退成「全部續抱」的備援 D-Plan。

### 7.2 時程（比賽 11/27 結束，剩 11/18～11/27 共 8 個交易日）

| 時間 | 工作 | 能影響幾個交易日 |
| --- | --- | --- |
| 今晚（不寫程式） | 人可以直接調既有設定降低加曝險的速度，例如把 `offense.by_view.probe.beta_stop` 調回較低的值、減少 `max_exposure_pairs`。限制：管不到 thesis 配對與一般買單。是否要做由使用者決定 | 8 |
| 11/18～11/19 | 階段 1 實作、測試、16 天同輸入單日重算 | — |
| 最快 11/19 晚或 11/20 晨的決策 | 階段 1 開啟（前提：16 天重算 Guard 全過、沒有閘門時逐位元回歸測試通過） | 約 5～6 |
| 11/20～11/23 | 階段 2 以 `front.mode: record` 實作並上線觀察 | 只紀錄，不影響配置 |
| 11/24 以後 | 階段 2 `brief` 是否開啟 | 最多 3～4 |

### 7.3 值不值得

- **階段 1**：只有在使用者的目標是「不要再出現一天賠掉一成的部位、守住現在的名次」時才值得做。它不會把落後追回來，行情急彈時會落後。若目標是追名次，不建議開。
- **階段 2**：比賽結束前不值得接上配置。專案自己的樣本門檻是每個 horizon 至少 10 個完整決策日（`calibration.MIN_DECISION_DAYS`），剩 8 天連門檻都到不了。建議只做 `record`，把「提前給姿態」的即時紀錄留下來，賽後再評估。
- **方案 C**：不做。

### 7.4 需要人決定的事

1. 目標是守名次還是追名次（決定階段 1 開不開）。
2. 風險桶的四個門檻。參考：六檔跌停股的 beta 為 1.37～1.95、ATR 占比 8.5%～10.6%；金融與電信持股的 beta 多在 0.3 以下、ATR 占比約 2.5%～3.6%。
3. 三列姿態的高風險桶上限與每日新進檔數。參考：11/17 開盤前 beta ≥ 1 的持股約 29.8%，11/11 時幾乎沒有。
4. `program_posture_by_regime.bear` 要填 `defend` 還是 `balanced`（後者保留 bear 期間參與反彈的空間，也保留這次的風險）。
5. 要不要 `shock_guard`（第 6.4 節的資料不支持它有效，建議先不開）。
6. 抄底（`quality_dip`）與反彈參與要不要受同一個上限限制（本計畫預設要）。
7. 今晚是否先用既有設定降低加曝險速度。

### 7.5 決定草案與執行順序（2026-11-17，尚未執行）

狀態：**草案，尚未執行**。以下是對 7.4 七題的建議答案，使用者還要逐題確認；確認前不改任何程式、設定或文件。

| 題 | 建議答案 | 理由 |
| --- | --- | --- |
| 1 目標 | 守名次，做階段 1 | 使用者擔心再一天大賠；本計畫不能追回落後第一名的差距 |
| 2 風險桶門檻 | 採用 7.4 的參考值（beta、ATR 占比），由本計畫填入 | 參考值取自實際跌停股與金融電信股的分界 |
| 3 上限 | 防守日高風險桶上限約 15%（暫定，實作前須再核對，數字由人填） | 11/17 開盤前高風險持股約 29.8%；Agent 不輸出數字 |
| 4 bear 對應 | `defend` | 目標是守名次 |
| 5 shock_guard | 不開 | 診斷資料看不出效果 |
| 6 抄底與反彈參與 | 同受上限 | 否則上限會被繞過 |
| 7 今晚 | 調低 `probe.beta_stop`（只改設定、可還原） | 唯一不用改程式、當天可做的降速 |

**執行順序（尚未開始）**
1. 使用者逐題確認（或指定要改的題號）。
2. 第 7 題若確認：先看 `config/decision_policy.json` 現值，改完在 `config/policy_versions/` 留回滾樣板，再更新 `docs/status.md`。
3. 階段 1：實作風險桶上限，先以 `record`／影子模式上線，再由人開啟；舊 run 重建不變。
4. 同輸入單日重算 16 天，核對上限實際會擋掉哪些買單（`replay_reference_only`）。
5. 補測試（含 fail-closed）、同步 `README.md`、`docs/architecture.md`、`docs/decision-contracts.md`、`docs/status.md`。
6. 階段 2 只做紀錄，賽後評估。

**使用者更正（2026-11-17，尚未執行）**：目標不是只在偏空時擋高風險股，而是**兩邊都調**：偏空日主動多買防守股（低波動、低 beta），偏多日多買進攻股（高波動、高 beta）。上面階段 1 只有「偏空擋高風險桶」，缺少兩件事，需另行設計並重新評估：
1. 偏空日的「防守桶優先」：同信心等級內，防守股排在進攻股之前，或為防守桶設最低占比（數字由人填）。
2. 偏多日的「進攻桶優先」：這等於依大盤看法加風險。誠實限制：診斷顯示現行 regime 標籤對隔天高波動股輸贏幾乎沒有預測力，偏多日多買進攻股的期望效果沒有資料支持；方向判錯時會放大虧損。須先同輸入重算 16 天，看這個偏向在歷史上實際會多買或少買哪些股，才決定要不要開偏多這一邊。
3. 與既有 `exposure_view`、`offense`、`rebound_participation`、`quality_dip` 的關係要重新對齊，避免互相矛盾。

上述兩項決定前，7.5 表中第 3、4 題的答案暫不採用，等重新設計後再確認。

**已知限制**：只擋之後的買進，不賣現有持股；只降波動，不提高期望報酬；上線最快 11/19 晚或 11/20 晨。
