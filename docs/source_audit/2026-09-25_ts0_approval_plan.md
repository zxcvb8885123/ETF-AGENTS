# TS0 交易狀態來源核准行動計劃

日期：2026-09-25。狀態：**部分來源對照完成；正式來源核准 0，正式決策維持 blocked**。本計劃承接[交易狀態 M1 計劃](../trading_status_m1_plan.md)與[當日來源稽核](2026-09-25_findings.md)，處理 150 檔股票在指定交易時段的可交易性。ETF 外部持股權重不在此閘門內。

## 已完成的查證

- 固定分母：`data/official_universe.csv` 為上市 100、上櫃 50 檔；9/24 與 9/25 兩次官方端點回應、時間、SHA-256 和 12 檔樣本已封存。
- [四份 TPEx 政府開放資料](2026-09-25_ogd_crosscheck.json)的原始 CSV 與 OpenAPI JSON 已逐欄對照：變更／分盤／管理 21／21 列、歷史停復牌 362／362 列完全相符；注意 CSV 20 列對 JSON 38 列、處置 CSV 17 列對 JSON 23 列，按 CSV 含有的公告日期篩選 JSON 後，對應欄位及列重數相符。
- 同一份[對照紀錄](2026-09-25_ogd_crosscheck.json)亦新增四份 TWSE 政府開放 CSV：[開休市日期](https://data.gov.tw/dataset/11761) 27／27 列、[暫停交易](https://data.gov.tw/dataset/11677) 1／1 列、[證券變更交易](https://data.gov.tw/dataset/11760) 10／10 列均逐欄相符；[集中市場處置](https://data.gov.tw/dataset/29082) 8／8 列在移除長文字排版空白後逐欄相符。暫停交易唯一列是已恢復的舊事件，不能由此推定指定交易日全市場無停牌。
- 政府資料開放平臺對上述四份 CSV 資源分別標示[變更／分盤／管理](https://data.gov.tw/dataset/11736)、[注意](https://data.gov.tw/dataset/11395)、[處置](https://data.gov.tw/dataset/11396)、[歷史停復牌](https://data.gov.tw/dataset/48665)的授權與更新頻率。這是**所列 CSV 資源**的授權證據；尚不能擴張為 OpenAPI JSON 或其他端點的核准。
- 9/25 與 9/28 兩市場休市；9/25 取得 9/24 資料不能被當作 9/25 可交易狀態。下一次交易日測試須先核對兩市場實際開市。
- 已加入離線重算工具，對八組封存 CSV／JSON 驗證原始 CSV 雜湊、列數、日期分布及對應欄位。處置列分開解析公告日與暫定生效區間：本次 TPEx 17 列、TWSE 8 列的生效起日均晚於公告日且文字提及順延。TPEx 歷史停復牌是 181 列暫停事件與 181 列恢復事件，不能依列數推出當下停牌名單。所有結果仍標記 `available_at_verified=false`、`matched_candidate_only`。
- 已用 9/25 13:27 封存版本執行 `normalize_trading_status_candidates.py`，八份 CSV 對固定 150 檔共命中 6 個不同股票代號；只記錄候選事實，不產生正式 `TradabilityAssessment`。上櫃歷史停復牌 362 列中有 332 列為五、六碼非本競賽股票代號，不能把原始總列數當作股票覆蓋數。`trading-status-policy-2` 將注意資料改為選配提示，避免把資訊警示誤當必要交易許可來源。

本機保有稽核原檔時，可執行：

```bash
.venv/bin/python scripts/verify_ogd_crosscheck.py \
  --report docs/source_audit/2026-09-25_ogd_crosscheck.json \
  --audit-dir artifacts/source-audit/2026-09-25
```

原始 CSV／JSON 在忽略版控的本機稽核目錄；若未保留原檔，驗證會失敗，不能只根據報告檔宣稱已重算。

## 來源逐項判定

| 範圍 | 目前證據 | 未解問題／下一個動作 | 結論 |
| --- | --- | --- | --- |
| TWSE 開休市 | [政府開放資料集 11761](https://data.gov.tw/dataset/11761)的 27 列 CSV／JSON 相符，列最新年度與授權 | 固定版本、雙市場時段、臨時休市與修訂處理；不能把最新一版回填歷史 | candidate |
| TWSE 停復牌 | [政府開放資料集 11677](https://data.gov.tw/dataset/11677)的 1 列 CSV／JSON 相符，但僅為已恢復的舊事件 | 核對停復牌是否完整現況、空列與取消／更正、生效時點 | candidate |
| TWSE 變更／分盤 | `TWT85U` 與[政府開放資料集 11760](https://data.gov.tw/dataset/11760) CSV 本次 10 列欄位相符 | 確認完整現況、分盤及其他不支援交易方式的有效期 | candidate |
| TWSE 注意／處置 | `notice` 有空代號佔位列；`punish` 與[政府開放資料集 29082](https://data.gov.tw/dataset/29082) CSV 本次 8 列排版空白正規化後相符 | 注意來源與授權、空列、公告日、未來生效及順延／更正仍待核實 | candidate |
| TPEx 變更／分盤／管理／停止 | CSV／JSON 在本次 21 列全部對應欄位相符 | 確認是完整當日現況或增量、何時發布完成、空欄與取消語意；優先考慮有明示授權的 CSV 資源 | candidate |
| TPEx 注意 | 本次 CSV 20 列與同日期 JSON 20 列相符；JSON 另含前一日 18 列 | 依公告日篩選；確認更新完成時間。注意標記依政策提示，不自行當作停牌 | candidate |
| TPEx 處置 | 本次 CSV 17 列與同日期 JSON 17 列相符；JSON 另含 9/24 六列 | 解析生效區間及休市／停牌順延，不能把公告日當生效日 | candidate |
| TPEx 當日／歷史停復牌 | 歷史 CSV／JSON 362 列對應欄位相符；當日 JSON 有空白佔位列 | 核實歷史範圍、未恢復狀態、當日空列語意與更新時間，兩份資料互相對帳 | candidate |

[資料集 11736](https://data.gov.tw/dataset/11736)描述每日有變更交易、分盤或管理狀態的上櫃股票；[資料集 11396](https://data.gov.tw/dataset/11396)描述每日**公告**的處置資訊。候選正規化依這兩種不同資料語意保留內容日與公告日，未將它們等同為完整指定交易時段的可交易狀態。

## 接下來的執行順序

1. **完成來源文件核對。** 為每個必要市場 × 限制類別保存資源 URL、官方授權、欄位／查詢語意、更新完成時間及核准結論。政府開放 CSV 可作授權候選路徑；JSON 若無等價證據，不能沿用 CSV 的核准。未證明的欄位保持 `candidate`，不得直接修改 `config/data_sources.json` 的正式 `sources`。
2. **補正式確定性映射。** 本次稽核工具已解析 ROC 日期及處置公告／暫定生效區間，並辨識歷史停復牌事件列；但尚未建立可送入 Guard 的正式映射。針對獲准資源再處理 ROC 時間、空白佔位列、重複列、取消／更正與跨日資料。現有 JSON parser 需要 ISO 時間欄位，不能把中文 CSV 原欄位直接塞入正式 `TradingStatusRecord`。保存 raw bytes、擷取時間及內容雜湊；來源失敗或日期不明輸出 `unknown`。
3. **在實際交易日重測。** 先核實兩市場開市，再以固定 12 檔於開盤前、官方公布後及 D-Plan 決策截止前後保留原始版本；比對內容日期、HTTP 時間、資料更正與前後差異。由已知版本判斷指定下一交易時段，不以現在抓到的「最新」回填過去 cutoff。
4. **執行 TS5。** 固定 150 檔 × 各市場必要類別分母，逐檔產生同一 Snapshot／cutoff 的 `TradingStatusBundle` 與 `TradabilityAssessment`，分開列 `allowed`、`blocked`、`unknown`、不適用及缺口。只把已核准且時間相符的來源送進正式 Guard；任何必要類別未覆蓋，正式 D-Plan 繼續 blocked。
5. **接正式決策。** TS5 通過後，仍須核對帳戶、規則、策略說明和研究／決策 artifact，再跑 D-Plan 匯出與本地驗證。來源核准本身不代表可送件，也不需要 FinMind Token 或 ETF 權重。

## 放行條件

只有當每個必要來源都有可保存的授權證據、可重建的完整／增量語意、明確發布與生效時間、空回應及更正規則，且 150 檔固定分母在同一 cutoff 完整驗證時，才可把對應來源設為 `approved`。單次 CSV／JSON 欄位相符、HTTP 200 或股票未出現在名單中，都不足以放行。任何條件未滿足須留下具體原因與 `unknown`，不能以人工推測、行情存在或 Risk Agent 判斷取代。

交易日每個觀測時段以 `.venv/bin/python scripts/capture_trading_status_candidates.py` 建立獨立封存。工具只請求八份已列入政府開放資料集的 CSV，保存原始回應、抓取起訖時間、HTTP 日期、ETag、SHA-256 及列數；CSV 無效時仍保存原始位元組並標記失敗。`captured_candidate_only` 只表示成功封存，**不是**資料已更新完成、覆蓋完整或來源已核准。時段間比對與正式語意核准仍按上述第 1～4 步執行。

候選事實可用下列指令對每個封存版本重算；輸出明列 `approved_sources=0`、各來源非股票列與交易池命中，不能作為 Guard 輸入：

```bash
.venv/bin/python scripts/normalize_trading_status_candidates.py \
  --manifest artifacts/source-audit/captures/20260925T052714327866Z/manifest.json \
  --output artifacts/source-audit/captures/20260925T052714327866Z/candidate-facts.json
```

2026-09-25 13:27（台北時間）試跑成功，封存於 `artifacts/source-audit/captures/20260925T052714327866Z/`；八份內容雜湊與當日稍早的基準完全相同，屬休市日鏈路驗證。已設定本任務在預定下個交易日 2026-09-29 的 08:30、10:30、12:30、14:30（台北時間）分時執行；每次先核對兩市場實際開市，最後更新稽核並停用排程。排程執行與來源核准是兩件事。
