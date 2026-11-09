# 反彈參與閘門（`position_sizing.rebound_participation` v1）：設計與重放結果

狀態：**2026-11-09 23:45 已開啟，只開 confirmed 一檔**（人決定；candidate 為實質不動、未填兩個放寬風控的選填欄位）。重放 (c) 的 1.0% NAV 假訊號成本目標未達，開啟是人承擔此風險的決定，不代表驗證通過。本檔所有數字都是 `replay_reference_only`：只固定封存的 Agent 意圖與價格重算確定性程式，不是對 LLM 的前瞻驗證；只有比賽期間的即時紀錄算證據。

## 動機與過擬合風險

2026-11-09 池等權 +5.51%，帳戶 NAV 只有 +0.40%，持股 beta 只有 0.13；防守姿態下 `exposure_rotation` 不觸發。但 10-28 的反彈之後 5 日池報酬約 −10.9%（假反彈）。歷史（61 個可算 regime 的日子，其中只有 7 個 bear 日）只有極少數大漲日，**門檻是 n ≤ 3 的擬合，過擬合風險高**；任何門檻都不應被視為已驗證。

## 設計

定義、前提、對映與雙鑰匙見 [decision-contracts.md](decision-contracts.md#反彈參與positionsizingrebound_participation-v1-已開啟v2-已實作尚未開啟)。要點：程式狀態機 `turn_state`（none／candidate／confirmed，只在 regime 為 bear 時有意義）與風險 Agent 既有 `turn_assessment` 取較保守者；只放寬防守姿態的曝險換股；缺資料永不升級；`team_inputs` 2.8。

實作時的解讀選擇（規格沒寫死）：「轉向日之後累積池報酬 ≥ 0」不含轉向日當天；candidate 第 1 日為轉向日，第 2、3 日可確認，第 4 日仍未確認回 none，期間再大漲不重設；`stance_floor` 只有為 defensive 時才強制 none（floor 為 neutral 時現行規則本來就允許 0.55 目標）；對映取「只放寬」（停止線取大、檔數取大、持有天數取小）。regime 離開 bear 即回 none：真實反彈常使 MA20 廣度快速站上 40%，confirmed 可能因此提早結束。

## 重放結果（2026-11-09 工作區；腳本在暫存目錄，不在專案內）

(a) 狀態機路徑（121 日池，含 11-09 的補入 bar，來自 `var/etf_agent.db`，標為合成）：只有 61 日可算；唯一事件是 11-09 none→candidate（池 +5.51%、站回 MA5 比例 0.32，只比門檻 0.30 高一點、新低比例 0.07）。10-28（+2.82%、regime 為 neutral）不會觸發。沒有 confirmed／退回事件，也沒有事件後 1／3／5 日報酬（尚無資料）。

(b) 11-09 真實持股、防守／middle／diversified、hold 全 medium、價格與 beta 固定：
- 照原 buy 名單：candidate／confirmed 在第 3 日起才換成（前兩日換入者因現金／配置被取消），第 4 日起始 beta 0.241／0.242，第 5 日 0.364，之後因 `no_eligible_fill` 停在約 0.37，**未達 0.40**；none 全程約 0.09。每日換手最高 0.2998，Guard 零拒絕，`ProposalValidator` 重建無錯誤。
- 另補 30 檔候選買單（敏感度）：candidate 第 4 日起始 beta 0.384、第 5 日 0.458；confirmed 0.375／0.436；**3 個決策日內仍未達 0.40**。最高換手 0.301（confirmed 第 3 日）略超 0.3。
- 全程為 Proposal 層級重建；2.8 整鏈 Finalizer 沒有 typed 夾具，未跑 approved 的端到端。

(c) 假訊號壓力（11-09 世界的持股，價格依 10-28 之後真實漲跌比率移動；candidate 只在前 3 個決策日）：相對 none 的 NAV 差，照原 buy 名單最差 −1.45%（11-06）、5 日後 −0.41%；補 30 檔買單最差 −3.39%（11-06）、5 日後 −1.43%；費稅合計 10.6～20.3 bp。**超過 1.0% NAV 的目標**。

### 第二輪（使用者確認後加入三項改動，2026-11-09 晚）

改動：(1) 條件式現金放寬（`by_state.<state>.defensive_cash_buffer` 0.09，兩把鑰匙都同意且姿態為 defensive 才覆寫風險 Agent 的 defensive 現金子等級，只放寬）；(2) 小動大動分開（candidate：`max_swaps_per_run` 2、`min_holding_days` 5，只放寬停止線；confirmed：4 與 0）；(3) 換手上限：`max_turnover_rate` 是 `config/decision_policy.json` 的可調 Policy 值，不是競賽硬規則（`competition_rules.json`、`competition_profile.json`、`decision_rules.json` 都沒有），故加選填 `by_state.confirmed.max_turnover_rate`（重放用 0.4，放寬風控、由人設定）。以下為重放（`replay_reference_only`；門檻未調整）。

(b) 11-09 世界（防守／middle／diversified，hold 全 medium）：
- 照原 buy 名單：candidate 第 4 日起始 beta 0.432、confirmed 第 3 日起始 0.428，**達 0.40**；每日換手 candidate 最高 0.267、confirmed 最高 0.312（超過 0.3、低於 0.4 的上限）；Guard 零拒絕、`ProposalValidator` 重建無錯誤；現金比例約 9.3～9.6%。
- 補 30 檔候選買單：candidate 第 4 日起始 0.374（**未達 0.40**，第 5 日 0.406）；confirmed 第 3 日起始 0.414、第 4 日 0.481（**達標**）；candidate 換手最高 0.267、confirmed 最高 0.348（第 1 日；confirmed 不可能發生在轉向日當天，第 2 日起 0.312）；Guard 零拒絕、重建無錯誤。

(c) 假訊號壓力（11-09 持股、10-28 之後真實漲跌比率；candidate／confirmed 只在前 3 個決策日，之後回 none；以**最差單日**相對 none 的 NAV 差判斷）：
- 照原 buy 名單：candidate 最差 −3.22%（11-06）、confirmed −3.26%；candidate 只 D1～D2 為 −1.80%。
- 補 30 檔買單：candidate 最差 −2.79%、confirmed −4.17%；candidate 只 D1～D2 為 −1.38%。費稅合計 15.8～27.9 bp。
- **全部超過 1.0% NAV**，且比第一輪（最差 −1.45%／−3.39%）不變好：現金放寬讓 beta 更快上到 0.4，也讓假訊號時多承擔下跌（beta 0.4 對 beta 0.09 在約 −8% 的行情下，光曝險差就約 −2.5%），退回 none 後防守姿態的 beta 目標仍為 null，不會自動降回，曝險由人與下一輪判斷。

結論：(b) 大致達標（只有補 30 檔買單的 candidate 差一點）；(c) 的 1.0% NAV 成本目標**未達**，且與 (b) 的 beta 目標在結構上互相拉扯（要在 3 日內參與就會在假訊號時付出曝險成本）。未調整任何門檻；若要調整須明說是事後調整，且會更過擬合。

## 開啟與回滾（人決定；本計劃不改設定）

先把 `exposure_rotation.version` 改成 `v3`（`beta_target_by_stance.defensive` 維持 null），再在 `position_sizing` 加入 `rebound_participation`（欄位見 decision-contracts）。回滾：刪除該欄位。
