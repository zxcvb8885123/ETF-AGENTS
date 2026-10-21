# 全市場新聞收集與接入

2026-10-05 完成：

- 全市場 RSS Provider
- 版本保存
- 全項目標籤核對
- 研究結論重建
- 第三位分析師接線

公司事件維持逐公司輸出，全市場情緒只輸出一次。本次不執行多空、交易或風險流程。

## 本次真實取得的結果

| 項目 | 內容 |
| --- | --- |
| 來源 | 中央通訊社財經 RSS 20 則、自由時報財經 RSS 40 則，共 60 則 |
| 保存 | 原始回應、內容雜湊、實際取得時間 |
| 資料截止 | 2026-10-05 15:17:17（台北時間） |
| 實際涵蓋 | RSS 只保留 10/4～10/5 的最近項目。查詢從 10/1 起，不代表 10/1 起的歷史都已補齊 |

### 標籤與看法

- Codex 讀了全部標題與前言，逐項標籤。
- 11 則直接涉及整體市場，49 則是單一公司、產業／海外背景或無關內容。
- 這不是關鍵字分類器，也不是新聞篇數投票。
- 診斷看法為正面，並保留市場分化的反向材料。
- 這是新聞材料的研究推論，不是代表全市場投資人的民調。

### 使用權與正式輸出

- RSS 只有標題與前言，沒有把前言當成全文。
- 中央社與自由時報的官方使用說明都限制非商業用途。本專案的比賽使用權尚未確認。
- 因此真實資料的**正式輸出是 `unavailable`**，診斷結果另存，不交給交易。
- 沒有自動核准來源。

### 其他限制

- 公司事件識別與上次一致，沿用先前已讀材料的判讀。全市場新聞分析則重新執行。
- 研究 Snapshot 的個股行情仍截至 10/2，不能宣稱是 10/5 收盤行情。
- 舊帳戶只重估供輸入驗證，不寫入真實帳戶。

來源規範：[中央通訊社 RSS](https://www.cna.com.tw/about/rss.aspx)、[自由時報 RSS](https://service.ltn.com.tw/RSS)

## 資料流

```text
MarketNewsRSSProvider
  → market_news_captures / ImmutableRunStore
  → prepare
  → 完整 labels + synthesis + source_approvals
  → evaluate
  → market_news_input
  → 第三位分析師
```

程式放在 `src/etf_agent/perception/market_news/`，依契約、Provider、repository、tools、validator、service 分檔。

## 契約規則

### 資料保存

- 資料庫新增 `market_news_captures`。全市場 scope 為 `TW_STOCK_MARKET`，只追加版本，不覆寫個股新聞。
- `market-news-1.0` 保留：`snapshot_id`、cutoff、`window_start`、所有 captures、所有 labels、synthesis、`source_approvals`、內容雜湊。
- 新聞來源與原文主機採白名單。禁止 XML entity、超大回應與任意重新導向。

### 標籤

每則標籤包含 `item_id`、`canonical_content_id`、`relevance`、`stance` 與中文理由。

- 不得少標、重標，也不得改內容識別。
- 相關性分四類：市場、公司、無關、不明。
- 只有「市場」項目能支持全市場發現。

### 綜合判讀

- 模型綜合決定 `positive`／`neutral`／`negative`／`unknown`，附 findings 與 `evidence_ids`。
- 不以篇數、固定權重或股價漲跌公式決定方向。
- 程式核對引用與時鐘。文字推論仍是研究判斷，不宣稱驗證了投資效果。

### 去重

- 聚合先依 canonical content ID 去重。
- 內容識別由「正規化標題＋發布日」建立。
- 不同標題描述同一事件時，可能仍有多列。分析師要辨認同一事件，不能用列數加強結論。
- 摘要會截斷，不能靠全文雜湊辨識跨站轉載。

### 正式輸出的門檻

正式輸出必須同時滿足：

1. 至少 5 個市場獨立內容。
2. 至少 2 個來源。
3. 查詢範圍內的全部項目都有標籤。
4. 沒有來源取得失敗。
5. 相關來源都有 `approved` 的 `competition_research` 核准紀錄與授權依據。

這些門檻只檢查樣本是否足夠，不決定情緒方向。人工測試中的核准紀錄只是 fixture，與真實授權無關。

未核准的內容只保留 `diagnostic_outlook`。此時正式 `outlook=unknown`、`findings=[]`，不能把診斷複製成正式結論。

### 接入第三位分析師

- `DecisionInputBuilder` 支援選配的 `market_news_input={bundle,result}`。
- 輸入驗證與報告驗證，都從 captures + labels 重建 result，並驗證相同 Snapshot／cutoff。
- 缺少接入時才使用原本的降級通道，不再永久固定為 `unknown`。
- 第三位分析師的 brief 已包含全量 market_news，正式 `market_sentiment` 只出現一次。
- 既有 daily pipeline 收到這個版本化的 DecisionInputBundle，就能沿用新通道，不需要第四位分析師。

## 執行方式

### 1. 補抓並保存

```bash
.venv/bin/python cli/market_news.py capture \
  --window-start 2026-10-05T00:00:00+08:00 \
  --database var/etf_agent.db \
  --output-root artifacts/market_news_captures
```

### 2. 分析師產生判讀

分析師讀取 prepared 的全量項目，交付：

- 完整 labels
- 綜合 synthesis
- 真實的 `source_approvals`

再與 captures 組成有內容雜湊的 market-pack。只有外部來源的真實授權核對，才能建立 `approved`。不能為了得到方向而修改狀態。

### 3. 接入同 Snapshot／cutoff 的資料包

```bash
.venv/bin/python cli/market_news.py integrate \
  --decision-input PATH_TO_DECISION_INPUT \
  --market-pack PATH_TO_MARKET_PACK \
  --output-root artifacts/market_news_research
```

### 注意

- CLI 不呼叫模型，也不自動推算情緒標籤。缺少判讀時，需由 Codex 依 Skill 執行。
- 沒有新增排程，也不宣稱每日會自動補抓與標籤。
- 原本的個股 PerceptionDataBundle 路徑維持兼容。

### 本次產出

報告在 `artifacts/_archive/runs/market_news_20261005/report.md`。同源 JSON、完整標籤、原始 captures、資料包、輸入與稽核，都保存在不可變 run。

| 狀態 | 項目 |
| --- | --- |
| 已完成 | 全市場來源接線 |
| 尚未完成 | 比賽使用權、完整歷史新聞庫 |

## 歷史來源更新

已新增資料包 1.1 與日期補抓入口，補入缺日資料。上文的 1.0 與缺口，是第一批 RSS 測試當時的狀態。最新數量、來源範圍與使用規範，見[市場新聞缺日補抓](market_news_history.md)。

## 來源使用紀錄的資料庫接入（2026-10-05）

### 問題

新聞存進資料庫，不代表使用範圍已核准。

原本 `market_news_captures` 只保存新聞回應。`source_approvals` 與規範原始頁只存在研究 artifact，所以資料庫無法重建「來源使用核對」。

### 做法

新增只追加的 `market_news_source_reviews`，保存：來源、核對結果、用途、核對時間、實際保存時間、原始規範、內容雜湊。

| 函式 | 作用 |
| --- | --- |
| `MarketNewsRepository.save_source_reviews` | 核對原始依據與時間 |
| `load_source_reviews` | 只讀 cutoff 前已保存的版本 |
| `apply_database_source_reviews` | 接入最新的、當時可得的核對 |

這條接線**不會**修改新聞、標籤或情緒結論，也不會把「未確認」自動升為「核准」。

CLI 可用 `integrate --database var/etf_agent.db` 使用它。

### 報告用語

| 情況 | 中文報告寫法 |
| --- | --- |
| 只有使用範圍未確認 | 「來源使用範圍尚未確認，正式結果暫不提供」 |
| 同時缺樣本、來源或完整輸入 | 維持「資料不足」 |

結構化的正式輸出維持 fail-closed。

### 本次查到的現況

| 項目 | 數量 |
| --- | --- |
| FinMind 新聞候選 | 3,664 筆 |
| 全市場來源回應 | 94 份 |
| 新聞原頁回應 | 40 份 |
| 標示 `approved` | 67 筆（皆為台糖公司新聞，不能代表整體台股，也不在本次十月查詢視窗內） |

- 沒有查到中央社、自由時報或 FinMind 的比賽研究核准紀錄。
- 已把既有官方規範原始頁，以及四個來源的「未確認」核對紀錄補存進資料庫，並重跑接入驗證。
- 這不等於取得新的外部授權。
