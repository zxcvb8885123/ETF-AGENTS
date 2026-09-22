#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_DIR"

MODE=${1:-auto}

case "$MODE" in
  auto|official|all|check|daily)
    ;;
  *)
    echo "用法：./start.sh [auto|official|all|check|daily]" >&2
    echo "  auto      有官方交易池就正式抓取，否則使用開發模式（預設）" >&2
    echo "  official  僅抓取官方交易池，交易池空白時停止" >&2
    echo "  all       開發用，抓取 TWSE 端點全部可解析證券" >&2
    echo "  check     建置映像並執行環境檢查與測試，不抓資料" >&2
    echo "  daily     一鍵驗證來源、更新行情／事件並建立今日 Snapshot" >&2
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

echo "[1/4] 建立 Docker 映像"
docker compose build

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
  DAILY_CUTOFF=${DAILY_CUTOFF:-$(TZ=Asia/Taipei date '+%Y-%m-%dT%H:%M:%S%z')}
  echo "[3b/4] 收集官方月營收與重大訊息"
  docker compose run --rm agent python3 cli/data_agent.py collect
  echo "[3c/4] 建立研究 Snapshot：$DAILY_CUTOFF"
  docker compose run --rm agent python3 cli/data_agent.py snapshot \
    --decision-cutoff "$DAILY_CUTOFF" \
    --output artifacts/research_snapshot_latest.json
fi

echo "[4/4] 顯示資料狀態"
docker compose run --rm agent python3 scripts/data_status.py
