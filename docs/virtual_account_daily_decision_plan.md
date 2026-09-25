# 十億虛擬帳戶與每日買賣決策接入計畫

狀態：VA1～VA3 fixture 工具鏈已完成（2026-09-23）；VA4 的 DailyReport 帳戶呈現及 workflow 對已封存 prepare-day run 的強制核對已實作、待驗收；workflow 自動 prepare/apply 與 VA5 多日正式資料驗收待完成。使用者確認競賽使用虛擬帳戶，初始本金為新臺幣 1,000,000,000 元。此計畫取代「必須先取得真實券商／平台帳戶匯出檔」作為每日決策的前置假設；既有外部帳戶匯入工具保留為日後有平台結算檔時的對帳支線。

## 目標與現況

第一個交易日由 `config/competition_rules.json` 的 `initial_capital_twd` 建立空倉虛擬帳戶；以後每次決策只讀前一個已驗證、已封存的帳本狀態。訂單是決策提案，不等同已成交；只有模擬成交或可驗證的外部成交紀錄，才能改變持倉與現金。報告須分清「買賣建議」「模擬成交」「帳戶實際狀態」。

已具備：10 億初始本金設定、Portfolio Decision 的 AccountSnapshot 契約與買賣／配置／風控工具、回測與虛擬帳戶共用的 `etf_agent.ledger`（`AccountLedger`／`ExecutionSimulator`）多日 fixture 實作，以及 Research Report → Decision run → DailyReport 的離線接線。

尚缺：report workflow 自動執行 `prepare-day`／`apply-decision`、可追溯的市場 Provider／正式規則來源、多日完整操作驗收及固定版本前向驗證。正式模式接入既有帳本時，已強制驗證當前 prepare-day run、Snapshot／cutoff 與 Decision AccountSnapshot 一致。交易狀態、核實的規則版本仍是正式決策前置缺口；ETF 基準持股僅供選配比較。

## 每日流程與帳務規則

```text
首次初始化：10 億設定＋版本／雜湊 → 空倉開帳 → 不可變 VirtualAccountState
第 N 日決策前：驗證前一狀態 → 到期交割／已知公司行動 → 以 cutoff 可得行情估值
               → AccountSnapshot → Portfolio Decision（買／賣、風控）→ 決策報告
第 N 日決策後：通過驗證的 DecisionResult＋當時可得的模擬成交資料
               → 成交／未成交明細、費稅、現金／持倉更新 → 日終 NAV → 新的不可變狀態
次日：只從前一已完成狀態續接；不得重新領取 10 億本金
```

交易建議沒有成交資料時保持 `awaiting_execution`，帳本不動；拒絕決策和 `no_trade` 也須封存狀態與原因。模擬成交要保留價格來源、`available_at`、成交時間、成交張數、滑價、手續費、交易稅、未成交數量與版本化假設。不得用決策截止後才可得的資料回填當時的決策；成交與日終價格可以在各自的後續時間點使用，但只能影響後續帳戶狀態。

虛擬帳戶的權威來源是已驗證的前一帳本狀態與逐筆異動，不是每天讀初始本金。若競賽平台日後提供虛擬帳戶結算檔，先以既有匯入工具對帳；差異未解決時停止續接，不以檔案直接覆寫歷史帳本。第一版不自動送單或向平台送件。

## 契約與封存

| 產物 | 必要內容 |
| --- | --- |
| `VirtualAccountGenesis` | account ID、初始本金 10 億、幣別、有效起點、設定檔版本／SHA-256、唯一 genesis ID |
| `VirtualAccountState` | 父狀態 ID／雜湊、截至時間、已交割現金、未交割應收與應付明細、持倉股數／成本、NAV、來源與版本 |
| `AccountTransition` | 來源 Decision run、決策狀態、模擬成交／未成交、費稅、交割及公司行動引用、前後帳務恆等式 |
| `AccountSnapshot` | 經驗證帳本在本次 cutoff 的持倉、現金、NAV、`available_at` 與來源證據 ID；須與 DecisionInputBundle 的 Snapshot／cutoff 一致 |

金額以 Decimal 計算；保留來源精度，只在明定的費稅與結算邊界進位。虛擬帳本可保留未交割應收／應付，若現行 DecisionInputBundle 不能無損表示，先阻擋決策並明確升版契約與配置、情境、Guard 的購買力計算。帳本不得把賣出應收當成已交割現金，也不得因空倉或缺價格跳過 NAV 驗證。

每個狀態與異動保存 manifest、內容雜湊及父子引用。相同執行鍵與相同輸入只重用原結果；同鍵不同內容、斷鏈、重複套用成交或重新初始化同一帳戶均拒絕。`latest` 只能指向完整驗證的狀態，部分寫入不可見。帳戶主線與 fixture 回測 run 分目錄，避免把測試帳務冒充本次競賽帳戶。

## 執行順序

| 階段 | 工作與交付 | 驗收條件 |
| --- | --- | --- |
| VA0 規則與起點固定 | 核對 10 億設定的來源、幣別、生效時間、帳戶起始日與成交／交割假設；封存版本 | 初始資金只在唯一 genesis 使用；缺規則依據時只允許標明為內部模擬 |
| VA1 虛擬帳本契約與 Repository | 建立 genesis、狀態、異動與 manifest；CLI 提供 `init`、`status`、`verify` | 空倉＝現金／NAV 10 億；重複初始化、改寫父狀態、缺檔及時間倒退均拒絕 |
| VA2 決策前狀態接入 | 重用或抽取回測的確定性交割、公司行動與估值邏輯；生成同 cutoff AccountSnapshot 並進入 DecisionInputBundle | 第 2 日起讀前次封存狀態；缺昨日帳本、待交割明細、行情或時間證據就阻擋；不混用其他 Snapshot |
| VA3 買賣決策與成交分離 | 接既有 Momentum、隔離 Buy／Sell、Trade Adjudicator、Portfolio Risk 與 Guard；決策後另接模擬執行並保存逐筆異動 | 未驗證的 DecisionResult 不執行；部分／零成交、拒絕及 no_trade 均能重建，帳戶更新不超賣、不負現金 |
| VA4 報告與續跑 | 報告呈現虛擬帳戶起點、決策前持倉、買賣提案、模擬成交狀態及下一步；與既有 workflow 的等待／失敗狀態接線 | 只在完整 Decision／Risk 通過時交付 DailyReport；等待成交時不得把提案寫成已成交；隔日續接前先驗證前 run |
| VA5 多日驗收 | 用合成資料先走空倉→買入→部分成交→交割→賣出→日終估值，再對同一邏輯做固定資料的前向演練 | 獨立手算現金、股數、費稅、NAV；來源版本與 cutoff 可重建。正式每日啟用仍須交易狀態、基準、規則及前向驗證通過 |

## 執行紀錄（2026-09-23）

- VA1：新增 `virtual_account.py init`，依競賽規則檔建立唯一 10 億 TWD 空倉 genesis；重複初始化只重用原狀態，不重新注資。帳戶 run 使用 manifest、內容雜湊、原子 latest 指標、單寫入鎖及連續父狀態驗證。
- VA2：新增 `prepare-day`，要求可用 Snapshot、較新的 cutoff 與逐檔行情；先結算到期款項／套用有 cutoff 證據的公司行動，再產生可直接接入決策包的 AccountSnapshot。`attach-account` 將該快照綁入同 Snapshot／cutoff 的 DecisionInputBundle 並完整驗證。
- VA3：新增 `apply-decision`，只接受 DecisionRepository 已封存且完整重建驗證的 Decision run，並要求其 AccountSnapshot 與最新 prepare 狀態完全一致。模擬成交資料必須晚於 decision cutoff，收盤後依成交、費稅與未交割款產生新帳本狀態；拒絕／no_trade 不會產生訂單成交。
- fixture 端到端測試已涵蓋 10 億開帳、決策前 AccountSnapshot、完整 Decision run 驗證、模擬買入、日終持倉／現金更新、重複開帳、未來狀態分叉拒絕與封存竄改拒絕。
- VA4 部分完成：DailyReport 1.1 顯示決策前帳戶，正式 workflow 在呼叫報告 Builder 前核對封存 prepare-day run 的 manifest、latest 狀態、Snapshot／cutoff 與 DecisionInputBundle AccountSnapshot，並記錄帳戶 run lineage；缺失或不一致即停止。CLI 續跑需提供 `--virtual-account-repository`、`--virtual-account-account-id`、`--virtual-account-run-id`。workflow 尚未自動呼叫帳本 `prepare-day`／`apply-decision`，也未呈現成交後帳務轉移；VA4 未整體完成。VA5 真實 Provider 與前向驗收也未完成。

## 執行紀錄（2026-09-25）

- 新增 `virtual_account.py settle／daily` 與 `start.sh daily` 帳本步驟：收集行情後，先以 Decision cutoff 後第一個交易日的官方收盤價（13:30 +08:00）結算最新 prepare 狀態對應的 Decision run，再以新 Snapshot 建立 prepare 狀態。成交價只取 `TWSE_STOCK_DAY`／`TWSE_STOCK_DAY_ALL`／`TPEX_TRADING_STOCK`／`TPEX_MAINBOARD_QUOTES`，不用 Yahoo；費稅、整張與賣款再用沿用 Decision run 的 rules。
- 假設（尚無官方依據）：可成交張數上限為當日成交量；交割日以週一至週五近似 T+2，不含國定假日。
- 收盤價未公布時回 `waiting_for_close_data` 並阻擋新 prepare；沒有對應 Decision run 時直接以新 Snapshot 續接估值。持股缺官方收盤價時 fail-closed。
- 修正 `apply-decision` 未傳 `reuse_sell_proceeds` 給成交模擬器（賣單成交時會 KeyError），並將日終狀態交易日改用執行日收盤資料的 `trade_date`。
- 新增唯讀 FastAPI 儀表板（`cli/dashboard.py`、`./start.sh dashboard`）顯示本金、NAV、今日／累積報酬、現金、持倉與每日紀錄。

## 失敗與邊界案例

- 第一日資金不是 10 億、使用錯誤版本的設定檔、第二日又注入 10 億、父狀態被改寫或同日重複成交。
- 股數不是整張、賣超既有持倉、買單超出可動用現金、賣出應收被錯誤重用、費稅或 NAV 改寫。
- 成交價／日終價缺失、價格時間晚於對應階段、公司行動或交割日證據缺失、隔日只有部分成交。
- 研究結果為 `pending`、決策 `rejected`／`no_trade`、Guard 硬性失敗、報告與帳本的 Decision run 不一致。
- 中斷後重跑、兩個每日 run 同時更新同一父狀態、磁碟檔案竄改、fixture 資料混入正式競賽模式。

Python、契約、CLI 或 Skill 實作後依 AGENTS.md 執行全專案 unittest、compileall 與 `git diff --check`；新增 Skill 時另執行 quick_validate。階段性成果需明確標記「虛擬帳務可重建」「決策已驗證」「正式每日啟用」各自是否完成，不以單一 fixture 測試代替全部驗收。

## 使用者可見的結果

完成 VA1～VA3 fixture 工具鏈後，可用 CLI 建立 10 億帳戶、把帳戶快照接入完整 Decision run、模擬成交並查看封存後的收盤狀態。這仍是 fixture 驗收；真實 150 檔行情、事件研究、交易狀態、規則與風控輸入通過前，不會自動形成正式每日買賣報告。
