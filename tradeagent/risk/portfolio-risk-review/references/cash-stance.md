# 配置前現金姿態：欄位與閘門規則

主控提示會指出本次簡報含哪些欄位；沒有的欄位就不適用該節。所有比例、格點與門檻都由程式計算，你只選列舉並說明理由。

## 固定輸出

- `level`：`aggressive`／`neutral`／`defensive`。
- `breadth`：`concentrated`（集中）／`balanced`（平衡）／`diversified`（分散）。對應檔數由 Policy 的 `positions_by_breadth` 決定，入選的挑選與排序由程式依交易 Agent 的信心等級 high→medium→low 完成，你不需要推論是哪幾檔。檔數是上限不是目標：買進候選少於目標檔數時程式只買那些。高信心候選明確且集中時偏集中；高信心少、看法分歧或事件風險高時偏分散。
- `rationale`：同時說明現金姿態與持股廣度的理由。
- `evidence_ids`：只從簡報的 `citable_evidence_ids`（含 `market_level_evidence_ids`）或公司事件 findings 的 `evidence_ids` 中選。

## 判斷依據

- `regime_assessment`：程式依交易池廣度判定的市場狀態。
- `market_sentiment`：整體台股情緒（全市場新聞，不可用時為恐懼貪婪指標）；`status` 非 `available` 時只代表沒有情緒結論。
- 分析師看法分布、交易狀態計數、公司事件（含 findings 與 data_gaps）、帳戶現金與持股。
- `drawdown_control`：程式從帳本 NAV 歷史計算的峰值回撤；`stance_floor` 不是 `aggressive` 時，姿態至少要和它一樣防守（程式強制，更積極即退回重答）。
- `cash_buffer_by_stance` 與 `cash_ratio_now`：各姿態的現金緩衝與目前現金比例，是姿態的代價——目前現金低於某姿態的緩衝時，選該姿態會讓買單被緩衝擋掉（出場賣股收入買的替補除外），把這個後果寫進理由。

## 閘門（簡報有該欄位時才適用）

- **`market_level_evidence_ids`（決策輸入帶 `evidence_policy: typed_evidence_v1`）**：`evidence_ids` 至少一個市場層級證據——全市場情緒或恐懼貪婪成分的 `evidence_id`、`overnight_market.components` 的 `evidence_id`，或 `regime_assessment.evidence_id`（`market-regime:pool-breadth`）；個股行情證據只作補充，單獨引用某幾檔的行情不能成立整體姿態。`market_sentiment.status` 為 `available` 且 `outlook` 為 `negative` 時，姿態為 `neutral` 或 `defensive`（程式不接受 `aggressive`），`rationale` 說明依據。
- **`overnight_market`**（隔夜市場背景，程式依 overnight-v1.1 計算：美股指數 ^GSPC、^IXIC、^SOX，台股代理 EWT／TSM，日韓指數 ^N225／^KS11，VIX，與夜盤結束後才有的台指期夜盤 `night_session`；舊封存 overnight-v1 只有美股與台股代理）：只是現金姿態的背景之一。`status` 非 `available` 時沒有結論；`night_session` 為 null 代表夜盤不可用（例如平日 21:00 夜盤還在進行），維持不知道；`agreement` 只是各方向訊號的同向程度，不是預測。它與 `market_sentiment` 是兩回事，只用於整體姿態，不用來推論個股或預測今日台股指數方向。引用時只用 `overnight_market.components` 的 `evidence_id`（格式 `overnight:<來源>:<代號>:<雜湊>`）。
- **`high_conviction_cash_floor_by_stance`**（Policy 啟用高信心現金下限）：該姿態下高信心買單可動用原有現金到這條較低的下限（姿態緩衝 × 固定比例，程式計算），中低信心仍只能用賣股收入；防守姿態的程式減碼也只補到這條線，所以防守的實際現金落點是這條下限而非完整緩衝，理由要反映這一點。
- **`cash_grade_allowed_by_stance`**（Policy 啟用 `cash_ratio_range`，此時不附 `cash_buffer_by_stance`）：除了姿態，另選姿態內子等級 `cash_grade`：`lower`、`middle` 或 `upper`（由低到高），現金比例由 `cash_grade_grid` 對應。只能選 `cash_grade_allowed_by_stance` 列出的該姿態子等級：單日只限制現金拉高，今日上限 = max(該姿態 lower, `cash_ratio_anchor.ratio` + `max_daily_raise`)，往下不限，切到更防守的姿態時當天只到該姿態 lower；選了不可選的子等級即退回重答，程式不會替你夾到邊界。`cash_ratio_anchor` 是前次決策的現金比例（`status=account_cash` 時為本次起始現金比例），只是背景、不是證據、不可引用。防守姿態時程式會依 ATR 賣股把現金補到所選比例，子等級越高、當天被逼出的賣單越多；`rationale` 說明為何選這個子等級與這個代價。沒有此欄位時輸出裡沒有 `cash_grade`。

- **`evidence_recency`**（Policy 啟用 `cash_stance_recency`，簡報第一個鍵；需 `typed_evidence_v1`）：程式依**內容時間**（新聞發布時間、時段收盤時間、資料日，不是抓取時間）把市場層級證據分三組，界線是台股最近交易日 13:30。`after_latest_close`＝最近收盤之後才出現的資料（週末新聞、美股與日韓收盤、VIX、夜盤）；`latest_session`＝最近交易日當天的資料；`accumulated`＝累積性走勢（`market-regime:pool-breadth`，MA20／MA60 廣度）。每筆附 `hours_before_cutoff`（距截止小時數，程式算）。另輸出列舉 `recency_reconciliation`：`newest_confirms`（最新一組與走勢方向一致）、`newest_contradicts`（相反）、`newest_insufficient`（最新一組沒有證據）。`after_latest_close` 有證據時不得填 `newest_insufficient`，且 `evidence_ids` 至少引用其中一個；沒有證據時只能填 `newest_insufficient`。`after_latest_close` 裡的事件與新聞（`kind=news`）發生在最近一次台股收盤之後，走勢與廣度還沒反映它們：判斷「情況有沒有改變」時，預設以這些事件與新聞為主，利多與利空同樣適用；不採用時，`rationale` 要說明理由（例如消息彼此矛盾、只是評論沒有新事實、措施尚未生效）。隔夜行情（`kind=overnight`）仍只是背景，不因為較新就升級成事件；累積走勢要看一段時間才有意義，不因為較舊就作廢。新不等於已被市場消化，也可能失敗，把這個風險寫進理由。列舉與姿態之間沒有對應要求，現金姿態與子等級仍由你判斷，程式不因為最新一組而放寬任何現金格點或上限。
- **`turn_facts`**（Policy 啟用 `cash_stance_turn_facts`，簡報 `regime_assessment` 旁；需 `typed_evidence_v1`）：程式依行情算的交易池轉向事實（最近兩日池等權報酬、連續上漲日數、站回 MA5／MA20 的股票比例、MA20 之上比例的 5 日變化、20 日新低比例、regime 連續 bear 天數，缺資料記入 `data_gaps`），**是事實，不是訊號**。`regime_assessment` 是均線廣度的累積水位，可能落後短期價格變化；短期反彈也不保證趨勢反轉。只用本次截止前可得的證據，不補入未提供的歷史案例、後續報酬或費稅數字。另輸出列舉 `turn_assessment`：`no_turn_evidence`、`turn_unconfirmed`、`turn_confirmed`，只是紀錄你怎麼看，**不要求與現金姿態一致**，程式不因它放寬或收緊任何現金格點、上限或回撤下限。考量反彈失敗及交易成本的風險，不自行填補數值。

## 正例

- 市場狀態 neutral、全市場情緒 `negative`、回撤未達門檻、分析師看法分歧：`level=neutral`、`breadth=balanced`，`evidence_ids` 含情緒成分與 `market-regime:pool-breadth`，理由寫「情緒負面但廣度未轉弱，保留中性現金；看法分歧，持股維持平衡」。
- `cash_ratio_range` 啟用、前次比例在 aggressive 格點、今天要切到 `defensive`：當天只能選 `lower`，理由寫「切到防守，今日先到防守下限，程式會依 ATR 補現金，避免一天逼出過多賣單」。
