# 目前每日決策架構

本文件說明已接線的每日決策流程。

- 欄位細節以 `src/etf_agent/` 的契約與 Validator 為準。
- Agent Skill 位於 `tradeagent/`。
- 流程產出供人工檢視的封存決策與中文報告，**不自動下單或送件**。

## 流程總覽

```text
資料與 ResearchSnapshot → 技術／基本面／事件分析師
  → 同輸入且互相隔離的多頭／空頭研究員
  → 交易 Agent → 風險 Agent
  → Python 配置、情境、CompetitionGuard → Decision run 與報告
```

原則：**確定性歸程式，不確定性歸 LLM。**

| 階段 | Agent 的判斷 | Python 的確定性工作 |
| --- | --- | --- |
| 資料 | 無投資判斷 | 保存來源、原始回應、`available_at`、版本與不可變 Snapshot；依 cutoff 排除未來資料 |
| 三位分析師 | 全池逐檔輸出 outlook、事件重大性與有證據的發現；全市場情緒只輸出一次 | 計算指標與財務比率、分批與覆蓋驗證；市場情緒由程式依全市場新聞或恐懼貪婪指標重建，兩者都不可用時輸出 `unavailable` |
| 多頭／空頭 | 同一共同輸入、互相隔離，逐檔提出有證據的 claims 與強度 | 核對 `shared_input_sha256`、引用歸屬、全池覆蓋、`peer_packet_ids` 為空 |
| 交易 | 逐檔取捨每個多空 claim，給買賣意圖與 buy／add 信心等級 | 驗證每個 claim 剛好被採納或否決一次；依 Policy、ATR 與風險現金姿態計算權重與張數 |
| 風險 | 配置前給現金姿態；配置後審查並提出受限修正 | 重算費稅、現金、情境與競賽限制；硬性規則失敗即拒絕，修正循環最多 3 次 |

## 持股檔數上限的取捨順序

買進候選超過 `max_positions` 時，程式依下列順序決定誰先入選，其餘標為 `MAX_POSITIONS`：

1. 信心等級：`high`、`medium`、`low`。
2. 同等級時，交易決策採納的多頭 claim 數減空頭 claim 數，越大越先（`support_by_symbol`，綁在 Policy 並由 Validator 重算）。
3. 仍相同時，依股票代號。

舊 Decision run 沒有 `support_by_symbol`，仍只依等級與代號。

## 主辦方資料包對帳

- 每日 `run` 在建立帳戶快照與交易狀態之後、呼叫任何子 Agent 之前，讀取本隊、本目標交易日的主辦方 DataPack，與帳本快照比對持股股數（必須完全相同）與現金（容忍比率可調）。
- DataPack 是不受信任的外部檔：欄位嚴格白名單，`null` 與空陣列不互相代替；結算未產生、身分或日期不符、對不上時一律停止。
- 結果寫入 `pipeline.json` 的 `datapack`；D-Plan 只接受 `matched` 或明確 `skipped`，並檢查每檔賣出不超過 DataPack 的 `shares`（應收配股不可賣）。
- 不一致時預設停止；明確加 `--rebase-to-datapack` 才以 DataPack 重建帳本（新增 `datapack_rebase` 狀態，舊狀態保留），並強制建立新 Snapshot，使資料包產生時間早於決策截止時間，避免回填未來資訊。
- 實作：`src/etf_agent/competition/datapack.py`；比賽第一天開新帳戶與重建帳本：`src/etf_agent/competition/account_opening.py`。

## 帳本結算

帳本若有前次已封存的決策，必須先用該決策指定成交日的 TWSE／TPEx 官方價量結算，才能建立新的「決策前帳戶快照」。

歷史上櫃價量有缺口時，可用 `scripts/repair_pending_tpex_close.py` 從 TPEx 指定日期的原頁補抓：

- 抓取時間不回填成歷史可得時間。
- 原文與來源雜湊都保留。
- 仍須通過原帳本的結算驗證。

## 價格口徑

| 欄位 | 用途 |
| --- | --- |
| `execution_close_price` | 決策與帳本用價。同日官方原價，附 `execution_price_source` 與 `execution_source_evidence_id` |
| `analysis_close_price` | 調整價，只供研究與技術指標 |

- 同日沒有官方價時，`execution_*` 為空。配置、風控與 D-Plan 退回 `analysis_close_price`。
- 重放過去日期時，退回的價格含事後除權息調整，這是已知限制。
- 舊封存沒有此欄位，仍可重建驗證。

## 平行執行與耗時

`--max-workers N`（1～8，CLI 預設 6；程式內 `DailyDecisionPipeline` 預設 1 即串行）讓下列階段同時執行子 Agent：

- 技術／基本面／事件三位分析師的所有批次。
- 多頭與空頭研究員的所有批次。兩方仍只讀同一份共同輸入，任務之間不共享輸出。
- 交易 Agent 的所有批次。各批只讀共同的帳戶、多空與動能輸入，不讀其他批的輸出。

現金姿態與風險審查維持串行。平行只改變執行方式，不改 brief、Validator、Policy 與 Guard：

- 結果依提交順序合併，`agent_calls` 也依提交順序整理，不受完成先後影響。
- 任一任務失敗就取消尚未開始的任務並整體失敗（fail-closed），不使用部分結果。
- `--resume` 沿用以任務名稱區分的 `*_raw_N.json`，平行與串行的續跑互通。
- `pipeline.json` 的 `stage_durations` 記錄 `analysts`、`research`、`trader`、`cash_stance`、`allocation_review` 的牆鐘秒數。
- 同時呼叫越多，越容易撞到訂閱用量或速率上限。
- 實測（2026-10-22，同一模擬帳戶與 Snapshot，各一次）：

  | 階段 | `--max-workers 3`（交易串行） | `--max-workers 6`（交易平行） |
  | --- | --- | --- |
  | 分析師 | 368.0 秒 | 248.8 秒 |
  | 多空研究 | 253.5 秒 | 176.2 秒 |
  | 交易 | 267.3 秒 | 120.8 秒 |
  | 現金姿態 | 15.8 秒 | 28.8 秒 |
  | 配置與審查 | 24.1 秒 | 26.4 秒 |
  | 合計 | 928.7 秒 | 601.0 秒 |
  | 呼叫數／重試 | 27／0 | 28／1（`event_b9` 驗證重試） |

  - 兩次都 `approved`、30 筆委託，沒有撞到用量上限。`agent_step` 只在輸出驗證失敗時重試；用量或執行錯誤會直接失敗，所以那次重試是輸出驗證問題，不是限流。
  - 每種設定只跑一次，LLM 本身有變異，不能當嚴格的加速比；8 尚未實測。
  - 實測當時的程式尚未包含 #69 的事件調整（當時事件分析師 10 批、分析師共 16 次呼叫）；之後無重大訊息的股票改由程式輸出，事件批次會減少，數字需重新量測。

## 降低 token 的設定

詳見[降 token 計劃](token_reduction_plan.md)。

### 技術面閘門

預設開啟 `--technical-gate`。`--no-technical-gate` 可改回「全部附完整 `trend_history`」。

- **趨勢一致**的股票，只附 `momentum` 與證據 ID。趨勢一致的定義：
  - 收盤同在 MA20／MA60 之上，且 5／20／60 日報酬皆為正；或
  - 收盤同在 MA20／MA60 之下，且 5／20／60 日報酬皆為負。
- 其他股票與資料不足者，仍附完整歷史。
- 分析師仍對全部股票給 outlook，Validator 與引用檢查不變。
- 10/20 同 Snapshot 的有／無閘門對照，樣本只有一天。

### 分析師模型

`--analyst-model` 在 Claude CLI 預設為 `sonnet`（`inherit` 沿用 `--model`）。

- 技術／基本面／事件批次使用它。
- 多空、交易與現金姿態維持 `--model`。
- `agent_calls` 會記錄實際使用的模型。

### 其他

- 固定錨點摘要與事件首尾摘錄，只在另存的研究試驗中使用，**沒有**取代每日決策輸入。
- 交易 brief 已把重複的完整帳戶持倉，改成帳戶現金／NAV、持股代號與檔數。本批逐股的 `held_shares` 仍保留。
- 完整 AccountSnapshot 與分析師原始報告，繼續供 Validator 重建。
- 試驗差異與後續門檻，見[降 token 計劃](token_reduction_plan.md)。

## 配置與風控

配置程式產生提案時，就依 Policy 的單股上限（含特殊上限）檢查既有持股與候選：

- 依整張數降低預計超重的部位。
- 用同一個情境引擎，檢查部分成交後的曝險。
- `constraint_flags.RISK_STOCK_WEIGHT` 記錄程式必要的硬規則減碼。交易 Agent 原本的 `hold` 等意圖不會被改寫。
- 風險 Agent 不手寫股數或權重。
- 交易受限、整張／現金／換手等限制無法同時滿足時，CompetitionGuard 仍然拒絕。

## Decision run 的 `team_inputs`

| 版本 | 內容 |
| --- | --- |
| 2.1（目前） | 三份分析報告與現金姿態 |
| 2.0（舊封存） | 依原始分析報告與事件研究輸入重建 |

Finalizer 與下游驗證遇到以下情況一律拒絕：

- 缺少必要輸入
- 引用不存在
- 版本不合
- 數值被改寫

1.0 的 Buy／Sell 舊決策鏈不再接受。

## 校準旁觀者

`src/etf_agent/calibration/` 只讀已封存的 Decision run 與官方價量，記錄各股的信心等級與多空強度，價格到齊後以官方成交均價算前瞻報酬，並審計決策用的前日收盤與當日成交均價的差距。**不在決策鏈內**：決策、自動化與風控程式都不匯入它，也不回寫任何決策 artifact。完整口徑與限制見[校準旁觀者](../src/etf_agent/calibration/README.md)。

## 事件如何處理

- 重大公司事件保留在事件分析師報告，直接交給股票層級的多頭／空頭研究。
- 每日鏈不再額外執行 Fact／Bull／Bear／Adjudicator。
- 獨立事件研究與舊封存驗證仍可使用，見[事件研究整合說明](integrated_event_analysis.md)。
- 第三位分析師把全市場情緒與逐家公司事件分開，見[市場與公司事件範圍](event_market_scope.md)。

## 子 Agent 執行器

用 `--agent-cli claude|codex|opencode` 選擇。三者的輸出都進入同一套 Validator，續跑必須沿用原執行器。

| 執行器 | 驗證狀態 |
| --- | --- |
| Codex | 已完成真實 150 檔每日鏈 |
| OpenCode | 已通過本機唯讀、JSONL、預檢煙霧測試。**尚未**執行真實 150 檔端到端驗收 |
| Claude | 每日鏈使用中 |

OpenCode 的限制：

- 使用 `run --pure --format json` 與單次隔離的唯讀 Agent。
- 只准讀檔（`.env` 與外部目錄禁止），不准執行命令或修改檔案。
- OpenCode CLI 沒有本流程可直接用的 `--output-schema`。提示會帶 JSON Schema，但仍要靠確定性 Validator 與有限重試，拒絕不合格的結果。

全市場情緒優先使用全市場新聞（`automation/market_news_stage.py`：截止前 RSS 抓取、子 Agent 逐則標註、程式重建，失敗時降級）；新聞不可用時改用[台股恐懼貪婪指標](market_fear_greed.md)（程式計算，cutoff 前抓取）；兩者都不可用時維持 `unavailable`。沒有合法、歷史化的分析師共識 Provider 時，共識維持 `unavailable`。不得用公司新聞或交易背景補值。
