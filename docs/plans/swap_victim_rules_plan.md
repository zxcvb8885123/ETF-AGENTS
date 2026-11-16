# 換股換出者規則（`agent_swap.victim_rules`）：計畫

狀態（2026-11-16）：**已實作並於 2026-11-16 開啟**（`policy_id strategy-2026-11-16-victim-rules-1`，回滾樣板 `config/policy_versions/2026-11-16-before-victim-rules.json`）。 人已決定：`thesis_max_hold_conviction=low`、`holding_days_basis=trading_days`、`min_holding_days` 維持 5、風險 Agent 補賣單行情要做（不需閘門，已隨實作生效）；以下第 6 節「未動手」為計畫時的描述，實作範圍以程式為準：`exposure`／`quality_dip` 資格不變（只 `purpose=thesis` 改交易日）、測試在 `tests/decision/test_swap_victim_rules.py`。 依據是 2026-11-16 一次唯讀調查（行號為分支 `163-` 工作區）。

## 1. 問題

2026-11-16 的決策（`decision-20261115T234306Z`）裡，交易 Agent 第一輪對 2880.TW 給「續抱／中信心」、對 2327.TW 給「續抱／低信心」，第二輪「換股提名」卻把兩檔都換出（2880→3044、2327→2337），最終委託賣出。報告上寫「原意圖續抱、最終結果賣出」，使用者覺得自相矛盾。

事實：

- 兩筆換出都通過現行全部檢查，不是程式漏驗，是現行規則允許。
- 當天大盤 -6%，金融股較抗跌、換入的成長股跌更多；**決策當下無法知道，不能拿事後結果當設計理由。**

## 2. 現行約束（事實）

`src/etf_agent/decision/swap_nomination.py`：

- 換出者資格（`nomination_facts`，350–387 行）：持有、第一輪 `intent == "hold"`、可交易、持有天數 ≥ `agent_swap.min_holding_days`、至少一個已採納的空頭 claim／review 警示／position 事實。
- **purpose 為 `thesis` 時不看 `hold_conviction`**（信心只寫進簡報，365 行；`_check_sell` 1022–1035 行只看 `nominable`）。只有 `quality_dip`（須 low，原因碼 `hold_conviction_not_low`）與 `exposure`（高信心須排在中低信心之後並逐檔交代）用到信心。
- 換出依據只能引用該股第一輪已採納的 bear／review／position（1037–1092 行）；**沒有任何「新股優於舊股」的比較檢查**。
- 「已採納空頭 claim」沒有篩選力：抽看的 7 檔 `rejected_claim_ids` 全是空的，Agent 習慣「採納為風險但不改變行動」。

持有天數：`decision/exposure.py:562-576` 的 `holding_days_by_symbol` 是**日曆天**。2327 於 11-10 進場，到 11-16 算 6 天，過 `min_holding_days 5`；同一份 run 的 `position_review.holding_trading_days` 只有 3（`decision/position_review.py:257`）。

Skill 引導：`tradeagent/trading/trader/references/swap-nomination.md`「反彈期的換位置問題」明講防守股可列為換出者，`data-rules.md:50` 把這題留到第二輪。所以 2880→3044 是提示的預期行為，不是 Agent 違規。

推論（依理由文字判讀，**程式無法證明**）：2880、2885、2891 三組的理由都是「反彈期只是低波動部位」，動機是曝險參與度，`purpose` 卻填 `thesis`。thesis 配對不占曝險名額、不受 `offense_beta_stop` 限制；同一天兩組 `purpose=exposure` 配對（3711、6789）正是被 `offense_beta_stop` 取消。等於動機屬曝險、標籤走 thesis，繞過曝險風控。

## 3. 風險 Agent 看不到賣出標的行情（事實＋推論）

風險審查的 `unresolved_questions` 寫「可引用證據清單沒有 2327、2880、2885、2891、8046 的行情 evidence」。`automation/risk_stages.py:616-621` 的可引用清單只取 `proposal.allocation_proposal.positions`（配置後持股），全數賣出者不在內；`decision/risk.py:663` 的 Validator 本來就接受 bundle 內任何 evidence。推論為偶然的設計缺口（清單原意是防止編造 ID）；未逐一打開 `proposal.json` 核對。

## 4. 修法比較

五個修法都不違反 AGENTS.md 三原則（Agent 仍只輸出列舉與文字、閘門只由人設定、不改寫交易 Agent 意圖）：都是 5c 驗證退回並給中性回饋。因為 Finalizer 用同一個 Validator，每一項都必須掛版本閘門，沒有欄位的舊 run 逐位元不變。

| 修法 | 評估 | 成本 |
| --- | --- | --- |
| (a) thesis 換出者的第一輪 hold 信心不得高於 low | 單純，與 `quality_dip` 既有規則一致；會擋掉 11-16 的 2880、2885、2891。副作用：Agent 可能在第一輪壓低 hold 信心以保留彈性。 | 低 |
| (b) 須引用已採納空頭 claim／警示，且信心 low | 前半已存在且無篩選力，後半等於 (a)；可加「thesis 依據不得只有 position 事實」。 | 低，併入 (a) |
| (c) 須有「新股明顯優於舊股」的可比較依據 | 程式無法判定文字上的「明顯優於」；做成硬規則要人填數字門檻，本質是追動能。**不建議。** | 高 |
| (d) 持有天數下限改算交易日 | 下限本來就有，問題在日曆天口徑；改讀 `position_review.holding_trading_days`，人填數字不變；會擋掉 2327（3 < 5）。 | 低 |
| (e) 換股成本納入門檻 | 要有「預期好處」數字才能比，Agent 不能輸出；只能在簡報附來回成本當背景（`citable=false`）。 | 硬門檻高、背景低 |

## 5. 建議：(a)＋(d)＋Skill 文字＋風險 Agent 補行情

1. **子閘門** `position_sizing.agent_swap.victim_rules`：`{"version":"v1","thesis_max_hold_conviction":"low","holding_days_basis":"trading_days"}`。
   - 不升 `agent_swap` v3（專案慣例是子閘門，如 `exposure`、`quality_dip`、`entry_reserve`、`proceeds_to_add`；升 v3 得改動所有 `version == v2` 判斷）。
   - 人填、策略反思禁區；`team_inputs` 不升版、不加新鍵；沒有欄位時逐位元不變。
2. **Skill 文字**：「反彈期換位置」改為「理由是參與度就用 `purpose=exposure`」。驗證回饋並列「改用 exposure」與「刪除該對」兩種修法。
3. **風險 Agent**：可引用清單聯集本次委託（至少賣單）的行情 evidence。只改 pipeline 工作檔，不動契約與 Validator，舊 run 重建不受影響。**不**把提名理由或交易 Agent 意圖交給風險 Agent（既有邊界）。

理由：不是禁止換掉防守股，而是讓動機與標籤一致。以參與度為由的換股回到 `exposure` 通道，受 `exposure_view` 對數上限與 beta 停止線約束，這本來就是設計好的風控；thesis 留給「這檔自己的理由已弱到只剩低信心」的持股，兩輪就不再矛盾。此理由在決策當下成立，不依賴 11-16 的大跌。**這不保證績效更好**，任何回放只算 `replay_reference_only`。

## 6. 實作範圍（未動手）

程式：

- `src/etf_agent/decision/sizing.py`：`AGENT_SWAP_FIELDS`（214–217 行）加子閘門；`_validate_agent_swap`（792 行起）加值域與前提（`agent_swap` v2、`position_review`）；`GATE_PREREQUISITES`（約 364–410 行）加一筆。
- `src/etf_agent/decision/swap_nomination.py`：`nomination_facts` 在閘門開啟時對 thesis 加原因碼（如 `hold_conviction_above_cap`）；持有天數改讀 `position_review` 交易日，缺值 fail-closed 視為不足；`_check_sell` 依 purpose 分流（`exposure` 資格不變）；簡報（581 行起）只在閘門開啟時多附欄位。
- `src/etf_agent/automation/risk_stages.py:616-620`：清單加入委託標的。
- `allocation.py` 應不需改（只讀 `nominated_pairs`），實作時再搜尋確認。
- `strategy_reflection/contracts.py`：確認 `agent_swap` 前綴已涵蓋新子閘門（不得被反思改）。

Skill 與文件：`tradeagent/trading/trader/references/swap-nomination.md`、`data-rules.md:50`、`AGENTS.md`、`docs/decision-contracts.md`、`docs/status.md`、`docs/glossary.md`；`config/policy_versions/` 存切換前樣板。

測試（`unittest.TestCase` 風格）：

- `tests/decision/test_swap_nomination.py`：medium／high 的 hold 以 thesis 提名被拒且回饋並列兩種修法；同一檔改 `exposure` 可過；low＋空頭 claim 可過；日曆 6 天、交易日 3 天被拒；`position_review` 缺交易日數 fail-closed；沒有閘門時事實與簡報逐鍵不變。
- `tests/decision/test_agent_swap_policy.py`：缺前提、值域錯誤、樣板預填被拒。
- `tests/decision/test_archived_runs_replay.py`：舊封存重建雜湊不變。
- 風險審查階段測試：全數賣出股票的行情在可引用清單內。
- 選配：`calibration` 唯讀統計「過去提名有幾對會被新規則擋下」，標 `replay_reference_only`。

## 7. 需要人決定

1. 要不要做（或先觀察幾天；`offense` 11-11、11-12 才調整，樣本很少）。
2. `thesis_max_hold_conviction` 填 `low` 還是 `medium`（`medium` 只擋 high，2880 仍可被換出）。
3. 持有天數口徑改交易日後，`min_holding_days` 是否維持 5。
4. 風險 Agent 補賣單行情是否與閘門分開先做（不需閘門、不影響舊 run）。
