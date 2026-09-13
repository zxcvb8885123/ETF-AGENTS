# ETF Agent Manager

AI CUP 2026「Agent 基金經理人」的可稽核 Agent 專案骨架。這個版本先把
**競賽硬性風控**做成程式，而非投資模型：送出交易前，策略輸出必須通過檢查。

## 已內建的初賽檢核

- 限制官方 150 檔交易池（名單尚未公布時，程式會明確標記為待補）
- 持股數 20–30 檔
- 現金不得為負且必須低於 NAV 的 25%
- 台積電 25% 上限；其他個股 10% 上限
- Active Share 必須至少 20%（需要官方主動式 ETF 前十大資料）
- 不合格的送件會被拒絕，並留下結構化稽核結果

## Agent 的四個開發主軸

本專案依主辦方複賽簡報要求，朝以下四個方向實作。所有新功能應明確歸屬
於其中一項，並且必須通過風控後才可以產生送件交易。

1. **Agent 投資策略核心**：蒐集與分析外部資料，產生選股、買賣與持有理由。
2. **投資組合邏輯**：將策略訊號轉成 20–30 檔股票、目標權重、加減碼與分散配置。
3. **風險控管策略**：管理現金、個股權重、交易成本、回撤與 Active Share，並攔截所有競賽違規。
4. **Agent 技術架構**：串接資料來源、模型/LLM、決策流程、風控、稽核紀錄，以及每日報告與交易書產製。

整體流程：`資料蒐集 → 策略決策 → 投資組合／風險控管 → 報告與交易提交`。

## 快速開始

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/check_setup.py
```

目前 `data/official_universe.csv` 和 `data/active_etf_top10.csv` 是官方資料的
匯入位置。收到競賽平台資料後，請保留欄位名稱，填入資料後再執行
`python3 scripts/check_setup.py`；兩份資料皆完整時，才可進入每日送件流程。

## 專案結構

```
config/                 競賽規則與可調整的安全緩衝
data/                   官方交易池與 Active Share 基準資料
docs/                   Agent 三模組開發規劃
src/etf_agent/          投資組合資料模型與送件前風控
scripts/check_setup.py  初始化與資料就緒檢查
tests/                  規則單元測試
```

完整規劃請參閱 [docs/agent_plan.md](docs/agent_plan.md)。

投資訊號、新聞/RAG、LLM 決策器與平台送件器尚未接入；它們應只產生提案，
不能繞過 `CompetitionGuard.validate()`。
