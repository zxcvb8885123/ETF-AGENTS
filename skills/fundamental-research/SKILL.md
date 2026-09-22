---
name: fundamental-research
description: 使用同一份 ResearchSnapshot 的已驗證財報與確定性指標，研究台股公司的營運與財務結構。用於建立、驗證或封存 FundamentalResearchResult；不得用於產生交易候選、權重、目標價或訂單。
---

# 基本面研究 Agent

只讀取指定 cutoff 的可用 `ResearchSnapshot`，由 Python 建立 `FundamentalDataBundle` 及重算 `FundamentalMetrics`。本 Skill 的角色是解讀已驗證事實、假設、反證條件與限制；不自行抓取網頁、修改財報、產生市場共識或交易決策。

首版只支援已有明確欄位映射的一般業 `ci`／`mim`。可重算的指標是營業利益率、負債占資產比率、營收／淨利同比與營業利益率年差。沒有比較期間、欄位、可比幣別或正值比較基期時維持 `unavailable`，不得補猜。毛利率、現金流品質、估值、金融業公式與歷史回測不在首版範圍。

完整契約與輸出範例請在建立草稿前閱讀[基本面研究契約](references/fundamental-contract.md)。

## 工作流程

1. 執行 `status`。若 Snapshot 不可用、cutoff／來源證據缺失或業別不支援，停止受影響範圍並保留原因。
2. 以固定股票、年度、季度與指標清單執行 `build-bundle`。不可在過程中刪除缺資料股票或放寬請求分母。
3. 執行 `compute-metrics`。所有數字、單位與公式以此輸出為準；不要手算或改寫值。
4. 每個股票寫一個基本面研究 item。`observations` 的每一項都要列出該公司 bundle 的 `evidence_ids`；指標主張同時列出 `metric_ids`。事實與推論分開，明列假設、反證條件與限制。
5. 執行 `validate-result`。只有 `valid=true` 的輸出可被封存或交給後續人工研究；驗證失敗最多修正兩次，之後封存失敗原因，不改寫上游數字。
6. 需要保存已驗證結果時才執行 `archive`。它拒絕覆寫既有檔案。

從專案根目錄執行：

```bash
.venv/bin/python cli/fundamental_research.py \
  --snapshot artifacts/research_snapshot_latest.json status

.venv/bin/python cli/fundamental_research.py \
  --snapshot artifacts/research_snapshot_latest.json build-bundle \
  --symbols 2330.TW --fiscal-year 2026 --fiscal-quarter 2 \
  --output artifacts/fundamental_bundle.json

.venv/bin/python cli/fundamental_research.py \
  --snapshot artifacts/research_snapshot_latest.json compute-metrics \
  --bundle artifacts/fundamental_bundle.json \
  --output artifacts/fundamental_metrics.json

.venv/bin/python cli/fundamental_research.py \
  --snapshot artifacts/research_snapshot_latest.json validate-result \
  --bundle artifacts/fundamental_bundle.json \
  --metrics artifacts/fundamental_metrics.json \
  --input artifacts/fundamental_research_draft.json \
  --output artifacts/fundamental_research_validated.json
```

exit code `0` 是完整可用結果；`2` 是已產出但降級、不可用或驗證不通過；`1` 是輸入、時間、版本或檔案錯誤。降級不是成功的投資結論。

## 邊界

- `FundamentalResearchResult` 不包含買進／賣出、候選、評等、目標價、權重、股數、訂單或績效承諾。
- 不把財報同比、趨勢或單一比率宣稱為市場預期、分析師共識或必然股價方向。
- 不可混用不同 Snapshot、cutoff、幣別、報表口徑或期間；財報更正後須以新 Snapshot 重跑。
- 現有 Research Report、Portfolio Decision 與 DailyReport 尚未接收本結果；不要將封存結果自行放進既有嚴格契約。
