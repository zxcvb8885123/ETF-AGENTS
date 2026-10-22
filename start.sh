#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_DIR"

# 關閉 Docker 的「What's next」提示。
export DOCKER_CLI_HINTS=false

# 每個資料階段結束時印出耗時，方便找出最慢的一步。
STAGE_AT=$(date +%s)
stage_time() {
  STAGE_NOW=$(date +%s)
  echo "  耗時 $((STAGE_NOW - STAGE_AT)) 秒：$1"
  STAGE_AT=$STAGE_NOW
}

# macOS ChatGPT.app 內附的 Codex CLI；非互動 shell／launchd 不會載入 ~/.zshrc。
if [ -x "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex" ]; then
  PATH="/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin:$PATH"
  export PATH
fi
if [ -x "$HOME/.opencode/bin/opencode" ]; then
  PATH="$HOME/.opencode/bin:$PATH"
  export PATH
fi

MODE=${1:-dashboard}
if [ "$#" -gt 0 ]; then
  shift
fi

usage() {
  echo "用法：./start.sh [dashboard|auto|run|status|daily|check|data|official|all]"
  echo "  dashboard 看帳戶與績效（預設；首次建立虛擬帳戶）"
  echo "  auto      更新行情與研究資料，不建立快照或推進帳本"
  echo "  run       完整每日分析與決策，可接 --agent-cli codex 等原有參數"
  echo "  status    查看既有資料狀態，可接 --database 路徑"
  echo "  daily     只更新資料、建立快照與準備帳本，不執行決策 Agent"
  echo "  check     檢查 Docker 環境與執行測試"
  echo "  data      auto 的相容別名"
  echo "  official  只更新官方交易池行情；空白時停止"
  echo "  all       開發模式：更新全部可解析 TWSE 最新行情"
  echo "  --help    顯示此說明；run --help／status --help 查看完整參數"
}

case "$MODE" in
  -h|--help|help)
    usage
    exit 0
    ;;
  run|status)
    if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
      echo "找不到專案 Python 環境，請先執行 python3 -m venv .venv，再執行 .venv/bin/python -m pip install -r requirements.txt。" >&2
      exit 1
    fi
    if [ "$MODE" = "run" ]; then
      exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/run_daily_pipeline.py" "$@"
    fi
    exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/data_status.py" "$@"
    ;;
  auto|official|all|check|data|daily|dashboard)
    if [ "$#" -gt 0 ]; then
      echo "此模式不接受額外參數：${MODE}；請執行 ./start.sh --help 查看用法。" >&2
      exit 2
    fi
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

if ! command -v docker >/dev/null 2>&1; then
  echo "找不到 Docker，請先安裝並啟動 Docker Desktop。" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker 尚未啟動，請先開啟 Docker Desktop。" >&2
  exit 1
fi

HOST_UID=$(id -u)
HOST_GID=$(id -g)
export HOST_UID HOST_GID

DAILY_RUN=0
DATA_ONLY=0
if [ "$MODE" = "data" ] || [ "$MODE" = "auto" ]; then
  DAILY_RUN=1
  DATA_ONLY=1
  MODE=auto
elif [ "$MODE" = "daily" ]; then
  DAILY_RUN=1
  MODE=official
fi

if command -v shasum >/dev/null 2>&1; then
  BUILD_INPUT_SHA=$(shasum -a 256 Dockerfile requirements.txt | shasum -a 256 | awk '{print $1}')
else
  BUILD_INPUT_SHA=$(sha256sum Dockerfile requirements.txt | sha256sum | awk '{print $1}')
fi
IMAGE_INPUT_SHA=$(docker image inspect etf-agent:local \
  --format '{{ index .Config.Labels "org.etf-agent.build-input-sha" }}' 2>/dev/null || true)
if [ "${FORCE_DOCKER_BUILD:-0}" = "1" ] || [ "$IMAGE_INPUT_SHA" != "$BUILD_INPUT_SHA" ]; then
  echo "[1/4] 建立 Docker 映像（依賴或 Dockerfile 已變更）"
  docker compose build --build-arg "BUILD_INPUT_SHA=$BUILD_INPUT_SHA"
else
  echo "[1/4] Docker 映像已是目前依賴版本，略過建置"
fi

if [ "$MODE" = "dashboard" ]; then
  ACCOUNT_ID=${ACCOUNT_ID:-ai-cup-2026}
  export ACCOUNT_ID
  mkdir -p artifacts/ledger artifacts/data
  echo "[2/3] 驗證既有帳戶或建立新帳戶：$ACCOUNT_ID"
  docker compose run --rm agent python3 cli/virtual_account.py \
    --account-id "$ACCOUNT_ID" ensure-init
  echo "[3/3] 啟動績效儀表板：http://127.0.0.1:${DASHBOARD_PORT:-8000}"
  exec docker compose up dashboard
fi

echo "[2/4] 檢查專案設定"
docker compose run --rm agent

if [ "$MODE" = "check" ]; then
  echo "[3/4] 執行測試"
  docker compose run --rm agent python3 -m pytest
  echo "[4/4] 檢查完成"
  exit 0
fi

echo "[3/4] 初始化資料庫並抓取行情"
docker compose run --rm agent python3 scripts/init_db.py

if [ "$DAILY_RUN" -eq 1 ] && [ "$DATA_ONLY" -eq 0 ]; then
  echo "[3a/4] 驗證官方來源與交易池"
  docker compose run --rm agent python3 scripts/probe_data_sources.py
  stage_time "來源探測"
fi

if [ "$MODE" = "auto" ]; then
  UNIVERSE_ROWS=$(awk -F, 'NR > 1 && $1 != "" { count++ } END { print count + 0 }' data/official_universe.csv)
  if [ "$UNIVERSE_ROWS" -gt 0 ]; then
    MODE=official
    echo "偵測到官方交易池 $UNIVERSE_ROWS 檔，使用正式模式。"
  else
    MODE=all
    DAILY_RUN=0
    echo "官方交易池尚未填入，使用開發模式抓取全部 TWSE 最新行情。"
  fi
fi

# 收盤行情是固定時間定案的資料：資料庫已有收盤後抓到的完整當日行情就不重抓。
# 重大訊息、月營收、財報、新聞等發布時間不固定，不受此影響，下面照常收集。FORCE_FETCH=1 可強制重抓行情。
prices_current() {
  [ "${FORCE_FETCH:-0}" != "1" ] || return 1
  docker compose run --rm agent python3 scripts/prices_current.py --step "$1" ${HISTORY_END:+--date "$HISTORY_END"}
}

if [ "$MODE" = "official" ]; then
  if ! prices_current official_latest; then
    docker compose run --rm agent python3 scripts/collect_latest_prices.py
  fi
  if prices_current yahoo; then
    YAHOO_EXIT=0
  elif [ "$DATA_ONLY" -eq 1 ]; then
    set +e
    docker compose run --rm agent python3 scripts/collect_history.py ${HISTORY_END:+--end "$HISTORY_END"}
    YAHOO_EXIT=$?
    set -e
    if [ "$YAHOO_EXIT" -ne 0 ]; then
      echo "Yahoo 當日日線未完整（exit ${YAHOO_EXIT}）；仍繼續抓其他資料，資料排程最後回報非零狀態。" >&2
    fi
  else
    docker compose run --rm agent python3 scripts/collect_history.py ${HISTORY_END:+--end "$HISTORY_END"}
  fi
  if [ "$DAILY_RUN" -eq 1 ] && [ "$DATA_ONLY" -eq 0 ] && ! prices_current official_history; then
    # 正式帳本需當日官方成交金額／股數；全市場端點延遲時只在決策流程補個股原價。
    set +e
    docker compose run --rm agent python3 scripts/collect_official_history.py >/dev/null
    OFFICIAL_HISTORY_EXIT=$?
    set -e
    if [ "$OFFICIAL_HISTORY_EXIT" -eq 1 ]; then
      echo "帳本官方個股價量補抓失敗；結算將等待可用行情。詳見 artifacts/data/official_history/latest.json" >&2
    fi
  fi
else
  docker compose run --rm agent python3 scripts/collect_twse.py --all-listed
fi
stage_time "行情"

if [ "$DAILY_RUN" -eq 1 ]; then
  DATA_STATUS=0
  echo "[3b/4] 收集官方月營收與重大訊息"
  docker compose run --rm agent python3 cli/data_agent.py collect
  stage_time "月營收與重大訊息"
  echo "[3b+/4] 收集最近已到期季度的官方財報"
  # 覆蓋不完整（exit 2）屬預期，基本面分析會標示缺口；只有執行失敗才提示，但不中止每日流程。
  set +e
  docker compose run --rm agent python3 scripts/collect_financial_statements.py \
    --latest-due --report artifacts/data/financial_statements_latest.json >/dev/null
  FINANCIAL_EXIT=$?
  set -e
  if [ "$FINANCIAL_EXIT" -eq 1 ]; then
    echo "財報收集失敗；本次基本面分析將只使用月營收。詳見 artifacts/data/financial_statements_latest.json" >&2
  fi
  stage_time "官方財報"
  echo "[3b+/4] 更新官方公司基本資料與產業分類候選"
  set +e
  docker compose run --rm agent python3 scripts/collect_sector_classification.py \
    --output artifacts/data/sector_classification_latest.json >/dev/null
  SECTOR_EXIT=$?
  set -e
  if [ "$SECTOR_EXIT" -ne 0 ]; then
    echo "公司資料擷取未完整（exit ${SECTOR_EXIT}）；正式決策仍沿用現有已驗證分類。" >&2
    DATA_STATUS=2
  fi
  stage_time "產業分類"
  if [ -z "${FINMIND_TOKEN:-}" ] && [ -r .env ]; then
    FINMIND_TOKEN=$(sed -n 's/^FINMIND_TOKEN=//p' .env | sed -n '1p')
    export FINMIND_TOKEN
  fi
  if [ -z "${FINMIND_TOKEN:-}" ] && command -v security >/dev/null 2>&1; then
    FINMIND_TOKEN=$(security find-generic-password -s etf-agent-finmind -w 2>/dev/null || true)
    export FINMIND_TOKEN
  fi
  if [ -n "${FINMIND_TOKEN:-}" ]; then
    FINMIND_END=$(TZ=Asia/Taipei date '+%Y-%m-%d')
    # 財報只在公布區間內變動：首次補 FINMIND_YEARS 年（預設 3，可設 2～5）歷史，之後只在區間內每天更新一次。
    # FORCE_FETCH=1 可強制更新。
    for DATASET in TaiwanStockFinancialStatements TaiwanStockBalanceSheet TaiwanStockCashFlowsStatement; do
      FINMIND_FORCE=""
      [ "${FORCE_FETCH:-0}" != "1" ] || FINMIND_FORCE="--force"
      # 排程狀態讀不到時略過這一組並提醒，不讓補充資料的問題中斷每日流程。
      FINMIND_PLAN=$(docker compose run --rm -T agent python3 scripts/finmind_schedule.py plan --dataset "$DATASET" $FINMIND_FORCE) || {
        FINMIND_PLAN="skip||排程狀態讀取失敗"
        DATA_STATUS=2
      }
      FINMIND_ACTION=${FINMIND_PLAN%%|*}
      FINMIND_REST=${FINMIND_PLAN#*|}
      FINMIND_START=${FINMIND_REST%%|*}
      FINMIND_REASON=${FINMIND_REST#*|}
      if [ "$FINMIND_ACTION" = "skip" ]; then
        echo "[3b+/4] FinMind ${DATASET}：略過（${FINMIND_REASON}）"
        continue
      fi
      echo "[3b+/4] FinMind ${DATASET}：逐股更新財報候選（${FINMIND_REASON}，自 ${FINMIND_START}）"
      set +e
      docker compose run --rm -e FINMIND_TOKEN agent python3 scripts/collect_supplemental_sources.py \
        finmind --dataset "$DATASET" --universe data/official_universe.csv \
        --start "$FINMIND_START" --end "$FINMIND_END"
      FINMIND_EXIT=$?
      set -e
      # exit 2 只是部分公司尚未公布（沒有資料列），整批仍算完成；exit 1 才是執行失敗。
      if [ "$FINMIND_EXIT" -eq 0 ] || [ "$FINMIND_EXIT" -eq 2 ]; then
        docker compose run --rm agent python3 scripts/finmind_schedule.py done --dataset "$DATASET" --start "$FINMIND_START"
      fi
      if [ "$FINMIND_EXIT" -ne 0 ]; then
        echo "FinMind ${DATASET} 候選擷取未完整（exit ${FINMIND_EXIT}）；不影響正式 Snapshot。" >&2
        DATA_STATUS=2
      fi
      stage_time "FinMind ${DATASET}"
      if [ "$FINMIND_EXIT" -eq 1 ]; then
        echo "FinMind 發生錯誤（例如 HTTP 402 額度用完），其餘資料集本次不再嘗試；下次執行會接續補抓。" >&2
        break
      fi
    done
  else
    echo "未設定 FINMIND_TOKEN；略過 FinMind 候選擷取。" >&2
    DATA_STATUS=2
  fi
  # 必須在 cutoff 之前抓，決策才看得到；失敗只讓市場情緒降級，不中止每日流程。
  echo "[3b+/4] 抓取台股恐懼貪婪指標來源（VIX、Put/Call 比、融資）"
  set +e
  docker compose run --rm -e FINMIND_TOKEN agent python3 cli/market_fear_greed.py capture >/dev/null
  FEAR_GREED_EXIT=$?
  set -e
  if [ "$FEAR_GREED_EXIT" -ne 0 ]; then
    echo "恐懼貪婪指標來源未完整（exit ${FEAR_GREED_EXIT}）；市場情緒可能維持資料不足。" >&2
    DATA_STATUS=2
  fi
  stage_time "恐懼貪婪指標"
  if [ "$DATA_ONLY" -eq 1 ]; then
    echo "[4/4] 顯示資料狀態"
    docker compose run --rm agent python3 scripts/data_status.py
    if [ "${YAHOO_EXIT:-0}" -ne 0 ]; then
      exit "$YAHOO_EXIT"
    fi
    exit "$DATA_STATUS"
  fi
  # 虛擬帳本要用 cutoff 前封存的官方開休市日期表算交割日；沒有就先抓，且必須在固定 cutoff 之前。
  echo "[3b++/4] 確認官方開休市日期表（缺少才抓）"
  set +e
  docker compose run --rm agent python3 scripts/capture_trading_status_candidates.py --only-if-missing >/dev/null
  CALENDAR_EXIT=$?
  set -e
  if [ "$CALENDAR_EXIT" -ne 0 ]; then
    echo "開休市日期表抓取失敗（exit ${CALENDAR_EXIT}）；虛擬帳本將無法計算交割日。" >&2
    DATA_STATUS=2
  fi
  # cutoff 只記到整秒，剛抓完的資料若在同一秒內完成，會被判成晚於 cutoff 而排除；多等一秒避免。
  sleep 1
  # 未指定歷史 cutoff 時，必須在所有資料收集完成後才固定現在時間；
  # 否則本次抓到的文件會因 available_at 晚於 cutoff 而被正確排除。
  DAILY_CUTOFF=${DAILY_CUTOFF:-$(TZ=Asia/Taipei date '+%Y-%m-%dT%H:%M:%S%z' | sed -E 's/([+-][0-9]{2})([0-9]{2})$/\1:\2/')}
  echo "[3c/4] 建立研究 Snapshot：$DAILY_CUTOFF"
  docker compose run --rm agent python3 cli/data_agent.py snapshot \
    --decision-cutoff "$DAILY_CUTOFF" \
    --output artifacts/data/research_snapshot_latest.json
  stage_time "研究 Snapshot"
  ACCOUNT_ID=${ACCOUNT_ID:-ai-cup-2026}
  ACCOUNT_RUN_ID=${ACCOUNT_RUN_ID:-prepare-$(TZ=UTC date '+%Y%m%dT%H%M%SZ')}
  echo "[3c+/4] 推進虛擬帳本：以收盤價結算前一份決策，並建立決策前帳戶快照"
  set +e
  docker compose run --rm agent python3 cli/virtual_account.py \
    --account-id "$ACCOUNT_ID" daily \
    --snapshot artifacts/data/research_snapshot_latest.json \
    --database var/etf_agent.db \
    --run-id "$ACCOUNT_RUN_ID" \
    --account-output "artifacts/ledger/$ACCOUNT_ID/account_snapshot_latest.json"
  ACCOUNT_EXIT=$?
  set -e
  stage_time "虛擬帳本"
fi

echo "[4/4] 顯示資料狀態"
docker compose run --rm agent python3 scripts/data_status.py

if [ "$DAILY_RUN" -eq 1 ] && [ "${ACCOUNT_EXIT:-0}" -ne 0 ]; then
  echo "虛擬帳本推進失敗，績效儀表板不會更新；請查看上方錯誤訊息。" >&2
  exit "$ACCOUNT_EXIT"
fi

if [ "$DATA_ONLY" -eq 1 ] && [ "$DAILY_RUN" -eq 0 ]; then
  echo "官方交易池空白；僅更新開發行情，研究資料未更新。" >&2
  exit 2
fi
