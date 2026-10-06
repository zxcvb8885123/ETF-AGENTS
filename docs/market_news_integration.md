# 全市場新聞收集與接入

2026-10-05 完成全市場 RSS Provider、版本保存、全項目標籤核對、研究結論重建與第三位分析師接線。公司事件維持逐公司輸出，全市場情緒只輸出一次。不執行多空、交易或風險流程。

## 真實取得與本次結果

- 中央通訊社財經 RSS 20 則、自由時報財經 RSS 40 則，共 60 則。均保存原始回應、內容雜湊與實際取得時間。
- Codex 已讀取全部標題與前言並逐項標籤；11 則直接涉及整體市場，49 則為單一公司、產業／海外背景或無關內容。不是關鍵字分類器或新聞篇數投票。
- 本次新聞診斷看法為正面，並保留市場分化的反向材料。這是新聞材料的研究推論，不是代表全市場投資人的民調。
- 資料截止為 2026-10-05 15:17:17（台北時間）。本次 RSS 實際保留 10/4～10/5 的最近項目；查詢從 10/1 起，不代表 10/1 起的歷史全部補齊。
- RSS 只有標題與前言，未把前言當成全文。中央社與自由時報的官方使用說明皆限制非商業用途；本專案比賽使用權尚未確認。因此真實資料的正式輸出為 unavailable，診斷結果另存，不交給交易。沒有自動核准來源。
- 公司事件識別與上次一致，沿用先前已讀材料的判讀；本次重新執行全市場新聞分析。研究 Snapshot 的個股行情仍截至 10/2，不能宣稱是 10/5 收盤行情。舊帳戶只重估供輸入驗證，不寫入真實帳戶。

來源：[中央通訊社 RSS 規範](https://www.cna.com.tw/about/rss.aspx)、[自由時報 RSS 規範](https://service.ltn.com.tw/RSS)。

## 契約與資料流

`MarketNewsRSSProvider → market_news_captures / ImmutableRunStore → prepare → 完整 labels + synthesis + source_approvals → evaluate → market_news_input → 第三位分析師`

- 程式放在 `src/etf_agent/perception/market_news/`，按契約、Provider、repository、tools、validator、service 分檔。
- 資料庫新增 `market_news_captures`，全市場 scope 為 `TW_STOCK_MARKET`，只追加版本；不覆寫個股新聞。
- `market-news-1.0` 保留 snapshot_id、cutoff、window_start、所有 captures、所有 labels、synthesis、source_approvals 及內容雜湊。新聞來源與原文主機為白名單，禁止 XML entity、超大回應及任意重新導向。
- 每則標籤包含 item_id、canonical_content_id、relevance、stance 與中文理由；不得少標、重標或改內容識別。相關性為市場、公司、無關或不明，只有市場項目能支持全市場發現。
- 模型綜合決定 positive／neutral／negative／unknown，附 findings 與 evidence_ids。不以篇數、固定權重或股價漲跌公式決定方向。程式核對引用與時鐘；文字推論仍是研究判斷，不宣稱驗證了投資效果。
- 聚合先按 canonical content ID 去重。內容識別以正規化標題與發布日建立；不同標題描述同一事件可能仍有多列，分析師需辨認同一事件，不能以列數加強結論。摘要截斷，不能靠全文雜湊完成跨站轉載辨識。
- 正式輸出需至少五個市場獨立內容、兩個來源、查詢全項目標籤、沒有來源取得失敗，以及相關來源皆具 `approved` 的 `competition_research` 核准紀錄與授權依據。此門檻只檢查樣本是否足夠，不決定情緒方向。人工測試中的核准紀錄只是 fixture，與真實授權無關。
- 未核准內容只保留 diagnostic_outlook；正式 outlook=unknown、findings=[]。不能把診斷複製成正式結論。
- `DecisionInputBuilder` 支援選配 `market_news_input={bundle,result}`。輸入驗證與報告驗證都從 captures + labels 重建 result，驗證相同 Snapshot/cutoff。缺少接入才用原本的降級通道；不再永久固定 unknown。
- 第三位分析師 brief 已包含全量 market_news，正式 market_sentiment 只出現一次。既有 daily pipeline 收到此版本化 DecisionInputBundle 時可沿用新通道，無需第四位分析師。

## 執行

補抓並保存資料庫與不可變檔案：

```bash
.venv/bin/python cli/market_news.py capture \
  --window-start 2026-10-05T00:00:00+08:00 \
  --database var/etf_agent.db \
  --output-root artifacts/market_news_captures
```

分析師讀取 prepared 的全量項目後，交付完整 labels、綜合 synthesis 及真實 source_approvals，與 captures 組成有內容雜湊的 market-pack。只有外部來源的真實授權核對可以建立 approved，不能為了得到方向修改狀態。將同 Snapshot/cutoff 的市場資料包接入：

```bash
.venv/bin/python cli/market_news.py integrate \
  --decision-input PATH_TO_DECISION_INPUT \
  --market-pack PATH_TO_MARKET_PACK \
  --output-root artifacts/market_news_research
```

CLI 不呼叫模型，不自動推算情緒標籤。缺少判讀時需由 Codex 按 Skill 執行；沒有新增排程，也不宣稱目前每日會自動補抓與標籤。原本個股 PerceptionDataBundle 路徑維持兼容。

本次報告在 `artifacts/market_news_20261005/report.md`，同源 JSON、完整標籤、原始 captures、資料包、輸入與稽核均保留於不可變 run。全市場來源接線已完成；比賽使用權與完整歷史新聞庫尚未完成。

## 歷史來源更新

已新增資料包1.1與日期補抓入口，補入缺日資料；上文1.0與缺口為第一批RSS測試當時狀態。最新數量、來源範圍與已查明的使用規範，見 [市場新聞歷史補抓](market_news_history.md)。

## 來源使用紀錄的資料庫接入（2026-10-05）

新聞存入資料庫不代表使用範圍已核准。原先 `market_news_captures` 只保存新聞回應，`source_approvals` 與規範原始頁只保存在研究 artifact，因此資料庫無法重建來源使用核對。

現已新增追加式 `market_news_source_reviews`：保存來源、核對結果、用途、核對時間、實際保存時間、原始規範及內容雜湊。`MarketNewsRepository.save_source_reviews` 核對原始依據與時間；`load_source_reviews` 只讀 cutoff 前已保存的版本。`apply_database_source_reviews` 接入最新的當時可得核對；不改新聞、標籤或情緒結論，不自動把未確認狀態提升為核准。CLI `integrate --database var/etf_agent.db` 可使用這條接線。

若只有使用範圍未確認，中文報告顯示「來源使用範圍尚未確認，正式結果暫不提供」，不再概括寫成新聞資料不足。若同時缺少樣本、來源或完整輸入，仍保留資料不足。結構化正式輸出維持 fail-closed。

本次查得：資料庫有 3,664 筆 FinMind 新聞候選、94 份全市場來源回應、40 份新聞原頁回應；已標示 approved 的 67 筆為台糖公司新聞，不能代表整體台股，且不在本次十月查詢視窗。沒有查到中央社、自由時報或 FinMind 的比賽研究核准紀錄。已將既有官方規範原始頁與四個來源的未確認核對紀錄補存資料庫，並重跑接入驗證；這不等於取得新的外部授權。
