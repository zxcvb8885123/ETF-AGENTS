# 市場轉向時的持股曝險調整（`position_sizing.exposure_rotation`）：設計計劃

狀態（2026-11-09）：**v1 已實作（commit `df9a884`），端到端測試已補（`tests/decision/test_exposure_rotation_e2e.py`），並已在 `config/decision_policy.json` 開啟**（人決定；數字用本文建議值，但 `max_rotation_fraction` 取 0.08）。開啟後從 2026-11-10 07:00 的決策起生效；明顯變差由人刪除該欄位即回滾。開啟後至少觀察 10 個決策日（`calibration.MIN_DECISION_DAYS`）。實作與本文不同處：持有天數用台北時區日曆日；交易 Schema 不依閘門切換（hold 信心由驗證器與提示詞要求）；`EXPOSURE_ROTATION_SKIPPED` 旗標的 symbol 用 `PORTFOLIO`。所有閘門只由人設定，策略反思不得改，沒有欄位的舊 Policy／舊 run 逐位元不變。本文取代 [信心換股計劃](conviction_swap_plan.md)（併入本文）。「事實」來自封存資料或程式；「推論」另外標示。

## 1. 要解決的問題

使用者的原意：目前報酬贏大盤，是因為剛好遇到大跌、持股又全是低波動股；**問題是市場反轉時吃不到大盤的漲幅，更別說超額報酬**。要的是系統能判斷市場方向，覺得市場轉變時調整持股，而不是一直抱著金融股這類低波動股。

## 2. 現況（事實，封存資料到 2026-11-06 收盤）

- 帳戶 NAV 自開帳 −5.45%；加權指數 10-27→11-06 −13.87%；交易池 150 檔等權 −17.35%。**11-09 當日收盤資料不在封存**，使用者說的當日大漲不當數字使用。
- 持股 beta（對交易池等權，60 日）：含現金整體 **0.11**；金融 8 檔（佔 NAV 25.8%）0.02；beta < 0.3 的持股佔 **62.0%** NAV。
- 唯一上漲日 10-28：帳戶 −0.19%，加權指數 +2.04%（1 個樣本，只是示意）。
- 金融股 `exit` 0 次、`trim` 2 次；23 個 run 只有 1 筆金融股賣單。

## 3. 根因（推論，已抽查程式）

1. **現金姿態只動現金，不動持股組成**：defensive 18% 轉到 neutral 8% 最多釋出 10% NAV，而且只能買交易 Agent 當天標 buy／add 的股票。
2. **回撤減碼單向**：`drawdown_derisk` 依 ATR 由高到低賣高波動股（11-04 一次賣掉 8 檔電子／材料），姿態回到 neutral 時沒有對稱的買回，beta 被「棘輪」到 0.1。
3. **沒有角色擁有組合層級曝險**：風險 Agent 只動現金；交易 Agent 分批、逐檔，且跑在風險 Agent 之前；「市場參與度低」不是個股理由不成立。
4. 賣出依據比想像的軟（只採納 `position_id` 就能 trim／exit），但 Skill 給的標準（理由成立、相對交易池強）在防守贏家身上永遠通過。

## 4. 方向判斷（事實與推論）

- 現有 regime（`momentum.py` 的池廣度）是趨勢確認器不是轉折偵測器：全池同步上漲，bear→neutral 要約 3～5 個交易日（+15% 以上），到 bull 要超過兩週。不改它（動它會讓舊 run 重建失敗）。
- 07:00 能用的資料是 T−1 收盤與隔夜；**盤中轉向最早在下一個決策日才看得到**，至少落後一個交易日。
- 證據很少：`cross_market_study` 只支持隔夜訊號預測開盤跳空；比賽期間 9 個交易日、1 個上漲日、風險 Agent 從未選過 aggressive。沒有證據支持能提前辨認轉向。
- **設計原則：不押注判斷對，押注判斷錯也付得起**：每次換手有上限、有遲滯、方向對稱、不由單一程式訊號自動觸發。誰判斷方向：**維持風險 Agent**（既有現金姿態列舉，已有 typed evidence、回撤下限與 `cash_stance_recency` 護欄）；程式只做對映與執行；LLM 不輸出任何數字。

## 5. 方案與推薦

| | A：組合曝險目標（推薦） | B：市場方向帶進交易 Agent | C：信心換股加條件 |
| --- | --- | --- | --- |
| 判斷者 | 風險 Agent（既有列舉） | 交易 Agent（看不到組合、先於風險 Agent 執行） | 程式 regime（落後 3～10 天） |
| 換手 | 有上限（每次 ≤ 2 檔、≤ 8% NAV） | 不可控 | 低但金融股多半被保護 |
| 假反彈代價 | 每次增加有限 beta | 可能一天拉滿 | 小，效果也小 |

**推薦 A，並吸收 B 的事實部分與 C 的機制**：每檔 beta 進持股事實；hold 也給信心（保護與排序）；換出換入全由程式做，觸發由風險 Agent 的現金姿態對映。不寫任何產業規則（今天低 beta 是金融與電信，明天可能是別的）。

## 6. 規則

### 6.1 閘門與 Policy 欄位

```json
"position_sizing": {
  "exposure_rotation": {
    "version": "v1",
    "beta_window_days": 60,
    "beta_floor_by_stance": {"aggressive": "0.90", "neutral": "0.55", "defensive": null},
    "release_margin": "0.10",
    "max_swaps_per_run": 2,
    "max_rotation_fraction": "0.08",
    "min_holding_days": 5,
    "min_new_conviction": "medium",
    "hold_conviction": "v1",
    "conviction_gap_swap": "v1"
  }
}
```

- 數字只由人設定。`beta_floor_by_stance` 是**含現金的整體 beta 下限**（對交易池等權）；`defensive: null` 表示不觸發；回撤下限 `stance_floor` 照舊先約束姿態。
- 為什麼下限不是 0.70：使用者要吃到接近大盤的漲幅，進攻狀態下限 0.90；含現金 3～8% 時，投資部位 beta 約 1.0。中性 0.55 是折衷。這些值上線前由人再決定，程式不寫死。
- `release_margin`：遲滯，beta 低於下限才觸發、補到「下限 − margin」以上就停，避免每日來回。
- 前提（登記於 `sizing.GATE_PREREQUISITES`）：`rotation`、`held_add_priority`、`selection_ranking`、`position_review`、`reentry_cooldown`、`turnover_fit`、`cash_buffer_floor`；`hold_conviction` 必須與 `exposure_rotation` 同時出現；`FORBIDDEN_PREFIXES` 加 `exposure_rotation`。

### 6.2 確定性模組 `decision/exposure.py`

- `build_exposure_review(bundle, policy)`：每檔 `beta_60d_vs_pool`、`up_capture_60d`、`down_capture_60d`（覆蓋不足為 `null`，不補值）；組合層級 `portfolio_beta`（含現金）、`invested_beta`、`low_beta_share`（beta < 0.3 的 NAV 比例）。全部 `Decimal`；**不輸出任何市場狀態或轉向判斷**。
- `validate_exposure_review`：整份重算比對（範本 `position_review.py`）。
- `exposure_rotation_plan`：純函式，`allocation.py` 與 Finalizer 共用。

換股規則（全部成立才換）：
1. 最終姿態在 `beta_floor_by_stance` 有非 null 下限，且 `portfolio_beta` 低於下限。
2. **換入**：交易 Agent 本次 `buy`、未持有、信心 ≥ `min_new_conviction`、被 `MAX_POSITIONS` 擋下、beta ≥ 組合 beta + `release_margin`、可交易、不在冷卻名單；順序沿用既有入選排序。
3. **換出**：交易 Agent 標 `hold`（不動 add／trim／exit／no_trade）、hold 信心非 `high`、持有 ≥ `min_holding_days`、可交易、不在被留住的出場名單、beta 低於組合 beta；順序：信心 low 優先，再 beta 由低到高，再代號。
4. 逐對配對直到：達 `max_swaps_per_run`、累計賣出市值 ≥ `max_rotation_fraction × NAV`、名額用完、或預估換後 beta ≥ 下限 − margin。現金可行性沿用 `_forced_swap_victims` 的 room／proceeds 檢查。
5. 賣單意圖由程式標 `exit`，旗標 `RISK_EXPOSURE_ROTATION`（換入者 `EXPOSURE_ROTATION_FILL`）；不改寫交易 Agent 的原意圖；被換出者進隔日 `reentry_cooldown`。不觸發時留 `EXPOSURE_ROTATION_SKIPPED:<原因>`。
6. 名額優先序：交易 Agent 的 exit／trim ＞ `forced_swap` ＞ 曝險換股 ＞ 信心差換股。與 `drawdown_derisk` 互斥（防守不觸發）。
7. `conviction_gap_swap`：新股 `high` 對持股 `low`，不看姿態，同一套上限，可與曝險觸發獨立開關。

「不得為騰名額而賣」改寫為「不得**純粹為了買別檔**而賣」：交易 Agent 分批工作，評持股時看不到別批的新股，所以持股信心不是為了買別檔而給。

### 6.3 hold 信心

閘門開啟時交易 Agent 對 `hold` 也必填 `conviction`（意思：以目前資訊，這檔還值得佔一個位置的程度）；沒開時 hold 帶信心被拒（現行）。市場參與度低不是賣出理由，但持股的 beta 與 up／down capture 當作持股事實給它，供它決定信心。

## 7. 接線

| 位置 | 改動 |
| --- | --- |
| `decision/sizing.py` | 登記 `exposure_rotation`；形狀驗證（數值範圍、floor 遞增、`defensive` 只能 null、`held_since_by_symbol` 由 pipeline 綁定且不得預填） |
| `decision/exposure.py` | 新檔 |
| `automation/daily_pipeline.py` | `position_review` 之後建 `exposure_review`；`bind_holding_days` 綁持有天數進 Policy（做法同 `reentry_cooldown`，由帳本算） |
| `automation/team_stages.py`、`tradeagent/trading/trader/` | 閘門開啟時持股附 beta 事實（背景、不可引用）；hold 必填信心；Skill 改寫 |
| `automation/agent_schemas.py`、`decision/trader.py` | 交易 Schema 依閘門切換；舊 `TRADE_SCHEMA` 物件不動 |
| `decision/allocation.py` | `_forced_swap_victims` 之後加曝險換股；留住邏輯比照 `RISK_FORCED_SWAP` |
| `decision/finalization.py` | `team_inputs` 升 **2.7**（= 2.6 ＋ `exposure_review`，須與閘門同時存在）；重算並比對 `exposure_rotation_plan` 與旗標；`held_since` 以帳本獨立重算，讀不到即拒 |
| `evaluation/replay.py` | 舊 run 無 hold 信心與持有天數：回放不換，notes 標 `exposure_rotation: ignored_in_replay` |
| `strategy_reflection/contracts.py` | `FORBIDDEN_PREFIXES` 加 `exposure_rotation` |
| 文件 | `AGENTS.md`、`docs/decision-contracts.md`、`docs/status.md`（未開啟）、`docs/glossary.md`、`docs/architecture.md`（team_inputs 2.7）、`README.md` |

`team_inputs` 必須升版：`exposure_review` 是整池的新程式產物，不在 trade decision 雜湊內，要像 `position_review` 一樣另存並重算。

## 8. 測試（`unittest.TestCase` 風格）

- 無閘門：Schema、提示詞、簡報、配置結果、team_inputs 版本逐位元不變；hold 帶信心被拒；缺 `hold_conviction` 被拒。
- 有閘門（fail-closed）：`exposure_review` 竄改或缺檔、`held_since_by_symbol` 預填或與帳本不符或帳本讀不到，Finalizer 全部拒絕；hold 缺信心被拒。
- 觸發與遲滯：defensive 或 beta 已達標不換；`下限 − margin` 邊界。
- 排除：add／trim／exit／no_trade、hold high、持有天數不足、不可交易、被留住的出場、beta 不低於組合；換入的對應反例。
- 上限：檔數、`max_rotation_fraction`、名額優先序、`turnover_fit`、`min_positions`、`sector_fit`；**不得出現只賣不買**。
- 與 `drawdown_derisk` 互斥；換出者次日在 `reentry_cooldown`；回放舊 run 不換。
- `test_gate_prerequisites.py` 與 `test_design_guards.py` 登記新閘門。

## 9. 驗證與回滾

- **離線只能驗程式正確性與不變性**：封存姿態 10-27～10-29 neutral、之後幾乎全 defensive，觸發次數接近 0；舊 run 沒有 hold 信心；樣本遠低於 10 個決策日門檻（`calibration.MIN_DECISION_DAYS`）。可用封存 bundle 重算 `exposure_review` 看 beta 軌跡，驗證根因 2。
- **開啟後觀察**（週報）：每日 `portfolio_beta`、`low_beta_share`、姿態、換股清單；換股對事後 5／10 日報酬差與費稅；池漲逾 1% 與跌逾 1% 兩類日的帳戶對交易池報酬差；假反彈計數（觸發後 5 日內姿態切回 defensive）。
- **回滾**（人工刪欄位）：10 個決策日內換股累計淨貢獻 < −0.5% NAV，或假反彈 ≥ 3 次，或任一 run 因本閘門被 Guard 拒絕兩次。
- **風險**：換股來回費稅約 0.585%；假反彈時 beta 變高跌得更多；真轉向時每天最多補約 0.1 beta，10% 反彈仍少賺。這個設計降低「完全吃不到」的風險，不保證追上大盤。

## 10. 需要人決定

1. **哪些姿態觸發**：推薦 `neutral` 也觸發（目前回撤 5.4% 把下限鎖在 neutral，風險 Agent 從未選過 aggressive）。
2. **換出保護強度**：推薦只排除 hold `high`；連 `medium` 也排除，金融股會全被保護，效果近零。
3. **beta 基準**：推薦對交易池等權（與 `position_review` 同口徑，資料已在 bundle）；另抓加權指數要新增來源與授權審查，且 2330 佔指數權重高，對映不乾淨。
4. 上線前確認 `beta_floor_by_stance` 的數字。
