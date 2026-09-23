# 真實研究續跑與決策報告驗收計畫

日期：2026-09-23。狀態：W0～W5 已完成有限範圍驗收；W6 因正式決策輸入不足而等待。本次先完成按需研究交付，再依正式輸入可用性接決策報告；不把程式接線完成當成真實資料驗收通過。

## 目標與現況

讓使用者在既有 Agent 工作階段提出一次報告請求後，取得可閱讀、可重建的 Research Report，以及清楚的決策前置缺口。必要正式輸入齊備時，才延伸到 DailyReport。

目前已核對的執行為 `rpt4-stage-20260922`，狀態是 `waiting_for_agent`；沿用 Snapshot `3061d9e0-376d-49c7-b348-4e16fb5e0101`，cutoff 為 `2026-09-22T14:21:56+00:00`，已封存 187 筆事件候選。工作區尚無 `artifacts/event_research_validated.json`。這是 9 月 22 日封存資料，不能標成 9 月 23 日即時研究。

RPT0～RPT4 已有工作流、CLI、Skill 與下游報告接線；仍缺本輪真實研究輸出、實際角色工作階段啟動／回傳整合，以及正式 Decision／Risk 輸入。先前其他 Snapshot 的三事件演練不替代本輪驗收。

## 執行順序

| 階段 | 工作 | 交付與驗收 |
| --- | --- | --- |
| W0 固定輸入與檢查續跑 | 驗證父 run manifest，使用封存的 input_snapshot 而非可變 latest；確認原始文件與 DB 仍可依版本重建 | 記錄 commit、Skill 雜湊、父 run、Snapshot／cutoff；缺原始證據即停止。補抓晚到資料必須另建 Snapshot／run |
| W1 工作流可靠性補強 | 檢查 resume 是否驗證父 run 與輸入一致性；檢查重複執行是否重用最新有效 attempt；檢查決策輸入指紋及等待／失敗分支 | 用測試確認等待→研究→等待決策→交付的轉移；同鍵異內容、錯父 run、改寫輸入、下游失敗均不能誤報成功。僅修正驗收發現的缺口 |
| W2 明確研究範圍 | 保留完整 187 件清單；首輪依既有工具支援及證據可驗證性選最多 5 件，可得時間由新到舊，同時間按 event_id 排序 | 保存選取規則、入選／未入選理由、候選總數與實際研究數；不得宣稱已研究全部 187 件，不依多空方向挑選 |
| W3 執行研究角色 | 主控讀取 daily-report、event-analysis 與角色契約；Fact 後由隔離的 Bull／Bear 工作階段或子 Agent 讀同一 FactPacket，再交 Adjudicator | 保存角色輸入、輸出、工作階段識別與工具紀錄；Bull／Bear 不含對方輸出或歷史。無法隔離時維持等待 |
| W4 驗證與報告交付 | debate／result 雙重驗證後封存 ResearchResult，使用明確的封存 Snapshot 路徑續跑，建立並重建驗證 Research Report | 至少一件真實事件通過；JSON／Markdown 同源、引用與數字可重算；報告列研究範圍、限制與缺漏，固定入口可找到本輪報告 |
| W5 決策前置盤點 | 核對帳戶、持倉、現金、交割、交易狀態、基準與競賽規則；列明回測／前向驗收證據 | 每項保存來源、版本、available_at、cutoff、驗證結果與缺漏取得方式；必備項缺少則交付研究報告與缺口清單，維持 waiting_for_decision |
| W6 條件式 DailyReport | 僅在 W5 通過後，用同一 Snapshot／cutoff 建立 Portfolio Decision 輸入，完成 Momentum、隔離 Buy／Sell、裁決與 Risk，再接報告 Builder | 確定性計算配置、股數、費稅、現金與情境；Guard／Risk 與結果重建通過才交付 DailyReport，拒絕時保存 FailureReport |

本次執行範圍為 W0～W5；W6 僅於正式前置資料與驗收齊備時執行。盤點結果須區分「缺資料」「有資料但未驗證」「驗證失敗」，不可只列籠統的待完成。

## 決策輸入清單

| 輸入 | 最低要求 |
| --- | --- |
| 帳戶與持倉 | 首日由 10 億本金設定建立一次空倉虛擬帳戶；後續由已驗證的前次帳本、交割與模擬成交續接，固定 cutoff 的股數、可動用現金、未交割款與 NAV 須可重算；詳見[虛擬帳戶計畫](virtual_account_daily_decision_plan.md) |
| 交易狀態 | 官方來源核准及 150 檔覆蓋；未知、衝突或時間不符維持不可交易 |
| 基準 | 所有必備 ETF 基準成分與權重、有效日期、版本及證據 |
| 競賽規則 | 有效版本、來源與雜湊、持股／現金／交易限制；不能只用 fixture 規則 |
| 研究與行情 | 相同 Snapshot／cutoff、已驗證 ResearchResult、必要歷史行情覆蓋 |
| 正式驗收 | 所需歷史回測與固定版本前向驗證證據；離線帳務測試不能代替策略有效性驗收 |

## 產物與使用體驗

本輪研究工作檔保存在 `artifacts/local_research/acceptance-20260923-3374/`：選取紀錄、角色輸入輸出、DebateBundle、驗證輸出、決策缺口清單與執行紀錄。其他 run 仍須各自建立獨立目錄。

通過驗證的結果先保存於該 run 的不可變路徑，再作為 workflow resume 的明確輸入；若更新 `artifacts/event_research_validated.json` 便捷入口，仍須保留原 run 與雜湊，不能混用其他 Snapshot。

報告由既有 `artifacts/report_runs/<run_id>/` 封存，`artifacts/reports/latest.md` 顯示最新執行、報告位置及下一步。研究完成但等待決策時，必須能直接閱讀 Research Report；`waiting_for_decision` 不能讓使用者誤以為研究失敗。DailyReport 與 FailureReport 也須提供明確連結。

Shell 命令目前只能準備輸入與接收結果。此次驗收在互動 Agent 工作階段完成角色交接；無人值守模型執行與每日排程不列為本輪完成條件。

## 測試與完成條件

- 產物驗證：DebateBundle、ResearchResult、Research Report 重建、workflow manifest 與所有輸入版本一致。
- 工作流測試：缺研究輸出、缺決策、下游拒絕、重複續跑、父 run 不存在／不相符、cutoff 不符及檔案改寫。
- 若修改 Python／CLI／契約／Skill，依 AGENTS.md 執行完整 unittest、compileall、git diff --check；大幅修改 Skill 時執行 quick_validate。
- 完成 W0～W5：至少一件真實事件完成隔離研究與雙重驗證、報告可重建、固定入口可閱讀，且決策缺口逐項有證據與下一步。全部事件驗證失敗時，本輪未通過，不以診斷頁宣稱研究報告完成。
- W6 通過才宣稱本輪 DailyReport 完成；真實前置資料不足不以 fixture 補齊。

## 後續開發順序

依 W5 缺口先補官方交易狀態 TS0／TS5 與帳戶／規則／基準，再做真實決策整合；正式回測與前向驗證沿既有計畫推進。基本面 FR4／FR5 與合法 Perception Provider 分別驗收，缺 Perception 可明確降級，不阻擋本輪研究報告。

D-Plan 待官方 schema 與語意規則；正式每日排程待資料、決策及驗收完成後另行啟用。維持既有不直接串接模型 API、LangChain／LangGraph、不自動送件或下單的邊界。

## 本輪執行結果（2026-09-23）

- W0：驗證父 run manifest；封存 Snapshot 與原 `latest` 檔雜湊相同，原始 DB 可讀 187 件事件與 337 筆證據。之後續跑始終指定父 run 的 `input_snapshot.json`。
- W1：補上 resume 的父 run 存在性、可續跑狀態、Snapshot／cutoff／模式、ResearchResult 一致性檢查；同執行鍵優先重用最新有效 attempt，衝突診斷 run 不阻擋合法續跑。新增缺父 run、改寫 Snapshot、重複續跑、衝突後合法續跑與下游 FailureReport 測試；全專案 181 項測試通過。
- W2：完整 187 件清單保存在父 run；依固定時間排序選第一件具可核對財務資訊的 3374.TWO。研究一件，其餘 186 件沒有被宣稱已研究。
- W3～W4：Fact、獨立 Bull／Bear、Adjudicator 四 packet 已保存，DebateBundle 與 ResearchResult 均通過 CLI Validator。裁決是 `indeterminate`／`uncertain`／`pending`。報告工作流 `acceptance-20260923-3374` 交付降級 Research Report，重建驗證與 manifest 驗證均通過；降級因無合法市場認知資料。
- W5：逐項缺口在 `decision_readiness.md`。該次盤點時尚未釐清帳戶來源；使用者其後確認是 10 億起始的虛擬帳戶，故帳戶缺口改為每日虛擬帳本建立與續接。完整基準、交易狀態 150 檔驗收、正式規則版本與前向驗證仍未就緒；W6 不能通過前置檢查，工作流維持 `waiting_for_decision`。
- 工作階段、Skill 雜湊與驗證紀錄在 `execution_record.md`。這次是互動 Agent 工作階段執行，不代表每日無人值守能力。
