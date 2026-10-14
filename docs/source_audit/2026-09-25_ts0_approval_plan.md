# TS0 交易狀態來源核准紀錄

本檔案名稱沿用既有證據路徑；`config/trading_status_approvals.json` 的七筆 `evidence` 均指向此檔。此處記錄 **2026-09-28 已完成的人工核准決定**，不代表任何特定決策日已通過 Guard。核准者、含時區核准時間及有效時數以設定檔為準。

## 查核依據與核准範圍

- `data/official_universe.csv` 固定交易池為上市 100、上櫃 50 檔。八份政府開放 CSV 與官方 JSON 的列數、日期分布及對應欄位，記於[逐欄對照資料](2026-09-25_ogd_crosscheck.json)；原始 CSV／JSON 位於未納入 Git 的 `artifacts/source-audit/2026-09-25`。沒有原檔時不能宣稱已重新驗算。
- TPEx 變更／分盤／管理 21／21 列、歷史停復牌 362／362 列對應欄位相符；注意與處置 CSV 分別有 20、17 列，JSON 含其他公告日的列，按 CSV 公告日期篩選後相符。
- TWSE 開休市 27／27 列、暫停交易 1／1 列、變更交易 10／10 列相符；處置 8／8 列在長文字排版空白正規化後相符。暫停交易唯一列是已恢復的舊事件，不能據此推論指定日沒有停牌。
- 來源授權證據來自政府資料開放平臺所列的 CSV 資源：[TPEx 變更／分盤／管理](https://data.gov.tw/dataset/11736)、[注意](https://data.gov.tw/dataset/11395)、[處置](https://data.gov.tw/dataset/11396)、[歷史停復牌](https://data.gov.tw/dataset/48665)、[TWSE 開休市](https://data.gov.tw/dataset/11761)、[暫停交易](https://data.gov.tw/dataset/11677)、[變更交易](https://data.gov.tw/dataset/11760)、[處置](https://data.gov.tw/dataset/29082)。CSV 的授權不得擴張至其他端點。

2026-09-28，使用者決定核准七個交易狀態來源：`TPEX_SPECIAL_OGD`、`TPEX_HALTS_HISTORY_OGD`、`TPEX_DISPOSAL_OGD`、`TPEX_ATTENTION_OGD`、`TWSE_HALTS_OGD`、`TWSE_SPECIAL_OGD`、`TWSE_DISPOSAL_OGD`；`max_age_hours` 均為 72。取捨是當時尚未在實際交易日驗證更新完成時點，因此捕捉版本超過 72 小時或缺少必要覆蓋時，Guard 仍須 fail-closed。注意資料只作提示，不自行當作停牌；處置公告日不能直接當成生效日。

TWSE「管理股票」對上市交易池不適用的官方依據及 2026-09-28 的獨立人工核准，見[管理股票判定紀錄](2026-09-28_twse_management_basis.md)。該類別由 `not_applicable` 政策表達，不能以不存在資料來源推定可交易。

## 重算與決策限制

有本機原檔時可重算對照：

```bash
.venv/bin/python scripts/verify_ogd_crosscheck.py \
  --report docs/source_audit/2026-09-25_ogd_crosscheck.json \
  --audit-dir artifacts/source-audit/2026-09-25
```

每日流程只使用 cutoff 前取得、同一 Snapshot 的封存資料；來源核准、日期覆蓋及時效均須由 Validator 和 Guard 檢查。來源核准不等於 150 檔當日完整可交易，也不等於完成正式 D-Plan 驗證。原始計畫與當時逐步查核內容可從 Git 歷史讀取。
