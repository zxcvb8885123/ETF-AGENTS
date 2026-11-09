# 策略樣板內容快照

- `2026-11-09-before-review.json`：2026-11-09 本次六項審查修正前的正式樣板，原樣保留（canonical SHA-256：`11f94abcd3d50043a4856065d5aa06d136f30ed1eb1bae5e7f2a232750c109a0`）。其中沿用的舊 `policy_id`／`available_at` 正是本次修正的問題；此檔不是該舊日期當時策略的證據，不可用它回填歷史。
- 正式樣板仍是 `config/decision_policy.json`；目前版本及生效時間只記在 `docs/status.md`。版本變更另存新檔，不覆寫既有快照。
- 舊 run 以各自封存的 `policy.json` 重建，不改寫既有 artifacts。將新策略套用較早日期的測試屬反事實評測，必須保留原始生效時間並標示。
- 舊行為測試可明確使用本目錄固定快照；只有正式設定整合測試讀 live 樣板，避免正式開關變動悄悄改變舊 fixture。
