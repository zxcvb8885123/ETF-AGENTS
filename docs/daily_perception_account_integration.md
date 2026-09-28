# 每日情緒／共識與帳戶接入（2026-09-28）

## 已完成

### 情緒與分析師共識

每日決策不再固定捨棄 `perception_inputs`。流程為：已授權且與 Snapshot 相同 cutoff 的資料包 → 情緒 Agent 逐筆標註 → Python 去重聚合／共識修正 → Result validator → DecisionInputBundle → 情緒分析報告 → 多空研究及下游報告。缺資料的股票仍覆蓋並標示 unavailable；共識水準不直接轉成多空方向、候選或權重。

```bash
# 同一快照的資料已備妥時，逐筆標註由既有 Claude runner 執行。
.venv/bin/python scripts/run_daily_pipeline.py --skip-data \
  --perception-bundle /absolute/path/perception_data.json

# 已有研究結果時可略過標註；仍完整重算驗證。
.venv/bin/python scripts/run_daily_pipeline.py --skip-data \
  --perception-bundle /absolute/path/perception_data.json \
  --perception /absolute/path/perception_result.json

# 也可從既有標籤建立全池結果（輸出已存在時拒絕覆寫）。
.venv/bin/python cli/sentiment_research.py --bundle /absolute/path/perception_data.json \
  build-result --symbols 2330.TW 2317.TW --labels /absolute/path/labels.json \
  --run-id perception-daily --output /absolute/path/perception_result.json
```

`labels.json` 為 `{"labels": [...]}`；每筆保留 item_id、symbol、evidence_id、relevance、stance、rationale、model_version。只要視窗內有項目就必須完整標註，不得丟棄看似無關或重複的內容；去重在聚合階段處理。只有分析師資料而沒有情緒項目時，可用空 labels 陣列，不需呼叫模型。

`portfolio_decision.py build-input` 同樣支援 `--perception-bundle` 與 `--perception`，此入口必須成對提供。每日流程保存同一份資料包及結果供報告使用，Decision run 的輸入包保存整份資料與結果。情緒分析報告在下游重建時與確定性 adapter 比對，改寫文字後即使重算 hash 也拒絕。標註快取名稱綁定資料包內容；來源改版不沿用舊標籤。共識修正引用同時包含當期與前期來源。

### 每日虛擬帳戶

既有 `./start.sh daily` → `virtual_account.py daily` 流程保留，補強：

- 持股估值使用未還原 `close_price`；缺少原價即停止，不能拿調整價冒充現金價值。
- 決策含交易狀態包時，成交日固定使用其 `target_session.end`；缺少當日行情就等待，不改成後來有資料的日子。舊版沒有交易狀態包的封存決策保留原回放方式。
- 待買賣股票與持股必須都有官方行情；缺漏時保留待成交決策，不建立下一個 prepare。
- 新快照 cutoff 後才抓到的收盤資料不能用來更新當時帳戶。
- 開盤前依當天日期釋放到期交割款，不再用昨日行情日延遲一天；實際成交前也先釋放到期款。
- 成交行情、日終行情與交割日須符合實際成交日；錯誤即拒絕。
- 同一快照重跑重用原 prepare，補寫帳戶輸出檔，不新增帳本狀態或重複計費。
- 先檢查新快照基本契約與原價，再開始結算，避免壞快照造成部分更新。既有帳本鎖、父狀態連續性與不可變封存繼續生效。

## 部分完成與尚未接入

- **真實情緒／共識資料商尚未接入**：目前入口是 `JsonPerceptionDataProvider` 的已授權匯出格式。程式檢查 `license_status=approved` 不等同取得授權；資料供應方、使用權、歷史可得時間及匯出映射仍需確認。測試 fixture 不代表正式來源。
- **交割日曆尚未接入**：自動 T+N 仍沿用週一至週五近似，結果明確回傳 `settlement_calendar_basis=weekday_approximation`；假日及僅辦理交割的日期尚未驗證，不能宣稱正式交割對帳完成。
- 帳戶成交仍是規則驅動的虛擬成交，不是券商成交；公司行動自動資料接入、主辦方對帳及連續多日真實決策驗收仍待完成。
- 本次未重跑一個月回測，也未呼叫付費資料服務或啟動正式模型決策。

## 驗證

全套單元測試、Python 編譯與 `git diff --check` 通過。另以目前真實 Snapshot 重跑 prepare：回傳 `reused`，前後帳戶狀態雜湊相同；未新增成交或改變 10 億元空倉帳戶。此檢查不等同多日真實成交驗收。
