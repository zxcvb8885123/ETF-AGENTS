# 市場轉向時的持股曝險：由 Agent 判斷、程式守界線

狀態（2026-11-10）：**階段 1（事實與紀錄）已實作，閘門 `position_sizing.exposure_view` 已開啟（2026-11-10，使用者決定）；純紀錄、不影響配置；回滾＝刪掉 `position_sizing.exposure_view`**（使用者 2026-11-10 下令開工階段 1；實作紀錄見第 10 節）；階段 2、3 尚未實作，開工需使用者另行下令。第 7 節五項設計取捨已由使用者於 2026-11-10 決定；這只決定了設計；開啟閘門是使用者另行決定的。第 1～9 節是計畫原文（撰寫時不改程式、設定、Skill 與既有文件）；文中 `exposure_view` 已照暫名實作，`purpose`、`review:<代號>:exposure` 等其餘新名稱仍是提案、名稱暫定。

數字是用 2026-11-10 封存 run（`decision-20261109T230435Z`）的 `team_inputs.json`、`intent.json`、`debate.json`、`decision_input.json` 以暫存腳本唯讀重算的，證據等級一律是 `replay_reference_only`：只說明「當天 Agent 會看到什麼事實」，不代表 Agent 會怎麼判斷，也不代表改了績效會更好。

與平行工作的關係（先講結論）：**本題併入 [換股改由交易 Agent 提名](agent_nominated_rotation_plan.md)（`agent_swap`），不另立一條換股路徑**。那份計畫已經回答「誰換誰由交易 Agent 提名」；本檔只補它沒有涵蓋的三件事（第 3 節），並指出兩處必須協調的衝突（第 4.3 節）。上層方向見 [把策略裁量還給 Agent](agent_autonomy_plan.md)。

## 1. 問題（事實）

- 當天持股 27 檔。含現金組合 beta 0.106、持股 beta 0.129；63.5% 的權重在 beta 低於 0.3 的持股（18 檔）；現金 18.0%。
- 產業代碼 `industry:17`（金融保險）8 檔、權重合計 26.0%；其中 2880、2886、2891 是 `hold`／high。另外電信 2412（權重 7.0%，beta −0.24）與 3045 也是 `hold`／high。
- 最近交易日（11-09）交易池等權 +5.51%，加權指數 +4.94%；以起始權重乘當日個股報酬估算，持股部位當天約 +0.67%。
- 轉向事實：regime 為 bear、站回 MA5 比例 0.32、20 日新低比例 6.7%。風險 Agent 當天填 `turn_unconfirmed`、姿態 `defensive`／`lower`。
- 26 檔 `hold` 中 24 檔的 20 日相對交易池報酬為正（+1%～+32%），5 日相對報酬也幾乎全為正。交易 Agent 常寫的重估條件「持續落後交易池」，對這類持股在反彈初期**不會觸發**：它們跌的時候跌得少，所以即使反彈日沒跟上，相對報酬仍是正的。
- 當天程式的結果：`REBOUND_PARTICIPATION:candidate`、`EXPOSURE_ROTATION_SKIPPED:beta_at_target`（當時 candidate 的停止線是 0.01）、`CONVICTION_GAP_SWAP_SKIPPED:no_eligible_fill`。沒有任何換股。

## 2. 診斷：每一步是誰決定的

| 步驟 | 現在由誰決定 | 位置 | 本該由誰 |
| --- | --- | --- | --- |
| 市場有沒有轉向 | 風險 Agent（`turn_assessment` 列舉）；資料不可用或回撤下限為防守時程式壓回 none | `decision/turn_facts.py` 的 `validate_turn_assessment`、`decision/rebound.py` 的 `effective_state` | Agent（現況正確） |
| 轉向後組合要參與到什麼程度 | **程式**：列舉直接對映成 beta 停止線（candidate 0.30、confirmed 0.55） | `rebound.apply_rebound`、Policy 的 `rebound_participation.by_state.*.defensive_target`、`exposure_rotation.beta_target_by_stance` | Agent 判斷方向與急迫程度；人設上限 |
| 風險 Agent 判斷時看不看得到組合曝險 | **看不到**：簡報有帳戶持股清單，沒有組合 beta、低 beta 占比 | `automation/risk_stages.py`（簡報只附 `account` 的 cash／nav／positions） | 應該看得到（事實） |
| 一天能換幾檔、持有幾天才能換 | 人設數字（2 檔／5 天；confirmed 4 檔／0 天） | 同上 `by_state`、`exposure_rotation.max_swaps_per_run`／`min_holding_days` | 人（硬性界線，現況正確） |
| **哪些持股讓出位置** | **程式**：`hold`、信心非 high、持有天數夠、beta 低於組合；排序是信心 low 優先、再 beta 由低到高、再代號 | `decision/exposure.py` 的 `exposure_rotation_plan`、`decision/allocation.py` 的 `_exposure_rotation` | Agent |
| 哪些新股補進來 | 交易 Agent 給 buy 名單與名次；**程式**再用「beta 高於組合」篩 | 同上 | Agent |
| 交易 Agent 能不能因為「組合太防守」而放掉某檔 | **Skill 明文禁止**：「市場參與度低本身不是賣出理由」「不得純粹為了買別檔而賣」「hold 信心只依這檔自己的證據」 | `tradeagent/trading/trader/SKILL.md`、`references/data-rules.md`、`references/swap-nomination.md`、`exposure.REVIEW_NOTE` | 這正是使用者要 Agent 做的判斷 |
| 交易 Agent 判斷持股時看不看得到整體 | 第一輪分批，看不到別批；只有第二輪排序與換股提名（5c）看得到全部 | `automation/team_stages.py` | 整體判斷要放在看得到全部的那一步 |
| 重估條件會不會提醒 | 程式給相對交易池報酬；對低 beta 持股在反彈初期結構性不觸發 | `decision/position_review.py` 的 `relative_to_pool_*` | 程式應補「上漲日跟不跟得上」的事實 |

被程式固定、本該由 Agent 判斷的環節有三個：**參與到什麼程度**（列舉直接換成 beta 數字）、**誰讓位**（信心與 beta 的排序公式）、**能不能以組合理由讓位**（Skill 直接禁止）。另有兩個「Agent 看不到事實」的缺口：風險 Agent 看不到組合 beta；交易 Agent 的換股提名簡報沒有轉向與大盤事實。

使用者指出的三個缺口對應如下：

- (a) 反彈第一天無法確認：這是判斷，程式解不了。能做的是讓 Agent 看到組合有多防守，自己權衡「還沒確認但參與度極低」。
- (b) 一次 2 檔、持有 5 天：這是人設的硬性界線，用來限制換手與來回交易；要不要放寬是人的決定，不是 Agent 的。
- (c) 被換掉的是信心低的、不是 beta 低的：這是程式的排序公式，應該交給 Agent。

## 3. 以 Agent 為主軸的設計

### 3.1 分工

| 誰 | 做什麼 | 形式 |
| --- | --- | --- |
| 風險 Agent | 看市場事實與組合曝險事實，回答「現在該不該開始把市場參與度補回來」 | 列舉 `exposure_view`：`keep_defensive`（維持防守）／`start_rebuild`（開始回補）／`rebuild`（回補），附理由與市場層級證據 |
| 交易 Agent（換股提名那一步，看得到全部持股與全部新股） | 回答「如果要回補，哪些持股願意讓出位置、換成哪檔新股、為什麼」 | 沿用 `agent_swap` 的有序配對清單，每對多一個列舉 `purpose`：`thesis`（持股自己的理由）或 `exposure`（為了提高市場參與度讓位） |
| 程式 | 整理事實；把 Agent 的配對在硬性界線內執行；記錄；整份重算驗證 | 不挑人、不排序、不設 beta 目標 |
| 人 | 設硬性界線的數字；決定閘門開不開 | Policy |

Agent 的輸出只有列舉、代號、識別碼與文字，沒有數字。

### 3.2 兩把鑰匙怎麼接

換股提名在風險 Agent 之前（[換股提名計畫](agent_nominated_rotation_plan.md)已決定不改順序），所以交易 Agent 提名時不知道風險 Agent 的看法。做法是沿用它「有序候選清單、需要才用」的語意：

- 交易 Agent 可以提名 `purpose: exposure` 的配對，意思是「若風險 Agent 認為該回補，就依序用這些」。
- 配置時，`exposure` 配對只有在風險 Agent 的 `exposure_view` 不是 `keep_defensive` 時才取用；可取用的對數上限由 Policy 依列舉對映（人設的硬上限，做法同 `cash_grade` 對映 `grid`）。已決定不因回補放寬：對映的值不得高於現行的每次換股上限，`rebuild` 也一樣；它只能讓 `start_rebuild` 比 `rebuild` 少，不能讓任何一級比現在多。
- `thesis` 配對不受 `exposure_view` 影響，照換股提名計畫原本的規則。
- 兩個 Agent 都同意才會有曝險換股：風險 Agent 說「回補」但交易 Agent 沒有提名，就不換；交易 Agent 提名了但風險 Agent 說維持防守，也不換。程式兩種情況都不補挑。

### 3.3 曝險配對的依據

換股提名計畫要求換出依據必須是「第一輪已採納的空頭論點或持股事實」。以組合理由讓位的持股通常沒有空頭論點（當天 26 檔 `hold` 有 7 檔沒有任何可用依據：2412、2886、2880、2891、2892、1402、2885，其中 5 檔是金融股，三檔高信心金融股全在內）。所以需要一個新的依據種類：

- 程式為每檔持股產生曝險事實編號 `review:<代號>:exposure`，內容取自 `exposure_review`（beta、上行／下行捕捉、權重）加上「最近一個交易池上漲日該股與交易池的報酬」。它和 `review:<代號>:position` 一樣是程式算的持股事實，附該股行情的 `evidence_id`，由 Finalizer 重算。
- `purpose: exposure` 的配對，`sell_basis_ids` 必須含這個編號，**不要求第一輪已採納**（第一輪分批、本來就做不了組合層級的判斷）。`buy_basis_ids` 照舊要是新股已採納的多頭論點。
- 理由必須寫明兩件事：為什麼是這檔讓位（而不是別檔），以及放掉它的代價（例如它是高信心持股）。程式不驗文字，但必填。
- 這不違反「背景不是證據」：曝險事實是由該股行情重算的事實，不是前次決策、論點檢視或名額這類背景。組合層級的數字（組合 beta、低 beta 占比）仍是背景、不可引用。

### 3.4 高信心持股的讓位順序（已決定）

使用者 2026-11-10 的決定：高信心持股**可以**以 `purpose: exposure` 讓位，但要先處理中、低信心持股，再談高信心。使用者另已確認（2026-11-10 選 A）：這是同一階段內的順序約束，不是把高信心留到後面的階段；「先處理」指先被考慮並交代，不要求中、低信心持股必須先成交換出。

怎麼做（判斷歸 Agent，程式只驗結構）：

- 交易 Agent 提名曝險配對時，必須先交代中、低信心持股的考慮結果。輸出多一個清單 `lower_conviction_review`：對每一檔「可以曝險理由提名、第一輪信心為 medium 或 low」的持股各列一次，填列舉 `nominated`（已在本次清單中讓位）或 `kept`（不讓位），`kept` 必須附一句理由。
- 只有在這份清單完整時，才可以提名高信心持股；該對的理由必須另外寫明放掉高信心持股的代價。
- 在配對清單裡，高信心持股的曝險配對必須排在所有中、低信心持股的曝險配對之後。配置時依清單先後取用，所以對數上限不夠時，先成交的一定是中、低信心的配對。
- 沒有提名高信心持股時，不需要這份清單。

程式驗什麼、不驗什麼：

| 程式驗（結構） | 程式不做（判斷） |
| --- | --- |
| 提名了高信心持股時，`lower_conviction_review` 是否剛好涵蓋每一檔可提名的中、低信心持股各一次 | 不判斷 `kept` 的理由好不好 |
| 標 `nominated` 的是否真的出現在配對裡、標 `kept` 的是否沒有出現 | 不替 Agent 決定哪一檔中、低信心持股該先讓位 |
| 高信心配對是否排在中、低信心曝險配對之後 | 不依 beta、信心或代號排序，不設「beta 低於多少才能換」 |
| 高信心配對的理由欄是否非空 | 不要求中、低信心持股必須全部換完 |

為什麼這樣仍是 Agent 主導：

- 「先中低、後高信心」是使用者定的界線，性質與每次對數上限相同，由人設、程式守。
- 界線之內，哪一檔中、低信心持股不適合讓位、為什麼，完全由 Agent 說明。這一點很重要：當天信心 low 的 8046、3374 的 beta 是 1.59 與 1.73，它們本來就在參與反彈，照「低信心先換」的死規則換掉它們反而降低參與度。`kept` 加理由讓 Agent 可以跳過它們，這正是現行排序公式做不到的事。
- 程式沒有任何一步在挑人或排序，只確認 Agent 有沒有交代。

取捨（如實說明）：

- 這個驗證確實是一條程式強制的規則，但它驗的是「有沒有說明」，不是「說明對不對」。風險是 Agent 可能對每一檔都填 `kept` 加一句敷衍的理由，程式擋不住；這要靠影子期的紀錄由人檢視。
- 代價是輸出變長：當天可提名的中、低信心持股有 19 檔（medium 12、low 7），提名高信心持股就要逐檔交代。這個成本是刻意的——它讓「放掉高信心持股」比「放掉中低信心持股」更費事。
- 較輕的替代做法是只要求一段文字說明。它對 Agent 負擔小，但程式無從確認是否涵蓋了每一檔，等於沒有驗證。**建議採逐檔清單。**

### 3.5 Agent 判斷時看得到的事實（全部由程式算）

| 事實 | 給誰 | 來源 | 現況 |
| --- | --- | --- | --- |
| 組合 beta、持股 beta、低 beta 占比、現金權重 | 風險 Agent、交易 Agent（提名步驟） | `exposure_review.portfolio` | 風險 Agent 目前沒有 |
| 交易池轉向事實、最近 5 日路徑 | 風險 Agent、交易 Agent（提名步驟） | `turn_facts`、`turn_state` | 提名簡報目前沒有 |
| 加權指數事實 | 同上 | `market_index_facts` | 提名簡報目前沒有 |
| 每檔持股的 beta、上行捕捉、下行捕捉 | 交易 Agent | `exposure_review.items` | 已有（背景） |
| 每檔持股 5／20 日相對交易池報酬 | 交易 Agent | `position_review` 的 context | 已有 |
| 最近一個交易池上漲日，每檔持股與交易池的報酬 | 交易 Agent | 新增（由 `price_series` 算） | 沒有 |
| 持股依產業的權重合計 | 風險 Agent、交易 Agent（提名步驟） | `data/sector_classification.json` 加帳戶權重 | 風險審查階段才有 `sector_exposure` |
| 被名額擋下的新股的 beta 與上行捕捉 | 交易 Agent（提名步驟） | `BetaModel` | 沒有（目前只算持股） |

### 3.6 什麼留在程式

硬性界線，Agent 不能放寬，全部照舊：

- 每次換股對數上限（`rotation.max_swaps_per_run`、`agent_swap.max_swaps_per_run`，以及本計畫依 `exposure_view` 對映的上限）。
- 換手上限（`turnover_fit`、`max_turnover_rate`）、現金下限（`cash_ratio_range.grid`、`cash_buffer_floor`）、單股與產業上限、Active Share。
- 持有天數下限、再買冷卻、一換一（不只賣不買）。
- 高信心持股讓位前必須先交代中、低信心持股（第 3.4 節；使用者定的順序界線，程式只驗結構）。
- 減碼日與回撤強制防守時整個略過。
- 持股 20～30 檔、CompetitionGuard、整份重算與不可變封存。

從程式移走的只有三樣：beta 停止線（不再由列舉換成 beta 數字）、換出者的排序公式、「參與度低不是賣出理由」這條禁令（只在 `purpose: exposure` 的配對下解除）。

### 3.7 為什麼不違反「確定性歸程式、不確定性歸 LLM」

- 「市場是不是在轉向」「組合是不是太防守」「哪一檔最該讓位」都沒有可重算的正確答案，是不確定性。現行做法把它們寫成 beta 目標與排序公式，等於讓程式替 Agent 判斷。
- beta、捕捉率、報酬、權重、張數、費稅、上限檢查都有唯一答案，仍然全部由程式算，Agent 不輸出任何數字。
- 列舉對映數字（`exposure_view` → 對數上限）與 `cash_grade` → `grid` 是同一種做法：Agent 選方向，人設數字，程式執行。

## 4. 與現有閘門的關係

### 4.1 併入 `agent_swap`，不另立換股閘門

| 項目 | 做法 |
| --- | --- |
| 交易 Agent 的曝險配對 | `agent_swap` 的下一個版本或子區塊（暫名 `agent_swap.exposure`）：欄位只有依 `exposure_view` 對映的對數上限（不得高於現行每次換股上限），以及高信心持股讓位的順序約束（第 3.4 節）。沿用它的提名步驟、封存、降級規則與配置時的取消規則 |
| 風險 Agent 的 `exposure_view` | 獨立小閘門（暫名 `position_sizing.exposure_view: {"version": "v1"}`），做法比照 `cash_stance_turn_facts`：閘門決定現金姿態輸出允許的欄位。單獨開啟時只是紀錄 |
| 新事實 | 進風險 Agent 簡報與提名簡報；曝險事實編號需要 `exposure_review` 出新版本 |

全部預設不存在＝關閉；只由人設定、策略反思不得改；沒有欄位的舊 Policy／舊 run 逐位元不變；回放略過並在 notes 標示。

### 4.2 `team_inputs`

- `exposure_view` 放在現金姿態輸出內，不升版（與 `turn_assessment`、`cash_grade` 相同做法）。
- 配對的 `purpose` 欄位屬於 `swap_nomination` 的新版本；`team_inputs` 版本是否要動，取決於換股提名計畫 2.9 的欄位白名單寫法，**待確認**。
- `exposure_review` 加曝險事實編號會改變它的內容，須出新版本並由閘門決定，舊版本不動。

### 4.3 與換股提名計畫必須協調的地方（本檔不修改它）

1. **Skill 文字衝突**：`references/swap-nomination.md` 寫「市場參與度低本身不是賣出理由」「不得純粹為了買別檔而賣」。本計畫只在 `purpose: exposure` 的配對下解除前一句。做法是條件式說明（簡報出現曝險區塊時才適用），不是改掉原句。
2. **依據規則衝突**：它要求換出依據必須第一輪已採納。本計畫對 `exposure` 配對開一個例外（第 3.3 節）。這是那份計畫第 2.4 節的規則變更；本檔這一側已決定採例外（第 7 節第 4 項），那份計畫的文字要在實作時同步修改。
3. **`rebound_participation` 與 `exposure_rotation` 的去留**：換股提名 v2 開啟後，它們對換股的放寬已經失效，只剩現金與換手的選填放寬（目前都沒填）。本計畫開啟後，`turn_assessment` 回到純紀錄。是否正式退場，沿用上層計畫的決定：先觀察滿 10 個決策日。
4. **影子期**：換股提名目前是 v1 影子（配置不讀提名）。本計畫的第二階段可以搭同一個影子期，不需要另開。

### 4.4 過渡期

換股提名 v2 還沒開之前，現行的 `rebound_participation` v2 加 `exposure_rotation` v3 照舊運作，本計畫不動它們的數字。第一階段只加事實與紀錄，與現行路徑並存。

## 5. 風險與反例

| 風險 | 說明 | 擋法 |
| --- | --- | --- |
| 過早追高、假反彈 | 風險 Agent 填回補、交易 Agent 提名，結果隔天續跌；換進來的高 beta 股跌更多 | 兩個 Agent 都同意才動；每次對數有硬上限；現金下限與換手不放寬；回撤下限為防守時整個略過；買進後仍受持股檢視 |
| 換手與滑價 | 一對約等於兩倍單股權重的成交額；連續幾天回補會吃掉換手額度 | `turnover_fit` 是最終上限；持有天數下限與再買冷卻擋來回 |
| 放掉高信心金融股的代價 | 金融股在下跌時保護了淨值；若反彈失敗，放掉它們等於同時失去保護又買在高點；冷卻期內買不回 | 已決定可以讓位但有順序：必須先逐檔交代中、低信心持股，高信心配對排在清單最後、對數不夠時最先被捨棄（第 3.4 節）；理由必須寫明代價；影子期先看 Agent 會提名誰 |
| 順序約束流於形式 | 已決定「交代即可、不要求先成交」，所以 Agent 可以對每檔中、低信心持股都填 `kept` 加敷衍理由，直接提名高信心持股 | 程式只能驗結構，擋不住；影子期記錄「提名高信心持股時 `kept` 的檔數與理由」，由人檢視後才決定是否進正式階段 |
| 單一 Agent 被帶偏 | 新聞或公告文字誘導「已經轉向」 | `exposure_view` 必須引用市場層級證據（比照現金姿態）；外部文字照舊是不受信任資料；交易 Agent 的配對另需各自的依據 |
| 列舉變成變相數字 | `exposure_view` 三級對映對數，Agent 可能每天都選最高級 | 對映是上限不是目標；交易 Agent 沒提名就不換；影子期記錄選擇分布 |
| 曝險事實被當成賣出理由濫用 | `review:<代號>:exposure` 出現在非曝險配對或第一輪 | Validator：只能出現在 `purpose: exposure` 的 `sell_basis_ids`；第一輪採納即拒絕 |
| 背景被引用 | 組合 beta、轉向事實、加權指數事實寫進依據 | 維持 `citable=false`、沒有識別碼；文字欄位出現前次決策識別碼即拒絕 |
| 驗證薄弱 | 舊封存沒有這些輸出，回放不了 | 只做等價測試與影子紀錄，全部標 `replay_reference_only`；只有比賽期間的即時紀錄算證據 |

### 5.1 11-10 當天這個設計會讓 Agent 看到什麼（事實，不含 Agent 的決定）

風險 Agent 會多看到：

- 組合 beta 0.106、持股 beta 0.129、低 beta 占比 63.5%、現金 18.0%。
- 金融保險 8 檔合計權重 26.0%。
- 已有的：交易池當日 +5.51%、站回 MA5 比例 0.32、20 日新低比例 6.7%、regime 連續 bear；加權指數當日 +4.94%、近 5 日 −7.25%、近 20 日 −16.35%。

交易 Agent 在提名步驟會多看到：

| 持股 | 權重 | beta | 上行捕捉 | 當日報酬（交易池 +5.5%） | 20 日相對交易池 | 第一輪 | 現行規則下可否被提名 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2412 | 7.0% | −0.24 | −0.02 | −2.8% | +27.7% | `hold`／high | 否（沒有已採納依據） |
| 2886 | 4.9% | −0.22 | −0.12 | −1.3% | +24.5% | `hold`／high | 否 |
| 2880 | 4.1% | −0.05 | +0.22 | +1.3% | +24.2% | `hold`／high | 否 |
| 2891 | 3.0% | +0.05 | +0.32 | +3.4% | +28.9% | `hold`／high | 否 |
| 2885 | 4.7% | +0.39 | +0.64 | +3.7% | +17.6% | `hold`／medium | 否 |
| 4904 | 5.4% | −0.29 | −0.23 | −5.4% | +18.4% | `hold`／medium | 可 |
| 1216 | 4.1% | −0.01 | −0.05 | −0.1% | +20.0% | `hold`／low | 可 |

- 依已決定的順序，交易 Agent 若要提名上表的高信心持股（2412、2886、2880、2891），必須先對 19 檔可提名的中、低信心持股逐檔交代；其中 4904、1216 這類低 beta 的中、低信心持股會先被考慮。
- 現行曝險換股的排序是「信心 low 優先」，所以即使觸發，先被換的會是 1216、2603、2609、3034、2379、8046、3374 這類 low 持股；其中 8046、3374 的 beta 是 1.59 與 1.73，換掉它們反而降低參與度。這是缺口 (c) 的具體樣子。
- 被名額擋下的 7 檔買進中，medium 的有 1815、3665、6770、8415；它們的 beta 目前不在任何簡報裡。
- 當天風險 Agent 實際填的是 `turn_unconfirmed`。若換成本設計，它會不會填「開始回補」、交易 Agent 會不會提名金融股，**無法由重算得知**，本檔不推測。

## 6. 分階段、測試與文件

| 階段 | 範圍 | 進入條件 | 影響配置 |
| --- | --- | --- | --- |
| 1 事實與紀錄 | 風險 Agent 簡報加組合曝險與產業權重事實；風險 Agent 多填 `exposure_view`（純紀錄）；提名簡報加轉向、加權指數、組合曝險、上漲日報酬、新股 beta | 使用者下令開工 | 否 |
| 2 影子 | 提名多一個 `purpose`、曝險事實編號與 `lower_conviction_review`（含高信心讓位的順序驗證）；配置不讀 | 換股提名 v1 影子已在跑；換股提名計畫與 Skill 依第 4.3 節 1、2 項同步修改 | 否 |
| 3 正式 | 配置依 `exposure_view` 取用 `exposure` 配對；高信心配對與中、低信心配對同一階段上線，但排在清單最後（已決定，第 3.4 節） | 換股提名 v2 已開；第 1、2 階段有即時紀錄；人看過後決定 | 是 |

測試（`unittest.TestCase` 風格）：

- **事實重算**：組合曝險、上漲日報酬、新股 beta 以同一 bundle 重算，任一欄被改即拒絕；任何 bar 晚於最近交易日拒絕；缺行情為 `null`、不補值；交易池沒有上漲日時該欄不可用。
- **列舉**：`exposure_view` 必填與值域；非 `keep_defensive` 時必須引用至少一個市場層級證據；沒有閘門時輸出帶此欄位即拒絕。
- **依據**：曝險事實編號只能出現在 `purpose: exposure` 的換出依據；`exposure` 配對缺它即拒絕；引用別檔的編號拒絕；文字含前次決策識別碼拒絕。
- **高信心讓位的順序**：提名高信心持股而缺 `lower_conviction_review`、清單漏列或重複、`kept` 沒有理由、標 `nominated` 卻不在配對裡、高信心配對排在中、低信心曝險配對之前、理由欄為空，一律退回重答；沒有提名高信心持股時帶這份清單也拒絕；對數上限不足時先被捨棄的是清單尾端的高信心配對。
- **兩把鑰匙**：`keep_defensive` 時 `exposure` 配對一對都不成交且換出者不賣；沒有提名時程式不補挑；超過對映上限從清單尾端取消。
- **硬性界線**：減碼日與回撤強制防守略過；換手、現金、單股、產業、檔數任何一項不過，該對取消、換出者不賣；不只賣不買。
- **降級**：提名失敗只取消換股，其餘訂單照常（沿用換股提名計畫的規則）。
- **舊 run 不變**：沒有閘門的 Policy，風險 Agent 與提名簡報逐鍵相同；封存 run 重建雜湊不變。

實作時要同步的文件（本次不改）：`AGENTS.md`（風險 Agent 與交易 Agent 邊界、閘門清單）、`README.md`、`docs/status.md`、`docs/decision-contracts.md`、`docs/architecture.md`、`docs/glossary.md`、`docs/README.md`、`docs/agent_nominated_rotation_plan.md`、`docs/agent_autonomy_plan.md`、`docs/rebound_participation_v2_plan.md`、`docs/exposure_rotation_v2_plan.md`、`docs/operations.md`；Skill：`tradeagent/trading/trader/`（`SKILL.md`、`references/data-rules.md`、`references/swap-nomination.md`）、`tradeagent/risk/portfolio-risk-review/`（`references/cash-stance.md`）。

## 7. 已決定（使用者 2026-11-10）

五項都已決定：第 1、2、4、5 項照建議，第 3 項為使用者自己的決定。**這些是設計取捨的決定，不是開工指令：全部尚未實作，開工需使用者另行下令；閘門是否開啟仍由人逐一決定。**

1. **併入換股提名**（已決定，照建議）：併入 `agent_swap`，本檔只補三件事，不另立曝險換股閘門。理由：同一個步驟、同一份清單、同一套取消規則，分開做會有兩條互相搶名額的換股路徑。
2. **風險 Agent 的回補意見**（已決定，照建議）：新增列舉 `exposure_view`，先當紀錄。理由：`turn_assessment` 問的是「市場有沒有轉向」，`exposure_view` 問的是「組合要不要開始回補」；組合極度防守時兩者的答案可以不同。
3. **高信心持股能不能以曝險理由讓位**（**已決定，使用者 2026-11-10**）：可以，但要先把中、低信心持股處理完，再談高信心。兩個解讀也已決定（使用者 2026-11-10 選 A）：
   - 「先換掉」＝先被考慮並交代即可，Agent 可以說明某檔中、低信心持股不適合讓位。理由紀錄：當天信心 low 的 8046、3374 的 beta 最高，強制先成交換出它們會降低參與度，也等於回到程式排序。
   - 「再來說高信心」＝同一階段內以順序約束處理，不另開後續階段。理由紀錄：順序由清單先後與逐檔交代保證，對數不夠時最先被捨棄的就是高信心配對。
   - 順序驗證的形式建議採逐檔清單 `lower_conviction_review`（第 3.4 節）；這一點是本檔的建議，使用者沒有單獨回覆。
4. **曝險配對的換出依據**（已決定，照建議）：用程式算的曝險事實編號 `review:<代號>:exposure`，不要求第一輪採納。理由：第一輪分批，做不了組合層級的判斷；否則當天 7 檔（含三檔高信心金融股）永遠不能被提名。
5. **每次對數上限**（已決定，照建議）：維持現行上限，只改「誰決定換誰」，不因回補放寬。理由：先把判斷交還 Agent；速度是另一個問題，等有即時紀錄再談。

仍待確認的技術問題（不是取捨，實作時查明）：配對新增 `purpose` 後 `team_inputs` 版本是否要動；順序驗證的形式（本檔建議逐檔清單，使用者沒有單獨回覆）。

## 8. 回答「是不是倒過來了」

只列觀察，不改任何文件。

- **目前的分界偏向程式。** 數字計算與硬性規則在程式，這是對的；但近期幾個閘門把「判斷」也寫成了公式。Agent 給的是逐檔意圖與幾個列舉，真正決定「賣誰、換誰、參與多少」的是程式。[把策略裁量還給 Agent](agent_autonomy_plan.md) 的統計也指向同一件事：11 份決策 256 筆買進意圖只有 40 筆有買單。
- **值得回頭檢視的設計**：
  - `rebound_participation` 的 `by_state`：Agent 選一個列舉，程式就把它換成 beta 0.30 或 0.55。方向是 Agent 的，幅度是公式的。
  - `exposure_rotation` 與 `conviction_gap_swap` 的換出者排序：信心、beta、代號，Agent 沒被問過。
  - `drawdown_derisk` 依 ATR 挑股賣：已發生過賣掉 Agent 明說不該賣的持股。
  - Skill 裡的禁令（「參與度低不是賣出理由」「hold 信心只看這檔自己」）：它們防的是亂賣，但同時禁止了組合層級的判斷。
  - 我在 [quality_dip_plan.md](quality_dip_plan.md) 寫的第三階段也有同樣傾向：用一串程式條件決定哪個買進可以換股。候選標示只當事實是對的；第三階段的換股應該改走換股提名，由交易 Agent 提名，而不是再開一條程式配對路徑。
- **不該回頭的部分**：硬性界線、資料時間、證據、重算驗證、不可變封存。把判斷還給 Agent，不等於放寬這些。
- **一個簡單的檢查方法**：新設計裡如果出現「程式依 X 排序挑出一檔」或「列舉對映成一個目標值讓程式去追」，那一步多半就是把判斷寫進了程式。可以接受的形式是「Agent 選、程式檢查可不可行、不可行就取消並留紀錄」。

## 9. 摘要

- 問題：組合 beta 只有 0.11、金融股占 26%，反彈日交易池 +5.5%、持股約 +0.7%；重估條件「落後交易池」對低 beta 持股在反彈初期不會觸發。
- 診斷：參與到什麼程度、誰讓位、能不能以組合理由讓位，這三件事現在都由程式公式或 Skill 禁令決定，不是 Agent。
- 設計：風險 Agent 用列舉表達要不要回補；交易 Agent 在換股提名那一步提名哪些持股讓位、為什麼；兩者都同意才換。程式不挑人、不排序、不設 beta 目標。
- 留在程式的：每次對數、換手、現金下限、單股與產業上限、持有天數、冷卻、Guard、整份重算。全部不放寬。
- 與平行工作：已決定併入換股提名（`agent_swap`），不另立換股路徑；那份計畫與 Skill 有兩處文字要在實作時同步修改（Skill 禁令、換出依據）。
- 當天事實：7 檔持股（含三檔高信心金融股）在現行規則下不能被提名；現行排序會先換掉信心低的，其中兩檔 beta 反而最高。
- 第一階段：只加事實與紀錄（風險 Agent 看得到組合曝險、多填一個列舉；提名簡報補市場事實），不改配置。
- 反省的回答：分界目前偏向程式，幾個近期閘門把判斷寫成了公式；硬性界線與驗證不該動。
- 已決定：高信心持股可以曝險理由讓位，但要先逐檔交代中、低信心持股，且排在清單最後；程式只驗結構。與中、低信心同一階段上線；「先處理」指先交代，不要求先成交。
- 其餘四項也已決定照建議：併入換股提名、新增 `exposure_view`、換出依據用曝險事實編號、每次對數上限維持現行。全部尚未實作，開工需使用者另行下令。

## 10. 階段 1 實作紀錄（2026-11-10）

使用者 2026-11-10 下令開工階段 1。只做第 6 節表的第 1 列；第 7 節的決定不變。實作時閘門預設關閉；**使用者 2026-11-10 決定開啟**：`config/decision_policy.json` 的 `position_sizing.exposure_view: {"version": "v1"}`，純紀錄、不影響配置，回滾＝刪掉該欄位。

- **閘門**：`position_sizing.exposure_view: {"version": "v1"}`（沿用第 4.1 節的暫名），形狀只有 version。前提（`GATE_PREREQUISITES`）：`exposure_rotation`（同一個 beta 口徑與 `exposure_review`）、`cash_stance_turn_facts` 與 `market_index_facts`（提名簡報要附的轉向與加權指數事實由它們算）；`typed_evidence_v1` 在驗證現金姿態時檢查。不要求 `agent_swap`：單獨開啟時只有風險 Agent 那一半。策略反思不得改（`FORBIDDEN_PREFIXES`）；回放略過閘門並拿掉封存姿態的 `exposure_view`（notes 標 `ignored_in_replay`）。
- **事實**（`src/etf_agent/decision/exposure_facts.py`）：只讀 bundle 與 Policy 中配置前後相同的欄位，不讀任何 Agent 輸出。組合曝險重用 `build_exposure_review`；產業權重讀 Policy 綁定的 `sector_by_symbol`（不在執行時讀 `data/sector_classification.json`），金融保險為 `industry:17`；上漲日用 `turn_facts` 的池報酬函式，往回最多 20 個交易日；持股當日貢獻＝Σ（本次決策起始權重 × 當日收盤報酬），這個口徑能重現第 5.1 節的 +0.67%（用前一日回推的權重是 +0.57%、只算持股部位是 +0.70%）；非持股的 beta 全部存起來，提名簡報只列候選。
- **風險 Agent**：簡報多 `exposure_facts`（背景、不可引用、沒有 facts_id）；輸出多必填列舉 `exposure_view`。非 `keep_defensive` 時須引用市場層級證據（專屬檢查與訊息；在 `typed_evidence_v1` 下原本就要求至少一個市場層級證據，所以這條是重申）；理由寫出 `exposure_facts` 或 facts_id 即拒絕。純紀錄，配置不讀。
- **換股提名簡報**：`agent_swap` 也開啟時多一個 `exposure_facts` 區塊。接在 `automation/team_stages.py`，**沒有改 `swap_nomination.py`**，提名的驗證規則、輸出欄位與封存都不變；Skill 的禁令句子也沒改，只在 `swap-nomination.md` 加一節說明這個區塊是背景。
- **team_inputs**：選配鍵 `exposure_facts`（做法同 `dip_facts`），不升版、須 2.7 以上，與閘門雙向檢查，Finalizer 整份重算。`exposure_view` 放在現金姿態內（同 `turn_assessment`）。與 `agent_swap` 的 2.9 白名單不衝突：選配鍵在版本白名單之外另行檢查。
- **封存資料唯讀重算**（2026-11-10 `decision-20261109T230435Z`，`replay_reference_only`）：組合 beta 0.105601、持股 beta 0.128716、低 beta 占比 0.635254（18／27 檔）、現金 0.179580、金融保險 8 檔 0.260000、上漲日為 11-09（最近交易日）池報酬 +0.055064、持股當日貢獻 +0.006667，與第 5.1 節一致；第 5.1 節表中 7 檔的權重、beta、當日報酬也一致（`tests/decision/test_exposure_view.py`）。
- **階段 2 待辦**（本次刻意不做）：提名的 `purpose`、曝險事實編號 `review:<代號>:exposure`、`lower_conviction_review`、提名理由的背景識別碼檢查（需要改 `SwapNominationValidator`，屬於換股提名的驗證規則），以及第 4.3 節 1、2 項對換股提名計畫與 Skill 禁令句子的同步修改。

## 11. 階段 2、3 實作紀錄（2026-11-10）

使用者下令做階段 2、3；**閘門沒有開**（`config/decision_policy.json` 未改），行為為「已實作、尚未開啟」。

- **閘門**：`position_sizing.agent_swap.exposure`，v1 影子（只有 `version`）／v2 正式（另有 `max_pairs_by_view`＝`start_rebuild`、`rebuild` 兩格整數，0≤`start_rebuild`≤`rebuild`≤`max_swaps_per_run`；`view` 由 pipeline 綁）。前提 `agent_swap` 與 `exposure_view`（`GATE_PREREQUISITES` 的 `agent_swap.exposure`）；v2 另需 `agent_swap` v2。`keep_defensive` 沒有對數格：恆為 0。策略反思不得改（`agent_swap` 前綴已在禁區）。沒有這個欄位的 Policy 逐位元不變。程式在 `decision/exposure_swap.py`。
- **階段 2（提名）**：每對必填 `purpose`；曝險事實 `review:<代號>:exposure`（`exposure_basis_by_symbol`：持股權重、beta、上行／下行捕捉、產業、上漲日報酬，附該股行情 evidence_id，由 `team_inputs.exposure_facts` 重算）；`exposure` 配對的換出者不需要空頭論點；`lower_conviction_review`（第 3.4 節）；提名理由出現曝險事實區塊識別碼即拒絕。Agent 輸出 Schema 為 `SWAP_NOMINATION_EXPOSURE_SCHEMA`（多 `purpose`、`lower_conviction_review`，其餘欄位不變）。簡報與提示不告知影子期，v1／v2 完全相同。
- **階段 3（配置）**：`allocation._agent_swap` 在 v2 依綁定的 `view`：`keep_defensive` 取消全部 exposure 配對（`AGENT_SWAP_CANCELLED:exposure_view`）；其餘依 `max_pairs_by_view` 取清單前幾對、超過取消（`exposure_cap`，尾端的高信心配對先捨棄）；留下的與 thesis 配對走同一套可行性檢查與降級。v1 影子配置略過 exposure 配對。減碼日整個略過；`exposure_rotation` 的換股步驟照舊停用。
- **第 4.3 節 1、2 項同步**：Skill `references/swap-nomination.md` 加條件式一節（簡報有 `exposure_swap` 才適用，保留原句）；換股提名計畫第 2.4 節加例外說明。
- **回答原先的開放問題**：`team_inputs` 不升版（沿用 2.9，`exposure_facts` 本來就是選配鍵）；順序驗證是逐檔（`lower_conviction_review` 的每個 symbol）；exposure 配對仍遵守名額語意——換入者不換股即有名額時為 `AGENT_SWAP_UNNEEDED`，該對不佔對數上限。
- **Finalizer**：`apply_trade_decision` 把風險 Agent 的 `exposure_view` 綁進 Policy，Finalizer 比對綁定值與現金姿態；`apply_swap_nomination` 綁含 `purpose` 的配對；缺 `exposure_facts` 拒絕。
- **測試**：`tests/decision/test_exposure_swap.py`、`test_exposure_swap_allocation.py`、`tests/automation/test_exposure_swap_pipeline.py`。
- **尚未做**：開閘門（需人決定，先看第 1、2 階段即時紀錄）；影子報告（`calibration/shadow_swap_report.py`）尚未統計 `purpose` 與 `kept`。
