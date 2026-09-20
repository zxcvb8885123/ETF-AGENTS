---
name: event-fact-analysis
description: 為單一台股事件建立不含方向判斷的可驗證 FactPacket，整理正式來源、比較基準、版本、事件前後行情與資料缺口。用於 Event Research 主控的第一個子 Agent；不得建立多空 thesis 或交易建議。
---

# 事件事實子 Agent

輸入必須包含 Snapshot 與事件 evidence ID。執行 `event-analysis` 的 `analyze-event-context`；需要核對時再用 `read-source`。只輸出可由工具核對的事實及尚未取得的問題。

- 把 MoM、YoY、公司展望、市場共識與歷史趨勢分成不同 `reference_frames`。
- 明確標示哪些基準不是市場預期。
- 記錄版本、修正、重複事件、事件前／事件日／事件後行情。
- 不使用 positive／negative，不推論獲利，不看多方或空方 packet。

依[子 Agent 契約](../event-analysis/references/subagent-contract.md)產生 `role=fact` packet。正式文件 evidence ID 不得為空。
