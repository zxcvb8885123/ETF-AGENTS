#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_DIR"

MODE=${1:-auto}

case "$MODE" in
  auto|official|all|check|daily|report|dashboard)
    ;;
  *)
    echo "用法：./start.sh [auto|official|all|check|daily|report|dashboard]" >&2
    echo "  auto      有官方交易池就正式抓取，否則使用開發模式（預設）" >&2
    echo "  official  僅抓取官方交易池，交易池空白時停止" >&2
    echo "  all       開發用，抓取 TWSE 端點全部可解析證券" >&2
    echo "  check     視需要建置映像並執行環境檢查與測試，不抓資料" >&2
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
if [ "$MODE" = "daily" ]; then
  DAILY_RUN=1
  MODE=official
fi

REPORT_ONLY=0
if [ "$MODE" = "report" ]; then
  REPORT_ONLY=1
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

if [ "$REPORT_ONLY" -eq 1 ]; then
  REPORT_RUN_ID=${REPORT_RUN_ID:-report-$(TZ=UTC date '+%Y%m%dT%H%M%SZ')}
  REPORT_GENERATED_AT=${REPORT_GENERATED_AT:-$(TZ=Asia/Taipei date '+%Y-%m-%dT%H:%M:%S%z' | sed -E 's/([+-][0-9]{2})([0-9]{2})$/\1:\2/')}
  echo "[3/4] 執行報告工作流：$REPORT_RUN_ID"
  set +e
  docker compose run --rm agent python3 cli/report_workflow.py run \
    --snapshot artifacts/research_snapshot_latest.json \
    --database var/etf_agent.db \
    --research artifacts/event_research_validated.json \
    --repository artifacts/report_runs \
    --reports artifacts/reports \
    --execution-mode official \
    --run-id "$REPORT_RUN_ID" \
    --generated-at "$REPORT_GENERATED_AT"
  WORKFLOW_EXIT=$?
  set -e
  echo "[4/4] 報告工作流完成，狀態檔：artifacts/reports/latest.md"
  exit "$WORKFLOW_EXIT"
fi

echo "[3/4] 初始化資料庫並抓取行情"
docker compose run --rm agent python3 scripts/init_db.py

if [ "$DAILY_RUN" -eq 1 ]; then
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
  docker compose run --rm agent python3 scripts/collect_latest_prices.py
  docker compose run --rm agent python3 scripts/collect_history.py
else
  docker compose run --rm agent python3 scripts/collect_twse.py --all-listed
fi

if [ "$DAILY_RUN" -eq 1 ]; then
  echo "[3b/4] 收集官方月營收與重大訊息"
  docker compose run --rm agent python3 cli/data_agent.py collect
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
  REPORT_RUN_ID=${REPORT_RUN_ID:-daily-$(TZ=UTC date '+%Y%m%dT%H%M%SZ')}
  REPORT_GENERATED_AT=${REPORT_GENERATED_AT:-$(TZ=Asia/Taipei date '+%Y-%m-%dT%H:%M:%S%z' | sed -E 's/([+-][0-9]{2})([0-9]{2})$/\1:\2/')}
  echo "[3d/4] 執行報告工作流：$REPORT_RUN_ID"
  set +e
  docker compose run --rm agent python3 cli/report_workflow.py run \
    --snapshot artifacts/research_snapshot_latest.json \
    --database var/etf_agent.db \
    --research artifacts/event_research_validated.json \
    --repository artifacts/report_runs \
    --reports artifacts/reports \
    --execution-mode official \
    --run-id "$REPORT_RUN_ID" \
    --generated-at "$REPORT_GENERATED_AT"
  WORKFLOW_EXIT=$?
  set -e
fi

echo "[4/4] 顯示資料狀態"
docker compose run --rm agent python3 scripts/data_status.py

if [ "$DAILY_RUN" -eq 1 ] && [ "${ACCOUNT_EXIT:-0}" -ne 0 ]; then
  echo "虛擬帳本推進失敗，績效儀表板不會更新；請查看上方錯誤訊息。" >&2
fi

if [ "$DAILY_RUN" -eq 1 ] && [ "${WORKFLOW_EXIT:-0}" -ne 0 ]; then
  echo "報告工作流尚未完成；請查看 artifacts/reports/latest.md。" >&2
  exit "$WORKFLOW_EXIT"
fi
