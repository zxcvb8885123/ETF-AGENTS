# evaluation｜Agent 評測

回答四個問題，**不屬於決策鏈**：只讀資料庫與已封存的每日 run，重跑結果寫在 `artifacts/evaluation/`，有自己的決策庫，不碰正式決策庫、帳本或 D-Plan。

| 問題 | 怎麼量 | 會不會花用量 |
| --- | --- | --- |
| 資料抓齊了嗎（招回率） | 重新解析資料庫裡官方來源最新一次原始回應，看交易池項目有幾成已存、是否為最新版；月營收與財報另看 150 檔是否都有；行情看各來源最新交易日覆蓋 | 不會 |
| 和當時結論一樣嗎 | 以已封存的 `decision_input.json` 當考卷重跑，比較分析師逐檔看法、事件重要程度、交易意圖／信心、現金姿態與委託 | 會 |
| 重跑穩不穩 | 同一份考卷重跑多次，兩兩比較同樣的項目；正面↔負面算「相反」 | 會 |
| 跑的過程 | 每次呼叫的重試、驗證失敗次數與前幾項錯誤、用量與耗時 | 跟著重跑一起記 |
| 準不準 | 考卷的答案是之後真正的官方成交均價：看好的（分析師正面、buy／add、買單）有沒有贏交易池平均，看壞的（負面、trim／exit、賣單）有沒有輸 | 不會 |
| 換 Policy 會怎樣（便宜版回測） | 多日滾動回放：逐日讀封存 run 的 Agent 輸出（交易意圖／信心、現金姿態、多空辯論），用指定策略樣板重算權重、張數、訂單，依帳本口徑以下一交易日官方均價成交、費稅、T+2 交割，結轉到下一天；兩個 Policy 可同期比較 | 不會 |

「相同」只表示和當時那次一樣，不代表對錯。答案隨時間自己長出來：價格沒到齊的 horizon 標 pending，之後再跑 `report` 就會更新。

## 檔案清單

| 檔案 | 職責 | 主要公開項目 |
| --- | --- | --- |
| `contracts.py` | 版本、錯誤型別與比較項目 | `EvaluationError`、`EVALUATION_SCHEMA_VERSION` |
| `coverage.py` | 資料抓齊率（只讀、不連網） | `data_coverage`、`document_recall`、`universe_coverage`、`price_coverage` |
| `compare.py` | 讀出可比較的結論並計算相同比例、相反次數與過程統計 | `load_outputs`、`compare_outputs`、`pairwise_stability`、`summarize`、`process_stats` |
| `answers.py` | 考卷答案（之後的官方均價超額報酬）與命中率；沿用 `calibration` 的價格與樣本門檻 | `answer_key`、`score_day`、`pool_scores`、`score_runs` |
| `service.py` | 建立評測集、重跑、產生報告 | `build_suite`、`run_suite`、`evaluate`、`render_report`、`eval_policy_template` |
| `replay_market.py` | 回放自己的模擬帳本（沿用 `ledger`）與官方成交價來源（沿用 `OfficialCloseMarket`，只看 `as_of` 前抓到的列、拒絕前視價格） | `ReplayLedger`、`ReplayMarket` |
| `replay_tools.py` | NAV 績效指標（總報酬、年化、最大回撤、Sharpe、換手率、費稅）與來源／版本雜湊 | `nav_metrics`、`tree_sha256`、`policy_template_sha256`、`code_version` |
| `replay.py` | 挑 run、驗證封存、逐日換帳戶重算 policy／配置／Guard／成交、封存報告 | `select_runs`、`load_archived_run`、`build_replay`、`save_replay`、`read_replay`、`ReplayRepository` |
| `replay_report.py` | 回放 Markdown 與兩個 Policy 的同期比較 | `render_replay_report`、`compare_replays`、`render_comparison` |
| `replay_validator.py` | 以報告記錄的輸入整段重算並逐欄比對 | `verify_replay` |

## 口徑與限制

- 考卷：完成、未還原、有 `decision_input.json` 與 `intent.json` 的每日 run；同一份輸入只收一次，輸入檔改變就拒絕重跑。
- 重跑用 `DailyDecisionPipeline.run_from_bundle`：Bundle 重新驗證後才跑，不抓資料。分析師的資料可用性說明仍讀目前的資料庫（只算 cutoff 前的資料），文字可能和當時略有不同。
- 策略設定版本晚於考卷 cutoff 時，把 `available_at` 提前到考卷 cutoff 才能重建 Policy，並在報告標示；其他設定不變。
- 帳本歷史不傳入，回撤控制記為 unavailable。
- 只跑分析師（`--stop-after analysts`）時，交易、現金姿態與委託不比較。
- 準度沿用 `calibration`：成交日 T 是 cutoff 後第一個有官方收盤的交易日，報酬 = T+h 日均價 ÷ T 日均價 − 1，減去同日交易池平均；窗口內除權息的股票不計，有價格的股票不到八成時當天不給答案。超額報酬剛好為 0 的不算命中也不算錯。
- 完整決策日少於 20 天時只給天數與筆數，不給命中率；同一天重跑多次只算一天。報告另列單日結果，只供參考。

## CLI

```bash
.venv/bin/python cli/agent_eval.py coverage                       # 資料抓齊率，不花用量
.venv/bin/python cli/agent_eval.py suite --name test-period       # 用全部合格的每日 run 建評測集
.venv/bin/python cli/agent_eval.py run --suite test-period --repeats 2 --yes
.venv/bin/python cli/agent_eval.py report --eval-id eval-<UTC時間>
```

`run` 不加 `--yes` 只印估算用量（考卷當時用量 × 重跑次數）。重跑和每日排程共用 Claude 用量，不要和 07:00／21:00 排程同時跑。

## 便宜版回測（多日 Policy 回放，零 token）

`cli/policy_replay.py` 不呼叫 Agent，只重算確定性程式：

```bash
# 依目標時段日範圍回放目前的策略樣板，並和另一個樣板同期比較
.venv/bin/python cli/policy_replay.py run --policy config/decision_policy.json \
  --start 2026-10-23 --end 2026-10-31 --compare-policy config/decision_policy_b.json
.venv/bin/python cli/policy_replay.py run --runs decision-20261022T201548Z     # 指定 run
.venv/bin/python cli/policy_replay.py verify --replay-id replay-<UTC時間>       # 重算驗證
.venv/bin/python cli/policy_replay.py compare --replay-a A --replay-b B        # 比較兩個已封存回放
```

口徑：

- 每天讀該日封存 run 的 `intent.json`（意圖／信心）、`team_inputs.json`（現金姿態）、`debate.json`、`momentum.json`；先過 manifest 雜湊、動能重算與 TradeDecision 重建驗證，失敗即停止。
- 把封存輸入的 `account_snapshot` 換成回放帳本（以該日 Snapshot 的決策用價重新估值，與 `prepare_day` 同口徑）後重新驗證，再用指定樣板建 policy：產業分類沿用封存 policy 的 `sector_by_symbol`，回撤由回放 NAV 歷史計算，封存現金姿態低於回撤下限時由程式提高到下限並標示；樣板版本晚於 cutoff 時 `available_at` 提前到 cutoff 並標示。
- 配置、情境、Guard 都是正式引擎；Guard 未通過標 `rejected`、當日不下單（風險 Agent 的修正循環需要 Agent，不重現）。
- 成交日＝目標時段日，必須等於資料庫 cutoff 後第一個官方交易日，否則視為缺價或缺日停止；成交價為官方成交均價、日終以官方收盤估值，只用 `as_of` 前抓到的列，成交日資料在該日收盤前就抓到視為前視價格並拒絕；價格尚未到齊的日子標 `pending_execution` 並停止之後的回放。
- 同一目標時段日有多個 run 時取 cutoff 最新者，略過的 run 記在報告；有官方交易日卻沒有決策 run 時停止，加 `--allow-gaps` 才以收盤估值標為缺日。
- 起點預設為第一個 run 封存的帳戶快照（`--initial-cash` 改全現金）；換 Policy 後持股與實際不同，之後一律讀回放帳本。
- 報告（`artifacts/evaluation/replays/<replay_id>/report.json` 與旁邊的 `.md`）記錄策略樣板 `content_sha256`、`tradeagent/` 目錄雜湊、git commit、使用的 run 與 manifest 雜湊、每筆成交的來源列雜湊；`verify` 以同樣輸入整段重算逐欄比對，程式或 Skill 檔案改變只警告。
- Sharpe 少於 20 個日報酬 unavailable；年化少於 60 個交易日標不可靠；少於 20 個成交日的比較沒有統計意義。交割日以週一至週五近似；公司行動不套用。
- 運算在 Decimal 預設精度（28 位）下進行：每日決策鏈以此封存，本套件經 `calibration.tools` 匯入時全域精度會被改成 40，回放鎖回 28 才能逐位重建。

- 測試：`tests/automation/test_agent_eval.py`、`tests/automation/test_policy_replay.py`。
