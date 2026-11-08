# 執行時資料夾：`artifacts/` 與 `var/`

程式跑起來之後，會在專案根目錄產生 `artifacts/`（報告、決策、帳本）和 `var/`（資料庫、日誌）兩個資料夾。這份文件告訴你：每個資料夾放什麼、誰寫入、能不能刪、想找某樣東西該去哪。

## 先記住這幾件事

- 兩個資料夾都不進 git，新環境一開始是空的，各子資料夾在第一次成功寫入時才建立。
- 裡面是真實執行產物，不是測試資料（測試資料在 `tests/fixtures/`）。
- 寫入者是 `./start.sh` 與每日流程 `scripts/run_daily_pipeline.py`。Docker 的 `agent` 服務可寫這兩個資料夾，`dashboard` 服務只能讀。
- **絕對不要刪**：`var/etf_agent.db`（所有資料與來源原文，刪了無法依原時間點重建）、`artifacts/ledger/`（帳戶唯一狀態，刪了帳戶歷史與績效消失）、`artifacts/decisions/`（報告、D-Plan、帳本結算都靠它重建）。
- 日期資料夾一律用台北日期 `YYYY-MM-DD`，只是排列方式，run 名稱與雜湊不含日期。

## 哪個步驟寫到哪裡

| 步驟 | 指令 | 寫到 |
| --- | --- | --- |
| 抓資料、建快照 | `./start.sh daily`（或 `auto`） | `var/etf_agent.db`、`artifacts/data/`、`artifacts/debug/` |
| 推進虛擬帳本 | `./start.sh daily` 內的 `cli/virtual_account.py daily` | `artifacts/ledger/` |
| 跑決策鏈 | `./start.sh run`（會先執行 `daily`） | `artifacts/daily/`、`artifacts/decisions/` |
| 產生中文報告 | 同上，或 `cli/decision_delivery.py` | `artifacts/reports/`、`artifacts/latest/` |
| 產生 D-Plan | 同上，或 `cli/dplan.py` | `artifacts/competition_d_plan/`（備援候選在 `artifacts/fallback_decisions/`） |
| 看儀表板 | `./start.sh` | 只建帳戶（`artifacts/ledger/`），其餘唯讀 |

## `artifacts/` 各資料夾

「能刪嗎」是依程式行為給的建議，刪之前請自行評估。

| 路徑 | 放什麼 | 能刪嗎 |
| --- | --- | --- |
| `latest/` | 最新中文報告捷徑：`latest.md`（報告）與 `latest.json`（索引，權威檔）。失敗時放失敗診斷 | 可刪，下次交付會重建 |
| `daily/` | 每日流程的狀態與 Agent 中間產物，見下節 | 可刪舊日期；刪了無法續跑與審計當日 Agent 原始輸出 |
| `decisions/` | 不可變的決策封存，含決策輸入與所有中間結論 | 不建議刪 |
| `reports/` | 決策報告：`runs/`（交付紀錄）、`views/`（閱讀副本）、`dated/`（依日期好找的副本） | 不建議刪；刪了要用 `cli/decision_delivery.py` 從決策重建 |
| `competition_d_plan/` | D-Plan 候選：`D-Plan_<team_id>_<交易日>.json`、每版歷史 `versions/`、內部 `contexts/` | 已人工上傳的可刪；同日重跑會取代主檔，已上傳的檔案不會跟著更新 |
| `fallback_decisions/` | 備援 D-Plan（全部續抱、無委託）的封存 | 可刪舊的 |
| `ledger/` | 虛擬帳本與帳戶快照，見下節 | 不要刪 |
| `data/` | 最新研究快照與資料收集狀態，見下節 | 多數可刪，下次收集會重建 |
| `debug/` | 來源原始抓取（`source_audit/`）與探測報告（`source_probe/`），只在稽核來源時用 | 可刪 |
| `archive/` | 被新決策取代的舊決策（`superseded_decisions/`） | 可刪；這些決策已不參與帳本 |
| `calibration/` | 校準旁觀者的觀測紀錄，依日期分資料夾 | 可刪，由決策封存重新記錄 |
| `code_versions/` | D-Plan 標記的程式版本。程式有未提交修改時，把修改內容存在這裡，讓當天執行的程式能還原 | 不建議刪，D-Plan 的程式版本會指向這裡 |
| `evaluation/` | Agent 評測與多日回放的輸出（手動執行才有） | 可刪 |
| `cross_market_study/` | 跨市場隔夜訊號離線研究的資料與結果（手動執行才有） | 可刪 |
| `fundamental_repairs/`、`market_context_probes/`、`research_report.*`、`event_research_validated.json` | 手動 CLI 的預設輸出，每日流程不用 | 可刪 |

### 每日流程資料夾 `daily/`

| 路徑 | 內容 |
| --- | --- |
| `daily/<日期>/daily-<YYYYMMDD>-<HHMMSS>/pipeline.json` | 這次執行的總狀態：決策 run、交付結果、D-Plan 結果、阻擋原因、每次 Agent 呼叫的耗時與用量，以及這次各 Skill 檔案的版本指紋（`skill_versions`）。**今天流程成功還是失敗，先看這個** |
| 同資料夾 `brief_*.json` | 給子 Agent 的任務簡報，用來審計當時餵了什麼 |
| 同資料夾 `analyst_*.json` | 三位分析師的報告 |
| 同資料夾 `*_raw_<N>.json` | 子 Agent 的原始輸出；續跑時靠這些檔判斷哪些呼叫已完成 |
| 同資料夾 `decision_input.json`、`trading_status.json`、`datapack.json` | 當次的決策輸入、交易狀態與資料包對帳副本 |
| 同資料夾其他檔（多空、交易、風險、修正紀錄等） | 決策鏈走到哪就有到哪 |
| `daily/.lock` | 防止兩個每日流程同時跑。被佔用時略過並通知；超過 6 小時沒結束會標示疑似殭屍 |
| `daily/.data.lock` | 防止兩個抓資料流程同時寫資料庫；等太久（預設 30 分鐘）以結束碼 75 停止並通知 |
| `daily/data-launchd.log`、`finmind-news-launchd.log` | 對應排程的輸出，安裝排程後才有 |

### 決策封存 `decisions/`

`decisions/<日期>/decision-<UTC時間>Z/` 是一次決策的完整封存：決策輸入、動能、多空辯論、交易意圖、政策、委託提案、情境、風控檢查、風險審查、修正紀錄、最終決策、團隊輸入各一個 `.json`，外加 `manifest.json`。內容用雜湊鎖住，改動會被驗證擋下。

### 帳本 `ledger/`

| 路徑 | 內容 |
| --- | --- |
| `ledger/<account_id>/latest.json` | 指向最新已驗證狀態 |
| `ledger/<account_id>/account_snapshot_latest.json` | 決策前的帳戶快照，每日決策讀它 |
| `ledger/<account_id>/runs/genesis/` | 初始狀態，刪了帳戶就沒有起點 |
| `ledger/<account_id>/runs/prepare-<UTC時間>Z/` | 每日推進後的帳本狀態，序號必須連續 |
| `ledger/<account_id>/rolled_back/` | 開盤前重跑退回的狀態，不在帳本鏈上，可刪 |
| `ledger/<account_id>/.write.lock` | 寫入時暫時存在；若殘留，確認沒有程式在跑才可刪 |

### 資料 `data/` 與除錯 `debug/`

| 路徑 | 內容 | 刪了會怎樣 |
| --- | --- | --- |
| `data/research_snapshot_latest.json` | 最新研究快照 | 每日決策需要，下次 `daily` 重建 |
| `data/financial_statements_latest.json`、`sector_classification_latest.json`、`official_history/latest.json` | 財報、產業分類、官方歷史行情的補抓報告 | 只是報告，可刪。正式決策用的產業分類是 `data/sector_classification.json`（專案根目錄的 `data/`） |
| `data/finmind_schedule.json` | FinMind 財報補抓進度 | 刪了視為首次補抓，重新消耗額度 |
| `data/trading_status/<日期>/<UTC時間>Z/` | 開休市日期表與注意、處置、暫停交易股票的政府 CSV（八份全成功才取代舊的，只留最新） | 刪了算不出交割日；系統會在截止時間前嘗試重抓 |
| `data/market_news/` | 全市場新聞原始回應與整理項目 | 可刪舊的；刪了當天新聞改用恐懼貪婪指標 |
| `debug/source_probe/`、`debug/source_audit/` | 來源探測、交易池核對、官方公司基本資料原始回應 | 可刪 |

## `var/` 各項目

| 路徑 | 放什麼 | 能刪嗎 |
| --- | --- | --- |
| `etf_agent.db` | SQLite 資料庫：行情（每次內容改變就新增版本）、公司文件、財報、來源原文、快照、新聞等，全部帶可得時間 | 不要刪 |
| `etf_agent.db-wal`、`-shm` | SQLite 運作中的暫存 | 資料庫使用中不要手動刪 |
| `datapack/` | 人工從參賽者後台下載的主辦方資料包（`DataPack_<team_id>_<日期>.json`） | 可刪舊檔；當天的不要刪 |
| `logs/daily-<YYYY-MM-DD>.log` | 每日流程自己的訊息，一天一檔，超過 30 天自動清除 | 可刪 |
| `logs/daily_status.log` | 每次排程結束寫的一行狀態 | 可刪 |
| `logs/launchd.log` | 安裝排程後，排程整個行程的輸出；只追加不輪替，大了可清空 | 可清空 |
| `strategy_reflection/` | 每週策略反思：封存、報告 `reports/weekly-<日期>.md`、套用歷史 `history.json`、套用前備份 `policies/` | 刪 `history.json` 會失去冷卻期與回滾依據，刪 `policies/` 就無法回滾 |
| `inferred_closed_days.json` | 程式推斷的臨時休市日（如颱風假） | 可刪，會重新推斷 |
| `yahoo_industries.json`、`current_price_snapshot.json`、`current_strategy_proposal.json`、`etf_agent.db.pre-source-documents-migration.bak` | 舊版遺留檔，程式不再使用 | 確認不需要後可刪 |

## 保存規則

- **FinMind 財報是候選資料**，不進正式快照。首次補 `FINMIND_YEARS` 年歷史（預設 3，可設 2 至 5），之後只在財報公布區間每天更新一次（約 2/15-4/10、4/20-5/25、7/20-8/24、10/20-11/24），日常只抓最近兩個季末；`FORCE_FETCH=1` 可強制更新。補抓的歷史以抓取時間為可得時間，不回填到過去的決策。
- **交易狀態封存**：每次八份政府 CSV 全部成功才取代舊封存，任何一份失敗就保留舊的。每日決策另把當次交易狀態存進該次 `daily/` 資料夾，重建不依賴舊封存。
- **重複 run 名稱**：同一個 run 名稱出現在兩個日期資料夾會被拒絕。
- **舊名稱**：`daily_runs`、`decision_runs`、`decision_reports`、`latest_reports`、`virtual_accounts`、`source_audit`、`source_probe_reports`、`official_history` 與頂層 `*_latest.json` 都已清除，沒有相容連結，不要搬回來。

## 我想找 X 該看哪裡

| 我想找 | 看哪裡 |
| --- | --- |
| 今天最新的中文決策報告 | `artifacts/latest/latest.md`（依日期的副本在 `artifacts/reports/dated/<日期>/`） |
| 今天流程成功還是失敗、為什麼 | `artifacts/daily/<日期>/daily-<YYYYMMDD>-<HHMMSS>/pipeline.json`；失敗時也看 `artifacts/latest/latest.md` |
| 每日流程當天的訊息 | `var/logs/daily-<YYYY-MM-DD>.log` |
| 排程輸出的錯誤 | `var/logs/launchd.log`、`artifacts/daily/data-launchd.log`、`finmind-news-launchd.log` |
| 某個子 Agent 當時回了什麼、被餵了什麼 | 同一次執行資料夾的 `*_raw_<N>.json` 與 `brief_*.json` |
| 最終決策、委託與風控修正 | `artifacts/decisions/<日期>/decision-<UTC時間>Z/`（`decision.json`、`proposal.json`、`risk_review.json`、`revision_history.json`） |
| 某次決策用了哪份快照與帳戶 | 同一決策資料夾的 `decision_input.json` |
| 決策被取代後去哪了 | `artifacts/archive/superseded_decisions/<日期>/` |
| 要上傳的 D-Plan | `artifacts/competition_d_plan/D-Plan_<team_id>_<交易日>.json` |
| D-Plan 為什麼沒產生 | `pipeline.json` 的 `dplan` 欄位 |
| 帳戶現金、持股、績效 | `artifacts/ledger/<account_id>/runs/<最新>/state.json`，或 `./start.sh` 開儀表板 |
| 最新研究快照 | `artifacts/data/research_snapshot_latest.json` |
| 開休市日期表與注意、處置股 | `artifacts/data/trading_status/` |
| 行情、營收、重大訊息、財報、來源原文 | `var/etf_agent.db`（用 `./start.sh status` 看狀態） |
| 每週策略反思報告 | `var/strategy_reflection/reports/` |
| 來源核對與核准證據 | `docs/source_audit/`（進 git，不可刪） |
| 交易池與核准的產業分類 | `data/official_universe.csv`、`data/sector_classification.json`（進 git，不是執行產物） |

## 附錄：名稱對照

| 白話說明 | 程式名稱 |
| --- | --- |
| 不可變封存（內容雜湊鎖住） | `ImmutableRunStore` |
| 決策封存寫入者 | `DecisionRepository.save`（`src/etf_agent/decision/service.py`） |
| 報告交付寫入者 | `DeliveryRepository`（`src/etf_agent/decision_delivery/repository.py`） |
| 帳本寫入者 | `VirtualAccountRepository`（`src/etf_agent/virtual_account/service.py`） |
| D-Plan 匯出 | `DPlanExportService`（`src/etf_agent/competition/service.py`） |
| 決策被取代時搬到 `archive/` | `supersede_decision_runs`（`src/etf_agent/virtual_account/daily.py`） |
