# `./start.sh run` 可調整的參數

`./start.sh run` 是完整的每日決策。參數會原樣傳給 `scripts/run_daily_pipeline.py`，完整說明用 `./start.sh run --help` 查看。不帶任何參數就是用下表「預設」欄。排程（21:00、07:00）本身不帶參數，這份文件主要給你手動跑時查。

## 我想做什麼

| 情況 | 指令 |
| --- | --- |
| 不耗用量，先確認環境與登入沒問題 | `./start.sh run --preflight-only --skip-data` |
| 撞到用量上限，續跑今天未完成的 run | `./start.sh run --resume` |
| 跨午夜或額度重置後，續跑指定的 run | `./start.sh run --resume daily-20261020-180000` |
| 只重建某次 run 的報告，不跑 Agent | `./start.sh run --delivery-only daily-20261020-213724` |
| 休市日、或當日已完成仍要重跑 | `./start.sh run --force` |
| 帳本跟資料包對不上 | `./start.sh run --rebase-to-datapack` |
| 回到最保守的完整輸入、單一模型 | `./start.sh run --no-technical-gate --analyst-model inherit` |
| 新聞步驟出問題，緊急關掉全市場新聞 | `./start.sh run --no-market-news` |
| 換用 Codex 或 OpenCode 跑子 Agent | `./start.sh run --agent-cli codex`（或 `opencode`） |

## 控制用量與模型

| 參數 | 預設 | 作用 |
| --- | --- | --- |
| `--agent-cli claude\|opencode\|codex` | `claude` | 子 Agent 使用的 CLI。換執行器後不可續用其他執行器的 run。 |
| `--research-agent-cli claude\|opencode\|codex` | 與 `--agent-cli` 相同 | 多頭／空頭研究員使用的 CLI。多空必須互相看不到對方輸出；Codex 無法限制讀檔，所以研究員為 Codex 時改由「研究階段輸出暫存」保證（兩方都完成才一起寫入），隔離弱於 Claude／OpenCode，且研究階段失敗等於整段重跑。與主 CLI 不同時，兩個 CLI 的登入都要通過；續跑時兩者都須與原 run 相同。 |
| `--model 模型` | Claude 為 `opus`；其他 CLI 沿用其設定 | 交易、現金姿態與風險審查（最後拍板的階段）。 |
| `--research-model 模型` | Claude 為 `sonnet`；其他 CLI 沿用 `--model` | 只用於多頭／空頭研究員。傳 `inherit` 沿用 `--model`。 |
| `--analyst-model 模型` | Claude 為 `sonnet`；其他 CLI 沿用 `--model` | 只用於技術／基本面／事件三位分析師，量最大。傳 `inherit` 沿用 `--model`。 |
| `--max-workers N` | 8 | 同時執行的子 Agent 數（1～8，1 為串行）。只縮短時間，不改決策內容；同時越多越容易撞用量上限。現金姿態與風險審查仍串行。各階段耗時記在 `pipeline.json` 的 `stage_durations`。 |
| `--technical-gate` ／ `--no-technical-gate` | 開啟 | 趨勢一致的股票，技術資料只附動能、不附逐日歷史，輸入約少一半；資料不足或訊號衝突者仍附完整歷史。見[降 token 說明](token_reduction_plan.md)。 |
| `--max-budget-usd 金額` | 3 | 單次子 Agent 呼叫的估算用量上限，只適用 Claude CLI。 |

## 續跑與重跑

| 參數 | 預設 | 作用 |
| --- | --- | --- |
| `--resume [RUN_ID]` | 關 | 沿用已通過驗證的 Agent 輸出續跑，並沿用既有 Snapshot、不重新更新資料。不帶值：續跑今天最近一個未完成的 run；帶 `RUN_ID`：續跑指定 run。以下情況不能續跑，改用 `--force`：run 已被還原成重做前狀態（`pipeline.json` 有 `rolled_back`）；Snapshot 截止時間早於策略設定檔 `config/decision_policy.json` 的生效時間（`available_at`，目前 2026-11-03 20:30 台北）。 |
| `--no-auto-resume` | 關 | 預設在決策鏈失敗時，若離 08:55 還有 25 分鐘以上、且原因不是額度用完或安全拒絕，程式會沿用已驗證輸出自動續跑一次（記在 `pipeline.json` 的 `auto_resume`）。加此參數就不續跑。 |
| `--skip-data` | 關 | 沿用既有 Snapshot 與帳戶快照，不更新資料。續跑時，或 Snapshot 已含最新交易日完整資料（150 檔齊全、且是該日 15:00 後建立）時會自動套用，通常不用手動加。 |
| `--force` | 關 | 休市日或當日已完成仍執行。預設同一台北日期、同一目標時段在 1 小時內已完成就不重跑（早上做當天時段、晚上做隔天時段，兩者不算重複）；休市日 15:00 後若隔天是交易日，不加也會做隔天備案；更早完成的（例如半夜手動跑）照常重跑，舊 run 與 D-Plan 版本都保留。 |
| `--delivery-only RUN_ID` | 無 | 只重建既有 run 的最終交付，不抓資料、不跑 Agent、不耗用量。 |

## 檢查與輸入

| 參數 | 預設 | 作用 |
| --- | --- | --- |
| `--preflight-only` | 關 | 預檢（Docker、Agent CLI 登入、帳戶、必要檔案、磁碟）每次 `run` 都會先做，失敗就停。加此參數則檢查完就結束，不抓資料、不跑 Agent。 |
| `--min-free-gb GiB` | 2.0 | 執行前要求的最低可用磁碟空間。 |
| `--account-id ID` | `config/active_account.json` 的帳戶 | 指定帳戶。所有入口預設讀同一個設定檔，開新帳戶時會自動切換。 |
| `--datapack PATH` | 自動尋找 | 指定要對帳的資料包。省略時在 `var/datapack/` 找本隊、本交易日最新的一份；資料夾可用 `--datapack-dir` 改。 |
| `--rebase-to-datapack` | 關 | 帳本與資料包不一致時，以資料包重建帳本，再重抓資料建立新 Snapshot，對帳一致才跑 Agent。舊帳本狀態保留。不能和 `--skip-datapack`、`--resume` 同用。 |
| `--skip-datapack` | 關 | 略過資料包對帳，`pipeline.json` 記為 `skipped`。比賽期間不要用。不能和 `--datapack` 同用。 |
| `--datapack-cash-tolerance 比率` | 0.0005 | 帳本與資料包現金差的容忍比率。實際差額記在 `pipeline.json` 的 `datapack.cash`，可依真實資料包校準。 |
| `--no-market-news` | 關 | 不接入全市場新聞，市場情緒改用恐懼貪婪指標。`pipeline.json` 記為 `market_news: disabled`。 |
| `--perception BUNDLE RESULT` | 無 | 提供市場情緒資料包與其驗證結果，兩個檔案須同時給。尚未接入合法來源，平常不需要。 |

## 不是命令列參數的設定

策略樣板（等級乘數、現金姿態對應、`max_revisions` 等）在 `config/decision_policy.json`，修改須符合[架構文件](architecture.md)的契約。`./start.sh status` 另可用 `--database 路徑` 指定資料庫。
