# strategy_reflection｜策略反思旁觀者

讀校準報告（信心等級與多空強度事後有沒有說中），讓 Agent 提出策略樣板旋鈕的調整提案；提案經確定性驗證與影子回放後，由程式**自動、原子地**改寫 `config/decision_policy.json`。**不屬於決策鏈**：`decision/`、`automation/` 與 `scripts/run_daily_pipeline.py` 都不匯入本套件（有測試鎖定）。

## 每週節奏

- **週五 19:30**（`auto --no-apply`）：週五的收盤資料（20:00 更新）與 21:00 決策都還沒進來，所以報告涵蓋到週四；程式產生**診斷包**（`diagnostics.py`）與**每週反思報告**（`report.py`，`var/strategy_reflection/reports/weekly-<日期>.md`），並封存反思。樣本不足時不呼叫 Agent（零用量），報告仍然產生，每個指標列出天數、筆數與門檻並標「樣本不足，不下結論」。排程固定加 `--force`，所以每週都會出報告；手動 `auto` 距上一份不到 6 天仍會略過。
- **週六 10:00**（`apply-pending`）：套用週五封存、尚未套用且 8 天內的提案，避開 20:50～09:00 的封鎖窗；週日 21:00 是第一個讀到新策略的決策。
- 比賽初賽到 11/27，只有少數幾個週五有足夠樣本；窗口與冷卻期都依這個時程設定。

## 經驗教訓（回頭看過去買進，再改做法）

每週反思 Agent 的第三件事（`lessons.py`，規則見 Skill `strategy-reflection` 的 `references/lessons.md`）：讀只含數值與列舉的持股整理（`thesis.*` 聚合與 `holdings`，不含理由或新聞文字），歸納最多 3 條一般性做法建議。每條 10～120 字、至少引用一個簡報 evidence_id、不得含個股代號／年份／識別碼／網址（`decision/strategy_lessons.py` 驗證）。

- 週五封存提案到 `var/strategy_reflection/lessons/pending.json`；週六 `apply-pending` 在旋鈕沒套用時，經取鎖與 20:50～09:00 保護，寫入 `config/strategy_lessons.json`（先備份），歷史記 `lessons_apply`。
- 交易 Agent 在每日 brief 的 `strategy_lessons` 讀到（`decision/strategy_lessons.py` 載入；檔案缺失、雜湊不符或任一條不合格都視為沒有教訓，不影響決策）。它只是背景：不是證據、不可引用、不覆蓋硬性規則。
- 套用後同樣 5 個交易日試用觀察期；教訓回放驗不了，所以走 observe 規則（看不出差別延長一次，仍看不出差別就還原）。試用期間不整理新教訓、不套用旋鈕提案。連續 2 次還原會自動關閉自動套用。
- 人工還原：`.venv/bin/python cli/strategy_reflection.py lessons-rollback`（現有檔被手動改過時不動）。

## 開關、試用觀察期與累積偏移

- `config/strategy_reflection.json`：`enabled`＝false 整體停用（只產報告）；`auto_apply`＝false 為人工模式（提案只封存，用 `apply` 手動套用）。缺檔視為全開，格式錯誤 fail-closed 全關。連續 2 次試用被還原會自動把 `auto_apply` 改為 false。
- 套用後的**試用觀察期**（`trial.py`）：比較套用前最近 10 個交易日與套用後新交易日的「帳戶日報酬 − 等權基準日報酬」平均；套用日之後 5 個交易日起算，變差超過 max(基準期標準差的一半, 0.002) 才還原、變好超過同一門檻才保留；落在門檻之內（看不出差別）或基準期少於 4 日時延長 3 日一次，仍看不出差別：回放驗得了的旋鈕保留，停損／停利門檻（`position_review.*`）還原。還原走 `rollback`（取每日鎖、避開 20:50～09:00、設定檔被人改過就不動），由週六 `apply-pending` 執行。觀察期未結案時禁止新提案。這是護欄不是獲利證明，比賽只有 24 個交易日。
- 每個旋鈕離「人設定的起點」最多累積偏離 2 個 step（`knob_origins` 由 history 的 `origins` 計算，回滾會抵銷）；單次變幅已縮小（乘數 0.10、現金 0.02、檔數 1、`volatility_exponent` 0.10、換股 1 檔、`trim_fraction` 0.05）。

**買進論點追蹤**（`thesis.py`，報告首節）：每檔持股列買進日、預估成交價、至今報酬、期間最大回落、買進後的交易意圖與警示次數，依容忍度（policy 的 `position_review.loss_alert`）分成「照理由走／短期回檔在容忍範圍內／請回頭檢查」；報酬用官方成交均價，買進後有除權息的不算報酬。交易 Agent 當時寫的理由與重估條件只放在週報給人讀，不進診斷包與反思 Agent 的簡報。

**論點檢視**（`thesis_review.py`，Skill `strategy-reflection` 的 `references/thesis-review.md`）：每週由同一個反思 Agent 另做一件事，逐檔（10 檔一批）對照「當初的重估條件」與最新決策日同一檔的分析師發現／多空 claim，逐條判斷 triggered／not_triggered／unknown，整體 intact／weakened／broken／unknown。只能引用該檔最新的 finding／claim ID；觸發必須引用 ID、broken 至少要有一個觸發條件；價格事實只是背景，短期波動不算理由失效。Agent 失敗或輸出沒過驗證的批次標 unknown，不中斷反思；結果存 `agent_inputs/<時間>/thesis_review.json`，只進週報，不進簡報、policy 或交易。

診斷包只含數值與列舉（分析師與交易判斷的命中率、賣出之後的走勢、換手與費稅拖累、警示與回應次數、現金姿態與廣度分布），**不含新聞、claims、findings 等自由文字**，簡報因此不會被不受信任的內容誘導；只有 `status=ok` 的指標成為 `diag.*` 證據。與 0050／等權基準的比較尚未接入。

## 流程（`cli/strategy_reflection.py auto`）

1. 程式用校準報告與目前樣板產生簡報 `brief`（每個統計一個 `evidence_id`；只收 `status=ok` 的 horizon）。
2. **門檻**：1 日或 5 日 horizon 任一為 `ok`（完整決策日 ≥ `calibration.MIN_DECISION_DAYS`，目前 10）才呼叫 Agent；否則封存 `observe_only` 反思，**零用量**。
3. Agent 只輸出 `{summary, observations, proposals}`（Skill：`tradeagent/risk/strategy-reflection`）；狀態、`current`、數值格式與雜湊由程式補算。
4. `validate_reflection` 重建驗證：欄位白名單、雜湊、門檻、evidence 存在、旋鈕白名單／變幅／上下限／方向、冷卻期、樣板未過期。
5. 影子回放：目前策略與候選策略各回放一次已封存決策（`evaluation.build_replay`，零 token）；候選期末 NAV 不得低於目前策略、Guard 拒絕日不得增加、至少完成一個成交日，否則不套用。回放只是必要條件，不是獲利證明。
6. 通過才原子寫入設定檔（暫存檔再 rename），新 `policy_id`＝`strategy-<日期>-r<序號>`、`available_at`＝套用時間；舊檔備份到 `var/strategy_reflection/policies/`，歷史寫入 `history.json`。任一步失敗都不改設定檔。

## 可調與禁區

| 可調旋鈕 | 單次變幅上限 | 範圍 |
| --- | --- | --- |
| `conviction_multipliers.{high,medium,low}` | 0.10 | 0.1～3，且 high ≥ medium ≥ low |
| `cash_buffer_by_stance.{aggressive,neutral,defensive}` | 0.02 | 0～0.22，且 aggressive ≤ neutral ≤ defensive |
| `positions_by_breadth.{concentrated,balanced,diversified}` | 1 檔 | 夾在 `min_positions`～`max_positions`，且集中 ≤ 平衡 ≤ 分散 |
| `drawdown_control.{neutral_at,defensive_at}` | 0.01 | 只准調低（更保守），且 neutral_at < defensive_at |
| `volatility_floor` | 0.005 | 0.005～0.05 |
| `trim_fraction` | 0.05 | 0.1～1 |
| `volatility_exponent` | 0.10 | 0～1 |
| `rotation.max_swaps_per_run` | 1 檔 | 0～10 |
| `rotation.min_new_conviction` | 相鄰一階 | low／medium／high |
| `position_review.loss_alert`／`retrace_alert_20d` | 0.01 | 0.05～0.20／0.06～0.25，影子回放驗不了，一週最多一個，走試用觀察期 |

禁區（出現即整份拒絕）：單股／產業上限、現金上限、持股數、費稅、Active Share、`slippage_bps`、壓力情境、換手與成交率假設、`policy_id`／`available_at`、個股權重與股數。滑價與壓力情境不可調，因為影子回放用它們衡量候選策略，自己調自己的尺會失去意義。

## 其他保護

- 同一旋鈕套用後，再累積 `COOLDOWN_DECISION_DAYS`（10）個新決策日才可再調；每天最多套用一次。
- 鎖只在最後寫入設定檔時取（Agent 與回放可能很久，不能佔住 `artifacts/daily/.lock` 讓 21:00 決策被略過）；取鎖後重讀樣板，與反思當時的雜湊不同就不套用。鎖被佔用時反思照樣封存、不套用，之後可 `apply`。`rollback` 取鎖失敗時 exit 3。
- 自動套用不得落在台北 20:50～隔天 09:00（21:00 備案與 07:00 重做要讀到同一份策略樣板）；手動 `apply` 不受此限。Agent 呼叫逾時 600 秒。
- 每份提案至少要引用一則校準統計（`cal.h*`）或診斷統計（`diag.*`），只引用目前旋鈕值不算證據。Agent 原始輸出存成 `var/strategy_reflection/agent_inputs/<時間>/agent_output_raw.json`，被拒絕時另有 `rejected.json` 記原因。
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
