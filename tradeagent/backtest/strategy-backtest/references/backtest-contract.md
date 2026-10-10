# P7 B0～B2 回測契約

`BacktestRequest` 固定 Asia/Taipei 的 08:55 decision cutoff、版本化交易日曆、初始 settled／unsettled cash、整張持股、資料 manifest 與成交假設。`data_manifest.mode` 為 `fixture`、`exploratory` 或 `historical_verified`；缺少歷史 available_at 證明時不能使用最後一種。

每日輸入以交易日為 key，必須提供：

- `research_versions`：每版有 artifact_id、available_at、payload 與 payload 的 content_sha256；Provider 只選 cutoff 前最後可用版。`historical_verified` 另強制 evidence_id、source、content_as_of、published_at、fetched_at，缺一即停止。
- `decision`：保存的 DecisionResult，必須有正確 content_sha256。僅 `approved` 可執行 orders；`rejected` 與 `no_trade` 仍保留帳務日。
- `execution_market`：`price_basis=unadjusted`、不晚於 `execution_at` 的 available_at、各代號 execution_price、tradable、available_lots。分析用還原價不得放入此欄位。
- `close_market`：`price_basis=unadjusted`、不早於 `close_at` 的 available_at，以及收盤 `quotes`；收盤估值只能使用其中未還原價格。
- 選配 `corporate_actions`：現金股息或拆併股，保存生效／付款資訊。

Ledger 在賣出日將淨賣款記為 unsettled cash，到版本化 settlement_date 才轉成 settled cash。買進只能使用 settled cash，或明確允許的同日賣款；買賣、費稅與未成交必須以一張 1,000 股記錄。每日 Result 保存整份 execution、ledger snapshot 與 content_sha256。

`historical_verified` 每日還必須提供完整 `decision_run`（bundle、policy、momentum、debate、intent、proposal、scenario、guard、risk_review、history），並以既有 `DecisionResultValidator` 重建保存的決策。fixture／exploratory 模式不具備正式績效或可交易結論。

CLI 依序使用 `validate-request`、`inspect-coverage`、`replay`、`validate-run`；通過重播後可用 `save-run` 保存 request、每日輸入與 run 的不可變 manifest，並以 `build-report` 產生帳務驗收 JSON／Markdown。
