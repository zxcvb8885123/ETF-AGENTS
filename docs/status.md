# 目前狀態與限制

本頁記錄各功能目前是已接入、部分接入還是尚未接入，以及各來源的核准狀態。功能或來源狀態改變時請更新本頁。

- **已接入**：150 檔交易池、yfinance 研究日線、官方月營收／重大訊息／財報、虛擬帳本、每日決策鏈與可重建的中文報告。帳本使用 TWSE／TPEx 官方價量。
- **價格口徑**：Snapshot 的決策與帳本用價是同日官方原價（`execution_close_price`），研究指標用調整價；上市與上櫃官方歷史月行情皆可補抓。缺官方價時退回調整價，重放過去日期時含事後除權息調整，見[架構](architecture.md)。
- **已接入**：[台股恐懼貪婪指標](market_fear_greed.md)（期交所 VIX、Put/Call 比、FinMind 融資），`./start.sh daily` 在 cutoff 前抓取，由程式算出整體台股情緒；VIX 依使用者決定以非商業比賽研究用途核准，見[來源核對](source_audit/2026-10-23_market_fear_greed_sources.md)。
- **部分接入（來源已由使用者核准）**：[隔夜市場背景](overnight_market.md)（Yahoo Finance 的 ^GSPC、^IXIC、^SOX、台股代理 EWT／TSM、日韓指數 ^N225／^KS11、VIX，加期交所官網即時行情的台指期夜盤，`overnight-v1.1`）只給風險 Agent 現金姿態與最終報告當背景，不併入全市場情緒。`./start.sh daily` 在 cutoff 前抓取，`config/overnight_market_sources.json` 已核准（2026-10-30，apollo），空窗或抓取失敗時為 `unavailable`；改回未核准也是 `unavailable`。台指期夜盤只在台北 05:00 結束後才有（平日 21:00 的決策一律沒有）；期交所 OpenAPI 與 FinMind 的盤後資料落後一天，不能用，改用官網即時行情，**夜盤的日期與時間格式尚待明早 05:00 後第一次實抓驗證**，不符時會維持 `unavailable`。
- **已接入**：全市場新聞（中央社、自由時報 RSS，2026-10-23 核准）。`./start.sh daily` 在 cutoff 前抓取，每日決策由子 Agent 逐則標註後程式重建；至少 5 則獨立市場新聞、2 家媒體才算可用，否則改用恐懼貪婪指標。新聞步驟失敗不會讓整天失敗。
- **已接入**：FinMind 現金流量表（稅前淨利與官方損益表相符才用）進基本面分析；FinMind 單季財報經官方金額核對後，可補足去年同期累計財報（`cli/fundamental_repair.py`）。
- **已接入（待執行核對）**：FinMind 個股新聞進事件分析師（只進公司事件通道，不代表全市場情緒）。只在決策截止時已核准時加入，且只用截止前最後一批抓取、連結網域在媒體白名單內的標題；事件報告升為 2.3。使用者已於 2026-10-25 自行判斷核准（僅標題、內部研究、不轉載，不是媒體書面同意），但**尚未執行**核對指令：`cli/company_news.py review --database var/etf_agent.db --status approved --evidence "..."`；執行前每日決策不會讀到個股新聞。見[來源核對](source_audit/2026-10-25_finmind_stock_news.md)。
- **已接入**：主辦方資料包（DataPack）對帳。每日 `run` 在呼叫任何 Agent 前，把帳本持股與現金和資料包比對，對不上就停止；D-Plan 只在對帳一致（或明確略過）時產生，賣出不得超過資料包持股。見下方「主辦方資料包」。
- **已接入但仍需人工上傳**：每日流程在決策為 `approved`／`no_trade`、競賽設定已確認、帳本綁定與資料包對帳都通過時，自動建立 D-Plan v4.3 候選。主辦方伺服器驗證與上傳仍由人工執行；回測功能已移除。
