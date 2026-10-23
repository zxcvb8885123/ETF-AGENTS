# evaluation｜Agent 評測

回答四個問題，**不屬於決策鏈**：只讀資料庫與已封存的每日 run，重跑結果寫在 `artifacts/evaluation/`，有自己的決策庫，不碰正式決策庫、帳本或 D-Plan。

| 問題 | 怎麼量 | 會不會花用量 |
| --- | --- | --- |
| 資料抓齊了嗎（招回率） | 重新解析資料庫裡官方來源最新一次原始回應，看交易池項目有幾成已存、是否為最新版；月營收與財報另看 150 檔是否都有；行情看各來源最新交易日覆蓋 | 不會 |
| 和當時結論一樣嗎 | 以已封存的 `decision_input.json` 當考卷重跑，比較分析師逐檔看法、事件重要程度、交易意圖／信心、現金姿態與委託 | 會 |
| 重跑穩不穩 | 同一份考卷重跑多次，兩兩比較同樣的項目；正面↔負面算「相反」 | 會 |
| 跑的過程 | 每次呼叫的重試、驗證失敗次數與前幾項錯誤、用量與耗時 | 跟著重跑一起記 |
| 準不準 | 考卷的答案是之後真正的官方成交均價：看好的（分析師正面、buy／add、買單）有沒有贏交易池平均，看壞的（負面、trim／exit、賣單）有沒有輸 | 不會 |

「相同」只表示和當時那次一樣，不代表對錯。答案隨時間自己長出來：價格沒到齊的 horizon 標 pending，之後再跑 `report` 就會更新。

## 檔案清單

| 檔案 | 職責 | 主要公開項目 |
| --- | --- | --- |
| `contracts.py` | 版本、錯誤型別與比較項目 | `EvaluationError`、`EVALUATION_SCHEMA_VERSION` |
| `coverage.py` | 資料抓齊率（只讀、不連網） | `data_coverage`、`document_recall`、`universe_coverage`、`price_coverage` |
| `compare.py` | 讀出可比較的結論並計算相同比例、相反次數與過程統計 | `load_outputs`、`compare_outputs`、`pairwise_stability`、`summarize`、`process_stats` |
| `answers.py` | 考卷答案（之後的官方均價超額報酬）與命中率；沿用 `calibration` 的價格與樣本門檻 | `answer_key`、`score_day`、`pool_scores`、`score_runs` |
| `service.py` | 建立評測集、重跑、產生報告 | `build_suite`、`run_suite`、`evaluate`、`render_report`、`eval_policy_template` |

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

- 測試：`tests/automation/test_agent_eval.py`。
