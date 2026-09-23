# 正式帳戶資料接入與對帳計畫

狀態：AC1～AC4 fixture 版已完成（2026-09-23）。使用者確認主線帳戶為 10 億起始的虛擬帳戶，因此本計畫改列選配的外部結算檔對帳支線；每日決策帳戶主線見[虛擬帳戶與每日買賣決策接入計畫](virtual_account_daily_decision_plan.md)。AC0 正式欄位核實及 AC5 匯出檔驗收仍待平台樣本，不阻擋虛擬帳本實作。

## 目標與完成邊界

讓使用者提供競賽平台的帳戶匯出檔後，系統能保存原始資料、驗證時間與來源、重算帳務、列出差異，再將通過驗證的帳戶輸入交給 Portfolio Decision。資料匯入及數字驗證由確定性 Python 程式負責，沿用 Data Agent 與決策層的分工。

第一版採本地檔案匯入，不預設平台已有 API。檔案格式以取得的正式樣本為準；可先建立標示為 fixture 的標準 JSON 與測試。正式樣本未取得前，可完成 AC1～AC4 的程式與 fixture 驗收，AC0 的來源確認及 AC5 的真實驗收必須保持未完成。

帳戶通過只解除帳戶這一項阻擋。交易狀態、完整 ETF 基準、有效競賽規則及正式回測／前向驗收仍需分別完成，才能接正式 Decision／Risk 與 DailyReport。

## 現有實作與缺口

- `src/etf_agent/decision/contracts.py` 已驗證 `account_snapshot` 的來源引用、可得時間、估值時間、持倉及 NAV；目前要求 `cash = settled_cash + unsettled_cash`，且三項現金皆非負。
- 現有帳戶欄位採嚴格白名單：`account_id`、`available_at`、`valuation_at`、`source_evidence_id`、`cash`、`settled_cash`、`unsettled_cash`、`nav`、`positions`。持倉保存代號、股數與平均成本。
- 配置與情境引擎以 `settled_cash` 作為初始購買力。真實帳戶的凍結款、應付交割款及平台可用額度未必符合此假設，不能直接填入。
- 已有標準 JSON 匯入、原始檔封存與逐筆交割對帳；尚無經核准的外部平台 Provider 或其正式欄位映射。設定檔初始本金則供虛擬帳戶唯一開帳使用，不能充當外部結算檔證據。
- 目前整張決策流程不支援零股；匯入須保留原始股數並回報限制，不得取整或刪除部位。

## 資料與時間契約

規劃新增 `AccountDataBundle` 與 `AccountReconciliationResult`，與既有精簡 `AccountSnapshot` 分開保存。契約名稱、版本與欄位於 AC1 固定：

| 資料 | 必須保留的內容 |
| --- | --- |
| 來源與版本 | 平台／來源識別、匿名帳戶識別、原始檔雜湊、原始檔引用、解析器版本、執行模式及欄位映射版本 |
| 時間 | 帳務時間、估值時間、檔案匯出時間（來源有提供時）、實際取得時間與可證明的 `available_at`，全部含時區 |
| 現金 | 幣別、帳面現金、已交割金額、平台可動用金額及凍結／保留款；來源未提供時標記缺漏，不補零 |
| 交割明細 | 唯一識別、買賣／應收應付方向、金額、來源提供的交割日與完成狀態，避免淨額掩蓋義務 |
| 持倉 | 股票代號、實際股數、平均成本、可賣股數或限制狀態（來源有提供時）；完整保留交易池外部位 |
| 估值與對帳 | 平台原始 NAV、其估值口徑、Snapshot 重算值、差額、容許誤差的版本依據及逐項證據 |

`decision_cutoff` 與 `snapshot_id` 綁定使用的 ResearchSnapshot。現在才取得的帳戶檔，不能自行把 `available_at` 回填為歷史帳務日；要續用歷史 cutoff，必須有當時已可得的證據，否則另建新 Snapshot 與研究流程。過期帳戶、帳務與估值時點不一致的可接受範圍須以版本化政策明訂，不能只檢查「早於 cutoff」。

## 執行順序

| 階段 | 工作與交付 | 通過條件 |
| --- | --- | --- |
| AC0 來源與欄位確認 | 盤點平台可提供的匯出格式、欄位定義、幣別、時間、交割及 NAV 口徑；建立來源／欄位對照文件 | 正式樣本與欄位語意有可追溯依據；未取得時明確列為待取得 |
| AC1 契約與相容性 | 定義資料包、對帳結果、嚴格 schema、數值精度、缺漏與拒絕代碼；核對現行決策契約是否能無損表示 | 應收、應付、凍結款與可動用現金不混用；無法表示時阻擋 adapter，必要升版另列明確變更與回歸範圍 |
| AC2 匯入與封存 | 建立本地檔案 Provider、Parser 與 Repository；保存原始檔、正規化內容、版本與 manifest | 同內容可重用，不同內容不覆寫；拒絕重複交易識別、錯誤型別、缺少必要欄位及路徑越界 |
| AC3 確定性對帳 | 以 Decimal 計算持倉市值、NAV、交割淨額與可動用現金；產生 JSON／Markdown 差異報告 | 能由原始帳戶與固定價格重建；差異超限、缺行情、未知現金口徑、可用額度不足均不可放行 |
| AC4 決策接入與 CLI | 驗證原始鏈後產生 AccountSnapshot 與來源引用；串接既有 DecisionInputBundle validator，報告工作流顯示帳戶缺口 | 無法由未驗證 JSON、偽造通過標記、舊對帳或其他 Snapshot 繞過；fixture 不能通過正式模式 |
| AC5 真實資料驗收 | 使用正式匯出樣本、對齊的 Snapshot 與核實政策完整執行，保存驗收紀錄 | 原始檔 → 資料包 → 對帳 → 決策帳戶輸入皆可重建；只標記帳戶前置通過，其他缺口仍保留 |

## 執行紀錄（2026-09-23）

- AC1：建立 `AccountDataBundle`／對帳輸出的嚴格 JSON 欄位與 Decimal 數值檢查；不改寫既有 DecisionInputBundle schema。
- AC2：本地標準 JSON 匯入器保存來源雜湊、匿名帳戶識別、parser 版本及原始檔；帳戶 run 以 manifest 不可變封存並驗證檔案集合與 SHA-256。
- AC3：依同一 cutoff Snapshot 重算持倉市值、NAV、帳面現金及可用現金上限，核對交割明細、價格覆蓋、交易池、幣別與來源時間。任何檢查失敗回傳 `blocked`。
- AC4：提供 `import`、`validate`、`reconcile`、`export-decision-account`、`verify-run` CLI，並輸出 JSON／Markdown 對帳結果。正式匯入與決策 adapter 都檢查 `config/account_sources.json` 的 provider／版本核准狀態；目前清單為空。fixture 不能匯出。既有契約沒有凍結現金與可用額度欄位，第一版因此要求凍結款、未交割款為零且可用現金等於已交割現金，否則阻擋而不猜測交易購買力。
- 使用合成 fixture 通過七項帳戶測試：NAV 重算、cutoff 未來資料拒絕、來源核准閘門、交割明細對帳、缺價格阻擋、fixture 禁止匯出及封存竄改偵測。全專案測試以最新執行結果為準。
- AC0／AC5 未完成：目前未找到官方帳戶匯出樣本或已核實的欄位／NAV／交割語意。本輪不能宣稱正式平台接入；取得去識別化樣本並完成欄位與 NAV 口徑核實後，再加入來源核准設定及執行真實驗收。

AC1 優先保留現有決策契約。若凍結款、應付交割款或其他負債無法無損轉換，不能改名塞入 `settled_cash` 或以負數塞入目前非負的 `unsettled_cash`。須升版時同步修改配置、情境、Guard、報告重建與相關回測相容性；舊 fixture 不自動升格為正式資料。

## 對帳與停止規則

- 先核實來源對現金及交割的入帳方式，再固定公式。未交割金額若已包含於帳面現金，不得再次加總；應付款不得當成可用本金。
- 平台原始 NAV 保留不變，另算 Snapshot 估值 NAV；估值日或價格口徑不同時，分開呈現差異，不直接覆寫成相同數字以通過檢查。
- 重算需涵蓋所有持倉。缺行情、交易池外部位或不支援的資產仍留在帳戶資料包中，adapter 明確阻擋，不忽略後繼續估值。
- 交割日優先保留來源欄位；需要推算時必須有可追溯的有效規則與交易日曆，不以固定日曆天數推算。
- 原始檔或衍生內容被改寫、引用缺失、版本／帳戶／Snapshot 不符、cutoff 後資料、數值不有限或對帳超限時，輸出失敗原因且不產生可用的決策帳戶輸入。
- 帳戶資料屬本地私有 artifacts，原始帳號以匿名識別替代一般報告顯示；正式帳戶檔不放入 Git，測試只使用合成 fixture。

## 預計程式與產物

核心邏輯已放在 `src/etf_agent/accounts/`，CLI 放在 `cli/account_data.py`，測試放在 `tests/test_account_data.py`。目前只輸出決策帳戶欄位，尚未把它自動填入完整 DecisionInputBundle；沿用現有 Skills，需要變動操作方式時才同步修改。

現有 CLI 子命令為 `import`、`reconcile`、`validate`、`verify-run` 與 `export-decision-account`；尚無 `status`。操作入口見 README，正式來源核准設定目前為空。

目前 `import` 會封存至 `artifacts/account_runs/<run_id>/`：

- `raw/`：原始匯出檔及來源 metadata。
- `account_bundle.json`：正規化但仍保留來源語意的帳戶資料。
- `manifest.json`：匯入原件、資料包版本與檔案雜湊。

`reconcile` 另依 CLI 指定位置輸出 `reconciliation.json` 與 Markdown；`export-decision-account` 輸出 `decision_account.json`，它不是完整 DecisionInputBundle。對帳與匯出結果的不可變 run 封存仍待後續擴充；本選配支線目前不得作為每日虛擬帳本來源。

## 驗證與交付標準

正常案例涵蓋空倉但有正式現金證據、一般持倉、未交割應收／應付、來源明確的凍結款；以獨立手算的預期值驗證，不能只讓同一演算法比較自己的輸出。

失敗案例涵蓋未知欄位、重複股票／交割識別、負股數、布林股數、NaN／Infinity、無時區、cutoff 後資料、過期帳戶、錯誤價格日期、缺價格、NAV 差異、零股、來源被改寫及 fixture 混入正式模式。另驗證同資料重跑、不同版本隔離、父子引用、部分寫入後重啟與 adapter 拒絕無法表示的帳務。

修改 Python／CLI／契約／Skill 後依 AGENTS.md 執行全專案 unittest、compileall 與 `git diff --check`；新增或大幅修改 Skill 時執行 quick_validate。

完成後使用者可匯入帳戶檔、查看對帳報告與取得已驗證的決策帳戶輸入。正式樣本不足時，交付已實作功能、fixture 驗收與確切待取得欄位，不標記真實接入完成。此階段不自動建立 commit。
