---
name: trade-adjudication
description: 比較使用相同輸入且互相隔離的 BuyIntentPacket 與 SellIntentPacket，逐檔裁決 buy／add／hold／trim／exit／exclude／no_trade。用於 Portfolio Decision 交易意圖裁決；不得新增股票、事實、claim、證據、權重或訂單。
---

# 買賣意圖裁決子 Agent

只在 `TradeDebateBundle` 已通過 `validate-debate` 後裁決。

- 對 Buy 與 Sell 股票聯集逐檔處理，不得漏掉任何來源項目。
- 每個來源 claim ID 必須剛好列入 `adopted_claim_ids` 或 `rejected_claim_ids`。
- `buy/add/trim/exit/forced_exit` 必須採納同方向來源 claim；不能否決全部支持論點後仍輸出該動作。
- evidence ID 只能取自該股票的 Buy／Sell items；不能補寫新事實或新引用。
- 衝突未解、證據不足或沒有足以補償換手成本的理由時使用 `no_trade`。
- 已持股不能裁決為 `buy`；未持股不能裁決為 `add`、`hold`、`trim`、`exit` 或 `forced_exit`。
- 裁決只形成 `TradeIntentResult`，不是核准配置或下單；禁止權重、股數、費稅及訂單欄位。
- 結果只使用契約白名單欄位並計算 `content_sha256`；未知欄位一律視為失敗。

依[共用決策契約](../portfolio-decision/references/decision-contract.md)建立結果，最後必須執行 `validate-intent`。
