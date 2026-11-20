# 復盤檢索（Postmortem Search）計畫

狀態：2026-11-20 草案，**尚未實作**。本文件不是現行行為。

## 1. 要解決什麼

比賽結束後（或比賽期間人工復盤時），人想問：「過去類似的事件後來發生什麼事？」「這個論點在哪幾天出現過？」目前只能手翻 `artifacts/`。

這是**旁觀者工具**，不進每日決策鏈。每日鏈不用 RAG 的理由見 [`token_efficiency_plan.md`](token_efficiency_plan.md#6-向量索引向量資料庫rag適不適用)，本計畫不推翻它。

## 2. 邊界（全部是硬限制）

- 放在獨立套件 `src/etf_agent/postmortem_search/`。`decision/`、`automation/`、`strategy_reflection/` 不得匯入它；加一個測試檢查匯入邊界（做法照 `strategy_reflection`）。
- **不呼叫任何模型 API，也不做向量嵌入**（`AGENTS.md`：不直接在專案內串接模型 API）。第一版只用字詞檢索。
- 輸出每一筆都標 `citable=false`、`evidence_grade: replay_reference_only`：只供人閱讀，不是證據，不能餵回每日鏈任何 Agent 的 brief。
- 查詢必須帶 `as_of`（含時區）。只回傳 `available_at <= as_of` 的項目，避免復盤時把當時拿不到的資料當成當時的資訊。缺少 `available_at` 的項目一律不回傳，不填假值。
- 唯讀：不修改 `artifacts/`、不修改 `config/`。索引檔放 `var/postmortem_search/`，可刪除重建。
- 不改契約、不改版本閘門、不動 content hash。

## 3. 技術選擇：SQLite FTS5 trigram

已在本機 `.venv`（SQLite 3.53.1）實測：`tokenize='trigram'` 可用，中文子字串「重大訊息」能命中「台積電公告重大訊息」。

為什麼不用向量：

- 不需新增相依（`sqlite3` 是標準庫），不動 `requirements.lock`、不用重建 Docker 映像。
- 結果是確定性的、可重算；能回答「為什麼這則被找到」（字串命中）。
- trigram 需要查詢至少 3 個字元；較短的詞改用 `LIKE`，在計畫裡註明限制。

若將來要語意檢索（嵌入），列為**另一項待使用者核准的計畫**，須先解決：模型檔雜湊釘死、本機模型相依、Docker 重建、安全審查。本計畫不含。

## 4. 索引哪些資料（第一階段）

只索引已封存、有時間欄位的文字：

| 來源 | 用途 |
| --- | --- |
| 事件分析師輸出與引用的公告標題 | 查過去同類事件 |
| 交易 Agent 的理由與重估條件 | 查某論點出現過哪幾天 |
| 多空 claims | 對照當時的多空論點與後來走勢 |
| 策略教訓與論點檢視 | 復盤用背景 |

每筆保存：`run_id`、`snapshot_id`、`decision_cutoff`、`available_at`、來源檔路徑、內容雜湊。索引建立時重新計算雜湊並與來源比對，不符則該筆不入索引並列入報告。

## 5. 介面

- CLI：`python -m etf_agent.postmortem_search build`、`... query --as-of <含時區時間> "<字詞>"`。
- 輸出：每筆含來源路徑、時間、命中片段、`citable=false`。不產生結論、不產生買賣建議。
- 結果數量有上限，超過時明說「還有 N 筆未顯示」，不靜默截斷。

## 6. 測試（unittest 風格）

- `available_at` 晚於 `as_of`、缺少時區、缺少 `available_at`：一律不回傳或拒絕。
- 來源檔被改動（雜湊不符）：不入索引。
- 匯入邊界：`decision/`、`automation/`、`strategy_reflection/` 不得匯入本套件。
- 輸出每筆 `citable=false`。
- 短於 3 字元的查詢走 `LIKE`，結果一致。
- 重建索引兩次，結果相同。

## 7. 時程與做不做

- 比賽 2026-11-27 結束；這件事**不影響比賽**，建議比賽結束後再實作，避免佔用比賽期間的修改風險。
- 若使用者要提前做，也只在不被排程使用的 worktree 中開發。

## 8. 還沒確認的事

- 各封存檔的實際欄位與 `available_at` 是否齊全，尚未逐一核對；實作第一步是盤點，缺欄位的來源先不收。
- 復盤真正會問的問題類型尚未收集，第一版查詢介面可能需要調整。
