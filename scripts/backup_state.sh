#!/bin/bash
# 把帳本、決策、D-Plan、設定備份到私人 GitHub 倉庫 etf-agents-backup。
# 只複製小而重要的資料；不含 .env、var/ 資料庫、artifacts/daily 暫存。
# 用法：scripts/backup_state.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${ETF_BACKUP_DIR:-$HOME/stock/etf-agents-backup}"
REMOTE="git@github.com:zxcvb8885123/etf-agents-backup.git"

if [ ! -d "$DEST/.git" ]; then
  git clone "$REMOTE" "$DEST"
fi

for d in ledger decisions competition_d_plan fallback_decisions; do
  if [ -d "$SRC/artifacts/$d" ]; then
    mkdir -p "$DEST/artifacts/$d"
    rsync -a --delete "$SRC/artifacts/$d/" "$DEST/artifacts/$d/"
  fi
done
mkdir -p "$DEST/config"
rsync -a --delete --exclude '.env*' "$SRC/config/" "$DEST/config/"

cd "$DEST"
git add -A
if git diff --cached --quiet; then
  echo "沒有變更，不需備份。"
  exit 0
fi
git commit -q -m "backup $(TZ=Asia/Taipei date '+%Y-%m-%d %H:%M')"
git push -q origin HEAD
echo "備份完成：$(git rev-parse --short HEAD)"
