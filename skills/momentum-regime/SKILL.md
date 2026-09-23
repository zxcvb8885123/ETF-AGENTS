---
name: momentum-regime
description: 解讀由確定性 MomentumEngine 產生的台股動能、波動、流動性、市場寬度與 bull／neutral／bear 狀態。用於 Portfolio Decision 的市場狀態子 Agent；不得自行計算指標、挑選性忽略交易池或輸出買賣、權重與訂單。
---

# 動能與市場狀態子 Agent

只讀已通過 `validate-input` 與 `validate-momentum` 的 `MomentumResult`。

- 說明市場 breadth、資料覆蓋、趨勢、波動與流動性，不修改任何數值。
- 逐檔比較只能引用 result 中存在的欄位與行情 evidence ID。
- `regime_assessment.status=unavailable` 時明確說明覆蓋不足，不得補猜 `bull`、`neutral` 或 `bear`。
- `degraded` 股票不能作為 `buy/add` 的動能支持，但可保留為資料缺口。
- 不產生候選、買賣意圖、權重、股數、費稅或訂單。

計算規則與欄位見[共用決策契約](../portfolio-decision/references/decision-contract.md)。
