# Research Report V0 計畫

> 開發順序：在正式動能／配置／風控與 DailyReport 之前，先把現有 Data、Event Research 與 Market Perception 輸出整合成可閱讀、可稽核且不含交易建議的研究報告。

## 定位

Research Report V0 是研究層的確定性整合產物，不是新的投資決策 Agent。它只複製或重組已驗證的上游欄位，不增加市場結論、不計算權重、不產生訂單，也不取代未來的正式 `DailyReport`、`D-Plan` 或 `FailureReport`。

第一版由 Codex／Claude 載入 `research-report` Skill，再呼叫 Python Builder。JSON 與 Markdown 必須來自同一份 `ResearchReport 1.0`；Markdown renderer 不得重新推論或改寫數字。

## 輸入

必要輸入：

- 可用的 `ResearchSnapshot`。
- `status=completed|degraded` 的 `ResearchResult 2.1`。

選配且必須成對提供：

- `MarketPerceptionResult 1.0`。
- 產生該結果的 `PerceptionDataBundle 1.0`，用來補回完整來源 metadata。

所有輸入的 `snapshot_id` 與 `decision_cutoff` 必須一致。若沒有市場情緒／分析師資料，仍產出 `degraded` 報告並標示 `MARKET_PERCEPTION_UNAVAILABLE`。

## 輸出

- `research_report.json`：固定 `ResearchReport 1.0` 契約。
- `research_report.md`：由 JSON 確定性渲染的人類可讀版本。

報告包含資料品質、研究覆蓋、公司最新分析價格、事件摘要、事實、多方、空方、裁決、行情確認、情緒、分析師共識、預期差、證據、風險、缺漏與限制。

報告不包含 `DecisionResult`、配置、權重、股數、費稅、訂單、績效承諾或送件欄位。

## 元件

| 元件 | 責任 |
| --- | --- |
| `ResearchReportBuilder` | 驗證輸入版本與引用，建立固定 JSON |
| `ResearchReportValidator` | 用原始輸入重建整份報告，拒絕任何數字或文字改寫 |
| `ResearchReportMarkdownRenderer` | 對不受信任文字做 Markdown escaping，再渲染可讀報告 |
| `ResearchReportApplicationService` | 讀取檔案、建立與驗證 JSON／Markdown artifact |
| `research-report` Skill | 定義輸入選擇、CLI、停止條件與交付邊界 |

## Fail-closed 規則

- Snapshot 不可用或上游結果為 `failed` 時停止。
- `snapshot_id` 或 cutoff 不一致時停止。
- Event 或 Perception 引用不存在、股票不一致或 evidence ID 衝突時停止。
- 提供 PerceptionResult 卻未提供原始 Bundle 時停止。
- `generated_at` 早於 cutoff 時停止。
- 報告被人工或 LLM 改寫後，Validator 重建比較必須失敗。
- 原始摘要、thesis 與來源文字視為不受信任內容；Markdown 渲染時逸出控制字元。
- Report V0 不接受交易欄位，也不宣稱完成正式 D-Plan。

## 里程碑

1. **R0 契約**：固定 `ResearchReport 1.0`、來源與缺漏欄位。
2. **R1 Builder**：整合 Snapshot、Event Research 與選配 Perception。
3. **R2 Validator**：以同一組輸入重建並比對完整結果。
4. **R3 Renderer／CLI**：輸出同源 JSON 與 Markdown。
5. **R4 Skill／測試**：建立 `research-report` Skill，覆蓋版本、cutoff、引用、竄改、缺少 Perception 與安全 escaping。

## 完成條件

- 相同輸入、`report_id` 與 `generated_at` 產生完全相同 JSON 與 Markdown。
- 每個事件、價格、情緒及共識引用都能追到上游 evidence。
- 沒有 Perception 時仍能產生明確降級的事件研究報告。
- 任一報告欄位被修改後 Validator 必定拒絕。
- 全套測試與 Skill 結構驗證通過。
