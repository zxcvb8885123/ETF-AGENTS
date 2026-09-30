#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_DIR"

MODE=${1:-auto}

case "$MODE" in
  auto|official|all|check|data|daily|dashboard)
    ;;
  *)
    echo "用法：./start.sh [auto|official|all|check|data|daily|dashboard]" >&2
    echo "  auto      有官方交易池就正式抓取，否則使用開發模式（預設）" >&2
    echo "  official  僅抓取官方交易池，交易池空白時停止" >&2
    echo "  all       開發用，抓取 TWSE 端點全部可解析證券" >&2
    echo "  check     視需要建置映像並執行環境檢查與測試，不抓資料" >&2
    echo "  data      每日資料擷取，不建立 Snapshot 或推進帳本" >&2
    echo "  daily     一鍵驗證來源、更新行情／事件、建立 Snapshot 並交付報告" >&2
    echo "  report    使用既有 Snapshot 執行或續跑報告工作流" >&2
    echo "  dashboard 啟動唯讀績效儀表板 http://127.0.0.1:8000" >&2
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
if [ "$MODE" = "data" ]; then
  DAILY_RUN=1
  DATA_ONLY=1
  MODE=official
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
  echo "[2/2] 啟動績效儀表板：http://127.0.0.1:${DASHBOARD_PORT:-8000}"
  exec docker compose up dashboard
fi

echo "[2/4] 檢查專案設定"
docker compose run --rm agent

if [ "$MODE" = "check" ]; then
  echo "[3/4] 執行測試"
  docker compose run --rm agent python3 -m unittest discover -s tests -v
  echo "[4/4] 檢查完成"
  exit 0
fi

echo "[3/4] 初始化資料庫並抓取行情"
docker compose run --rm agent python3 scripts/init_db.py

if [ "$DAILY_RUN" -eq 1 ] && [ "$DATA_ONLY" -eq 0 ]; then
  echo "[3a/4] 驗證官方來源與交易池"
  docker compose run --rm agent python3 scripts/probe_data_sources.py
fi

if [ "$MODE" = "auto" ]; then
  UNIVERSE_ROWS=$(awk -F, 'NR > 1 && $1 != "" { count++ } END { print count + 0 }' data/official_universe.csv)
  if [ "$UNIVERSE_ROWS" -gt 0 ]; then
    MODE=official
    echo "偵測到官方交易池 $UNIVERSE_ROWS 檔，使用正式模式。"
  else
    MODE=all
    echo "官方交易池尚未填入，使用開發模式抓取全部 TWSE 最新行情。"
  fi
fi

if [ "$MODE" = "official" ]; then
  if [ "$DATA_ONLY" -eq 0 ]; then
    docker compose run --rm agent python3 scripts/collect_latest_prices.py
  fi
  if [ "$DATA_ONLY" -eq 1 ]; then
    set +e
    docker compose run --rm agent python3 scripts/collect_history.py
    YAHOO_EXIT=$?
    set -e
    if [ "$YAHOO_EXIT" -ne 0 ]; then
      echo "Yahoo 當日日線未完整（exit ${YAHOO_EXIT}）；仍繼續抓其他資料，資料排程最後回報非零狀態。" >&2
    fi
  else
    docker compose run --rm agent python3 scripts/collect_history.py
  fi
  if [ "$DAILY_RUN" -eq 1 ] && [ "$DATA_ONLY" -eq 0 ]; then
    # 正式帳本需當日官方成交金額／股數；全市場端點延遲時只在決策流程補個股原價。
    set +e
    docker compose run --rm agent python3 scripts/collect_official_history.py >/dev/null
    OFFICIAL_HISTORY_EXIT=$?
    set -e
    if [ "$OFFICIAL_HISTORY_EXIT" -eq 1 ]; then
      echo "帳本官方個股價量補抓失敗；結算將等待可用行情。詳見 artifacts/official-history/latest.json" >&2
    fi
  fi
else
  docker compose run --rm agent python3 scripts/collect_twse.py --all-listed
fi

if [ "$DAILY_RUN" -eq 1 ]; then
  DATA_STATUS=0
  echo "[3b/4] 收集官方月營收與重大訊息"
  docker compose run --rm agent python3 cli/data_agent.py collect
  echo "[3b+/4] 收集最近已到期季度的官方財報"
  # 覆蓋不完整（exit 2）屬預期，基本面分析會標示缺口；只有執行失敗才提示，但不中止每日流程。
  set +e
  docker compose run --rm agent python3 scripts/collect_financial_statements.py \
    --latest-due --report artifacts/financial_statements_latest.json >/dev/null
  FINANCIAL_EXIT=$?
  set -e
  if [ "$FINANCIAL_EXIT" -eq 1 ]; then
    echo "財報收集失敗；本次基本面分析將只使用月營收。詳見 artifacts/financial_statements_latest.json" >&2
  fi
  echo "[3b+/4] 更新官方公司基本資料與產業分類候選"
  set +e
  docker compose run --rm agent python3 scripts/collect_sector_classification.py \
    --output artifacts/sector_classification_latest.json >/dev/null
  SECTOR_EXIT=$?
  set -e
  if [ "$SECTOR_EXIT" -ne 0 ]; then
    echo "公司資料擷取未完整（exit ${SECTOR_EXIT}）；正式決策仍沿用現有已驗證分類。" >&2
    DATA_STATUS=2
  fi
  if [ -z "${FINMIND_TOKEN:-}" ] && [ -r .env ]; then
    FINMIND_TOKEN=$(sed -n 's/^FINMIND_TOKEN=//p' .env | sed -n '1p')
    export FINMIND_TOKEN
  fi
  if [ -z "${FINMIND_TOKEN:-}" ] && command -v security >/dev/null 2>&1; then
    FINMIND_TOKEN=$(security find-generic-password -s etf-agent-finmind -w 2>/dev/null || true)
    export FINMIND_TOKEN
  fi
  if [ -n "${FINMIND_TOKEN:-}" ]; then
    FINMIND_YEAR=$(TZ=Asia/Taipei date '+%Y')
    FINMIND_START="$((FINMIND_YEAR - 2))-01-01"
    FINMIND_END=$(TZ=Asia/Taipei date '+%Y-%m-%d')
    for DATASET in TaiwanStockFinancialStatements TaiwanStockBalanceSheet TaiwanStockCashFlowsStatement; do
      echo "[3b+/4] FinMind ${DATASET}：逐股更新財報候選"
      set +e
      docker compose run --rm -e FINMIND_TOKEN agent python3 scripts/collect_supplemental_sources.py \
        finmind --dataset "$DATASET" --universe data/official_universe.csv \
        --start "$FINMIND_START" --end "$FINMIND_END"
      FINMIND_EXIT=$?
      set -e
      if [ "$FINMIND_EXIT" -ne 0 ]; then
        echo "FinMind ${DATASET} 候選擷取未完整（exit ${FINMIND_EXIT}）；不影響正式 Snapshot。" >&2
        DATA_STATUS=2
      fi
    done
  else
    echo "未設定 FINMIND_TOKEN；略過 FinMind 候選擷取。" >&2
    DATA_STATUS=2
  fi
  if [ "$DATA_ONLY" -eq 1 ]; then
    echo "[4/4] 顯示資料狀態"
    docker compose run --rm agent python3 scripts/data_status.py
    if [ "${YAHOO_EXIT:-0}" -ne 0 ]; then
      exit "$YAHOO_EXIT"
    fi
    exit "$DATA_STATUS"
  fi
  # 未指定歷史 cutoff 時，必須在所有資料收集完成後才固定現在時間；
  # 否則本次抓到的文件會因 available_at 晚於 cutoff 而被正確排除。
  DAILY_CUTOFF=${DAILY_CUTOFF:-$(TZ=Asia/Taipei date '+%Y-%m-%dT%H:%M:%S%z' | sed -E 's/([+-][0-9]{2})([0-9]{2})$/\1:\2/')}
  echo "[3c/4] 建立研究 Snapshot：$DAILY_CUTOFF"
  docker compose run --rm agent python3 cli/data_agent.py snapshot \
    --decision-cutoff "$DAILY_CUTOFF" \
    --output artifacts/research_snapshot_latest.json
  ACCOUNT_ID=${ACCOUNT_ID:-ai-cup-2026}
  ACCOUNT_RUN_ID=${ACCOUNT_RUN_ID:-prepare-$(TZ=UTC date '+%Y%m%dT%H%M%SZ')}
  echo "[3c+/4] 推進虛擬帳本：以收盤價結算前一份決策，並建立決策前帳戶快照"
  set +e
  docker compose run --rm agent python3 cli/virtual_account.py \
    --account-id "$ACCOUNT_ID" daily \
    --snapshot artifacts/research_snapshot_latest.json \
    --database var/etf_agent.db \
    --run-id "$ACCOUNT_RUN_ID" \
    --account-output "artifacts/virtual_accounts/$ACCOUNT_ID/account_snapshot_latest.json"
  ACCOUNT_EXIT=$?
  set -e
fi

echo "[4/4] 顯示資料狀態"
docker compose run --rm agent python3 scripts/data_status.py

if [ "$DAILY_RUN" -eq 1 ] && [ "${ACCOUNT_EXIT:-0}" -ne 0 ]; then
  echo "虛擬帳本推進失敗，績效儀表板不會更新；請查看上方錯誤訊息。" >&2
  exit "$ACCOUNT_EXIT"
fi
