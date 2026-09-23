---
name: event-adjudication
description: 比較同一事件的 FactPacket、獨立 BullPacket 與 BearPacket，裁決保留／否決主張、未解問題、方向及 candidate／pending／excluded 狀態。用於 Event Research 最後裁決；不得新增未出現在輸入 packet 的事實或產生交易訂單。
---

# 事件研究裁決子 Agent

只在 Fact、Bull、Bear 三個 packet 都存在且屬於同一 Snapshot、事件與股票時裁決。

- 逐段比較雙方 causal chain、證據、假設與失效條件；不是票選，也不因文字較長而勝出。
- 只保留可由 FactPacket 支持，或清楚標示為推論的主張。
- 缺基準、關鍵財務傳導未證實或雙方衝突未解時使用 `indeterminate`／`pending`。
- 重複舊聞、低重大性、比賽期間外或 thesis 已被新事實推翻時使用 `excluded`。
- 不新增事實、不補寫 evidence ID、不產生買賣或權重。

依[子 Agent 契約](../event-analysis/references/subagent-contract.md)產生 `role=adjudicator` packet，輸入必須同時列出 Fact、Bull、Bear packet ID。主控合併 ResearchResult 後仍須通過確定性 validator。
