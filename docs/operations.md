# 日常操作

每日排程、D-Plan、主辦方資料包、開新帳戶、Agent 評測與常見問題。指令總表見[根目錄 README](../README.md#常用指令)。

## D-Plan 候選

正式產生前，請人工確認 `config/competition_profile.json` 的隊伍、策略與實際模型資訊，並將 `strategy_status` 設為 `approved`。完整 `run` 會自動嘗試匯出；D-Plan 失敗不會否定已封存決策或阻止中文報告，原因會寫入同一次 `pipeline.json`。

若決策與報告已完成，只想重試最新候選，不需重跑 Agent：

```bash
docker compose run --rm agent python3 cli/dplan.py build-latest
```

輸出只是供人工檢查的候選檔，不會自動送件，也不代表已通過主辦方平台驗證。

## 每日排程：晚上備案、早上重做

排程（`scripts/launchd/`）週一到週五 07:00、週一到週四 21:00 各跑一次 `run`；週五晚上不跑（週一的決策繳交時間從前一日週日 19:30 才開始），改由週日 21:00 做週一的決策（看得到週末新聞，也讀到週六套用的新策略），週一 07:00 再重做（後做的取代先做的）：

- **21:00 備案**：用當天收盤資料做下一個交易日的決策與 D-Plan。早上出事時，手上仍有一份能交。主辦方約 19:00 才產生隔天的 DataPack，**21:00 前要先下載到 `var/datapack/`**，否則對帳不過、不會有備案。
- **07:00 重做**：在 08:55 交件期限前，用晚上到早上的新事件、新聞與情緒重做，取代晚上那份。
- **何時略過**：同一目標時段在 1 小時內已有完成的 run 才略過（例如 06:30 手動跑完，07:00 不跑）。半夜手動跑的不算，07:00 照常重做。
- **每次都保留**：run 目錄一律帶台北時間（`daily-<YYYYMMDD>-<HHMMSS>`）。每份 D-Plan 另存在 `artifacts/competition_d_plan/versions/<交易日>/`，檔名結尾是決策時間（台北）；主檔 `D-Plan_<team_id>_<交易日>.json` 永遠是最新一份。
- **跑完已過 08:55 不換主檔**：決策跑完時若已過目標時段的交件期限，不產生 D-Plan、不取代已上傳的決策；沒有備案時新決策移到封存區，帳本視為該時段沒交件。
- **重做失敗時自動還原**：早上重做沒產生新 D-Plan（失敗、被擋、決策被拒或跑完已過 08:55），程式把帳本、晚上的決策與 Snapshot 還原成重做前，重做建立的帳本狀態移到 `artifacts/ledger/<account_id>/rolled_back/`（不刪除）。通知會寫「已還原重跑前的備案決策」。

還原之後：

1. **上傳原本的 D-Plan 主檔**，它和帳本對得上。`artifacts/latest/latest.md` 仍是重做那次的報告或失敗診斷，上傳時以 D-Plan 主檔為準。
2. **該 run 不能 `--resume`**。若還想重做，08:30 前執行 `./start.sh run --force`（整條重跑，約 20 分鐘）。
3. **查原因**：腳本訊息一天一檔在 `var/logs/daily-<YYYY-MM-DD>.log`（台北日期，保留 30 天），launchd 的完整 stdout／stderr（含 `start.sh daily` 子程序輸出）在 `var/logs/launchd.log`；該次 `pipeline.json` 有 `superseded_pending`、`rolled_back`；還原本身失敗時記 `rollback_error`，需人工檢查帳本。子 Agent 逾時、CLI 非零結束或輸出不是 JSON 時會自動再試一次（間隔 30 秒，不拖過 08:55），`agent_calls` 中帶 `transient_error` 的那筆就是失敗的嘗試。

如果先手動跑並上傳了，之後 07:00 又重做成功，主檔會換成新的一份，請**重新上傳主檔**。否則帳本記的是新決策，交出去的是舊的，隔天和資料包對帳會對不上。

## 主辦方資料包（DataPack）

資料包是主辦方每天提供的結算結果（現金、持股、前日委託結果、違規紀錄），比賽以它為準。每天跑 `run` 前，到參賽者後台「文件繳交」頁下載資料包，放進 `var/datapack/`（不進 git，檔名不限）。

- 找不到本交易日的資料包、主辦方結算尚未產生（`missing` 含 `prev_settlement`）、持股股數不同或現金差超過容忍值時，流程在呼叫 Agent 前停止，並列出逐檔差異與可能原因（前日整批作廢、被排除代號、違規天數）。
- 對帳通過後，D-Plan 的 `target_weight` 以資料包的 `nav_tradable` 反推（主辦方公式 `floor(target_weight × 前日淨值 ÷ 前日收盤價 ÷ 1000) × 1000` 用的就是它），帳本淨值的小差異不會讓邊界上的股數差一張；資料包持股的收盤價和 Snapshot 不同、或要換算的股票缺交易所收盤價時，不產生 D-Plan。略過對帳時沿用帳本淨值。
- 不跑 Agent、只想確認帳本和資料包是否一致：`.venv/bin/python cli/virtual_account.py reconcile --datapack var/datapack/<檔名>.json`（一致回傳 0，不一致回傳 2，並印出現金差，可用來校準容忍值）。
- 對不上時，先確認資料包是最新的，再執行 `./start.sh run --rebase-to-datapack`：以資料包重建帳本（成本以結算收盤價代替），重建決策前快照，再對帳一次才跑 Agent。資料包須早於新 Snapshot 的截止時間、結算日須等於行情日；目前快照已有待成交決策時拒絕。
- 上傳前可再用最新資料包檢查 D-Plan（同隊同日、賣出不超過持股、每檔持股都有交代），不跑 Agent：

```bash
.venv/bin/python cli/dplan.py check-datapack --input artifacts/competition_d_plan/D-Plan_TEAM_11067_<交易日>.json
```

## 比賽第一天開新帳戶

測試期帳本會帶著測試持股，比賽第一天要開全新帳戶，舊帳戶保留不刪：

```bash
.venv/bin/python cli/virtual_account.py open-competition --new-account-id ai-cup-2026-final \
  --datapack var/datapack/<第一天的資料包>.json
```

- 會核對資料包是第一個交易日（全現金、0 檔、金額等於規則本金 10 億）才開帳，並把 `config/active_account.json` 改成新帳戶；之後 `start.sh`、每日流程、儀表板與 D-Plan 預設都用新帳戶。
- 第一天資料包還拿不到時，可先省略 `--datapack` 開帳（直接用規則本金），跑 `run` 時加 `--skip-datapack`，上傳前再用 `check-datapack` 以真正的資料包檢查。
- 要開在產生當天 Snapshot 之前；新帳戶的開帳時間必須早於決策截止時間。

## Agent 評測

- **資料抓齊率**（不花用量）：`.venv/bin/python cli/agent_eval.py coverage`。官方公告、月營收、財報與行情有沒有漏存；不完整時回傳 2。
- **評測集**：拿過去已封存的每日決策當考卷，`suite --name 名稱` 建立。
- **一致度與穩定度**（會花用量）：`run --suite 名稱 --repeats 2 --yes` 重跑，比較和當時結論、以及兩次重跑之間有多少相同、有幾檔正負相反。`--stop-after analysts` 只跑分析師，較省。
- **過程**：報告列出每次呼叫的重試、驗證失敗與用量。

- **準不準**：考卷的答案是之後真正的官方成交均價。看好的有沒有贏交易池平均、看壞的有沒有輸；價格還沒出來標「待答案」，累積到 10 個決策日才給命中率。之後再跑 `report --eval-id 編號` 就會更新。
- **換 Policy 會怎樣（便宜版回測，不花用量）**：`cli/policy_replay.py run --policy config/decision_policy.json --start 起日 --end 迄日 [--compare-policy 另一份樣板]` 逐日讀封存 run 的 Agent 輸出，用指定樣板重算權重、張數與訂單，依帳本口徑以下一交易日官方均價成交並結轉到下一天（回放自己的模擬帳本）。報告在 `artifacts/evaluation/replays/`，記錄樣板雜湊、`tradeagent/` 雜湊、git commit 與使用的 run；`verify --replay-id 編號` 整段重算驗證。缺日、缺價、前視價格與封存驗證失敗一律停止或標示；成交日少於 20 天的結果沒有統計意義。

## 資料與報告位置

| 路徑 | 內容 |
| --- | --- |
| `data/official_universe.csv` | 150 檔交易池輸入 |
| `var/etf_agent.db` | 行情、公司文件、來源原文、時間與版本 |
| `artifacts/latest/latest.md` | 最新中文報告或失敗診斷 |
| `artifacts/daily/<日期>/<run_id>/pipeline.json` | 每日執行狀態與阻擋原因 |
| `artifacts/decisions/<日期>/<run_id>/` | 不可變決策封存與重建輸入 |
| `artifacts/ledger/` | 虛擬帳本與帳戶快照 |
| `var/datapack/` | 人工下載的主辦方資料包（不進 git）；每次 run 的對帳副本在 `artifacts/daily/<日期>/<run_id>/datapack.json` |
| `config/active_account.json` | 使用中的帳戶 ID |
| `artifacts/competition_d_plan/D-Plan_<team_id>_<日期>.json` | 可人工上傳的 D-Plan v4.3 候選（同交易日永遠是最新一份） |
| `artifacts/competition_d_plan/versions/<交易日>/` | 每份 D-Plan 的帶時間副本，依交易日分資料夾，查得到交過哪一版；不刪不覆蓋 |
| `artifacts/competition_d_plan/contexts/` | D-Plan 內部 context 稽核檔；不是主辦方繳交檔 |

## 常見問題

### 資料更新成功，但當天不能決策

資料更新成功不代表當天可決策。行情覆蓋、官方價量、交易狀態與日曆都要通過檢查；沒通過時 `run` 會停止並顯示缺口。可先用 `./start.sh status` 查看資料狀態。

### `run` 顯示「虛擬帳本未建立本次 prepare 狀態」

原因是上一次決策還在等指定成交日的官方價量，帳本無法結算。依缺的資料處理：

| 缺的資料 | 處理 |
| --- | --- |
| 歷史上櫃（TPEx）價量 | 依下方三個步驟補抓並結算 |
| 上市（TWSE）價量，或來源未提供 | 無法補抓；`run` 會停止並顯示缺口，不會改用 Yahoo 價格 |

補抓上櫃價量，在專案根目錄依序執行：

```bash
# 1. 只補目前待結算決策所需的 TPEx 官方價量
.venv/bin/python scripts/repair_pending_tpex_close.py

# 2. 結算帳本，確認輸出 status=settled
.venv/bin/python cli/virtual_account.py settle

# 3. 再跑完整每日決策
./start.sh run
```
