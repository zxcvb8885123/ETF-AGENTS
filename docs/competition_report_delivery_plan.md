# 正式競賽決策報告與交易書交付計畫

日期：2026-09-23。狀態：執行中；官方策略說明書格式、D-Plan v4.0 Schema／指南、正反例及 ETF 清單已盤點。DailyReport 帳戶呈現與正式工作流帳本綁定已實作；D-Plan 候選匯出 CLI 與本地結構／引用鏈檢查已新增並有合成測試。官方 150 檔個股清單已在專案中，9/22 官方日線亦已補齊；尚缺已確認的隊伍策略、ETF 基準持股、正式交易狀態與可重建的真實決策 run。目標仍是用真實、可追溯資料及連續 10 億元虛擬帳本交付主辦方策略說明書與每日 D-Plan JSON，供人工檢視。

## 完成定義

一次按需執行可完成資料準備、隔離研究、決策、風控及報告交付。需要模型判斷的步驟由本地 Codex／Claude 工作階段執行；CLI 保存交接與等待狀態。使用者能從固定入口取得：

- 每日決策報告：資料截止時間、帳戶、持倉、研究理由、買賣／不交易決策、配置、費稅、現金、風控與限制。
- 官方交易書／機器可讀檔：檔名、格式、欄位、單位及版本以實際取得的主辦方規格為準。
- 驗證結果與稽核 manifest：原始規格、輸入、程式／策略版本、引用、雜湊及重建結果。

研究報告可先交付；只有官方格式、正式資料與決策閘門全部通過，才標記為「正式格式驗證通過、待人工檢視」。本地驗證通過不等於平台已收件；平台回執須另有實際證據。格式合格也不代表策略有效。

## 現有基礎與缺口

| 項目 | 現況 | 本計畫需補齊 |
| --- | --- | --- |
| 研究與報告 | 有事件研究、Research Report V0、離線 DailyReport／FailureReport | 同一次真實資料 run 的完整交付 |
| 投資組合 | P0～P6 fixture 驗收完成 | 真實資料、完整基準、規則與交易狀態驗收 |
| 虛擬帳戶 | 10 億設定、VA1～VA3 fixture 工具完成；DailyReport workflow 現要求核對已封存 prepare-day run | 自動 prepare/apply、跨日帳務與真實資料驗收 |
| 官方格式 | 已取得策略說明書欄位表、D-Plan schema v4.0、指南與正反例 | 建立版控契約、欄位映射、結構／語意驗證及官方樣本比對 |
| 日常運作 | CLI 可準備、等待與續跑 | 本地 Agent 交接、操作手冊與端到端演練 |
| 策略驗證 | B0～B2 fixture 帳務回測 | 真實歷史資料、必要回測及固定版本前向驗證 |

先前只有「每日決策報告／交易書」的交付物描述，尚未持有欄位契約；本次收到原始 D-Plan v4.0 Schema、指南與樣例後，欄位格式已可核實。伺服器端完整語意驗證器及全部競賽辦法仍未提供，因此只把取得的 schema／guide 作為 exporter 契約，不把例檔的註解或未提供的 C 檢查實作宣稱為官方 validator。

## 實作順序

### F0：核實官方交付規格

1. 從官方活動頁、參賽平台或使用者提供的主辦方附件，取得競賽辦法、決策報告模板、交易書範例、欄位／schema 及驗證說明。
2. 保存原始檔、來源 URL、取得時間、生效期間、版本與 SHA-256；建立規則對照表，逐條標明來源頁碼或欄位。
3. 核對本金、交易池、持股與現金限制、基準／Active Share、股數單位、費稅、成交／交割口徑、零交易格式、提交時區與截止時間。現有設定值逐項對照，不把設定檔本身當作官方證據。
4. 建立「官方欄位 → 上游契約 → 轉換／計算 → 驗證方式」映射表。未取得的必填規格列為阻擋項。

驗收：每個官方欄位與規則都有版本化來源和上游映射；未取得的語意檢查器不得假稱已重現。**狀態：格式文件已取得，F0 文件盤點完成；程式化欄位映射及驗證器仍待 F4。**

附件盤點紀錄（2026-09-23）：使用者提供七個本地檔案，以下 SHA-256 用於識別所讀版本；文件內未含原始下載 URL，來源記為使用者提供，不推測官方網址。

| 檔案 | SHA-256 | 盤點結果 |
| --- | --- | --- |
| `玉山挑戰賽_ETF投資策略說明文件_參賽隊伍繳交格式_v1.docx` | `55c17d9c867026d939c49f5871f0c9f0bcc0e1ffc6b18b18a36e7c9837e490df` | 空白填寫表：ETF 名稱、投資主題、投資理念 |
| `玉山挑戰賽_ETF投資策略說明文件_範例文件_v1.pdf` | `bca9df0ca964b4f258959a42500716be6199343093874b6c993154edbb6e7a60` | 名稱 20 字內且以「主動」開頭；主題 50 字內；理念 100–300 字。說明書須於 2026-10-21 至 2026-10-26 繳交，初賽交易須與主題、理念及因子合理關聯；長期偏離可被要求改善或取消資格 |
| `D-Plan.schema.json` | `b4703cbb2387065d3612d799018cac0b8671f49305bcb63fe3c518f8bc89b95f` | D-Plan schema 4.0；單一 JSON，嚴格禁止未定義欄位；結構通過後仍需語意層 C 檢查 |
| `D-Plan_撰寫指南.md` | `7e17f4685c76568da2999719e7aeaeba71fba90f1073752a42e37f357c2bd839` | 完整來源→事實→市場／個股推論→決策→委託引用鏈、時間／ID 規則、委託推導及自我檢查 |
| `D-Plan_TEAM_042_2026-10-27.json` | `5457ed6ab02343273e570b0fca7019ff6c5c6fe50035102237e4569ee5984665` | 正例結構展示；內文自註市場數據為合理推估、非真實行情，且持股清單只節錄，不能當正式日報或真實資料 |
| `D-Plan_TEAM_043_2026-10-27.json` | `98035d562b286bbd44bfebb6863b4f0e9ed5bc90560871125e2a563953d8580e` | 反例示範孤兒推論引用、錯誤股數及市場姿態與訂單不一致；不得以其市場內容或 TEAM_043 作實際資料 |
| `玉山挑戰賽_主動型ETF列表_v1.pdf` | `d19cb228af7569b662cc0cfd741c726a4016ec3cc37536bf1c1d7bd891ec49d5` | 以 2026-09-11 證交所揭露為準的 30 檔主動型 ETF 代號／名稱；不等同 D-Plan 中四位數個股交易池、完整基準成分或權重 |

已確認的 D-Plan 核心契約：每日 05:00–08:55（台北時間）繳交唯一 JSON，檔名 `D-Plan_<team_id>_<trade_date>.json`；時間戳固定 `+08:00`，來源／觀察／推論／決策 ID 各層從 1 連續編號。必填 `sources`、`observations`、`market_view`、`inferences`、`decisions`、`no_trade_decisions`、`orders` 與 `agent_metadata`；來源 authority 有交易所／主管機關、金融機構、媒體、資料商等分類，來源分類依內容性質。每項資訊需由下層引用上層；facts 不可混入推論。

指南要求決策覆蓋所有前日持股；下單以目標權重、前日 NAV、前日收盤及 1,000 股整數單位推導；持股 20–30 檔、現金 0–25%、2330 上限 25%、其他個股 10%。說明書範例另描述買賣手續費各 0.1425%、賣出證交稅 0.3% 及依當日成交均價更新帳務；本計畫仍須把這些值逐條比對 `config/competition_rules.json`，不可只因現有設定相同便視為核實。指南提及平台語意驗證器 `verify_dplan.py`，但使用者提供的檔案不含該程式；故本地只能宣稱實作之結構／規則子集已驗證，不宣稱等同主辦方 server C 檢查。

### F1：補齊正式決策資料

- 完成交易狀態 TS0／TS5：核准官方來源並驗收 150 檔必要類別覆蓋；未知、過期及衝突保持不可交易。
- 補齊官方要求的全部 ETF 基準成分、權重、生效時間及來源版本，重算 Active Share。
- 固定研究 Snapshot、完整交易日行情、帳戶及規則版本；建立逐項 readiness 結果，區分缺資料、未驗證、驗證失敗。
- 記錄研究範圍及排除理由；不把部分事件研究宣稱為全交易池研究。市場情緒／分析師資料缺少可降級；若官方列為必填，再依 F0 改為必要輸入。

驗收：所有必要輸入可追溯且在 cutoff 前可得，數字可重算；缺任一必要項停止正式決策。基本面 FR5 尚未完成前，不直接插入舊版報告或決策契約。

執行盤點（2026-09-23）：TS1～TS4 工具已存在，但 TS0 官方交易狀態來源核准及 TS5 150 檔實測未完成。主辦方提供的 30 檔主動型 ETF 清單不是 D-Plan 的四位數股票標的清單，也沒有基準權重；不可把它當完整 ETF 基準。`config/competition_rules.json` 所列 150 檔來源為 2026-09-14 的另一份 100 上市＋50 上櫃清單，與本次 ETF 清單是不同用途。手續費 0.1425%、賣出稅 0.3%、持股 20–30 檔、個股權重上限等已能與指南比對；但設定的現金嚴格 `<25%` 與 Schema 的 target cash range `≤25%` 邊界不同，設定的提交開始時間 19:30 與 Schema 描述的 05:00–08:55 範圍也尚待完整競賽辦法釐清。min successful days 等設定值亦未由本次附件確認。已建立 9/22 同日 150 檔官方價格 Snapshot，但尚無正式 ETF 基準、交易狀態、研究與決策 artifacts；F1 readiness 仍 blocked。

9/23 行情回補：`scripts/collect_latest_prices.py` 保存 TWSE 9/22 的 100 檔與 TPEx 9/23 的 50 檔最新回應；由於兩市場回應日期不同，以最近共同官方日 9/22 執行 `scripts/collect_official_history.py --start 2026-09-18 --end 2026-09-22`。封存的 `artifacts/official-history/coverage-2026-09-22.json` 記錄 150/150 檔終止日覆蓋、450 筆新增官方日線、零缺漏；9/18、9/21、9/22 各日皆有 150 檔。`artifacts/research_snapshot_2026-09-23_194119.json` 的 cutoff 為 2026-09-23 19:41:19 +08:00、行情日期同為 9/22、150/150 覆蓋且 `usable=true`。此 Snapshot 僅供目前資料研究；尚未取得 9/23 TWSE 同日官方價，不得拿 9/22 TWSE 與 9/23 TPEx 混成 9/23 決策價。歷史區間沒有版本化交易日曆，覆蓋報告也不宣稱每個應有交易日完整。

ETF 基準來源檢查：證交所說明主動式 ETF 須每日揭露實際投資組合，且指出投資人可到各投信網站查詢；參見 [主動式 ETF 商品說明](https://wwwc.twse.com.tw/zh/products/securities/etf/products/active-list.html) 與 [證交所觀點](https://wwwc.twse.com.tw/market_insights/zh/detail/8a8216d69517ec0c01954108487600b0)。這只確認有公開揭露義務，不等於已取得本賽事所需全部 ETF 的完整、可版本化持股權重。現有 `data/active_etf_top10.csv` 僅有標頭；後續須逐一核對主辦方要求的 ETF 集合、各投信來源、揭露日期／可得時間、完整權重與授權，再接入 Active Share。不可用第三方估算或主辦方的 ETF 名單代填權重。

交易狀態檢查：`config/data_sources.json` 的 `trading_status.sources` 仍為空，TPEx 四個端點只列 candidate；`cli/trading_status.py status` 尚無正式收集 run。TS1～TS4 的 parser 與 Guard 雖可重建資料，但不能把候選端點、空回應或行情存在推定為 150 檔可交易。TS0 官方來源語意／授權核准與 TS5 固定分母實測仍是正式決策阻擋項。

### F2：將虛擬帳本接入每日流程

- 完成 VA4：唯一開帳 → 續接前日狀態 → 交割／公司行動 → 決策前帳戶快照 → Portfolio Decision。
- 第一天本金為設定的 1,000,000,000 TWD；後續只讀已驗證的前次帳本，不能每天重新注資。
- 綁定 account state、Snapshot、cutoff 與 Decision run；帳戶父狀態及輸入雜湊不符即拒絕續跑。
- 決策報告列出決策前持倉、提案、預估成本與提案後配置。後續模擬成交及日終估值另存帳本，不回寫已封存的決策報告。

進度：DailyReport Builder 已將 DecisionInputBundle 中經驗證的決策時帳戶 NAV、現金、未交割款、持倉、估值時間與帳戶證據 ID 顯示在 JSON／Markdown，並標示「內部人工檢視」及「官方格式未驗證」。正式模式的 report workflow 現要求傳入虛擬帳戶 repository／account ID／prepare-day run ID；產生 DailyReport 前驗證 run manifest、latest 指標、prepared 狀態、Snapshot 全文與 ID／cutoff，以及 DecisionInputBundle 的 AccountSnapshot 一致性，並將帳戶 run／state／快照雜湊記入 downstream provenance。Fixture 模式仍可不傳帳本，但不可作正式交付。本次只把已封存帳戶接到報告，不由 workflow 自動執行 `prepare-day` 或 `apply-decision`，VA4 與 VA5 整體尚未完成。DailyReport 不是 D-Plan；目前缺少完整來源—事實—市場姿態—全持股決策的映射，不能直接轉存成官方 JSON。

本批程式改動將 DailyReport schema 升至 1.1，並納入決策時帳戶快照雜湊。`DecisionInputBundle` 與 Snapshot／cutoff／Decision run 的既有一致性驗證仍是報告前置條件；本次不把 fixture 帳戶升格為正式帳戶證據。

驗收：空倉、持倉、部分成交、未成交、交割、賣出與跨日續接均可重建；現金、股數及 NAV 相符；重複執行不重複成交或注資。若主辦方結算口徑不同，先完成對帳再續接。

### F3：完成本地研究到決策的交接

- 沿用 daily-report 與既有研究／決策 Skills，封存各角色 input、output、工作階段識別與工具紀錄。
- Fact → 隔離 Bull／Bear → Adjudicator → 雙重研究驗證；再執行 Momentum → 隔離 Buy／Sell → Trade Adjudicator → 確定性配置／訂單 → Portfolio Risk／Guard。
- Buy／Sell 使用相同資料、獨立 role input；風險修正最多三次，硬性拒絕不可轉為警告。
- CLI 顯示需接手的工作與續跑指令；不存在 Agent 工作階段時保持等待。完成後自動接回既有報告 Builder。
- 研究 pending、候選不足、決策拒絕與合法 no_trade 分開處理；不能將資料失敗包裝成不交易決策。空倉 no_trade 仍須核對持股數等規則是否允許。

驗收：以真實資料完成同一 Snapshot 的完整 Decision run，所有角色引用、帳戶與決策重建通過；並驗證缺輸入、拒絕與等待分支。

### F4：官方格式 exporter 與驗證（第一版已實作；不是完整 C validator）

- 已新增 `src/etf_agent/competition/dplan.py` 與 `cli/dplan.py`：從已驗證／可完整重建的 DecisionRepository run 讀取結果，要求 Agent 明確提供已審閱的 sources、observations、market_view、inferences、逐股票推論引用、模型 metadata、官方 150 檔清單與策略說明書欄位；倉位、目標權重、買賣動作與訂單股數由程式依前日帳戶、收盤價及主辦指南公式導出，不讓 LLM 填寫數字。
- 匯出前要求 20–30 檔、官方 150 檔股票集合、2330／其他標的上限、C12 淨曝險及費稅後現金區間通過；目標股數若與已驗證 Decision orders 不一致就拒絕輸出。候選檔採「只新建、不覆寫」並標示人工檢視、主辦方驗證未執行。
- 策略說明 context 核對名稱以「主動」開頭且 ≤20 字、主題 ≤50 字、理念 100–300 字；此 context 仍需與使用者實際核准文件一致。DailyReport 與 D-Plan 是分開的交付物，不能把前者直接改副檔名或宣稱為官方格式。
- 本次尚未填寫或輸出策略說明書 DOCX；沒有使用者確認的名稱、主題與理念前只驗欄位，不替使用者編造或核准投資承諾。
- 新增本地 D-Plan v4.0 結構／引用鏈支援檢查，並以合成素材測試 orphan reference、未允許欄位、非千股委託與缺少官方 context。
- 人類可讀報告與官方交易書由同一份決策產生；數量、方向、單位、費稅、現金及引用逐項交叉核對。
- 本地結構檢查覆蓋主要 required／additionalProperties／列舉／格式／長度與引用鏈，但目前未完整實作 JSON Schema Draft 2020-12，也不是伺服器端完整語意限制；本地成功不可稱「官方 schema 全驗證通過」。指南提到的 `verify_dplan.py` 未提供，平台驗證仍不可執行。
- 官方檔、內部研究報告與 failure 診斷分開封存；格式驗證失敗不得發布可用的送件候選。

驗收：本地檢查可讀取 TEAM_042 結構例並拒絕 TEAM_043 的孤兒引用；合成測試可拒絕錯欄位與非千股委託。下一階段仍須補齊價格／時鐘／全持股／平台 C 檢查、策略文件產生、端到端真實演練與官方伺服器驗證。例檔的資料自註為合成／節錄，不可將它們作真實報告或視為完整認證。

### 使用方式與目前阻擋

```bash
.venv/bin/python cli/dplan.py build \
  --team-id YOUR_ORGANIZER_TEAM_ID \
  --trade-date YYYY-MM-DD \
  --context artifacts/competition/dplan_context.json \
  --decision-repository artifacts/portfolio_decisions \
  --decision-run-id VERIFIED_DECISION_RUN_ID

.venv/bin/python cli/dplan.py validate \
  --input artifacts/competition/D-Plan_YOUR_ORGANIZER_TEAM_ID_YYYY-MM-DD.json
```

`dplan_context.json` 需要 `sources`、`observations`、`market_view`、`inferences`、`inference_refs_by_ticker`、`agent_metadata`、`eligible_tickers`（恰為 150 個官方四位數個股代碼）、`eligible_universe_source_refs` 及 `strategy_statement`。策略說明物件需包含 `name`、`theme`、`philosophy`、`approved_at`、`sha256`；內容必須與使用者確認並保存的主辦方格式文件一致。CLI 不接受 TEAM_042／TEAM_043 範例當真實輸入。`validate` 只回報本地支援子集，輸出 `local-v4-subset-not-organizer-server`。

截至 2026-09-23，`data/official_universe.csv` 已有 150 檔（TWSE 100、TPEX 50），同日官方 9/22 行情也已覆蓋 150/150；`artifacts/research_snapshot_latest.json` 仍是 9/17 的舊檔，新 Snapshot 另存於 `artifacts/research_snapshot_2026-09-23_194119.json`。TS0／TS5 交易狀態驗收尚未完成、`data/active_etf_top10.csv` 只有標頭、沒有已驗證 Decision run。隊伍代號與核准策略亦未提供。故此版提供匯出能力與阻擋檢查，**目前不能聲稱已產生可繳交的正式候選**。

### F5：固定入口與操作體驗

- 延伸既有 start.sh daily／report 與 report_workflow run／status／resume／verify，提供資料準備、研究等待、決策等待、格式阻擋及完成的清楚狀態。
- artifacts/reports/latest.md 顯示本次日期、cutoff、產物連結、驗證狀態與下一步；舊成功報告保留日期，不能替代今日失敗。
- 發布前驗證所有產物，原子更新索引；同鍵同輸入重用，同鍵不同輸入拒絕覆寫。
- 操作文件提供從乾淨工作區準備資料、Agent 接手、續跑、產檔及人工檢視的完整範例。明確區分現有命令與新增參數。

驗收：使用者能從單一入口找到正式格式候選或確切阻擋原因；只執行 Shell 時不誤稱已完成模型研究。

### F6：真實演練與正式啟用

- 先跑 fixture 故障案例，再以真實來源做完整按需演練；保存所有必要來源、研究、帳務、Decision、報告及驗證結果。
- 完成 VA5 多日操作：買入、未成交、交割、續抱、賣出及日終估值；同時核對人工可重算帳務。
- 依既有回測計畫補齊必要歷史資料、策略比較、研究品質評估及固定版本前向驗證。實作前寫下期間與通過門檻，不能事後選擇有利結果。
- 分別記錄格式驗收、資料／帳務驗收、策略驗收、平台收件驗證；任一未完成不以其他項目替代。
- 正式每日啟用須通過既有前置閘門。排程在按需交付完成後另接 RPT5，使用核實的時區、交易日及截止時間；本地模型工作階段未配置時維持等待。

驗收：至少一份真實資料決策報告與官方交易書通過重建及格式檢查，完成多日帳務和原定策略／前向驗收後才標記正式啟用。所有失敗日與零交易日保留，不能刪除或跳過。

## 實作範圍與驗證

核心沿用 src/etf_agent/automation、reporting、portfolio 與帳務模組的實際介面；開工先核對現有程式，不按文件臆造模組。cli 提供共用入口，Skills 定義本地 Agent 流程。契約升版需同步重建 Validator、下游 adapter、README 與相關計畫。

修改 Python、契約、CLI 或 Skill 後依 AGENTS.md 執行完整 unittest、compileall、git diff --check；新增或大幅修改 Skill 再執行 quick_validate。測試重點是時間／版本錯配、帳戶斷鏈、重複成交、硬性風控、合法 no_trade、官方欄位及跨檔一致性、竄改、等待續跑與部分發布。

正式交付前需要取得的外部資料是主辦方格式與規則原始檔、必要市場／基準資料，以及若有提供的平台驗證結果。缺少時明確列出取得方式與受阻階段，不用 fixture 補作正式證據。

系統交付到人工檢視為止；沿用本地 Codex／Claude，不新增模型 API 或平台送件／交易執行 Agent。此計畫不建立 commit、不改寫既有執行產物。
