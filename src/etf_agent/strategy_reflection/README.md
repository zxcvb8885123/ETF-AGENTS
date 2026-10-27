# strategy_reflection｜策略反思旁觀者

讀校準報告（信心等級與多空強度事後有沒有說中），讓 Agent 提出策略樣板旋鈕的調整提案；提案經確定性驗證與影子回放後，由程式**自動、原子地**改寫 `config/decision_policy.json`。**不屬於決策鏈**：`decision/`、`automation/` 與 `scripts/run_daily_pipeline.py` 都不匯入本套件（有測試鎖定）。

## 流程（`cli/strategy_reflection.py auto`，平日 20:30）

1. 程式用校準報告與目前樣板產生簡報 `brief`（每個統計一個 `evidence_id`；只收 `status=ok` 的 horizon）。
2. **門檻**：1 日或 5 日 horizon 任一為 `ok`（完整決策日 ≥ `calibration.MIN_DECISION_DAYS`，目前 10）才呼叫 Agent；否則封存 `observe_only` 反思，**零用量**。
3. Agent 只輸出 `{summary, observations, proposals}`（Skill：`tradeagent/risk/strategy-reflection`）；狀態、`current`、數值格式與雜湊由程式補算。
4. `validate_reflection` 重建驗證：欄位白名單、雜湊、門檻、evidence 存在、旋鈕白名單／變幅／上下限／方向、冷卻期、樣板未過期。
5. 影子回放：目前策略與候選策略各回放一次已封存決策（`evaluation.build_replay`，零 token）；候選期末 NAV 不得低於目前策略、Guard 拒絕日不得增加、至少完成一個成交日，否則不套用。回放只是必要條件，不是獲利證明。
6. 通過才原子寫入設定檔（暫存檔再 rename），新 `policy_id`＝`strategy-<日期>-r<序號>`、`available_at`＝套用時間；舊檔備份到 `var/strategy_reflection/policies/`，歷史寫入 `history.json`。任一步失敗都不改設定檔。

## 可調與禁區

| 可調旋鈕 | 單次變幅上限 | 範圍 |
| --- | --- | --- |
| `conviction_multipliers.{high,medium,low}` | 0.25 | 0.1～3，且 high ≥ medium ≥ low |
| `cash_buffer_by_stance.{aggressive,neutral,defensive}` | 0.03 | 0～0.22，且 aggressive ≤ neutral ≤ defensive |
| `positions_by_breadth.{concentrated,balanced,diversified}` | 2 檔 | 夾在 `min_positions`～`max_positions`，且集中 ≤ 平衡 ≤ 分散 |
| `drawdown_control.{neutral_at,defensive_at}` | 0.01 | 只准調低（更保守），且 neutral_at < defensive_at |
| `volatility_floor` | 0.005 | 0.005～0.05 |
| `trim_fraction` | 0.1 | 0.1～1 |

禁區（出現即整份拒絕）：單股／產業上限、現金上限、持股數、費稅、Active Share、`slippage_bps`、壓力情境、換手與成交率假設、`policy_id`／`available_at`、個股權重與股數。滑價與壓力情境不可調，因為影子回放用它們衡量候選策略，自己調自己的尺會失去意義。

## 其他保護

- 同一旋鈕套用後，再累積 `COOLDOWN_DECISION_DAYS`（10）個新決策日才可再調；每天最多套用一次。
- 鎖只在最後寫入設定檔時取（Agent 與回放可能很久，不能佔住 `artifacts/daily/.lock` 讓 21:00 決策被略過）；取鎖後重讀樣板，與反思當時的雜湊不同就不套用。鎖被佔用時反思照樣封存、不套用，之後可 `apply`。`rollback` 取鎖失敗時 exit 3。
- 自動套用不得落在台北 20:50～隔天 09:00（21:00 備案與 07:00 重做要讀到同一份策略樣板）；手動 `apply` 不受此限。Agent 呼叫逾時 600 秒。
- 每份提案至少要引用一則校準統計（`cal.h*`），只引用目前旋鈕值不算證據。Agent 原始輸出存成 `var/strategy_reflection/agent_inputs/<時間>/agent_output_raw.json`，被拒絕時另有 `rejected.json` 記原因。
- 設定檔位於 `config/`（Docker 唯讀掛載），所以本套件在主機執行，不在 Docker。`config/decision_policy.json` 已被 git 追蹤，自動改寫後是未提交修改，請自行檢視後再 commit；套用歷史與備份放在 `var/`，不受清空 `artifacts/` 影響。
- `rollback`：把最近一次套用還原為備份；設定檔的 `policy_id` 或內容雜湊與該次套用結果不同（被手動改過）則拒絕。
- `available_at` 晚於舊 run cutoff 的設定不會回填到舊決策。

## 檔案

| 檔案 | 職責 |
| --- | --- |
| `contracts.py` | 常數、可調旋鈕表（`KNOBS`）、禁區、`ReflectionError` |
| `tools.py` | 旋鈕讀取、單筆提案檢查、套用提案、樣板預檢（沿用 `validate_position_sizing`） |
| `brief.py` | 校準報告與樣板 → 帶 `evidence_id` 的簡報與門檻 |
| `validator.py` | 反思重建驗證 |
| `repository.py` | 不可變反思封存、原子寫檔、套用歷史（`var/strategy_reflection/`） |
| `service.py` | Agent 任務、反思建構、影子回放閘門、`apply_reflection`、`rollback`、`run_auto` |

## 限制

- 目前只有少量決策日，統計樣本小；10 日門檻不代表統計顯著，Agent 被要求「不確定就不提案」，但最終保護是變幅上限、回放閘門與冷卻期。
- 20 日 horizon 在決策日少時不會 `ok`，因此不作為門檻。
- 影子回放用已封存的 Agent 輸出換 Policy，持股路徑與真實執行會分歧；5／20 日重疊且樣本少時，回放比較只看方向。
