# 計畫與設計紀錄

這個資料夾放的是**計畫、設計提案與稽核紀錄**，不是現行行為說明。每份文件寫的是當時為什麼要做、方案怎麼比較、做到哪個階段，以及重放數字（一律是 `replay_reference_only`，不是前瞻驗證）。

- 現行行為以 [`../status.md`](../status.md) 與 [`../decision-contracts.md`](../decision-contracts.md) 為準；閘門是否開啟只看 [`../status.md`](../status.md#版本閘門啟用狀態) 與 `config/`。
- 這些文件開頭的「狀態」是撰寫當時的快照，可能已被後續版本取代；把計畫文件裡的行為當成現行行為是錯的。
- 已全部做完且併入現行說明的計畫（信心權重、現金姿態轉向與時效、大盤指數事實、曝險換股 v1／v2／v3、反彈參與 v1／v2、換股提名、換出者規則、Agent 主導曝險與進攻、優質股組數與份量、自主權計畫、兩份稽核與修正計畫等）已刪除，需要時看 git 歷史；現行做法看 [`../decision-contracts.md`](../decision-contracts.md)、[`../decision-details.md`](../decision-details.md)、[`../glossary.md`](../glossary.md)。

下表的狀態是依 [`../status.md`](../status.md) 與 [`AGENTS.md`](../../AGENTS.md) 的記載整理，數值與開啟時間請看 status.md。

## 配置與現金

| 文件 | 內容 | 狀態 |
| --- | --- | --- |
| [cash_buffer_high_conviction_plan.md](cash_buffer_high_conviction_plan.md) | 防守現金緩衝下的高信心買進（`high_conviction_cash_floor`） | 已實作、未開啟（與 `cash_ratio_range` 互斥） |
| [agent_cash_ratio_range_plan.md](agent_cash_ratio_range_plan.md) | 風險 Agent 在姿態範圍內選現金子等級（`cash_ratio_range`）；第二階段（Agent 填數字）未實作 | 第一階段已開啟 |

## 換股、曝險與進攻

| 文件 | 內容 | 狀態 |
| --- | --- | --- |
| [agent_led_sizing_plan.md](agent_led_sizing_plan.md) | 換股組數、份量與賣誰由 Agent 主導、人只設硬上限 | 設計計畫；實作部分見 status.md（`entry_reserve`、`quality_dip` v3／v4） |
| [quality_dip_plan.md](quality_dip_plan.md) | 優質股被大盤拖累時的進場判斷（事實、估值、換股提名） | 事實與紀錄已開啟；`valuation_facts` 已實作未開啟；估值 V3 未實作 |
| [stop_loss_take_profit_plan.md](stop_loss_take_profit_plan.md) | 停利停損：警示給交易 Agent、程式兜底硬停損（`stop_exit`） | 警示（`position_review` v2）已開啟；`stop_exit` 已實作、未開啟 |

## 資料與其他

| 文件 | 內容 | 狀態 |
| --- | --- | --- |
| [fundamental_data_gaps_plan.md](fundamental_data_gaps_plan.md) | 基本面資料缺口補強：去年同期、現金流備援、產業比較 | A1 已實作；C1 只收集與稽核；C2、B 未實作 |
| [event_brief_slimming_plan.md](event_brief_slimming_plan.md) | 事件分析師 brief 瘦身（草案） | 草案，狀態見 status.md |
| [context_slimming_plan.md](context_slimming_plan.md) | 各階段 Agent 輸入瘦身（2026-11-13 草案） | 草案，未實作 |
| [postmortem_search_plan.md](postmortem_search_plan.md) | 復盤用字詞檢索（SQLite FTS5，旁觀者、不進每日鏈、不用嵌入） | 草案，未實作（建議比賽結束後） |
| [day_trading_chips_facts_plan.md](day_trading_chips_facts_plan.md) | 當沖籌碼背景事實（2026-11-15） | 只有計劃，未實作 |
| [market_regime_front_plan.md](market_regime_front_plan.md) | 大盤姿態前置並影響選股（`market_posture`） | 設計草案，未實作 |
| [market_regime_research_report.md](market_regime_research_report.md) | 大盤多空判斷的證據與決策報告 | 報告，未改程式 |
| [risk_bucket_cap_plan.md](risk_bucket_cap_plan.md) | 高風險股上限（`high_risk_cap`）執行版 | 已實作，2026-11-22 以 `enforce`、`scope defensive` 開啟 |
| [research_improvement_and_paper_plan.md](research_improvement_and_paper_plan.md) | 文獻對照、專案改進與論文計畫 | 計畫，未實作 |
| [token_cost_reduction_plan.md](token_cost_reduction_plan.md) | 每日決策 token／美元成本降低 | 草案，部分項目（事件跨日沿用）已實作 |
| [token_efficiency_plan.md](token_efficiency_plan.md) | token 花費地圖、壓縮與向量索引（RAG）評估：每日鏈不用 RAG | 草案 |
| [event_analyst_token_reduction_plan.md](event_analyst_token_reduction_plan.md) | 事件分析師 token 降量（沿用） | 草案，跨日逐檔沿用已實作 |
