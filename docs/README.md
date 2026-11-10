# 文件索引

這裡是 `docs/` 的入口。先看「我想知道什麼」，再點進對應文件。這些文件都是現況說明：寫的是系統現在怎麼做、哪些還沒接，不是待辦計畫。

## 我想知道什麼

| 我想知道 | 看這份 |
| --- | --- |
| 現在哪些功能已經在跑、哪些還沒接 | [status.md](status.md) |
| 每天系統自己做什麼、我要做什麼、出事怎麼辦 | [operations.md](operations.md) |
| 每日決策怎麼跑（分析團隊、多空、交易、風險） | [architecture.md](architecture.md) |
| 決策原則、權重與產業上限怎麼處理 | [decision-details.md](decision-details.md) |
| 某條規則的完整條件、哪些閘門已開啟 | [decision-contracts.md](decision-contracts.md)、[status.md](status.md#版本閘門啟用狀態) |
| 某個閘門、欄位或旗標的程式名稱是什麼、在哪 | [glossary.md](glossary.md) |
| `./start.sh run` 有哪些參數 | [run-options.md](run-options.md) |
| 怎麼安裝、用 Docker 跑 | [docker.md](docker.md) |
| 單步命令怎麼用 | [../cli/README.md](../cli/README.md) |
| 專案有哪些資料夾、各放什麼 | [repository-layout.md](repository-layout.md) |
| `src/etf_agent/` 各套件做什麼、誰依賴誰 | [modules.md](modules.md) |
| `artifacts/`、`var/` 裡的東西是什麼、能不能刪 | [runtime-artifacts.md](runtime-artifacts.md) |
| 資料從哪來、授權狀態 | [data_sources_inventory.md](data_sources_inventory.md) |

## 全部文件分類

### 總覽與架構

- [repository-layout.md](repository-layout.md)：頂層資料夾分工。
- [modules.md](modules.md)：核心程式套件與依賴。
- [architecture.md](architecture.md)：每日決策架構。
- [decision-details.md](decision-details.md)：決策原則與參賽策略細節。
- [glossary.md](glossary.md)：決策閘門、欄位與旗標的名稱對照（是什麼、在哪；不寫啟用狀態）。
- [decision-contracts.md](decision-contracts.md)：每日決策鏈各角色的契約、版本閘門的完整條件、子 Agent 執行器與策略反思的邊界細節（2026-11-08 自 `AGENTS.md` 搬出）；啟用狀態與目前數值見 [status.md](status.md#版本閘門啟用狀態)。
- [sizing_rotation_v2_design.md](sizing_rotation_v2_design.md)、[sizing_rotation_v2_plan.md](sizing_rotation_v2_plan.md)：配置、權重與滿檔換股（後者已併入前者）。
- [cash_buffer_high_conviction_plan.md](cash_buffer_high_conviction_plan.md)、[limit_aware_conviction_weights_plan.md](limit_aware_conviction_weights_plan.md)、[agent_cash_ratio_range_plan.md](agent_cash_ratio_range_plan.md)：2026-11-05 的三個配置閘門設計計畫（高信心現金下限、高信心份額下限、姿態內現金子等級；內含重放數字，`replay_reference_only`），目前啟用狀態以 `config/decision_policy.json` 與 [status.md](status.md) 為準。
- [quality_dip_plan.md](quality_dip_plan.md)：優質股被大盤拖累時的進場判斷；第一階段事實（`quality_dip_facts`）、第二階段（`quality_dip_assessment`、`event_direction`）與估值 V1（收集與稽核，已掛進資料排程）已實作、三個閘門 2026-11-10 已開啟（純紀錄）；第三階段（改走換股提名）、估值 V2／V3 尚未實作。
- [agent_led_exposure_plan.md](agent_led_exposure_plan.md)：市場轉向時的持股曝險改由 Agent 判斷；階段 1（`exposure_view`，事實與紀錄）已實作並開啟（2026-11-10，使用者決定；純紀錄、不影響配置）；階段 2、3（`agent_swap.exposure`，提名 `purpose` 與依 `exposure_view` 取用曝險配對）已實作、尚未開啟。
- [agent_autonomy_plan.md](agent_autonomy_plan.md)：把策略裁量還給 Agent 的分階段計畫；階段 0（`intent-landing` 統計）已實作，已開啟（2026-11-10）：`agent_swap` v1 影子、`reentry_cooldown` v2、`drawdown_derisk` v5、現金三個 v2；已實作、尚未開啟：`agent_swap` v2、`reentry_cooldown` v3。
- [agent_nominated_rotation_plan.md](agent_nominated_rotation_plan.md)：換股由交易 Agent 提名（階段 3）的詳細設計；v1 影子已開啟、v2 配置與 `shadow-swap` 報告已實作、尚未開啟。
- [follow_up_plan.md](follow_up_plan.md)：Agent 自主權線的後續優化與上線計畫（2026-11-10）；目前狀態、依證據排序的項目、「現在可以開」清單（撰寫時為沒有）與 12 個工作包。
- [runtime-artifacts.md](runtime-artifacts.md)：執行時產物資料夾。

### 操作

- [operations.md](operations.md)：每日排程、D-Plan、資料包、開新帳戶、策略反思放行、評測、常見問題。
- [run-options.md](run-options.md)：`./start.sh run` 參數。
- [token_reduction_plan.md](token_reduction_plan.md)：Token 用量控制的現況。
- [status.md](status.md)：目前狀態與限制。
- [docker.md](docker.md)：環境安裝與 Docker。
- [../cli/README.md](../cli/README.md)：命令列單步操作。
- [../scripts/README.md](../scripts/README.md)：維運與補抓腳本。

### 資料與來源

- [data_sources_inventory.md](data_sources_inventory.md)：資料來源與授權限制。
- [market_news_integration.md](market_news_integration.md)、[market_news_history.md](market_news_history.md)：全市場新聞的接入與缺日補抓。
- [market_fear_greed.md](market_fear_greed.md)：台股恐懼貪婪指標。
- [margin_level_signal_plan.md](margin_level_signal_plan.md)：融資水位訊號。
- [overnight_market.md](overnight_market.md)：隔夜市場背景。
- [technical_trend_history.md](technical_trend_history.md)：技術分析的歷史輸入。
- [fundamental_data_repair.md](fundamental_data_repair.md)：基本面缺口補抓。
- [fundamental_growth_signal_plan.md](fundamental_growth_signal_plan.md)：基本面成長訊號。
- `source_audit/`：來源核對與核准證據，程式與設定會引用，不可刪。

### 事件與情緒

- [event_market_scope.md](event_market_scope.md)：公司事件與整體市場情緒的邊界。
- [integrated_event_analysis.md](integrated_event_analysis.md)：事件分析如何接進每日決策。

### 研究（不在決策鏈）

- [cross_market_study.md](cross_market_study.md)：跨市場隔夜訊號的離線回測。

## 各資料夾的 README

| 資料夾 | 說明 |
| --- | --- |
| [config/](../config/README.md) | 設定檔 |
| [data/](../data/README.md) | 靜態輸入 |
| [game_docs/](../game_docs/README.md) | 主辦方原始文件 |
| [scripts/](../scripts/README.md) | 維運腳本 |
| [tradeagent/](../tradeagent/README.md) | 子 Agent 的工作說明書 |
| [tests/](../tests/README.md) | 測試 |
| `src/etf_agent/<套件>/README.md` | 見 [modules.md](modules.md) 的連結表 |

## 文件更新紀錄

| 日期 | 內容 |
| --- | --- |
| 2026-10-22 | 資料套件新增臨時休市日推斷（`inferred_closures.py`），說明見 `src/etf_agent/data/README.md`。 |
| 2026-10-22 | 新增本索引、`repository-layout.md`、`modules.md`、`runtime-artifacts.md`；各資料夾與 `src/etf_agent/` 各套件新增 README；根 `README.md` 加上整體架構圖。 |
| 2026-10-31 | 根 `README.md` 精簡為入口頁；目前狀態、日常操作、`run` 參數、決策流程細節分別移到 `status.md`、`operations.md`、`run-options.md`、`decision-details.md`。 |
| 2026-11-04 | 文件改寫為白話說明、計畫文件改為現況說明。 |
| 2026-11-05 | 新增三份配置閘門設計計畫（`cash_buffer_high_conviction_plan.md`、`limit_aware_conviction_weights_plan.md`、`agent_cash_ratio_range_plan.md`），並同步閘門啟用狀態。 |
| 2026-11-08 | 修正閘門啟用狀態、狀態頁「尚未實跑」、樣本門檻 5→10 與策略反思排程說法等過期敘述。 |
| 2026-11-08 | `architecture.md` 與 `decision-details.md` 的兩份「附錄：名稱對照」合併為 `glossary.md`，兩處附錄改為連結。 |
| 2026-11-08 | `AGENTS.md` 瘦身為原則與邊界；契約與閘門細節搬到新檔 `decision-contracts.md`，閘門啟用狀態表集中到 `status.md`。 |
| 2026-11-10 | 索引加入 `agent_autonomy_plan.md`、`agent_nominated_rotation_plan.md`；同步 `agent_swap` v1 影子（已開啟）與已實作未開啟版本的狀態、契約、`team_inputs` 2.9 與名稱對照。 |
| 2026-11-10 | 同步 `status.md` 閘門表與各計畫的進度敘述（`reentry_cooldown` v2、`drawdown_derisk` v5、現金三個 v2 已開啟）；索引加入 `follow_up_plan.md`。 |

## 維護規則

- 改了資料夾用途、Agent 邊界或契約，要同步更新對應 README 與本索引。
- 新增文件時，加到上面的分類並在更新紀錄補一行。
- 文件只描述已完成、部分完成或尚未接入的功能，不把測試資料或介面說成真實資料來源。
- 文件用白話寫，正文不用程式名稱。決策閘門、欄位與旗標的名稱統一放在 [glossary.md](glossary.md)（只寫是什麼、在哪，不寫啟用狀態；狀態只在 `config/` 與 [status.md](status.md)），其他主題的程式名稱放各文件文末「附錄：名稱對照」。
