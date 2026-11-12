# 設計審查修正計畫（2026-11-11）

本文件只有計畫，尚未實作任何一項。來源是 2026-11-11 的唯讀設計審查（發現 A～H），每一項都已在撰寫當下開檔核實；核實結果與原發現不同之處寫在各項的「核實」與第 7 節。

實作者請先讀 [`AGENTS.md`](../../AGENTS.md)，再讀本文件第 1、2 節，然後只做被指派的那一項。

## 1. 目標、範圍外與全域限制

### 1.1 目標

1. 補上多空研究員隔離的漏洞（A），這是唯一會讓當天決策的獨立性失效的問題，最優先。
2. 讓「Agent 不輸出數字」「背景可重算」「時間點一致」三條原則與程式實際行為一致（B、C、E、F、G）。
3. 讓回放與文件說真話（D、H）。

### 1.2 範圍外

- 不改任何現行配置行為、不開啟任何版本閘門。新閘門只「實作、不開啟」，由使用者自行寫進 `config/decision_policy.json`。
- 不重做既有回放、不改寫既有 run 或封存。
- 不處理 Codex 無法強制拒讀的根本限制（只補文件與啟動前檢查）。
- 不改 `src/etf_agent/decision/allocation.py` 的配置邏輯（B 的 `special_weight_limits` 互動只改說明文字，見 4.1）。

### 1.3 全域限制（每一項都適用）

- **不得編輯 `config/decision_policy.json`**：使用者正在改這個檔。需要新閘門時，實作者只寫程式、測試與文件，並在回報中列出「要由人加進設定的 JSON 片段」。
- **舊 run 逐位元不變**：沒有新欄位的舊 Policy／舊 bundle／舊 run，重建結果必須與現在完全相同。任何會讓既有 run 重建失敗或結果不同的驗證收緊，都必須放在版本閘門後面，或只放在 pipeline 的即時檢查（不進 Finalizer 的重建路徑）。
- **版本閘門只由人設定**，並須列入策略反思禁區（比照 `valuation_facts` 在 `src/etf_agent/decision/sizing.py` 與 `src/etf_agent/strategy_reflection/contracts.py` 的登記方式）。
- **不 commit、不 merge、不 push**。修改保留為未提交狀態，只回報。
- **工作區有他人進行中的修改**（撰寫時 `git status` 顯示）：`src/etf_agent/automation/team_stages.py`、`src/etf_agent/decision/trader.py`、`src/etf_agent/decision/allocation.py`、`src/etf_agent/decision/position_review.py`、`src/etf_agent/strategy_reflection/contracts.py`、`tests/automation/test_daily_pipeline.py`、`tests/decision/test_exposure_rotation.py`、`tradeagent/trading/trader/` 下兩個檔。不得還原或覆蓋這些修改；要改這些檔的項目（A2、C、F）開工前先 `git diff <檔>` 看清現況，只加自己的段落。
- **行號會漂移**：本文件的行號是 2026-11-11 撰寫時的位置。實作時一律先用函式名或關鍵字搜尋定位，不要照行號盲改。
- **不要整份讀 `allocation.py`**：先搜尋，再讀行段。
- **測試風格**：`unittest.TestCase`（`setUp`、`self.assertEqual`／`assertIn`／`assertRaises`），不寫純函式加 `assert`。每項都要有正常流程與 fail-closed 分支。
- **驗證指令**（每項完成後都要跑，見 `AGENTS.md`「測試與驗證」）：

```bash
PYTHONPYCACHEPREFIX=/tmp/etf-agent-pycache .venv/bin/python -m pytest
PYTHONPYCACHEPREFIX=/tmp/etf-agent-pycache .venv/bin/python -m compileall -q src tradeagent tests
git diff --check
```

- 文字（錯誤訊息、文件、Skill）用繁體中文；識別字用英文。Canonical JSON、雜湊、時間與 Decimal 解析一律用 `etf_agent.core`。

## 2. 執行順序與可否平行

### 2.1 項目總表

| 編號 | 內容 | 優先 | 需要人先決定 | 主要改動檔 |
| --- | --- | --- | --- | --- |
| A1 | run 目錄內後段產物拒讀清單補漏 | P0 | 否 | `automation/agent_runner.py` |
| A2 | 專案層級拒讀路徑（報告、封存、反思產物、紀錄） | P0 | 否 | `automation/agent_runner.py`、`automation/team_stages.py` |
| A3 | OpenCode 跨 run 拒讀；Codex 只補文件 | P1 | 否 | `automation/agent_runner.py`、`automation/README.md` |
| A4 | trace 補記非 Read 工具；明確禁用 Grep 等工具 | P0 | 否 | `automation/agent_runner.py` |
| B | 風險修正動作的數值 | P1 | **是（4.1）** | `decision/risk.py`、`automation/agent_schemas.py`、`automation/risk_stages.py`、`decision/sizing.py` |
| C | 交易背景（教訓、論點檢視）進 `team_inputs` | P1 | **是（4.2）** | 新檔 `decision/trader_background.py`、`decision/finalization.py`、`automation/team_stages.py`、`automation/daily_pipeline.py`、`decision/sizing.py` |
| D | 回放是樣本內評估：補揭露 | P2 | **是（4.3）** | `strategy_reflection/service.py`、`strategy_reflection/report.py` |
| E1 | 交易池依 cutoff 過濾 | P2 | 否 | `data/snapshot_repository.py`、`data/snapshot.py` |
| E2 | `ex_rights_symbols` 補 `fetched_at <= as_of` | P2 | 否 | `calibration/prices.py` |
| F | 背景識別碼禁令：文件說真話＋即時照抄檢查 | P2 | 否 | `decision/trader.py`、`automation/team_stages.py`、文件 |
| G1 | 正式入口缺 `nav_history` 時拒絕 | P1 | 否 | `automation/daily_pipeline.py`、`scripts/run_daily_pipeline.py` |
| G2 | 硬規則數字兩個來源的一致性測試 | P1 | 否 | 只加測試 |
| H | 文件與設定不一致 | P2 | 否 | `AGENTS.md`、`docs/` |

共 13 項。

### 2.2 順序

1. **第一批（立即，P0）**：A1 → A4 → A2，同一位實作者依序做（都改 `agent_runner.py`）。
2. **第二批（可與第一批平行）**：G2、E2、E1 互不相干，可各自平行。
3. **第三批**：A3（等第一批完成，同檔）；G1（可與 A3 平行）。
4. **第四批（等人決定後）**：B、C、D。B 與 D 可平行；C 要等 A2 與 G1 完成（同檔），且要等他人對 `team_stages.py` 的修改落定。
5. **第五批**：F（等 C 完成，同樣改 `team_stages.py` 交易段與 `trader.py`）。
6. **最後**：H（所有項目的文件更新都完成後再做，避免同一段文件被改兩次）。

### 2.3 不可平行的組合（會改同一檔）

- `automation/agent_runner.py`：A1、A2、A3、A4 必須依序。
- `automation/team_stages.py`：A2、C、F 必須依序，且與他人進行中的修改錯開。
- `automation/daily_pipeline.py`：G1、C 必須依序。
- `decision/sizing.py`：B、C 都要登記新閘門，必須依序（B 先）。
- `strategy_reflection/contracts.py`：B、C 登記禁區，必須依序，且此檔有他人進行中的修改。
- `AGENTS.md`、`docs/status.md`、`docs/decision-contracts.md`：B、C、D、F 各自只「新增一段」；H 最後統一修正舊敘述。

## 3. 各項修正

### A1　run 目錄內後段產物拒讀清單補漏（P0）

**問題與證據（已核實）**

- `src/etf_agent/automation/agent_runner.py:70-81` 的 `LATER_STAGE_GLOBS` 有 `brief_release_order*`，沒有 `brief_swap_nomination*`。
- `src/etf_agent/automation/team_stages.py:160-163` 的 `_peer_deny_reads` 只回傳「對方的 `<peer>_*`、`brief_<peer>_*`」加 `LATER_STAGE_GLOBS`。
- 實測 `artifacts/daily/2026-11-11/daily-20261110-233102/` 內含空頭 claim 識別碼的檔案：`brief_swap_nomination.json` 在內、不在拒讀清單。同一天第二次 run 的研究員可讀前一次 run 的這個檔（Claude 的規則是 `Read(**/<樣式>)`，跨目錄生效，所以清單漏列就是漏擋）。
- 同屬後段產物、同樣沒列：`brief_selection_ranking*`（含交易 Agent 的意圖與信心）、`ranker_*`（入選排序嘗試紀錄）。

**修法**

在 `LATER_STAGE_GLOBS` 加入三個樣式，各附一行註解說明原因：

```python
"brief_swap_nomination*",  # 換股提名簡報含雙方 claim 全文，研究員不得讀
"brief_selection_ranking*", "ranker_*",  # 入選排序的簡報與嘗試紀錄含交易 Agent 意圖，研究員不得讀
```

注意：`LATER_STAGE_GLOBS` 同時被 `pipeline_base.py:95` 的 `RESEARCH_RESIDUE_GLOBS` 使用（Codex 暫存的殘留偵測）。上面三種檔都在多空研究之後才產生，加入後殘留偵測的語意不變。**不要**把 `pipeline.json`、`decision_input*`、`brief_bull_*`、`brief_bear_*` 這類研究階段前就存在的檔加進這個常數，否則每次續跑都會被判成殘留。

**測試**（加在 `tests/automation/test_agent_runner.py` 的 `test_bull_and_bear_tasks_cannot_read_each_others_output` 旁）

- 正常：`_peer_deny_reads("bull")` 與 `("bear")` 都含三個新樣式；`claude_read_deny_settings(_peer_deny_reads("bull"))` 的 deny 清單含 `Read(**/brief_swap_nomination*)`。
- fail-closed：新增一個「清單完整性」測試：列出目前 pipeline 會寫進 run 目錄、且屬於研究階段之後的檔名前綴（`research_debate`、`debate`、`intent`、`trade_decision`、`trader_b0_raw_1`、`brief_trader_b0`、`team_inputs`、`selection_ranking`、`brief_selection_ranking`、`ranker_raw_1`、`swap_nomination`、`brief_swap_nomination`、`swap_nominator_raw_1`、`release_order`、`brief_release_order`、`quality_dip_ruling`、`brief_quality_dip_ruling`），用 `fnmatch.fnmatch(name + ".json", pattern)` 斷言每一個都至少命中一個樣式。
- 殘留偵測：`decision_input.json`、`brief_bull_b0.json` 不得命中 `LATER_STAGE_GLOBS` 任何樣式。

**驗收**：上述測試通過；全套測試通過。

**風險與回滾**：只多擋檔案，研究員的合法輸入（`brief_bull_*`／`brief_bear_*`、`decision_input.json`、Skill 檔）不受影響。回滾＝刪掉三個樣式。

### A4　trace 補記非 Read 工具；明確禁用其他工具（P0）

**問題與證據（已核實）**

- `ClaudeAgentRunner.run`（`agent_runner.py:221-232`）只傳 `--tools Read`。
- `var/logs/agent_trace/` 全部 trace 的工具統計：`Read` 2769 次、`StructuredOutput` 558 次、`Grep` 3 次（`daily-20261110-233102/bull_b1.jsonl` 第 26 行、`daily-20261105-220314/bear_b1.jsonl`）。Grep 呼叫 0.1 秒後就有回應，推測是被拒絕，但 trace 沒有記回應是否為錯誤，**無法確認**。
- `_trace_summary`（`agent_runner.py:258-286`）只記 `input.file_path`，Grep／Glob 的 `path` 不會被記下，所以 trace 顯示 `"file": null`。

**修法**

1. `_trace_summary`：工具摘要多記一個 `path` 欄位（`tool_input.get("path")` 是字串才記，否則 `None`）。不記 `pattern`、`command` 等模型自寫文字（維持「不存模型文字」）。
2. `_trace_summary`：`event.type == "user"` 且 `message.content` 內有 `type == "tool_result"` 的區塊時，記 `"tool_errors": <is_error 為真的區塊數>`（只有數量，不記內容）。
3. `ClaudeAgentRunner.run` 的指令在 `"--tools", "Read"` 之後加 `"--disallowedTools", "Grep,Glob,Bash,Write,Edit,WebFetch,WebSearch,Task,NotebookEdit"`。先執行 `claude --help | grep -i disallowed` 確認目前安裝的版本（撰寫時為 2.1.322）支援這個旗標與寫法；不支援就不要加，改在回報中說明。
4. `claude_read_deny_settings`：每個樣式除了 `Read(**/<樣式>)`，再加 `Grep(**/<樣式>)` 與 `Glob(**/<樣式>)` 兩條（工具被禁用後這兩條是第二層保險）。
5. `ClaudeAgentRunner.run` 解析完結果後：若 trace 摘要中出現 `Read`、`StructuredOutput` 以外的工具名稱，在 `AgentCall` 之外用既有的 log 管道留一行警告即可，**不要**因此讓任務失敗（避免 Claude 版本更新造成整天停擺）。若現有程式沒有方便的 log 管道，就略過這一步並在回報中說明。

**測試**（`tests/automation/test_agent_runner.py`）

- 正常：`_trace_summary` 對 Grep 的 `tool_use`（`input` 含 `path` 與 `pattern`）輸出 `{"name": "Grep", "file": None, "path": "<該路徑>"}`，且輸出的 JSON 字串不含 pattern 內容。
- 正常：含一個 `is_error: true` 的 `tool_result` 事件，摘要有 `tool_errors == 1`。
- 正常：`test_structured_output_and_read_only_command` 的指令斷言加上 `--disallowedTools`（步驟 3 有做的話）。
- fail-closed：`claude_read_deny_settings(("bear_*",))` 的 deny 清單同時含 `Read(**/bear_*)`、`Grep(**/bear_*)`、`Glob(**/bear_*)`，敏感檔（`.env`）也三種都有。
- 既有測試 `test_trace_does_not_store_model_text_or_secrets` 必須仍通過。

**驗收**：測試通過。另請在回報中附上「人工確認步驟」供使用者執行一次真實 run 後檢查：`grep -l '"Grep"\|"Glob"' var/logs/agent_trace/<新 run>/*.jsonl` 應為空，或其後一列 `tool_errors` 為 1。

**風險與回滾**：`--disallowedTools` 寫法若與 CLI 版本不合，Claude 會啟動失敗（暫時性錯誤、整天失敗），所以步驟 3 必須先確認旗標。回滾＝移除該旗標與新增的 deny 規則。

### A2　專案層級拒讀路徑（P0）

**問題與證據（已核實）**

用空頭 claim 識別碼樣式（`bear-<代號>-<序號>`）掃整個 `artifacts/` 與 `var/`，除了 run 目錄與決策封存中已被既有樣式擋住的檔，還有這些**沒被擋**：

| 位置 | 檔名 | 筆數 |
| --- | --- | --- |
| `artifacts/reports/runs/<日期>/delivery-*/` | `report.json` | 28 |
| `var/strategy_reflection/` 之下 | `thesis_review_b*.json`、`thesis_review_b*_raw.json`、`thesis_review.json` | 68 |
| `var/strategy_reflection/reports/` | `weekly-*.md` | 4 |
| `artifacts/daily/<日期>/<run>/` | `pipeline.json`（錯誤訊息帶到 claim 識別碼） | 1 |

`artifacts/archive/superseded_decisions/` 與 `artifacts/decisions/` 內的 `debate.json`、`intent.json`、`team_inputs.json` 已被 `debate*`、`intent*`、`team_inputs*` 擋住（Claude 的 `**/` 規則跨目錄）。

原發現寫的路徑 `artifacts/reports/runs/*/delivery-*/report.json` 正確。

**修法**

1. `agent_runner.py` 新增常數（放在 `LATER_STAGE_GLOBS` 之後）：

```python
# 專案層級、含多空論點的目錄：研究員不需要讀，整個目錄拒讀。與 LATER_STAGE_GLOBS 分開，
# 因為這些不是 run 目錄內的檔名，不能進殘留偵測。
RESEARCH_DENY_DIRECTORIES = (
    "artifacts/reports", "artifacts/archive", "artifacts/decisions", "artifacts/evaluation",
    "var/strategy_reflection", "var/logs",
)
```

2. `AgentTask` 新增欄位 `deny_read_directories: Sequence[str] = ()`（放在 `stage_isolated` 之後，維持既有位置參數相容）。
3. `claude_read_deny_settings(extra, directories=())`：每個目錄加 `Read(**/<目錄>/**)`、`Grep(**/<目錄>/**)`、`Glob(**/<目錄>/**)`（A4 已做 Grep／Glob 的話比照，否則只加 Read）。`ClaudeAgentRunner.run` 把 `task.deny_read_directories` 傳入。
4. `_opencode_read_permission(extra, run_dir, directories=())`：每個目錄加 `"*/<目錄>/*": "deny"`（OpenCode 的 `*` 跨 `/`）。`OpenCodeAgentRunner.run` 傳入。
5. `CodexAgentRunner.run`：`deny_read_directories` 視同 `deny_reads` 的一部分——既有判斷 `if task.deny_reads and not task.stage_isolated` 改為 `if (task.deny_reads or task.deny_read_directories) and not task.stage_isolated`。
6. `team_stages.py` 建立多空研究員 `AgentTask` 的地方（搜尋 `deny_reads=_peer_deny_reads(role)`，約 397 行）加 `deny_read_directories=RESEARCH_DENY_DIRECTORIES`。
7. `pipeline_base.py` 約 490 行重建 `AgentTask` 的地方（搜尋 `task.deny_reads, stage_isolated=`）要把 `deny_read_directories` 一併帶過去，否則重試時會掉。
8. `pipeline.json`：在 `_peer_deny_reads` 回傳值加 `"pipeline.json"`（只加在這裡，不加進 `LATER_STAGE_GLOBS`，原因見 A1）。

開工前先確認研究員的合法輸入不在這些目錄：讀 `team_stages.py` 的研究員提示（搜尋 `def run_research` 或 `brief_%s`），確認只引用 run 目錄的 `brief_<role>_b*.json` 與 `tradeagent/` 下的 Skill 檔。若發現研究員被指示讀 `artifacts/decisions` 等目錄，停止並回報，不要自行決定。

**測試**（`tests/automation/test_agent_runner.py`、`tests/automation/test_research_staging.py`）

- 正常：`claude_read_deny_settings((), ("artifacts/reports",))` 含 `Read(**/artifacts/reports/**)`。
- 正常：`_opencode_read_permission((), Path("artifacts/daily/run1"), ("artifacts/reports",))["*/artifacts/reports/*"] == "deny"`，且 run 目錄錨定規則不變。
- 正常：以假 runner 跑研究階段（比照 `test_research_staging.py` 既有做法），攔下的多空任務 `deny_read_directories == RESEARCH_DENY_DIRECTORIES`，且 `deny_reads` 含 `pipeline.json`；重試後的任務仍帶這個欄位。
- fail-closed：`CodexAgentRunner` 遇到只有 `deny_read_directories`、沒有 `stage_isolated` 的任務時拋 `DailyPipelineError`、不啟動子程序。
- 不影響其他任務：分析師與交易 Agent 的 `AgentTask.deny_read_directories` 為空。

**驗收**：測試通過；`grep -rn "RESEARCH_DENY_DIRECTORIES" src` 顯示常數定義、`team_stages.py` 使用各一處。

**風險與回滾**：若日後有人讓研究員讀這些目錄，會被拒讀而失敗（屬預期）。回滾＝`team_stages.py` 不傳 `deny_read_directories`。

### A3　OpenCode 跨 run 拒讀；Codex 補文件（P1）

**問題與證據（已核實）**

- `_opencode_read_permission`（`agent_runner.py:92-100`）把任務專屬樣式錨定在本次 run 目錄名稱之後（`*/<run 名稱>/<樣式>`），所以其他 run 目錄的 `bear_*`、`debate*` 不會被擋。錨定的原因寫在註解：OpenCode 的 `*` 跨 `/`，不錨定會誤擋 Skill 目錄。
- `CodexAgentRunner` 的說明（`agent_runner.py:388-393`）已寫明是時序隔離、Codex 仍可讀整個專案含舊 run。這是已知且已記載的限制，不是新發現。

**修法**

1. `_opencode_read_permission` 新增參數 `sibling_run_names: Sequence[str] = ()`：對每個名稱與每個任務專屬樣式，加 `"*/<名稱>/<樣式>": "deny"`。
2. `OpenCodeAgentRunner.run`：`task.deny_reads` 非空時，列出 `run_dir.parent.parent` 之下所有「日期目錄／run 目錄」的名稱（排除本次 run；用 `Path.iterdir()`，只取目錄，目錄不存在或讀不到時視為空清單），傳入 `sibling_run_names`。A2 的 `artifacts/decisions`、`artifacts/archive` 整個目錄拒讀已涵蓋封存。
3. 已知差距寫進 `src/etf_agent/automation/README.md` 的拒讀段落：本次 run 啟動後才建立的其他 run 目錄不在清單內；Codex 無任何讀取限制，同一天同 snapshot 重跑時，研究員 CLI 為 Codex 的隔離弱於 Claude。
4. `AGENTS.md`「子 Agent 執行器與安全邊界」的 Codex 條目末尾補一句：「同一 Snapshot 重跑時 Codex 研究員可讀到前一次 run 的對方論點，正式比賽請用 Claude 當研究員 CLI。」

**測試**

- 正常：`_opencode_read_permission(("bear_*",), Path("a/2026-11-11/run2"), sibling_run_names=("run1",))` 同時有 `*/run2/bear_*` 與 `*/run1/bear_*` 兩條 deny。
- 正常：在暫存目錄建 `daily/2026-11-10/runA`、`daily/2026-11-11/runB`、`daily/2026-11-11/runC`，對 `runC` 執行（比照 `test_isolated_read_only_command_and_usage` 攔 `subprocess.run`），檢查 `OPENCODE_CONFIG_CONTENT` 含 `runA`、`runB` 的規則、不含重複的 `runC` 規則。
- fail-closed：`run_dir.parent.parent` 不存在時不拋例外、仍產生本次 run 的錨定規則。
- 不誤擋：產生的規則 key 沒有任何一條是未錨定的 `*/bear_*`。

**驗收**：測試通過；文件兩處已更新。

**風險與回滾**：run 目錄很多時規則數量變大（每個 run × 約 30 個樣式）。若超過 500 條，只取最近 14 個日期目錄，並在 README 註明。回滾＝不傳 `sibling_run_names`。

### B　風險修正動作的數值（P1，**等 4.1 決定後才實作**）

**問題與證據（已核實）**

- `src/etf_agent/automation/agent_schemas.py:305-317`：`REVIEW_SCHEMA.revision_actions[].value` 是任意字串。
- `src/etf_agent/decision/risk.py:726-764`（`_validate_action` 的數值分支）：只檢查 0～1、`increase_cash_buffer` 不得降低且有上限、其餘兩種「不得高於目前值」。`reduce_max_stock_weight`、`reduce_turnover_limit` **沒有下限**，填 `0.01` 也會通過。
- `risk.py:767-804` 的 `revision_effects` 直接把數字寫進 `overrides`。
- `allocation.py:1474`：持股上限股數＝NAV × `_weight_limit` × 封頂係數，上限調低會直接產生賣單。
- `allocation.py:2103-2107` 的 `_weight_limit`：`special_weight_limits` 有該股時直接回傳特殊上限，不看調低後的 `max_stock_weight`（原發現寫 2101-2102，檔案有他人修改後位移）。
- 補充事實：`artifacts/decisions` 與 `artifacts/archive/superseded_decisions` 共 28 個 run 的 `revision_history.json`，**沒有任何一次**出現 `revision_actions`。這條路徑至今沒被實際用過。

**修法（依 4.1 的建議選項「補下限＋承認例外」撰寫；若使用者選「列舉格點」，改依 4.1 選項二的說明，本節的閘門登記與測試骨架不變）**

1. 新版本閘門 `position_sizing.revision_bounds`，形狀 `{"version": "v1", "min_max_stock_weight": "<小數字串>", "min_max_turnover_rate": "<小數字串>", "step": "0.01"}`，三個數字全部由人填。
2. `src/etf_agent/decision/sizing.py`：登記欄位白名單與驗證（比照 `valuation_facts` 的登記方式，搜尋 `"valuation_facts"`）：`version` 必須是 `v1`；兩個下限須大於 0 且不高於 Policy 的 `max_stock_weight`／`max_turnover_rate`；`step` 須大於 0。新增 `revision_bounds_gate(sizing)` 回傳閘門或 `None`。
3. `src/etf_agent/strategy_reflection/contracts.py`：把 `revision_bounds` 加進禁區清單（搜尋 `"valuation_facts"`，照樣加一行；此檔有他人修改，只加這一行）。
4. `risk.py` `_validate_action`：閘門存在時，對 `reduce_max_stock_weight`／`reduce_turnover_limit` 多兩個檢查——`value` 不得低於對應下限；`value` 必須是 `step` 的整數倍（用 `Decimal` 取餘數）。閘門不存在時一行都不變。
5. `automation/risk_stages.py`：審查簡報／提示在閘門存在時附上兩個下限與 `step`（搜尋 `increase_cash_buffer 的 value 不得高於`，約 572 行，比照那段的做法）。
6. `tradeagent/risk/portfolio-risk-review/references/risk-review-output.md`：表格補「有 `revision_bounds` 時的下限與步距」，並新增一句說明「`special_weight_limits` 內的股票（目前為 2330）不受 `reduce_max_stock_weight` 影響，要降低它的權重請用 `remove_candidate` 或 `increase_cash_buffer`」。
7. 文件：`docs/decision-contracts.md` 風險審查段新增 `revision_bounds` 條目，並明寫「修正動作的 `value` 是『Agent 不輸出數字』的已知例外：只能往降風險方向、有上下界、由 Validator 檢查」；`AGENTS.md` 總則第一條末尾加同一句的精簡版；`docs/status.md` 版本閘門表新增一列「已實作、尚未開啟」。

**測試**（新檔 `tests/decision/test_revision_bounds.py`）

- 正常：閘門開、值在下限以上且為步距倍數 → 通過；`revision_effects` 產生對應 `overrides`。
- fail-closed：值低於下限 → 錯誤訊息含下限值；值不是步距倍數 → 拒絕；閘門 `version` 不是 `v1`、下限高於 Policy 上限、缺欄位 → Policy 驗證失敗。
- 逐位元：同一份 review 與 proposal，在沒有閘門的 Policy 下，`RiskReviewValidator` 的錯誤清單與修改前完全相同（含 `value = "0.01"` 仍通過）。
- 禁區：策略反思提案含 `revision_bounds` 時被拒（比照 `tests/strategy_reflection/test_design_guards.py` 既有寫法）。
- `increase_cash_buffer` 的既有檢查（現金上限邊際、`cash_ratio_range` 格點）在閘門開關兩種情況下都不變。

**驗收**：測試通過；`config/decision_policy.json` 沒有被改；回報中附上要由人加入的 JSON 片段與建議值的空格（不替人填數字）。

**風險與回滾**：閘門未開時零影響。開啟後風險 Agent 可能因下限而多一輪修正循環。回滾＝刪掉 `position_sizing.revision_bounds`。

### C　交易背景進 `team_inputs`（P1，**等 4.2 決定後才實作**）

**問題與證據（已核實）**

- `src/etf_agent/automation/team_stages.py:495-496`、`:500`、`:525-526`：交易簡報的 `strategy_lessons` 來自 `config/strategy_lessons.json`、`thesis_review_background` 來自論點檢視結論檔，兩者每次 run 當場讀檔（原發現寫 495-522，他人修改後位移）。
- `src/etf_agent/decision/finalization.py:116-272` 的 `build_team_inputs` 沒有這兩者的參數與鍵；Finalizer 無法知道交易 Agent 當時看到哪一版教訓。兩個來源檔每週會被策略反思改寫，事後無從還原。
- 既有先例：`valuation_facts`、`decline_depth_facts`、`offense_facts` 都是「選配鍵、不升版本、與閘門雙向檢查」（`finalization.py:509`、`:516`）。論點檢視另有 `verdicts_ref` 綁在 Policy（`swap_nomination.py:1147-1173`）。

**修法（依 4.2 的建議選項「選配鍵＋版本閘門、不升版本」撰寫）**

1. 新檔 `src/etf_agent/decision/trader_background.py`：
   - `TRADER_BACKGROUND_KEY = "trader_background"`。
   - `trader_background_gate(sizing)`：回傳 `position_sizing.trader_background_record`（`{"version": "v1"}`）或 `None`。
   - `validate_trader_background_gate(sizing, errors)`：只允許 `{"version": "v1"}`。
   - `build_trader_background(root, bundle)`：讀兩個來源檔的**完整內容**（含 `content_sha256`），回傳 `{"version": "v1", "strategy_lessons": <完整教訓檔或 None>, "thesis_verdicts": <完整結論檔或 None>, "content_sha256": <canonical_sha256>}`。可用與否的判斷必須與 `load_strategy_lessons`／`load_thesis_verdicts` 完全一致（驗證失敗、空清單、晚於 cutoff、結論檔超過 `MAX_AGE_DAYS` 都記 `None`）。
   - `validate_trader_background(record, bundle, policy)`：回傳錯誤清單。檢查：欄位白名單；`content_sha256` 可重算；教訓檔非 `None` 時 `validate_lessons_file` 無錯、`lessons` 非空、`created_at <= decision_cutoff`；結論檔非 `None` 時 `validate_verdicts_file` 無錯、`items` 非空、`created_at <= decision_cutoff` 且未超過 `MAX_AGE_DAYS`；Policy 的 `position_sizing.agent_swap.verdicts_ref` 存在時，結論檔不得為 `None` 且其 `content_sha256` 必須等於 `verdicts_ref.sha256`。
   - `lessons_from_record(record)`、`verdicts_from_record(record)`：轉成 `lessons_brief`／`verdicts_brief` 需要的輸入形狀（`{"lessons_id", "texts", "sha256"}`、`{"verdicts_id", "as_of", "items", "sha256"}`）。
2. `decision/strategy_lessons.py`、`decision/thesis_verdicts.py`：各新增一個回傳完整檔案內容的函式（例如 `load_strategy_lessons_file`、`load_thesis_verdicts_file`），既有的 `load_*` 改為呼叫它再取欄位，**回傳值不得改變**。
3. `automation/team_stages.py` `run_trader`：閘門存在時，先 `build_trader_background` 並存成 run 目錄的 `trader_background.json`（檔名以 `trader_` 開頭，已被研究員拒讀樣式 `trader_*` 涵蓋）；續跑時若檔案已存在則讀回、驗證後沿用，不重讀來源檔；簡報的 `strategy_lessons`、`thesis_review_background` 改由這份紀錄經 `lessons_from_record`／`verdicts_from_record` 產生。閘門不存在時這段程式完全不走，簡報逐鍵不變。
4. `decision/finalization.py`：`build_team_inputs` 加選配參數 `trader_background`，有值時寫入 `team[TRADER_BACKGROUND_KEY]`；允許的選配鍵清單（約 475 行，搜尋 `VALUATION_FACTS_KEY, DECLINE_DEPTH_FACTS_KEY`）加入新鍵；比照 `valuation_gate` 的雙向檢查（約 509 行）加「鍵存在 ⇔ 閘門存在」，並呼叫 `validate_trader_background`。`schema_version` 不變。
5. `automation/daily_pipeline.py`：`build_team_inputs(` 呼叫處（約 558 行）傳入這份紀錄。
6. `decision/sizing.py` 與 `strategy_reflection/contracts.py`：登記閘門與禁區（同 B 的做法；B 先做）。
7. 文件：`docs/decision-contracts.md` 的 `team_inputs` 段、`docs/architecture.md` 的 `team_inputs` 版本史各加一段；`AGENTS.md`「背景不是證據」條目末尾加一句；`docs/status.md` 加一列「已實作、尚未開啟」。文件必須寫明限制：Finalizer 重算的是「封存內容是否自洽、時間是否合法、是否與 Policy 綁定的 `verdicts_ref` 一致」，無法證明來源檔當時沒有別的版本。

**測試**（新檔 `tests/decision/test_trader_background.py`，pipeline 部分加在 `tests/automation/` 新檔）

- 正常：有教訓與結論時，`build_trader_background` 的紀錄通過 `validate_trader_background`；由紀錄產生的簡報兩個區塊與現行 `lessons_brief(load_strategy_lessons(...))`、`verdicts_brief(load_thesis_verdicts(...), held)` 完全相等。
- 正常：兩者皆無 → 紀錄的兩個欄位為 `None`、仍通過驗證、簡報沒有那兩個鍵。
- fail-closed：竄改紀錄內一條教訓文字（雜湊不符）；教訓 `created_at` 晚於 cutoff；結論檔超過有效天數；Policy 有 `verdicts_ref` 但紀錄結論為 `None` 或雜湊不同；多出未知欄位——各自被拒。
- 雙向：閘門開而 `team_inputs` 沒有鍵 → Finalizer 拒絕；閘門沒開而有鍵 → 拒絕。
- 逐位元：沒有閘門的 Policy 跑一次既有的 pipeline 測試情境，`team_inputs` 與交易簡報的鍵集合與修改前相同（沿用既有測試的斷言即可，不得需要改既有測試的期望值）。
- 續跑：run 目錄已有 `trader_background.json`，之後把來源教訓檔改掉，續跑的簡報仍用封存那一份。

**驗收**：測試通過；既有測試不需修改期望值；`config/decision_policy.json` 沒有被改；回報附上要由人加入的 `"trader_background_record": {"version": "v1"}`。

**風險與回滾**：閘門未開時零影響。開啟後 `team_inputs` 變大（多兩份小檔）。回滾＝刪掉閘門欄位。

### D　回放是樣本內評估：補揭露（P2，**等 4.3 決定後才實作**）

**問題與證據（已核實，但性質與原發現不同）**

- `src/etf_agent/evaluation/replay.py:220-224`：樣板的 `available_at` 晚於 cutoff 時改成 cutoff，並設 `overridden = True`。
- 這個改寫**有被揭露**：`replay.py:328` 把 `available_at_overridden` 寫進每日的 `policy` 註記，`evaluation/replay_report.py:79`、`evaluation/html_report.py:275` 會顯示。
- `src/etf_agent/strategy_reflection/service.py:450-456`：`apply_reflection` 以目前樣板與候選樣板各回放一次，交給 `replay_gate` 決定是否套用。`replay_gate` 的說明（`service.py:307-314`）已寫「回放與提案用同一段歷史」，結果標 `evidence_grade: replay_reference_only`。
- `config/strategy_reflection.json` 的 `auto_apply` 為 `false`；自動套用另外只放行降風險方向。
- 結論：這是**已知並已標示的設計限制**，不是隱藏的未來資訊洩漏。真正缺的是：`replay_gate` 的輸出與週報沒有直接寫出「這是樣本內比較」與「有幾天改寫了樣板時間」。

**修法（依 4.3 的建議選項「只補揭露、不重做回放、不改閘門判定」撰寫）**

1. `strategy_reflection/service.py` `replay_gate` 的回傳值新增兩個鍵：`"in_sample": True`（固定值）與 `"available_at_overridden_days": <候選回放中 day["policy"]["available_at_overridden"] 為真的天數>`。`passed`、`reasons`、`segments` 的計算一行都不改。
2. `apply_reflection` 寫入歷史的 `replay_gate` 摘要（約 495 行）多帶這兩個鍵。
3. `strategy_reflection/report.py`：週報在既有 `REPLAY_NOTICE` 那一行（約 129 行）之後多一行白話說明：「影子回放用的是提案時已經看過的同一段日期，不是樣本外驗證；其中 N 天的策略樣板建立時間晚於當日截止時間。」
4. `docs/decision-contracts.md`「策略反思旁觀者」段補同一句。

**測試**（`tests/strategy_reflection/test_strategy_reflection.py`）

- 正常：`replay_gate` 回傳含 `in_sample is True`；兩天中一天 `available_at_overridden` 為真時計數為 1；沒有 `policy` 註記的舊回放資料計數為 0、不拋例外。
- 不變：同一組輸入的 `passed`、`reasons`、`segments` 與修改前相同（沿用既有測試）。
- fail-closed：回放未完成（`status != "completed"`）時仍回傳 `in_sample`，`available_at_overridden_days` 為 0，`passed` 為假。
- 週報含新增的那一句。

**驗收**：測試通過；既有回放測試不需改期望值（若既有測試對 `replay_gate` 回傳值做完整相等比對，只補上兩個新鍵）。

**風險與回滾**：純新增輸出鍵。讀取歷史檔的程式若對 `replay_gate` 摘要做欄位白名單，要同步放行（實作前先搜尋 `"replay_gate"`）。回滾＝移除兩個鍵。

### E1　交易池依 cutoff 過濾（P2）

**問題與證據（已核實，風險比原發現小）**

- `src/etf_agent/data/snapshot_repository.py:48-55` `load_universe`：`SELECT symbol, source_date FROM instruments WHERE in_competition_universe = 1`，沒有 cutoff 條件。
- `instruments` 表（`data/database.py:105-113`）以 `symbol` 為主鍵、沒有版本史；`updated_at` 每次載入都被改寫（實測全部是今天），不能拿來判斷時間。`source_date` 格式不一致（多數是 `2026-09-14`，有一筆是 `2026-10-23T23:50:00+08:00`）。
- 已有的部分防護：`data/snapshot.py:403-409`——`universe_validation_runs` 依 `generated_at <= cutoff` 取最新一筆，其 `universe_version` 與目前交易池不同時標 `STALE_UNIVERSE_VALIDATION`。所以交易池在 cutoff 之後被改動，多數情況會被擋下。
- 剩下的缺口：`source_date` 晚於 cutoff 的股票仍會進入舊 cutoff 的 Snapshot（重建歷史 Snapshot 時）。目前 150 檔的 `source_date` 都是 2026-09-14，實際上沒有發生。

**修法**

1. `load_universe(connection, cutoff_date: Optional[str] = None)`：有 `cutoff_date` 時 SQL 加 `AND substr(source_date, 1, 10) <= ?`。參數預設 `None` 以維持其他呼叫端相容。
2. `data/snapshot.py` 的呼叫處（約 460 行，搜尋 `self.repository.load_universe(connection)`）傳入 cutoff 的台北日期（同檔已有 `_price_date_limit(cutoff)`，先讀它的定義；語意相同就直接用，不同就另寫一個小函式取 cutoff 換成 Asia/Taipei 後的日期字串）。
3. 過濾後為空時沿用既有的 `EMPTY_COMPETITION_UNIVERSE` 旗標，不另外處理。

**測試**（`tests/data/test_data_agent.py` 或同目錄新檔）

- 正常：三檔 `source_date` 都早於 cutoff → 全部進 Snapshot，`universe_version` 與修改前相同。
- 正常：`source_date` 是含時區的時間字串（`2026-10-23T23:50:00+08:00`）、cutoff 日期晚於它 → 納入。
- fail-closed：一檔 `source_date` 晚於 cutoff → 不進 Snapshot，且因 `universe_version` 與驗證紀錄不同而出現 `STALE_UNIVERSE_VALIDATION`（或驗證紀錄相符時正常通過，依測試資料而定，兩種都要寫）。
- fail-closed：全部晚於 cutoff → `EMPTY_COMPETITION_UNIVERSE`。

**驗收**：測試通過；以目前資料庫建一次 Snapshot（或既有整合測試），交易池仍為 150 檔。

**風險與回滾**：`source_date` 若有非日期開頭的髒資料，字串比較會誤判；實作前先跑 `sqlite3 var/etf_agent.db "select distinct substr(source_date,1,10) from instruments where in_competition_universe=1"` 確認都是日期。回滾＝呼叫端不傳 `cutoff_date`。

### E2　`ex_rights_symbols` 補 `fetched_at <= as_of`（P2）

**問題與證據（已核實）**

- `src/etf_agent/calibration/prices.py:66-73`：`ex_rights_symbols` 的 SQL 只有 `ex_rights_marker = 1 AND trade_date > ? AND trade_date <= ?`。
- 同檔 `trading_days`（`:41-50`）有 `AND fetched_at <= ?`、參數為 `self.as_of`。兩者不一致。

**修法**：SQL 加 `AND fetched_at <= ?`，參數補 `self.as_of`。

**測試**（`tests/calibration/test_calibration.py`）

- 正常：`fetched_at` 早於 `as_of` 的除權息列 → 該股在回傳集合內。
- fail-closed：同一檔另有一筆 `fetched_at` 晚於 `as_of` 的除權息列（且沒有較早的列）→ 不在回傳集合內。
- 邊界：`fetched_at == as_of` → 納入。

**驗收**：測試通過。

**風險與回滾**：校準結果可能因少排除幾檔而略有不同（那些排除原本就用到了事後資料）。回滾＝移除該條件。

### F　背景識別碼禁令（P2）

**問題與證據（已核實）**

- `src/etf_agent/decision/prior_decision.py:295-303` `prior_reference_tokens`：禁用字串是字面 `prior_decision`、`context_id` 與來源的 `run_id`／`decision_id`／`intent_id`。
- `prior_decision.py:306-328` `prior_decision_brief`：簡報只給來源的 `decision_cutoff`、`snapshot_id`、`decision_status` 與逐檔 `intent`／`conviction`／`invalidation_conditions`，**不含**上述任何識別碼。交易 Agent 看不到這些字串，`trader.py:229-235` 的檢查實際上只會擋到字面 `prior_decision`。
- 教訓與論點檢視的簡報同樣刻意不給識別碼（`strategy_lessons.py:100-108`、`thesis_verdicts.py:89-104`），程式沒有任何檢查。
- 原發現的後半（不傳決策庫時只做結構驗證）：`prior_decision.py:416` 屬實，但目前正式設定有 `cash_ratio_range.selection_check: "v1"`，此時沒有決策庫會直接拒絕（`prior_decision.py:427-430`）。`cli/decision_delivery.py:18` 的決策庫有預設值、`cli/dplan.py:46` 為必填。所以**現行設定下這個缺口不存在**，只影響沒有 `selection_check` 的舊 Policy（依舊 run 不變原則不動）。

**修法**

1. 文件說真話（必做）：`AGENTS.md`「背景不是證據」條目與 `docs/decision-contracts.md` 的前次決策背景段，把「文字欄位出現其識別碼即拒絕」改寫為：程式只能擋識別碼字串與（開啟第 2 步後的）整句照抄；改寫轉述無法由程式偵測，靠提示與「行動須由本次 claim 支持」的驗證把關。
2. 即時照抄檢查（只在 pipeline 即時驗證，不進 Finalizer 重建路徑，所以舊 run 不受影響）：
   - `decision/trader.py` 的驗證器建構子（搜尋 `self.prior_tokens = prior_reference_tokens`，約 139 行）新增選配參數 `background_texts: Sequence[str] = ()`，存成 `self.background_texts`。
   - 在 `prior_tokens` 檢查旁（約 229-235 行）加一段：`rationale`、`unresolved_questions`、`invalidation_conditions` 任一欄位包含 `background_texts` 中任一字串（完整子字串）時，加錯誤「不得照抄策略教訓或論點檢視背景；它不是本次證據，請以本次多空 claim 的依據說明」。長度不足 10 個字的背景字串略過（避免誤擋）。
   - `automation/team_stages.py` `run_trader` 建立驗證器的地方，把該批簡報內的教訓文字與論點檢視 `summary` 傳入 `background_texts`。Finalizer 與其他重建呼叫端**不傳**。
   - 前次決策的 `invalidation_conditions` **不**列入（續抱同一檔時沿用同一句重估條件是合法的）。
3. 此檔與 `team_stages.py` 都有他人進行中的修改，且第 2 步要等 C 完成後再做。

**測試**（`tests/decision/test_trader.py`）

- 正常：理由沒有照抄 → 通過；理由只是意思相近的改寫 → 通過（並在測試名稱或註解寫明這是已知無法偵測的情況）。
- fail-closed：理由含一條完整教訓文字 → 拒絕；`invalidation_conditions` 含完整的論點檢視摘要 → 拒絕。
- 不變：不傳 `background_texts` 時，同一份輸出的錯誤清單與修改前相同。
- 邊界：9 個字的背景字串不觸發。

**驗收**：測試通過；既有的 Finalizer／重建測試不需修改。

**風險與回滾**：可能多一次修正循環（訊息已說明修法）。回滾＝`run_trader` 不傳 `background_texts`。

### G1　正式入口缺 `nav_history` 時拒絕（P1）

**問題與證據（已核實）**

- `src/etf_agent/automation/daily_pipeline.py:120`、`:178`：`nav_history` 預設 `None`。
- `src/etf_agent/decision/drawdown.py:150-161`：`nav_history is None` 時回撤記 `unavailable`、不設姿態下限。`drawdown.py:233-239` 的驗證只在「呼叫端有提供可驗證歷史」時才拒絕 `unavailable`。
- 正式入口 `scripts/run_daily_pipeline.py:1236` 有傳 `VirtualAccountRepository(...).nav_history()`。`src/etf_agent/evaluation/service.py:224` 在沒有帳本情境時傳 `None`（評測用途，屬預期）。
- 結論：目前正式路徑沒有問題；風險是日後有人改入口而漏傳，回撤下限會無聲失效。

**修法**

1. `DailyDecisionPipeline.run` 與 `_run`（以及往下傳到 `_decide` 的路徑）新增關鍵字參數 `require_nav_history: bool = False`。
2. 在 `bind_drawdown` 之前（約 287 行，搜尋 `base_policy = bind_drawdown(`）：`require_nav_history` 為真、Policy 有 `drawdown_control`、而 `nav_history is None` 時，拋 `DailyPipelineError("正式流程必須提供帳本 NAV 歷史（nav_history），否則回撤下限無法計算；已停止")`。這個檢查要在任何子 Agent 被呼叫之前發生；若 `bind_drawdown` 的位置已在分析師之後，就把檢查提前到 `_run` 一開始。
3. `scripts/run_daily_pipeline.py:1225` 的 `pipeline.run(...)` 加 `require_nav_history=True`。
4. `evaluation/service.py` 不改（預設 `False`）。

**測試**（`tests/automation/test_daily_pipeline.py`；此檔有他人修改，只新增測試方法）

- 正常：`require_nav_history=True` 且傳入空清單 `[]`（新帳戶）→ 不拋例外，回撤為 `available`、`history_length` 0。
- fail-closed：`require_nav_history=True`、`nav_history=None`、Policy 有 `drawdown_control` → 拋 `DailyPipelineError`，假 runner 的呼叫次數為 0。
- 不變：`require_nav_history` 未傳、`nav_history=None` → 行為與修改前相同（回撤 `unavailable`）。
- Policy 沒有 `drawdown_control` 時，即使 `require_nav_history=True` 也不拒絕。

**驗收**：測試通過；`grep -n "require_nav_history" scripts/run_daily_pipeline.py` 有一處。

**風險與回滾**：帳本讀取失敗時 `nav_history()` 是拋例外而不是回 `None`，行為不變。回滾＝腳本不傳該參數。

### G2　硬規則數字兩個來源的一致性測試（P1）

**問題與證據（已核實）**

- `config/decision_rules.json`：`min_positions` 20、`max_positions` 30、`max_stock_weight` `"0.1"`、`special_weight_limits` `{"2330.TW": "0.25"}`、`cash_weight_must_be_below` `"0.25"`。
- `src/etf_agent/competition/fallback.py:46-49`：`MIN_POSITIONS, MAX_POSITIONS = 20, 30`、`CASH_RATIO_LIMIT = Decimal("0.25")`、`STOCK_WEIGHT_LIMIT = {"2330": Decimal("0.25")}`、`DEFAULT_STOCK_WEIGHT_LIMIT = Decimal("0.10")`。目前數值一致，但沒有任何檢查；代號形式也不同（`2330.TW` 對 `2330`）。
- 備援計畫刻意不依賴設定檔（設定壞掉時仍要能產生），所以不改成執行時讀檔。

**修法**：只加測試，不改程式。在 `tests/competition/test_fallback_dplan.py` 新增一個測試類別，讀 `config/decision_rules.json`，逐項比對：

- `MIN_POSITIONS == rules["min_positions"]`、`MAX_POSITIONS == rules["max_positions"]`。
- `CASH_RATIO_LIMIT == Decimal(rules["cash_weight_must_be_below"])`。
- `DEFAULT_STOCK_WEIGHT_LIMIT == Decimal(rules["max_stock_weight"])`。
- `STOCK_WEIGHT_LIMIT` 與 `rules["special_weight_limits"]` 去掉 `.TW`／`.TWO` 後綴後，鍵集合相同、值以 `Decimal` 比較相等。
- 若 `config/competition_rules.json` 存在且有對應欄位，同樣比對（先讀檔看欄位名稱；沒有對應欄位就不比，並在測試註解寫明）。

**測試的 fail-closed 分支**：另寫一個測試，用 `unittest.mock.patch.object` 把 `fallback.CASH_RATIO_LIMIT` 換成 `Decimal("0.30")`，確認比對用的輔助函式回報不一致（把比對邏輯寫成測試檔內的輔助函式，回傳差異清單，兩個測試共用）。

**驗收**：測試通過。

**風險與回滾**：無執行期影響。日後主辦方改規則時，這個測試會提醒兩邊一起改。

### H　文件與設定不一致（P2，最後做）

**問題與證據（已核實；以下「現值」是 2026-11-11 撰寫時讀到的 `config/decision_policy.json`，`policy_id strategy-2026-11-11-review-alerts-1`。該檔仍在被編輯，實作時必須重新讀取現值，以實作當下為準）**

| 位置 | 文件寫的 | 設定現值 |
| --- | --- | --- |
| `docs/architecture.md:117-118` | 回撤達 10% 至少防守、降到 7% 以下回中性 | `defensive_at 0.15`、`release_defensive_below 0.11`（中性 5%／3% 相符） |
| `docs/decision-details.md:66-70` | 表格「達 10%／回到 7% 以下」 | 同上 |
| `AGENTS.md` 版本閘門第一條 | 已開啟含 `forced_swap`＋`confirmation` | `position_sizing.rotation` 只有 `version`、`max_swaps_per_run`、`min_new_conviction`，沒有 `forced_swap`（同檔後面與 `docs/status.md:58` 都寫已移除） |
| `AGENTS.md`「2026-11-11 12:36 其餘進攻設定」條 | `max_pairs_by_view` `start_rebuild 3`；`rebound_participation.by_state` 有 `defensive_cash_buffer` | `start_rebuild 4`（另有 `probe 2`）；`by_state` 已無 `defensive_cash_buffer` |
| `docs/status.md:70` | `start_rebuild 3`／`rebuild 5` | `probe 2`／`start_rebuild 4`／`rebuild 5` |
| `docs/status.md:79` | `reentry_cooldown` v2，v3 尚未開啟 | v3 |
| `AGENTS.md`「已實作、尚未開啟（優質股第三階段 v2 正式版）」與「抄底梯度 C1」兩條 | 尚未開啟 | `quality_dip` v4 已開、`ladder.exposure_basis` 為 `quality_dip_fills` |
| `docs/decision-contracts.md:181` | `agent_swap` v2（尚未開啟） | v2 |
| `docs/decision-contracts.md:182` | `derisk_day_view` v1 尚未開啟 | 設定有 `derisk_day_view: {"version": "v1"}` |
| `docs/decision-contracts.md:185-188` | `agent_swap.exposure`、`quality_dip`、`quality_dip` v3／v4、`exposure_view` v2 尚未開啟 | `exposure` v2、`quality_dip` v4、`exposure_view` v2 |
| `AGENTS.md` 版本閘門第一條 | `position_review` 只設 `loss_alert` 0.15 | `loss_alert 0.12`、`retrace_alert_20d 0.20`、`gain_alert 0.10`（**這是使用者正在進行的修改，H 不要動這一句**，交給那項工作的作者） |

**修法**

1. 開工先執行下面指令取得現值，貼在工作筆記（不要寫進文件）：

```bash
.venv/bin/python -c "import json;p=json.load(open('config/decision_policy.json'));print(p['policy_id']);print(json.dumps(p.get('drawdown_control')));print(json.dumps(p['position_sizing'], ensure_ascii=False, indent=1))"
```

2. 依現值逐列修正上表（最後一列除外）。原則：
   - 數值只寫在 `docs/status.md`；`docs/architecture.md`、`docs/decision-details.md` 的回撤段改成現值，並加一句「目前數值以 status.md 為準」。
   - `AGENTS.md` 內互相矛盾的條目：保留較新的敘述，把較舊條目中已失效的數字或狀態改掉，不要整條刪除歷史（回滾樣板路徑要留）。「已實作、尚未開啟」但其實已開啟的條目，把開頭改為「已開啟」並刪掉句尾的「把它的行為當成現行行為是錯的」。
   - `docs/decision-contracts.md:181-188`：把「（尚未開啟）」「已實作、尚未開啟」改成「已開啟（日期見 status.md）」；契約內容本身不改。
3. 不改任何程式、設定與測試。

**測試**：無新增測試（純文件）。執行 `git diff --check` 與 `git diff --stat` 確認只動到 `AGENTS.md` 與 `docs/` 下的檔。

**驗收**

- `grep -n "達 10%\|7% 以下" docs/architecture.md docs/decision-details.md` 無結果（或只剩標明為歷史值的敘述）。
- `grep -n "start_rebuild 3" AGENTS.md docs/status.md` 只剩明確標為歷史的敘述。
- `grep -n "尚未開啟" docs/decision-contracts.md` 的每一筆都對得上現值中確實不存在的欄位（逐筆核對後在回報列出仍保留的項目）。

**風險與回滾**：設定檔在實作期間可能又變。回報中註明核對時的 `policy_id`。回滾＝`git checkout` 這些文件檔（由使用者決定）。

## 4. 需要人決定的事項

以下三項在使用者回覆前**不得實作**：B、C、D。其餘十項可直接動工。

### 4.1 B：風險修正動作的數值怎麼處理

| 選項 | 內容 | 代價 |
| --- | --- | --- |
| 一（建議）補下限＋承認例外 | 新閘門 `revision_bounds`，人填兩個下限與步距；文件明寫這是「Agent 不輸出數字」的已知例外 | 原則上仍有例外；改動最小，Schema 不變 |
| 二 改為列舉格點 | 風險 Agent 只輸出列舉 `step`（例如 `one_step`／`two_steps`），程式依人填的格點換成數值；閘門開啟時 `REVIEW_SCHEMA` 換一份 | 要改 Schema、提示、Skill 參考檔與修正循環回饋；三種 CLI 都要重測 |

建議選項一。理由：28 個封存 run 沒有任何一次用到修正動作；比賽 2026-11-27 結束，選項二的改動面與重測成本不成比例。選項一已把「無下限可一次砍光」的實際風險關掉。

另一個要一併決定的小題：`special_weight_limits` 的股票（2330）不受 `reduce_max_stock_weight` 影響。建議**不改配置**，只在 Skill 參考檔與契約文件說明（已寫進 B 的修法第 6 步）；若要改成「特殊上限也跟著等比例降」，需另開計畫，因為會動到 `allocation.py`。

人要提供的東西：選項；若選一，下限與步距的數值由使用者自己寫進設定。

### 4.2 C：`team_inputs` 要不要升版

| 選項 | 內容 | 代價 |
| --- | --- | --- |
| 一（建議）選配鍵＋閘門、不升版本 | 新閘門 `trader_background_record`；鍵與閘門雙向檢查；`schema_version` 不變 | 要由人開閘門才生效 |
| 二 升版到 2.11 | 新版本必帶 `trader_background` | 版本判斷鏈、所有版本常數集合與大量既有測試都要改；與 2.10（`quality_dip_ruling`）的先後關係要重新定義 |

建議選項一，與 `valuation_facts`、`decline_depth_facts`、`offense_facts` 的既有做法一致，舊 run 逐位元不變最容易保證。

人要提供的東西：選項；實作完成後是否開啟閘門。

### 4.3 D：要不要重做回放、要不要加樣本外條件

| 選項 | 內容 | 代價 |
| --- | --- | --- |
| 一（建議）只補揭露 | `replay_gate` 與週報寫明是樣本內比較與改寫天數；不重做既有回放、不改通過條件 | 自動套用的把關強度不變（目前 `auto_apply` 為 false） |
| 二 加樣本外條件 | 回放日期中須有反思簡報資料視窗之後的決策日，且那一段單獨不劣才通過 | 比賽剩不到兩週，樣本幾乎一定不夠，等於永久關閉自動套用；要先定義簡報視窗的結束日如何取得 |

建議選項一，且不重做既有回放：既有回放已標 `replay_reference_only`，重做不會改變任何已套用的設定。若使用者希望在 `auto_apply` 重新開啟前有樣本外把關，再另開計畫做選項二。

人要提供的東西：選項；是否要求在開啟 `auto_apply` 之前先完成選項二。

## 5. 給實作模型的逐項提示範本

每段可直接貼給實作模型。貼之前把「〈決定〉」換成使用者的決定。

**A1**

> 讀 `/Users/apollo/stock/ETF_AGENTS/AGENTS.md`，再讀 `docs/plans/audit_fix_plan.md` 第 1 節與「A1」節，只做 A1。在 `src/etf_agent/automation/agent_runner.py` 的 `LATER_STAGE_GLOBS` 加入 `brief_swap_nomination*`、`brief_selection_ranking*`、`ranker_*`（各附註解），不要加任何研究階段前就存在的檔名。在 `tests/automation/test_agent_runner.py` 依計畫新增三個測試（`unittest.TestCase` 風格）。跑 AGENTS.md「測試與驗證」的三個指令。不要 commit，不要碰 `config/decision_policy.json`。回報修改的檔案與測試結果。

**A4**

> 讀 `AGENTS.md`，再讀 `docs/plans/audit_fix_plan.md` 第 1 節與「A4」節，只做 A4（A1 已完成）。修改 `src/etf_agent/automation/agent_runner.py`：`_trace_summary` 多記 `path` 與 `tool_errors`（不記 pattern 等模型文字）；`claude_read_deny_settings` 每個樣式多加 `Grep(...)`、`Glob(...)` 規則；先用 `claude --help` 確認 `--disallowedTools` 旗標存在才加到 `ClaudeAgentRunner.run` 的指令，不存在就不加並回報。依計畫補測試。既有測試 `test_trace_does_not_store_model_text_or_secrets` 必須仍通過。跑三個驗證指令。不要 commit。回報時附上計畫要求的人工確認步驟。

**A2**

> 讀 `AGENTS.md`，再讀 `docs/plans/audit_fix_plan.md` 第 1 節與「A2」節，只做 A2（A1、A4 已完成）。`src/etf_agent/automation/team_stages.py` 有他人未提交的修改：先 `git diff` 看過，不得還原，只加自己的一行。依計畫新增 `RESEARCH_DENY_DIRECTORIES`、`AgentTask.deny_read_directories`，接到 Claude、OpenCode、Codex 三個 runner、`team_stages.py` 的研究員任務與 `pipeline_base.py` 重建任務處；`_peer_deny_reads` 加 `pipeline.json`。開工前先確認研究員提示沒有要求讀這些目錄，若有就停下回報。依計畫補測試、跑三個驗證指令。不要 commit。

**A3**

> 讀 `AGENTS.md`，再讀 `docs/plans/audit_fix_plan.md` 第 1 節與「A3」節，只做 A3（A1、A2、A4 已完成）。在 `_opencode_read_permission` 加 `sibling_run_names`，`OpenCodeAgentRunner.run` 列出其他 run 目錄名稱傳入；目錄不存在時不得拋例外。更新 `src/etf_agent/automation/README.md` 與 `AGENTS.md` 的 Codex 條目（各加一句已知差距）。Codex runner 的程式不改。依計畫補測試、跑三個驗證指令。不要 commit。

**B**（使用者決定後才可使用）

> 讀 `AGENTS.md`，再讀 `docs/plans/audit_fix_plan.md` 第 1 節、「B」節與 4.1。使用者的決定是：〈決定〉。只做 B。不得編輯 `config/decision_policy.json`，不得修改 `allocation.py`。新增版本閘門 `position_sizing.revision_bounds`（實作、不開啟）：在 `decision/sizing.py` 登記與驗證、在 `strategy_reflection/contracts.py` 列入禁區（此檔有他人修改，只加一行）、在 `decision/risk.py` 的 `_validate_action` 加下限與步距檢查（閘門不存在時行為必須逐位元不變）、在 `automation/risk_stages.py` 的簡報與 `tradeagent/risk/portfolio-risk-review/references/risk-review-output.md` 補說明。依計畫新增 `tests/decision/test_revision_bounds.py`，並更新計畫列出的三份文件（只新增段落）。跑三個驗證指令。不要 commit。回報時附上要由人加入設定的 JSON 片段（數值留空）。

**C**（使用者決定後才可使用）

> 讀 `AGENTS.md`，再讀 `docs/plans/audit_fix_plan.md` 第 1 節、「C」節與 4.2。使用者的決定是：〈決定〉。只做 C（A2、G1、B 已完成）。`team_stages.py` 有他人修改，先 `git diff` 看過、不得還原。新增 `src/etf_agent/decision/trader_background.py`（閘門 `position_sizing.trader_background_record`、建立與驗證函式），在 `strategy_lessons.py`、`thesis_verdicts.py` 各加一個回傳完整檔案的函式（既有 `load_*` 的回傳值不得改變），接到 `run_trader`、`build_team_inputs`、Finalizer 的選配鍵雙向檢查與 `daily_pipeline.py`。`team_inputs` 的 `schema_version` 不得改變；沒有閘門時簡報與 `team_inputs` 必須逐鍵不變，既有測試不得需要改期望值。依計畫新增測試與文件段落。不得編輯 `config/decision_policy.json`。跑三個驗證指令。不要 commit。

**D**（使用者決定後才可使用）

> 讀 `AGENTS.md`，再讀 `docs/plans/audit_fix_plan.md` 第 1 節、「D」節與 4.3。使用者的決定是：〈決定〉。只做 D。在 `src/etf_agent/strategy_reflection/service.py` 的 `replay_gate` 回傳值新增 `in_sample` 與 `available_at_overridden_days`，`passed`、`reasons`、`segments` 的計算不得改動；`apply_reflection` 的歷史摘要與 `report.py` 的週報各補一處；`docs/decision-contracts.md` 補一句。先搜尋 `"replay_gate"` 確認沒有欄位白名單會擋新鍵。不重做任何既有回放。依計畫補測試、跑三個驗證指令。不要 commit。

**E1**

> 讀 `AGENTS.md`，再讀 `docs/plans/audit_fix_plan.md` 第 1 節與「E1」節，只做 E1。先執行計畫中的 sqlite 指令確認 `source_date` 都以日期開頭。`SnapshotRepository.load_universe` 加選配參數 `cutoff_date`，`data/snapshot.py` 的呼叫處傳入 cutoff 的台北日期。不改資料表結構。依計畫在 `tests/data/` 補四個情境的測試。跑三個驗證指令。不要 commit。

**E2**

> 讀 `AGENTS.md`，再讀 `docs/plans/audit_fix_plan.md` 第 1 節與「E2」節，只做 E2。在 `src/etf_agent/calibration/prices.py` 的 `ex_rights_symbols` SQL 加 `AND fetched_at <= ?`（參數 `self.as_of`），比照同檔 `trading_days`。在 `tests/calibration/test_calibration.py` 補三個測試（早於、晚於、等於 `as_of`）。跑三個驗證指令。不要 commit。

**F**

> 讀 `AGENTS.md`，再讀 `docs/plans/audit_fix_plan.md` 第 1 節與「F」節，只做 F（C 已完成）。`decision/trader.py` 與 `automation/team_stages.py` 有他人修改，先 `git diff` 看過、不得還原。第一步改文件用語（`AGENTS.md` 與 `docs/decision-contracts.md`）；第二步在交易驗證器加選配參數 `background_texts`，只由 `run_trader` 的即時驗證傳入，Finalizer 與任何重建路徑不得傳入；前次決策的 `invalidation_conditions` 不列入。不傳參數時行為必須與修改前相同。依計畫在 `tests/decision/test_trader.py` 補測試。跑三個驗證指令。不要 commit。

**G1**

> 讀 `AGENTS.md`，再讀 `docs/plans/audit_fix_plan.md` 第 1 節與「G1」節，只做 G1。`DailyDecisionPipeline.run`／`_run` 加關鍵字參數 `require_nav_history: bool = False`；為真、Policy 有 `drawdown_control` 且 `nav_history is None` 時，在任何子 Agent 被呼叫前拋 `DailyPipelineError`。`scripts/run_daily_pipeline.py` 的 `pipeline.run(...)` 傳 `require_nav_history=True`。`evaluation/service.py` 不改。`tests/automation/test_daily_pipeline.py` 有他人修改，只新增測試方法。跑三個驗證指令。不要 commit。

**G2**

> 讀 `AGENTS.md`，再讀 `docs/plans/audit_fix_plan.md` 第 1 節與「G2」節，只做 G2。只加測試、不改程式：在 `tests/competition/test_fallback_dplan.py` 新增測試類別，比對 `src/etf_agent/competition/fallback.py` 的五個常數與 `config/decision_rules.json`（代號去掉 `.TW`／`.TWO` 後比對），並用 `patch.object` 寫一個不一致時會被偵測到的測試。跑三個驗證指令。不要 commit。

**H**

> 讀 `AGENTS.md`，再讀 `docs/plans/audit_fix_plan.md` 第 1 節與「H」節，只做 H（其他項目都已完成）。只改 `AGENTS.md` 與 `docs/` 下的文件，不改程式、設定、測試。先用計畫中的指令讀出 `config/decision_policy.json` 現值（只讀），以現值為準逐列修正計畫表格列出的不一致；`position_review` 的數值那一句不要動。不刪歷史與回滾樣板路徑。完成後執行計畫「驗收」的三個 grep 與 `git diff --check`、`git diff --stat`。不要 commit。回報核對時的 `policy_id` 與仍保留「尚未開啟」字樣的項目清單。

## 6. 完成定義

- 十三項各自的驗收標準都滿足，全套 pytest、compileall、`git diff --check` 通過。
- `config/decision_policy.json` 沒有被本計畫的任何一項修改。
- 沒有任何 commit、merge、push。
- 新閘門（`revision_bounds`、`trader_background_record`）在 `docs/status.md` 列為「已實作、尚未開啟」，由使用者決定是否開啟。

## 7. 核實結果與原發現不同之處

1. **A**：除了 `brief_swap_nomination*` 與 `report.json`，另外找到未被擋的 `var/strategy_reflection/` 下的論點檢視產物與週報（共 72 個檔含 claim 識別碼）、`brief_selection_ranking*`、`ranker_*`，以及一個 `pipeline.json`。已納入 A1、A2。
2. **A（Grep）**：全部 trace 中 Grep 只出現 3 次，回應在 0.1 秒內回來，推測是被拒絕，但無法由 trace 證實；A4 讓之後可以證實。
3. **A（Codex）**：時序隔離的限制已寫在程式說明與 `AGENTS.md`，是已知限制、不是新漏洞；計畫只補文件。
4. **B**：`_weight_limit` 現在位於 `allocation.py:2103-2107`（原寫 2101-2102）。另查得 28 個封存 run 從未用過任何修正動作。
5. **C**：行號因他人修改位移為 `team_stages.py:495-526`。
6. **D**：樣板時間改寫有被記錄與顯示（`available_at_overridden`），`replay_gate` 的說明也已承認用同一段歷史，`auto_apply` 目前為 false。這是已標示的設計限制，計畫降為 P2 並以補揭露為主。
7. **E**：交易池已有 `STALE_UNIVERSE_VALIDATION` 的部分防護；`instruments` 沒有版本史，只能用 `source_date` 過濾。目前 150 檔的 `source_date` 都早於所有比賽期 cutoff，實際未發生洩漏。
8. **F**：後半（不傳決策庫只做結構驗證）在現行設定下不成立：`cash_ratio_range.selection_check` 已開，沒有決策庫會直接拒絕；兩個 CLI 也都會帶決策庫。
9. **G**：硬規則兩個來源目前數值一致；差異只在代號形式（`2330.TW` 對 `2330`）。
10. **H**：另外找到 `docs/status.md:79`（`reentry_cooldown` 寫 v2、設定為 v3）、`docs/status.md:70`（`start_rebuild 3`）與 `AGENTS.md` 中 `position_review` 數值（使用者正在修改，H 不處理）。`derisk_day_view` 在設定中已存在。
