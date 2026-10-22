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
| 三位分析師 | 全池逐檔輸出 outlook、事件重大性與有證據的發現；全市場情緒只輸出一次 | 計算指標與財務比率、分批與覆蓋驗證；無核准來源時，市場情緒輸出 `unavailable` |
| 多頭／空頭 | 同一共同輸入、互相隔離，逐檔提出有證據的 claims 與強度 | 核對 `shared_input_sha256`、引用歸屬、全池覆蓋、`peer_packet_ids` 為空 |
| 交易 | 逐檔取捨每個多空 claim，給買賣意圖與 buy／add 信心等級 | 驗證每個 claim 剛好被採納或否決一次；依 Policy、ATR 與風險現金姿態計算權重與張數 |
| 風險 | 配置前給現金姿態；配置後審查並提出受限修正 | 重算費稅、現金、情境與競賽限制；硬性規則失敗即拒絕，修正循環最多 3 次 |

## 持股檔數上限的取捨順序

買進候選超過 `max_positions` 時，程式依下列順序決定誰先入選，其餘標為 `MAX_POSITIONS`：

1. 信心等級：`high`、`medium`、`low`。
2. 同等級時，交易決策採納的多頭 claim 數減空頭 claim 數，越大越先（`support_by_symbol`，綁在 Policy 並由 Validator 重算）。
3. 仍相同時，依股票代號。

舊 Decision run 沒有 `support_by_symbol`，仍只依等級與代號。

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

沒有合法、歷史化的全市場情緒或分析師共識 Provider 時，正式結果維持 `unavailable`。不得用公司新聞或交易背景補值。
