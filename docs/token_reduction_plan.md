# 降低每日決策 token 用量計劃

最後更新：2026-10-21

## 目標與原則

縮小餵給 Agent 的重複輸入、減少不必要的重試，但**不改變**驗證規則、研究覆蓋與證據可追溯性。硬性規則仍由 Python 計算及 Guard 驗證（確定性歸程式，不確定性歸 LLM）。

**核心教訓**：直接縮資料會改變判斷（見〈試驗結果〉）。所以改走兩條路：① 程式先算出能確定的結論，LLM 只處理例外；② 無損地縮表示法。任何縮減都必須先過品質關卡，位元組降幅不是驗收標準。

## 一、現況基準

「bytes」是 `brief_*.json` 的檔案大小，**不是 token**。真實 token 尚未量得。

### 兩份可參考的 run

| Run | 執行器 | 結果 | 說明 |
| --- | --- | --- | --- |
| `daily-20261019-220447` | Codex，150 檔，每批 50 檔 | 基準（19 份 brief 共 8,882,656 bytes，存於 `artifacts/archive/experiments/token_reduction/baseline-20261019.json`） | Codex runner 的 `estimated_usage_usd` 回報 0，不能當成沒有用量。 |
| `daily-20261020-213724` | Claude | `approved`，5 張單 | 續跑後 `pipeline.json.agent_calls` 只列當次新呼叫，估算用量 **10.36 美元**不等於整輪費用；續跑前的技術／基本面／事件 b0 不在內。 |

### 各類 brief 大小（bytes，三批 b0／b1／b2）

| Brief | 10/19 Codex | 10/20 Claude | 備註 |
| --- | --- | --- | --- |
| 技術 | 1,920,268／1,893,871／1,918,819 | 759,817／1,032,485／794,930 | 仍是最大宗。10/20 較小是因為該輪開了 `--technical-gate`（brief 內 `technical_gate` 記錄 84 檔只附 momentum、66 檔附完整歷史）。 |
| 事件 | 462,976／696,313／346,167 | 468,556／747,643／354,949 | 事件分析師判斷重大性前，不能先篩掉非 high 事件。 |
| 基本面 | 85,315～96,391 | 92,426／96,391／85,315 | 小。 |
| 多頭、空頭（各） | 128,012～156,866 | 167,996／146,303／144,477 | 共同輸入仍帶分析師逐股發現。 |
| 交易 | 106,325～113,524 | 117,267／105,815／108,216 | 已只取 outlook、多空 claims 等必要欄位。 |
| 現金姿態 | 190,698 | 207,587 | |

### 重試與修正

- `agent_step` 每個任務最多 2 次。10/19 Codex 保存的逐批原始輸出均為第 1 次，風險審查 `revision_index=0`，沒有 `revise` 循環。
- 10/20 Claude 在 `event_b1` 重試後遇到訂閱額度上限（session limit），後來以 `--resume` 續跑完成；不能用其部分估算推算 Codex 全程。
- 多頭與空頭各自一次呼叫、彼此隔離，沒有來回辯論。
- 既有持股超重已另行處理：10/19 的 2412.TW 原持 686 張，重估權重 10.45% 被 Guard 拒絕。#59 已新增單股上限與部分成交情境的確定性減碼；原封存決策仍是 rejected，不算節省成果。

## 二、各項措施狀態

| # | 措施 | 狀態 |
| --- | --- | --- |
| 0 | 用量量測 | bytes 基準完成；**token 未量得**（優先 1） |
| 1 | 技術面閘門 `--technical-gate` | 已實作，2026-10-21 起預設開啟；品質只有一天的對照 |
| 2 | 分級模型 `--analyst-model` | 已實作，Claude CLI 預設 `sonnet`；品質無實跑對照 |
| 3 | 交易 brief 去重 | 已接入 |
| 4 | 減少無效重試 | 部分完成 |
| 5 | 增量執行 | 研究項目，不接入 |
| 6 | 無損表示再縮小 | 尚未開始（優先 2） |
| 7 | 連續視窗與事件 parent 檢索 | 尚未開始，先做研究原型（優先 3） |

### 措施 0：用量量測

- OpenCode runner 已對新呼叫記錄 JSONL `step_finish.tokens`（小型實機煙霧測試通過）。
- Codex 下一步：評估 `codex exec --json` 的 `turn.completed.usage`（`input_tokens`、`cached_input_tokens`、`output_tokens`）。
- 成功與失敗／重試分開記錄。
- 不回填舊 run，也不從 `estimated_usage_usd=0` 猜 token。

### 措施 2：分級模型

- 2026-10-21 起，Claude CLI 預設 `sonnet`。`inherit` 沿用 `--model`；Codex／OpenCode 未指定時沿用 `--model`。
- 只套用於技術／基本面／事件分析師批次。多空、交易、現金姿態維持 `--model`。
- `pipeline.json.agent_calls` 記錄實際使用的模型。
- 品質差異未量得。

### 措施 3：交易 brief 去重

- 完整帳戶持倉改為持股代號與檔數，各批少 3,176 bytes。
- 逐股 `held_shares`、雙方 claims 與引用維持完整。
- 多空 findings 與事件脈絡，沒有安全的等價刪減，暫不裁剪。

### 措施 4：減少無效重試

- 已完成：#59 的單股硬上限改由程式處理；`liquidity_fill_rate` 改為 1，避免 Guard 全批拒絕。
- 待補：送交易 Agent 前，用程式預算「持股數貼近上限」等會使 Guard 失敗的狀況，並提早警示。
- `max_revisions` 維持 3，不放寬 Guard。

### 措施 5：增量執行

- 做法：以每檔輸入內容雜湊判斷，未變動的股票沿用前一日已驗證的分析。
- **風險最高**：須標明來源 run 與 cutoff，Validator 要能證明雜湊相同，否則一律重跑。
- 多空與交易仍對全池給當日判斷。
- 有跨日資料後再評估。

### 措施 6、7

見〈三、後續方案〉。

### 技術面閘門（措施 1）細節

- 程式只決定每檔附完整歷史或只附 `momentum`；LLM 仍對全部 150 檔給 `outlook`，契約與 Validator 不變。規則版本 `1.0`，只用 `momentum` 已算好的欄位，不重算指標。brief 內附 `technical_gate`（版本、規則、被省略歷史的股票清單）。資料不足、訊號衝突、欄位缺漏一律附完整歷史。
- 以 10/20 真實輸入離線重算（沒呼叫 Agent）：150 檔中 **84 檔**趨勢一致，技術 brief 序列化後由約 3.57 MB 降至約 1.61 MB（約少 55%）。這是輸入大小，不是 token，也**沒有品質數據**。
- 規則寫在程式常數（`TECHNICAL_GATE_VERSION`／`TECHNICAL_GATE_RULE`）並記錄在 brief；因只影響 Agent 輸入、不進可驗證的決策產物，沒有放進 Policy 雜湊。若日後改為由程式直接決定 outlook（規則一致就不給 LLM 看），就必須進 Policy 並由 Validator 重算，且會改變 Agent 邊界，要同步更新契約文件。
- **10/20 同 Snapshot 對照（不需另跑 Agent）**：`daily-20261020`（全部附完整歷史）與 `daily-20261020-213724`（開閘門）使用相同 `snapshot_id` 與 cutoff，150 檔 outlook 比對如下。
  - 被閘門省略歷史的 84 檔：**7 檔改變（8.3%）**。
  - 兩輪輸入完全相同、都附完整歷史的 66 檔：**16 檔改變（24%）**，代表模型自身的重跑變異已大於閘門造成的差異。
  - 全部 150 檔共 23 檔改變，兩輪都各有 1 檔 `unknown`。
  - 限制：只有一天、一次重跑；這 66 檔本來就是訊號衝突、較難判斷的股票，變異偏高，不能直接與 84 檔趨勢一致者相比；兩輪之間程式也有調整，未逐項排除。所以只能說「沒有看到閘門比模型自身變異更糟」，不是品質已證實。據此 2026-10-21 起預設開啟，累積更多天的對照後再檢討；若省略歷史的股票改變比例持續高於重跑變異，就改回 `--no-technical-gate`。

## 三、後續方案

### A. 無損表示再縮小（優先 2）

- 先刪每批重複的欄名與包裝，用欄位表加逐行資料表示完整時間序列，保持每列日期、數值、`available_at`、來源 ID／版本。
- 建立可逆的 `encode → decode` 檢查：還原後須與原始 canonical 資料逐欄一致，150 檔與全部視窗都保留。先在技術 brief 試，對照真實 `input_tokens` 與品質。
- 限制：大量 JSON 壓成單行可能被 CLI Read 工具截斷，改用有界區塊；不要因欄位變短讓模型猜單位或日期。相同前綴才可能命中 prompt cache，且快取只減少重複前綴的處理成本與延遲，不縮小實際上下文，也不保證跨獨立 CLI 工作階段命中。

### B. 保留局部與整體脈絡（優先 3）

- 技術資料以**連續時間窗**保存：起點、近期完整 20／60 日、轉折與均線交叉附近的前後觀測及極值；視窗交界標明時間與是否缺觀測。指標衝突、視窗跳空、疑似反轉，或本輪判斷與完整歷史版不同時，**整檔回退完整序列**。
- 事件先列每一則的 ID、時間、來源、標題與必要事實，需要時按 ID 取得相鄰段落與完整原文；高重大性與歧義事件先展開完整脈絡，仍須對所有事件分級。
- 檢索命中的小段不代表已得到足夠事實，必須追到同 Snapshot 的原始 parent 及證據 ID。未展開卻需要的資料標「資料不足」，不填假值。
- 實作只用確定性 Python 和既有 SQLite／Snapshot，借用「小段定位、回讀相鄰／父段、排序與來源映射」的概念。

### C. 可用工具

| 工具 | 本機狀態 | 用途與邊界 |
| --- | --- | --- |
| Codex CLI | 已安裝、已在每日鏈使用；`codex exec --help` 支援 `--json` | 最優先：在現有 `CodexAgentRunner` 擷取用量。需修改 runner 並驗證 JSONL 解析、失敗重試與輸出 schema；只量未來呼叫，也不是訂閱帳單。 |
| SQLite FTS5 | 本機 `.venv` 的 `sqlite3` 已確認能建立 FTS5 表 | 沿用現有 SQLite 保存來源 ID→原文及相鄰段落索引，先用精確 evidence ID 取原文，再評估 FTS5 搜尋是否必要。索引只輔助定位，不能讓排名篩掉任何需分級事件；中文分詞與 `snippet()` 截段可能漏語意，須測全覆蓋並回讀完整 parent 原文。 |
| rank-bm25 | 尚未安裝 | FTS5 不足時才評估；需新增依賴（含 NumPy）並自備中文分詞，只能排序，不能裁定重大性或略過來源。先做 FTS5 基準再決定。 |
| LangChain／LlamaIndex | 不安裝 | 專案規則不直接引入；只參考其公開實作的設計。 |

目前沒有已驗證的「裝上就能既少 token 又維持品質」的套件。

## 四、試驗結果（2026-10-19 Codex，同一 Snapshot 離線試跑）

### 技術 brief 摘要（`1.1-summary`）

- 做法：固定 12 錨點、收盤極值、最大量及近 5 筆，由原始歷史重建。三批各由約 1.9 MB 降至約 0.36 MB（輸入位元組約少 81%）。
- 結果：`scripts/evaluate_technical_brief.py` 對 150 檔試跑，全部通過原始輸入 Validator，沒有新增 `unknown`，但 **12 檔 outlook 改變**。
- 第二次獨立試跑（`artifacts/archive/experiments/token_reduction/technical-repeat-20261019/`）：同樣通過 Validator、沒有新增 `unknown`，相對完整歷史版亦有 12 檔改變。兩次摘要共同改變 8 檔且新標籤一致；其餘各有 4 檔只在其中一次改變；兩次摘要彼此有 8 檔標籤不同。
- 判讀：模型輸出本身有變動，也不能排除抽樣改變了那 8 檔的判讀。完整歷史版只有一次輸出，無法判定哪版較好。**正式每日鏈繼續使用完整歷史，摘要不接入。**

### 事件 brief 首尾摘錄

- 做法：body 取首尾摘錄、完整來源另存。三批合計由 1,505,456 降至 1,251,030 bytes（約少 17%；完整來源另存約 0.96 MB，Agent 若全部讀取可能不省）。
- 結果：`scripts/evaluate_event_brief.py` 對 447 則事件試跑，Validator 全通過、事件 ID 全覆蓋、沒有新增逐股 `unknown`，但 **21 則 materiality 改變，含 10 則 high／medium 互換**。重大性差異尚未獨立裁決。**正式事件 brief 保持原狀，不接入。**

這兩項都不能證明原版或摘要版較準，也不能只靠 Validator 的引用檢查裁決。

## 五、驗收順序

1. **雙側基準**：相同 Snapshot／cutoff／Policy、相同執行器與設定，完整歷史版也獨立重跑至少一次；比較原版內部變異，再比較新方案與原版的變異；逐檔保留原始證據及完整文字理由。
2. **逐階段 A/B，不一次全縮**：先無損表示，再連續視窗與條件式回退，最後才試事件 parent 段落。每階段統計**所有**初始輸入、追加讀取、重試與輸出 token（有 CLI 數值時）、延遲及 bytes，避免「初始 brief 變小但查原文更花」。試驗另存，不寫入既有不可變 Decision run。
3. **品質關卡**：所有股票／事件及 evidence ID 全量覆蓋、來源授權與 `available_at` 合法、還原與 Validator 通過、不新增 `unknown`；對每個 outlook 與 high／medium 變動逐條核對上下文、反證與數值口徑。無法證明是可接受的判斷差異就維持完整輸入，不因降幅達標而放行。

## 六、外部參考（只借設計，結果不是台股準確率證據）

| 來源 | 查證到的內容 | 對本專案的啟發 |
| --- | --- | --- |
| [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) | 已讀 `default_config.py`：分 `deep_think_llm`／`quick_think_llm` 兩級模型；`max_debate_rounds=1`、`max_risk_discuss_rounds=1`；`max_tool_rounds`、`llm_max_retries` 有上限；`news_article_limit=20`。 | 分級模型（措施 2）；多空已是單輪，不需再改；重試與輸入量需有明確上限。第三方估算每檔約 0.2～0.8 美元只能當粗略參考，不可套用。 |
| [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | README 列出技術、基本面、估值、情緒各自產生訊號，再由 Risk Manager 與 Portfolio Manager 彙整。 | 指標訊號可先由程式算好再交給下游。**未能讀到其技術面原始碼**，「訊號是否完全由程式計算」未驗證。 |
| [awesome-llm-token-optimization](https://github.com/pleasedodisturb/awesome-llm-token-optimization) | 通用的 prompt caching、批次、模型路由彙整。 | 通用方法；快取與批次屬 CLI／provider 行為，專案不直接串 API，不能宣稱必定省錢。 |
| [Codex 非互動文件](https://learn.chatgpt.com/docs/non-interactive-mode.md)、[JSONL usage 結構](https://github.com/openai/codex/blob/main/codex-rs/exec/src/exec_events.rs) | `turn.completed.usage` 欄位。 | 措施 0。 |
| [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)、[Claude Code caching](https://code.claude.com/docs/en/prompt-caching) | 相同前綴才可能命中快取。 | 措施 6 的限制。 |
| [Lost in the Middle](https://aclanthology.org/2024.tacl-1.9/)、[Anthropic Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval)、[LangChain parent-document](https://github.com/langchain-ai/langchain/blob/master/libs/langchain/langchain_classic/retrievers/parent_document_retriever.py)、[LlamaIndex LongContextReorder](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/postprocessor/node.py) | 重要資訊置中時較難取用；小段定位、回讀父段、重排。 | 措施 7。 |

## 七、底線

- 瘦身後 Validator 仍須用原始資料重建並驗證；摘要只給 Agent 看，不能取代可重算的原始序列。
- 不得因輸入變小讓 Agent 把有資料的股票標成 `unknown`；摘要要明確標示哪些是真實缺口。
- `max_revisions` 維持 3（`allocation.py` 會驗證此值）。
- 股數、現金與單股／產業權重由 Python 與 Guard 負責，風險 Agent 不輸出數字；交易受限或情境無法符合全部硬規則時仍 fail-closed。
- 修改 Agent 輸入契約時，同步更新 `docs/architecture.md` 與相關 Skill 的 `references/`。

## 八、風險

- **摘要失真**：技術與事件摘要都曾改變判斷。有資料變成 `unknown`、引用無法核對，或關鍵方向改變卻無法由保留的證據解釋時，不採用；補回必要錨點後重新比較。測試遺漏事件、錯股引用、刪除反證及改寫摘要必須 fail-closed。
- **沒有真實 token 紀錄**：既有 run 只能報 bytes。沒有真實數值就明寫「未量得」，不得把 bytes 換算成確定 token、把 `estimated_usage_usd=0` 當成零成本，或把 API 快取定價套到訂閱 CLI。
- **分級模型與增量執行的品質**：尚未量測；增量執行若沿用過期分析，會造成時間點洩漏或資訊過時，故一律要求輸入雜湊相同才沿用。
