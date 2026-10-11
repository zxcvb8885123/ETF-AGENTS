# 風控後最終決策交付層設計

日期：2026-10-11。狀態：FD1 中文報告及 FD2 每日交付接線已實作；FD3 D-Plan 自動映射與 FD4 真實演練尚未完成。本文件承接 `competition_report_delivery_plan.md` 的 F4／F5。

### 本次落地範圍

- `decision_delivery` 提供封存載入、完整重建、JSON／中文 Markdown、不可變保存及有序 latest 發布；核准、零交易、拒絕分開呈現，重建失敗只產診斷。
- `cli/decision_delivery.py build --decision-run-id RUN` 與 `validate --delivery-id DELIVERY` 已可執行；預設歷史重建不更新 latest，不接受手寫交易日覆蓋。交易日取封存交易狀態 target_session，舊封存缺少時明列未提供，不由今天日期推測。
- 每日腳本封存後自動產報告；`scripts/run_daily_pipeline.py --delivery-only DAILY_RUN_ID` 只補交付。D-Plan 一律明示 blocked，既有 `cli/dplan.py` 維持獨立入口；未完成 context adapter 前不接受新交付服務的手寫 context。
- 實際目錄採 `artifacts/decision_deliveries/runs/<id>/` 保存 JSON artifacts，Markdown 精確文字封存在 `rendering.json` 並納入 manifest；`views/<id>/report.md` 是必須比對的閱讀副本。這避免向 core 的 JSON-only immutable run 混入未驗證 Markdown 檔案。下文平面目錄為原設計示意，以本段實際路徑為準。
- latest 依每日 run 原始 started_at 排序，歷史補交付不冒充今日。相同執行序允許失敗升級為可用報告；同一 Decision run 重建發現來源失效時，必須降為診斷。遲到的一般失敗與舊執行不能覆蓋新結果。發布鎖支援 Windows／POSIX；寫入失敗會回報，不能聲稱已更新索引。
- 報告保留全池意圖附錄、原始研究及風控 JSON；主文列有持倉或委託的標的。股票名稱在來源未提供時不補造，使用代碼；完整情境放在同源 JSON，Markdown 保留 Guard 情境檢查與警告。

下文未標示已落地的 D-Plan context adapter、官方 mapping sidecar、候選發布及真實資料演練仍屬後續設計。

## 目標與現況

修改前每日流程在風險審查後已有 Finalizer、完整重建驗證與 Decision run 封存，但只寫執行摘要與通知；本次新增風控後中文交付。`trader_report.md` 仍是風控前交易意圖；舊 DailyReport／FailureReport 工作流不恢復。`cli/dplan.py` 有獨立候選匯出及本地檢查，尚無每日完整 context 映射與交付串接。

本次設計補上使用者最後要看的「今天最終決定什麼、配置多少、為什麼、風險是否通過」，並為 D-Plan 建立同源交付入口。優先完成中文閱讀版，不讓隊伍策略或官方格式的缺口阻擋內部檢視。

```text
風險審查／修正循環
  → Finalizer → DecisionResultValidator → 不可變 Decision run
  → 交付服務重新載入並完整重建
      ├─ 最終決策 JSON → 中文 Markdown
      └─ D-Plan context 映射 → 既有 Exporter → 本地驗證
  → 交付 manifest／狀態 → latest 索引 → 人工檢視
```

交付層由確定性 Python 實作，不新增投資判斷 Agent，不呼叫模型生成摘要、不補事實或方向，也不改寫上游結果。Research Report V0 維持研究用途，不加入配置或委託。

## 使用者看到的中文報告

| 順序 | 內容 | 來源與呈現規則 |
| --- | --- | --- |
| 1 | 今日結果 | 交易日、資料截止時間（台北）、決策狀態、閱讀版驗證與 D-Plan 狀態各自呈現 |
| 2 | 帳戶與配置摘要 | 決策前 NAV／現金／持股；最終預估現金、費稅、持股數、買賣筆數，註明估值基準及分母 |
| 3 | 最終買賣清單 | 代碼、名稱、方向、股數、張數、參考價格及時間、預估金額／費稅、配置比例；全部來自已驗證結果 |
| 4 | 最終持倉與不交易 | 決策前股數、預計股數、最終配置；涵蓋所有原持倉、目標持倉及有委託標的，明列續抱與零交易原因 |
| 5 | 決策理由及風控調整 | 交易 Agent 原理由與信心；逐檔列出風控修正及其理由，區分「原意圖」和「最終結果」 |
| 6 | 風險與限制 | 現金姿態、Guard 硬性檢查、情境警告、修正次數、未解問題、重估條件、資料缺口 |
| 7 | 證據與驗證 | 引用索引、來源與可得時間、Snapshot／run／版本／雜湊、重建結果、D-Plan 阻擋原因 |

主文不重貼三位分析師和多空全文；附錄保留全交易池意圖及原報告連結。文字只做固定模板、列舉翻譯與原文引用，保留限制條件。事實、情緒、分析師共識、研究推論與交易決策分開標示；來源 unavailable 不改成中性。缺少可驗證的不交易理由時明示缺口，不代寫理由。

預計持倉及現金均標示「尚未成交」，不得呈現為實際帳戶狀態。內部目標比例、整張後實際比例與 D-Plan target_weight 若口徑不同須分欄、註明計算方式，不混用。

## 輸入與契約

交付請求指定 `decision_repository`、`decision_run_id`、`trade_date`；交易日必須核對封存的交易時鐘，不由執行當日推測。D-Plan 額外需要隊伍代號、核准策略、context 與 prepare-day 帳本綁定。請求不接受手寫配置、股數或現金覆蓋。

必需的 Decision artifacts：`decision_input`、`momentum`、`debate`、`intent`、`policy`、`proposal`、`scenario`、`guard`、`risk_review`、`revision_history`、`decision`、`team_inputs`。先驗 manifest，再跑 DecisionInputValidator 與 DecisionResultValidator；不把檔案雜湊通過視為語意驗證通過。沿用 team_inputs 2.1 及舊 2.0 的原始重建，拒絕已移除的 1.0。

`FinalDecisionReport` 1.0 的目前欄位（Validator 重建全文、拒絕新增或缺少欄位）：

- `schema_version`、`report_id`、`content_sha256`：使用 core canonical JSON／hash。
- `provenance`：決策 run、manifest hash、Snapshot、cutoff、交易日、帳戶快照 hash、輸入版本、builder／renderer 版本。
- `decision_status`：沿用 approved／no_trade／rejected。
- `account_before`、`portfolio_after_estimate`、`orders`：由封存與確定性工具取得；金額使用有限 Decimal 字串並附單位、幣別、估值時間與比例分母。
- `decision_rows`：原意圖、最終結果、原理由、信心、風控修正、claims／evidence 引用；每個值可回到 artifact 及欄位路徑。
- `risk_summary`、`analyst_reports`、`research_claims`、`evidence_index`、`reference_prices`、`errors`：保留原分類、資料缺口、來源、拒絕理由與未解風險；不另造資料限制摘要。
- `order_totals`：已驗證引擎的費稅與現金合計；拒絕時為 null。

Validator 從原始輸入重建整份 JSON，並以固定 renderer 重建 Markdown 比對。張數、摘要合計等顯示用衍生值由工具計算並重算；禁止累加四捨五入後的顯示值。不存在的欄位不得以零填補。

報告的投資內容與交付狀態分開保存：`delivery_status.json` 包含 `report_status`、`dplan_status`、結構化 `blockers`（代碼、階段、中文原因、輸入定位、下一步）、驗證層級與產物連結。產生時間屬交付 metadata，不回填資料 available_at。

## 狀態與失敗處理

| 條件 | 中文閱讀版 | D-Plan |
| --- | --- | --- |
| approved 且完整重建通過 | 已驗證的最終決策，含預計委託 | 條件齊全才匯出，標示本地檢查通過、待人工檢視 |
| no_trade 且完整重建通過 | 明列本日無委託、保留持倉及原因 | 零委託仍須通過持倉覆蓋與格式檢查 |
| rejected 且完整重建通過 | 已驗證的拒絕結果；最終配置為空、委託為空，呈現拒絕理由 | blocked，不產候選 |
| 缺 artifact、引用／時間／版本錯誤、竄改 | 僅產診斷頁，不稱為已驗證決策報告 | blocked |
| 決策通過，但隊伍、策略、context 或帳本不符 | 閱讀版仍可交付；另列 D-Plan 阻擋原因 | blocked |
| 寫檔或發布中斷 | 不發布不完整成功產物，保留可辨識的失敗狀態 | 不發布候選 |

`report_status` 使用 ready／blocked／failed；`dplan_status` 使用 not_requested／blocked／local_validated／failed。即使流程正常結束，rejected 仍不是核准交易；不得以單一 completed 掩蓋決策或交付狀態。平台驗證另記 not_run，未有實際回執不可稱已送件。

## D-Plan 映射與閘門

沿用現有 `DPlanExporter` 與 `verify_account_binding`，不建立第二份配置引擎。歷史閱讀版按封存帳戶重建；當次可交付候選仍遵守目前 latest prepare-day 綁定，帳本前進後重匯須被拒絕，不能為方便重播放寬。

| 官方欄位 | 映射來源／限制 |
| --- | --- |
| sources／observations | Snapshot 與既有 evidence 的來源、事實及確定性數值，保留原可得時間，去重且固定排序編號 |
| inferences | 已有且驗證過的研究 claims／交易論證；保留反證及原始引用，不由匯出器生成新推論 |
| market_view | 僅映射上游已有且語意一致的市場判斷；現金姿態不自動等同 risk_on／risk_off，缺欄位列阻擋 |
| decisions／no_trade_decisions／orders | 最終核准配置與委託，包含全部前日持倉；股票層引用必須與標的相符 |
| agent_metadata | 真實執行紀錄及程式版本；缺少實際模型／時間資料不得推測 |

新增的 context adapter 必須保存每個官方欄位到原 artifact／欄位／evidence 的映射 sidecar，Validator 逐欄重建。既有手工 context 路徑不等於完成此驗證，接入每日流程前須補 provenance、cutoff、策略內容 hash 與核准版本一致性檢查。

來源／觀察／推論等官方數量上限沿用已保存規格。僅選取最終決策所需的完整引用閉包，不能任意截斷；仍超限則阻擋並列出超限層級。這不改變上游情緒須涵蓋完整查詢視窗的規則。

Exporter 的官方股數推導若與 Decision orders 不同，直接拒絕，不調整權重湊格式。全市場情緒 unavailable 不可用交易池行情補成市場情緒。官方必填而上游未表達的判斷列為契約缺口，另行修改上游契約與測試後才接入。

## 模組、入口與保存

已新增 `src/etf_agent/decision_delivery/`：`contracts.py` 定義版本、必要輸入與錯誤；`provider.py` 載入封存；`tools.py` 做顯示格式與時鐘解析；`builder.py`、`renderer.py`、`validator.py` 分別建報告、呈現及重建；`repository.py` 繼承 core 的 ImmutableRunStore；`service.py` 統一入口，`__init__.py` 匯出公開介面。

D-Plan adapter 放在 `competition/`，交付服務呼叫既有 exporter。可抽取現有 load_decision_run 至共用服務並保留相容匯出；不得複製 loader 或核心時間／Decimal／hash 工具。

已提供 `cli/decision_delivery.py build／validate`，build 指定 Decision run 與輸出 repository，交易日由封存推導；D-Plan 選項尚未接入，交付狀態固定列出對應阻擋。CLI 僅包裝服務，ready 回傳 0、blocked 回傳 2、執行／發布錯誤回傳 1；validate 對診斷回報 `decision_validated=false`，不能視為決策驗證成功。

預定路徑：

```text
artifacts/decision_deliveries/<delivery_id>/
  report.json / report.md       # 報告可驗證才產生
  diagnostics.json / diagnostics.md  # 無法重建時的診斷
  dplan_context.json / mapping.json  # 選配、須驗證
  D-Plan_<team_id>_<trade_date>.json   # 本地檢查通過才產生
  validation.json / delivery_status.json / manifest.json
artifacts/reports/latest.json   # 唯一權威發布索引
artifacts/reports/latest.md     # 可重建的閱讀捷徑，含 run 與日期
```

先在暫存區完成重建與產物驗證，再原子封存；驗證結果綁定所有輸入與產物 hash。同鍵同輸入重用、同鍵不同輸入拒絕。交付識別涵蓋 Decision manifest、交易日、context／策略／帳本綁定及程式版本，補齊 context 後建立新交付，不覆寫舊 run。

latest.json 以原子替換指向完整交付，latest.md 從該索引重建；兩者可能短暫不同步，閱讀端須核對 delivery_id，不能假設多檔同時原子。今日失敗亦發布今日診斷；舊成功只列歷史連結，不冒充今天結果。歷史重播預設不更新 latest；並行發布需鎖定與比較交易日／本次執行順序，拒絕晚完成的舊 run 蓋掉新狀態。若連失敗索引也無法寫入，CLI／通知直接回報發布失敗，不宣稱 latest 已更新。

每日腳本在 Decision 封存後呼叫交付服務；模型或前置資料階段失敗、尚無 Decision run 時，走獨立診斷入口。摘要新增 report／D-Plan 狀態與路徑，通知呈現阻擋原因。續跑只重做交付，不重跑已完成的 Agent。無自動下單、帳本成交寫入或平台送件。

## 分階段實作與驗收

1. **FD1 中文最終報告**：實作契約、Builder／Renderer／Validator／CLI，先接受已封存 Decision run。驗收 approved、no_trade、rejected 與竄改拒絕；逐檔理由、風控前後差異及金額均可重建。
2. **FD2 每日交付接線**：封存、latest、失敗診斷與續跑；區分決策成功及報告成功。驗收缺資料、發布中斷、同鍵衝突、歷史重播與並行完成。
3. **FD3 D-Plan 同源輸出**：實作 context adapter／mapping validator，補齊真實隊伍策略與 metadata 後串既有 exporter。驗收全持倉覆蓋、引用閉包上限、時鐘／帳本／策略 hash 不一致、官方推導股數差異皆 fail-closed。
4. **FD4 真實演練**：指定一份可完整重建的真實 Decision run，逐筆人工核對報告、委託、費稅及 D-Plan；缺來源則保留阻擋結果，不用 fixture 代替。平台驗證與策略有效性分開記錄。

修改 Python／CLI 時依 AGENTS.md 執行完整 unittest、compileall、git diff --check。測試須包含 cutoff 後資料、缺時區、跨 Snapshot、錯股票引用、缺 team_inputs、數值與 Markdown 竄改、風控否決仍有候選、unavailable 被誤表為中性及舊成功遮蔽今日失敗。fixture 僅證明程式行為，真實資料驗收另列。

完成 FD1／FD2 即可解決「跑到風險 Agent 後沒有最後一份報告」；FD3／FD4 完成前仍不得宣稱已完成正式 D-Plan 端到端交付。
