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
| 哪一級評級後來有 alpha（回測格子） | 每筆已封存決策（run × symbol）一列，按信心等級、交易意圖與多空強度分組，**並列**對交易池平均與對基準 0050.TW 的超額報酬與贏過比例，不合成單一分數 | 不會 |

「相同」只表示和當時那次一樣，不代表對錯。答案隨時間自己長出來：價格沒到齊的 horizon 標 pending，之後再跑 `report` 就會更新。回放、格子與評測報告旁各有一頁自包含 HTML（衍生品，不進 manifest）。

**證據等級**：回放、格子與評測重跑都只是對舊日期的重算——回放固定已封存的 Agent 意圖重算確定性配置，重跑讓模型重看它可能已在訓練資料裡看過的日期——不構成對 LLM 判斷的前瞻驗證。所有這類報告的 JSON 帶 `evidence_grade: "replay_reference_only"`，Markdown 首段固定寫「僅供參考：回放使用已封存的 Agent 意圖，不構成對 LLM 判斷的前瞻驗證；只有比賽期間的即時紀錄才算證據」（常數在 `calibration.contracts.REPLAY_NOTICE`）。舊封存報告沒有這兩個欄位，`verify` 不比對它們。

## 檔案清單

| 檔案 | 職責 | 主要公開項目 |
| --- | --- | --- |
| `contracts.py` | 版本、錯誤型別與比較項目 | `EvaluationError`、`ReplayError`、`GridError`、`EVALUATION_SCHEMA_VERSION`、`GRID_SCHEMA_VERSION`、`DEFAULT_BENCHMARK_SYMBOL` |
| `coverage.py` | 資料抓齊率（只讀、不連網） | `data_coverage`、`document_recall`、`universe_coverage`、`price_coverage` |
| `compare.py` | 讀出可比較的結論並計算相同比例、相反次數與過程統計 | `load_outputs`、`compare_outputs`、`pairwise_stability`、`summarize`、`process_stats` |
| `answers.py` | 考卷答案（之後的官方均價超額報酬）與命中率；沿用 `calibration` 的價格與樣本門檻 | `answer_key`、`score_day`、`pool_scores`、`score_runs` |
| `service.py` | 建立評測集、重跑（可續跑）、產生報告 | `build_suite`、`run_suite`、`unfinished_runs`、`evaluate`、`render_report`、`eval_policy_template` |
| `grid.py` | 回測格子：讀封存 run 成觀測、逐日逐檔算報酬與兩種超額、分組統計、不可變封存與重算驗證 | `build_grid`、`save_grid`、`read_grid`、`verify_grid`、`GridRepository`、`GRID_ROOT` |
| `html_report.py` | 一頁自包含 HTML（純標準庫、inline SVG／style、CSP、全文 `html.escape`）；只排版 JSON，不重算 | `render_replay_html`、`render_grid_html`、`render_eval_html` |
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
- 完整決策日少於 `calibration.MIN_DECISION_DAYS`（10）天時只給天數與筆數，不給命中率；同一天重跑多次只算一天。報告另列單日結果，只供參考。

## CLI

```bash
.venv/bin/python cli/agent_eval.py coverage                       # 資料抓齊率，不花用量
.venv/bin/python cli/agent_eval.py suite --name test-period       # 用全部合格的每日 run 建評測集
.venv/bin/python cli/agent_eval.py run --suite test-period --repeats 2 --yes
.venv/bin/python cli/agent_eval.py run --suite test-period --repeats 2 --resume eval-<UTC時間> --yes   # 斷點續跑
.venv/bin/python cli/agent_eval.py report --eval-id eval-<UTC時間>  # report.json／report.md／report.html
```

`run` 不加 `--yes` 只印估算用量（考卷當時用量 × 重跑次數；續跑時只算未完成卷次）。重跑和每日排程共用 Claude 用量，不要和 07:00／21:00 排程同時跑。

### 斷點續跑（`--resume`）

- 只重跑沒有 `eval_result.json` 或狀態不是 `completed` 的卷次；已完成的沿用原紀錄。重跑的卷次以 `resume=True` 建 pipeline：卷次目錄內先前的原始 Agent 輸出（`*_raw_N.json`）以**目前的** Validator 重驗，通過就沿用、不再呼叫 Agent；輸入改變時 envelope 對不上自然重跑。
- 續跑前核對：評測集名稱、`repeats`、`stop_after`、manifest 已記錄的每卷 `bundle_sha256`、評測用策略樣板（`policy_template.json` 與目前 `config/decision_policy.json` 經 cutoff 調整後的 canonical 雜湊）任一不符即停止，不會建立任何 pipeline；要換設定請建新的評測 run。
- 卷次紀錄多 `resumed_from_attempts`（續跑前已有的原始輸出份數）與 `resumed_at`；manifest 多 `resumes[]`（每次續跑時間與重跑卷次）；報告標示續跑。

## 回測格子（run × symbol，零 token）

```bash
.venv/bin/python cli/agent_eval.py grid --start 2026-10-23 --end 2026-11-30 [--benchmark 0050.TW] [--as-of ISO]
.venv/bin/python cli/agent_eval.py grid --runs decision-20261022T201548Z ...
.venv/bin/python cli/agent_eval.py grid-verify --grid-id grid-<UTC時間>
```

口徑：

- 輸入只有已封存、manifest 驗證通過的 Decision run（`calibration.build_observation`）與官方成交均價（`OfficialPriceBook`，只用 `fetched_at` ≤ `as_of` 的官方列）。成交日 T、報酬、同日取 cutoff 最晚、除權息排除、單日 80% 覆蓋與 10 個完整決策日的門檻全部沿用 `calibration`，`by_conviction` 與校準報告的數字一致（有測試鎖定）。
- 每列：`entry_day`、`run_id`、`symbol`、`intent`、`conviction`、`bull_strength`／`bear_strength`／`net_strength`，每個 horizon 一格：`return`、`excess_vs_pool`（減同日交易池平均，主）、`excess_vs_benchmark`（減基準 ETF 同窗口報酬，附）、`benchmark_status`；價格未到齊、除權息、無價、覆蓋不足的格只有 `status`。
- 分組（`by_conviction` 只算 buy／add 且有信心；`by_intent`、`by_bull_strength`、`by_bear_strength`、`by_net_strength` 含全部股票）：`n_obs`、`mean_return`、`mean_excess_vs_pool`、`beat_pool`（超額 > 0 的筆數 ÷ 超額 ≠ 0 的筆數，剛好 0 另計 `ties`）、`n_obs_benchmark`、`mean_excess_vs_benchmark`、`beat_benchmark`。**兩種 alpha 並列，不合成單一分數。**
- 基準在 T 或 T+h 缺官方列（含抓取時間晚於 `as_of`、資料庫沒有該 ETF）→ 該日 `benchmark_status=unavailable`；窗口 (T, T+h] 內除權息 → `ex_rights`；兩者都只讓基準欄留空、不計入 `n_obs_benchmark`，池 alpha 照算、不補值。`horizons.<h>.benchmark_days` 統計三種狀態的天數。
- `inputs.run_ids` 釘住實際納入（含同日被取代）的 run；`grid-verify` 以它重建逐欄比對，決策庫之後新增 run 不影響既有格子；程式版本改變只警告。`content_sha256` 不含 `grid_id`／`generated_at`。
- `provenance.provenance_class` 本輪一律 `archived`。

### 下一步：往更早歷史重建（尚未實作）

格子契約預留 `provenance_class="reconstructed"`，但本輪**不**往比賽前重建決策，原因：

- AGENTS.md 要求評測只收已封存、manifest 驗證通過的 run，且「今日補抓的可得時間不得回填到過去」；重建歷史決策需要每日 Snapshot 的 `available_at` 時間線，今天的資料庫只有行情、財報與有原始發布時間的公告能滿足，新聞與情緒無法重現當時可得集合。
- 模型記憶洩漏：Agent 模型的知識截止約 2026-06，之前日期的重跑可能憑記憶而非當時資料，結果不能和真實決策混在一起比較。
- 若日後實作，必須：只用行情／財報與有原始發布時間的資料、每份重建 run 標 `provenance_class="reconstructed"` 與模型知識截止、與 `archived` 分開統計、**不得**餵給 `strategy_reflection` 的自動套用（本套件的 `grid`／`html_report` 已禁止被 `decision/`、`automation/`、`strategy_reflection/` 匯入，有測試鎖定）。

## 一頁 HTML 報告

`save_replay`、`save_grid` 與 `evaluate` 會在 JSON 旁寫 `<replay_id>.html`、`<grid_id>.html`、`report.html`：純標準庫產生，inline SVG（NAV 折線、分組平均超額長條）與 inline style，不含 script、外連資源、`url(`、`<link>`、`<iframe>`，頁首帶 CSP meta（`default-src 'none'`），所有來自 JSON 的文字一律 `html.escape`。它只排版、不重算：NAV 少於兩點或沒有可畫的分組平均時顯示「無圖」；三種 HTML（回測格子、策略重算、評測）為了好讀，都不附頁尾指紋與重算指令、也不放提醒首段，標題用報告日期；回測格子的提醒改為「怎麼讀這份報告」的一條說明（驗證請用 JSON 與 CLI 的 verify）。HTML 是衍生品，寫在不可變 run 目錄**旁邊**、不進 manifest，可隨時由 JSON 重新產生。

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

- 測試：`tests/automation/test_agent_eval.py`（含續跑）、`tests/automation/test_policy_replay.py`、`tests/automation/test_decision_grid.py`（格子、HTML、隔離）。
