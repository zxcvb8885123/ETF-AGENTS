# 降低每日決策 token 用量計劃

## 目標

不改變決策驗證規則、研究覆蓋與證據可追溯性，縮小餵給 Agent 的重複輸入，降低不必要的重試。硬性規則仍由 Python 計算及 Guard 驗證。

## 現況觀察

新增基準為 `artifacts/daily_runs/daily-20261019-220447/`（Codex、150 檔、每批 50 檔）。以下是 `brief_*.json` 的**檔案位元組數**，不是實際 token；早期 `artifacts/codex_daily_runs/` 只供歷史參考。Codex runner 的 `estimated_usage_usd` 回報 0，不能當成沒有用量；續跑後的 `pipeline.json.agent_calls` 也只列當次新呼叫，不等於整輪費用。

- **技術 brief 仍最大**：三批各 1,920,268／1,893,871／1,918,819 bytes；`analysts.py` 仍逐檔傳完整 `trend_history`。早期抽樣每檔歷史約 23 KB、占技術 brief 約 98%；程式已有較短的 `momentum` 摘要，但歷史變化仍是分析依據。
- **事件輸入比舊基準更大**：三批各 462,976／696,313／346,167 bytes；基本面三批為 85,315～96,391 bytes。事件分析師尚未判斷重大性前，不能先篩掉非 high 事件。
- **多空與交易**：多頭、空頭各批 128,012～156,866 bytes，交易各批 106,325～113,524 bytes；多空共同輸入仍帶分析師逐股發現，交易 brief 已只取 outlook、多空 claims 等必要欄位，並未重傳三份分析師全文。現金姿態 brief 為 190,698 bytes。
- **重試與修正分開看**：`agent_step` 每個任務最多 2 次；10/19 Codex 已保存的逐批原始輸出均為第 1 次，風險審查 `revision_index=0`，沒有 `revise` 循環。另一次 Claude run 在 `event_b1` 重試後遇到訂閱額度上限，不能用其部分估算費用推算 Codex 全程。
- **多空沒有來回辯論**：多頭與空頭各自一次呼叫且彼此隔離。
- **既有持股超重已另行處理**：10/19 的 2412.TW 原持 686 張，交易 Agent 給 `hold`，重估權重 10.45% 而被 Guard 拒絕。#59 已新增單股上限及部分成交情境的確定性減碼；同一封存輸入的非封存重算預計賣 48 張、權重約 9.72%、Guard 通過。原封存決策仍是 rejected，不算正式新 run，也不算 token 節省成果。

## 外部研究與方案調整（2026-10-20）

**問題不是只把每檔歷史縮短，而是如何保留事件與價格路徑的上下文關係。**兩次抽樣技術試跑中，有 8 檔都與完整歷史版不同；首尾摘錄也造成 10 則事件在 high／medium 間變動。這些差異不能證明原版或摘要版較準，也不能只靠 Validator 的引用檢查裁決。外部研究的結果只作方法參考，不是台股分析準確率的證據。

| 查得方法 | 對本專案的做法 | 限制與優先序 |
| --- | --- | --- |
| **先量 CLI 回報用量**：Codex CLI `exec --json` 的 `turn.completed.usage` 回報 `input_tokens`、`cached_input_tokens`、`output_tokens`，文件另有 `reasoning_output_tokens`；本機 `codex exec --help` 已確認支援 `--json`。 | OpenCode 已新增唯讀 runner，對新呼叫的 JSONL `step_finish.tokens` 記錄用量（小型實機煙霧測試通過）；Codex runner 下一步才評估以 `--json` 擷取每次子 Agent 用量。將成功與失敗／重試分開記錄，不從 `estimated_usage_usd=0` 猜 token。 | **優先 1**。新記錄不能回填 10/19 舊 run；token 用量不等於訂閱制帳單。不得因量測改變唯讀 sandbox、輸出 schema 或 Agent 驗證。來源：[Codex 非互動文件](https://learn.chatgpt.com/docs/non-interactive-mode.md)、[Codex JSONL usage 結構](https://github.com/openai/codex/blob/main/codex-rs/exec/src/exec_events.rs)。 |
| **無損表示再縮小**：相同前綴才可能命中 prompt cache；先刪每批重複的欄名／包裝、用欄位表加逐行資料表示完整時間序列，並保持每列日期、數值、`available_at`、來源 ID／版本。 | 建立可逆的 `encode → decode` 檢查：還原後須與原始 canonical 資料逐欄一致，所有 150 檔及全部視窗資料都保留；先在技術 brief 試，對照真實 `input_tokens` 與品質。可以縮表示，不先刪掉中段走勢。 | **優先 2**。若將大量 JSON 壓成單行導致 CLI Read 工具截斷，就用有界區塊而非單行；不要因欄位變短讓模型猜單位或日期。快取只能減少重複前綴的處理成本／延遲，不會縮小模型需要理解的實際上下文，也不保證跨獨立 CLI 工作階段命中。來源：[OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)、[Claude Code caching](https://code.claude.com/docs/en/prompt-caching)。 |
| **保留局部與整體脈絡**：長上下文研究發現重要資訊置中時可能較難取用；GitHub 的 parent-document retriever 以小段定位、回到較大父段，long-context reorder 以位置降低資訊埋沒。 | 技術資料先以**連續時間窗**保存起點、近期完整 20／60 日、轉折與均線交叉附近的前後觀測及極值；視窗交界標明時間與是否缺觀測。遇到指標衝突、視窗跳空、疑似反轉或本輪判斷與完整歷史版不同，**整檔回退完整序列**。事件先列每一則 ID、時間、來源、標題與必要事實，按 ID 取得相鄰段落與完整原文；高重大性與歧義事件先展開完整脈絡，仍須對所有事件分級。 | **優先 3，先做研究原型**。固定 12 錨點與首尾摘錄已見方向／重大性差異，不直接部署；檢索命中小段不代表已得到足夠事實，必須追到同 Snapshot 的原始 parent 及證據 ID。任何未展開卻需要的資料應標資料不足，不能填假值。來源：[Lost in the Middle 論文](https://aclanthology.org/2024.tacl-1.9/)、[Anthropic Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval)、[LangChain parent-document 實作](https://github.com/langchain-ai/langchain/blob/master/libs/langchain/langchain_classic/retrievers/parent_document_retriever.py)、[LlamaIndex LongContextReorder 實作](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/postprocessor/node.py)。 |

上述 GitHub 程式碼僅借用**小段定位、回讀相鄰／父段、排序和來源映射**的概念；依專案規則用確定性 Python 和既有 SQLite／Snapshot 實作，不直接引入 LangChain／LlamaIndex、向量庫、外部模型服務或讓模型自行搜尋未核准來源。Prompt caching 屬 provider／CLI 行為；目前由 Claude／Codex CLI 管理，專案不直接串模型 API，也不能宣稱快取一定省錢。

### 哪些現成工具可直接使用

| 工具／專案 | 本機狀態與直接用途 | 不可誤用的邊界 |
| --- | --- | --- |
| [OpenAI Codex CLI](https://github.com/openai/codex) | **已安裝、已在每日鏈使用**；本機 `codex exec --help` 支援 `--json`，可在現有 `CodexAgentRunner` 擷取 `turn.completed.usage`。這是最優先的直接使用項目，不需新增依賴或模型 API。 | 仍須修改 runner 並驗證 JSONL 解析、失敗重試及輸出 schema；只量未來呼叫，不代表既有 run 的用量，也不是訂閱帳單。 |
| [SQLite FTS5](https://sqlite.org/fts5.html) | **本機 `.venv` 的 `sqlite3` 已確認能建立 FTS5 表**；可沿用現有 SQLite 保存來源 ID→原文及相鄰段落索引，先用精確 evidence ID 取原文，再評估 FTS5 搜尋是否必要，無需新框架。 | 索引只輔助定位；不能讓搜尋排名篩掉任何需分級事件。中文分詞及 FTS5 `snippet()` 截段可能漏語意，須測試全覆蓋並回讀完整 parent 原文。 |
| [rank-bm25](https://github.com/dorianbrown/rank_bm25) | **尚未安裝**；若 FTS5 無法滿足研究階段的文字檢索，可評估這個獨立 Python 套件作候選段落排序，不必引入整套 RAG 框架。 | 需要新增依賴（包含 NumPy）及自備適合中文的分詞；只能排序，不能裁定事件重大性、略過來源或取代證據核對。先做 FTS5 基準再決定是否加裝。 |
| [LangChain ParentDocumentRetriever](https://github.com/langchain-ai/langchain/blob/master/libs/langchain/langchain_classic/retrievers/parent_document_retriever.py)／[LlamaIndex LongContextReorder](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/postprocessor/node.py) | **不直接安裝**；可直接參考其公開實作的「命中小段後讀父段」與長文重排設計。 | 本專案規則不直接接入 LangChain／LlamaIndex；其通用相似度排名也未保證台股事件的時間、授權與全量覆蓋。 |

目前沒有一個已驗證的「裝上就能既少 token 又維持分析品質」套件。先用**現有 Codex + SQLite + 可逆 Python 表示**實作最小原型，避免把固定錨點／首尾摘錄的資訊流失再包進新的框架。

### 新的驗收順序

1. **先做雙側基準**：相同 Snapshot／cutoff／Policy、相同執行器與設定，完整歷史版本也獨立重跑至少一次；目前只有原始完整歷史一次、摘要兩次，不能將全部 8 檔共同差異歸因於抽樣。比較原版內部變異，再比較新方案與原版變異；逐檔保留原始證據及完整文字理由。
2. **逐階段 A/B，而非一次全縮**：先無損表示；再連續視窗與條件式回退；最後才試事件 parent 段落。每階段統計**所有**初始輸入、追加讀取、重試和輸出 token（有 CLI 數值時）、延遲及 bytes，避免「初始 brief 變小但查原文更花」。同一 Snapshot 的試驗另存，不寫入既有不可變 Decision run。
3. **品質關卡**：所有股票／事件及 evidence ID 全量覆蓋、來源授權與 `available_at` 合法、還原與 Validator 通過、不能新增 `unknown`；對每個 outlook 與 high／medium 變動，逐條核對上下文、反證與數值口徑。無法證明是可接受的判斷差異就維持正式完整輸入，不因 token 降幅達標而放行。

## 步驟

### 本次執行結果（2026-10-20）

- 步驟 0 已執行：只讀 `scripts/measure_daily_briefs.py` 對 10/19 Codex run 的 19 份 brief 量測，共 **8,882,656 bytes**；基準存於 `artifacts/token_reduction/baseline-20261019.json`。包含真正送入 Agent 的檔案大小與已保存的嘗試序號；真實 token／費用仍未量得。
- 步驟 1 研究試跑：`1.1-summary` 以固定 12 錨點、收盤極值、最大量及近 5 筆由原始歷史重建，三批各由約 1.9 MB 降至約 **0.36 MB**（輸入位元組約少 81%）。`scripts/evaluate_technical_brief.py` 在同一 Snapshot 對 150 檔以 Codex 試跑，報告均通過原始輸入 Validator，沒有新增 `unknown`，但 **12 檔 outlook 改變**。方向差異可能是模型變動或抽樣影響，僅憑一次試跑無法分辨；正式每日流程仍沿用完整歷史，摘要暫不接入，待檢查差異證據及更多樣本。
- 技術摘要第二次獨立試跑存於 `artifacts/token_reduction/technical-repeat-20261019/`：相同 Snapshot／cutoff、相同三批摘要，全部通過 Validator，沒有新增 `unknown`。相對完整歷史版亦有 **12 檔 outlook 改變**；兩次摘要試跑共同改變 **8 檔且新標籤一致**，其餘各有 4 檔只在其中一次改變；兩次摘要彼此有 **8 檔標籤不同**。這顯示模型輸出有變動，亦不能排除抽樣改變了 8 檔的判讀；完整歷史版只有一次模型輸出，沒有足夠對照可判定哪版較好。正式每日技術 brief 繼續使用完整歷史，摘要仍未接入。
- 步驟 2 研究試跑：事件 body 首尾摘錄、完整來源另存的三批 brief 合計由 **1,505,456 降至 1,251,030 bytes**（約少 17%；完整來源另存約 0.96 MB，Agent 若全部讀取則可能不省）。`scripts/evaluate_event_brief.py` 對 447 則事件試跑，Validator 全通過、事件 ID 全覆蓋、沒有新增逐股 `unknown`，但 **21 則 materiality 改變，含 10 則 high／medium 互換**。重大性差異尚未獨立裁決，正式每日事件 brief 保持原狀，不接入首尾摘錄。
- 步驟 3 的低風險部分已接入：交易 brief 將重複的完整帳戶持倉改為持股代號與檔數，各批各少 **3,176 bytes**；逐股 `held_shares`、雙方 claims 與引用維持完整。多空 findings 及事件脈絡沒有安全的等價刪減，暫不裁剪。
- 步驟 4：#59 的既有持股單股限額修正已完成；本次試驗沒有修改 Guard 或 `max_revisions`。步驟 5 的結構化驗證與同 Snapshot 研究試跑已做，尚無真實 token／費用或跨日品質驗收，不能宣稱已達計畫目標。

| 步驟 | 狀態 | 內容與驗收 |
| --- | --- | --- |
| 0. 建立可比較基準 | 已完成 bytes 基準；token 未量得 | 量測腳本區分每份 brief、已保存嘗試與續跑限制，保留 snapshot_id 與 cutoff；下一步試 `codex exec --json` 的 `turn.completed.usage`，驗證後才新增實際 token 欄位，不回填舊 run。 |
| 1. 技術 brief 瘦身 | 已試跑，未接入正式每日鏈 | 不直接啟用 `1.1-summary`；先審查 12 檔方向差異、重跑完整歷史基準，再做全序列可逆欄位編碼與連續視窗／訊號衝突回退。約 80 KB／批是早期目標，現有抽樣試驗約 0.36 MB／批，但品質未過關。 |
| 2. 事件 brief 去重 | 已試跑，未接入正式每日鏈 | 10 則 high／medium 變動須核對原文與相鄰段落；試同 Snapshot 的 ID→完整 parent 原文、保留全部事件的階層式讀取，將追加讀取 token 一併計入，未能排除資料流失前不以摘錄取代正式 brief。 |
| 3. 多空與交易精簡 | 部分完成 | 交易帳戶摘要已去重；多空共同輸入仍保留分析師發現、事件重大性、反證與缺口，後續只精簡能證明等價的重複欄位。維持 `shared_input_sha256`、全池覆蓋與隔離。 |
| 4. 減少無效重試 | 部分完成：#59 單股硬上限已改由程式處理 | 對其餘 Guard 失敗分類：可預先確定的硬規則由配置程式計算，僅不確定的市場風險交 Agent；修正後仍重建 Scenario／Guard。量測重試率與 `revise` 次數，不以提示詞或放寬 Guard 保證符合數值限制。 |
| 5. 驗證節省與品質 | 部分完成 | 已做同 Snapshot 技術／事件試跑與原始 Validator 核對；先補完整歷史自身的重跑變異，再覆核差異、擴充跨日案例，統計追加檢索與重試後的總 token／bytes；確定可接入後才比較正式新 run。試驗另存，不覆寫不可變舊封存。 |

## 底線

- 瘦身後 Validator 仍須用原始資料重建並驗證；摘要只給 Agent 看，不能取代可重算的原始序列。
- 不得因輸入變小讓 Agent 把有資料的股票標成 unknown；摘要要明確標示哪些是真實缺口。
- `max_revisions` 維持 3（`allocation.py` 會驗證此值）。
- 股數、現金與單股／產業權重由 Python 與 Guard 負責，風險 Agent 不輸出數字；交易受限或情境無法符合全部硬規則時仍 fail-closed。
- 修改 Agent 輸入契約時，同步更新 `docs/architecture.md` 與相關 Skill 的 `references/`。

## 風險

- **技術摘要失真**：先保留完整原始序列，以同一 Snapshot／cutoff／Policy 對照壓縮前後的逐股 `outlook`、趨勢理由、引用、缺口與 Validator 結果；另挑選反轉、震盪、跳空及資料不足的股票檢查是否失去判斷依據。有資料卻變成 `unknown`、引用無法核對，或關鍵方向改變卻無法由保留的證據解釋時，不採用該版摘要；補回必要錨點後重新比較。byte 降幅不作為單獨驗收標準。
- **事件與多空證據流失**：對每批核對原始事件 ID、時間、證據 ID 與逐股覆蓋，確認各事件都能判斷重大性；對多空及交易結果核對 finding／claim ID 仍可追到原始證據，反證與缺口沒有消失。測試遺漏事件、錯股引用、刪除反證及改寫摘要必須 fail-closed；若無法維持完整引用，回退該步精簡，不放寬 Validator。
- **節省幅度尚無可核對的 token 紀錄**：舊版 runner 未記錄 Codex JSONL usage，既有 10/19 run 仍只能報 brief bytes。後續可試 `codex exec --json` 實測每次 input／cached／output token，並將追加原文讀取、重試納入；本機與格式未驗證前不宣稱可用。沒有真實數值就明寫「未量得」，不得把 bytes 換算成確定 token、把 `estimated_usage_usd=0` 當成零成本，或把 API 快取定價套到訂閱 CLI。
