# TS0 交易狀態來源核准紀錄

檔名沿用既有的證據路徑。`config/trading_status_approvals.json` 的七筆 `evidence`，都指向此檔。

此處記錄的是 **2026-09-28 已完成的人工核准決定**。它不代表任何特定決策日已通過 Guard。核准者、含時區的核准時間與有效時數，以設定檔為準。

## 查核依據

### 交易池與原始資料

- `data/official_universe.csv` 固定交易池：上市 100 檔、上櫃 50 檔。
- 八份政府開放 CSV 與官方 JSON，其列數、日期分布與對應欄位，記錄在[逐欄對照資料](2026-09-25_ogd_crosscheck.json)。
- 原始 CSV／JSON 位於未納入 Git 的 `artifacts/source-audit/2026-09-25`。沒有原檔時，不能宣稱已重新驗算。

### 對照結果

| 來源 | 結果 |
| --- | --- |
| TPEx 變更／分盤／管理 | 21／21 列對應欄位相符 |
| TPEx 歷史停復牌 | 362／362 列相符 |
| TPEx 注意、處置 | CSV 分別有 20、17 列。JSON 含其他公告日的列，依 CSV 公告日期篩選後相符 |
| TWSE 開休市 | 27／27 列相符 |
| TWSE 暫停交易 | 1／1 列相符 |
| TWSE 變更交易 | 10／10 列相符 |
| TWSE 處置 | 8／8 列相符（長文字排版空白正規化後） |

TWSE 暫停交易的唯一一列，是已恢復的舊事件。不能據此推論「指定日沒有停牌」。

### 授權證據

來自政府資料開放平臺所列的 CSV 資源：

- [TPEx 變更／分盤／管理](https://data.gov.tw/dataset/11736)
- [TPEx 注意](https://data.gov.tw/dataset/11395)
- [TPEx 處置](https://data.gov.tw/dataset/11396)
- [TPEx 歷史停復牌](https://data.gov.tw/dataset/48665)
- [TWSE 開休市](https://data.gov.tw/dataset/11761)
- [TWSE 暫停交易](https://data.gov.tw/dataset/11677)
- [TWSE 變更交易](https://data.gov.tw/dataset/11760)
- [TWSE 處置](https://data.gov.tw/dataset/29082)

CSV 的授權不得擴張到其他端點。

## 核准決定

2026-09-28，使用者決定核准七個交易狀態來源：

| 來源代碼 | 內容 |
| --- | --- |
| `TPEX_SPECIAL_OGD` | TPEx 變更／分盤／管理 |
| `TPEX_HALTS_HISTORY_OGD` | TPEx 歷史停復牌 |
| `TPEX_DISPOSAL_OGD` | TPEx 處置 |
| `TPEX_ATTENTION_OGD` | TPEx 注意 |
| `TWSE_HALTS_OGD` | TWSE 暫停交易 |
| `TWSE_SPECIAL_OGD` | TWSE 變更交易 |
| `TWSE_DISPOSAL_OGD` | TWSE 處置 |

`max_age_hours` 均為 72。

**取捨**：當時還沒在實際交易日驗證「更新完成的時點」。所以捕捉版本超過 72 小時，或缺少必要覆蓋時，Guard 仍須 fail-closed。

**使用規則**

- 注意資料只作提示，不自行當作停牌。
- 處置公告日不能直接當成生效日。

## TWSE 管理股票

TWSE「管理股票」對上市交易池不適用。官方依據與 2026-09-28 的獨立人工核准，見[管理股票判定紀錄](2026-09-28_twse_management_basis.md)。

該類別用 `not_applicable` 政策表達，不能因為「不存在資料來源」就推定可交易。

## 重算與決策限制

有本機原檔時，可重算對照：

```bash
.venv/bin/python scripts/verify_ogd_crosscheck.py \
  --report docs/source_audit/2026-09-25_ogd_crosscheck.json \
  --audit-dir artifacts/source-audit/2026-09-25
```

- 每日流程只使用 cutoff 前取得、同一 Snapshot 的封存資料。
- 來源核准、日期覆蓋與時效，都由 Validator 和 Guard 檢查。
- 來源核准不等於 150 檔當日完整可交易，也不等於完成正式 D-Plan 驗證。
- 原始計畫與當時逐步查核的內容，可從 Git 歷史讀取。
