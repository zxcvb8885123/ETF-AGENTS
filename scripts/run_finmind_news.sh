#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"

# 與其他資料抓取共用 artifacts/daily/.data.lock：被佔用時最多等 ETF_DATA_LOCK_WAIT_SECONDS（預設 1800）秒，逾時停止。
if [ -z "${ETF_DATA_LOCK_HELD:-}" ]; then
  if [ -x .venv/bin/python ]; then LOCK_PYTHON=.venv/bin/python; else LOCK_PYTHON=python3; fi
  ETF_DATA_LOCK_HELD=1 exec "$LOCK_PYTHON" scripts/with_data_lock.py -- sh "$PROJECT_DIR/scripts/run_finmind_news.sh" "$@"
fi

if [ -z "${FINMIND_TOKEN:-}" ] && [ -r .env ]; then
  FINMIND_TOKEN=$(sed -n 's/^FINMIND_TOKEN=//p' .env | sed -n '1p')
  export FINMIND_TOKEN
fi
if [ -z "${FINMIND_TOKEN:-}" ]; then
  echo "缺少 FinMind Token；個股新聞候選未擷取。" >&2
  exit 2
fi

NEWS_DATE=${1:-$(TZ=Asia/Taipei date '+%Y-%m-%d')}
NEWS_EXIT=0
.venv/bin/python scripts/collect_supplemental_sources.py \
  finmind-news --universe data/official_universe.csv --date "$NEWS_DATE" || NEWS_EXIT=$?
# exit 2 代表部分股票當日沒有新聞，屬正常；只把真正的執行失敗回報給排程。
if [ "$NEWS_EXIT" -eq 2 ]; then
  echo "部分股票 ${NEWS_DATE} 沒有個股新聞（屬預期）。"
  exit 0
fi
exit "$NEWS_EXIT"
