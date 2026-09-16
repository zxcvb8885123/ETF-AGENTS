# 第一版 Agent 技術架構

狀態（2026-09-17）：架構設計完成，Data Agent 基礎資料層已實作，目前進行 M0「來源健康與交易池閘門」。依據 [四層開發架構](agent_plan.md) 與 [Data Agent 計畫](data_agent_plan.md)。

## 1. 整體設計

採用一個 Python 應用程式，由每日流程控制器依序呼叫各層。第一版使用 SQLite 與檔案保存資料快照及執行結果，無須先部署多個服務。需要資料補查或事件理解時，由 Codex／Claude 工作階段載入共用 Skill 並呼叫確定性 Python 工具；第一版不在專案內串接模型 API。

| 模組 | 輸入 | 職責 | 輸出 |
| --- | --- | --- | --- |
| 資料蒐集 | 官方名單、行情、基本面、新聞、主辦持倉 | 收集、清理、檢查時間與品質、計算特徵 | ResearchSnapshot |
| 策略決策 | ResearchSnapshot、目前持倉、策略設定 | 量化排序、事件分析、提出配置理由 | StrategyProposal |
| 組合／資金風控 | StrategyProposal、帳戶快照、競賽規則 | 配置、交易數量計算、成交模擬、限制檢查 | ApprovedDecision 或 RejectedDecision |

報告輸出與稽核是共用支援功能。只從通過檢查的同一份決策產生報告與交易書，避免兩份內容不一致。

## 2. 資料蒐集模組

### 元件

- `providers`：分別取得行情、基本面、新聞、官方交易池與 ETF 基準；統一資料格式，保留原始回應。
- `account_loader`：讀取主辦方最新持倉、現金、NAV、公司行動調整與結算日期。初始本金依競賽辦法設定為新臺幣 10 億元。
- `quality`：驗證股票代碼、重複值、缺值、有限數值、資料日期與來源完整性。
- `features`：用截止時間以前的資料計算趨勢、波動、成交量與成長特徵。
- `snapshot_store`：保存固定版本的研究與帳戶快照，供策略、回測與稽核共同使用。

每筆資料保留 `published_at`（發布時間）、`fetched_at`（取得時間）與 `source_id`。歷史模擬依當時可取得版本取資料，不能用後來修訂的財報或未來新聞。

交易池、帳戶、必要價格或 ETF 基準缺失時停止該次流程；個別新聞缺失則標記缺失，不自動解讀為利多或利空。目前 `data/official_universe.csv` 已有 150 檔，但實測發現 `5371` 在 TPEx 來源缺漏、`3718` 已出現，因此仍須每日驗證代號與可交易狀態；主辦規則確認前不得自動替換。

## 3. 策略決策模組

### 元件

- `scoring`：計算量化分數與排名；每項分數保留原始指標及計算設定。
- `event_analysis`：LLM 只根據研究快照分析新聞，輸出事件、影響、證據 ID 與不確定性。
- `proposal`：綜合排名、事件與現有持倉，提出候選、建議權重、增減持理由及退出條件。

第一個可跑版本先採量化排序作為基準，再接入 LLM，比較對決策與績效的影響。評分權重與換股門檻屬於待驗證的策略參數，不是官方限制。

LLM 不負責金額加總或整張數量計算。其輸出必須符合結構化格式，引用的證據必須存在。格式錯誤最多重試設定次數；仍失敗則標记該次執行失敗，或採用事先設定並記錄的量化備援策略。

## 4. 組合／資金風控模組

### 元件與順序

1. `allocator`：依候選與設定產生 20–30 檔目標部位，檢查個股及可選的產業集中限制。
2. `order_builder`：以目標部位減目前部位得到淨交易量。同一股票只產生單一方向的交易；禁止超賣與放空。內部以股數表示、交易要求整張倍數，最終欄位依官方格式確認。
3. `simulator`：用預估成交價計算現金及費稅，用預估收盤價計算部位市值與 NAV，兩種價格不可混用。
4. `scenario_check`：模擬不同成交與收盤價格，包括買進成本上升、持股下跌、個股權重上升等情境。
5. `validator`：檢查交易池、檔數、現金、個股權重、交易限制與 Active Share。
6. `repair`：檢查失敗時調整數量或替換候選，最多執行設定次數；耗盡後回傳拒絕結果，禁止無限重算。

送件前無法知道當日成交均價與收盤價，情境模擬只能提供安全餘裕，不能保證結算時一定合規。結算後必須依主辦方實際結果再次核對。

### 規則處理

- 現金不得為負，採嚴格低於 NAV 25% 的檢查；22% 是原規劃中的自訂緩衝值。
- 台積電上限 25%，其他個股上限 10%；價格波動超限與加碼超限分開處理，保存首次超限日期，以交易日追蹤五日調整期限。
- Active Share 要逐一比對官方指定各 ETF 的前十大。前十大權重是否重新正規化、基準更新日期與同名次處理需依官方說明確認，未確認不可宣稱完整合規。
- 保存每個 ETF 的 Active Share 與連續違規日數，不可只比對一檔 ETF。
- 手續費與賣出稅率取自競賽設定；金額使用十進位計算，費稅取整方式待官方格式確認。
- 除息金額依辦法於期末計入，不能提前當成可用現金；公司行動以主辦帳戶結果核對。

## 5. 模組間資料契約

所有資料都有 `schema_version`、`run_id`、`trade_date` 與 `created_at`；時間使用含時區格式，交易日以 Asia/Taipei 判定。

| 物件 | 主要欄位 |
| --- | --- |
| RunContext | decision_cutoff、config_version、code_version、mode |
| AccountSnapshot | settlement_date、positions、cash、nav、source_id |
| ResearchSnapshot | account_snapshot_id、universe_version、benchmark_version、features、events、sources、quality_flags |
| StrategyProposal | snapshot_id、candidates、scores、suggested_weights、reasons、evidence_ids、reduce_triggers |
| OrderPlan | proposal_id、orders、estimated_fees、estimated_cash、projected_positions |
| RiskResult | passed、violations、warnings、scenario_results、active_share_by_etf |
| ApprovedDecision | order_plan_id、risk_result_id、final_reasons、content_hash |

只有控制器能在 RiskResult 通過後建立 ApprovedDecision。RejectedDecision 保留原因與修正歷程，但不能進入正式交易書輸出。

## 6. 每日流程與故障處理

1. 取得前一交易日主辦結算，核對持倉、現金、公司行動與當地帳本。
2. 在提交窗口內啟動執行，固定資料截止時間與 run_id。
3. 建立研究快照，通過資料品質檢查後執行策略。
4. 計算配置、交易與價格情境，執行風控及有次數上限的修正。
5. 通過後從同一份 ApprovedDecision 產出報告和交易書，保存內容雜湊。
6. 若後續接入送件器，送出後保存平台回執，以平台接受結果認定成功，不以本地檔案生成認定成功。
7. 收到當日主辦結算後更新帳戶、NAV 歷史、MDD 與違規追蹤，再開始下一交易日。

提交窗口為交易日前一日 19:30 至當日 08:55，以主辦系統為準。控制器使用官方交易日曆，設定提早截止的緩衝時間，追蹤每日最多 25 次提交及成功提交天數。

網路請求採有上限重試。送件逾時先查回執，不能直接重複送出。策略或資料失敗時告警；零交易也必須檢查現有組合並產生每日報告，不能假定不交易就一定合規。

## 7. 預定程式配置

以下為目前及預定的程式配置。

| 路徑 | 責任 |
| --- | --- |
| src/etf_agent/pipeline.py | 每日流程控制、狀態與重試 |
| src/etf_agent/contracts.py | 模組資料契約與驗證 |
| src/etf_agent/data/ | 已有：TWSE 最新行情、交易池讀取、SQLite 與收集流程；待補 quality、features、account_loader 及其他來源 |
| src/etf_agent/strategy/ | scoring、event_analysis、proposal |
| src/etf_agent/portfolio/ | allocator、order_builder、simulator、repair |
| src/etf_agent/risk/ | 情境檢查、Active Share、超限日數、MDD |
| src/etf_agent/reporting/ | 報告與交易書產製、同源一致性檢查 |
| src/etf_agent/data/database.py | 已有：行情、原始回應與執行紀錄；待擴充其他模組資料表 |
| src/etf_agent/models.py | 既有：部位與投資組合資料模型 |
| src/etf_agent/guard.py | 既有：初步風控檢查，後續擴充或轉接 risk |
| scripts/collect_twse.py | 已有：TWSE 最新交易日行情收集入口 |
| scripts/init_db.py、scripts/data_status.py | 已有：初始化資料庫與查看資料狀態 |
| scripts/run_daily.py | 預定完整每日執行入口 |
| config/strategy.json | 預定策略參數 |
| config/competition_rules.json | 既有競賽設定 |
| artifacts/{trade_date}/{run_id}/ | 預定保存快照、提案、風控、報告與回執 |

## 8. 現況與實作里程碑

目前已有基本資料模型、部分規則檢查、SQLite schema、150 檔交易池匯入、TWSE 最新行情、TWSE／TPEx 歷史行情、月營收、重大訊息、原始回應、版本紀錄與不可變 Snapshot。現有 guard 仍只接受單一 benchmark，尚未做前十大裁切、全部 ETF 比對、完整帳戶輸入、成交模擬或完整 Agent 稽核持久化；資料抓取成功不代表即可正式送件。

里程碑以 [Data Agent 計畫](data_agent_plan.md) 的 M0～M4 為資料主線：先完成 M0 來源健康與交易池閘門，再依序接入 M1 官方資料、M2 新聞候選與 M3 Agent 工具循環。事件研究通過後，才進入量化策略、完整風控、時間一致回測及送件格式。不得因已有事件策略原型，就跳過資料閘門直接產生正式交易決策。

驗收包含：資料缺漏會停止、未來資料不能進入決策、整張與成本計算正確、違規不會輸出正式交易書、同一快照與保存的模型輸出可以重播、報告與交易清單一致。歷史 NAV 用收盤價估值、成交用當日均價，包含交易成本並與主辦結算核對。
