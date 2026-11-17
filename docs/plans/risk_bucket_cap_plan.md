# 高風險股上限（`position_sizing.high_risk_cap`）：實作計畫

狀態：**計畫，尚未實作**（2026-11-17）。使用者已同意方向「做高風險股上限」，細節數字待確認。不 commit、不改程式，直到使用者說開始。

本檔是 [`market_regime_front_plan.md`](market_regime_front_plan.md) 階段 1 的精簡執行版；證據與取捨見該檔與 [`market_regime_research_report.md`](market_regime_research_report.md)。

## 1. 目標與不做的事

- **目標**：下次大盤急殺時，高波動股在帳戶裡的占比有上限，少虧一點。只降波動，不提高期望報酬，追不回落後第一名。
- **不預測大盤**：不看多空、不依 regime 放寬或收緊。三種姿態用同一組數字（等於一個固定上限）。
- **不做**：偏多日加碼進攻股（無證據）、第四位大盤分析師、風險 Agent 提前（階段 2，只做紀錄，賽後再評）、`shock_guard`、賣出現有持股。

## 2. 規則（草案，數字由人填）

1. **風險桶（程式計算，Agent 不參與）**
   - 輸入：`exposure_rotation.beta_window_days` 的 beta（對交易池等權，沿用 `BetaModel`）與 `atr_14_pct`，只用 bundle 的 `price_series`。
   - beta 或 ATR 任一達高門檻 → `high`；其餘不是 `high`（階段 1 只需二分）。
   - **缺 beta 或 ATR 一律當 `high`**，事實欄位寫 `unknown`，不補值。
   - 參考門檻（待確認）：beta ≥ 1.0、ATR 占比 ≥ 6%。實際跌停六檔 beta 1.37～1.95、ATR 8.5%～10.6%；金融電信多在 beta 0.3 以下、ATR 2.5%～3.6%。
2. **上限**：高風險桶總權重 ≤ `max_high_risk_weight`（建議 0.15；11/17 開盤前約 0.298）；每日新進高風險檔數 ≤ `max_new_high_risk_entries`（建議 1）。
3. **只擋買、不賣**：超過上限只有「不能再買、不能再加」，不賣現有持股。
4. **作用範圍**：所有買進路徑——換股提名（thesis／exposure／quality_dip 三種目的）、一般買進與加碼、反彈參與。補上 `offense.beta_stop` 只管 exposure 配對的漏洞。
5. **不改寫意圖**：只縮減或取消買單並留旗標（`RISK_BUCKET_CAP`、換股取消用 `AGENT_SWAP_CANCELLED:risk_bucket_cap`）；交易 Agent 的意圖與信心封存不變。
6. **硬規則優先**：封頂若使現金逼近 25% 上限、持股少於 20 檔或 Active Share 不足，依序把被縮的買單放回並留 `RISK_BUCKET_CAP_RELAXED:<原因>`，上限不讓 Guard 因它拒絕。

## 3. 閘門與契約

- 新版本閘門 `position_sizing.high_risk_cap`，形狀（草案）：

```json
"high_risk_cap": {
  "version": "v1",
  "mode": "record",
  "high": {"beta_at_least": "1.0", "atr_pct_at_least": "0.06"},
  "max_high_risk_weight": "0.15",
  "max_new_high_risk_entries": 1
}
```

- `mode`：`record`（只算、只封存「若開啟會擋什麼」，不改配置）→ `enforce`（真的擋）。由人設定；先 `record` 再改 `enforce`，改 `enforce` 是另一次人為決定。
- 驗證：缺欄位、多餘欄位、版本錯、數字非有限 Decimal、上限不在 (0, 單股上限×檔數] 合理範圍、前提閘門缺（`exposure_rotation`、`agent_swap` v2）→ 整份 Policy 拒絕。
- 樣板預填任何程式綁定欄位（如 `risk_bucket_facts`）即拒絕。
- 只由人設定、列入策略反思的 `FORBIDDEN_PREFIXES`（`strategy_reflection/contracts.py`）。
- `team_inputs` 不升版（維持 2.10）：多一個選配鍵 `risk_bucket_facts`，與閘門雙向檢查；Finalizer 以 bundle 與 Policy 整份重算比對（比照 `dip_facts`、`exposure_facts`）。
- 沒有此閘門的舊 Policy／舊 bundle／舊 run：配置與簡報逐位元不變。

## 4. 實作步驟與檔案（落點取自 grep，動手前須再讀對應段落）

> 注意：`decision/allocation.py` 很大，只搜尋後讀需要的行段。

| 步驟 | 內容 | 預期檔案 |
| --- | --- | --- |
| 1 | 風險桶計算與事實封存（`risk_bucket_facts`），含時間檢查：bar 晚於 `snapshot.latest_trade_date` 或可得時間缺時區／晚於 cutoff 即拒絕 | 新增 `src/etf_agent/decision/risk_bucket.py`；參考 `exposure_facts.py`、`dip_facts.py` |
| 2 | Policy 驗證與前提登記 | `decision/sizing.py`（`validate_position_sizing`）、`decision/offense.py` 的驗證寫法可參考 |
| 3 | 換股路徑：換入者為高風險且預估成交後超上限（或新進檔數已滿）→ 只取消該組、換出者不賣 | `decision/allocation.py`（換股取消處，參考 `offense_beta_stop` 約第 1507 行） |
| 4 | 一般買單封頂：優先序（信心、名次、代號）最低者先縮，向下取整張，不足一張取消；釋出預算比照 `liquidity_fit` v2 重新分配 | `decision/allocation.py`（參考 `liquidity_fit` 約第 2120 行） |
| 5 | `record` 模式：同樣的計算，結果只寫進 `risk_bucket_facts.would_cap`，不改配置 | 同上 |
| 6 | Finalizer／重建驗證 | `decision/finalization.py`、`input_validator.py` |
| 7 | 策略反思禁區 | `strategy_reflection/contracts.py` |
| 8 | 測試（`unittest.TestCase` 風格） | `tests/decision/test_risk_bucket.py` 等 |
| 9 | 文件同步 | `README.md`、`docs/status.md`、`docs/decision-contracts.md`、`docs/glossary.md`、`docs/architecture.md`、`docs/plans/README.md`、`config/policy_versions/` 回滾樣板 |

## 5. 測試清單

正常流程：高、非高各一例（beta 高但 ATR 低仍為 `high`）；換股超上限只取消該組、換出者不賣、後面未超限的組照常成交；一般買單封頂由最低優先序先縮；釋出預算流向非高風險候選；交易 Agent 意圖與 `conviction_by_symbol` 封存不變。

Fail-closed：缺 beta／ATR → `high` 且 `unknown`；未來 bar／缺時區／晚於 cutoff → 拒絕；Policy 缺欄位、多餘欄位、版本錯、缺前提 → 整份拒絕；`risk_bucket_facts` 與閘門只存在其一 → 拒絕；事實數值被改 → Finalizer 重算不符拒絕；封頂違反現金上限、最少檔數或 Active Share → 放回並留旗標、Guard 仍通過；策略反思提案碰到此前綴 → 拒絕；交易／風險 Agent 文字出現事實鍵名 → 驗證失敗。

回歸：沒有此閘門的舊 Policy 對同一份輸入，`proposal.json` 逐位元相同（至少取 11/12、11/13、11/16、11/17 四份封存 run）。

完成後依專案規定跑全套 `pytest`、`compileall`、`git diff --check`。

## 6. 驗證與上線

1. 先用 16 個決策日的封存輸入做**同輸入單日重算**，列出上限實際會擋哪些買單、會縮多少（標 `replay_reference_only`）。現行 `cli/policy_replay.py` 略過 `agent_swap`，而高波動股正是經換股提名進場，所以滾動回放要另補。
2. 以 `record` 模式上線，至少看一天的 `would_cap`。
3. 使用者確認後改 `enforce`；先存 `config/policy_versions/<日期>-before-high-risk-cap.json`。
4. 時間：剩 11/18～11/27 共 8 個交易日。最快 11/19 晚上 `record`、11/20 晨 `enforce`，實際影響約 5～6 個交易日。若來不及，寧可不開，也不要略過測試。
5. 回滾：把 `mode` 改回 `record`，或刪掉 `position_sizing.high_risk_cap`（舊 run 仍讀各自封存的 `policy.json`）。

## 7. 誠實的限制

- 只降波動，追不回落後第一名的 6.5 個百分點；急彈時會落後。
- 不賣現有持股；現有約三成高 beta 持股只能靠交易 Agent 自己的 exit／trim 降下來。
- 證據弱：外部研究只支持「波動可預測、方向難預測」，沒有 8 個交易日尺度的直接驗證；自己的資料只有一段下跌行情。
- 與既有閘門（`offense.beta_stop`、`exposure_view`、`rebound_participation`、`quality_dip`、`cash_ratio_range`）要一起核對，避免互相矛盾；實作前須逐一讀對應程式。

## 8. 待使用者確認

1. 高風險門檻：beta ≥ 1.0、ATR 占比 ≥ 6%？
2. 上限：總權重 0.15、每日新進 1 檔？
3. 先 `record` 一天再 `enforce`，還是直接 `enforce`？（建議先 `record`）
4. 反彈參與與抄底是否同受上限？（建議同受）
