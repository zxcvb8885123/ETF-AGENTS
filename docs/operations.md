# 日常操作

每日排程、D-Plan、主辦方資料包、開新帳戶、Agent 評測與常見問題。指令總表見[根目錄 README](../README.md#常用指令)。

## D-Plan 候選

正式產生前，請人工確認 `config/competition_profile.json` 的隊伍、策略與實際模型資訊，並將 `strategy_status` 設為 `approved`。完整 `run` 會自動嘗試匯出；D-Plan 失敗不會否定已封存決策或阻止中文報告，原因會寫入同一次 `pipeline.json`。

若決策與報告已完成，只想重試最新候選，不需重跑 Agent：

```bash
docker compose run --rm agent python3 cli/dplan.py build-latest
```

輸出只是供人工檢查的候選檔，不會自動送件，也不代表已通過主辦方平台驗證。

### DataPack 與 Snapshot 收盤價不同時

主辦方換算股數用的是資料包／交易所的前日收盤價。匯出器對「持股」一律以已對帳 DataPack 的 `close_prices` 換算 `target_weight`：權重與股數用同一個價格互推，所以不論取哪個價，推導出的委託都等於已核准 Decision 的 orders（`proposed == derived` 仍是硬性檢查）。因此下列情況只記警告、不再拒絕，警告寫在 `pipeline.json` 的 `dplan.warnings`：

- 持股的 Snapshot 官方收盤價與 DataPack 不同（相對差 ≤ 15%）。
- 持股的 Snapshot 缺同日官方價，但 DataPack 有該檔收盤價。

仍維持 fail-closed：新買進（DataPack 沒有該檔價格）缺 Snapshot 官方價、相對差超過 15%（可能是代號對應錯誤或除權息未調整）、150 檔與 20–30 檔限制、`proposed != derived`。

## 備援 D-Plan（決策被拒、D-Plan 被擋或流程失敗時）

比賽辦法六(十四)(1)：當日未繳交 Agent 決策報告書記警告一次，累計三次取消資格。為了不因程式或 Agent 問題空手，每日 `run` 在下列情況會由程式另外產生一份「持股全部續抱、無委託」的備援 D-Plan **候選**：

- 決策為 `rejected`（風控拒絕）；
- 決策完成但 D-Plan 匯出器拒絕輸出（`dplan.status=blocked`）；
- 每日流程失敗（含子 Agent 逾時、錯誤、資料階段之後的任何例外）。

備援的性質：

- 內容由程式確定性產生，不含任何 Agent 文字；`agent_metadata.model_provider=other`、`model_version=etf-agents-rule-fallback-v1-no-llm`，不宣稱是模型產製。
- 走不可變封存：`artifacts/fallback_decisions/<交易日>/fallback-<UTC 時間>/`，含 `decision.json`（`fallback=true`、`status=no_trade`、原因代碼與細節）、`dplan.json`、`account_snapshot.json`、`preconditions.json` 與 `manifest.json`；**不放進** `artifacts/decisions/`，帳本與前次決策背景都不會把它當成 Agent 決策。
- 必須通過同一個 `DPlanValidator`，並先通過前提檢查：目前持股 20–30 檔、DataPack 現金比 <25%、單一持股上限（2330 為 25%，其餘 10%，以 DataPack 收盤價與 `nav_tradable` 計）、持股全在 150 檔內、帳本持股等於 DataPack 結算持股、DataPack 對帳 `matched`、Snapshot cutoff 與此刻都早於 08:55。**任一不滿足就不產生**，`pipeline.json` 的 `dplan.fallback.status=not_generated` 列出原因並發本機通知。
- **絕不自動上傳或送件**；要不要交由人決定。沒有經過本日多空研究與風險審查，個股風險訊號未被評估。

不會產生備援的情況：早上重做失敗但已還原晚上的備案（原主檔仍有效）；流程在 DataPack 對帳前就失敗（沒有可信的持股依據）；已過 08:55；`--skip-datapack`。

檔案與主檔規則：

- 每份備援另存 `artifacts/competition_d_plan/versions/<交易日>/D-Plan_<team_id>_<交易日>_<時間>_fallback.json`。
- 主檔 `D-Plan_<team_id>_<交易日>.json` 只在「沒有主檔」或「主檔本身就是備援」時換成備援；已有正式決策的主檔一律保留，備援只在 `versions/`，通知會寫「主檔仍是正式決策」。
- `pipeline.json`：`dplan.status=fallback`，`dplan.output` 是檔案路徑，`dplan.run_id`／`dplan.run_dir` 是封存位置，`dplan.reason` 是原因代碼（`decision_rejected`／`dplan_blocked`／`pipeline_failed`），`dplan.original` 保留原本的 blocked 紀錄；`final_status` 為 `fallback`。

上傳前可用資料包再檢查一次：`.venv/bin/python cli/dplan.py check-datapack --input artifacts/competition_d_plan/D-Plan_TEAM_11067_<交易日>.json`。

## 排程完成通知與狀態紀錄

每次 `run` 結束（成功、失敗、略過都算）會做兩件事：

1. 本機通知（macOS `display notification`，不會跳出要按的對話框）。通知失敗（osascript 不存在、逾時 15 秒、權限被拒）只寫入日誌，不影響 run 的結果與結束碼。
2. 在 `var/logs/daily_status.log` 附加一行 TSV（不進 git）：

   ```text
   2026-11-03T07:31:02+08:00	run_type=morning	status=approved	trade_date=2026-11-03	run_id=daily-20261103-070000	detail=/Users/apollo/stock/ETF_AGENTS/artifacts/competition_d_plan/D-Plan_TEAM_11067_2026-11-03.json
   ```

   - `run_type`：中午前為 `morning`（07:00 重做當日時段），其餘為 `evening`（21:00 做隔天備案）；看門狗寫 `watchdog`。
   - `status`：`approved`／`no_trade`（決策完成且 D-Plan ready）、`fallback`（備援已產生）、`rejected`（決策被拒且沒有備援）、`blocked`（決策完成但 D-Plan 被擋且沒有備援）、`failed`（流程失敗且沒有備援）、`skipped`（休市、同時段剛完成、鎖被佔用）。
   - `detail`：有 D-Plan 時是路徑，否則是未產生的原因（含備援未產生的原因）。
   - 同一狀態也寫在該次 `pipeline.json` 的 `final_status`。

macOS 通知權限：launchd 跑的 osascript 以「Script Editor」（或「osascript」）名義送通知。第一次會跳出允許詢問；若沒看到通知，到「系統設定 → 通知」找 Script Editor，確認「允許通知」開著、樣式不是「無」，並檢查「專注模式」沒有擋掉。手動驗證（不跑決策）：

```bash
osascript -e 'display notification "測試" with title "ETF Agent"'
```

有跳出通知即可；若命令結束碼非 0 或印出 `not allowed`，就是權限問題。

## D-Plan 看門狗（08:20）

`scripts/check_dplan_ready.py` 在交件期限前檢查今天該交的 D-Plan：主檔 `artifacts/competition_d_plan/D-Plan_<team_id>_<今天>.json` 是否存在、通過本地驗證、`team_id`／`trade_date` 正確，並讀該交易日最新 run 的 `pipeline.json` `dplan.status`。只讀檔，不跑 Agent、不上傳、不改任何檔案。結果寫一行到 `var/logs/daily_status.log`（`run_type=watchdog`），狀態不是 `dplan_ready` 時發本機通知：

| 狀態 | 意義 | 結束碼 |
| --- | --- | --- |
| `dplan_ready` | 主檔可交；若最新 run 沒產生新 D-Plan（例如已還原的備案），`detail` 會提醒上傳前確認 | 0 |
| `dplan_fallback` | 主檔是程式產生的備援（全部續抱），要人決定是否繳交 | 0 |
| `dplan_missing` | 沒有主檔；`detail` 帶最新 run 的狀態與原因 | 1 |
| `dplan_invalid` | 主檔讀不到、驗證失敗或隊伍／日期不符 | 1 |
| `skipped` | 今天休市、已過 08:55 或日曆不可用（不猜） | 2 |

手動執行：

```bash
.venv/bin/python scripts/check_dplan_ready.py
.venv/bin/python scripts/check_dplan_ready.py --no-notify            # 只印結果與寫狀態列
.venv/bin/python scripts/check_dplan_ready.py --at 2026-11-04T08:20:00+08:00   # 以指定時間檢查
```

用 launchd 在週一到週五 08:20 排程（樣板 `scripts/launchd/com.etf-agents.dplan-watchdog.plist.template`；以下路徑是本機實際路徑，專案在 `/Users/apollo/stock/ETF_AGENTS`、家目錄 `/Users/apollo`）：

```bash
mkdir -p /Users/apollo/stock/ETF_AGENTS/var/logs
sed -e 's#__PROJECT_ROOT__#/Users/apollo/stock/ETF_AGENTS#g' -e 's#__HOME__#/Users/apollo#g' \
  /Users/apollo/stock/ETF_AGENTS/scripts/launchd/com.etf-agents.dplan-watchdog.plist.template \
  > /Users/apollo/Library/LaunchAgents/com.etf-agents.dplan-watchdog.plist
plutil -lint /Users/apollo/Library/LaunchAgents/com.etf-agents.dplan-watchdog.plist
launchctl load /Users/apollo/Library/LaunchAgents/com.etf-agents.dplan-watchdog.plist
launchctl list | grep com.etf-agents.dplan-watchdog        # 已載入會列出
launchctl start com.etf-agents.dplan-watchdog               # 立刻跑一次驗證（不等 08:20）
tail -n 3 /Users/apollo/stock/ETF_AGENTS/var/logs/daily_status.log
```

產生出來的 plist 內容（已代入路徑）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.etf-agents.dplan-watchdog</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/apollo/stock/ETF_AGENTS/.venv/bin/python</string>
    <string>/Users/apollo/stock/ETF_AGENTS/scripts/check_dplan_ready.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/apollo/stock/ETF_AGENTS</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/Users/apollo/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>TZ</key>
    <string>Asia/Taipei</string>
  </dict>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>20</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>20</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>20</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>20</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>20</integer></dict>
  </array>
  <key>StandardOutPath</key>
  <string>/Users/apollo/stock/ETF_AGENTS/var/logs/dplan-watchdog-launchd.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/apollo/stock/ETF_AGENTS/var/logs/dplan-watchdog-launchd.log</string>
</dict>
</plist>
```

改了 plist 要重新載入：`launchctl unload ... && launchctl load ...`。卸載：`launchctl unload /Users/apollo/Library/LaunchAgents/com.etf-agents.dplan-watchdog.plist`。Mac 在 08:20 休眠時 launchd 不會補跑，請和 07:00 的決策排程一樣保持喚醒。

## 每日排程：晚上備案、早上重做

排程（`scripts/launchd/`）週一到週五 07:00、週一到週四 21:00 各跑一次 `run`；週五晚上不跑（週一的決策繳交時間從前一日週日 19:30 才開始），改由週日 21:00 做週一的決策（看得到週末新聞，也讀到週六套用的新策略），週一 07:00 再重做（後做的取代先做的）：

- **21:00 備案**：用當天收盤資料做下一個交易日的決策與 D-Plan。早上出事時，手上仍有一份能交。主辦方約 19:00 才產生隔天的 DataPack，**21:00 前要先下載到 `var/datapack/`**，否則對帳不過、不會有備案。
- **07:00 重做**：在 08:55 交件期限前，用晚上到早上的新事件、新聞與情緒重做，取代晚上那份。
- **互斥**：同一時間只有一個每日流程（`artifacts/daily/.lock`）；抓資料另有 `artifacts/daily/.data.lock`，20:00 資料、20:40 新聞與決策自己的資料階段會輪流執行，等不到鎖（預設 30 分鐘）就停止並通知。鎖被佔用而略過、或取鎖失敗時都會跳通知。
- **何時略過**：同一目標時段在 1 小時內已有完成的 run 才略過（例如 06:30 手動跑完，07:00 不跑）。半夜手動跑的不算，07:00 照常重做。
- **每次都保留**：run 目錄一律帶台北時間（`daily-<YYYYMMDD>-<HHMMSS>`）。每份 D-Plan 另存在 `artifacts/competition_d_plan/versions/<交易日>/`，檔名結尾是決策時間（台北）；主檔 `D-Plan_<team_id>_<交易日>.json` 永遠是最新一份。
- **跑完已過 08:55 不換主檔**：決策跑完時若已過目標時段的交件期限，不產生 D-Plan、不取代已上傳的決策；沒有備案時新決策移到封存區，帳本視為該時段沒交件。
- **重做失敗時自動還原**：早上重做沒產生新 D-Plan（失敗、被擋、決策被拒或跑完已過 08:55），程式把帳本、晚上的決策與 Snapshot 還原成重做前，重做建立的帳本狀態移到 `artifacts/ledger/<account_id>/rolled_back/`（不刪除）。通知會寫「已還原重跑前的備案決策」。還原成功時不產生備援 D-Plan（晚上那份主檔仍有效）；沒有備案可還原時才走上方的備援 D-Plan。

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

- **準不準**：考卷的答案是之後真正的官方成交均價。看好的有沒有贏交易池平均、看壞的有沒有輸；價格還沒出來標「待答案」，累積到 10 個完整決策日（`calibration.MIN_DECISION_DAYS`）才給命中率。之後再跑 `report --eval-id 編號` 就會更新。評測重跑、回放與格子的報告首段都固定提醒「僅供參考：回放使用已封存的 Agent 意圖，不構成對 LLM 判斷的前瞻驗證；只有比賽期間的即時紀錄才算證據」（JSON `evidence_grade: "replay_reference_only"`）——模型對舊日期可能有訓練資料記憶。
- **斷點續跑**：`run` 中途失敗（逾時、Agent 錯誤）後用 `run --suite 名稱 --repeats 同原值 --resume eval-編號 --yes` 續跑：只重跑沒有完成的卷次，卷次目錄內先前通過目前 Validator 的原始 Agent 輸出直接沿用、不再呼叫 Agent；評測集、repeats、stop_after、每卷輸入雜湊或評測用策略樣板任一不同就停止。不加 `--yes` 只列出未完成卷次與估算上限。
- **回測格子（不花用量）**：`cli/agent_eval.py grid --start 起日 --end 迄日 [--benchmark 0050.TW]` 讀已封存 Decision run，每筆決策（run × symbol）一列，按信心等級、交易意圖與多空強度分組，**並列**兩種超額報酬：贏過交易池平均（主）與贏過基準 ETF 0050.TW（附），不合成單一分數。基準在 T 或 T+h 缺官方列（含抓取時間晚於 `--as-of`）時該日基準欄 `unavailable`、窗口內除權息時 `ex_rights`，池 alpha 照算、不補值；完整決策日少於 5 天只給天數與筆數；超額剛好 0 不算贏也不算輸。結果在 `artifacts/evaluation/grids/<grid_id>/grid.json`，`grid-verify --grid-id 編號` 重算驗證。只收已封存 run（`provenance_class=archived`），不往更早歷史重建。
- **策略反思的人工放行**：週六 `cli/strategy_reflection.py apply-pending` 只自動套用降風險方向的旋鈕提案；週報標 `needs_human` 的提案（加風險或方向不明）要人看過後執行 `.venv/bin/python cli/strategy_reflection.py apply-pending --allow-risk-increase <reflection_id>`（id 必須是最近一份待套用的反思；`auto_apply=false` 的人工模式也有效，只套旋鈕）或 `apply --reflection-id <id> --allow-risk-increase`。兩者仍要通過驗證、影子回放（對等權基準的超額、兩段子區間）與時段保護。
- **一頁 HTML**：回放、格子與評測報告旁各有一份自包含 HTML（inline SVG、無 script、無外連、有 CSP），只排版 JSON、不重算，不進 manifest，可隨時由 JSON 重新產生；頁尾印 JSON 的 content_sha256 與驗證指令。
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
| `artifacts/fallback_decisions/<交易日>/` | 程式產生的備援 D-Plan 不可變封存（`fallback=true`）；不是 Agent 決策，不會自動上傳 |
| `var/logs/daily_status.log` | 每次排程 run 與看門狗的一行狀態紀錄（不進 git） |

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
