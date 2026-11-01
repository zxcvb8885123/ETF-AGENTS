# 跨市場隔夜訊號回測（Phase 3）

## 目的與範圍

驗證[隔夜市場背景](overnight_market.md)的訊號（美股、日韓、VIX、台指期夜盤、方向一致性）過去兩年對台股隔日有沒有預測力，作為「要不要把它變成確定性姿態下限」的依據。**離線研究，不屬於決策鏈**：`decision/`、`automation/` 與 `scripts/run_daily_pipeline.py` 都不匯入 `research/cross_market_study/`（有測試鎖定），也不回寫任何 Decision artifact。

- 程式：`src/etf_agent/research/cross_market_study/`（`contracts.py` 事先登記的判準、`provider.py` 抓歷史、`tools.py` 重建與統計、`service.py` 不可變保存），CLI 為 `cli/cross_market_study.py`。
- 訊號重用正式流程的 `historical_signals`（`perception/overnight_market/tools.py`，與每日背景同一套視窗與公式），決策時點固定台北 07:00。
- 資料：Yahoo 日線（`^GSPC`、`^IXIC`、`^SOX`、`EWT`、`TSM`、`^N225`、`^KS11`、`^VIX`、目標 `^TWII` OHLC）與 FinMind `TaiwanFuturesDaily` 的 TX 盤後列，約兩年（760 日曆日，才夠 250 個交易日的最低樣本）。原始值不可變保存在 `artifacts/cross_market_study/data/`，結果在 `studies/`。

```bash
set -a; . ./.env; set +a          # 需要 FINMIND_TOKEN
.venv/bin/python cli/cross_market_study.py fetch      # 抓並保存歷史
.venv/bin/python cli/cross_market_study.py fetch-pool --decision-input <decision_input.json>   # 凍結交易池日線與官方均價
.venv/bin/python cli/cross_market_study.py analyze    # 重建並統計（有交易池資料時一併做現金姿態模擬）
```

## 事先登記的判準（看結果前寫死在 `contracts.py`）

- 樣本少於 250 個交易日只報筆數，不判通過。
- 主要檢定只有三組：`us_main`（^SOX 與 ^GSPC 平均）、`night`（夜盤漲跌幅）、`agreement_score`（上下方向差 ÷ 總數）對**開盤跳空**（t 日開盤 ÷ t−1 日收盤）的 Rank IC。三組 Bonferroni 近似，|t| ≥ 2.4 且前後兩半樣本符號一致才算 `supported`。
- 其他目標（開→收、收→收、均價代理、振幅）與其他訊號只作探索，**不判通過**（多重檢定）。
- 事後補的目標（第一輪結果出來後才加，報告標 `post_hoc`）：`fill_to_close`、`fill_to_next_avg`，以 t 日均價代理成交，看成交後的報酬。這才是現金姿態的決策價值所在，但因為是看過結果才加，可信度低於事先登記者。

## 第一輪結果（2024-10-01～2026-10-30，約 500 個交易日）

- **事實**：隔夜訊號與同日開盤跳空高度相關。Rank IC：夜盤 0.75（t 25）、us_main 0.63（t 18）、agreement_score 0.57（t 16），前後兩半皆同號，三組主要檢定都 `supported`；跳空方向命中率夜盤 82%、us_main 77%、agreement 78%（Wilson 95% 下界皆 > 73%）。
- **事實**：`negative` 日（69 天）平均跳空 −0.68%、收→收 −1.43%；`positive` 日（141 天）平均跳空 +0.63%、收→收 +1.08%。`aligned_down`（54 天）跳空 −0.41%，`aligned_up`（105 天）+0.51%。
- **事實（事後補的目標）**：訊號對「t 日均價 → t+1 日均價」的預測力弱得多：夜盤 IC 0.17（t 3.9，前半 0.09、後半 0.22，`supported` 但前半很弱），us_main 0.08（t 1.7）與 agreement 0.06（t 1.4）都 `not_supported`。
- **推論**：隔夜訊號幾乎完全被開盤價吸收，而比賽以當日成交均價成交；也就是說它解釋「今天的價位」，對「買進之後賺不賺」只有夜盤有一點點、且不穩的額外資訊。拿它直接調高現金（少買）不能從這份結果證明有利。
- **限制**：①目標用 `^TWII` 指數與 (O+H+L+C)/4 均價代理，不是比賽用的官方成交均價，也不是交易池；正式結論要用 `calibration` 的官方均價重跑。②夜盤採 FinMind 歷史（日期語意：當日日盤之前的那段夜盤）。③第一輪沒有扣除交易成本，也沒有模擬現金姿態對 NAV 的影響。④`fill_to_close` 的 IC（0.19～0.28）受均價代理含收盤價影響，可能有機械性偏誤，不當證據。

## 結論與下一步

- 目前的證據只支持「隔夜訊號是開盤跳空的好指標」，**不支持**把它做成確定性姿態下限。維持「風險 Agent 的背景」的現狀是合理的。
- 若要再往下：①用官方成交均價與交易池（`OfficialPriceBook`）重跑 `fill_to_next_avg`；②模擬「隔夜偏負面→現金拉高」對 NAV、回撤的影響；③累積自己的 07:00 決策日再看。以上都還沒做。
