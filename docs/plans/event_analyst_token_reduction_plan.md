# 事件分析師 token 降量計畫（草案，未實作）

狀態：草案，2026-11-17 深夜撰寫；**只有計畫，沒有改任何程式、契約、設定、Skill 或其他文件**。
所有「預估省多少」都是用一個封存 run 的實測數字推算的區間，不是實測結果。

與既有文件的關係：

- [`token_cost_reduction_plan.md`](token_cost_reduction_plan.md) 4.1 已經提過三種沿用：(a) 同日比對放寬、(b) 跨交易日逐檔沿用、(c) 逐則沿用。其中 (c) 被列為「比賽期間不建議」。
  本計畫的方案 2 就是 (c)，第 3.7 節說明這次為什麼重新評估，以及為什麼仍建議先做 (b)。
- [`context_slimming_plan.md`](context_slimming_plan.md) 4.6 說明逐則沿用需要改每批的即時驗證。本計畫第 3.3 節查證了 Validator，給出不必改 Validator 的做法。
- 單價、寫入／輸出拆分與每字元 token 數，沿用 `token_cost_reduction_plan.md` 第 2 節的實測值：sonnet 寫入 $2.50／M、讀取 $0.10／M、輸出 $10／M，事件簡報每字元約 0.79 token。

## 1. 結論先講

1. **事件分析師的錢幾乎都花在「每個目標交易日的第一次 run」**。同一個目標交易日內的重跑，現行 `event_reuse` 已經沿用：11/16 23:01 沿用 149 檔，事件花費 $0；11/17 07:03 沿用 112 檔，花費 $2.95。
   所以「每天重跑 2～6 次 × $8」高估了事件分析師的占比。重跑真正花的錢是其他階段：每次約 $10～12，包括交易 Agent $3.3、多空 $3.9、技術與基本面 $2.4、新聞標註 $1.3。
2. **方案 1（縮短公告回溯期）和 quality_dip 互相牽動**。`dip_facts` 判斷「新壞事」時，看的是近 20 個交易日（約 28～30 個日曆日）內新可得的 high 事件，而 `agent_swap.quality_dip` v4 已經開啟，會依這個結果決定能不能換股。
   回溯期若短於約 30 天，第 15～30 天內的 high 負面事件會從報告消失，股票就可能從「有新壞事」變成 quality_dip 候選，屬於**加風險**方向。
   要不動到 quality_dip，回溯期得至少約 30 天，這時只省約 $2／run。改成 14 天約省 $3.9／run，但同時要接受上述行為改變，還會讓本次 16 則 high 事件中的 9 則不再進報告。
3. **方案 2（逐則跨天沿用）省最多**。11/17 21:17 這次只有 37 則新重大訊息（分布在 29 檔）和 73 則新聞是新的，事件分析師由 $8.3 估計降到 $1.0～1.5。
   但它要改簡報、提示、Skill 與每批的合併流程。Validator 和契約可以不改，只是工作量大約一整天，**趕不上 11/19**。
4. **能趕上 11/19 的只有「方案 2 的第 0 步：跨交易日逐檔沿用」**，也就是 `token_cost_reduction_plan.md` 4.1(b)。它是現行 `event_reuse` 的延伸：簡報、Validator 與契約都不改，失敗時照樣 fail-closed。
   用 11/17 的資料實測：約省 $4.1～5.5（$8.3 → $2.8～4.2）。週末之類沒有新公告的日子最多省 $8.2，例如 11/15 → 11/16 的 149 檔全部相同。
   前提是要做第 3.0 節的「鏈狀退回」；不做的話，上線第一晚只省約 $1。
5. **0 程式碼、立刻能做的，是少開新 run**。失敗時用 `--resume` 續跑，不要手動另開一個新的 run；每避免一次重跑約省 $10～12，比任何事件方案都多。
6. 推薦順序：0 程式碼的做法 → 第 0 步（跨日逐檔沿用，11/18 晚上前）→ 視用量再決定是否做方案 2（逐則）→ 方案 1 比賽期間不建議做（第 6 節）。

## 2. 現況與實測（11/18 決策：`artifacts/daily/2026-11-18/daily-20261117-211732`）

| 項目 | 數字 |
| --- | --- |
| 整個 run 估計 | $20.49 |
| 事件分析師 | 25 次呼叫（24 批），$8.30：寫入 1.91M token（$4.78）、讀取 0.80M（$0.08）、輸出 0.345M（$3.45） |
| 事件簡報 | 149 檔、1,654 則重大訊息、73 則新聞；逐檔列壓成單行 JSON 約 1.92M 字元 |
| 公告可得日期（台北日曆日，相對 `decision_cutoff`） | 7 天內 440 則、14 天內 795 則、21 天內 1,053 則、30 天內 1,207 則、最早 2026-09-14 發布 |
| 事件分析師的 high | 16 則：14 天內 7 則，14 天以上 9 則（9 則都是 positive 或 neutral，例如 2330 的 33 天、6239 的 63 天） |
| 與前一個目標交易日比（11/16 22:20 → 11/17 21:17） | 新增重大訊息 37 則，分布在 29 檔，沒有被移除的；新聞 15 → 73 則，73 則全部是新的；「事件與新聞逐檔相同」94／149 檔 |
| 同一目標日內重跑 | 11/16 23:01 沿用 149 檔，事件 $0；11/17 07:03 沿用 112 檔，事件 $2.95 |

計數口徑：用 `available_at` 換成台北日曆日，與 `decision_cutoff` 的台北日期相減。這個口徑與使用者查到的 440／795 一致。改用 `published_at` 則是 349／760／1,207。

成本模型（用來推算各方案）：

- 寫入 ≈（簡報字元 × 0.79 ＋ 批數 × 16k）× $2.5／M
- 輸出 ≈ 則數 × 200 × $10／M
- 讀取 ≈ 批數 × 32k × $0.1／M

把全量代入，得到 $8.29，與實際的 $8.30 相符。

## 3. 方案 2：跨天沿用（以單則公告為單位）

### 3.0 第 0 步：跨交易日「逐檔」沿用（建議 11/18 晚上前上線）

做法：放寬現行 `automation/event_reuse.py` 的「只在同一交易日資料夾」限制，其餘規則不變。

- **來源**：往前找最近一個已完成的 run，可以在前一個目標交易日的資料夾裡；最多回看幾個日曆日由常數或 CLI 參數限定，數字由人填。
  前一次 run 必須有 `analyst_event.json`，而且 `status=completed`。這個檔案只有在合併驗證通過後才會寫出，所以有這個檔就代表當時驗證過。
- **比對**：沿用現行的 `row_digest`，不看 `data_availability`。`events`、`news` 與 `financial_context` 都要逐檔相同才沿用。
  `financial_context` 留在比對裡比較保守：11/17 的 94 檔中有 88 檔的財務脈絡也相同。
- **要先補的缺口**：`_previous_rows` 只讀 `brief_event_b*.json`，但沿用掉的股票不會出現在 brief 裡，例如 07:03 那次只有 37 檔有 brief。`previous_run_dir` 又會挑最近一個「有任何 brief」的 run。
  所以如果只看最近一個 run，跨日沿用的比對基準只剩那幾十檔。用 11/17 07:03 → 11/17 21:17 實測，只有 24 檔可以沿用，第一晚幾乎不省錢。
  解法分兩部分：
  1. **往後**：每個 run 另存 `event_row_digests.json`，形狀 `{symbol: {"row_sha256": ..., "first_classified_run": ...}}`，涵蓋全部 149 檔，包括 Agent 判的、沿用的和程式輸出 unknown 的。
     沿用時，`first_classified_run` 照抄來源的值；Agent 重判時改成本次 run。K 的年齡檢查就靠這個欄位。
  2. **第一晚的退回做法（舊 run 沒有這個檔）**：沿著同一條鏈往前找，每檔取「最近一個有該檔 brief 列的 run」的列，item 則一律取鏈上最新 run 的 `analyst_event.json`。
     這樣做是正確的：同日沿用只有在列完全相同時才複製 item，所以鏈上的列與 item 會一致。
     用 07:03（37 列）加上 11/16 22:20（149 列）對 11/17 21:17 實測，有 **112 檔**可以沿用，要重送 37 檔、588 則。
     舊 run 沒有 `first_classified_run`，所以以它找到列的那個 run 當起點，這樣算出的年齡偏保守。
- **重驗**：沿用現行 `_reusable_event_items`，用本次 bundle 與 `AnalystReportValidator` 重驗。建議順手改成「逐檔重驗、驗不過的那一檔改送 Agent」，取代現在的「整批不沿用」。
- **紀錄**：`event_reuse.json` 多記 `source_run`、`source_target_date`、`scope: cross_day`。`pipeline.json` 記本次用的範圍。
- **開關**：在 `scripts/run_daily_pipeline.py` 加 `--event-reuse-scope same_day|cross_day`。是否改成預設 `cross_day` 由使用者決定，這是原則變更，見第 7 節第 2 題。
- **契約**：`analyst_event.json` 形狀不變、`team_inputs` 不升版。Finalizer 與重建只驗報告內容、不管是誰產生的，所以舊 run 逐位元不變。
- **預估省**：
  - 以前一晚為基準（11/16 22:20 → 11/17 21:17）：要重送 55 檔、914 則，約 $4.2，省約 $4.1。
  - 以鏈上最新列為基準（加上 11/17 07:03）：要重送 37 檔、588 則，約 $2.8，省約 $5.5。
  - 沒有新公告也沒有新聞變動的日子最多省 $8.2。新聞量突然增加的日子省得較少，例如 11/17 新聞從 15 則變成 73 則。
  - 若不做上面的鏈狀退回，上線第一晚只能沿用約 24 檔，省約 $1；要等隔天晚上才會有完整效果。
- **品質風險**：中等偏低。輸入逐檔完全相同時，沿用的期望品質不比重判差，但會把一次抽樣的結果固定下來；同一輸入重判時，high 的一致率只有 68.5%（見 `token_cost_reduction_plan.md` 2.5）。
  緩解做法：同一份判斷最多沿用 K 個交易日，以第一次判斷的 run 起算，不以最近一次沿用起算；K 由人填。

### 3.1 逐則沿用：身分鍵

- 重大訊息的 `evidence_id` 是 `document:<資料庫列號>`（`data/evidence.py`）。公告內容一更正，`corporate_collector` 就新增一個版本、拿到新的列號，所以內容不同就不會是同一個 ID。
- 身分鍵用 **(symbol, evidence_id, content_sha256)**，其中 `content_sha256` 取自 Snapshot 文件。三者都相同才算同一則：
  - 列號相同但雜湊不同（理論上不該發生）時，視為新公告。
  - 資料庫重建造成列號全部改變時，全部視為新公告，等於多花一次錢，屬於 fail-closed。
- 新聞的身分鍵用 **(symbol, evidence_id, title, publisher)** 的 canonical 雜湊，來源是 `company_news_input` 的 `news_by_symbol`。新聞的 `evidence_id` 本身已由 canonical content ID 推出，再加標題與媒體是防止同 ID 但內容不同。
- 來源 run 的 `content_sha256` 一律從來源 run 自己的 `decision_input.json` 讀，不從本次 bundle 推回去。

### 3.2 流程

1. **找來源**：最近一個已完成的 run（規則同 3.0）。要讀它的 `analyst_event.json` 和 `decision_input.json`，兩者任一讀不到、無法解析、`status` 不是 completed，或報告的 `bundle_hash` 和來源 bundle 對不上，就整個不沿用。
2. **逐則分類**：本次 bundle 每檔的應分級集合 E，由 `AnalystReportValidator.events_by_symbol` 的同一個函式算出；如果方案 1 開啟，這裡已經套用回溯期。
   - 沿用集合 R：身分鍵與來源相同、來源報告裡有這則的分級、年齡沒有超過上限 K、而且不在強制重判清單內。
   - 新集合 N = E − R。
3. **整檔沿用**：N 為空，且該檔的 E、新聞與 `financial_context` 都與來源相同時，整個 item 沿用，包括 `outlook`、`findings` 與 `data_gaps`，等於第 0 步。
4. **部分沿用**：其餘有 N 的股票交給 Agent，簡報逐檔列改為：
   - `events`：只放 N，呈現格式與現在相同（`items_v1` 正文）。
   - `prior_events`：放 R，每則只給 `evidence_id`、`title`、`published_at`、`materiality`、`direction`、`summary`，以及來源的 `decision_cutoff`。
     提示要寫明：「這些是已分級、沿用的結果，可以引用；只有新事件讓舊事件的意義改變時，才把它放進 `events` 重新分級。」
   - Agent 照常寫整檔的 `outlook`、`findings` 與 `data_gaps`，判斷時要同時看新舊事件。
5. **每批合併**：在 `agent_step` 的 seal 函式（`team_stages.py` 第 351～356 行的 lambda）裡，程式把 R 中「Agent 沒有重新分級」的分級原樣併進該檔的 `events`，再交給現有的 `AnalystReportValidator(symbols=batch)`。
   - Agent 重新分級了某則舊事件時，以 Agent 的版本為準，程式不重複插入，所以不會觸發「重複分級」錯誤。
   - Agent 漏掉某則新事件時，合併後的「未分級全部重大訊息」檢查照樣會擋下並回饋。
6. **全份驗證**：照現行流程，合併整份報告後再用 `AnalystReportValidator` 驗一次，然後寫入 `analyst_event.json`。Finalizer、`stance.py` 與 `event_sentiment_report.py` 的重驗都不用改。
7. **串接**：本次的 `analyst_event.json` 本來就含有沿用的分級，下一次可以把它當來源。年齡上限 K 以「第一次判斷的 run」起算，這個資訊記在封存檔裡（見 3.4）。

### 3.3 Validator 與契約（查證結果）

- `AnalystReportValidator._validate_events` 要求「每則都剛好分級一次」，而 `_validate_company_event` 要求「方向要有事件引用」。這兩條驗的都是**合併後**的 item。
  所以只要合併在 seal 階段先做，**Validator 不必改**，每批即時驗證也不必另寫一套。`context_slimming_plan.md` 4.6 擔心的就是這一點，查證後確認不需要改。
- `event_analyst_schema` 可以不變。`_batch_corroboration_ids` 本來就包含整批的全部事件 ID，所以 Agent 用舊事件當 `corroborated_by` 仍然合法。
- `analyst_event.json` 形狀不變、`team_inputs` 不升版。沿用不改變任何重建行為：Finalizer 只驗報告與 bundle 是否一致，不驗是誰產生的。
- **要不要做成 Policy 版本閘門**：這個方案會改變 Agent 看到的內容，舊事件只剩摘要，所以建議走 Policy 閘門 `position_sizing.event_item_reuse`，形狀 `{"version": "v1", "max_age_trading_days": K}`。
  - 只由人設定，加進 `strategy_reflection/contracts.py` 的 `FORBIDDEN_PREFIXES`，並在 `sizing.py` 登記 `GateRule`。
  - 沒有這個欄位就走現行路徑，簡報逐鍵不變。
  - 不新增 Agent 輸出欄位、Finalizer 不重算，屬於只影響簡報與流程的閘門，比照 `cash_stance_brief`。
  - 若使用者認為這只是省錢的流程設定，也可以只做 CLI 開關，比照 `--event-reuse`，由使用者決定。

### 3.4 封存格式

每個 run 另存 `event_item_reuse.json`，不進報告本體、不進雜湊，比照現行的 `event_reuse.json`：

```json
{
  "schema_version": "event_item_reuse_v1",
  "source_run": "daily-20261117-070305",
  "source_report_sha256": "<來源 analyst_event.json 的 content_sha256>",
  "source_bundle_sha256": "<來源 decision_input 的 bundle_sha256>",
  "reused_items": ["2330.TW", "..."],
  "reused_events": [
    {"symbol": "2330.TW", "evidence_id": "document:123", "content_sha256": "...",
     "first_classified_run": "daily-20261116-222032", "age_trading_days": 1, "overridden_by_agent": false}
  ],
  "agent_symbols": ["..."],
  "forced_rejudge": [{"symbol": "...", "evidence_id": "...", "reason": "max_age|held_high|..."}]
}
```

### 3.5 fail-closed

| 情況 | 處理 |
| --- | --- |
| 找不到來源、來源檔毀損、`status` 不是 completed、`bundle_hash` 對不上 | 整個不沿用，全部交給 Agent |
| 某則的身分鍵不同、來源報告沒有這則、年齡超過 K | 這一則列入 N，交給 Agent |
| 沿用的整檔 item 用本次 bundle 重驗不過（例如引用的財報編號已經不在） | 這一檔改送 Agent，不影響其他檔 |
| Agent 輸出含不屬於 E 的 ID，或漏掉 N | 現有 Validator 回饋，走修正循環，用盡就照現行方式停止 |
| 合併後全份驗證失敗 | 照現行流程丟出 `DailyPipelineError`，不會產生 fallback 以外的決策 |
| 簡報格式換版（例如加入 `prior_events`） | 第 0 步的逐檔雜湊全部不同，那一次全數重跑，只多花一次錢 |

### 3.6 與現有 `event_reuse` 共存

1. 第一層：現行的同日逐檔沿用，或第 0 步的跨日逐檔沿用，整檔沿用最便宜，先做。
2. 第二層：剩下的 `agent_symbols` 才進入逐則沿用。
3. 兩層各自寫自己的封存檔（`event_reuse.json`、`event_item_reuse.json`），都受 `--event-reuse` 總開關控制。`--no-event-reuse` 時兩層都關，結果逐位元等於現行的全量重判。

### 3.7 預估、風險與為什麼重新評估

- **預估省**（11/17 這組）：新事件 37 則加新聞 73 則，交給 Agent 的約 55 檔，含 `prior_events` 摘要約 800 則（每則約 250 字元），約 4～5 批。
  - 寫入約 0.37M token（$0.9），輸出約 30k（$0.3），合計約 $1.2，比全量的 $8.3 **省約 $7**。
  - 比第 0 步再多省約 $1.5～3／晚間 run。比賽剩約 6 個晚間 run（11/27 結束），合計再多省約 $10～18。
- **品質風險**：
  1. 舊分級固定下來，因為 high 的重判一致率只有 68.5%。緩解方式是 Agent 可以覆寫，加上年齡上限 K。
  2. 舊摘要可能含「近日」「本週」之類相對時間，所以簡報要附來源的 cutoff。
  3. Agent 只看摘要、不看原文，就可能誤解舊事件。可以考慮讓持股與前次 high 事件一律附原文重判，這幾則量很小。
- **為什麼 `token_cost_reduction_plan.md` 說比賽期間不做，這裡仍然列出**：當時估計要改契約。查證後發現 Validator 不必改（3.3），風險從「契約」降成「提示與 Skill」。
  但工作量仍約一整天，而且要改 `tradeagent/analysis/event/event-analyst/SKILL.md` 與 `references/data-rules.md`。所以結論不變：**先做第 0 步；第 0 步上線後若用量仍吃緊，再做逐則沿用。**

## 4. 方案 1：縮短公告回溯期（版本閘門）

### 4.1 閘門放在哪：必須讀得到 bundle

`AnalystReportValidator(bundle, name)` 只拿 bundle，而它在三個地方被呼叫：

- `stance.py:200`：多空研究員的輸入驗證
- `event_sentiment_report.py:29`
- Finalizer 透過 `ResearchDebateBundleValidator` 呼叫

所以回溯期必須能**只從 bundle 讀到**。做法比照 `fundamental_period_policy`／`evidence_policy`（bundle 頂層的版本閘門），以及 `benchmark_facts`（從策略樣板讀出、建 bundle 時寫入）：

1. 人填的數字放在策略樣板 `config/decision_policy.json` 的 `position_sizing.event_lookback`，形狀 `{"version": "v1", "calendar_days": N}`。
   在 `sizing.py` 登記 `GateRule`，並加進 `FORBIDDEN_PREFIXES`，策略反思不能改。
2. `daily_pipeline.py` 約第 234 行已經在讀樣板的 `benchmark_facts`。用同樣的方式讀 `event_lookback`，依序傳給 `service.build_input_file` 和 `DecisionInputBuilder`。
   bundle 寫入頂層欄位 `event_lookback_policy: {"version": "event_lookback_v1", "calendar_days": N, "basis": "available_at_taipei_date"}`，這個欄位進 `bundle_sha256`。
3. `input_validator.py` 的欄位白名單加入這個欄位，並檢查形狀：版本必須是列舉值，`calendar_days` 是 1～90 的整數，否則拒絕。
4. Finalizer 加雙向檢查：Policy 有 `event_lookback`，bundle 就必須有 `event_lookback_policy` 且天數相同，反之亦然。比照 `fundamental_period_policy` 與 `team_inputs` 2.6 的雙向檢查。
5. 沒有這個欄位的舊 bundle、舊 Policy 與舊 run 一律走原路徑，逐位元不變。
6. 實際使用的天數一律由建 bundle 的程式算好，寫進 `event_lookback_policy.calendar_days`；選了 4.3 的「自動延伸」時另記 `requested_days` 與 `extended_to_dip_window: true`。
   所有 Validator 只讀這個欄位，不讀 Policy，也不自己用價格序列重算。Finalizer 再用 Policy 與 bundle 價格序列重算一次，並和這個欄位比對。

### 4.2 只在一個地方過濾，再逐一查繞路的地方

- **唯一的過濾點**：`analysts.py` 的 `_documents(bundle, "material_event")`。在現有的「`available_at` 晚於 cutoff 就拒絕」與「`published_at` 晚於 `available_at` 就拒絕」兩項檢查**之後**，再依 `event_lookback_policy` 過濾，原本的 fail-closed 檢查都保留。
  比較的是 `available_at` 換成台北日曆日，與 bundle 的 `decision_cutoff` 台北日期之差；不用執行當下的時鐘。
- 以下地方都經由 `_documents` 自動跟著過濾：

| 讀事件的地方 | 位置 | 處理 |
| --- | --- | --- |
| 事件簡報 | `build_analyst_brief` 第 317 行 | 自動 |
| 應分級集合、引用範圍、佐證 | `AnalystReportValidator.__init__` 第 620～626 行 | 自動，所以簡報和 Validator 一定一致 |
| 程式直接輸出 unknown、批次權重 | `team_stages.py` 第 245～260 行，讀 brief 列 | 自動；7 天時 33 檔會變成程式輸出 unknown |
| high 事件交給多空研究員 | 多空 brief 的 `analysts.event` 讀報告，`high_materiality_events` 也讀報告 | 自動，但視窗外的 high 會消失（見 4.4） |
| 風險 Agent 的公司事件摘要 | `cash_stance_brief.company_events_summary` 讀報告 | 自動 |
| `event_direction`／quality_dip 新壞事 | `dip_facts._event_flags`，用報告逐則判斷 | **會改變行為**，見 4.3 |
| 同日重用 | `event_reuse.row_digest`，讀 brief 列 | 自動；開閘門那一次全數重跑 |

- 以下地方**不經過** `_documents`，要另外決定怎麼處理：

| 位置 | 現況 | 建議 |
| --- | --- | --- |
| `compact_event_batch`（第 477 行直接掃 snapshot） | 只有 `scripts/evaluate_event_brief.py` 用，不在每日鏈 | 不改，或順手改成走 `_documents` |
| `dip_facts._Documents`（第 394 行） | 只是 evidence → 可得時間的查表 | 不改 |
| 多空 brief 的 `evidence_kinds`（`stance.py` 第 135～138 行，讀 `DecisionContext`） | 仍會列出視窗外的 `document:` ID，研究員可能引用看不到內容的公告；`StancePacketValidator` 會放行，因為它在 bundle 裡 | 建議閘門開啟時，簡報只列視窗內的事件 ID（只改簡報）。不建議改 Validator 去拒絕，那會動到多空與交易的引用契約 |
| `DecisionContext` 證據索引 | 包含全部 Snapshot 文件 | 不改，Snapshot 不可變，舊公告仍是合法事實 |

- 公告被更正時會新增一列、拿到新的 `available_at`，所以更正過的舊公告會重新進入視窗。這是正確行為：更正本身就是新資訊。

### 4.3 與 quality_dip 的牽動（決定天數的關鍵）

- `dip_facts.WINDOW_DAYS = 20` 個交易日。`base` 是往前第 21 個交易日，約 28～30 個日曆日，遇到連假還會更長。新壞事的判準是「`base` 之後新可得的 high 事件」，事件面有兩種：
  - 舊判準：整檔 `event_outlook` 為 negative。
  - `event_direction` 已開啟：該則的 `direction` 為 negative。
- `_candidate` 在 `new_adverse_facts` 非空時一律給 `none`，而 `agent_swap.quality_dip` v4（已開啟）依這個結果決定能不能換入。
- 所以回溯期短於 `cutoff − base` 時，第 N+1 天到 `base` 之間的 high 負面事件不會進入報告，股票就可能從 `none` 變成 `market_dragged` 或 `oversold`。**這是加風險方向的配置改變**，不只是紀錄改變。
  本次 run 的 9 則舊 high 事件全是 positive 或 neutral，所以今天沒有實際影響，但不能保證以後也沒有。
- 三種處理方式，由使用者選（第 7 節第 1 題）：
  1. **N ≥ 30**：不動 quality_dip。這時 1,207 則、18 批，約 $6.25，省約 $2.0／run。
  2. **N 自動延伸**：建 bundle 時，若樣板開著 `quality_dip_facts`，就以 `max(N, cutoff_date − base_date)` 當實際回溯期（由 bundle 價格序列確定性算出），寫進 `event_lookback_policy`（見 4.1 第 6 點）。
     Validator 只讀寫好的天數；Finalizer 用 Policy 重算比對。quality_dip 開啟期間實際上就是約 30 天，效果與選項 1 相同；quality_dip 關閉後，N 才真正生效。
  3. **N = 14，並宣告行為改變**：省約 $3.9／run，但 quality_dip 的新壞事檢查只看近 14 天。這要人明確接受，並在 `docs/decision-contracts.md` 寫明。

### 4.4 被截掉的舊公告對判斷的風險

- 本次 16 則 high 事件中有 9 則超過 14 天，其中 8 則 positive、1 則 neutral，例如 2330、2303、1303、6239。14 天的回溯期會讓多頭研究員失去這些「仍在發酵的利多」。
  7 天時會有 33 檔完全沒有事件，由程式輸出 unknown，多空研究員因此少了公司事件面的依據。
- 長效事件最容易被截掉：併購、增資或私募、資本支出、庫藏股、股利政策、訴訟、重大合約。它們的影響常常超過兩週。
  相反地，視窗內留下來的多半是例行公告，例如董事會日期、法說會通知、取得有價證券。整檔 `outlook` 可能因此被例行公告主導。
- 資料面：`analyst_data_availability` 的筆數仍然是資料庫的全部存量，可能出現「資料庫有 60 筆，查詢視窗內 0 筆」，要在簡報頂層說明回溯期。

### 4.5 預估省（用本次 brief 依各回溯期重新過濾後實測字元數，加上 4.3 的成本模型）

| 回溯期 | 則數 | 交給 Agent 的檔數 | 批數 | 估計花費 | 省 |
| --- | --- | --- | --- | --- | --- |
| 現行（全部） | 1,654 | 149 | 24 | $8.29 | — |
| 30 天 | 1,207 | 148 | 18 | $6.25 | $2.0 |
| 21 天 | 1,053 | 148 | 16 | $5.54 | $2.8 |
| 14 天 | 795 | 146 | 13 | $4.37 | $3.9 |
| 7 天 | 440 | 116 | 8 | $2.68 | $5.6 |

`EVENT_BATCH_MAX_EVENTS=80` 與 `EVENT_BATCH_SIZE=15` 決定批數的下限，例如 795 則至少要 10 批，149 檔至少要 10 批。

## 5. 兩者疊加

- **邏輯上不衝突**：方案 1 決定應分級集合 E，方案 2 決定 E 裡哪些沿用。沿用時以本次的 E 為準，已出視窗的舊分級自然不會被併入，Validator 也不會要求。
- **開方案 1 的那一次**，brief 列改變，同日逐檔沿用與第 0 步全部失效，全數重跑一次，只多花一次錢。之後恢復正常。
- **方案 2 上線後，方案 1 剩下的邊際效益很小**：只剩 `prior_events` 摘要的 token，約 $0.1～0.3／run，還要付出 4.3 與 4.4 的代價。所以若做方案 2，就不建議再做方案 1。
- 方案 1 改 N 時，整檔 `outlook` 的依據改變，所以該檔不能整檔沿用。只要 E 改變，3.2 第 3 步本來就不會整檔沿用。

## 6. 實作順序、上線時程與推薦

| 順序 | 項目 | 省／首次 run（估算） | 工作量 | 11/19 前（11/18 21:00 run） |
| --- | --- | --- | --- | --- |
| 0 | 0 程式碼：少開新 run（第 9 節） | 每避免一次重跑 $10～12 | 無 | 今天就能做 |
| 1 | 第 0 步：跨交易日逐檔沿用、`event_row_digests.json` 與鏈狀退回 | 約 $4～5.5（範圍 1～8.2） | 3～4 小時，含測試 | **可以**，前提是使用者同意跨日沿用；鏈狀退回一定要做，否則第一晚只省約 $1 |
| 2 | 方案 2：逐則沿用 | 再多約 $1.5～3（合計約 $7） | 約 1 天，含 Skill 與 A/B 觀察 | 不行，最早 11/20 |
| 3 | 方案 1：回溯期閘門 | 30 天約 $2；14 天約 $3.9 但改變 quality_dip | 約半天到 1 天，含 Finalizer、文件與回滾樣板 | 勉強可以，但不建議 |

**推薦**：0 → 1。第 1 步上線並觀察兩個晚間 run 後，若 Max 用量仍然吃緊再做 2。比賽期間不做 3：它省的錢最少，又會改變 quality_dip 與多頭研究員能看到的利多。

## 7. 要使用者決定的數字（3 題）

1. **方案 1 要不要做？要做的話 N 是多少？** 建議不做；若要做，選 30 或「自動延伸」，不建議 14。14 天會讓 quality_dip 新壞事只看近 14 天，屬於加風險方向。
2. **同不同意跨交易日沿用？** 同意的話，沿用來源最多回看幾個日曆日（建議 4，可以跨週末），以及同一份判斷最多沿用幾個交易日 K（建議 3）。這是原則變更：現行只在同一交易日內沿用。
3. **若之後做方案 2，持股與前次 high 事件是否一律附原文重判？** 建議「是」：量很小（約 16 則加持股），換來 high 不會被固定下來。

## 8. 改動檔案清單（已逐一搜尋確認存在與位置）

**第 0 步**

- `src/etf_agent/automation/event_reuse.py`：`previous_run_dir` 擴大到前幾個目標日資料夾；讀 `event_row_digests.json`；逐檔重驗。
- `src/etf_agent/automation/team_stages.py`：`run_analyst_team` 第 236～260 行存 `event_row_digests.json`；`_reusable_event_items` 第 283～299 行改成逐檔重驗並記錄範圍。
- `src/etf_agent/automation/pipeline_base.py`：第 190、224 行加 scope 參數。
- `scripts/run_daily_pipeline.py`：第 907 行附近加 `--event-reuse-scope`，並在第 1205 行傳入。
- 測試：`tests/automation/test_event_reuse.py`、`tests/automation/test_market_news_stage.py`（`event_reuse` 屬性的相容性）。
- 文件：`docs/operations.md` 第 359 行一段、`docs/token_usage.md`、`src/etf_agent/automation/README.md`。

**方案 2（逐則）**

- 前述 `event_reuse.py`，或新檔 `automation/event_item_reuse.py`（身分鍵、來源讀取、R／N 分類、合併）。
- `team_stages.py`：簡報 `prior_events`、seal 合併、`_EVENT_RULES` 提示。
- 若做成閘門：`decision/sizing.py`（`GateRule` 與 validate）、`strategy_reflection/contracts.py`（`FORBIDDEN_PREFIXES`）、`config/policy_versions/` 回滾樣板。
- Skill：`tradeagent/analysis/event/event-analyst/SKILL.md`、`references/data-rules.md`、`references/event-sentiment-contract.md`。
- 文件：`docs/decision-contracts.md`（事件分析師段）、`docs/status.md`（閘門表）、`docs/architecture.md`、`README.md`、`AGENTS.md`（事件分析師邊界一句）。

**方案 1（回溯期）**

- `src/etf_agent/decision/analysts.py`：`_documents` 過濾、閘門解析函式。
- `src/etf_agent/decision/input_validator.py`：白名單與形狀檢查。
- `src/etf_agent/decision/input_builder.py`：參數與寫入欄位。
- `src/etf_agent/decision/service.py`：`build_input_file`。
- `src/etf_agent/automation/daily_pipeline.py`：約第 234 行，從樣板讀閘門。
- `src/etf_agent/decision/finalization.py`：雙向一致檢查。
- `src/etf_agent/decision/sizing.py`：`GateRule`。
- `src/etf_agent/strategy_reflection/contracts.py`：`FORBIDDEN_PREFIXES`。
- `src/etf_agent/decision/dip_facts.py`：只在選了「自動延伸」時改。
- `src/etf_agent/decision/stance.py`：`evidence_kinds` 簡報過濾。
- `src/etf_agent/automation/team_stages.py`：簡報頂層的回溯期說明。
- 文件與 Skill：`AGENTS.md`（版本閘門總則加 bundle 欄位）、`docs/decision-contracts.md`、`docs/status.md`、`src/etf_agent/decision/README.md`、事件分析師 Skill 的 `references/data-rules.md`。
- 設定：`config/decision_policy.json`，由人在開啟時填寫；回滾樣板放在 `config/policy_versions/`。

## 9. 0 程式碼的替代：少開新 run

事實：同一目標交易日內的重跑，事件分析師已經幾乎不花錢，但每次重跑其他階段仍要約 $10～12。所以減少重跑比任何事件方案都省。建議：

1. **失敗後用 `--resume` 續跑，不要另開新的 run**（`scripts/run_daily_pipeline.py` 第 887 行，不帶 RUN_ID 時續跑今天最近一個未完成的 run）。續跑會沿用已完成的階段；另開新 run 會從頭再付一次技術、基本面、多空與交易 Agent 的錢。
2. **重跑前先看失敗原因**。例如 11/17 21:00 那次 failed、21:17 重開，要先看 `pipeline.json` 的 `final_status` 和 `var/logs/daily-<日期>.log`。
   資料問題沒修好就重跑，只會再失敗一次；能續跑的就續跑。
3. **排程已經包含晚上 21:00 與早上 07:00 兩次，不要再手動另開**。手動跑出一個 approved 決策後，若沒有重大新資訊，就不要再跑一次「確認」。
   `docs/operations.md` 也提醒過：手動跑完上傳後，07:00 若又成功，還得重新上傳。
4. **不要用會產生不同目標日資料夾的參數手動跑**，否則同日沿用找不到前一次 run，事件分析師會全量重判（$8.3）。
5. 早上 07:00 那次（約 $12）要不要保留是策略問題：它會用隔夜事件與新聞重做決策。本計畫不建議取消，只列出它的成本供參考。

## 10. 測試清單（實作時照專案慣例用 `unittest.TestCase` 風格）

**第 0 步**

- 前一個目標日的 run 與本次逐檔相同 → 沿用，並記錄 `scope=cross_day`。任何一個 event、news 或 `financial_context` 欄位不同 → 不沿用。
- 來源 run `status` 不是 completed、檔案毀損、超過回看天數、超過 K 個交易日（以 `first_classified_run` 起算）→ 不沿用。
- 沿用的 item 引用本次 bundle 沒有的證據 → 只有那一檔改送 Agent，其他檔照樣沿用。
- `event_row_digests.json` 不存在（舊 run）→ 鏈狀退回：每檔取鏈上最近一個有該檔 brief 列的 run，item 取最新 run。比對不到的送 Agent。
  鏈上任何一個 run 毀損 → 停在那裡、不再往前找。
- `first_classified_run` 沿用時照抄、Agent 重判時更新；年齡超過 K 就送 Agent。
- scope 為 `same_day` 或 `--no-event-reuse` 時，結果與現行逐位元相同（回歸）。

**方案 2**

- 身分鍵：同 ID 不同 `content_sha256` → 視為新公告；資料庫列號改變 → 全部視為新公告。
- 合併：Agent 覆寫舊事件 → 以 Agent 版本為準、不重複；Agent 漏掉新事件 → Validator 擋下並回饋；Agent 輸出不屬於 E 的 ID → 拒絕。
- 合併後的報告通過 `AnalystReportValidator`；同一份報告交給 `ResearchDebateBundleValidator` 與 Finalizer 重驗也通過，證明重建不受影響。
- 串接：第三次 run 用第二次（含沿用）的報告當來源，年齡從第一次判斷起算。
- fail-closed：來源 `bundle_hash` 對不上、`decision_input.json` 讀不到 → 不沿用。
- 閘門關閉（沒有欄位）→ 簡報逐鍵與現行相同。

**方案 1**

- bundle 沒有 `event_lookback_policy` → `build_analyst_brief`、`AnalystReportValidator`、`compute_fundamental_metrics` 與 `bundle_sha256` 逐位元不變（舊 bundle 回歸）。用一個封存的舊 run 跑 Finalizer 重建，結果相同。
- 有欄位時，簡報與 Validator 的應分級集合相同；只分級視窗內事件的報告通過；分級了視窗外事件的報告被拒（「不是該股的重大訊息」）。
- 邊界：恰好 N 天前（台北日期）納入，N+1 天前排除；`available_at` 跨 UTC／台北換日的案例；缺時區時拒絕；cutoff 之後的資料照樣拒絕（原有檢查保留）。
- 欄位形狀錯誤（版本未知、天數非整數、0 或超過 90）→ `DecisionInputValidator` 拒絕。Policy 與 bundle 不一致（只有一方有欄位或天數不同）→ Finalizer 拒絕。
- 策略反思提案改 `event_lookback` → 被 `FORBIDDEN_PREFIXES` 擋下。
- quality_dip：在 N+1 天到 `base` 之間放一則 high 負面事件。「自動延伸」時仍列為新壞事；N=14 時明確測出行為改變，並與文件描述一致。
- 7 天時全部事件都在視窗外的股票 → 由程式輸出 unknown，`data_gaps` 說明回溯期。

**共同**

```bash
PYTHONPYCACHEPREFIX=/tmp/etf-agent-pycache .venv/bin/python -m pytest
PYTHONPYCACHEPREFIX=/tmp/etf-agent-pycache .venv/bin/python -m compileall -q src tradeagent tests
git diff --check
```

改 Skill 時另外跑 `quick_validate.py`。

## 11. 風險彙整

| 風險 | 方案 | 程度 | 緩解 |
| --- | --- | --- | --- |
| 一次抽樣的分級被固定下來（high 一致率 68.5%） | 0、2 | 中 | 年齡上限 K、Agent 可覆寫、持股與 high 附原文重判 |
| 舊摘要含相對時間用語 | 2 | 低 | 簡報附來源 cutoff |
| Agent 只看摘要、誤解舊事件 | 2 | 中 | 同上；上線後比對沿用值與重判值的差異率 |
| quality_dip 新壞事漏判，屬加風險方向 | 1（N < 約 30） | 高 | N ≥ 30 或自動延伸 |
| 長效利多或利空被截掉、例行公告主導 `outlook` | 1 | 中～高 | 不在比賽期間做 |
| 研究員引用看不到內容的舊事件 ID | 1 | 低 | 簡報的 `evidence_kinds` 只列視窗內 |
| 換版那一次全數重跑 | 0、1、2 | 低（只多花錢） | 選在晚間 run 前上線，避開早上 |
| 測試只證明可重建，不證明判斷品質 | 全部 | — | 所有離線比對都標 `replay_reference_only`；品質只能看比賽期間的即時紀錄 |
