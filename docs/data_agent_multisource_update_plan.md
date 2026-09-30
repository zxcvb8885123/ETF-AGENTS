# Data Agent 資料來源更新計畫

更新：2026-09-30。資料來源以 150 檔研究日線、官方公司資料、FinMind 基本面與個股新聞線索為主；正式帳本價量及風控所需官方來源維持獨立。實際可執行清單見[資料來源清單](data_sources_inventory.md)。

## 已接入

| 資料 | 擷取與保存 | 現在的用途 |
| --- | --- | --- |
| 150 檔研究日線 | yfinance 每日增量更新；保存原始與調整收盤價、來源與抓取版本；檢查同日 150 檔覆蓋 | 技術研究與 Snapshot 行情；缺檔降級，不混入官方未調整原價 |
| 官方月營收、重大訊息、財報、公司基本資料 | MOPS／TWSE／TPEx 公開端點，保留原始回應與實際取得時間 | 已驗證部分供基本面與事件事實；產業分類候選需核准後升版 |
| FinMind 三大報表 | `TaiwanStockFinancialStatements`、`TaiwanStockBalanceSheet`、`TaiwanStockCashFlowsStatement`；逐股保存原始回應與版本 | 2026-09-30 三表各 149／150 檔有資料，`3718.TWO` 空結果；目前只作候選，未進 Snapshot |
| FinMind 個股新聞 | `TaiwanStockNews` 逐股、逐日查詢；保存股票、標題、來源媒體、連結、原始時間字串與抓取版本 | 只作 `license_status=unverified` 的新聞線索；不進正式事件研究、情緒或回測 |
| 正式帳本價量、交易限制與日曆 | TWSE／TPEx 官方當日價量、必要時上市個股月補缺；政府開放交易狀態與開休市日曆 | 比賽成交均價、持股估值、交易 Guard 與 T+2 交割；資料不足時 fail-closed |

2026-09-29 個股新聞實抓 150 檔，116 檔有資料、34 檔空結果，共保存 1,004 筆未核准線索。這是資料集回傳量，不是新聞市場覆蓋率或來源授權證明。

已從每日流程移除與 150 檔無關的台糖 RSS。舊資料與原始回應保留供稽核，不新增台糖新聞候選。研究日線不再用官方個股月行情補；正式帳本仍按主辦方口徑使用官方成交金額與股數。

## 自動擷取與配額

平日台北時間 20:00 的 `com.etf-agents.data` 執行 `./start.sh data`，更新 yfinance、官方公司資料及 FinMind 三大報表，不執行決策或帳本。FinMind 個股新聞需每檔各查一天，全市場單次查詢在目前註冊等級不可用；平日 22:00 的 `com.etf-agents.finmind-news` 每日查詢 150 檔，與約 450 次財報請求分開，避免同一小時耗盡 600 次額度。兩個排程的來源失敗或空結果都以非零狀態明確回報，不把缺資料補成成功。

Token 由執行環境或本機 `.env` 載入；`.env` 權限 600、Git 忽略。Token 不寫入原始回應、SQLite、URL、日誌、Snapshot 或版控。直接執行 CLI 前須自行提供 `FINMIND_TOKEN` 環境變數。

## 正式研究接線前的必要工作

1. **時間證據**：FinMind 財報 `date` 是會計期間；新聞 `date` 雖有時分秒，API 未標時區。兩者目前 `published_at` 均不得憑猜測填入；`available_at` 固定本次抓取時間。今日回補不能回灌歷史決策。
2. **授權**：FinMind 的 API 使用權不等於新聞原媒體的再利用授權。逐篇確認授權前保持 `unverified`，Market Sentiment and Analyst Agent 固定輸出 `unavailable`。
3. **覆蓋與去重**：以 150 檔固定分母報告無資料、重複連結、跨股誤配與來源集中；新聞依 canonical content ID 去重後才能評估完整查詢視窗。
4. **財務口徑**：核對單位、幣別、合併／個別、單季／累計、金融業分類與官方更正；先釐清 `3718.TWO` 的承接與缺報表原因。衍生比率只能由確定性 Python 重算。
5. **契約與驗證**：只有完成來源核准、時間證據、Snapshot 契約與 Validator 升版後，候選資料才可交給下游；任何缺漏維持降級或停止。

本計畫只涵蓋已選定來源與必要缺口，不把未接入的資料服務列為現行來源。[FinMind 基本面](https://finmind.github.io/tutor/TaiwanMarket/Fundamental/)、[FinMind 個股新聞](https://finmind.github.io/en/tutor/TaiwanMarket/Others/)、[FinMind 授權界線](https://finmind.github.io/en/Disclaimer/)。
