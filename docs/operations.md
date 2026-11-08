# 日常操作

這份文件回答「我今天要做什麼」。分四塊：每天固定要做的事、出事時怎麼辦、開新帳戶、安裝排程。指令總表見[根目錄 README](../README.md#常用指令)，`run` 的參數見[參數說明](run-options.md)。

## 系統每天自己會做的事

排程裝好後，時間都是台北時間：

| 時間 | 做什麼 | 排程名稱 |
| --- | --- | --- |
| 平日 20:00 | 更新行情、月營收、重大訊息、財報等資料 | `com.etf-agents.data` |
| 平日 20:40 | 抓個股新聞 | `com.etf-agents.finmind-news` |
| 週一至週四、週日 21:00 | 用當天收盤資料做「下一個交易日」的決策與 D-Plan，當作備案 | `com.etf-agents.daily` |
| 每天 05:05 | 抓隔夜市場背景（美股、日韓、VIX、台指期夜盤） | `com.etf-agents.overnight-capture` |
| 週一至週五 07:00 | 用新事件、新聞重做當天的決策，取代昨晚的備案 | `com.etf-agents.daily` |
| 週一至週五 08:00 | 看門狗：檢查今天該交的 D-Plan 在不在、有沒有通過驗證；07:00 重做還在跑時回報「尚在執行」 | `com.etf-agents.dplan-watchdog` |
| 週五 19:30 | 策略反思週報 | `com.etf-agents.reflection` |
| 週六 10:00 | 套用週報裡通過驗證的策略調整 | `com.etf-agents.reflection-apply` |

交件期限是每個交易日 08:55。週五晚上不做決策，因為週一的決策繳交時間從週日 19:30 就開始，所以週一的決策由週日 21:00 與週一 07:00 負責。

同一時間只會有一個每日流程在跑；抓資料另有一把鎖，等不到鎖（預設 30 分鐘）就停止並通知。

## 每天你要做的事

1. **晚上 19:00 後、21:00 前：下載資料包。** 主辦方約 19:00 產生隔天的資料包（DataPack，內含現金、持股、前日委託結果），到參賽者後台「文件繳交」頁下載，放進 `var/datapack/`（檔名不限，不進 git）。沒放，21:00 對帳過不了，就不會有備案。
2. **早上 08:00 後：看結果。** 看門狗（08:00）會跳本機通知；通知寫 `dplan_pending` 代表 07:00 重做還沒跑完，請自己看最後幾行：

   ```bash
   tail -n 5 var/logs/daily_status.log
   ```

3. **08:55 前：上傳 D-Plan。** 檔案是 `artifacts/competition_d_plan/D-Plan_<team_id>_<交易日>.json`。上傳前可再用最新資料包檢查一次（不跑 Agent）：

   ```bash
   .venv/bin/python cli/dplan.py check-datapack --input artifacts/competition_d_plan/D-Plan_TEAM_11067_<交易日>.json
   ```

   這個指令會檢查同隊同日、賣出沒超過持股、每檔持股都有交代。系統只產生候選檔，**絕不自動上傳**。

4. **如果你先手動跑並上傳了，07:00 又重做成功**，主檔會換成新的一份，請重新上傳，否則帳本記的是新決策、交出去的是舊的，隔天對帳會對不上。

## 怎麼看排程結果

每次 `run` 結束（成功、失敗、略過都算）會跳一則 macOS 本機通知，並在 `var/logs/daily_status.log` 加一行：

```text
2026-11-03T07:31:02+08:00	run_type=morning	status=approved	trade_date=2026-11-03	run_id=daily-20261103-070000	detail=<D-Plan 路徑>
```

- `run_type`：`morning` 是 07:00 重做、`evening` 是 21:00 備案、`watchdog` 是 08:00 看門狗。
- `status` 的意思：

| status | 意思 | 你要做 |
| --- | --- | --- |
| `approved`、`no_trade` | 決策完成，D-Plan 可用 | 上傳 |
| `fallback` | 決策出問題，程式產生了「全部續抱、無委託」的備援 | 決定要不要交，見下方 |
| `rejected`、`blocked`、`failed` | 決策被風控拒絕、D-Plan 被擋、流程失敗，且沒有備援 | 看 `detail`，見下方 |
| `skipped` | 休市、同時段剛完成、或鎖被佔用 | 通常不用做 |

- `detail`：有 D-Plan 時是檔案路徑，否則是原因。
- 每次 run 的詳細狀態在 `artifacts/daily/<日期>/<run_id>/pipeline.json` 的 `final_status`。
- 腳本訊息一天一檔：`var/logs/daily-<YYYY-MM-DD>.log`（保留 30 天）；launchd 的完整輸出在 `var/logs/launchd.log`，不會自動輪替，太大時可自行清空。

沒跳通知時：到「系統設定 → 通知」找 Script Editor，確認允許通知、樣式不是「無」、專注模式沒擋。可手動測試，有跳出即正常，結束碼非 0 或印出 `not allowed` 就是權限問題：

```bash
osascript -e 'display notification "測試" with title "ETF Agent"'
```

### 看門狗的結果

`scripts/check_dplan_ready.py` 只讀檔，不跑 Agent、不上傳、不改任何檔案。狀態不是 `dplan_ready` 時發通知。平日排 08:00 一次：07:00 重做慢的日子會跑到 08:00 之後，此時昨晚的主檔還在，看門狗先探測每日流程鎖 `artifacts/daily/.lock` 是否仍被持有（鎖檔跑完不會刪，所以看的是有沒有人握著鎖，不是檔案在不在），仍被持有就回報 `dplan_pending`，不把昨晚那份報成可交；此時沒有下一次檢查，請自己看 `daily_status.log`。

| 狀態 | 意思 | 結束碼 |
| --- | --- | --- |
| `dplan_ready` | 主檔可交 | 0 |
| `dplan_fallback` | 主檔是程式產生的備援（全部續抱），要你決定是否繳交 | 0 |
| `dplan_pending` | 每日流程還在跑（鎖被持有）；`detail` 帶持有者 pid、開始時間與目前主檔是否存在。請自己盯 `daily_status.log`，08:55 前決定上傳哪一份 | 2 |
| `dplan_missing` | 沒有主檔；`detail` 帶最新 run 的狀態與原因 | 1 |
| `dplan_invalid` | 主檔讀不到、驗證失敗，或隊伍、日期不符 | 1 |
| `skipped` | 今天休市、已過 08:55，或日曆不可用 | 2 |

探測鎖的方式是非阻塞試取、取到立刻釋放，不留狀態；若此刻剛好有流程正要啟動（機率極低），那個流程會記一筆 `skipped` 而不會誤跑。

手動執行：

```bash
.venv/bin/python scripts/check_dplan_ready.py
.venv/bin/python scripts/check_dplan_ready.py --no-notify                      # 只印結果與寫狀態列
.venv/bin/python scripts/check_dplan_ready.py --at 2026-11-04T08:20:00+08:00   # 以指定時間檢查
```

## 出事了怎麼辦

### 早上重做失敗

07:00 重做沒產生新的 D-Plan（失敗、被擋、決策被拒，或跑完已過 08:55），程式會自動把帳本、昨晚的決策與 Snapshot 還原成重做前的狀態，通知會寫「已還原重跑前的備案決策」。重做建立的帳本狀態移到 `artifacts/ledger/<account_id>/rolled_back/`，不刪除。

1. **上傳原本的 D-Plan 主檔**，它和帳本對得上。`artifacts/latest/latest.md` 仍是重做那次的報告或失敗診斷，上傳時以 D-Plan 主檔為準。
2. 該 run 不能 `--resume`。還想重做的話，08:30 前執行 `./start.sh run --force`（整條重跑約 20 分鐘）。
3. 查原因：看前述日誌，以及該次 `pipeline.json` 的 `superseded_pending`、`rolled_back`；還原本身失敗會記 `rollback_error`，需人工檢查帳本。子 Agent 逾時、非零結束或輸出不是 JSON 時，程式會自動再試一次（間隔 30 秒，不拖過 08:55），`agent_calls` 裡帶 `transient_error` 的就是失敗的那次。

還原成功時不會產生備援 D-Plan，因為昨晚那份主檔仍有效。

另外：跑完時如果已過 08:55，不會產生 D-Plan，也不會取代已上傳的決策；沒有備案時，新決策移到封存區，帳本視為該時段沒交件。

### 決策被拒、D-Plan 被擋或流程失敗：備援 D-Plan

比賽辦法規定當日沒繳交決策報告書記警告一次，累計三次取消資格。為了不空手，下列三種情況程式會另外產生一份「持股全部續抱、無委託」的備援候選：

- 決策被風控拒絕；
- 決策完成，但 D-Plan 匯出被擋；
- 每日流程失敗（子 Agent 逾時、資料階段之後的任何例外）。

備援的性質：

- 內容全由程式產生，沒有 Agent 文字，不宣稱是模型產製，也沒評估當天個股風險。
- 只在前提都滿足時才產生：持股 20–30 檔、現金比低於 25%、單一持股上限（2330 為 25%，其餘 10%）、持股全在 150 檔內、帳本持股等於資料包持股、資料包對帳一致、此刻與 Snapshot 都早於 08:55。任一不滿足就不硬產，只通知，原因寫在 `pipeline.json` 的 `dplan.fallback`。
- 不會產生的情況：已還原昨晚備案（原主檔仍有效）；流程在資料包對帳前就失敗；已過 08:55；用了 `--skip-datapack`。
- **絕不自動上傳**，要不要交由你決定。

存放位置：

- 完整封存在 `artifacts/fallback_decisions/<交易日>/fallback-<UTC 時間>/`，不會被當成 Agent 決策。
- 另存一份 `artifacts/competition_d_plan/versions/<交易日>/D-Plan_<team_id>_<交易日>_<時間>_fallback.json`。
- 主檔只在「沒有主檔」或「主檔本身就是備援」時換成備援；已有正式決策的主檔一律保留，通知會寫「主檔仍是正式決策」。

### 決策已完成，只想重試 D-Plan

不需重跑 Agent：

```bash
docker compose run --rm agent python3 cli/dplan.py build-latest
```

匯出前請先確認 `config/competition_profile.json` 的隊伍、策略與模型資訊正確，且 `strategy_status` 為 `approved`。D-Plan 失敗不會否定已封存的決策，也不會阻止中文報告，原因寫在 `pipeline.json`。

D-Plan 的匯出器會用資料包的收盤價換算股數。持股的 Snapshot 收盤價與資料包不同（相對差 15% 以內），或 Snapshot 缺該日官方價但資料包有，只記警告在 `dplan.warnings`，不會拒絕。仍會拒絕的是：新買進的股票缺官方價、價差超過 15%（可能代號對錯或除權息未調整）、檔數超出 20–30 檔或 150 檔限制。

### 帳本與資料包對不上

流程在呼叫 Agent 前就會停止，並列出逐檔差異與可能原因（前日整批作廢、被排除代號、違規天數）。找不到本交易日資料包、主辦方結算尚未產生、持股股數不同、現金差超過容忍值，都會停止。

先不跑 Agent、只確認兩邊是否一致（一致回傳 0，不一致回傳 2 並印出現金差）：

```bash
.venv/bin/python cli/virtual_account.py reconcile --datapack var/datapack/<檔名>.json
```

處理順序：

1. 確認資料包是最新的。
2. 仍對不上，執行 `./start.sh run --rebase-to-datapack`：以資料包重建帳本（成本以結算收盤價代替），重抓資料建立新 Snapshot，對帳一致才跑 Agent。資料包須早於新 Snapshot 的截止時間、結算日須等於行情日；目前快照已有待成交決策時拒絕。

重建與決策前帳戶快照會先在暫存帳本完整產生並驗證，再持寫鎖發布；發布時若 latest 已被其他流程推進或 run ID 已存在，一律拒絕。缺持股行情、輸出準備失敗不會先推進正式 latest；一般發布例外會嘗試恢復原 latest、帳戶輸出及撤除本次未完成 runs，無法恢復時明確報錯並停止。這是例外處理與並行寫入保護，**不是跨檔案的斷電／程序強制終止交易保證**；異常中止後仍須先核對帳本與帳戶輸出，不可直接視為重建成功。
3. 現金差常在邊界，可用 `--datapack-cash-tolerance` 校準容忍值。

D-Plan 的 `target_weight` 以資料包的 `nav_tradable` 反推，帳本淨值的小差異不會讓股數差一張。

### `run` 顯示「虛擬帳本未建立本次 prepare 狀態」

上一次決策還在等指定成交日的官方價量，帳本無法結算。依缺的資料處理：

| 缺的資料 | 處理 |
| --- | --- |
| 上櫃（TPEx）價量 | 依下面三步補抓並結算 |
| 上市（TWSE）價量，或來源未提供 | 無法補抓，`run` 會停止並顯示缺口，不會改用 Yahoo 價格 |

```bash
# 1. 只補目前待結算決策所需的 TPEx 官方價量
.venv/bin/python scripts/repair_pending_tpex_close.py

# 2. 結算帳本，確認輸出 status=settled
.venv/bin/python cli/virtual_account.py settle

# 3. 再跑完整每日決策
./start.sh run
```

### 資料更新成功，但當天不能決策

資料更新成功不代表可決策。行情覆蓋、官方價量、交易狀態與日曆都要通過檢查，沒通過時 `run` 會停止並顯示缺口。先用 `./start.sh status` 看資料狀態。

### 撞到用量上限或需要重跑

見[參數說明](run-options.md)的「續跑與重跑」。

## 開新帳戶（比賽第一天）

測試期的帳本帶著測試持股，比賽第一天要開全新帳戶，舊帳戶保留不刪：

```bash
.venv/bin/python cli/virtual_account.py open-competition --new-account-id ai-cup-2026-final \
  --datapack var/datapack/<第一天的資料包>.json
```

- 會核對資料包確實是第一個交易日（全現金、0 檔、金額等於規則本金 10 億）才開帳，並把 `config/active_account.json` 改成新帳戶。之後 `start.sh`、每日流程、儀表板與 D-Plan 預設都用新帳戶。
- 第一天資料包還拿不到時，可先省略 `--datapack`（直接用規則本金）開帳，跑 `run` 時加 `--skip-datapack`，上傳前再用 `check-datapack` 以真正的資料包檢查。
- 要開在產生當天 Snapshot 之前；新帳戶的開帳時間必須早於決策截止時間。

目前使用中的帳戶是 `ai-cup-2026-final`（舊帳戶 `ai-cup-2026`）。

## 安裝排程

排程由 macOS launchd 執行，樣板都在 `scripts/launchd/`。安裝前需求：Docker Desktop 已啟動、`claude` CLI 已登入、`.venv` 已建立；Mac 在排程時間不能是睡著的（launchd 不會把睡著的 Mac 叫醒，也不補跑），請保持喚醒或關閉接電源時自動睡眠。

| 樣板 | 用途 | 必要 |
| --- | --- | --- |
| `daily` | 每日決策（21:00 備案、07:00 重做） | 是 |
| `data` | 20:00 資料更新 | 是 |
| `finmind-news` | 20:40 個股新聞 | 建議 |
| `overnight-capture` | 05:05 隔夜市場背景 | 建議 |
| `dplan-watchdog` | 08:00 看門狗 | 建議 |
| `backup` | 08:20、23:30 把帳本、決策、D-Plan、設定備份到私人 GitHub 倉庫 `etf-agents-backup` | 建議 |
| `reflection`、`reflection-apply` | 週五反思週報、週六套用 | 選用 |
| `taifex-mis-probe`、`taifex-night-probe` | 只觀察台指期夜盤何時可取得，不進決策 | 驗證完可移除 |

以看門狗為例（專案在 `/Users/apollo/stock/ETF_AGENTS`，家目錄 `/Users/apollo`），其他樣板把名稱換掉即可。已經裝過的話，先 `launchctl unload` 舊的再照下面重新產生與載入：

```bash
launchctl unload /Users/apollo/Library/LaunchAgents/com.etf-agents.dplan-watchdog.plist 2>/dev/null || true
mkdir -p /Users/apollo/stock/ETF_AGENTS/var/logs /Users/apollo/stock/ETF_AGENTS/artifacts/daily
sed -e 's#__PROJECT_ROOT__#/Users/apollo/stock/ETF_AGENTS#g' -e 's#__HOME__#/Users/apollo#g' \
  /Users/apollo/stock/ETF_AGENTS/scripts/launchd/com.etf-agents.dplan-watchdog.plist.template \
  > /Users/apollo/Library/LaunchAgents/com.etf-agents.dplan-watchdog.plist
plutil -lint /Users/apollo/Library/LaunchAgents/com.etf-agents.dplan-watchdog.plist
launchctl load /Users/apollo/Library/LaunchAgents/com.etf-agents.dplan-watchdog.plist
launchctl list | grep com.etf-agents.dplan-watchdog        # 已載入會列出
launchctl start com.etf-agents.dplan-watchdog               # 立刻跑一次驗證，不等排程時間
tail -n 3 /Users/apollo/stock/ETF_AGENTS/var/logs/daily_status.log
```

改了 plist 要重新載入：先 `launchctl unload` 再 `launchctl load`。卸載：`launchctl unload /Users/apollo/Library/LaunchAgents/com.etf-agents.dplan-watchdog.plist`。

各排程的日誌位置：`daily` 在 `var/logs/launchd.log`；`data`、`finmind-news` 在 `artifacts/daily/`；看門狗、反思與隔夜背景在 `var/logs/`。

## 每週的人工事項：策略反思放行

週五的反思週報會提出策略調整提案，週六 10:00 自動套用「降低風險方向」的提案。週報標 `needs_human` 的提案（加風險或方向不明）要你看過後手動放行，編號必須是最近一份待套用的反思：

```bash
.venv/bin/python cli/strategy_reflection.py apply-pending --allow-risk-increase <reflection_id>
```

放行後仍要通過驗證、影子回放與時段保護（20:50 到隔天 09:00 不套用）。想還原最近一次套用：`cli/strategy_reflection.py rollback`（經驗教訓用 `lessons-rollback`），看歷史用 `history`。

## 評測與回放（選用）

這些不影響每日決策，用來檢查 Agent 的穩定度與換策略的影響。所有回放與評測重跑都只重算已封存的 Agent 意圖，不是對 LLM 判斷的前瞻驗證，報告首段會固定提醒；只有比賽期間的即時紀錄才算證據。

| 想知道 | 指令 | 花用量 |
| --- | --- | --- |
| 資料有沒有漏抓 | `.venv/bin/python cli/agent_eval.py coverage` | 否 |
| 建立評測集（過去封存的決策當考卷） | `agent_eval.py suite --name 名稱` | 否 |
| 重跑考卷，看一致度與穩定度 | `agent_eval.py run --suite 名稱 --repeats 2 --yes`（`--stop-after analysts` 只跑分析師，較省） | 是 |
| 重跑中途失敗要續跑 | `agent_eval.py run --suite 名稱 --repeats 同原值 --resume eval-編號 --yes` | 是 |
| 更新評測報告 | `agent_eval.py report --eval-id 編號` | 否 |
| 看好的股票是否贏過交易池平均 | `./start.sh backtest`（可加 `--start 起日 --end 迄日 --benchmark 0050.TW`），`agent_eval.py grid-verify --grid-id 編號` 驗證 | 否 |
| 換一份策略樣板會怎樣 | `.venv/bin/python cli/policy_replay.py run --policy config/decision_policy.json --start 起日 --end 迄日 [--compare-policy 另一份]`，`verify --replay-id 編號` 驗證 | 否 |

解讀重點：

- 評測答案是之後真正的官方成交均價；價格未出來標「待答案」，累積到 10 個完整決策日才給命中率。
- 回測格子同時列兩種超額報酬（贏過交易池平均、贏過 0050.TW），不合成單一分數；完整決策日少於 10 天（`calibration.MIN_DECISION_DAYS`）只給天數與筆數。
- 換策略回放用下一交易日官方均價成交，成交日少於 20 天沒有統計意義；缺日、缺價、前視價格一律停止或標示。
- 結果分別在 `artifacts/evaluation/` 底下的 `grids/`、`replays/`；每份報告旁有一頁自包含 HTML，只排版、不重算，可隨時重產。

## 狀態備份

`scripts/backup_state.sh` 把 `artifacts/ledger`、`artifacts/decisions`、`artifacts/competition_d_plan`、`artifacts/fallback_decisions` 與 `config/`（不含 `.env*`）複製到本機 `~/stock/etf-agents-backup`，再推到私人 GitHub 倉庫 `etf-agents-backup`。沒有變更就不提交。`var/` 資料庫與 `artifacts/daily` 暫存不在備份內（可重抓、可重跑）。排程樣板是 `scripts/launchd/com.etf-agents.backup.plist.template`，每天 08:20 與 23:30 執行，結果寫在 `var/logs/backup.log`：

```bash
tail -n 3 var/logs/backup.log
```

備份失敗目前只留在這份 log，沒有通知。手動備份直接執行 `scripts/backup_state.sh`。

## 檔案位置

| 路徑 | 內容 |
| --- | --- |
| `data/official_universe.csv` | 150 檔交易池 |
| `var/etf_agent.db` | 行情、公司文件、來源原文、時間與版本 |
| `var/datapack/` | 你下載的主辦方資料包（不進 git）；每次 run 的對帳副本在 `artifacts/daily/<日期>/<run_id>/datapack.json` |
| `var/logs/daily_status.log` | 每次排程與看門狗的一行狀態（不進 git） |
| `config/active_account.json` | 使用中的帳戶 |
| `artifacts/latest/latest.md` | 最新中文報告或失敗診斷 |
| `artifacts/daily/<日期>/<run_id>/pipeline.json` | 每日執行狀態與阻擋原因；run 目錄一律帶台北時間 |
| `artifacts/decisions/<日期>/<run_id>/` | 不可變決策封存 |
| `artifacts/ledger/` | 虛擬帳本與帳戶快照 |
| `artifacts/competition_d_plan/D-Plan_<team_id>_<日期>.json` | 可上傳的 D-Plan 候選，同交易日永遠是最新一份 |
| `artifacts/competition_d_plan/versions/<交易日>/` | 每份 D-Plan 的帶時間副本，不刪不覆蓋 |
| `artifacts/fallback_decisions/<交易日>/` | 備援 D-Plan 封存 |

## 附錄：名稱對照

| 白話說明 | 程式名稱 |
| --- | --- |
| 備援的三種原因 | `pipeline.json` 的 `dplan.reason`：`decision_rejected`、`dplan_blocked`、`pipeline_failed` |
| 備援狀態 | `dplan.status=fallback`，`final_status=fallback` |
| 備援沒產生的原因 | `dplan.fallback.status=not_generated` |
| 備援的模型標記 | `model_version=etf-agents-rule-fallback-v1-no-llm` |
| D-Plan 被擋 | `dplan.status=blocked` |
| 資料包的可交易淨值 | `nav_tradable` |
| 已還原重做前狀態 | `pipeline.json` 的 `rolled_back` |
| 回放報告的證據等級 | `evidence_grade: replay_reference_only` |
| 評測的決策日樣本門檻 | `calibration.MIN_DECISION_DAYS`（10） |
