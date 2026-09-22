# 自動化報告 Agent：一鍵研究與報告交付計畫

日期：2026-09-22。狀態：待實作。本文件補充既有自動化排程／報告計畫，先完成按需研究交付，再接正式決策及排程；不把新增計畫視為已可用功能。

## 目標與目前缺口

使用者啟動一次流程後，能從固定入口看到本次執行結果、可閱讀的報告、資料缺漏與下一步。報告 Agent 負責編排、驗證、格式化、封存及本地交付；研究與交易判斷仍由既有 Agent 負責。

- 已完成：Research Report V0 Builder／Validator、事件研究工具與角色 Skills、離線 DailyReport／FailureReport A0／A1。
- 部分完成：`start.sh daily` 收集資料並建立 Snapshot，但沒有執行研究、接收 Agent 輸出或交付報告。
- 本次檢查：目前快照可用、包含 150 檔行情，但文件數為零；工作區未找到已驗證事件研究檔。文件記載的先前演練不能代替本次可驗證產物。
- 待排查：目前腳本在事件收集前固定預設 cutoff，可能排除新資料；也須檢查收集結果、來源時間、資料庫掛載與 Snapshot 篩選，不能僅由零文件斷定原因。
- 尚未接入：Agent 工作階段交接、續跑、固定交付入口、正式帳戶／規則及決策資料、官方 D-Plan 規格與正式排程。

## 使用流程與責任

```text
按需啟動 → 建立執行紀錄 → 收集與品質檢查 → 固定 Snapshot
  → 封存事件範圍與候選清單
  → 事件研究：Fact → 隔離 Bull／Bear → Adjudicator
  → debate 與 result 雙重驗證 → Research Report V0
  → 若要求決策報告：正式前置檢查 → Portfolio Decision／Risk → DailyReport
  → 完整重建驗證 → 不可變封存 → 更新本地報告入口
```

缺少可用 Agent 工作階段時，保存 `waiting_for_agent` 與角色輸入；工作階段完成後驗證並續跑。Shell／scheduler 不具備模型推論能力，不以模板產生假研究結果。維持現有不直接串接模型 API、LangChain／LangGraph 的限制。

第一版以 Codex／Claude 工作階段載入規劃中的 `daily-report` Skill 主控，確定性 Python 服務處理狀態、數字、驗證及交付。Skill 呼叫既有事件研究角色，Bull／Bear 必須使用隔離工作階段與相同 FactPacket。無工作階段時，一鍵 CLI 可準備輸入並交付等待狀態，不能宣稱已完成無人值守研究。

## 報告格式與交付規則

JSON 為權威內容，Markdown 由同一份已驗證 JSON 確定性渲染。首頁明列報告類型、執行模式、資料截止時間、產生時間、研究範圍與狀態。

| 區塊 | 固定內容與來源 |
| --- | --- |
| 執行摘要 | 本次成功、降級、等待或失敗；可用產物與缺漏，不新增市場結論 |
| 資料品質 | Snapshot、交易日、行情／文件覆蓋、來源版本及時間、缺漏原因 |
| 事件研究 | 可驗證事實、多空推論、裁決、不確定性及 evidence ID，分開呈現 |
| 情緒／分析師共識 | 只呈現成對驗證結果；無合法 Provider 時明列 unavailable |
| 決策與風控 | 只在 DailyReport 出現；使用通過驗證的決策、配置、訂單及 Guard |
| 待處理事項 | 阻擋階段、錯誤碼、下一步及重跑／續跑方法 |
| 稽核附錄 | run ID、cutoff、版本、來源引用、輸入輸出雜湊與驗證結果 |

- **研究報告**：有可用 Snapshot 與通過雙重驗證的 ResearchResult 才建立 Research Report V0；缺少選配情緒／共識可降級，不含配置或訂單。
- **決策報告**：具備相同 Snapshot／cutoff 的完整決策 run，且正式前置條件通過後才建立 DailyReport。fixture 必須明顯標示測試用途。
- **等待／失敗交付**：必備資料缺少、零文件、研究未完成或驗證失敗時，交付執行狀態／診斷頁；不得偽裝成成功研究報告，零文件也不等同市場沒有事件。
- **D-Plan**：獨立候選檔階段，待官方 schema、語意規則與正式前置驗收完成；不阻擋研究報告先交付。請求 D-Plan 失敗時該次交付為 FailureReport，已驗證研究可作稽核附檔。
- 基本面研究仍待 FR5 契約升版，不能直接塞入現有 Research Report 或 DailyReport。

規劃產物位置（尚未建立）：

```text
artifacts/report_runs/<run_id>/
  inputs/                 固定版本的必要輸入與角色交接
  attempts/               各次嘗試、等待、錯誤與續跑紀錄
  research_report.json/md 或 daily_report.json/md（驗證通過才出現）
  execution_report.json/md（執行狀態與診斷）
  manifest.json           輸入輸出雜湊、版本與驗證結果
artifacts/reports/latest.md       最近一次執行結果及檔案連結
artifacts/reports/latest.json     最近一次執行索引
artifacts/reports/latest_success.json 最近一次通過報告索引
```

沿用既有封存服務，新增外層執行紀錄與索引 adapter；不重複實作 Builder。最新執行失敗時，首頁必須顯示失敗，不把舊成功報告當今日結果。舊成功報告可連結，但明列其日期／cutoff。暫存區驗證完成後原子發布，已封存檔不得覆寫；續跑追加 attempt，保留原始紀錄。

## 實作階段與驗收

| 階段 | 工作 | 驗收條件 |
| --- | --- | --- |
| RPT0 資料與時間修復 | 追查零文件；記錄收集 exit code、數量、時間與 DB 路徑；預設收集完成後固定 cutoff | 新抓資料依真實 available_at 納入；使用者指定歷史 cutoff 時維持原值，不回填可得時間；零文件交付診斷 |
| RPT1 交付契約與格式 | 定義 execution report、索引及渲染契約；擴充既有 PipelineRun／manifest 的 adapter；先建立 run 再讀上游 | 檔案缺失、JSON 損壞或初始化失敗也可留診斷；無 Snapshot 時使用 null，不捏造 ID；JSON／Markdown 同源 |
| RPT2 研究編排與續跑 | 保存完整事件清單及選取規則；角色交接、獨立多空、雙重驗證；執行研究報告 Builder | 同 Snapshot／cutoff；無 Agent 轉等待；跨 run、過期或改寫結果被拒；至少一個真實事件完成驗證與報告 |
| RPT3 一鍵交付入口 | 擴充 `start.sh daily`；新增最薄的 workflow CLI（run／status／resume／verify）與 `daily-report` Skill；列印絕對報告路徑 | 成功、等待、降級、失敗都有可閱讀入口；重複啟動不重複發布；CLI 退出狀態不把等待當成功 |
| RPT4 決策報告接入 | 核對真實帳戶、現金、持倉、交易狀態、基準、規則及回測／前向驗收；串接既有 DailyReport | 真實決策與風控通過；缺必要資料時只交付診斷及已驗證研究，不以 fixture 補正式輸入 |
| RPT5 排程與候選檔 | 在原 A2／A3／A5 前置通過後啟用 scheduler、互斥鎖、有限重試、恢復與官方 D-Plan 驗證 | 逾時、並行、中斷、重複觸發測試通過；缺官方規格停止候選檔；最後交付人工檢視 |

下一批實作範圍為 **RPT0～RPT3**：讓使用者先能取得真實研究報告，並在不能產出時看到具體原因。這是按需研究交付，不提前啟用正式每日決策排程。RPT4 與 RPT5 依各自前置條件接續。

## 狀態、恢復與測試

- 工作階段狀態：pending／running／waiting_for_agent／succeeded／blocked／failed；報告狀態維持各既有契約。新增值須顯式升版，不直接混入舊 schema。
- 防重複鍵納入模式、業務日期、cutoff、設定與策略版本；內容指紋納入所有輸入雜湊。同鍵同輸入重用已驗證結果，同鍵不同輸入拒絕覆寫。
- 重試只針對暫時性收集錯誤；補抓晚於已固定 cutoff 時建立新快照／新 run。研究等待逾時保持可稽核失敗，不能放寬截止時間續用晚到資料。
- 本地交付為本批預設；不新增 Slack／Email 發送或自動平台送件。未指定正式執行時刻，不擅自建立排程。
- 測試涵蓋正常研究報告、選配資料缺少、零文件、收集失敗、等待與續跑、角色隔離、時間／版本混用、引用不存在、內容改寫、初始化錯誤、原子發布中斷、舊成功報告誤標與同時啟動。
- 修改程式／CLI／Skill 後執行 AGENTS.md 的完整 unittest、compileall、git diff --check；新增 Skill 再執行 quick_validate。真實演練另執行 debate／result、報告重建及 manifest 驗證。

完成 RPT0～RPT3 的標準：一次按需工作階段能交付有證據、可重建的真實研究報告；在每個已定義的等待／失敗分支都有清楚的本地狀態頁、錯誤原因與續跑方法，且使用者可從同一固定入口找到結果。
