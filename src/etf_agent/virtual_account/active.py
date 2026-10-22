"""目前使用中的帳戶 ID：集中在 config/active_account.json，避免各入口各自寫死。"""

import json
import os
import tempfile
from pathlib import Path

from .service import RUN_RE, VirtualAccountError

DEFAULT_ACCOUNT_ID = "ai-cup-2026"
CONFIG_PATH = Path("config") / "active_account.json"


def active_account_id(root: Path) -> str:
    """讀取使用中的帳戶；設定檔不存在時沿用預設帳戶，格式錯誤則拒絕。"""
    path = Path(root) / CONFIG_PATH
    if not path.exists():
        return DEFAULT_ACCOUNT_ID
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VirtualAccountError("無法讀取 %s" % path) from error
    account_id = value.get("account_id") if isinstance(value, dict) else None
    if not isinstance(account_id, str) or not RUN_RE.fullmatch(account_id):
        raise VirtualAccountError("%s 的 account_id 格式不合法" % path)
    return account_id


def write_active_account(root: Path, account_id: str, **extra: object) -> Path:
    """原子改寫使用中的帳戶；只換指標，不刪任何帳本。"""
    if not RUN_RE.fullmatch(account_id):
        raise VirtualAccountError("account_id 格式不合法")
    path = Path(root) / CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"account_id": account_id, **extra,
            "note": "所有入口（start.sh、每日流程、儀表板、D-Plan、虛擬帳戶 CLI）預設使用的帳戶。比賽第一天以 cli/virtual_account.py open-competition 開新帳戶時會自動改寫；舊帳戶保留不刪。"}
    fd, temporary = tempfile.mkstemp(prefix=".active-account-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(body, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path
