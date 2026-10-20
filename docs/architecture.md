# 目前每日決策架構

本文件說明已接線的每日決策流程。詳細欄位以 `src/etf_agent/` 的契約與 Validator 為準；Agent Skill 位於 `tradeagent/`。流程產出供人工檢視的封存決策與中文報告，不自動下單或送件。

```text
資料與 ResearchSnapshot → 技術／基本面／事件分析師
  → 同輸入且互相隔離的多頭／空頭研究員
  → 交易 Agent → 風險 Agent
  → Python 配置、情境、CompetitionGuard → Decision run 與報告
```

| 階段 | Agent 的判斷 | Python 的確定性工作 |
| --- | --- | --- |
| 資料 | 無投資判斷 | 保存來源、原始回應、`available_at`、版本與不可變 Snapshot；依 cutoff 排除未來資料 |
| 三位分析師 | 全池逐檔輸出 outlook、事件重大性及有證據的發現；全市場情緒只輸出一次 | 計算指標與財務比率、分批與覆蓋驗證；無核准來源時市場情緒輸出 `unavailable` |
| 多頭／空頭 | 同一共同輸入、互相隔離，逐檔提出有證據的 claims 與強度 | 核對 `shared_input_sha256`、引用歸屬、全池覆蓋及 `peer_packet_ids` 為空 |
| 交易 | 逐檔取捨每個多空 claim，給買賣意圖及 buy／add 信心等級 | 驗證每個 claim 剛好採納或否決一次；依 Policy、ATR 與風險現金姿態計算權重與張數 |
| 風險 | 配置前給現金姿態；配置後審查並提出受限修正 | 重算費稅、現金、情境與競賽限制；硬性規則失敗即拒絕，修正循環最多三次 |

決策與帳本用價是 Snapshot `latest_prices` 的 `execution_close_price`（同日官方原價，附 `execution_price_source` 與 `execution_source_evidence_id`）；`analysis_close_price` 是調整價，只供研究與技術指標。同日沒有官方價時 `execution_*` 為空，配置、風控與 D-Plan 退回 `analysis_close_price`，重放過去日期時該價含事後除權息調整，屬已知限制。舊封存沒有此欄位仍可重建驗證。

Decision run 另存 `team_inputs`。目前 2.1 版包含三份分析報告與現金姿態；舊 2.0 封存仍按其原始分析報告及事件研究輸入重建。Finalizer 與下游驗證缺少必要輸入、引用不存在、版本不合或數值被改寫時拒絕。1.0 Buy／Sell 舊決策鏈不再接受。

重大公司事件保留於事件分析師報告，直接交股票層級多頭／空頭研究；每日鏈不再額外執行 Fact／Bull／Bear／Adjudicator。獨立事件研究與舊封存驗證仍可使用，詳見[事件研究整合說明](integrated_event_analysis.md)。第三位分析師將全市場情緒與逐家公司事件分開，詳見[市場與公司事件範圍](event_market_scope.md)。

每日腳本可用 `--agent-cli claude|codex` 選擇子 Agent 執行器；兩者輸出都進入同一套 Validator。Codex 執行器已通過模擬 CLI 測試，尚未完成真實資料端到端驗收。沒有合法、歷史化的全市場情緒或分析師共識 Provider 時，正式結果維持 `unavailable`，不得由公司新聞或交易背景補值。
