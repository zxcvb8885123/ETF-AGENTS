# 基本面研究 Agent 計畫

日期：2026-09-22。狀態：**FR0～FR3 fixture MVP 已完成；FR4 真實資料演練與 FR5 下游升版尚未實作。**

## 1. 目標與架構位置

建立 `fundamental-research`，回答「在指定 cutoff，公司目前的營運與財務結構如何，有哪些可驗證的改善、惡化或資料缺口？」。事件研究回答單一事件改變了什麼；基本面研究提供不依賴當日事件的公司研究背景。

參考 [TradingAgents 的 Fundamentals Analyst 分工](https://github.com/TauricResearch/TradingAgents#tradingagents-framework)，採用獨立基本面研究角色；不引入其模型 API、LangGraph 或交易執行流程。首版由本地 Codex／Claude 載入 Skill，透過既有 CLI 慣例呼叫 Python 工具，不增加多空子 Agent。

```text
Data Agent → 不可變 ResearchSnapshot
                    ↓
         基本面資料包 → 確定性財務指標
                    ↓
         基本面研究 Agent → Validator
                    ↓
         FundamentalResearchResult
                    ↓
  研究報告／Portfolio Decision（後續升版接入）
```

角色邊界：

- Data Agent 負責取得、解析、版本化及驗證官方資料；基本面 Agent 不自行抓網頁或寫入核心財報。
- 基本面 Agent 解讀已驗證數值，區分事實、推論、假設、反證條件與限制；不將成長率當作市場共識或交易方向。
- 不輸出買賣評等、候選名單、目標價、權重、股數或訂單，不覆寫事件研究或風控結論。
- 事件研究、基本面研究與市場認知各自保存結果；重複引用同一財報不代表多份獨立證據。下游按 evidence ID 識別重複來源。

## 2. 現有基礎與首版範圍

已存在官方損益表／資產負債表收集、版本保存、`SnapshotFinancialStatement` 與財報覆蓋報告。既有文件記錄 2026 Q2 為 298／300，`3718.TWO` 缺兩張報表；這是既有驗收紀錄，不是本計畫重新實測，也不代表完整歷史資料可用。

首版採固定請求股票、期間與指標清單，逐公司產出研究，不做全市場排名。先支援可核實欄位的一般業；銀行、保險、證券等金融業須另訂業別公式與驗收，首版明列不支援，不套用一般業毛利率或負債判斷。

| 首版可規劃能力 | 資料條件 |
| --- | --- |
| 收入、利益、資產、負債與權益事實整理 | 已映射且語意明確的官方欄位 |
| 營業利益率、負債占資產比率 | 同一公司、幣別、口徑及適用期間；必要欄位均存在 |
| 同比變化與利潤率差異 | Snapshot 同時含當期與去年同期可比版本 |
| 財務限制與風險說明 | 引用已驗證指標與證據，不以單一比率斷言財務危機 |

現有 Data Agent 尚未映射可驗證的毛利欄位，因此毛利率不納入已實作首版。首版不做毛利率、單季拆算、TTM、ROE／ROA、DCF、目標價、同業估值排名、分析師共識、現金流品質或盈餘操縱判定。這些能力需額外欄位、期間、平均資產／權益、股數、現金流量表或其他資料與獨立計畫。只有一期資料時可描述當期，不能宣稱已有趨勢。

## 3. Provider 與時間邊界

- Provider 為既有 Snapshot／evidence repository 的唯讀 adapter；外部來源取得仍交給 Data Agent。
- 只接受已核准來源及可驗證原始回應、映射版本、內容版本。來源狀態未知時拒絕使用，不以搜尋摘要、模型記憶或 fixture 補足。
- 固定帶時區 `decision_cutoff`，所有證據 `available_at <= decision_cutoff`；財報期末日、資料產製日不能充當發布或可得時間。
- `published_at` 無法證明時保留 null；沿用資料層保守可得時間。今日取得的舊年度財報不能倒填為過去可用。
- 財報更正只能影響可得時間之後的新 Snapshot，既有結果與舊 cutoff 維持原版本。
- 不直接讀取資料庫「最新值」混入固定 Snapshot。需補資料時提出結構化缺口，另建新 cutoff／Snapshot 後重跑。

## 4. 擬定資料契約

FR0 已固定以下首版 schema；實際欄位與操作方式見 `skills/fundamental-research/references/fundamental-contract.md`。

| 物件 | 必要內容 |
| --- | --- |
| `FundamentalResearchRequest` | schema／request ID、snapshot ID 與內容雜湊、cutoff、股票清單、目標期間、比較期間、必要／選配指標、policy version |
| `FundamentalDataBundle` | 請求雜湊、固定覆蓋分母、逐公司報表、業別、合併／個別口徑、單季／累計／時點、幣別、單位、來源、時間、原始回應 ID、evidence ID、內容與映射版本、缺口 |
| `FundamentalMetrics` | bundle 雜湊、公式版本、metric ID、Decimal 值、單位、分子／分母及其引用、期間、適用性與無法計算原因 |
| `FundamentalResearchResult` | request／bundle／metrics 雜湊、snapshot ID、cutoff、generated_at、逐公司事實、推論、假設、反證條件、限制、覆蓋、狀態與內容雜湊 |

所有物件採嚴格欄位白名單與固定序列化規則。result 的事實只能引用 bundle，數值只能引用 metrics 或原始已驗證財報；推論須列 `evidence_ids`／`metric_ids`，不得增加數字或無來源事實。保存實際可取得的執行、程式、Skill 與模型識別；未知模型識別留空，不猜測。

狀態按公司及整批分別計算：

- `completed`：請求內全部必要資料、適用性及驗證通過；不代表公司值得投資。
- `degraded`：存在可用研究，但部分股票、期間或必要指標缺漏；明列不可支持的判斷。
- `unavailable`：無可用研究或全部業別不支援，保留完整缺口。
- `failed`：時間、引用、內容雜湊或契約違規，整份結果不可交給下游。

股票與指標覆蓋分母由 request 固定，不因缺資料或不支援而刪除。缺少選配指標保留原因；是否影響狀態由版本化 policy 固定。request 必要性不得由 LLM 執行中放寬。

## 5. 確定性計算與驗證

公式登錄表須保存適用業別、欄位語意、期間條件、單位、精度及捨入政策。首批擬定：

- 營業利益率＝營業利益／收入 × 100。
- 負債占資產比率＝總負債／總資產 × 100，同一資產負債表時點。
- 同比成長率＝（本期－去年同期）／去年同期 × 100，只在比較基期大於零且口徑可比時計算。零或負基期標示不可用，可另提供同單位絕對差額。
- 利潤率差異以百分點表示，不與百分比成長混用。

使用 Decimal；非有限值、未知單位、零或不適用分母不可計算。禁止跨公司、幣別、報表範圍及不等長期間混算；累計與單季不可直接比較。欄位不存在時不從相似名稱猜測映射。

Validator 必須：

1. 從固定 Snapshot 重建 bundle，核對請求、來源、時間、版本、覆蓋分母與雜湊。
2. 從原始輸入與公式版本重算全部 metrics，拒絕任何數值、精度或單位改寫。
3. 驗證 result 白名單、逐公司歸屬、引用、狀態、數值與所有依賴；拒絕交易欄位與未知引用。
4. 保存通過驗證的 result 與雜湊，後續報告只能複製該封存版本，不重新生成結論。

確定性 Validator 能驗證結構與證據一致性，不能證明自然語言推論必然正確。推論合理性以人工標註集另外評估，不把通過 schema 宣稱為研究正確率。

## 6. 實作位置與操作流程

核心已放在 `src/etf_agent/fundamentals/`，包含 Snapshot adapter、metrics、Validator 與 service；`cli/fundamental_research.py` 只做穩定包裝。持久化 repository 留待實際封存需求確認後再分離。

CLI 已提供：`status → build-bundle → compute-metrics → validate-result → archive`。本地 Agent 讀取 bundle／metrics 後提出結構化 result，再交給 Validator。exit code 已固定為 0＝完整通過、2＝明確降級／不可用或驗證不通過、1＝輸入、時間、版本或封存錯誤；不可僅憑檔案存在認定成功。`archive` 以不覆寫方式保存已驗證 result。

Skill 預定為 `skills/fundamental-research/SKILL.md`，詳細契約放 references，`agents/openai.yaml` 的 default_prompt 必須明列 `$fundamental-research`。首版不自動補抓資料，驗證失敗最多修正兩次；禁止改寫原始數字，耗盡後封存失敗原因。不建立 commit、排程、送件或下單副作用。

## 7. 開發順序與驗收

| 階段 | 工作 | 通過條件 |
| --- | --- | --- |
| FR0 契約與欄位盤點 | **已完成（fixture）**：固定 schemas、已映射欄位、公式及狀態政策 | 每個首版指標有來源欄位或明確不可用原因，無假設性映射 |
| FR1 Bundle adapter | **已完成（fixture）**：同一 Snapshot 的唯讀資料包、引用與覆蓋 | 可重建且 cutoff／更正版本隔離測試通過 |
| FR2 指標與 Validator | **已完成（fixture）**：Decimal 工具、引用驗證、嚴格欄位及封存驗證 | 正常與 fail-closed 測試通過，所有數值可重算 |
| FR3 CLI 與 Skill | **已完成（fixture）**：本地工作階段流程、有限修正、稽核與文件 | 同一 fixture 可重跑；Skill 結構驗證通過 |
| FR4 有限真實資料演練 | 預先固定 3～5 家、期間與選樣理由，保留完整缺口 | 至少一家產出可重建基本面研究；其餘缺漏如實降級，不宣稱 150 檔驗收 |
| FR5 下游升版接入 | Research Report、Portfolio Decision、離線 DailyReport 的契約與 adapter | 同 cutoff、來源去重、白名單、雜湊及重建回歸通過 |

FR0～FR3 已完成，下一步是 FR4 以當時實際可用資料進行有限範圍驗收。FR5 必須顯式升版並同步文件，不將新欄位直接塞入既有嚴格契約。Research Report 目前不接受此結果；後續接入時 result／bundle／metrics 必須一起驗證。Portfolio Decision 必須將同一已驗證結果放入 Buy／Sell 的共同輸入，再建立隔離 role artifacts，Adjudicator 不新增基本面事實。

本計畫是新增研究支線；原主線的 M1 交易狀態 → 細粒度工具 → M2 → M3 不因此取消或視為完成。FR0～FR3 可使用已驗證 fixture，不依賴新聞／情緒 Provider；FR4 依賴可用財報 Snapshot，FR5 正式決策仍受帳戶、交易狀態、規則與基準前置條件約束。歷史回測必須另通過 M4，不將目前最新財報當成歷史版本。

## 8. 測試與完成定義

測試至少涵蓋：

- 一般業正常資料、單期可用但無同比、缺表、缺欄位、不支援金融業與空資料。
- cutoff 後資料、無時區、後續更正、重抓去重、舊 Snapshot 維持原值。
- 股票誤配、引用不存在、原始回應或版本雜湊不一致。
- 千元／元、負值、零分母、非有限值、期間長度與合併口徑衝突。
- 指標或單位遭修改、漏列請求股票、竄改缺口與狀態、注入交易欄位。
- 不受信任來源文字不能變更工具權限；CLI 失敗退出碼、修正上限與封存衝突。
- FR5 增加報告重建、舊版相容及 Buy／Sell 相同輸入與隔離測試。

FR0～FR3 完成只能稱為「基本面研究 fixture MVP」；FR4 才能稱為「有限範圍真實研究驗收」，FR5 才能稱為已接入指定下游。人工檢核事實／推論區隔及引用正確性，保存失敗案例；不以少數樣本宣稱策略有效。

實作時執行 AGENTS.md 規定的完整 unittest、compileall、git diff --check，以及新 Skill 的 quick_validate.py。本次僅建立計畫與架構文件，不表示上述能力已可執行。
