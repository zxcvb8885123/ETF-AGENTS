# benchmark｜虛擬帳戶績效對基準的比較

把已驗證的虛擬帳本 NAV 序列，和兩個買入持有基準（0050、150 檔交易池等權）放在同一口徑下比較：同樣的本金、同樣的官方成交均價、同樣的費稅（`config/decision_rules.json`）、同樣的官方未還原收盤價估值。全部由確定性 Python 計算，不寫入帳本或資料庫，資料不足時該項 `unavailable`，不補假值。

## 檔案清單

| 檔案 | 職責 | 主要公開項目 |
| --- | --- | --- |
| `contracts.py` | 錯誤型別、費稅設定（讀 `decision_rules.json`）、官方日線列、來源優先序 | `BenchmarkError`、`FeeSchedule`、`OfficialPriceRow`、`OFFICIAL_SOURCE_PRIORITY` |
| `repository.py` | 唯讀（`mode=ro`）讀取 as-of 時點已可得的官方日線版本（`fetched_at ≤ as_of`、收盤 ≤ as_of、不含 Yahoo） | `BenchmarkPriceRepository` |
| `tools.py` | 整張買進（含手續費不超過預算）、每日估值、累積報酬、最大回撤、相對回撤 | `whole_lot_buy`、`buy_and_hold`、`valuation`、`cumulative_return`、`max_drawdown`、`relative_drawdown` |
| `capture.py` | 純觀察的每日上漲／下跌捕捉（逐日表、彙總、累積超額），純函式、不讀檔 | `build_daily_capture`、`MIN_CAPTURE_DAYS`、`DEFINITION` |
| `service.py` | 組合帳本摘要、基準設定、交易池與行情，產生比較結果 | `build_benchmark_comparison`、`METHOD` |

## 口徑

- 起點：帳戶 genesis（全現金）。基準在開帳後第一個可成交的官方交易日買進：開帳時間在交件期限（台北 08:55）前算當天，否則下一個交易日；交易日集合＝交易池在 as-of 時已有官方日線的日期。
- 成交：該日官方成交均價（成交金額 ÷ 成交股數，四捨五入 0.01，與帳本 `OfficialCloseMarket` 相同）；整張買進，手續費 `max(成交金額 × commission_rate, minimum_commission)` 無條件進位到元（`ledger.ExecutionSimulator.fill`）。0050 用全部本金；等權用本金 ÷ 150 當各檔預算，含手續費不得超過預算。剩餘現金留著，之後不再交易，不計現金股利與配股。
- 估值：每個交易日現金＋持股 × 官方未還原收盤價。任一持股當日缺官方收盤價，該日 NAV 為 `null`（列出缺價標的），不以其他來源補。
- 指標：累積報酬＝NAV ÷ 本金 − 1；最大回撤＝峰值到谷底最大跌幅；超額報酬＝帳戶累積報酬 − 基準累積報酬（取雙方都有估值的最後一個交易日）；相對回撤＝帳戶 NAV ÷ 基準 NAV 序列的最大回撤。
- 時點：只採用 `fetched_at ≤ as_of` 且收盤時間 ≤ as_of 的官方日線版本（`daily_prices` 為 append-only 版本表），事後用舊 as-of 重算會得到當時可見的結果。

## 每日上漲／下跌捕捉（純觀察）

每個 `status=ok` 的基準結果多一個 `daily_capture`（`compare --json`、`/api/benchmarks` 共用）。**只是觀察指標**：不進決策輸入、不進 `team_inputs`、不給任何 Agent、不改配置。目標：基準漲時帳戶漲得比它多（上漲捕捉 > 100%）、基準跌時帳戶跌得比它少（下跌捕捉 < 100%）。

- 日報酬：同一條 NAV 序列相鄰交易日的 NAV 比 − 1（開帳後第一天相對本金，所以第一天含基準與帳戶各自的成交價差）。前一日或當日任一邊缺估值（基準缺價、帳本缺日）就略過並列在 `skipped`，不跨日硬湊、不補值；日期完全對不上則整體 `unavailable`。
- 方向：基準日報酬 > 0 上漲日、< 0 下跌日、= 0 持平日（不計入任一側，記 `flat_days`）。
- 上漲捕捉＝上漲日帳戶日報酬**平均值** ÷ 同期基準日報酬平均值；下跌捕捉同理（各日報酬平均值之比，不是累積報酬比）。附各側 `days`、兩個平均值。
- `small_sample`：任一側少於 `MIN_CAPTURE_DAYS`（5）個交易日即標記，數字仍附但不可當結論；該側沒有任何日子則該側 `unavailable`（`capture` 為 `null`）。
- `cumulative`：帳戶累積報酬、基準累積報酬與兩者差（累積超額，取雙方都有估值的最後一個交易日，與 `comparison.excess_return` 同值）。
- `rows`：逐日表（交易日、帳戶日報酬、基準日報酬、差額＝帳戶−基準、`benchmark_direction`）。全部數值為有限 Decimal 字串（6 位小數）。
- 終端機 `compare` 在註記列印彙總；dashboard 基準比較區塊顯示彙總與可展開的逐日表。

## 上下游

- 上游：`core`、`data`（`benchmarks.py`、`price_finality`、`trading_calendar`、`universe`）、`ledger`（費稅）、`virtual_account`（帳本）、`dashboard.performance`（帳戶 NAV 序列）。
- 下游：`dashboard.app`（`/api/benchmarks`）、`cli/benchmark.py compare`。
- 讀：`var/etf_agent.db`（唯讀）、`artifacts/ledger/`、`config/benchmarks.json`、`config/decision_rules.json`、`data/official_universe.csv`。不寫任何檔案。

## 規則與邊界

- 只有 `license_status=approved` 的基準進正式計算；其他基準顯示 `unavailable` 與原因。
- 基準標的收集（`etf_agent.data.benchmarks.collect_benchmark_history`）以 `in_competition_universe=False` 保存，不污染交易池。
- 比較結果不是決策輸入，不產生候選、權重或訂單。
