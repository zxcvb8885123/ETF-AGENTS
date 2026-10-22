# 全市場新聞收集與接入

## 用途與範圍

為第三位分析師（事件與市場情緒）提供「一次整體台股情緒」的全市場新聞輸入。

- 收集財經 RSS、保存原始回應、對查詢視窗內全部項目逐則標籤、重建研究結論、接入決策輸入。
- 公司事件維持逐公司輸出；全市場情緒只輸出一次。
- 不執行多空、交易或風險流程，不產生配置、權重或訂單。
- 歷史原頁補抓與資料包 1.1 見[市場新聞缺日補抓](market_news_history.md)。

## 運作方式

```mermaid
flowchart LR
  A[MarketNewsRSSProvider] --> B[market_news_captures]
  B --> C[prepare]
  C --> D[labels + synthesis + source_approvals]
  D --> E[evaluate]
  E --> F[market_news_input]
  F --> G[第三位分析師]
```

程式在 `src/etf_agent/perception/market_news/`，依 `contracts`、`provider`、`history`、`repository`、`tools`、`validator`、`service` 分檔。

| 階段 | 執行者 | 說明 |
| --- | --- | --- |
| 收集與保存 | 程式 | 取得 RSS，保存原始回應、內容雜湊與實際取得時間 |
| 標籤與綜合判讀 | 分析師（Codex，依 Skill） | 讀全部標題與前言；CLI 不呼叫模型、不自動推算標籤 |
| 核對與重建 | 程式 | 檢查覆蓋、引用、時鐘與門檻，輸出 result |
| 接入 | 程式 | 產生 `market_news_input={bundle,result}` 並重跑輸入驗證 |

## 契約與規則

### 來源

| 來源代碼 | 媒體 | 類型 |
| --- | --- | --- |
| `CNA_FINANCE` | 中央通訊社財經 | RSS |
| `LTN_BUSINESS` | 自由時報財經 | RSS |

來源與原文主機採白名單；回應上限 2,000,000 位元組；轉向到未允許主機即視為失敗。RSS 只有標題與前言，不當作全文。

### 資料保存

| 項目 | 規則 |
| --- | --- |
| `market_news_captures` | 全市場 scope 為 `TW_STOCK_MARKET`，只追加，不覆寫個股新聞 |
| `market-news-1.0` | 保留 `snapshot_id`、cutoff、`window_start`、captures、labels、synthesis、`source_approvals`、內容雜湊 |
| `market_news_source_reviews` | 只追加；保存來源、核對結果、用途、核對時間、保存時間、原始規範、內容雜湊 |

### 標籤

每則含 `item_id`、`canonical_content_id`、`relevance`、`stance`、`reason`（中文理由）。

| 欄位 | 允許值 |
| --- | --- |
| `relevance` | `market`、`company`、`unrelated`、`ambiguous` |
| `stance` | `positive`、`negative`、`neutral`、`mixed` |

- 必須覆蓋查詢視窗內全部項目，不得少標、重標或改內容識別；同一內容的重複標籤須一致。
- 只有 `market` 項目能支持全市場發現。

### 綜合判讀

- `synthesis` 含 `outlook`（`positive`／`neutral`／`negative`／`unknown`）與 `findings`（`text`、`evidence_ids`）。
- 不以篇數、固定權重或股價漲跌公式決定方向；程式只核對引用與時鐘。
- 非 `unknown` 的結論必須有 `market` 項目與 findings。

### 去重

- 聚合前依 canonical content ID 去重，由「正規化標題＋發布日」建立。
- 不同標題描述同一事件時仍可能有多列，不能用列數加強結論。

### 正式輸出門檻

全部成立才輸出 `status=available`：

| 條件 | 要求 |
| --- | --- |
| 樣本 | 至少 5 個市場獨立內容 |
| 來源 | 至少 2 個獨立媒體 |
| 覆蓋 | 查詢範圍內全部項目都有標籤 |
| 取得 | 沒有來源取得失敗 |
| 授權 | 相關來源皆為 `approved`、`usage_scope=competition_research` 且有授權依據 |
| 方向 | 綜合判讀 `outlook` 不是 `unknown` |

未達門檻時 `status=unavailable`、`outlook=unknown`、`findings=[]`；原判讀只存於 `diagnostic_outlook`／`diagnostic_findings`，不得複製成正式結論。

### 來源使用核對

| 函式 | 作用 |
| --- | --- |
| `MarketNewsRepository.save_source_reviews` | 保存核對依據與時間 |
| `MarketNewsRepository.load_source_reviews` | 只讀 cutoff 前已保存的版本 |
| `apply_database_source_reviews` | 把最新且當時可得的核對套進 market-pack |

- 不修改新聞、標籤或結論，也不會把 `unverified` 自動升為 `approved`。
- 核准狀態只能來自外部來源的真實授權核對；測試 fixture 的核准與真實授權無關。

### 接入第三位分析師

- `DecisionInputBuilder` 支援選配 `market_news_input={bundle,result}`。
- 輸入驗證與報告驗證都從 captures + labels 重建 result，並驗證相同 Snapshot／cutoff。
- 缺少接入時使用原降級通道。
- brief 含全量 market_news，正式 `market_sentiment` 只出現一次；每日 pipeline 不需第四位分析師。

### 報告用語

| 情況 | 中文寫法 |
| --- | --- |
| 只有使用範圍未確認 | 「來源使用範圍尚未確認，正式結果暫不提供」 |
| 同時缺樣本、來源或完整輸入 | 「資料不足」 |

## 使用方式

### 1. 補抓並保存

```bash
.venv/bin/python cli/market_news.py capture \
  --window-start 2026-10-05T00:00:00+08:00 \
  --database var/etf_agent.db \
  --output-root artifacts/market_news_captures
```

### 2. 產生判讀

分析師讀取 prepared 全量項目，交付完整 labels、synthesis 與真實 `source_approvals`，與 captures 組成有內容雜湊的 market-pack。

### 3. 接入同 Snapshot／cutoff 的資料包

```bash
.venv/bin/python cli/market_news.py integrate \
  --decision-input PATH_TO_DECISION_INPUT \
  --market-pack PATH_TO_MARKET_PACK \
  --output-root artifacts/market_news_research \
  --database var/etf_agent.db
```

- `--database` 選配，用於讀取 cutoff 前已保存的來源使用核對。
- 結果另存於 `--output-root` 下 `decision_inputs/` 的不可變 run，不覆蓋既有決策輸入。
- 驗證失敗會以 `接入驗證失敗` 中止。

## 限制與注意事項

- RSS 只保留最近項目，不能單靠 RSS 宣稱涵蓋整段歷史；歷史補抓見[缺日補抓](market_news_history.md)。
- 中央社與自由時報的官方 RSS 條款限制非商業用途，本專案的比賽使用權尚未確認，因此真實資料的正式輸出為 `unavailable`，診斷結果不交給交易。
- 資料庫中標示 `approved` 的紀錄不涵蓋中央社、自由時報或 FinMind 的比賽研究核准，不能代表整體台股。
- 診斷看法是新聞材料的研究推論，不是投資人民調，也不驗證投資效果。
- 沒有排程，不會每日自動補抓與標籤。
- 原有個股 PerceptionDataBundle 路徑維持兼容。

來源規範：[中央通訊社 RSS](https://www.cna.com.tw/about/rss.aspx)、[自由時報 RSS](https://service.ltn.com.tw/RSS)

## 尚未實作

- 中央社、自由時報與 FinMind 新聞的比賽研究使用權核准。
- 完整歷史新聞庫（含當時內容版本與可得時間證據）。
- 每日自動補抓與標籤的排程。
