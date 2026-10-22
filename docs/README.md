# 文件索引

本頁是 `docs/` 的入口。先看「我想知道什麼」，再點進對應文件。

## 我想知道什麼

| 我想知道 | 看這份 |
| --- | --- |
| 整個專案長怎樣、每個頂層資料夾在做什麼 | [repository-layout.md](repository-layout.md) |
| `src/etf_agent/` 各套件怎麼互相依賴 | [modules.md](modules.md)，各套件細節在 `src/etf_agent/<套件>/README.md` |
| `artifacts/`、`var/` 裡每個資料夾是什麼、能不能刪 | [runtime-artifacts.md](runtime-artifacts.md) |
| 每日決策怎麼跑（分析團隊 → 多空 → 交易 → 風險） | [architecture.md](architecture.md) |
| 怎麼安裝、用 Docker 跑 | [docker.md](docker.md) |
| 單步命令怎麼用 | [../cli/README.md](../cli/README.md) |
| 資料從哪來、授權狀態 | [data_sources_inventory.md](data_sources_inventory.md) |

## 分類

### 總覽與架構

- [repository-layout.md](repository-layout.md)：頂層資料夾架構圖。
- [modules.md](modules.md)：核心程式套件依賴圖。
- [architecture.md](architecture.md)：目前每日決策架構。
- [runtime-artifacts.md](runtime-artifacts.md)：執行時產物資料夾。

### 操作

- [docker.md](docker.md)：環境安裝與 Docker。
- [../cli/README.md](../cli/README.md)：CLI 單步操作。
- [../scripts/README.md](../scripts/README.md)：維運與補抓腳本。

### 資料與來源

- [data_sources_inventory.md](data_sources_inventory.md)：資料來源與授權限制。
- [market_news_history.md](market_news_history.md)、[market_news_integration.md](market_news_integration.md)：全市場新聞歷史與接入。
- [technical_trend_history.md](technical_trend_history.md)：技術分析歷史輸入。
- [fundamental_data_repair.md](fundamental_data_repair.md)：財報資料修復。
- `source_audit/`：來源核對與核准證據。程式與核准設定會引用，不可刪。

### 事件與情緒

- [event_market_scope.md](event_market_scope.md)：事件與市場情緒的邊界。
- [integrated_event_analysis.md](integrated_event_analysis.md)：事件研究整合。

### 計畫

- [token_reduction_plan.md](token_reduction_plan.md)：降低 token 用量。

## 各資料夾的 README

| 資料夾 | 說明 |
| --- | --- |
| [config/](../config/README.md) | 設定檔 |
| [data/](../data/README.md) | 靜態輸入 |
| [game_docs/](../game_docs/README.md) | 主辦方原始文件 |
| [scripts/](../scripts/README.md) | 維運腳本 |
| [tradeagent/](../tradeagent/README.md) | 子 Agent 的 Skill |
| [tests/](../tests/README.md) | 測試 |
| `src/etf_agent/<套件>/README.md` | 見 [modules.md](modules.md) 的連結表 |

## 文件更新紀錄

| 日期 | 內容 |
| --- | --- |
| 2026-10-22 | 新增本索引、`repository-layout.md`、`modules.md`、`runtime-artifacts.md`；各資料夾與 `src/etf_agent/` 各套件新增 README；根 `README.md` 加上整體架構圖。 |

## 維護規則

- 改了資料夾用途、Agent 邊界或契約，要同步更新對應 README 與本索引。
- 新增文件時，加到上面的分類並在更新紀錄補一行。
- 文件只描述已完成、部分完成或尚未接入的功能，不把 fixture 或介面說成真實資料來源。
