# 現金姿態轉向事實（`position_sizing.cash_stance_turn_facts`）：只紀錄、不立規則

狀態（2026-11-09）：**v1 已實作，並已在 `config/decision_policy.json` 開啟**（人決定；2026-11-10 07:00 的決策起生效）。它只是紀錄，不改任何配置；明顯有害由人刪掉該欄位即回滾。觀察條件：至少 10 個決策日，且出現至少一次真的由 bear 轉出。

## 起因

2026-11-09 的研究結論：

- 現金姿態這條路本身慢，改善上限約 1% NAV。
- 領先指標（池報酬、站回短均線等）的 5 日假訊號率約 20%～50%。
- 可比較的轉向樣本只有 5～13 次，不足以立任何規則。

所以只做**紀錄**：程式把一組事實放進風險 Agent 簡報，Agent 多填一個列舉記錄自己怎麼看，累積真實樣本。沒有任何訊號決定現金下限、姿態或配置。

## 做法

- `decision/turn_facts.py` 的 `turn_facts(bundle)` 只依 `price_series` 與 `snapshot.latest_trade_date` 重算交易池層級數字（字串或整數）：最近兩日池等權日報酬、連續上漲日數、站回 MA5／MA20 比例、MA20／MA60 之上比例（呼叫 `MomentumEngine._regime`，不另寫定義）、MA20 之上比例 5 日變化、20 日新低比例、regime 連續 bear 天數。資料不足記 `data_gaps`，bar 晚於最近交易日即拒絕。
- 風險 Agent 多填 `turn_assessment`：`no_turn_evidence`／`turn_unconfirmed`／`turn_confirmed`。驗證只看必填與值域，列舉與姿態之間不設對應。
- Skill 與簡報 note 只保留一般性提醒：這些是事實不是訊號；均線廣度可能落後短期變化，反彈也可能失敗。不得硬寫特定日期案例、後續報酬或固定費稅數字，避免把 cutoff 之後的資訊帶進歷史重跑。
- 不改 `MomentumEngine._regime`、不動現金格點、`cash_ratio_range`、`stance_floor` 與 Guard；`team_inputs` 不升版。

## 觀察期與要蒐集的指標

- 觀察期：至少 10 個決策日，且至少經歷一次真實 regime 離開 bear。
- 蒐集：每日 `turn_facts` 全部欄位、`turn_assessment`、當日姿態與 `cash_grade`、之後 1／3／5 個交易日的池等權報酬與帳戶 NAV 變化。
- 事後才評估 `turn_assessment` 與後續報酬的關係；證據等級只有比賽期間的即時紀錄，重放不算。

## 開啟與回滾

- 開啟：在 `config/decision_policy.json` 的 `position_sizing` 加 `"cash_stance_turn_facts": {"version": "v1"}`（需 `typed_evidence_v1`，可獨立於 `cash_stance_recency`）。
- 回滾：刪除該欄位即可，舊 Policy／舊 run 逐位元不變；策略反思不得改此欄位。
