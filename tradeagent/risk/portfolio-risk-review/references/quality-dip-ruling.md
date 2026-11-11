# 優質股換股裁決（`agent_swap.quality_dip` v3，簡報有 `pairs` 與 `note` 時才適用）

這是現金姿態之外的**另一次獨立呼叫**，排在現金姿態之後、配置之前。現金姿態的簡報完全不變（仍不附交易 Agent 的意圖與信心統計）；本次簡報只附交易 Agent 提名的 `purpose=quality_dip` 配對，這是對「風險 Agent 簡報不附交易 Agent 意圖」的一個窄例外：只有這些配對與其背景，沒有其他股票的意圖、信心統計，也沒有 thesis 與 exposure 配對。

## 你要做什麼

交易 Agent 在換股提名提出了幾組「優質股被大盤拖累」的換股（買 `buy_symbol`、賣 `sell_symbol`），並為每組選了份量格 `size`：`small`（試單）、`medium`、`large`（把握最高）。組數與份量由它判斷，你負責風險裁決：對**每一組**依序給一個 `ruling`：

| `ruling` | 意思 |
| --- | --- |
| `approve` | 照交易 Agent 的份量格 |
| `downsize` | 份量降一格（`large`→`medium`、`medium`→`small`）；`small` 不能再降，只能 `approve` 或 `reject` |
| `reject` | 否決這一組：程式取消該對，換出者不賣 |

**你只能縮、不能放**：沒有升級的選項，不能新增或更換配對、不能改順序、不能改現金姿態、不能手寫權重或張數、不能覆寫 CompetitionGuard。全部 `reject` 是合法的（等於今天不做優質股換股）。

## 判斷依據

- 依市場與組合風險裁決：現金姿態（`cash_stance`：`level`、`cash_grade`、`turn_assessment`、`exposure_view`）、轉向事實 `turn_facts`、加權指數 `market_index_facts`、組合曝險與產業權重 `exposure_facts`、兩檔的 beta 與產業、換出者目前權重。
- 你**不重判個股論點的真假**，也不新增個股事實；`buy_basis`／`sell_basis` 的文字是交易 Agent 已採納的論點，只供你理解這一組在換什麼。
- 背景區塊（`turn_facts`、`turn_state`、`market_index_facts`、`exposure_facts`、`dip`）沒有 `evidence_id`、不可引用，理由也不要寫出這些區塊鍵名、`facts_id` 或前次決策識別碼（出現即退回重答）。

## 輸出欄位

```json
{
  "rulings": [
    {"buy_symbol": "XXXX.TW", "sell_symbol": "YYYY.TW", "ruling": "downsize",
     "reason_kind": "market_risk", "reason": "白話理由"}
  ],
  "evidence_ids": ["market-regime:pool-breadth"],
  "defensive_reconciliation": ""
}
```

- `rulings` 必須剛好涵蓋簡報 `pairs` 的每一組各一次、**順序相同**、代號相符；多一組、少一組都退回重答。
- `ruling` 是 `approve`／`downsize`／`reject`；`approve` 的 `reason_kind` 填 `none`；`downsize`／`reject` 的 `reason_kind` 必填：`market_risk`（市場風險）、`portfolio_concentration`（組合集中）、`timing`（時機）。每一項都要有 `reason`（簡短繁體中文）。
- `evidence_ids`：至少一個市場層級證據（簡報 `market_level_evidence_ids`），只從 `citable_evidence_ids` 選；個股行情只能作補充。
- `defensive_reconciliation`：本次現金姿態 `level` 為 `defensive`、或 `exposure_view` 為 `keep_defensive`，**而且你放行了任何一組**（有一組不是 `reject`）時必填，說明為什麼在防守看法下仍放行；否則（含全部 `reject`）填空字串。程式不會因為防守看法自動取消任何一組，它只驗有沒有填。
- 不得輸出任何數字（百分比、權重、張數、金額）。

## 失敗時

驗證失敗會帶錯誤回饋要你修正（最多三次）；仍失敗或暫時性錯誤時，程式封存為降級狀態並**取消全部 quality_dip 配對、換出者不賣**，其餘決策照常，不會退回「不經裁決直接成交」。
