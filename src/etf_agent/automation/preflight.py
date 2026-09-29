"""每日流程執行前檢查：在抓資料或呼叫任何子 Agent 前確認執行環境。

只檢查環境與必要檔案是否就緒，不修改任何資料；收盤價是否公布仍由資料階段與
帳本 ``settle`` 判定（``waiting_for_close_data`` 時不建立新決策）。外部指令都有
逾時，失敗訊息只記錄狀態，不保存帳號識別資訊。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from etf_agent.virtual_account import VirtualAccountError, VirtualAccountRepository


DEFAULT_MIN_FREE_BYTES = 2 * 1024 ** 3
COMMAND_TIMEOUT_SECONDS = 30
REQUIRED_FILES = (
    "config/decision_rules.json",
    "config/decision_policy.json",
    "config/trading_status_approvals.json",
    "data/sector_classification.json",
    "var/etf_agent.db",
    ".venv/bin/python",
)


def required_files(account_id: str, skip_data: bool) -> List[str]:
    """--skip-data 沿用既有 Snapshot 與帳戶快照，兩者必須已存在。"""
    files = list(REQUIRED_FILES)
    if skip_data:
        files += [
            "artifacts/research_snapshot_latest.json",
            "artifacts/virtual_accounts/%s/account_snapshot_latest.json" % account_id,
        ]
    return files


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    ok: bool
    detail: str


class PreflightError(RuntimeError):
    def __init__(self, checks: Sequence[PreflightCheck]):
        self.checks = list(checks)
        failed = [check for check in self.checks if not check.ok]
        super().__init__("執行前檢查未通過：%s" % "；".join("%s（%s）" % (check.name, check.detail) for check in failed))


CommandRunner = Callable[..., "subprocess.CompletedProcess[str]"]


def _run(runner: CommandRunner, command: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return runner(list(command), capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS, check=False)


def check_docker(runner: CommandRunner, which: Callable[[str], Optional[str]]) -> PreflightCheck:
    if which("docker") is None:
        return PreflightCheck("docker", False, "找不到 docker 指令")
    try:
        completed = _run(runner, ["docker", "info", "--format", "{{.ServerVersion}}"])
    except (OSError, subprocess.TimeoutExpired) as error:
        return PreflightCheck("docker", False, "docker info 失敗：%s" % error)
    if completed.returncode != 0:
        return PreflightCheck("docker", False, "Docker daemon 未啟動（請開啟 Docker Desktop）")
    return PreflightCheck("docker", True, "Docker %s" % completed.stdout.strip())


def check_claude(runner: CommandRunner, which: Callable[[str], Optional[str]], executable: str = "claude") -> PreflightCheck:
    if which(executable) is None:
        return PreflightCheck("claude", False, "找不到 %s 指令（launchd 需在 PATH 加入其安裝目錄）" % executable)
    try:
        completed = _run(runner, [executable, "auth", "status", "--json"])
        status = json.loads(completed.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        return PreflightCheck("claude", False, "無法讀取登入狀態：%s" % error)
    if not isinstance(status, dict) or status.get("loggedIn") is not True:
        return PreflightCheck("claude", False, "claude CLI 未登入（請執行 claude auth login）")
    return PreflightCheck("claude", True, "已登入（%s）" % status.get("authMethod", "unknown"))


def check_account(root: Path, account_id: str) -> PreflightCheck:
    account_root = root / "artifacts" / "virtual_accounts"
    # 先確認索引存在，避免建立 repository 時替未開帳的帳戶建出空目錄。
    if not (account_root / account_id / "latest.json").exists():
        return PreflightCheck("account", False, "虛擬帳戶 %s 尚未開帳（cli/virtual_account.py init）" % account_id)
    try:
        latest = VirtualAccountRepository(account_root, account_id).latest()
    except (VirtualAccountError, OSError, ValueError) as error:
        return PreflightCheck("account", False, "帳戶狀態驗證失敗：%s" % error)
    return PreflightCheck("account", True, "最新狀態 %s" % latest["run_id"])


def check_files(root: Path, relative_paths: Sequence[str]) -> PreflightCheck:
    missing = [path for path in relative_paths if not (root / path).exists()]
    if missing:
        return PreflightCheck("files", False, "缺少：%s" % "、".join(missing))
    return PreflightCheck("files", True, "%d 個必要檔案存在" % len(relative_paths))


def check_disk(root: Path, min_free_bytes: int, disk_usage: Callable[[Path], object]) -> PreflightCheck:
    free = int(getattr(disk_usage(root), "free"))
    detail = "可用 %.1f GiB（下限 %.1f GiB）" % (free / 1024 ** 3, min_free_bytes / 1024 ** 3)
    return PreflightCheck("disk", free >= min_free_bytes, detail)


def run_preflight(
    root: Path,
    account_id: str,
    skip_data: bool,
    runner: CommandRunner = subprocess.run,
    which: Callable[[str], Optional[str]] = shutil.which,
    disk_usage: Callable[[Path], object] = shutil.disk_usage,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    claude_executable: str = "claude",
) -> List[PreflightCheck]:
    """依序執行全部檢查並回傳結果；不在第一個失敗就停，方便一次看到所有問題。"""
    root = Path(root)
    checks = []
    if not skip_data:
        # 資料階段經 ./start.sh 在 Docker 內執行；--skip-data 時不需要。
        checks.append(check_docker(runner, which))
    checks += [
        check_claude(runner, which, claude_executable),
        check_account(root, account_id),
        check_files(root, required_files(account_id, skip_data)),
        check_disk(root, min_free_bytes, disk_usage),
    ]
    return checks


def require_preflight(checks: Sequence[PreflightCheck]) -> None:
    if not all(check.ok for check in checks):
        raise PreflightError(checks)


def checks_to_dict(checks: Sequence[PreflightCheck]) -> List[Dict[str, object]]:
    return [asdict(check) for check in checks]
