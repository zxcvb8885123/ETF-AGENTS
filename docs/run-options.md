# `./start.sh run` 可調整的參數

參數會原樣傳給 `scripts/run_daily_pipeline.py`，完整說明用 `./start.sh run --help` 查看。不帶任何參數即使用下表「預設」欄。

## 控制用量與模型

| 參數 | 預設 | 作用 |
| --- | --- | --- |
| `--agent-cli claude\|codex\|opencode` | `claude` | 子 Agent 使用的 CLI。換執行器時不可續用其他執行器的 run。 |
| `--model 模型` | Claude CLI 為 `opus`；其他 CLI 沿用該 CLI 設定 | 交易、現金姿態與風險審查使用的模型（最後拍板的階段）。 |
| `--research-model 模型` | Claude CLI 為 `sonnet`；其他 CLI 沿用 `--model` | 只用於多頭／空頭研究員批次。傳 `inherit` 則沿用 `--model`。 |
| `--analyst-model 模型` | Claude CLI 為 `sonnet`；其他 CLI 沿用 `--model` | 只用於技術／基本面／事件三位分析師的批次，量最大。傳 `inherit` 則沿用 `--model`。 |
| `--max-workers N` | 8 | 分析師、多空研究與交易階段同時執行的子 Agent 數（1～8）；1 為串行。只縮短時間，不改決策內容；同時呼叫越多越容易撞到用量上限。現金姿態與風險審查仍串行。同一 Snapshot 實跑對照：3 時 Agent 階段合計 928.7 秒，6 時 601.0 秒（約少 35%），皆未撞用量上限；6 的那次有 1 次事件批次驗證重試。2026-10-25 起預設改為 8（分析師 15 批由 3 輪變 2 輪，估計省 2～3 分鐘；用量不變），尚未有實跑紀錄。`pipeline.json` 的 `stage_durations` 記錄各階段秒數。 |
| `--technical-gate` ／ `--no-technical-gate` | 開啟 | 趨勢一致的股票技術 brief 只附 `momentum`、不附逐日歷史，約少一半輸入；資料不足或訊號衝突者仍附完整歷史。同 Snapshot 對照結果見[降 token 計劃](token_reduction_plan.md)，樣本只有一天。 |
| `--max-budget-usd 金額` | 3 | 單次子 Agent 呼叫的估算用量上限，只適用 Claude CLI。 |

## 續跑與重跑

| 參數 | 預設 | 作用 |
| --- | --- | --- |
| `--resume [RUN_ID]` | 關 | 沿用已通過驗證的 Agent 輸出續跑，例如撞到用量上限。不帶值：續跑今天最近一個未完成的 run；帶 `RUN_ID`（如 `daily-20261020-180000`）：續跑指定 run，跨午夜或額度重置後使用。找到可續跑的 run 時自動沿用既有 Snapshot，不重新更新資料。已還原成重跑前狀態的 run（`pipeline.json` 有 `rolled_back`）不能續跑，改用 `--force`。Snapshot 截止時間早於 `config/decision_policy.json` 的 `available_at`（目前 2026-10-23 00:00 台北）的舊 run 也不能續跑，建立 Policy 時會被擋下（策略設定晚於資料截止）；改用 `--force` 重抓資料重跑。 |
| `--skip-data` | 關 | 一律沿用既有 Snapshot 與帳戶快照，不重新執行資料更新，不看更新時間。續跑、或 Snapshot 已含應有的最新交易日資料（150 檔齊全，且是該日 15:00 後建立）時會自動套用，通常不用手動加。 |
| `--force` | 關 | 休市日或當日已完成時仍執行（休市日 15:00 後若隔天是交易日，不加也會做隔天的備案）；預設同一台北日期、同一目標時段在 1 小時內已完成就不重跑（早上為當天時段、晚上為隔天時段，兩者不算重複）；更早完成的（例如半夜手動跑）照常重跑，舊 run 目錄與 D-Plan 版本都保留。 |
| `--delivery-only RUN_ID` | 無 | 只重建既有 run 的最終交付，不執行資料準備或 Agent，不耗用量。 |

## 檢查與輸入

| 參數 | 預設 | 作用 |
| --- | --- | --- |
| `--preflight-only` | 未啟用 | 預檢（Docker、Agent CLI 登入、帳戶、必要檔案、磁碟）每次 `run` 都會先執行，失敗即停止；預檢通過後繼續跑決策。啟用此參數則檢查完就結束，不抓資料、不跑 Agent，不耗用量。 |
| `--min-free-gb GiB` | 2.0 | 執行前要求的最低可用磁碟空間。 |
| `--account-id ID` | `config/active_account.json` 的帳戶 | 指定帳戶。所有入口預設讀同一個設定檔，比賽第一天開新帳戶時會自動切換。 |
| `--datapack PATH` | 自動尋找 | 指定要對帳的主辦方資料包。省略時在 `var/datapack/` 找本隊、本交易日 `generated_at` 最新的一份。 |
| `--no-market-news` | 關 | 不接入全市場新聞，市場情緒改用恐懼貪婪指標。新聞或事件分析步驟出問題時的緊急開關，`pipeline.json` 記為 `market_news: disabled`。 |
| `--no-auto-resume` | 關（預設會自動續跑） | 決策鏈失敗時，若離 08:55 還有 25 分鐘以上、且錯誤不是額度用完或安全拒絕，程式沿用已驗證輸出自動續跑一次（`pipeline.json` 的 `auto_resume` 記錄）；加此參數則不續跑。 |
| `--rebase-to-datapack` | 關 | 帳本與資料包不一致時（例如前日整批作廢、被剔除），以資料包重建帳本，再重抓資料建立新 Snapshot 與決策前快照，對帳一致才跑 Agent。舊帳本狀態保留在雜湊鏈。不能和 `--skip-datapack`、`--resume` 同用。 |
| `--skip-datapack` | 關 | 明確略過資料包對帳，`pipeline.json` 會記為 `skipped`。比賽期間不要用。 |
| `--datapack-cash-tolerance 比率` | 0.0005 | 帳本與資料包現金差的容忍比率（分母 `nav_tradable`）。實際差額寫在 `pipeline.json` 的 `datapack.cash`，可依真實資料包校準。 |
| `--perception BUNDLE RESULT` | 無 | 提供市場情緒資料：資料包與其驗證結果，兩個檔案必須同時給。不給時這類資訊標為「無資料」。目前尚未接入合法來源，平常不需要使用。 |

## 常見組合

```bash
./start.sh run --preflight-only --skip-data                  # 不耗用量，先確認環境
./start.sh run --resume                                      # 撞到用量上限後，續跑今天未完成的 run
./start.sh run --resume daily-20261020-180000                # 跨午夜後，續跑指定的 run
./start.sh run --no-technical-gate --analyst-model inherit   # 回到最保守的完整輸入、單一模型
./start.sh run --delivery-only daily-20261020-213724         # 只重建報告
```

不在表中的設定（Policy 乘數、現金姿態對應、`max_revisions` 等）在 `config/decision_policy.json`，不是命令列參數，修改須符合[架構文件](architecture.md)的契約。`./start.sh status` 另可用 `--database 路徑` 指定資料庫。
