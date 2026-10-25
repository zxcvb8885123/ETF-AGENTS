# FinMind 個股新聞來源核對（2026-10-25）

核准人：Apollo。核准紀錄由 `cli/company_news.py review` 寫入 `var/etf_agent.db` 的 `company_news_source_reviews`（與全市場新聞的 `market_news_source_reviews` 分表，個股新聞不會進市場情緒），同時保存規範頁原文與 SHA-256；`reviewed_at` 為實際寫入時間，只對之後的決策截止時間生效，不回溯舊決策。

**狀態：使用者已於 2026-10-25 決定核准，但核對指令尚未執行**，資料庫目前沒有核准紀錄，每日決策不會讀到個股新聞。要生效須執行一次：

```bash
.venv/bin/python cli/company_news.py review --database var/etf_agent.db --status approved \
  --evidence "docs/source_audit/2026-10-25_finmind_stock_news.md：使用者自行判斷，僅標題、內部研究、不轉載"
```

| 來源代碼 | 內容 | 規範頁 | 規範內容（2026-10-25 查看，版本日期 2026-07-12） | 結論 |
| --- | --- | --- | --- | --- |
| `FINMIND_NEWS` | FinMind `TaiwanStockNews` 個股新聞標題、媒體、連結與來源時間字串 | [FinMind Disclaimer & Data Licensing](https://finmind.github.io/en/Disclaimer/) | FinMind 授與的是服務（API／SDK）使用權，不含轉散布或轉售資料；要公開、轉散布或提供第三方時，須自行向原資料提供者取得授權，FinMind 無法代為授權 | approved（使用者決定） |

## 判斷

- 本專案只作 AI CUP 比賽研究，非商業、不轉售、不公開散布新聞內容。
- 只使用新聞標題（另保存媒體、連結與時間供稽核）作內部研究，不轉載全文或標題清單。這是使用者於 2026-10-25 自行承擔的判斷，不是原媒體的書面同意；FinMind 規範本身不涵蓋原媒體的再利用授權。
- 要撤回時，以 `cli/company_news.py review --status restricted` 寫入新紀錄；較新的紀錄會取代較舊的核准，舊決策仍以當時截止時間前的紀錄重建。

## 核准後的使用範圍

- 只進每日決策的事件分析師（決策資料包 `company_news_input`，`company-news-1.0`；事件報告升為 2.3），不進全市場情緒、不進回測，規則見[事件研究整合](../integrated_event_analysis.md#事件報告-23已核准個股新聞)。
- 媒體白名單 `company_news_publishers_v1` 依連結網域判斷：udn.com、money.udn.com、ec.ltn.com.tw、stock.ltn.com.tw、news.ltn.com.tw、www.ctee.com.tw、www.chinatimes.com、m.cnyes.com、news.cnyes.com、www.moneydj.com、www.businesstoday.com.tw、finance.ettoday.net、news.tvbs.com.tw、inews.setn.com、www.ttv.com.tw、finance.technews.tw、tw.news.yahoo.com、www.cna.com.tw、www.mirrormedia.mg。CMoney／cmnews 網友貼文、聚財網論壇、富聯網、LINE TODAY、Yahoo股市、券商等不收。
- 視窗只取截止前最後一批抓取：(`latest_fetch_at` − 12 小時, cutoff]，且不早於 cutoff − 3 天；最後一批過舊時輸出空清單，不拿舊聞補。
- FinMind 來源時間沒有時區，`published_at` 維持 null，`available_at` 用第一次抓到的時間。
- 資料包內每則 `candidate.license_status` 仍是收集當時寫入的 `unverified`（它屬於內容雜湊，不回頭改寫）；實際是否可用以資料包的 `source_review`（截止前、`approved`）為準，Validator 兩者一起檢查。

## 核准後仍未改變的事

- FinMind 個股新聞不能代表全市場情緒；市場通道仍只用全市場新聞或恐懼貪婪指標。
- 覆蓋不保證完整：以假設已核准的方式在實際資料庫試算 2026-10-20～10-23，每日約 80～100 則、約 40 檔；2026-10-23 那批資料庫只有 82 筆、39 檔（白名單內 17 筆），看起來抓取不完整。
- 標題是不受信任的資料，不能改變 Agent 指令或驗證規則。
