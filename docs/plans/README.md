# 計畫與設計紀錄

這個資料夾放的是**計畫、設計提案與稽核紀錄**，不是現行行為說明。每份文件寫的是當時為什麼要做、方案怎麼比較、做到哪個階段，以及重放數字（一律是 `replay_reference_only`，不是前瞻驗證）。

- 現行行為以 [`../status.md`](../status.md) 與 [`../decision-contracts.md`](../decision-contracts.md) 為準；閘門是否開啟只看 [`../status.md`](../status.md#版本閘門啟用狀態) 與 `config/`。
- 這些文件開頭的「狀態」是撰寫當時的快照，可能已被後續版本取代；把計畫文件裡的行為當成現行行為是錯的。
- 檔案是從 `docs/` 搬進來的，內容未刪；連結與程式註解已改成 `docs/plans/` 路徑。

下表的狀態是依 [`../status.md`](../status.md) 與 [`AGENTS.md`](../../AGENTS.md) 的記載整理，數值與開啟時間請看 status.md。

## 配置與現金

| 文件 | 內容 | 狀態 |
| --- | --- | --- |
| [limit_aware_conviction_weights_plan.md](limit_aware_conviction_weights_plan.md) | 依單股上限放大的信心權重（`limit_scaled_conviction`，α 方案）與重放 | 已開啟 |
| [cash_buffer_high_conviction_plan.md](cash_buffer_high_conviction_plan.md) | 防守現金緩衝下的高信心買進（`high_conviction_cash_floor`） | 已實作、未開啟（與 `cash_ratio_range` 互斥） |
| [agent_cash_ratio_range_plan.md](agent_cash_ratio_range_plan.md) | 風險 Agent 在姿態範圍內選現金子等級（`cash_ratio_range`）；第二階段（Agent 填數字）未實作 | 第一階段已開啟 |
| [recency_weighting_plan.md](recency_weighting_plan.md) | 風險 Agent 現金姿態：越新的資料分量越重（`cash_stance_recency`） | 已開啟 |
| [turn_facts_plan.md](turn_facts_plan.md) | 現金姿態轉向事實（`cash_stance_turn_facts`），純紀錄 | 已開啟 |
| [market_index_facts_plan.md](market_index_facts_plan.md) | 大盤指數事實（`market_index_facts`），純紀錄 | 已開啟 |
| [sizing_rotation_v2_plan.md](sizing_rotation_v2_plan.md) | 舊連結入口，內容已併入 [配置與輪動](../sizing_rotation_v2_design.md)（現行做法說明） | 歷史 |

## 換股、曝險與進攻

| 文件 | 內容 | 狀態 |
| --- | --- | --- |
| [conviction_swap_plan.md](conviction_swap_plan.md) | 信心換股的原始資料與推導，已併入曝險換股計畫 | 歷史 |
| [exposure_rotation_plan.md](exposure_rotation_plan.md) | 市場轉向時的持股曝險換股（`exposure_rotation` v1） | 已開啟（現行版本見 status.md） |
| [exposure_rotation_v2_plan.md](exposure_rotation_v2_plan.md) | 曝險換股參數精簡（v2，末節 v3） | 已開啟 |
| [rebound_participation_plan.md](rebound_participation_plan.md) | 反彈參與閘門 v1 的設計與重放結果 | v1 保留供舊 run 重建，現行見 v2 |
| [rebound_participation_v2_plan.md](rebound_participation_v2_plan.md) | 反彈參與 v2：程式只整理事實、由風險 Agent 決定 | 已開啟 |
| [agent_autonomy_plan.md](agent_autonomy_plan.md) | 把策略裁量還給 Agent 的分階段計畫（再進場冷卻、回撤減碼順序、現金分配等） | 各階段已實作，開啟狀態見 status.md |
| [agent_nominated_rotation_plan.md](agent_nominated_rotation_plan.md) | 換股改由交易 Agent 提名（`agent_swap`） | v2 已開啟 |
| [agent_led_exposure_plan.md](agent_led_exposure_plan.md) | 市場轉向時的持股曝險由 Agent 判斷（`exposure_view`、`agent_swap.exposure`） | 已開啟 |
| [agent_led_sizing_plan.md](agent_led_sizing_plan.md) | 換股組數、份量與賣誰由 Agent 主導、人只設硬上限 | 設計計畫；實作部分見 status.md（`entry_reserve`、`quality_dip` v3／v4） |
| [agent_led_offense_plan.md](agent_led_offense_plan.md) | 連跌後逢低布局與轉向後進攻（`offense`、`entry_reserve`、`quality_dip` v4） | 已開啟 |
| [quality_dip_plan.md](quality_dip_plan.md) | 優質股被大盤拖累時的進場判斷（事實、估值、換股提名） | 事實與紀錄已開啟；`valuation_facts` 已實作未開啟；估值 V3 未實作 |
| [quality_dip_agent_sizing_plan.md](quality_dip_agent_sizing_plan.md) | 優質股換股的組數與份量由交易 Agent 提出、風險 Agent 裁決（v3） | 已開啟，之後升 v4 |
| [stop_loss_take_profit_plan.md](stop_loss_take_profit_plan.md) | 停利停損：警示給交易 Agent、程式兜底硬停損（`stop_exit`） | 警示（`position_review` v2）已開啟；`stop_exit` 已實作、未開啟 |

## 稽核與後續

| 文件 | 內容 | 狀態 |
| --- | --- | --- |
| [agent_vs_code_audit.md](agent_vs_code_audit.md) | 誰在做決定：Agent 與程式的分工盤點（2026-11-10） | 歷史稽核紀錄 |
| [follow_up_plan.md](follow_up_plan.md) | Agent 自主權線的後續優化與上線計畫（2026-11-10） | 歷史計畫，狀態見 status.md |
| [audit_fix_plan.md](audit_fix_plan.md) | 2026-11-11 設計審查發現 A～H 的修正計畫 | 各項狀態見 status.md |

## 資料與其他

| 文件 | 內容 | 狀態 |
| --- | --- | --- |
| [fundamental_data_gaps_plan.md](fundamental_data_gaps_plan.md) | 基本面資料缺口補強：去年同期、現金流備援、產業比較 | A1 已實作；C1 只收集與稽核；C2、B 未實作 |
| [event_brief_slimming_plan.md](event_brief_slimming_plan.md) | 事件分析師 brief 瘦身（草案） | 草案，狀態見 status.md |
