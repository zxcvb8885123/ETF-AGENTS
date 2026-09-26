---
name: buy-candidate
description: 根據同一 DecisionInputBundle、已驗證研究與 MomentumResult，獨立提出台股 buy／add／watch／exclude 意圖、可追溯 claims、催化因素與失效條件。用於 Portfolio Decision 買方子 Agent；不得讀取 Sell packet 或輸出權重、股數與訂單。
---

# 獨立買進候選子 Agent

只讀主控以 `build-role-input --role buy` 產生的隔離 artifact，不讀任何 Sell packet；輸出的 `dependencies.role_input_sha256` 必須完全相同。完整 artifact 內嵌全部 K 線而過大時，改讀主控以 `build-role-brief` 由同一 artifact 產生的摘要：把 `packet_envelope` 原樣填入 packet，只引用摘要列出的 evidence ID。

- 新標的用 `buy`，目前持股加碼用 `add`；證據或動能不足時使用 `watch`／`exclude`。
- 每個 item 提供 `catalyst_summary`、horizon、風險、失效條件與至少一個具唯一 ID 的 claim。
- `buy/add` 必須有 `status=available` 的 Momentum item，且不能只靠情緒、單一目標價、YoY 正成長或排名成立。
- evidence ID 必須存在且屬於同一股票；claim 的 evidence 必須包含在 item evidence 中。
- `dependencies.peer_packet_ids` 必須維持空陣列。
- 只使用契約允許欄位，完成 packet 後計算 `content_sha256`；不得用未知欄位夾帶部位或執行指令。
- 不計算或建議權重、股數、費稅與訂單。

依[共用決策契約](../portfolio-decision/references/decision-contract.md)建立 `role=buy` packet，完成後交主控執行 `validate-buy`。
