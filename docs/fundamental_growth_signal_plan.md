# 基本面成長訊號與可比性計畫

狀態：計畫，尚未實作。本文件只描述診斷與設計，不改變任何既有行為。

目標：讓基本面分析師能分辨「真的在成長」與「只有單一不可比訊號」，使信心等級對成長股／科技股具有區分力。證據全部取自 2026-10-28 的 `daily-20261028-205822` 與 `decision-20261028T125822Z`。

## 一、診斷

### 1.1 基本面 positive 佔 83／150，區分力不足

技術分析師的分布是 negative 57、neutral 48、positive 44（會分辨方向）；基本面是 **positive 83、neutral 57、negative 9**。目前 Skill 的定義是「正面：可用資料支持較好的經營狀況或改善方向」，沒有幅度門檻，也沒有跨股可比性要求。

### 1.2 程式算得出來的指標，成長類幾乎全缺

`fundamental_metrics.json`（150 檔，其中 **144 檔 `degraded`**）：

| 指標 | available | unavailable |
| --- | --- | --- |
| `liabilities_to_assets_pct` | 149 | 1 |
| `operating_margin_pct` | 131 | 19 |
| `revenue_yoy_pct` | **7** | **143** |
| `net_income_yoy_pct` | **5** | **145** |
| `operating_margin_pp_yoy` | **7** | **143** |

缺漏原因計數：

| scope／statement | reason_code | 檔數 |
| --- | --- | --- |
| prior_year／income_statement | `COMPARISON_STATEMENT_MISSING` | **143** |
| current／all | `UNSUPPORTED_INDUSTRY_fh`（金控） | 12 |
| current／all | `UNSUPPORTED_INDUSTRY_basi`（銀行） | 4 |
| current／all | `UNSUPPORTED_INDUSTRY_bd` | 1 |
| current | `CURRENT_STATEMENT_MISSING` | 2 |

### 1.3 更正：分析師並非「看不到成長」

**月營收年增 150 檔全部都有**（`brief_fundamental_b*.json` 的 `monthly_revenue`，欄位 `yoy_pct`、`cumulative_yoy_pct`）。所以準確的說法是：

- **看得到**：月營收年增（全部 150 檔）、當期營業利益率（131 檔）、負債占資產（149 檔）。
- **看不到**：獲利年增、營業利益率趨勢（143 檔缺去年同期損益表）。
- **完全沒有程式指標**：17 檔（金融業公式不支援）。

### 1.4 關鍵：7 檔金融持股的基本面依據只有一個不可比的訊號

使用者投資組合裡的 7 檔金融（2801、2880、2884、2885、2886、2890、2891）**全部**落在 `UNSUPPORTED_INDUSTRY_fh`／`basi`，程式一個比率都算不出來。它們仍得到 `positive`，依據實際上只有月營收年增：

| 股票 | 分析師依據（節錄） | 自述缺口 |
| --- | --- | --- |
| 2885.TW | 「月營收年增 8月57.94%、9月67.25%，累計年增105.17%，營收大幅成長」 | 無去年同期損益表、無現金流、無同業標準 |
| 2886.TW | 「月營收年增 8月15.55%、9月16.45%，累計年增16.56%，成長穩定」 | 同上 |
| 2801.TW | 「月營收年增 8月21.13%、9月12.75%，累計年增14.17%」「沒有去年同期可比資料，不能說獲利改善」 | 同上 |

分析師對缺口完全誠實（每檔都寫明），問題不在它說謊，而在**制度允許單一不可比訊號支撐 positive**。

金融業的「月營收」在會計上與製造業的營業收入不是同一種東西（含保險費收入、證券手續費等，波動極大）。把 2885 的累計年增 105.17% 與半導體的 35% 並列比較，會系統性高估金融股。這就是「前幾大是金融股」的上游成因；配置端的低波動放大（見[配置與輪動 v2](sizing_rotation_v2_design.md)）是下游放大器。

### 1.5 去年同期資料為何缺：流程缺口，不是額度用完

**更正**：2026-10-28 的執行紀錄只寫「FinMind 連線失敗」，以及樣板訊息「FinMind 發生錯誤（例如 HTTP 402 額度用完）」。**402 是訊息裡的舉例，不是已證實的診斷**；本文件不聲稱額度用完。

可確認的是：`cli/fundamental_repair.py`（補抓官方當期與 FinMind 去年同期累計財報，並要求當年金額與官方完全相符才納入）依 `docs/fundamental_data_repair.md` 的說明，**「每日排程不會自動呼叫此入口，僅供互動研究」**。排程只抓 FinMind 候選，沒有把候選核對成去年同期資料的自動步驟。目前只有 7 檔有年增率，與「曾經手動補抓過少數股票」一致。

## 二、設計（兩個部分，可分開做）

### A. 可比性規則：禁止單一不可比訊號支撐 positive

程式端（確定性，不靠 prompt）：

- `analysts.py` 的基本面 brief 為每檔加上程式判定的 `growth_signal_quality`：
  - `comparable`：有 `revenue_yoy_pct` 或 `net_income_yoy_pct`（來自同口徑財報）。
  - `monthly_revenue_only`：只有月營收年增，缺去年同期損益表。
  - `industry_formula_unsupported`：落在 `UNSUPPORTED_INDUSTRY_*`（金融業等），連當期比率都沒有。
- Validator 規則：`growth_signal_quality` 不是 `comparable` 時，`outlook` 不得為 `positive`，只能是 `neutral` 或 `unknown`，並必須在 `data_gaps` 說明原因。這是 fail-closed，不是調整幅度。

Skill 端（`tradeagent/analysis/fundamental/fundamental-analyst/SKILL.md`）同步寫明：月營收年增單獨不足以支持正面；金融業的月營收與一般業的營業收入不可直接比較。

預期效果（推論，需實跑驗證）：17 檔金融業與 143 檔缺去年同期者的 positive 會大量轉為 neutral，`positive` 從 83 檔降到接近 7～44 檔量級，信心等級才會對成長股產生區分力。

**風險**：短期內大量股票變 neutral，買進候選可能不足 30 檔，Guard 的 `min_positions` 會擋。所以 A 必須與 B 一起規劃，或先以影子回放估算候選數。

### B. 補齊去年同期資料：把 repair 納入排程

- 在每日資料階段（`start.sh daily` 之後、建立 Snapshot 之前）加一個**選配、可關閉**的步驟呼叫 `cli/fundamental_repair.py`，只補 `COMPARISON_STATEMENT_MISSING` 的股票，逐批、有速率上限，失敗一律降級不中斷（與既有 FinMind 候選步驟相同的 fail-closed 慣例）。
- 保留既有的硬規則：當年金額必須與官方完全相符才納入，晚於 `decision_cutoff` 的資料一律拒絕。
- 金融業（17 檔）**不會**被 B 修好，因為缺的是公式支援，不是資料。若要讓金融業有可比指標，需另外定義金融業專用指標（淨收益年增、利息淨收益、費用收入比），屬獨立工作。

## 三、建議順序

1. 先做 **A 的程式判定與 brief 欄位**（只標記，不改變 outlook 規則），實跑一天收集 `growth_signal_quality` 分布。
2. 依分布決定 Validator 是否直接禁止 positive，或先只要求在 `data_gaps` 說明。
3. 再做 **B**，等去年同期覆蓋率上來後重估 A 的門檻。
4. 金融業專用指標最後做。

## 四、不在本計畫範圍

- 配置權重與低波動偏好：見[配置與輪動 v2](sizing_rotation_v2_design.md) 的 PR-4。
- 技術分析師（分布已有區分力，negative 57／neutral 48／positive 44）。
- 競賽硬規則、費稅、現金與 Guard。
