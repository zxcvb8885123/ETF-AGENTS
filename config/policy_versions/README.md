# 策略樣板內容快照

- `2026-11-09-before-review.json`：2026-11-09 本次六項審查修正前的正式樣板，原樣保留（canonical SHA-256：`11f94abcd3d50043a4856065d5aa06d136f30ed1eb1bae5e7f2a232750c109a0`）。其中沿用的舊 `policy_id`／`available_at` 正是本次修正的問題；此檔不是該舊日期當時策略的證據，不可用它回填歷史。
- 正式樣板仍是 `config/decision_policy.json`；目前版本及生效時間只記在 `docs/status.md`。版本變更另存新檔，不覆寫既有快照。
- 舊 run 以各自封存的 `policy.json` 重建，不改寫既有 artifacts。將新策略套用較早日期的測試屬反事實評測，必須保留原始生效時間並標示。
- 舊行為測試可明確使用本目錄固定快照；只有正式設定整合測試讀 live 樣板，避免正式開關變動悄悄改變舊 fixture。
- `2026-11-09-exposure-rotation-v1.json`：2026-11-09 17:50 切換 `exposure_rotation` 到 v2 之前的正式樣板（`policy_id: strategy-2026-11-09-turnfacts-v1`，`available_at: 2026-11-09T16:10:00+08:00`，曝險換股 v1；canonical SHA-256：`02aa46be22779a7dafc3c9a280e0f193e84558d61340fec58ef555a5473eb54d`）。要回滾曝險換股時，以此檔的 `position_sizing.exposure_rotation` 區塊為準；舊 run 仍讀各自封存的 `policy.json`。
- `2026-11-10-rebound-v1.json`：2026-11-10 切換 `rebound_participation` 到 v2 之前的正式樣板（`policy_id: strategy-2026-11-09-rebound-v1-1`，反彈參與 v1、只開 confirmed，已含 `market_index_facts`）。要回滾反彈參與到 v1 時，以此檔的 `position_sizing.rebound_participation` 區塊為準；舊 run 仍讀各自封存的 `policy.json`。
- `2026-11-10-before-quality-dip.json`：2026-11-10 開啟 `quality_dip_facts`、`quality_dip_assessment`、`event_direction` 之前的正式樣板。要回滾優質股紀錄閘門，刪掉 `position_sizing` 的這三個欄位（舊 run 仍讀各自封存的 `policy.json`）。
- `2026-11-11-before-derisk-day-view.json`：2026-11-11 開啟 `agent_swap.derisk_day_view` 之前的正式樣板（`policy_id: strategy-2026-11-10-final-versions-1`）。要回滾，刪掉 `position_sizing.agent_swap.derisk_day_view`（舊 run 仍讀各自封存的 `policy.json`）。
- `2026-11-11-before-proceeds-to-add.json`：2026-11-11 開啟 `agent_swap.proceeds_to_add` 之前的正式樣板（`policy_id: strategy-2026-11-11-derisk-day-view-1`）。要回滾，刪掉 `position_sizing.agent_swap.proceeds_to_add`（舊 run 仍讀各自封存的 `policy.json`）。
- `2026-11-11-before-revision-bounds.json`：2026-11-11 開啟 `position_sizing.revision_bounds` 之前的正式樣板（`policy_id: strategy-2026-11-11-alerts-v2-1`）。要回滾，刪掉 `position_sizing.revision_bounds`（舊 run 仍讀各自封存的 `policy.json`）。
- `2026-11-11-before-swap-priority.json`：2026-11-11 開啟 `agent_swap.entry_reserve.swap_priority` 之前的正式樣板（`policy_id: strategy-2026-11-11-revision-bounds-1`）。要回滾，刪掉 `position_sizing.agent_swap.entry_reserve.swap_priority`（舊 run 仍讀各自封存的 `policy.json`）。
- `2026-11-11-before-liquidity-fit.json`：2026-11-11 開啟 `position_sizing.liquidity_fit` 之前的正式樣板（`policy_id: strategy-2026-11-11-swap-priority-1`）。要回滾，刪掉 `position_sizing.liquidity_fit`（舊 run 仍讀各自封存的 `policy.json`）。
- `2026-11-11-before-probe-3.json`：2026-11-11 把 probe 曝險配對由 2 改 3 之前的正式樣板（`policy_id: strategy-2026-11-11-liquidity-fit-1`）。要回滾，把 `offense.by_view.probe.max_exposure_pairs` 與 `agent_swap.exposure.max_pairs_by_view.probe` 改回 2。
- `2026-11-12-before-liquidity-v2-alerts.json`：2026-11-12 把 `liquidity_fit` 升 v2、放寬 `position_review` 的 `gain_alert`／`trail_alert`、`quality_dip.max_entry_weight` 改 0.06 之前的正式樣板（`policy_id: strategy-2026-11-11-probe-3-1`）。要回滾，以此檔對應區塊為準（`liquidity_fit` 改回 v1、警示改回 0.10／0.05／0.05、`max_entry_weight` 改回 0.04）。
- `2026-11-12-before-turnover-029.json`：2026-11-12 把 `max_turnover_rate` 由 0.3 改 0.29 之前的正式樣板（`policy_id: strategy-2026-11-12-liquidity-v2-1`）。要回滾，把 `max_turnover_rate` 改回 `"0.3"`。
- `2026-11-12-before-probe-beta-045.json`：2026-11-12 把 `offense.by_view.probe.beta_stop` 由 0.40 改 0.45 之前的正式樣板（`policy_id: strategy-2026-11-12-turnover-029-1`）。要回滾，把該值改回 `"0.40"`。
- `2026-11-12-before-swap-cap-7.json`：2026-11-12 把 `offense.by_view.probe.beta_stop` 由 0.45 改 0.55、`agent_swap.max_swaps_per_run` 與 `rotation.max_swaps_per_run` 由 5 改 7 之前的正式樣板（`policy_id: strategy-2026-11-12-turnover-033-1`）。要回滾，把這三個值改回 `"0.45"`、5、5。
