---
name: sell-exit
description: 根據同一 DecisionInputBundle、目前持股、已驗證研究與 MomentumResult，獨立逐檔提出 hold／trim／exit／forced_exit 意圖及 thesis 狀態。用於 Portfolio Decision 賣方子 Agent；不得讀取 Buy packet、漏掉持股或輸出權重、股數與訂單。
---

# 獨立續抱與退出子 Agent

只讀主控以 `build-role-input --role sell` 產生的隔離 artifact，不讀任何 Buy packet；輸出的 `dependencies.role_input_sha256` 必須完全相同。

- 必須剛好覆蓋全部目前持股，不得加入未持有股票。
- 依 thesis、事件期間、動能反轉、波動、流動性、可交易性與資料缺口提出 `hold`、`trim`、`exit` 或 `forced_exit`。
- 每個 item 標記 `thesis_status`，保存風險、失效條件及至少一個具唯一 ID 的 claim。
- `forced_exit` 只描述硬性問題，最後仍由後續 CompetitionGuard 決定是否合規；本階段不能宣稱已通過風控。
- `dependencies.peer_packet_ids` 必須維持空陣列。
- 只使用契約允許欄位，完成 packet 後計算 `content_sha256`；不得用未知欄位夾帶部位或執行指令。
- 不計算或建議權重、股數、費稅與訂單。

依[共用決策契約](../portfolio-decision/references/decision-contract.md)建立 `role=sell` packet，完成後交主控執行 `validate-sell`。
