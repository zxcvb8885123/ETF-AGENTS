# 大盤指數事實（`position_sizing.market_index_facts`）：只紀錄、不立規則

狀態（2026-11-10）：**v1 已實作、尚未開啟**（`config/decision_policy.json` 沒有此欄位，由人決定何時加）。已有 13 個單元測試與全套測試通過。

## 起因

2026-11-09 21:10 的晚間 run，風險 Agent 給 `defensive`。簡報裡的隔夜市場背景整體「資料不足」：美股 21:30 才開盤、台指期夜盤 05:00 才結束，晚上跑的決策**每次**都沒有。於是風險 Agent 看不到「今天大盤怎麼走」。

但證交所加權指數當天收盤後就有：`artifacts/data/market_fear_greed/` 的 `TWSE_TAIEX`（`FMTQIK` 月檔）在 21:10 的抓取已含當日列。實例：

| 日期 | 收盤 | 漲跌 |
| --- | --- | --- |
| 2026-10-30 | 45,211.83 | -1,156.50 |
| 2026-11-06 | 39,701.64 | -1,873.08 |
| 2026-11-09 | 41,661.70 | +1,960.06 |

現況：加權指數只被恐懼貪婪的「融資水位」一項用到，沒有單獨交給風險 Agent。

## 做法

- 新模組 `decision/market_index.py`：`market_index_facts(bundle)` 只讀決策輸入內（同 Snapshot／cutoff 之前已抓取）、`license_status=approved` 的 `TWSE_TAIEX` 原始回應，重算：最新收盤、當日漲跌點與漲跌幅、近 5／20 個交易日報酬、距近 60 日高點的回檔幅度、收盤相對 MA20／MA60。全部字串化 Decimal，Agent 不算數字。
- 時間規則：只取 `available_at <= decision_cutoff` 的列；最新列日期不等於 `snapshot.latest_trade_date` 時記 `data_gaps`、不補值；歷史不足 60 日，對應欄位不給並記缺口。
- 風險 Agent 簡報在 `regime_assessment` 旁附 `market_index_facts`（`citable=false`，是背景不是證據），Skill 只加一般性提醒：這是事實不是訊號，單日大漲不代表趨勢反轉，大跌後反彈也可能失敗。不寫特定日期案例。
- 不改現金姿態對應、`cash_grade` 格點、Guard、`MomentumEngine._regime`；不新增 Agent 輸出欄位（先不加列舉，避免和 `turn_assessment` 重複）。數字只讀決策輸入已封存的 `market_fear_greed_input` 原始回應與其來源核准（沒有額外 I/O），因此不需要 pipeline 另外綁定資料。
- 版本閘門：`config/decision_policy.json` 的 `position_sizing.market_index_facts: {"version": "v1"}`，只由人設定、策略反思不得改。沒有欄位的舊 Policy／舊 run 逐位元不變；`team_inputs` 不升版。不需要 `typed_evidence_v1`。因為沒有 Agent 輸出欄位也不影響配置，Finalizer 不重算（簡報已不可變封存，輸入 bundle 本身有雜湊）。

## 改動的檔案

- 新增：`src/etf_agent/decision/market_index.py`、`tests/decision/test_market_index_facts.py`
- 修改：`automation/risk_stages.py`（簡報）、`automation/daily_pipeline.py`（把閘門傳給風險步驟）、`decision/sizing.py`（閘門登記與形狀檢查）、`strategy_reflection/contracts.py`（列為人專用）、`tradeagent/risk/portfolio-risk-review/references/cash-stance.md`
- 文件：`docs/status.md`（閘門狀態）、`docs/decision-contracts.md`、`docs/glossary.md`、`README.md`、`AGENTS.md` 版本閘門清單

## 測試（fail-closed）

- cutoff 之後的列不得進入；缺時區拒絕。
- 最新列日期與 `latest_trade_date` 不符 → `data_gaps`，不補值。
- 歷史不足 60 日 → 對應欄位缺、不報假值。
- 原始回應被改、欄位名變更、單位衝突 → 拒絕。
- Finalizer 重算與簡報內容不同 → 拒絕。
- 沒有閘門的舊 Policy 簡報逐鍵相同。

## 觀察期與回滾

- 只紀錄、不改配置。觀察至少 10 個決策日，比較風險 Agent 姿態與當日大盤走勢是否一致。
- 回滾：刪除 `market_index_facts` 欄位即可。

## 已知限制

- `FMTQIK` 是收盤後資料；收盤前（例如 07:00 的 run）最新列是前一個交易日，屬正常。
- 證交所來源已於 2026-11-04 由使用者核准（見 [market_fear_greed.md](market_fear_greed.md)），本計畫不新增來源。
