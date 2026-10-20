# ETF-AGENTS｜AI CUP 2026 台股 ETF 決策系統

收集並驗證台股行情與公司資料，經分析團隊、多空研究、交易整合與風險審查，產生供人工檢視的決策報告。

## 快速啟動

在專案根目錄操作，需有可執行 `start.sh` 的 shell。首次安裝先建立本機環境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

啟動 Docker Desktop，執行：

```bash
./start.sh
```

首次會建立虛擬帳戶，之後驗證並沿用既有帳本，開啟[績效儀表板](http://127.0.0.1:8000)。按 `Ctrl+C` 關閉；已有帳戶不會重新注資。此指令不抓資料、不執行決策。

## 常用指令

| 用途 | 指令 | 執行內容 |
| --- | --- | --- |
| 看帳戶與績效 | `./start.sh` | 開帳或驗證既有帳戶，啟動唯讀儀表板；不更新資料 |
| 只更新資料 | `./start.sh auto` | 更新行情、公司事件與財報，顯示資料狀態；不建研究快照、不推進帳本或執行決策（`data` 為相容別名） |
| 更新資料與帳本 | `./start.sh daily` | 驗證來源、更新資料、建立 Snapshot、結算／準備虛擬帳本；不跑 Agent |
| 完整每日決策 | `./start.sh run` | 更新資料、建立快照、準備帳本，執行分析與決策 Agent，封存中文報告 |
| 查看資料狀態 | `./start.sh status` | 顯示資料數量、品質問題、驗證結果與最後更新時間（台北時間）；不重新抓取、不需 Docker |

首次跑完整決策前，先用 `./start.sh` 開帳。完整決策預設使用已登入的 Claude CLI；可改用 Codex 或 OpenCode：

```bash
./start.sh run --agent-cli codex
./start.sh run --agent-cli opencode
```

參數會原樣傳給 `run`／`status`，用 `./start.sh run --help` 查看（如 `--preflight-only`、`--resume`）。環境設定見 [Docker 使用說明](docs/docker.md)，單步操作見 [CLI 說明](cli/README.md)。

ChatGPT.app 內附的 Codex CLI 路徑已加入本機 zsh 的 PATH；`start.sh` 也會在 macOS 非互動環境偵測該路徑。OpenCode 以隔離唯讀 Agent 執行，禁用外部插件及寫入／命令工具；其 JSONL 用量只記錄 CLI 回報數值，並不等於實際訂閱費用。兩者的輸出均由同一套 Python Validator 重建；換執行器時不可續用其他執行器的 run。

## 決策流程

```text
資料驗證與研究快照 → 三位分析師 → 多頭／空頭研究
→ 交易 Agent → 風險 Agent → Python 配置與風控 → 封存報告 → 人工檢視
```

Agent 提出有證據的市場解讀與買賣意圖；指標、權重、股數、費稅及競賽限制由 Python 計算與驗證。所有下游輸入必須與研究快照的截止時間、資料版本一致，並以當時可得時間防止未來資訊洩漏。資料不足或風控失敗時停止或明確降級。

既有持股若因價格變動超出單股權重硬上限，配置程式會在 Guard 前按整張計算必要減碼，連同部分成交的壓力情境重算；原 Agent 意圖與程式風險減碼分開記錄。無法滿足所有硬性限制時仍會拒絕。

## 目前狀態與限制

- **已接入**：150 檔交易池、yfinance 研究日線、官方月營收／重大訊息／財報、虛擬帳本、每日決策鏈與可重建的中文報告。帳本使用 TWSE／TPEx 官方價量。
- **價格口徑**：Snapshot 的決策與帳本用價是同日官方原價（`execution_close_price`），研究指標用調整價；上市與上櫃官方歷史月行情皆可補抓。缺官方價時退回調整價，重放過去日期時含事後除權息調整，見[架構](docs/architecture.md)。
- **尚未接入**：正式全市場情緒與合法歷史分析師共識，維持 `unavailable`。FinMind 財報與新聞等候選資料，未通過授權、時間與覆蓋核對前不進正式決策。
- **尚未完成**：D-Plan 候選可由 CLI 匯出，但每日流程尚未自動呼叫；主辦方伺服器驗證尚未完成；回測功能已移除。

資料更新成功不代表當天可決策；行情覆蓋、官方價量、交易狀態與日曆仍須通過檢查。

若 `run` 顯示「虛擬帳本未建立本次 prepare 狀態」，先檢查是否有舊決策等待指定成交日官方價量。缺歷史上櫃價量時，在專案根目錄執行 `.venv/bin/python scripts/repair_pending_tpex_close.py`；此指令只補目前待結算決策所需的 TPEx 官方價量，然後執行 `.venv/bin/python cli/virtual_account.py settle`，確認輸出 `status=settled` 後再跑 `./start.sh run`。若缺的是 TWSE 官方價量或來源未提供資料，會停止並顯示缺口，不會改用 Yahoo 價格。

## 資料與報告位置

| 路徑 | 內容 |
| --- | --- |
| `data/official_universe.csv` | 150 檔交易池輸入 |
| `var/etf_agent.db` | 行情、公司文件、來源原文、時間與版本 |
| `artifacts/reports/latest.md` | 最新中文報告或失敗診斷 |
| `artifacts/daily_runs/<run_id>/pipeline.json` | 每日執行狀態與阻擋原因 |
| `artifacts/portfolio_decisions/` | 不可變決策封存與重建輸入 |
| `artifacts/virtual_accounts/` | 虛擬帳本與帳戶快照 |

`var/` 與 `artifacts/` 不納入 Git。

## 詳細文件

- [環境安裝與 Docker](docs/docker.md)、[CLI 與單步操作](cli/README.md)
- [每日決策架構](docs/architecture.md)、[資料來源與授權限制](docs/data_sources_inventory.md)
- [事件與市場情緒邊界](docs/event_market_scope.md)、[事件研究整合](docs/integrated_event_analysis.md)、[技術分析歷史輸入](docs/technical_trend_history.md)
- [開發規則與測試要求](AGENTS.md)：核心程式在 `src/etf_agent/`，命令入口在 `cli/`／`scripts/`，Agent Skill 在 `tradeagent/`。
