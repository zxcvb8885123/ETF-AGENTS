# P7 回測 Agent 第一批實作計畫：B0～B2

狀態：B0～B2 fixture MVP 已完成（2026-09-21）。接續 #23；沿用目前分支。真實歷史 Provider、策略比較與績效驗證仍未完成。

## 目標與範圍

建立可重播的多交易日 fixture 流程：歷史時鐘 → 當時可得資料 → 已驗證決策 → 模擬成交 → 跨日帳務 → 封存。固定一張 1,000 股，所有金額與績效由確定性 Python 計算。

第一批已提供 fixture 多日重播；`historical_verified` 模式會要求每個研究版本具備來源與時間證據，並重新驗證完整 DecisionResult 及 RevisionHistory。fixture／exploratory 僅驗證重播流程，不能升級為正式績效驗證。下一日決策的帳戶必須來自前一日 ledger，不能每天使用互不相干的帳戶快照。若保存的決策與重建帳戶不一致，停止並要求重新產生該日決策。

本批交付可手算的短期間重播及帳務報告。六組策略比較、完整績效統計、Agent 品質評估與固定版本前向驗證依 B3～B6 後續實作。真實歷史回測仍依賴 Data Agent M4。

## 現有能力與必要銜接

- 重用 DecisionInput、Momentum、Debate、Intent、Proposal、Scenario、Guard、RiskReview 與 RevisionHistory validators；hash 正確不代表語意合法。
- 現有歷史行情保留 fetched_at，但不足以證明歷史 available_at；新增時間點 Provider 契約，不能把今日補抓時間倒填成歷史可得時間。
- 現有 Snapshot 的 analysis_close_price 可能是還原價，且配置目前以此估算交易。本批須明確分開分析價格、cutoff 前未還原參考價與執行期未還原成交價；必要時調整共用配置契約並補回歸測試。
- ScenarioResult 是決策時的壓力假設；ExecutionResult 是模擬交易時根據執行資料生成的成交紀錄，兩者不得互換。
- #23 的 fixture 通過是起點；回測新增案例若揭露既有計算或驗證缺口，先補強共用引擎，再交接回測。

## B0：契約、版本與歷史時鐘

建立 BacktestRequest、BacktestRun、HistoricalClock 與 validators。

- Request 固定期間、Asia/Taipei cutoff、初始帳戶、交易日曆、資料 manifest、規則／策略／工具版本、成交假設與 run ID。
- 預設研究時點為交易日 D 的 08:55；只能看到 cutoff 前可得版本及 D−1 以前完整日線。交易日曆由版本化輸入提供，不能只用週一至週五推算。
- 區分 decision、execution、close、settlement 時點。D 日成交與收盤資料只能由相應模組在時鐘推進後讀取。
- run 明列 fixture、exploratory 或 historical_verified；缺歷史可得性證明時不得升級成正式歷史驗證。
- 嚴格檢查型別、時區、排序、重複日期、未知欄位、內容雜湊與版本。

驗收：週末／休市／跨月推進正確；缺日曆、無時區、未來規則、版本不一致均拒絕。

## B1：時間點資料與重播入口

建立 PointInTimeDataProvider、FixtureReplayProvider 與 ReplayOrchestrator。

- 每份資料保留 evidence_id、來源、內容時間、published_at（適用時）、available_at、fetched_at、版本、原始內容雜湊；取 available_at 不晚於 cutoff 的合法版本。
- 同一事件更正後，舊 cutoff 仍取得舊版本；不可用最新資料覆寫歷史。交易池、可交易狀態、基準、產業分類及公司行動同樣版本化。
- 決策端只能取得研究資料視圖；執行端另取 D 日價格與可成交量。不可把整日執行資料嵌入 role input。
- 逐日驗證保存的上游 artifacts，再重建並驗證決策、修正鏈及帳戶連續性。記錄缺資料、rejected、no_trade 與失敗日。
- 有效 rejected／no_trade 當日不建立新交易，但仍推進交割、公司行動及估值；缺必要估值或輸入不合法時停止，保存 failed run 與尚未完成日期。
- 採用今日交易池回放過去時，記錄交易池選擇／存活者偏差，不產生無偏績效聲明。

驗收：cutoff 後事件、更正版本、未來價格或錯誤帳戶均不能進入決策；失敗日不可被刪除。

## B2：整張成交、交割與帳務

建立 ExecutionSimulator、AccountLedger 及逐日重建 validator。

- 只接受完整驗證後 approved DecisionResult 的訂單；不執行 rejected 的提案訂單。
- 執行設定明列價格來源、滑價、成交量參與限制、費稅、最低費用、排序與交割規則。fixture 使用明確標示的假設；正式均價及交割口徑待正式來源核實，不硬編為已確認競賽規則。
- 以未還原價格成交及估值；還原價格僅供分析。成交股數為 1,000 股整數倍，保存全部未成交數量與原因，禁止超賣、負買力及重複成交。
- 買賣分別計算成交限制，支援全成、部分成交、零成交、停牌與缺價。買單不能使用未允許重用的賣款；買力不足時依固定順序逐張縮減並記錄原因。
- Ledger 分開 settled cash、應收／應付交割、持股與成本，按版本化日曆到期結算。定義決策帳戶 adapter，避免現有 cash 欄位漏算應付款或重複計入賣款。
- 公司行動先涵蓋現金股息與拆併股 fixture，保留權利日、入帳日及版本。導致零股或遇到未支援事件時明確停止；不暗中取整或重複計入股息。
- 每日保存期初、交易／交割／公司行動流水、期末持股、收盤市值、現金、NAV 及成本。以獨立不變量檢查資金與股數守恆。

驗收：至少一組三個以上交易日的手算案例，覆蓋買進、減碼、退出、交割與下一日帳戶銜接；另測最低費用、小數價格、一元現金邊界、1／2／3 張減碼、賣單未成／買單可成、零成交及股息不重複計算。

## 儲存、CLI 與輸出

核心已放在 `src/etf_agent/backtest/`，以 contracts、engine、service、repository 分開歷史時鐘、時間點資料、成交／帳務、重播與封存。共用交易計算仍應持續收斂，避免回測與每日決策各自維護不同費稅公式。

第一批已提供 `skills/strategy-backtest/scripts/backtest.py` 的 `validate-request`、`inspect-coverage`、`replay`、`validate-run`、`save-run` 與 `build-report` 指令；`strategy-backtest` Skill 已建立，但完整 Agent 工具循環與跨策略實驗仍維持 B5 順序。

封存 BacktestRun、每日 ReplayResult、ExecutionResult、AccountLedgerSnapshot、輸入與 manifest，產出同源 JSON／Markdown 帳務驗收報告。報告列出期初／期末 NAV、成交與成本、失敗日、資料模式及限制，不宣稱策略有效。

run 目錄與 manifest 驗證沿用 #23 的安全名稱、不可變保存及讀回檢查原則；測試缺檔、竄改、中斷、重跑、符號連結與路徑越界。Validator 從初始帳戶逐日重播，不能只比對保存的期末數字。

## 實作順序與完成門檻

1. B0 契約、日曆、歷史時鐘與時間隔離測試。
2. B1 fixture Provider、決策／執行視圖與每日帳戶綁定。
3. B2 共用價格／費稅邊界、成交、交割、公司行動及 ledger。
4. 串接多日重播、封存、CLI 與帳務報告。**已完成 fixture MVP。**
5. 全專案 unittest、compileall、git diff --check；同步 README、架構、回測計畫與契約文件。

已能以同一保存輸入重播相同逐日結果，所有成交與帳務可手算核對，未來資料測試確實被拒絕，並能封存及重建帳務驗收報告。B3～B6、真實歷史 Provider、完整績效統計與策略有效性驗證維持未完成。此計畫不預設下一個 commit 編號。
