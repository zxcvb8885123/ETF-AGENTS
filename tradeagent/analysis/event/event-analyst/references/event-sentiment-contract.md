# 公司事件與整體市場情緒交付格式

每日流程使用 AnalystReport(event) 2.2。模型只輸出 items，每檔包含 symbol、outlook、event_outlook、findings、data_gaps、events。

outlook 與 event_outlook 必須相同，僅表示該公司事件的結果，使用 positive／neutral／negative／unknown。市場氣氛不能替代事件引用。findings 包含 finding_id、text、evidence_ids。events 包含 evidence_id、materiality（high／medium／low／unknown）、summary。每則輸入事件剛好分級一次，不得漏掉。

程式在報告頂層加入一次 market_sentiment，包含 scope、snapshot_id、decision_cutoff、status、outlook、findings、data_gaps。公司項目不得包含 sentiment 或 market_sentiment；模型不得輸出或覆寫全市場通道。

原本個股情緒 Provider 只有公司維度；新增市場 RSS Provider 另用 market_news_input 接入。缺少市場成對輸入或未通過使用權與樣本檢查時，market_sentiment 為 unavailable／unknown。即使個股情緒來源已核准，也不能改成全市場來源。大盤成交、指數及法人買賣可另補抓為交易背景，不能替代這個缺口。

舊 2.1 報告保留原始重建驗證，仍有逐股 sentiment；每日新報告使用 2.2，不把舊稿改寫成新版。舊四份 2.0 報告仍只沿用原始封存路徑。

參見 [全市場與公司事件分工](../../../../../docs/event_market_scope.md)。Python schema 與 Validator 為最終契約。

### 全市場新聞資料包接入

DecisionInputBundle 可包含 market_news_input 的 bundle/result 成對資料。market_sentiment 由來源回應、完整標籤及綜合 findings 重建，不能改寫已驗證結果。RSS Provider 已接入；真實來源的比賽使用權尚未確認，所以本次正式結果 unavailable，新聞診斷結果只供研究測試。詳細資料契約見 [接入說明](../../../../../docs/market_news_integration.md)。

市場新聞1.1資料包可包含RSS與歷史原頁。原頁必須重建發布／取得時間與標題摘要；同媒體不同管道不增加來源數。舊1.0封存結果維持原重建，不改寫。1.1只交代具體查詢範圍，不固定加入「尚不能涵蓋全部歷史」警語。
