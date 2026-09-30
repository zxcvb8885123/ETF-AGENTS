#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"

if [ -z "${FINMIND_TOKEN:-}" ] && [ -r .env ]; then
  FINMIND_TOKEN=$(sed -n 's/^FINMIND_TOKEN=//p' .env | sed -n '1p')
  export FINMIND_TOKEN
fi
if [ -z "${FINMIND_TOKEN:-}" ]; then
  echo "缺少 FinMind Token；個股新聞候選未擷取。" >&2
  exit 2
fi

NEWS_DATE=${1:-$(TZ=Asia/Taipei date '+%Y-%m-%d')}
exec .venv/bin/python scripts/collect_supplemental_sources.py \
  finmind-news --universe data/official_universe.csv --date "$NEWS_DATE"
