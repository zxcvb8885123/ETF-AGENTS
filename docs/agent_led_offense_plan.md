# 連跌後逢低布局與轉向後進攻：由 Agent 判斷、人只填上限（計畫，併入換股份量計畫）

**白話摘要**（2026-11-11 第三版：兩條主軸，「連跌後逢低布局」在前）

1. 連跌那幾天交易 Agent 每天都列了 12～20 檔中高信心買進，但幾乎都沒成交：防守姿態的現金目標 18～20% 把買單砍成 0，11-04 程式還為了補現金賣掉 6.5% NAV 的持股。
2. 當時沒有任何逢低布局的機制：被大盤拖累的事實、換股提名、優質股換股都是 11-10 之後才開的；而現行優質股換股規定「必須有轉向跡象」，連跌中結構上不會成交。
3. 主軸一（抄底）：程式算「連跌深度事實」，交易 Agent 在連跌期間可分批提名買進優質股，風險 Agent 逐組裁決；人填每天組數、單檔權重、總抄底曝險、批數、間隔與暫停線。
4. 誠實的反事實：若在連跌第 3、5 天各買 5% NAV，到 11-06 最多虧約 1.5% NAV，到 11-10 仍虧約 0.6% NAV；要到第 6、7 天進的批才賺。抄底是分批承接，不是買了就漲。
5. 「大跌後常大漲」只能用程式從行情算的對照樣本：視窗內連跌滿 3 天共 5 次，前 4 次之後 5 日都上漲，第 5 次就是這次（之後 5 日 −10.6%）。樣本太小，只能當背景。
6. 主軸二（轉向後進攻）：風險 Agent 的回補意見分四級，各對映一列人填上限；進攻版 beta 停止線 0.40／0.55／0.70、每天最多 2／4／5 組、回補時現金 5%。
7. 進攻版在 11-04～11-06 的跌幅下回撤約 11.7%，會越過現行 10% 強制防守線；要同時決定是否把該線調到 15%。
8. 今天只改設定就能開的：換股組數 3→5、曝險配對 3／5、「轉向未確認」現金目標 0.10；優質股換股可切到已實作的 v3（組數另計、風險 Agent 裁決）並開超跌。但這些都還需要「轉向跡象」，不是真正的連跌中抄底。
9. 最快順序：今天改設定 → 簡報說真話 → 抄底梯度（約 11-16 可用）→ 換入者保留預算 → 分級上限表與試單。
10. 剩下 9 個交易日裡，抄底主軸只有再出現連跌才用得上；現在（已反彈兩天）用到的是主軸二。

狀態：**計畫，未實作、未開啟**（2026-11-11 撰寫；同日第二版加入進攻版）。本文件只寫設計，沒有改程式、設定、Policy、Skill 或其他文件的內容。例外說明：第 6 節的「階段 0」是只改設定的過渡手段，已由人於 2026-11-11 另行套用（`policy_id strategy-2026-11-11-offense-stage0-1`，樣板 `config/policy_versions/2026-11-11-before-offense-stage0.json`），不是本文件改的；目前值以 `config/decision_policy.json` 與 [`status.md`](status.md#版本閘門啟用狀態) 為準。文中新名稱（`probe`、`offense`、`offense_facts`、`entry_reserve` 等）都是暫名。把本文件的行為當成現行行為是錯的。

證據等級：所有數字都是以唯讀腳本讀封存檔算的，沒有驗 manifest 與雜湊；一律 `evidence_grade: replay_reference_only`，其中多數只是算術估計，連重放都不是。它們不是對 LLM 的前瞻驗證，只有比賽期間的即時紀錄算證據。

前置閱讀：[`agent_led_sizing_plan.md`](agent_led_sizing_plan.md)、[`agent_led_exposure_plan.md`](agent_led_exposure_plan.md)、[`rebound_participation_plan.md`](rebound_participation_plan.md)、[`rebound_participation_v2_plan.md`](rebound_participation_v2_plan.md)、[`turn_facts_plan.md`](turn_facts_plan.md)、[`quality_dip_plan.md`](quality_dip_plan.md)、[`quality_dip_agent_sizing_plan.md`](quality_dip_agent_sizing_plan.md)、[`decision-contracts.md`](decision-contracts.md)。閘門啟用狀態以 [`status.md`](status.md#版本閘門啟用狀態) 與 `config/decision_policy.json` 為準。

## 1. 目標與非目標

目標（使用者原意）：

- 比賽 2026-11-24 結束，11-12 起剩 9 個交易日，看短期報酬，要更多操作。
- 大跌時賣得保守是好的；但市場轉向時要能進攻。Agent 應該能在轉向前先買進攻股（預測型），保守版可以等確定訊號再進。
- 被錯殺的績優股要能買，不是只等。
- 使用者補充的核心問題（主軸一）：連續大跌好幾天後，很多優質股已經跌到打折價；Agent 在連跌期間沒有任何提前買進優質股的計畫，之後兩天大漲完全吃不到。要有「連跌後逢低布局」的路徑，與「轉向後進攻」並列。

非目標：

- 不放寬任何硬性規則：現金低於 25%、持股 20～30 檔、單股 10%（2330 為 25%）、產業 60%、單日換手 30%、Active Share 0.15、CompetitionGuard。
- Agent 不輸出數字；程式不挑股、不依 beta 排序選股、不為了達到 beta 目標自動換股。
- 不改舊 run 的重建結果。

## 2. 診斷：11-11 為什麼進攻不起來

出處：`artifacts/decisions/2026-11-11/decision-20261111T001308Z/` 的 `team_inputs.json`、`policy.json`、`intent.json`、`proposal.json`、`decision.json`、`decision_input.json`。

### 2.1 市場與組合（事實）

| 項目 | 數值 | 出處 |
| --- | --- | --- |
| 交易池等權日報酬 | 11-03 −5.28%、11-05 −5.26%、11-06 −5.04%、11-09 +5.51%、11-10 +3.01% | `team_inputs.turn_state.recent_path`、`price_series` 重算 |
| 站上 MA5 比例 | 11-06 2.7% → 11-09 32.0% → 11-10 72.0% | `turn_state.recent_path` |
| 20 日新低比例 | 11-06 84.7% → 11-10 8.7% | 同上 |
| regime | bear，連 8 日 | `turn_state.regime`、風險 Agent 理由 |
| 組合 beta（含現金） | 0.132；只算持股 0.155 | `exposure_facts.portfolio` |
| beta 低於 0.3 的持股 | 20 檔，權重合計 65.0% | 同上 |
| 現金 | 14.96%（盤前）→ 15.30%（盤後） | `exposure_facts`、`decision.json` |
| 11-10 持股貢獻 | +0.56% NAV（交易池 +3.01%） | `exposure_facts.up_day` |
| 11-09～11-10 兩日 | 交易池 +8.68%；持股以本次權重估算 +1.42% NAV | `price_series` 重算 |
| 回撤 | 4.70%，回撤下限 `stance_floor: neutral`（所以姿態不能選 aggressive） | `policy.drawdown_control` |

依 beta 分三組的報酬（`price_series` 與 `exposure_facts` 的 beta 重算，各 50 檔）：

| 組別 | beta 範圍 | 11-03 收盤→11-06 | 11-06→11-10 | 10-08→11-10 |
| --- | --- | --- | --- | --- |
| 低 | −0.28～0.51 | −3.7% | +0.8% | −1.3% |
| 中 | 0.52～1.47 | −11.2% | +9.2% | −18.7% |
| 高 | 1.48～2.20 | −16.2% | +16.0% | −27.2% |

組合幾乎全在低組：下跌三日少跌，反彈兩日也沒跟上。

### 2.2 每一道鎖是誰的決定

| # | 鎖 | 事實 | 誰決定 |
| --- | --- | --- | --- |
| 1 | 新買進預算為 0 | 現金目標 15%（`defensive`／`lower`）加既有持股 85.0% 依現有權重計入預算，剩餘為零。旗標原文：「現有權重 1.98% 已高於等級份額 0.00%」（1802、2615 的 `RISK_HELD_WEIGHT`） | 姿態與子等級是風險 Agent；格點數字是人；預算算法是程式 |
| 2 | 沒有名額 | 持股 30／30，廣度 `diversified` 對 30。25 檔 `buy` 中 23 檔 `MAX_POSITIONS` | 廣度是風險 Agent；上限 30 是硬規則 |
| 3 | 一天最多 2 組換股 | `agent_swap.max_swaps_per_run` 2，thesis、exposure、quality_dip（v2）共用。當天 2 組 thesis 用完 | 人 |
| 4 | 換入者拿不到錢 | 2408、6239 都是 `LOT_SIZE`，賣款流向 2330 加碼（`AGENT_SWAP_PROCEEDS_TO_ADD`）。結果：賣掉 2890（beta 0.15）、3034（0.34），買 2330（0.54）；持股 30→28 檔、現金 15.3% | 程式（換入者份量取自信心預算，見鎖 1） |
| 5 | 曝險配對一對都不用 | `exposure_view: keep_defensive` 對映 0 對；交易 Agent 當天也沒有提名 `purpose: exposure`，`lower_conviction_review` 為空 | 風險 Agent 與交易 Agent；對映數字是人 |
| 6 | 轉向判斷不影響配置 | `rebound_participation` 綁了 `effective_state: candidate`，但 `by_state` 的 beta 停止線、換股檔數、持有天數只作用在 `exposure_rotation` 的換股步驟；該步驟在 `agent_swap` v2 下提前返回（`allocation.py` 約 1375～1378 行，`apply_rebound` 在其後的 1385 行），當天又因減碼日更早返回（`EXPOSURE_ROTATION_SKIPPED:derisk_day`）。選填的 `defensive_cash_buffer`、`max_turnover_rate` 沒有填 | 程式路徑；選填欄位是人 |
| 7 | 優質股換股沒有動作 | `quality_dip` v2 已開（1 組、單檔 3%、門檻 `turn_unconfirmed` 當天通過），但交易 Agent 提名 0 組。就算提名：排在 2 組 thesis 之後會被 `swap_cap` 取消；買進股數仍取自信心預算（`allocation.py` 約 803～810 行，只縮不放），預算為 0 時不足一張、該對取消 | 交易 Agent（沒提名，原因無法由封存檔得知）；上限是人；份量是程式 |
| 8 | 減碼日 | 現金只比 15% 少 0.044% NAV 就觸發 `RISK_DRAWDOWN` | 程式；格點是人 |
| 9 | 高信心壓在低 beta 持股 | `hold`／high 六檔：2412（beta −0.23、7.00%）、2886（−0.21、4.86%）、2880（−0.06、4.03%）、3045（−0.21、3.95%）、2891（0.01、2.88%）、6446（0.02、2.01%），合計 24.7% NAV | 交易 Agent |

推論：

- 交易 Agent **有**買進意願，而且買的正是高 beta 與被大盤拖累的股票。25 檔 `buy` 中 high 1、medium 17、low 7；`dip_assessment` 為 `market_driven` 20 檔、`oversold_cause_unclear` 4 檔；中高信心的 18 檔在 11-06→11-10 平均 +12.2%（同一批在 11-03→11-06 平均 −12.2%）。
- 所以「看不到轉向買進的意願」在交易 Agent 這一層不成立；意願被鎖 1～4 擋在配置之前。
- 風險 Agent 的保守是判斷（引用了通膨、升息、殖利率、外資連賣的新聞），不是程式強迫。但有兩件事讓它偏保守：它沒有「小量先試」的選項，只能在維持防守與回補之間選；簡報文字寫「不要為了追上大盤而升級」「證據不足就填 `keep_defensive`」。
- 即使風險 Agent 當天填 `turn_confirmed`，配置也不會改變（鎖 6）。上一輪分析說「confirmed 可換 4 檔、目標 beta 0.55」是 `agent_swap` v2 開啟前的行為。

### 2.3 簡報文字與現行行為不一致（事實，實作前請再核對）

- `automation/risk_stages.py` 的 `EXPOSURE_VIEW_PROMPT` 寫 `exposure_view`「只是紀錄……不改……換股」；但 `agent_swap.exposure` v2 開啟後，它決定曝險配對用不用。
- `TURN_STATE_PROMPT_V2` 寫 `turn_assessment`「是唯一決定防守姿態下曝險換股……是否放寬的依據」；但那條路徑已停用（鎖 6）。
- 風險 Agent 因此不知道自己哪個列舉真的有作用。

### 2.4 要把 beta 拉上來需要多少動作（算術，非重放）

- 每 1% NAV 的現金買進 beta 1.5 的股票，組合 beta 約 +0.015。
- 每 3% NAV 由 beta 約 0 的持股換成 beta 1.5 的新股，約 +0.045，成交額約 6% NAV。
- 由 0.13 到 0.30：例如現金 15%→10%（+0.075）加 2 組 3% 的換股（+0.09）。
- 由 0.13 到 0.55：現金 15%→8%（+0.105）加約 7 組 3% 的換股（+0.315）；在一天 3 組的上限下至少 3 個交易日，單日換手約 25%。
- 由 0.13 到 0.70（進攻版）：現金 15%→5%（+0.15）加約 28% NAV 的換股（+0.42）。成交額合計約 66% NAV，在 30% 換手上限下要 3 個交易日：第 1 日現金 10% 加換股 10%，約 0.43；第 2 日換股 15%，約 0.65；第 3 日補到 0.70。
- 換股的來源夠不夠：11-11 beta 低於 0.3、且交易 Agent 信心不是 high 的持股共 14 檔、權重合計 40.3% NAV（含當天已換出的 2890 與持有未滿 5 天的 2610、5880、2892）。不動 `hold`／high 的六檔也足以支應 28%。
- 結論：現金與換股兩條都要開，只開一條不夠；剩 9 個交易日，動作慢一天就少一天。

### 2.5 連跌期間（10-29～11-06）為什麼沒有逢低布局

交易池自 10-29 起連跌 7 個交易日：10-29 −2.14%、10-30 −3.21%、11-02 −0.15%、11-03 −5.28%、11-04 −0.55%、11-05 −5.26%、11-06 −5.04%，累計 −19.8%；離 60 日高點回檔在 11-06 為 −23.4%。

逐份 run 的事實（出處：`artifacts/decisions/` 各 run 的 `policy.json`、`team_inputs.json`、`intent.json`、`proposal.json`、`decision.json`、`decision_input.json`）：

| run | 行情截至（連跌第幾天） | 姿態／現金目標 | 盤前現金 | 盤前檔數 | 交易 Agent 的中高信心 `buy` | 主要旗標 | 成交 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `decision-20261103T000604Z` | 11-02（3） | neutral／10% | 11.6% | 29 | 12 | `MAX_POSITIONS` 11、`CASH_BUFFER` 2 | 買 2330 加碼、6239 |
| `decision-20261103T231233Z` | 11-03（4） | defensive／20% | 10.7% | 30 | 13 | `RISK_DRAWDOWN` 8、`MAX_POSITIONS` 9、`CASH_BUFFER` 5 | **賣 8 筆共 6.5% NAV**：1815、3189、3443、3533、3653、6239 出場，2379、8046 減碼 |
| `decision-20261104T230000Z` | 11-04（5） | defensive／20% | 18.9% | 25 | 16 | `CASH_BUFFER` 7、`MAX_POSITIONS` 12、`RISK_REENTRY_COOLDOWN` 3、`RISK_CASH_FIT` 1 | 2890 減碼、1802 加碼 |
| `decision-20261105T230005Z` | 11-05（6） | defensive／middle 18% | 19.2% | 25 | 7 | `MAX_POSITIONS` 4、`CASH_BUFFER` 4、`RISK_CASH_FIT` 1 | 買 2892 |
| `decision-20261108T230407Z` | 11-06（7，低點） | defensive／middle 18% | 18.6% | 26 | 10 | `MAX_POSITIONS` 8、`LOT_SIZE` 3、`RISK_HELD_WEIGHT` 4 | 3034 減碼、買 6446 |
| `decision-20261109T230435Z` | 11-09（+5.51%） | defensive／lower 15% | 18.0% | 27 | 7 | `MAX_POSITIONS` 7、`EXPOSURE_ROTATION_SKIPPED` | 2890 減碼，買 2327、2610、5880 |

哪一道鎖真正擋住（依 run 內證據排序）：

| # | 鎖 | 證據 | 誰決定 |
| --- | --- | --- | --- |
| 1 | 當時沒有任何逢低布局的機制 | 上表六份 run 的 Policy 都沒有 `quality_dip_facts`、`quality_dip_assessment`、`agent_swap`；`team_inputs` 沒有 `dip_facts` 與 `swap_nomination`（版本 2.5～2.8）。這些閘門是 11-10 之後才開，第一份帶著它們的是 11-11 的 run | 人（閘門當時還不存在或未開） |
| 2 | 防守姿態的現金目標把買單砍成 0 | 11-03 行情的 run：姿態 `defensive`、目標 20%、現金 10.7%。風險 Agent 的理由寫明「用原有現金的買單會被緩衝擋掉……反彈時可能參與較少；在目前風險下我接受這個代價」。`CASH_BUFFER` 旗標原文：「買進股數由 169000 降為 0」 | 風險 Agent 選姿態；20% 是人填的數字 |
| 3 | 程式在連跌第 4 天為補現金賣股 | 同一份 run 的 `RISK_DRAWDOWN`：「現金低於 20% 的姿態緩衝，由程式依 ATR14 由高到低減碼」，賣出 6.5% NAV，現金由 10.7% 升到 18.9%。被賣的是高波動股；其中 3443、3533、6239、1815 在 11-11 又出現在交易 Agent 的買進名單 | 程式（當時是 ATR 順序）；觸發來自姿態 |
| 4 | 現金 18～19% 卻買不進去 | 11-04、11-05、11-06 行情的三份 run：現金 18.6～19.2%，目標 20%／18%，可用現金約 0～1% NAV；每天只成交一筆買單 | 風險 Agent 與格點 |
| 5 | 再買冷卻 | 11-04 行情的 run 有 3 檔 `RISK_REENTRY_COOLDOWN`（含前一天被程式賣出的 1815）。當時是 v1，程式賣出的也算冷卻 | 程式（現已改為 v3，不再擋買單） |
| 6 | 名額 | 六份 run 每天 4～12 檔 `MAX_POSITIONS`，旗標文字是「持股已滿且本次沒有出場釋出名額」。但 11-04 行情那份的盤前持股只有 25 檔、廣度對 30。**為什麼 25 檔會被判滿，本次沒有查清**，實作前要先查 | 待查 |

即使把 11-11 的閘門原樣搬回連跌期間，也不會抄底，原因有三（程式碼事實）：

- `agent_swap.quality_dip` 的 `min_turn_assessment` 只允許 `turn_unconfirmed`／`turn_confirmed`，驗證訊息寫明「必須有轉向跡象」；`turn_allows` 對 `no_turn_evidence` 恆不通過（`decision/quality_dip_swap.py` 約 206、335 行）。連跌中沒有轉向跡象，所有 quality_dip 配對都會被取消。這個閘門設計上是「轉向後買被錯殺的」，不是「連跌中分批買」。
- 超跌（`oversold`）要人另開區塊，目前沒開。11-10 的事實是 150 檔中 65 檔標 `oversold`、54 檔標 `market_dragged`。
- quality_dip 是一換一，買進不超過換出賣款：它不會動用那 18～19% 的現金，也不增加總持股。

交易 Agent 這一層（事實）：連跌期間它每天都給了 7～16 檔中高信心買進，沒有「不想買」。但這些名單當時進場是接刀（等權，收盤價進、不計費用）：

| 名單日 | 檔數 | 到 11-06 | 到 11-10 | 同期交易池到 11-06／11-10 |
| --- | --- | --- | --- | --- |
| 11-02（第 3 天） | 12 | −18.7% | −9.0% | −15.0%／−8.1% |
| 11-03（第 4 天） | 13 | −8.0% | −3.2% | −10.5%／−2.9% |
| 11-04（第 5 天） | 16 | −10.4% | −2.3% | −10.0%／−2.4% |
| 11-05（第 6 天） | 7 | −2.6% | +1.8% | −5.0%／+3.1% |
| 11-06（第 7 天） | 10 | — | +8.0% | —／+8.8% |

結論：

- 使用者的觀察成立：連跌期間系統的動作是往防守走（拉高現金、賣高波動股、買進的是 2892、5880、2610 這類低 beta 股），沒有逢低布局的路徑。
- 擋住的不是交易 Agent 的意願，而是「沒有機制」加上「防守姿態的現金目標」。
- 風險 Agent 的防守在當時也是對的一半：11-03 賣掉的那批到 11-06 又跌了 6～18%。逢低布局與防守是同一段行情裡互相衝突的兩個判斷，只能用分批與上限折衷。

## 3. 方案比較

| 項目 | 甲：只填既有欄位 | 乙：風險 Agent 的回補意見分級對映上限表（推薦） | 丙：只改 Skill，鼓勵選 neutral |
| --- | --- | --- | --- |
| 內容 | 填 `rebound_participation.by_state.*.defensive_cash_buffer`；調高 `agent_swap.max_swaps_per_run` 與 `exposure.max_pairs_by_view` | `exposure_view` 多一級 `probe`（預測型）；每一級對映一列人填的上限：防守姿態下的現金目標、曝險配對數、份量格上限、組合 beta 停止線；另加依據類別、失效條件與回撤停用線 | 改寫簡報與 Skill 措辭，讓風險 Agent 較常選 `neutral` |
| 姿態與現金解耦 | 部分：由 `turn_assessment` 覆寫防守子等級 | 是：姿態照市場證據填，現金目標由回補意見決定 | 否：仍要改姿態才有現金 |
| 預測型 | 沒有。只能把 `turn_unconfirmed` 當成預測用 | 有，且要寫依據類別與失效條件 | 沒有 |
| 換入者拿不到錢（鎖 4） | 未解，只是預算變多 | 解（階段 3，沿用換股份量計畫） | 未解 |
| 下行上限 | 只有現金數字與對數 | 組合 beta 停止線加回撤停用線 | 只有姿態格點 |
| 程式改動 | 無 | 中（新閘門、Validator、Finalizer、簡報） | Skill 與簡報文字（仍須版本閘門，否則舊 run 簡報重建會變） |
| 可稽核 | 弱：`turn_assessment` 只驗值域 | 較強：依據類別對得上程式事實 | 弱 |
| 工期 | 當天 | 階段 2 約 2～3 個工作天，階段 3 約 1.5～2 天 | 0.5 天 |
| 主要缺點 | 單一鑰匙、無預測、舊重放不適用 | 比賽只剩 9 個交易日，做完只剩一半時間可用 | 不可控；neutral 下現金 8～13% 但名額與換股的鎖都還在 |

推薦：**乙為主軸，甲當成今天的過渡（階段 0），丙的措辭修正併入乙（階段 1）**。

理由：

- 鎖有四道，甲只鬆開現金，丙連現金都不一定鬆。
- 使用者要的「預測型／保守型」是兩個不同的風險等級，需要兩列不同的上限，這正是「列舉對映人填數字」的既有做法（`cash_grade` 對 `grid`、`exposure_view` 對 `max_pairs_by_view`）。
- 乙不新增 Agent、不新增列舉欄位，只在既有的 `exposure_view` 加一個值，三個既有鑰匙各管一件事（見 4.1）。

## 4. 推薦設計

### 4.1 三個既有列舉各管什麼

| 列舉（風險 Agent） | 回答的問題 | 本計畫後的作用 |
| --- | --- | --- |
| `level`／`cash_grade` | 市場狀態下該留多少現金 | 不變。仍依證據填；不因想進攻而改姿態 |
| `turn_assessment` | 市場有沒有轉向（對事實的判斷） | 不再單獨放寬任何東西；變成回補等級的一致性條件，並維持 quality_dip 的門檻 |
| `exposure_view` | 組合要不要開始把市場參與度補回來（對行動的判斷） | 唯一的進攻撥盤：`keep_defensive`／`probe`（新）／`start_rebuild`／`rebuild` |

兩型的對應：

- 預測型＝`probe`：轉向還看不出來或未確認，先小量進場。`turn_assessment` 可以是 `no_turn_evidence` 或 `turn_unconfirmed`。
- 保守型＝`start_rebuild`、`rebuild`：等訊號。`start_rebuild` 須 `turn_assessment` 至少 `turn_unconfirmed`；`rebuild` 須 `turn_confirmed`。
- 人可以只開保守型：把 `probe` 那一列的現金目標填成不放寬、配對數填 0。

### 4.2 上限表（人填，程式不給預設）

新閘門 `position_sizing.offense`（暫名）v1，欄位形狀：

```json
"offense": {
  "version": "v1",
  "by_view": {
    "probe":         {"defensive_cash_buffer": "【人填】", "max_exposure_pairs": 【人填】, "max_size": "【人填列舉】", "beta_stop": "【人填】"},
    "start_rebuild": {"defensive_cash_buffer": "【人填】", "max_exposure_pairs": 【人填】, "max_size": "【人填列舉】", "beta_stop": "【人填】"},
    "rebuild":       {"defensive_cash_buffer": "【人填】", "max_exposure_pairs": 【人填】, "max_size": "【人填列舉】", "beta_stop": "【人填】"}
  },
  "cash_floor": "【人填】",
  "max_nominations": 【人填】,
  "offense_off_drawdown": "【人填】",
  "probe_max_days": 【人填，階段 4 才有】
}
```

各欄的意思：

| 欄位 | 作用 | 界線 |
| --- | --- | --- |
| `defensive_cash_buffer` | 姿態為 `defensive` 時，現金目標改為 `min(格點值, 本欄)`。只放寬、不收緊；`neutral`／`aggressive` 不動 | 不得低於閘門的 `cash_floor`（人填；穩健版＝中性姿態最低格 8%，進攻版見 4.12）；等級越高不得越保守 |
| `max_exposure_pairs` | 當天最多取用幾組 `purpose: exposure` 配對 | 不得高於 `agent_swap.max_swaps_per_run`；須與 `agent_swap.exposure.max_pairs_by_view` 的同名鍵相等（單一出處，不一致拒絕） |
| `max_size` | 曝險配對的份量格上限（`small`／`medium`／`large`）；交易 Agent 填的 `size` 高於上限時由程式縮到上限 | 階段 3 才生效 |
| `beta_stop` | 組合 beta 停止線：依序試算配對，預估成交後組合 beta 已達此線，其後的曝險配對不取用 | 是「不再加」的上限，不是目標；程式不會為了達到它而挑股或加買 |
| `offense_off_drawdown` | 回撤達此值，當天有效等級壓回 `keep_defensive` | 須低於 `drawdown_control.defensive_at` |

Policy 驗證（fail-closed）：缺任一必填欄、等級之間不單調、現金目標低於中性最低格、配對數超過換股總上限、`beta_stop` 高於 `exposure_rotation.beta_target_by_stance.aggressive`、與 `rebound_participation.by_state.*.defensive_cash_buffer` 同時存在（現金放寬只能有一個出處）、人預填 pipeline 綁定欄位：一律拒絕。

### 4.3 預測要有可稽核的事實

程式新增一組交易池層級事實 `offense_facts`（暫名），只用決策輸入已有、來源已核准的資料，全部在 cutoff 之前可得：

| 類別（列舉） | 程式算的事實 | 資料 |
| --- | --- | --- |
| `washout`（賣壓竭盡） | 20 日新低比例的近 5 日高點與最新值；交易池離 60 日高點的回檔；連續下跌日數 | `price_series` |
| `breadth_thrust`（廣度回升） | 站上 MA5 比例的 1／3／5 日變化；上漲檔數占比 | `price_series` |
| `volume_confirmation`（量能） | 最近上漲日成交量相對 20 日均量的中位數倍數 | `price_series` 的 `volume` |
| `sentiment_extreme`（情緒極端） | 恐懼貪婪分數與其 5 日變化 | 已核准的 `market_fear_greed`，不可用時該類別 `unavailable` |
| `index_reclaim`（指數站回） | 加權指數相對 MA5／MA20 的位置、離高點回檔 | `market_index_facts` |
| 對照（不是類別） | 本次行情視窗內，交易池單日漲幅落在視窗前 10% 的日子共幾次、之後 3／5 個交易日報酬為負的次數與最差值、樣本數與 `small_sample` 標記 | `price_series`；前瞻視窗必須結束在最近交易日之前 |

- 這些是事實，不是訊號；程式不設任何轉向門檻。
- 每一類別有 `data_status`；缺資料不補值。
- `team_inputs` 以選配鍵保存、Finalizer 整份重算（做法同 `dip_facts`、`exposure_facts`）。
- 對照欄讓 Agent 看到「大漲日之後常常怎樣」的基準，不必也不得靠模型記憶。以 11-11 的視窗（2026-05-19～11-10，119 個交易日）試算：單日 ≥ +2.5% 且有 5 日後資料的共 15 次，5 日後為負 5 次，最差 −11.9%（07-22 之後）；最近一次 10-28 之後 5 日 −10.9%。樣本重疊、彼此不獨立，只能當參考。

風險 Agent 填 `probe` 時多兩個必填欄：

| 欄位 | 內容 | 驗證 |
| --- | --- | --- |
| `offense_basis` | 上表類別的列舉清單 | 至少兩個不同類別；每個類別的事實必須 `available`；不在列舉內拒絕 |
| `failure_condition` | 文字：之後看得到、會讓這次試單算失敗的事，含時間條件 | 非空；沿用交易 Agent 重估條件的結構檢查；出現前次 run 識別碼或背景鍵名拒絕 |

所有非 `keep_defensive` 的等級另須：

- `evidence_ids` 至少兩個市場層級證據，其中至少一個不是新聞（新聞是不受信任的文字，不能單獨支持進攻）。
- 姿態為 `defensive` 時必填 `defensive_reconciliation`（為什麼防守的現金判斷與回補不矛盾）。
- 等級與 `turn_assessment` 的一致性（4.1）；不一致退回重答，不由程式夾到邊界。
- 全市場情緒負面時仍可填（底部的情緒本來就是負面），但 `aggressive` 姿態照舊禁止。

### 4.4 程式把等級壓回 `keep_defensive` 的情況（缺資料永不升級）

- `drawdown_control.stance_floor` 為 `defensive`，或帳本回撤歷史不可用。
- 回撤 ≥ `offense_off_drawdown`。
- `offense_facts` 或 `turn_state` 的資料不可用、覆蓋不足、bar 晚於最近交易日。
- 階段 4：前次失效檢查為 `failed`；`probe` 連續天數超過 `probe_max_days`。

有效等級由 `apply_trade_decision` 綁進閘門（`effective_view`），Finalizer 以 `team_inputs` 的現金姿態與事實重算比對；配置留旗標 `OFFENSE:<等級>` 或 `OFFENSE_SUPPRESSED:<原因>`。

### 4.5 買進路徑（誰挑股）

| 路徑 | 買誰 | 錢與名額從哪來 | 誰決定 |
| --- | --- | --- | --- |
| 現金進場 | 交易 Agent 的 buy／add 名單，依（信心、名次、代號） | 現金目標放寬後多出的預算；需要空名額 | 交易 Agent 挑股與排序；風險 Agent 的等級決定放多少現金 |
| 曝險配對（`purpose: exposure`） | 交易 Agent 提名的新股，換掉它指定的低參與度持股 | 換出者賣款；階段 3 起加上本組分到的格點以上現金 | 交易 Agent 提名與份量格；風險 Agent 的等級決定取用幾組與份量格上限 |
| 優質股配對（`purpose: quality_dip`） | 被大盤拖累候選，須有非價格的已採納多頭論點 | 換出者賣款 | 交易 Agent 提名；v3 另由風險 Agent 裁決 |
| 論點配對（`purpose: thesis`） | 不變 | 不變 | 交易 Agent |

- 程式不依 beta 篩選或排序新股。交易 Agent 的提名簡報已有候選新股的 beta 與上行／下行捕捉（背景）。
- 超跌（`oversold`，原因不明）仍須人另開 `quality_dip.oversold`；本計畫不替它開。
- 高信心低 beta 持股要不要讓位，照 `agent_swap.exposure` 已決定的順序：先逐檔交代中、低信心持股。

### 4.6 換入者先保留預算（併入換股份量計畫）

> 實作紀錄（2026-11-11 階段 3）：`agent_swap.entry_reserve` v1 已實作、尚未開啟（`decision/entry_reserve.py`、`allocation.py` 的 `_entry_reserve_plan`）；形狀與行為見 [decision-contracts.md](decision-contracts.md)。實作的取捨：以下 1～5 全做；格點以上現金依清單先後貪婪分配；換手累計只計入配置當下已決定的賣單與各組賣買 gross，之後的 `turnover_fit` 仍會再檢查。

解鎖 4。子閘門 `agent_swap.entry_reserve`（暫名）v1，內容取自 `agent_led_sizing_plan.md` 第 4.2、4.5 節：

1. 每一組配對必填列舉 `size`（`small`／`medium`／`large`），不只 quality_dip。
2. 份量格對成權重上限：`large`＝人填的 `max_entry_weight`，`medium` 2/3、`small` 1/3（沿用 quality_dip v3 的程式常數）。
3. 換入買進金額取最小：份量格權重 × NAV、該組換出者賣款淨額加本組分到的格點以上現金、單股與產業上限餘裕。只縮不放。
4. 換入者的買進先保留，再跑一般的信心預算；換入者不再參與信心預算。
5. 縮到不足一張：整組取消、換出者不賣。不會只賣不買。
6. 選中的組不適用 `proceeds_to_add`：換入者沒買到就整組取消，賣款不流向別檔。
7. 依清單先後累計換手，會超過 30% 的組從該組起取消。

### 4.7 與現有閘門的互動與互斥

| 閘門 | 互動 |
| --- | --- |
| `cash_ratio_range` | 格點與 `max_daily_raise` 不變。放寬後的比例成為次日的 `anchor`；次日若回到 `keep_defensive`，防守姿態的 lower 格（15%）一定可選，現金目標一天內回到格點。與 `high_conviction_cash_floor` 的互斥不變 |
| `rebound_participation` v2 | 仍是 `agent_swap` 的前提，不能刪。`by_state` 的 beta 停止線、換股檔數、持有天數在 `agent_swap` v2 下沒有作用，本計畫不依賴它。它的 `defensive_cash_buffer` 是階段 0 的過渡手段；`offense` 開啟後兩者不得並存 |
| `exposure_rotation` v3 | 換股步驟在 `agent_swap` v2 下停用，維持停用。`beta_target_by_stance` 不再是觸發線；本計畫的 `beta_stop` 只是曝險配對的停止線，兩者不混讀 |
| `agent_swap.exposure` v2 | `probe` 是新值：未開 `offense` 的 Policy 出現 `probe` 一律拒絕。對數上限與 `offense.by_view` 相等檢查 |
| `agent_swap.quality_dip` v2／v3 | 條件不變（回撤下限、`min_turn_assessment`、賣款）。v3 的組數另計、由風險 Agent 裁決；`max_size` 不套用在 quality_dip（它有自己的硬上限）。兩條路徑合計仍受 `turnover_fit` |
| `drawdown_derisk` v5 | 現金目標放寬後，減碼日的觸發線跟著降低（11-11 那種只差 0.044% 的減碼日不會發生）。等級降回 `keep_defensive` 的隔天，現金低於 15% 會觸發減碼，依交易 Agent 的 `release_order` 賣回現金：這就是退出機制，不是副作用 |
| `agent_swap.derisk_day_view` | 不變：減碼日各配對照自己的條件處理；`keep_defensive` 當天曝險配對為 0 |
| `agent_swap.proceeds_to_add` | 階段 3 起對有 `size` 的組停用（4.6 第 6 項） |
| `reentry_cooldown` v3、`min_holding_days` | 不變。新買進的進攻股 5 天內不能當換出者，但交易 Agent 仍可自己出場，減碼日也可依釋出順序賣 |
| `drawdown_control` | 不變。回撤 10% 強制防守時所有等級壓回 `keep_defensive` |

### 4.8 持股 beta 目標由誰填

- 人填的是每一級的 `beta_stop`（停止線）。
- 風險 Agent 選等級，等於選哪一條停止線適用。
- 交易 Agent 決定用哪些股票去補。
- 程式只算組合 beta、在到線時停止取用配對。組合 beta 沒到線不是程式要修正的事。

### 4.9 `team_inputs`、Finalizer 與舊版相容

- `offense_facts`：選配鍵，不升線性版本，要求 2.9 以上；與閘門雙向檢查（閘門在而鍵缺、鍵在而閘門缺都拒絕），Finalizer 整份重算。
- `cash_stance` 的新欄位（`probe` 值、`offense_basis`、`failure_condition`、`defensive_reconciliation`）依閘門放進欄位白名單；沒有閘門的 Policy 出現這些欄位即拒絕。
- 階段 3 的 `size`：放在 `swap_nomination` 內，Policy 的 `nominated_pairs` 綁 `size`，Finalizer 由提名重算比對。
- 線性版本號：2.10 已被 quality_dip v3 的裁決占用。本計畫階段 2、3 推薦不升線性版本（先例：`agent_swap.exposure` v2 讓 Agent 列舉影響配置而未升版）。若人要求「凡是會改配置的 Agent 輸出都升版」，則取 2.11 為 2.10 的超集，代價是 quality_dip v3 未開時版本鏈不再是單一直線。延後的配置裁決（階段 5）才使用 2.11 加必要鍵 `swap_ruling`。
- 沒有 `offense`／`entry_reserve` 的舊 Policy、舊 bundle、舊 run：簡報、Schema、欄位白名單、配置與重建雜湊逐位元不變。
- 回放隨 `agent_swap` 略過，notes 標 `offense: ignored_in_replay`。
- `offense`、`agent_swap` 前綴列入策略反思禁區，只由人設定。

### 4.10 失敗降級（fail-closed）

| 情況 | 處理 |
| --- | --- |
| 現金姿態驗證失敗（含等級一致性、依據類別、失效條件） | 帶回饋的修正循環，最多三次；用盡照現行規則停止 |
| 事實不可用 | 有效等級壓回 `keep_defensive`，其餘決策照常 |
| 提名 `exhausted`／`unavailable` | 不換股；現金放寬仍依有效等級 |
| 配對不可行（名額、現金、換手、單股、產業、不足一張） | 只取消該組、換出者不賣 |
| Finalizer 重算的有效等級、現金目標、`size` 與 Policy 綁定不符 | 拒絕 |
| 硬規則失敗 | 整份拒絕 |

### 4.11 轉向確認時一次大幅換股（進攻版）

目的：風險 Agent 認定轉向確立（`rebuild` 且 `turn_assessment` 為 `turn_confirmed`）時，把低 beta 持股成批換成交易 Agent 想買的高 beta 候選，不再一天一兩組慢慢換。

誰做什麼：

| 誰 | 做什麼 | 形式 |
| --- | --- | --- |
| 交易 Agent（換股提名） | 每天都列出完整的曝險讓位清單：願意讓位的持股、換成哪檔、先後、每組份量格。清單可以比當天成交上限長 | 代號配對、識別碼、列舉 `purpose`／`size`、文字。高信心持股讓位前仍須逐檔交代中低信心持股 |
| 風險 Agent（現金姿態） | 選等級。`rebuild` 就是「今天照清單大幅換」 | 列舉；不看清單、不寫數字 |
| 人 | 清單長度上限 `max_nominations`、每級成交組數、份量格上限、`beta_stop`、單檔換入上限、現金下限、回撤線 | Policy 數字，可回滾 |
| 程式 | 依清單先後逐組試算，碰到任一界線就停；其後的組取消、換出者不賣 | 不挑股、不排序、不補提名 |

三道既有的鎖怎麼處理：

| 鎖 | 處理 | 不變的界線 |
| --- | --- | --- |
| 名額（30 檔上限、23 檔 `MAX_POSITIONS`） | 一換一：每組賣一檔、買一檔，檔數不變。被名額擋下的 23 檔就是換入者的候選池。現金進場只用廣度內的空名額（11-11 盤後 28 檔，空 2 個） | 持股 20～30 檔；廣度仍由風險 Agent 選 |
| 新買進預算為 0 | 兩個來源：換入者以本組賣款先保留預算（4.6），不再跟信心預算搶；現金目標放寬後多出的現金，先分給清單內的組補足份量格，剩下的才進一般信心預算 | 現金不低於放寬後的目標；買進不超過賣款加分到的現金 |
| 換手 30% | 依清單先後累計成交額，會超過的那一組起全部取消（旗標 `OFFENSE_DEFERRED:turnover`）。隔天由兩個 Agent 依新的事實重新判斷與提名；程式不跨日記住未完成的清單 | 30% 不放寬；是否使用既有的確認日換手欄位是第 7 節的決策題 |

停止條件（任一成立即停，全部由程式算）：成交組數達該級上限、預估組合 beta 達 `beta_stop`、累計換手達上限、現金低於目標、單股或產業上限沒有餘裕、換入者不足一張。

節奏（算術，30% 換手）：第 1 日約 10% NAV 現金買進加 10% NAV 換股（例如 2 組 5% 或 3 組 3.3%）；第 2 日起每天約 15% NAV 換股（3 組 5%）。「每天 4 組以上」只有在份量較小（每組約 3.75% 以下）時才裝得進 30%。所以組數上限填 5 是讓小份量的組不被組數卡住，真正的節流閥是換手。

為什麼不是程式自動換：程式依 beta 挑股正是先前被否決的做法（`exposure_rotation` 的排序公式）。這裡程式只執行 Agent 的清單。代價是：交易 Agent 若不願意放掉低 beta 持股，再高的上限也不會成交。

### 4.12 提高持股比例，不只降現金

| 手段 | 內容 | 增加的曝險（算術） | 誰填 | 現況 |
| --- | --- | --- | --- | --- |
| 現金目標再往下 | `rebuild` 級的現金目標由 8% 降到 5%（進攻姿態 middle 格的數字）。需要 `offense.cash_floor` 低於中性最低格；姿態本身仍不得是 `aggressive`（回撤下限為 neutral、情緒負面時照舊禁止） | 多 3% NAV 持股，約 +0.045 beta | 人 | 需階段 2；既有的 `defensive_cash_buffer` 驗證不允許低於 8% |
| 單檔換入上限 | `entry_reserve.max_entry_weight` 0.06：`large` 6%、`medium` 4%、`small` 2% | 每組換股搬動的權重加倍（相對 3%） | 人 | 需階段 3 |
| 高信心份額 | `limit_scaled_conviction.alpha` 0.6→0.8，high 新股份額 6%→8% | 只有交易 Agent 給 high 的股票受益；會擠壓 medium 的預算 | 人 | 既有欄位，只改設定即可；不推薦先動 |
| 持股廣度 | 風險 Agent 選 `balanced`（26）或 `concentrated`（22）時每檔權重較大 | 不增加總持股比例，只提高集中度 | 風險 Agent | 既有列舉 |
| 單股上限餘裕 | 單股 10% 是硬規則。11-11 最大權重 7.19%（2330，上限 25%），其餘都在 7% 以下，餘裕充足 | — | 不動 | — |

結論：持股比例最多由 85% 提到 95%（+10% NAV）；其餘的曝險只能靠把低 beta 權重換成高 beta 權重。組合 beta 要到 0.70，換股占七成的工作量。

### 4.13 主軸一：連跌後逢低布局（抄底）

與主軸二（轉向後進攻）並列。差別：主軸二等轉向跡象或預測轉向；主軸一在還看不到轉向時，依「已經跌很深」分批承接優質股。

**連跌深度事實（程式算，暫名 `decline_depth_facts`）**

| 層級 | 事實 | 資料 | 現況 |
| --- | --- | --- | --- |
| 交易池 | 連續下跌日數；本輪連跌的起始日與累計跌幅；離 60 日高點回檔；20 日新低比例；站上 MA5 比例 | `price_series` | 後三項已在 `turn_facts`／`turn_state`；前兩項要新增 |
| 交易池對照 | 本次行情視窗內「連跌滿 3 天」與「回檔達目前深度」的日子各幾次、之後 3／5 日報酬的分布與最差值、樣本數、`small_sample` 標記 | `price_series`；前瞻視窗不得越過最近交易日 | 新增 |
| 個股 | 20 日報酬、扣掉 beta × 大盤後的殘差與名次、收盤相對 60／120 日高點、是否創 20 日新低、候選標示（被大盤拖累／超跌）、財報與事件的新壞事清單 | `dip_facts`（`quality_dip_facts` v1） | 已有 |
| 個股估值 | PER／PBR 在自身歷史區間的位置（`valuation_facts` v1） | 估值 V1 收集的紀錄 | **不可用**：閘門未開；V1 紀錄的授權範圍只到收集與稽核，開了也全部 `LICENSE_SCOPE_NOT_APPROVED`。須人先核准用途並改 V1 契約。本計畫不依賴它，「打折」只以價格位置與殘差表達，不得寫便宜或低估 |

- 全部是事實，不是訊號；沒有 `evidence_id`、不可引用；缺資料不補值。
- 對照欄就是「大跌後常大漲」唯一允許的來源，不得用模型記憶。以 11-11 的視窗試算（119 個交易日，`replay_reference_only`）：連跌滿 3 天共 5 次（06-08、07-20、07-30、09-14、11-02）；前 4 次之後 5 日為 +3.5%～+17.3%，第 5 次（11-02）之後 5 日 −10.6%。n＝5，必標 `small_sample`。
- `team_inputs` 以選配鍵保存、Finalizer 整份重算。

**誰做什麼**

| 誰 | 做什麼 | 形式 |
| --- | --- | --- |
| 交易 Agent（換股提名） | 提名 `purpose: quality_dip` 的配對：買哪檔優質股、賣哪檔持股、份量格。換入者須是候選、`dip_assessment` 相符、有非價格的已採納多頭論點；沒提名而有可提名目標時填原因 | 代號、識別碼、列舉、文字（沿用 quality_dip v3） |
| 風險 Agent（裁決） | 逐組 `approve`／`downsize`／`reject`；防守看法下放行須寫 `defensive_reconciliation`。只能維持或縮小 | 列舉、文字（沿用 v3） |
| 人 | 梯度的全部數字（下表）；要不要開超跌 | Policy |
| 程式 | 算事實；檢查梯度；買進以賣款為上限；不足一張整組取消 | 不挑股、不決定哪天是底 |

**閘門（暫名：`agent_swap.quality_dip` v4，v3 的超集）**

- `min_turn_assessment` 可填 `no_turn_evidence`，但只在同時有 `ladder` 區塊時允許。
- `ladder`（全部人填，程式不給預設）：

| 欄位 | 意思 | 怎麼算（都不需要跨日狀態） |
| --- | --- | --- |
| `min_pool_drawdown` | 交易池離 60 日高點回檔達此值，才開放「沒有轉向跡象也能抄底」 | `price_series` |
| `max_swaps_per_run`、`max_entry_weight`、`oversold` | 每天最多幾組、單檔買進權重上限、超跌另設的組數與權重 | 沿用 v3 欄位 |
| `max_dip_exposure` | 總抄底曝險：本輪連跌起始日以來新建倉部位的權重合計上限 | 帳本的 `held_since_by_symbol`（pipeline 已綁進 Policy）與連跌起始日 |
| `max_tranches` | 最多分幾批：本輪連跌以來有新建倉的不同交易日數 | 同上 |
| `min_days_between_tranches` | 距上一批至少幾個交易日 | 同上 |
| `pause_after_further_drop` | 上一批進場後交易池再跌達此值，暫停加碼，直到出現交易池上漲日 | `price_series` 與上一批日期 |

- 有轉向跡象（`turn_unconfirmed` 以上）時不受 `min_pool_drawdown` 限制，其餘梯度照舊。
- 換入者的買進以本組賣款先保留（4.6 的做法先套用在 quality_dip 配對），不再因信心預算為 0 而買不到。
- 抄底只做換股，不降低現金目標。是否另外動用現金是第 7.3 節的決策題。

**停止規則（任一成立，當天不取用任何抄底配對；全部 fail-closed）**

- 回撤下限 `stance_floor` 為 `defensive`、回撤歷史不可用、回撤達 `offense_off_drawdown`。
- 連跌深度事實或 `dip_facts` 不可用、覆蓋不足。
- 回檔未達 `min_pool_drawdown` 且沒有轉向跡象。
- 總曝險、批數、間隔、暫停線任一到線。
- 風險 Agent 裁決不是 `completed`。
- 個股層級：有新壞事即不是候選；超跌而未開 `oversold` 不得提名。

**買錯的下行上限（算術）**

- 單批：每天 2 組 × 4% ＝ 8% NAV；以這次最差的一批（第 3 天進、4 個交易日後 −18.7%）估，約 −1.5% NAV。
- 總上限：`max_dip_exposure` 12% NAV × −18.7% ≈ −2.2% NAV。暫停線讓後面的批不會在續跌中接著進。
- 抄底的錢來自換出持股，所以還要扣掉換出者若留著的表現；低 beta 持股在 11-03→11-06 約 −3.7%，淨差比上面的數字小一些。

**C1：梯度只計抄底成交（2026-11-11 已實作、尚未開啟）**

**`ladder.exposure_basis`（選配列舉，C1，已實作、尚未開啟）**：缺席＝總抄底曝險與批數照舊計所有新建倉（逐位元不變）；填 `quality_dip_fills` 只計 purpose=quality_dip 的成交（修正現狀：thesis／exposure／一般買進也算抄底曝險，兩組 medium 就 `exposure_full`、任何新建倉隔天都 `tranche_interval`）。成交清單 `quality_dip.ladder_entries`（`[{symbol, entry_date, run_id}]`）由 pipeline 比照 `bind_reentry_cooldown` 從決策庫確定性取得並綁進 Policy（來源：帳本已套用的前次 run，Policy 的 `nominated_pairs` 為 quality_dip、proposal 有買單與 `AGENT_SWAP_FILL`，只留目前仍持有且帳本持有起點日期不早於該 run 截止日者；樣板預填即拒絕、讀不到綁 null），`build_ladder_state` 只讀清單（公式不變），讀不到或與持股、帳本不一致即以新原因 `entries_unavailable` 關閉；Finalizer 以綁定清單重算並核對每筆在帳本驗證過的 `held_since_by_symbol` 內且日期相容、不重掃決策庫（信任層級同 `reentry_cooldown`）。沒有新的人填數字，開啟由人改 config。

**與主軸二的關係**

- 同一個換股提名、同一次風險 Agent 裁決、同一組硬上限；兩條主軸的組數分開計（quality_dip 另計），合計受 30% 換手。
- 階段 2 的 `probe` 上線後，「連跌中先小量進場」也可以由 `probe` 表達；屆時 `ladder` 仍是抄底配對自己的上限，不合併。

## 5. 與 `agent_led_sizing_plan.md` 的合併

本文件取代該檔的待辦。該檔保留為設計紀錄，不再單獨推進。

| 該檔內容 | 處置 | 說明 |
| --- | --- | --- |
| 第 2 節 11-11 的事實與數字 | 沿用 | 併入本文件第 2 節 |
| 4.2 全部配對必填 `size` | 沿用（階段 3） | |
| 4.5 第 2～7 項：份量格對權重、換入者先保留、不足一張整組取消、停用 `proceeds_to_add`、依先後累計換手 | 沿用（階段 3） | 進攻的必要條件 |
| 4.2 提名上限 `max_nominations` 大於成交上限 | 沿用（進攻版，階段 3） | 4.11 的整份換股清單需要它；超過成交上限的仍從清單尾端取消 |
| 4.3 風險 Agent 配置裁決（挑選、排序、可升級） | 延後到比賽後（階段 5） | 比賽期間改由等級的對數與份量格上限事前把關；quality_dip 仍用 v3 的裁決 |
| 4.6 `funding_symbols` 補款賣出 | 廢止 | 改由現金目標放寬補足：買進金額可含本組分到的格點以上現金 |
| 4.7 閘門名 `agent_swap.sizing`、以 `max_pairs_per_run` 取代 `max_swaps_per_run` | 廢止 | 改為 `agent_swap.entry_reserve`；組數上限維持既有欄位 |
| 4.7 `team_inputs` 2.11＋`swap_ruling` | 延後 | 隨配置裁決 |
| 第 5 節方案 B（風險 Agent 直接寫百分比） | 廢止 | 違反「Agent 不輸出數字」 |
| 第 10 節待決第 2、4、5(a)、6 項 | 已併入設計 | 列舉份量格、沿用 v3 比例、買進縮到賣款、簡報顯示可支應的份量格 |
| 第 10 節待決第 1、7、8、10 項 | 延後 | 隨配置裁決 |
| 第 10 節待決第 3、9 項 | 由本文件第 7 節取代 | 硬上限數值、上線方式 |

## 6. 實作分階段（以 11-24 前最快能用為準）

階段編號沿用第 4、5 節的稱呼，新增階段 D（抄底梯度）。**執行順序：0 → 0b → 1 → D → 3 → 2**；4、5 不在比賽期間做。

| 順序 | 階段 | 內容 | 改動 | 工作量 | 最快生效的決策日 | 之後剩幾個交易日 |
| --- | --- | --- | --- | --- | --- | --- |
| 已做 | 0 | `candidate`／`confirmed` 的 `defensive_cash_buffer` 0.12／0.08、`agent_swap.max_swaps_per_run` 3、`max_pairs_by_view` 2／3 | 只改設定（人已於 11-11 套用） | — | 11-12 | 9 |
| 1 | **0b（今天只改設定）** | 主軸二：`agent_swap.max_swaps_per_run` 3→5；`agent_swap.exposure.max_pairs_by_view` 2／3→3／5；`rebound_participation.by_state.candidate.defensive_cash_buffer` 0.12→0.10。主軸一：`agent_swap.quality_dip` 由 v2 切到已實作的 v3（組數另計、份量格、風險 Agent 裁決；`max_swaps_per_run` 與 `max_entry_weight` 由人填，`team_inputs` 隨之為 2.10），並加 `oversold` 區塊（`max_swaps_per_run`、`max_entry_weight`，不得高於上一層）。另兩項要先回答 7.0 第 3、4 題：`drawdown_control` 的強制防守線、`confirmed.max_turnover_rate` | 只改 `config/decision_policy.json`，改 `policy_id` 與真實 `available_at`；若有測試釘住正式設定的值須同步 | 0.5～1 小時 | 11-12 | 9 |
| 2 | 1 | 簡報與 Skill 說真話（2.3）：`exposure_view` 會決定曝險配對、`turn_assessment` 現在影響現金目標與 quality_dip；並列追高與錯過兩種錯誤；告訴交易 Agent 曝險清單會依序被取用 | 版本閘門下的新提示文字：`automation/risk_stages.py`、`decision/exposure_facts.py`、`decision/exposure_swap.py` 的 note、`tradeagent/risk/portfolio-risk-review/references/cash-stance.md`、`tradeagent/trading/trader/references/swap-nomination.md` | 0.5 天 | 11-12 或 11-13 | 8～9 |
| 3 | **D（抄底梯度）** | `agent_swap.quality_dip` v4：`ladder`、`no_turn_evidence` 下可抄底、連跌深度事實、quality_dip 換入者先保留預算（4.13） | 新檔 `decision/decline_depth.py`（事實）；`decision/quality_dip_swap.py`、`quality_dip_ruling.py`、`swap_nomination.py`、`sizing.py`、`allocation.py`（只讀 `_agent_swap` 的 quality_dip 行段與 `_quality_dip_entry`）、`finalization.py`、`automation/agent_schemas.py`、`team_stages.py`、`risk_stages.py`；Skill `swap-nomination.md`、`quality-dip-ruling.md`；文件 | 1.5～2 個工作天 | 11-16 | 7 |
| 4 | 3 | `agent_swap.entry_reserve` v1：把保留預算與 `size` 推廣到全部配對、`max_nominations`、有 `size` 的組停用 `proceeds_to_add`（4.6、4.11） | `decision/swap_nomination.py`、`sizing.py`、`allocation.py`、`finalization.py`、`automation/agent_schemas.py`、Skill 與文件 | 1～1.5 個工作天（階段 D 已做掉 quality_dip 那一部分） | 11-18 | 5 |
| 5 | 2（精簡版） | `offense` v1：`probe`、分級上限表、`cash_floor`、`offense_off_drawdown`、有效等級；事實重用既有的 | 新檔 `decision/offense.py` 與 4.2 所列檔案 | 2 個工作天 | 11-20 | 3 |
| 不做 | 4、5 | 跨日記憶、風險 Agent 配置裁決 | — | — | 比賽後 | — |

時程的實話：

- 今天能開的只有 0b。對抄底而言，0b 開的仍是「有轉向跡象才買被錯殺的」；真正的連跌中抄底要等階段 D，因為現行驗證不允許 `min_turn_assessment` 填 `no_turn_evidence`。
- 0b 切 v3 是第一次在正式 run 走這條路徑。裁決失敗只會取消 quality_dip 配對，其餘決策照常；但換入者的買進仍取自信心預算，在階段 D 之前可能買不到一張。
- 開 `oversold` 的風險：超跌的原因不明；11-10 有 65 檔帶這個標示。所以超跌的組數與權重要比被大盤拖累更小。
- 階段 D 只在再出現連跌時才派得上用場。市場若續漲，這 1.5～2 天沒有產出；人可以改成先做階段 3（7.3 第 5 題）。
- 階段 2 排到最後，生效時只剩 3 個交易日。若 7.0 第 1 題選「不開試單」，階段 2 可以不做。
- 階段 D、3、2 都改 `allocation.py` 的 `_agent_swap`，不適合平行。

### 6.1 開啟順序與回滾樣板

1. 0b：先存 `config/policy_versions/2026-11-11-before-offense-stage0b.json`。回滾＝換回樣板的 `rebound_participation`、`agent_swap`（含 `quality_dip` 區塊；`team_inputs` 隨之回到 2.9）與有改的話 `drawdown_control` 區塊。
2. 階段 1：提示文字跟著既有閘門的新版本走；回滾＝版本改回。
3. 階段 D：先存 `…-before-dip-ladder.json`。回滾＝把 `agent_swap.quality_dip` 換回樣板的區塊（v3 或 v2），`team_inputs` 隨之回到原版本。
4. 階段 3：先存 `…-before-entry-reserve.json`。回滾＝刪掉 `agent_swap.entry_reserve`。quality_dip v3 若一起開，另存 `…-before-quality-dip-v3.json`。
5. 階段 2：先存 `…-before-offense.json`；同一次改動移除 `rebound_participation` 的 `defensive_cash_buffer`（互斥）。回滾＝刪掉 `position_sizing.offense`，並把 `agent_swap.exposure.max_pairs_by_view` 與 `rebound_participation` 換回樣板。
6. 每次開啟後第一個決策日，檢視旗標（`OFFENSE:*`、`OFFENSE_DEFERRED:*`、`REBOUND_CASH_LOOSENED:*`、`AGENT_SWAP_*`）、成交後組合 beta、現金、換手與回撤。
7. 比賽結束後把 `drawdown_control` 與各上限換回樣板，除非人另行決定。

### 6.1b 階段 1 與階段 D 的實作紀錄（2026-11-11，已實作、尚未開啟）

- 階段 1：`position_sizing.exposure_view` 新版本 v2（前提 `agent_swap` v2 與 `agent_swap.exposure` v2）只改風險 Agent 的簡報與提示文字（`automation/risk_stages.py`、`decision/exposure_facts.py` 的 `RISK_NOTE_AGENT_LED`、`cash-stance.md`）；v1 與無閘門 Policy 逐位元沿用舊文字。`turn_assessment` 的真實作用（讀程式後確認）：防守姿態下 `rebound_participation.by_state` 的 `defensive_cash_buffer`、`confirmed.max_turnover_rate`（有填才有），以及 quality_dip 的 `min_turn_assessment`；它不再啟動曝險換股。`exposure_view` 決定 `purpose=exposure` 配對取用與對數上限。`team_inputs.exposure_facts` 的 `FACTS_NOTE` 中性、未改，所以舊 run 重建雜湊不變。`rebound.py` 的 `TURN_STATE_NOTE_V2` 仍寫「放寬曝險換股」，它在 `team_inputs.turn_state` 內（會進雜湊），本次未改。
- 階段 D：`agent_swap.quality_dip` version v4（`decision/decline_depth.py`）；`ladder` 欄位與關閉原因見 `docs/decision-contracts.md`。實作時的定義取捨：本輪下跌起點＝60 日視窗高點日（不是連跌起始日）、新建倉含所有目的的新買進、今天的買進權重上限逐組累計到剩餘曝險為止（只看上限、不看實際成交）、`offense_off_drawdown` 屬階段 2 未做、「換入者先保留預算」未做（留待階段 3）。

### 6.2 測試清單（`unittest.TestCase` 風格）

舊版相容：

- 沒有 `offense`／`entry_reserve`：簡報、Schema、白名單、配置與封存 run 重建雜湊逐位元不變。
- 沒有 `offense` 的 Policy 出現 `probe`、`offense_basis`、`failure_condition`：拒絕。

閘門：

- 缺必填、等級不單調、現金目標低於 `cash_floor`、`cash_floor` 低於進攻姿態最低格、對數超過總上限、與 `max_pairs_by_view` 不相等、與 `rebound_participation` 的現金放寬並存、`beta_stop` 高於 `exposure_rotation` 的進攻目標、`offense_off_drawdown` 不低於 `defensive_at`、人預填 `effective_view`：拒絕。
- 缺前提（`agent_swap` v2、`agent_swap.exposure` v2、`exposure_view`、`cash_ratio_range`）：拒絕。
- `max_nominations` 小於成交組數上限、`max_entry_weight` 超過單股上限：拒絕。

現金姿態：

- `probe`：`offense_basis` 少於兩類、類別不在列舉、類別事實 `unavailable`、缺 `failure_condition`、只有新聞證據：拒絕。
- `start_rebuild` 配 `no_turn_evidence`、`rebuild` 配非 `turn_confirmed`：拒絕。
- 防守姿態下非 `keep_defensive` 而缺 `defensive_reconciliation`；`keep_defensive` 卻填了：拒絕。
- 現金目標低於中性最低格時，姿態仍不得因此變成 `aggressive`；回撤下限與情緒負面的姿態規則照舊。
- 文字含前次 run 識別碼、事實鍵名、數字欄位：拒絕。

事實：

- cutoff 後的 bar、缺時區、bar 晚於最近交易日：拒絕。
- 覆蓋不足、行情缺漏、恐懼貪婪不可用：該類別 `unavailable`、不補值。
- 竄改任一事實數值：Finalizer 拒絕。

有效等級：

- 回撤下限 `defensive`、回撤歷史不可用、回撤達停用線、事實不可用：壓回 `keep_defensive`，現金不放寬、曝險配對為 0。
- Policy 綁定的 `effective_view` 或現金目標被改：Finalizer 拒絕。

配置：

- 現金目標只放寬不收緊；`neutral`／`aggressive` 姿態不受影響。
- 整份清單依先後成交：組數到上限、預估組合 beta 到線、累計換手到 30%、現金到目標，任一成立後面的組全部取消且換出者不賣；不跨日保留清單。
- 清單長度超過 `max_nominations`：提名退回重答。
- 換入者不被高信心加碼吃掉（以 11-11 情境回歸：2408 有買單，或賣款不足一張時整組取消、2890 不賣）；買進不高於賣款淨額加分到的格點以上現金；有 `size` 的組不出現 `AGENT_SWAP_PROCEEDS_TO_ADD`。
- 份量格高於該級 `max_size` 時由程式縮到上限；權重不高於 `max_entry_weight`、單股與產業上限。
- 換手不超過 30%；檔數 20～30、Active Share 照舊；任一硬規則不過整份拒絕。
- 次日降回 `keep_defensive`：現金目標回到格點，減碼依 `release_order`。
- 交易 Agent 意圖不被改寫。

抄底梯度（階段 D）：

- 沒有 `ladder` 區塊的 Policy：`min_turn_assessment` 填 `no_turn_evidence` 拒絕；v2／v3 與舊提名封存逐位元不變。
- `ladder` 缺任一必填數字、總曝險低於單檔權重、暫停線不是正數、人預填程式綁定欄位：拒絕。
- 連跌深度事實：cutoff 後的 bar、bar 晚於最近交易日拒絕；覆蓋不足或歷史不足 `unavailable`；對照樣本的前瞻視窗不得越過最近交易日，樣本不足標 `small_sample`；竄改任一數值 Finalizer 拒絕。
- 事實不可用、回撤下限 `defensive`、回撤達停用線、回檔深度未達人填門檻：`no_turn_evidence` 下的 quality_dip 配對全部取消、換出者不賣。
- 梯度：本輪新建倉權重合計達總曝險上限、批數達上限、距上一批未滿間隔、上一批之後交易池再跌達暫停線：當天不取用任何抄底配對。以帳本的 `held_since_by_symbol` 重算，竄改綁定值拒絕。
- 風險 Agent 裁決失敗（`exhausted`／`unavailable`）：取消全部抄底配對，不退回不經裁決的行為。
- 換入者有新壞事、候選標示與 `dip_assessment` 不符、超跌而未開 `oversold`、沒有非價格的已採納多頭論點：提名退回。
- 估值位置不可用時不得出現在簡報或依據；文字寫「便宜」「低估」而沒有可引用依據不由程式判斷，但背景鍵名出現即拒絕。
- 買進不超過換出賣款；不足一張整組取消；現金、換手、單股、產業、檔數照舊。

修改後照 `AGENTS.md` 執行完整 pytest、`compileall` 與 `git diff --check`。

## 7. 需要使用者決定的事

### 7.0 主軸二（轉向後進攻）的決策題

| 項 | 問題 | 選項 | 推薦與理由 |
| --- | --- | --- | --- |
| 1 | 試單（預測型）開多大。這題決定能不能吃到反彈的第一根 | (a) `probe` 停止線 0.40 (b) 0.55（極限版） (c) 不開試單 | **(a)**。等確認才動，第一天一定吃不到（7.2 的時間表）。(b) 等於不論有沒有轉向都長期帶著 0.55 的曝險，假訊號時與確認級虧得一樣多 |
| 2 | 今天要不要做 0b | (a) 做：組數 5、曝險配對 3／5、`candidate` 現金目標 0.10 (b) 不做 | **(a)**。只改設定、可回滾；成交仍要兩個 Agent 都同意 |
| 3 | 強制防守線 | (a) `drawdown_control.defensive_at` 0.10→0.15、`release_defensive_below` 0.07→0.11，進攻停用線 0.12；`neutral_at` 不動 (b) 維持 10%，`rebuild` 的 `beta_stop` 壓在 0.50 以下 | **(a)**，前提是使用者真的接受帳戶回撤到 15%。目前回撤 4.7%，離 10% 只有 5.3% NAV；組合 beta 0.70 時等於交易池跌 7.6% 就被強制防守並在低點賣出，進攻版會自相矛盾。這條線是 Policy 數字、不是競賽硬規則，只由人設定；舊 run 仍讀自己的 Policy |
| 4 | 確認日的換手 | (a) 維持 30%，由 0.13 到 0.70 約 3 個交易日 (b) 填既有的 `rebound_participation.by_state.confirmed.max_turnover_rate` 0.45，只在 `turn_confirmed` 當天生效，約 2 個交易日 | **(a)**。使用者原先把 30% 列為不可放寬。文件記載它是 Policy 值而非競賽規則（[decision-contracts](decision-contracts.md) 反彈參與節），(b) 只改設定即可；但它只由 `turn_assessment` 一把鑰匙觸發，確認錯了當天就換掉更多。要最快速度才選 (b) |
| 5 | 回補時的現金目標 | (a) `rebuild` 0.05（`offense.cash_floor` 0.05，階段 2 才有） (b) 維持 8% | **(a)**。多 3% NAV 持股，風險遠小於換股本身；姿態仍不得是 `aggressive` |
| 6 | 上限表用哪一欄 | 7.1 的進攻版或極限版 | **進攻版**。極限版的 0.85 需要換掉約 45% NAV 的持股，在剩下的交易日內做不完，而且會動到高信心持股 |
| 7 | 假反彈怎麼退 | (a) 風險 Agent 降回 `keep_defensive`→現金回格點→依交易 Agent 的 `release_order` 減碼，加回撤停用線 (b) 另由程式強制賣出進攻買進的部位 | **(a)**。(b) 要程式記住哪些是進攻部位並替 Agent 決定賣誰，違反「程式不改寫意圖」，工期也不夠 |
| 8 | 風險 Agent 與交易 Agent 的簡報措辭（階段 1） | (a) 改：說明各列舉的實際作用，並列追高與錯過兩種錯誤 (b) 不改 | **(a)**。現行文字與現行行為不一致（2.3） |
| 9 | 上線方式 | (a) 各階段測試通過後直接正式 (b) 先影子 | **(a)**。影子 2 天後剩不到一半的交易日 |
| 10 | quality_dip v3 | 移到 7.3 第 1、2 題 | 見 7.3 |
| 11 | 風險 Agent 的挑選排序與升級 | (a) 延到比賽後 (b) 比賽期間就做 | **(a)**。2～3 天工期換不到比賽期間的樣本 |

### 7.1 人填數字：進攻版（推薦）與極限版

全部 `replay_reference_only`，且多數是算術估計；不是驗證。穩健版（第一版的數字）列在最右供對照，使用者已不採用。

| 欄位 | 進攻版（推薦） | 極限版 | 穩健版（不採用） | 依據與限制 |
| --- | --- | --- | --- | --- |
| `beta_stop`（probe／start_rebuild／rebuild） | 0.40／0.55／0.70 | 0.55／0.70／0.85 | 0.25／0.35／0.45 | 不得高於 `exposure_rotation` 的進攻目標 0.90。0.70 需換掉約 28% NAV；0.85 約 45% NAV |
| `max_exposure_pairs` | 2／4／5 | 3／5／6 | 1／2／3 | 不得高於 `agent_swap.max_swaps_per_run`；極限版另須把 `rotation.max_swaps_per_run` 由 5 調到 6。實際成交受 30% 換手節流（4.11） |
| `agent_swap.max_swaps_per_run` | 5 | 6 | 3 | thesis 與 exposure 共用 |
| `max_size` | medium／large／large | large／large／large | small／medium／large | 試單也用中份量，否則 2 組只搬得動約 4% NAV |
| `entry_reserve.max_entry_weight` | 0.06 | 0.08 | 0.04 | 0.06 等於現行高信心份額（alpha 0.6 × 單股上限 10%）。換出者權重 ≥ 4% 的只有 9 檔，買進還受賣款限制 |
| `defensive_cash_buffer` | 0.10／0.08／0.05 | 0.08／0.05／0.05 | 0.12／0.10／0.08 | 5% 是進攻姿態 middle 格的數字；再低會撞到 `buy_gap_reserve_rate` 0.03 與整張取整 |
| `offense.cash_floor` | 0.05 | 0.05 | 0.08 | 第 5 題 |
| `max_nominations` | 8 | 10 | — | 清單比成交上限長，風險 Agent 升到 `rebuild` 時才有得用 |
| `drawdown_control.defensive_at`／`release_defensive_below` | 0.15／0.11 | 0.15／0.11 | 0.10／0.07（現行） | 第 3 題 |
| `offense_off_drawdown` | 0.12 | 0.13 | 0.08 | 須低於 `defensive_at` |
| 0b 的過渡值（階段 2 之前） | `candidate` 現金 0.10、`confirmed` 0.08、對數 3／5、組數 5 | 同左 | 0.12／0.08、2／3、3（現行） | `confirmed` 不能低於 8%（既有驗證） |

### 7.2 進攻版的 NAV 損益估算

算法：NAV 變化 ≈ 組合 beta × 交易池報酬。高 beta 股的實際表現與此相符（11-03→11-06 高 beta 組 −16.2% 對交易池 −10.5%；11-06→11-10 +16.0% 對 +8.7%）。回撤由 11-11 的 4.70% 起算。沒有計入個股選擇的好壞。

**已經到位時（整段期間都維持該 beta）**

| 組合 beta | 對應等級 | 假反彈 A：11-03 收盤→11-06（交易池 −10.5%） | 之後回撤 | 假反彈 B：10-28 收盤→11-06 不退出（−19.8%） | 之後回撤 | 真反彈：11-06→11-10（+8.7%） | 吃到的比例 | 建倉費稅滑價 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.13 | 現況 | −1.4% | 6.0% | −2.6% | 7.2% | +1.1%（實際 +1.4%） | 13% | — |
| 0.40 | 進攻版 `probe` | −4.2% | 8.7% | −7.9% | 12.3% | +3.5% | 40% | 約 0.09% |
| 0.55 | 進攻版 `start_rebuild` | −5.8% | 10.2% | −10.9% | 15.1% | +4.8% | 55% | 約 0.17% |
| 0.70 | 進攻版 `rebuild` | −7.4% | 11.7% | −13.9% | 17.9% | +6.1% | 70% | 約 0.24% |
| 0.85 | 極限版 `rebuild` | −8.9% | 13.2% | −16.9% | 20.8% | +7.4% | 85% | 約 0.32% |

讀法：

- 現行 10% 強制防守線：0.55 在情境 A 就越線。調到 15% 後，0.70 在情境 A 不越線（11.7%），但進攻停用線 12% 幾乎同時觸發。
- 情境 B 是「試單之後沒有人喊停」：0.55 以上連 15% 也會越過。退出機制（第 7 題）在這裡是唯一的保護。
- 建倉成本是由 0.13 換到該 beta 的單程費用；判斷錯誤再換回來，合計約兩倍。

**真反彈的時間表（進攻版，30% 換手；11-09 +5.51%、11-10 +3.01%）**

| 情境 | 11-09 當天的 beta | 11-10 當天的 beta | 兩日 NAV | 吃到的比例 |
| --- | --- | --- | --- | --- |
| 現況（實際） | 0.13 | 0.13 | +1.4% | 16% |
| 沒有試單，11-09 收盤後才確認並開始大幅換股 | 0.13 | 約 0.43 | 約 +2.0% | 23% |
| 11-06 前已有試單（0.40），11-09 收盤後升級 | 0.40 | 約 0.65 | 約 +4.2% | 48% |
| 極限版試單（0.55），11-09 收盤後升級 | 0.55 | 約 0.78 | 約 +5.4% | 62% |
| 兩天都在 0.70（做不到，只當上限參考） | 0.70 | 0.70 | +6.1% | 70% |

讀法：

- 保守型（等確認）即使把上限開到最大，這一段也只吃到約四分之一：第一根已經過了，第二天又受 30% 換手限制。
- 要吃到一半以上，試單必須在大漲前就到位。但 11-06 當天的事實是連 3 日大跌、20 日新低比例 84.7%；那時填 `probe` 是否合理，是風險 Agent 的判斷，本文件不推測它會不會填。
- 同一個試單若放在 10-28（當天 +2.82%），之後就是情境 B。試單的大小就是在這兩個結果之間選位置。

其他參考：

- 來回一趟的費稅與滑價約為成交金額的 0.79%（買 0.1425%、賣 0.1425% 加證交稅 0.3%、滑價各 10 bp）。
- `rebound_participation` v1 的第二輪重放（[`rebound_participation_plan.md`](rebound_participation_plan.md)）：現金放寬到 0.09、beta 約 0.4 時，假訊號情境相對不動的最差單日 NAV 差為 −2.8%～−4.2%。那次重放走的是現已停用的曝險換股路徑，數字不能直接套用，但量級與上表 0.40 那一列一致。

### 7.3 主軸一（抄底）的決策題與建議值

| 項 | 問題 | 選項 | 推薦與理由 |
| --- | --- | --- | --- |
| 1 | 今天要不要把 quality_dip 切到 v3 | (a) 切：每天 2 組、單檔 0.04 (b) 維持 v2（1 組、0.03、與 thesis 共用組數） | **(a)**。v2 下 11-11 的 2 組額度被 thesis 用完，quality_dip 排不進去；v3 另計組數、有風險 Agent 裁決，沒提名時要寫原因。風險：第一次在正式 run 走這條路徑，失敗只取消 quality_dip 配對 |
| 2 | 要不要開超跌（`oversold`） | (a) 開：每天 1 組、單檔 0.02 (b) 不開 | **(a)**，但用小上限。11-10 有 65 檔超跌、54 檔被大盤拖累，不開等於排除一半以上的下跌股；代價是原因不明，可能接到真的出事的股票 |
| 3 | 要不要做「連跌中沒有轉向跡象也能抄底」（階段 D） | (a) 做 (b) 不做，維持「有轉向跡象才買」 | **(a)**。這是使用者補充的核心問題；現行設計在連跌中結構上不會成交。代價見 7.4：這次連跌若照做，到 11-10 仍是小虧 |
| 4 | 抄底的錢從哪來 | (a) 只用換出持股的賣款（一換一） (b) 另外允許動用高於人填下限的現金，買進交易 Agent 名單 | **(a) 先做**。連跌中現金有 18～19% 沒用到，(b) 才會真的增加持股；但它等於在風險 Agent 選防守時由梯度覆寫現金目標，而且需要空名額。先看 (a) 的即時紀錄 |
| 5 | 階段 D 與階段 3 誰先 | (a) D 先（抄底） (b) 3 先（轉向後換股拿得到錢） | **(a)**，依使用者指定的優先順序。若使用者認為接下來較可能是續漲而不是再跌，選 (b) 較有用 |
| 6 | 梯度數值 | 見下表 | 進攻值 |

| 欄位 | 建議值（進攻） | 較保守 | 依據 |
| --- | --- | --- | --- |
| `max_swaps_per_run`（每天抄底組數） | 2 | 1 | 一組約 8% NAV 成交額，2 組加其他換股仍在 30% 內 |
| `max_entry_weight` | 0.04 | 0.03 | 等於 `default_target_weight` |
| `oversold.max_swaps_per_run`／`max_entry_weight` | 1／0.02 | 不開 | 原因不明，用一半的份量 |
| `ladder.min_pool_drawdown` | 0.10 | 0.15 | 這次連跌第 3 天回檔 9.6%、第 4 天 14.4%；0.10 會從第 4 天起開放 |
| `ladder.max_dip_exposure` | 0.12 | 0.08 | 最差一批 −18.7% 時約 −2.2% NAV |
| `ladder.max_tranches` | 3 | 2 | 每批 4～8% NAV |
| `ladder.min_days_between_tranches` | 2 | 3 | 避免連續兩天都接 |
| `ladder.pause_after_further_drop` | 0.05 | 0.03 | 這次第 4 天進場後，第 6 天累計再跌 5.8%，會暫停到 11-09 的上漲日 |

全部 `replay_reference_only`，是算術建議，不是驗證。

### 7.4 抄底的反事實：若在這次連跌中分批買進

算法：以當天收盤價等權買進「當天那份 run 的交易 Agent 中高信心 `buy` 名單」，不計費用（單程約 0.24%）；另列交易池等權當對照。每批 5% NAV。沒有計入換出持股若留著的表現，也沒有驗 manifest。證據等級 `replay_reference_only`，而且是事後算術：當時的 Agent 與裁決會不會提名、放行，無法得知。

| 批 | 進場（收盤） | 名單檔數 | 到 11-06（低點） | 到 11-10 | 5% NAV 的損益：低點／11-10 | 交易池同期：低點／11-10 |
| --- | --- | --- | --- | --- | --- | --- |
| 連跌第 3 天 | 11-02 | 12 | −18.7% | −9.0% | −0.94%／−0.45% | −15.0%／−8.1% |
| 連跌第 5 天 | 11-04 | 16 | −10.4% | −2.3% | −0.52%／−0.12% | −10.0%／−2.4% |
| 兩批合計（10% NAV） | — | — | — | — | **−1.46%／−0.57%** | −1.25%／−0.52% |
| 參考：第 6 天 | 11-05 | 7 | −2.6% | +1.8% | −0.13%／+0.09% | −5.0%／+3.1% |
| 參考：第 7 天（事後才知道是低點） | 11-06 | 10 | — | +8.0% | —／+0.40% | —／+8.8% |

對回撤的影響（實際回撤：11-06 收盤 5.45%、11-10 收盤 4.70%）：

| 做法 | 11-06 的回撤 | 11-10 的回撤 |
| --- | --- | --- |
| 實際 | 5.4% | 4.7% |
| 第 3、5 天各買 5% | 約 6.9% | 約 5.3% |
| 照 7.3 建議值的梯度：第 4 天（11-03）進一批 8%，之後因再跌 5.8% 暫停到 11-09 | 約 6.1%（該批 −8.0%） | 約 5.0%（該批 −3.2%） |

讀法：

- 在第 3、5 天抄底，到 11-10 還沒回本：第 3 天那批要再漲約 9.9%、第 5 天那批要再漲約 2.4%。使用者說的「這兩天大漲吃不到」是對的，但提前兩三天買進的部位在大漲之後仍是虧的，因為中間又跌了 10% 以上。
- 抄底真正賺的是第 6、7 天進的批，而那兩天正是暫停線會擋住的時候（剛再跌 5% 以上）。這是分批規則的內在矛盾：暫停線保護第 3～5 天不連續接刀，也會錯過最低點。
- 所以抄底主軸的合理期待是「反彈延續時，部位已經在裡面」，不是「買了就賺」。它的成本上限約 1.5～2.2% NAV，報酬取決於之後反彈有多大。
- 同一段期間程式實際做的是相反的事：第 4 天賣出 6.5% NAV。被賣的 8 檔到 11-06 又跌 6～18%，到 11-10 只有 3443（+5.5%）、3533（+1.4%）高於賣出日收盤。當時的防守在這段行情裡沒有吃虧。

## 8. 風險與反面

| 風險 | 怎麼虧 | 擋法與剩餘風險 |
| --- | --- | --- |
| 假反彈 | 在 11-09、11-10 這種大漲後進場，隨後像 10-28 之後 5 日 −10.9%。視窗內 15 次大漲日有 5 次 5 日後為負 | `beta_stop` 界定曝險；`probe` 用小份量；回撤停用線。擋不住的是：進場後的跌幅照 beta 全數承擔 |
| 回撤觸發強制防守後的來回 | 組合 beta 拉高後遇大跌，回撤越過 10%：姿態被強制防守、現金拉回 15% 以上、`drawdown_derisk` 在低點賣出；之後反彈又沒有部位 | `offense_off_drawdown` 先一步停止加碼；穩健值讓 −10.5% 的情境停在 9.2%。若跌幅更大仍會越線 |
| 等級來回切換 | 今天 `probe` 把現金放到 12%，明天 `keep_defensive` 回到 15%，後天又 `probe`：每次來回約 3% NAV 的成交 | `max_daily_raise` 與格點限制單日幅度；費稅小（約 2～3 bp NAV），主要成本是賣在低點。階段 4 的 `probe_max_days` 才有記憶 |
| 單一鑰匙被帶偏 | 新聞或公告文字誘導「已經轉向」，風險 Agent 升級 | 必須有非新聞的市場層級證據與至少兩類程式事實；外部文字照舊不受信任。階段 0 沒有這層保護：`turn_assessment` 只驗值域 |
| 列舉變成變相最大值 | 風險 Agent 每天都選 `rebuild` | `rebuild` 須 `turn_confirmed`；交易 Agent 沒提名就不換；上限是上限不是目標；紀錄選擇分布 |
| 失效條件流於形式 | `failure_condition` 寫得空泛，程式只能驗結構 | 階段 4 的前次失效檢查讓它隔天被追問；在那之前靠人檢視 |
| 放掉防守持股的代價 | 低 beta 的金融、電信股在下跌時保住淨值；換掉後反彈失敗，同時失去保護又買在高點 | 高信心持股讓位前須逐檔交代中低信心持股；`min_holding_days` 與再買提示擋來回 |
| 階段 0 改變 Agent 的行為 | 簡報已寫明 `turn_assessment` 會放寬現金；填了欄位後，風險 Agent 可能更不願意填 `turn_unconfirmed` | 無法事先得知；開啟後看列舉分布 |
| 高 beta 集中在同一產業 | 買進名單多為半導體與電子，同一事件一起跌 | 產業 60% 上限與 `sector_fit` 不變；風險審查照舊標示集中度 |
| 時程 | 做完只剩約 5 個交易日，樣本不足以判斷有沒有用 | 分階段，每階段可獨立回滾；階段 0 先上 |
| 沒有前瞻證據 | 舊 run 沒有這些輸出，回放略過本閘門 | 全部標 `replay_reference_only`；只有比賽期間即時紀錄算證據 |

抄底主軸的風險：

| 風險 | 怎麼虧 | 擋法與剩餘風險 |
| --- | --- | --- |
| 接刀 | 連跌第 3 天進場的那一批，4 個交易日後 −18.7%（7.4）。跌勢中沒有人知道哪一天是底 | 分批、間隔、暫停線、總曝險上限；單批最差約 −0.9% NAV、總上限 12% NAV 時最差約 −2.2% NAV |
| 超跌的原因不明 | `oversold` 只表示比 beta × 大盤多跌，可能是市場已知而資料還沒有的壞事 | 超跌另設更小的組數與權重；有新壞事即不是候選；不得寫「便宜」「低估」 |
| 把對照樣本當規律 | 視窗內只有 5 次連跌滿 3 天，第 5 次就是反例 | 標 `small_sample`、不可引用、不得寫進理由 |
| 賣掉防守持股去接刀 | 抄底配對的錢來自換出持股；低 beta 持股在下跌中跌得少，換掉後兩邊都虧 | 換出者須是 `hold`／low 且有自己的換出依據；買進不超過賣款 |
| 與防守姿態互相抵銷 | 風險 Agent 一邊拉高現金、一邊核准抄底 | 裁決須填 `defensive_reconciliation`；抄底只動換股、不降現金目標 |
| 剩下的交易日可能用不上 | 抄底只在再次連跌時啟動；若市場續漲，這段工期沒有產出 | 階段 D 排在簡報修正之後，約 1.5～2 天；人可選擇先做階段 3 |

進攻版另外增加的風險：

| 風險 | 怎麼虧 | 擋法與剩餘風險 |
| --- | --- | --- |
| 越過強制防守線 | 組合 beta 0.70 遇到 11-04～11-06 的跌幅，回撤約 11.7%。現行 10% 線會強制防守、把現金拉回 15% 以上，在低點賣出 | 第 7 節第 3 項：把線調到 15% 並設 12% 的進攻停用線。調高後，帳戶最大回撤的上限也跟著變大 |
| 沒有退出的連續下跌 | 10-28 收盤進場、持有到 11-06，交易池 −19.8%；組合 beta 0.70 約 −13.9% NAV，回撤約 18%，連 15% 的線也會越過 | 只有風險 Agent 及時降回 `keep_defensive`、或進攻停用線先觸發才擋得住；兩者都發生在已經虧損之後 |
| 一次大幅換股換在高點 | 確認日通常已是大漲之後；3 天內換掉 28% NAV 的低 beta 持股，之後若回落，保護與成本同時失去 | `beta_stop` 與換手讓它分 2～3 天完成，每天可由 Agent 重新判斷；建倉與撤回的費稅滑價合計約 0.5% NAV |
| 交易 Agent 不提名 | 上限再高，沒有清單就不成交 | 無法由程式解決；階段 1 的簡報修正讓它知道清單會被用到 |
| 高 beta 候選集中在半導體 | 11-11 中高信心的 18 檔買進有 9 檔在 `industry:24`、4 檔在 `industry:28` | 產業 60% 上限與 `sector_fit` 照舊；風險審查標示集中度 |
| 排名的兩面 | 比賽看短期報酬，進攻版拉大結果的分布：對的時候追上，錯的時候把目前 4.7% 的回撤擴大到 10% 以上 | 這是人的風險偏好決定，本文件只提供算術 |

反面看法（應該先想清楚再開）：

- 11-11 的風險 Agent 也可能是對的。新聞面是通膨高於預期、升息預期升溫、外資連 9 日賣超；MA20 廣度仍在下降。兩天反彈 8.7% 之後才進場，本身就是追高。
- 現行組合在 10-08～11-10 交易池 −16% 的過程中回撤只有 4.7%，這個結果來自低 beta。進攻計畫是拿這個保護去換反彈的參與，方向對了多賺約 1～3% NAV，錯了多虧約 1～3% NAV，並可能觸發強制防守。
- 風險偏好上調是人的決定；本計畫只保證上限、可稽核與可回滾，不保證獲利。

## 9. 不變的邊界

- 確定性歸程式、不確定性歸 LLM：Agent 只輸出列舉、代號、識別碼與文字；現金比例、權重、股數、組合 beta、費稅全部由程式算。
- 上限表的數字只由人填、可回滾；版本閘門只由人設定，策略反思不得改。
- CompetitionGuard 與硬性規則不放寬；`rebound_participation` 的 `confirmed.max_turnover_rate` 不填。
- 程式不挑股、不排序、不補提名、不改寫交易 Agent 的意圖，只留旗標。
- 一換一：不足一張整組取消、換出者不賣，不會只賣不買。
- 背景不是證據；`offense_facts` 沒有 `evidence_id`，不可引用，文字出現其鍵名即拒絕。
- 缺資料、回撤強制防守、歷史不可用時永不升級。
- 現金姿態簡報不附交易 Agent 的意圖與信心統計。
- 沒有新欄位的舊 Policy、舊 bundle、舊 run 逐位元維持原重建行為。

以上全部是計畫。

## 10. 階段 2 實作紀錄（2026-11-11，14:40 已開啟，回滾樣板 `config/policy_versions/2026-11-11-before-offense.json`）

- 程式：`decision/offense.py`（閘門、事實、現金姿態驗證、有效等級、綁定重算）；`sizing.py`、`exposure_swap.py`、`exposure_facts.py`、`trader.py`、`finalization.py`、`allocation.py`（`_offense_limits`）、`risk_stages.py`、`agent_schemas.py`、`daily_pipeline.py`、`evaluation/replay.py`、`strategy_reflection/contracts.py`（`offense` 禁區）。測試：`tests/decision/test_offense.py`、`tests/automation/test_offense_pipeline.py`。
- 額外前提：除計畫列的前提外多要求 `agent_swap.entry_reserve`（`max_size` 與 beta 估算需要提名的份量格）與 `exposure_view` v2。
- 7.1 數值：probe 0.10／2／medium／0.40、start_rebuild 0.08／4／large／0.55、`offense_off_drawdown` 0.12 通過驗證；rebuild 的 `defensive_cash_buffer` 0.05 低於中性姿態最低格點 0.08，被既有驗證拒絕（未放寬），建議改 0.08，或另加人填的現金下限欄位。
- 情緒極端類的 5 日變化（`score_change_5d`）：由資料包內的原始回應（VIX／Put-Call／融資／加權指數，內容日不晚於參考日）以 `market_fear_greed.tools.score_as_of` 重算 5 個交易日前的正式分數；成分不足、當日重算分數與結果不符或缺資料時維持 null 並記 data_gap，不補值。`failure_condition` 的時間條件改由 `decision/time_condition.py` 檢查（數字加單位、固定時間點用語、日期；模糊用語不算），回饋訊息並列補期限與降級兩種修法；交易 Agent 的 `invalidation_conditions` 程式端只檢查非空字串、沒有結構檢查，未接線（接線須另開版本閘門）。
- 開啟時的設定清單：刪除 `rebound_participation.by_state.*.defensive_cash_buffer`、`agent_swap.exposure.max_pairs_by_view` 改成 probe／start_rebuild／rebuild 三鍵、加 `position_sizing.offense`、換新 `policy_id`、存回滾樣板。**已開啟（2026-11-11 14:40，使用者決定，`policy_id strategy-2026-11-11-offense-1`；rebuild 的 `defensive_cash_buffer` 用 0.08）。**

## 11. 審查 A4／A2：兩個選配子欄位（2026-11-11 已開啟，`policy_id strategy-2026-11-11-offense-ext-1`，回滾樣板 `config/policy_versions/2026-11-11-before-offense-extensions.json`）

起因：fable 審查後決定，並在 11-11 封存輸入上以模擬驗證（thesis 份量格：probe、30 檔滿名額，2 組 thesis large 由 2408 買 5.6% NAV 變 4.0%；3 組 thesis large＋2 組 exposure large 由成交 3 組變成交 5 組、買 15.9% NAV、換手 27.4%。現金回升：現金 10.3%，隔日 defensive／lower＋keep_defensive，現狀目標 0.15、賣 4.75% NAV；改後目標 0.13、賣 2.74% NAV，再隔一天才補到 0.15）。兩項都是 `position_sizing.offense` 的選配子欄位，沒有新的人填數字，沒有欄位逐位元不變。

- **A4 `thesis_size_cap`**（決定：套份量格上限，不限制動用放寬的現金）：生效條件為閘門已綁定、`cash_loosening` 不是 null、有效等級在 `by_view` 內；`purpose=thesis` 配對份量格取 min(交易 Agent 的 size, 該級 `max_size`)，有縮留 `OFFENSE_SIZE_CAPPED`；只縮不取消、不占 `max_exposure_pairs`、不被 `beta_stop` 取消；`quality_dip` 不動。程式：`offense.py`（欄位形狀、`thesis_size_cap_active`）、`allocation.py`（`_offense_limits` thesis 分支，呼叫條件放寬成「有曝險配對或此欄位開啟」）；提名簡報 `entry_reserve.note` 與提名提示、`swap-nomination.md` 各補一句，只在欄位開啟時出現。
- **A2 `cash_glide`**（決定：要限制，但範圍收窄、不做成通用規則）：沿用既有人填的 `cash_ratio_range.max_daily_raise`。生效條件見 `decision-contracts.md`；現金目標＝min(今日目標, anchor＋`max_daily_raise`)，接在 offense 放寬之後；回撤達 `offense_off_drawdown` 或強制防守時立即取消滑行。程式：`cash_ratio.py`（`glide_enabled`、`_prior_offense_loosened`，anchor 開啟時多 `prior_offense_loosened`，綁定與驗證共用同一函式）、`offense.py`（`cash_glide_ceiling`、`cash_raise_limited`、`drawdown_allows_glide`、`cash_glide_brief`、綁定與 `expected_cash_buffer` 重算）、`sizing.py`（anchor 形狀、綁定欄位集合）、`trader.py`（`apply_trade_decision` 小範圍套用）、`risk_stages.py` 與 `cash-stance.md`（簡報多一段背景 `offense_cash_glide`）。
- 測試：`tests/decision/test_offense_extensions.py`、`tests/automation/test_offense_extensions_pipeline.py`（形狀 fail-closed、缺席逐位元不變、各條件不成立照格點、Finalizer 竄改即拒絕、前次 run 的 policy 雜湊不符拒絕）。
- 開啟紀錄（人已完成）：在 `position_sizing.offense` 加 `"thesis_size_cap": {"version": "v1"}` 與／或 `"cash_glide": {"version": "v1"}`、換 `policy_id`、存回滾樣板；回滾＝刪欄位。注意：前次 run 是在加欄位之前產生的，其 policy 沒有 `cash_raise_limited` 但仍有 `cash_loosening`，所以開啟當天就能讀到前次放寬記錄。
