# 研究方法來源與本專案取捨

本 Skill 沒有直接依賴或安裝 Anthropic 的模型 API、MCP 資料商或美股工作流。它只採用下列 Apache-2.0 專案中的通用研究設計，再改寫成台股官方交易池、時間點 Snapshot 與本專案契約：

- `earnings-analysis`：重點不是財報摘要，而是新資訊、actual 相對 expectation／guidance 的差異、分部與利潤率驅動，以及更新後的 thesis。
- `catalyst-calendar`：催化事件分類、影響層級、事件日期與事後結果保存。
- `thesis-tracker`：以 3～5 個可否證支柱追蹤 thesis；新資料需標明 strengthen、weaken 或 neutralize，支持與反證同等處理。
- `idea-generation`：篩選只是研究起點；必須說明市場可能忽略什麼、催化劑與主要風險。

上游專案：<https://github.com/anthropics/financial-services>

授權：Apache License 2.0。上游文件本身也明確要求金融研究結果交由合格人員審核，不自動執行交易。本專案進一步要求所有研究數字必須來自 `ResearchSnapshot`，並通過本機 validator 才能交給下游。

## TradingAgents 論文

本機參考論文：`/Users/apollo/paper/stock/TradingAgents Multi-Agents LLM Financial TradingFramework.pdf.pdf`。

採用的設計：

- 不把新聞／基本面／技術面混成一次提示；先形成專業分析輸入，再進入研究裁決。
- 研究階段明確建立 bullish 與 bearish 觀點，最後由 facilitator／judge 保存結構化結論。
- 用結構化報告傳遞狀態，只在需要權衡時使用自然語言辯論，減少多 Agent 逐層轉述造成的資訊失真。
- 研究後仍需交易與風險角色；事件 Agent 不直接產生訂單。

未直接照搬的部分：

- 第一版建立 Fact／Bull／Bear／Adjudicator 子 Agent 角色，但不部署常駐服務，也不要求專案串接 LLM API；由 Codex／Claude 執行一次性子 Agent，並以 packet 契約保存結果。
- 論文回測期間短、標的有限，而且作者也揭露呼叫成本與樣本限制；其績效不能當成此專案架構有效的證明。
- 本專案增加 cutoff、正式 evidence ID、確定性數值重算與 fail-closed validator，避免事後資料或無來源推論進入候選。
