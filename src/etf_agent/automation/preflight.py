"""每日流程執行前檢查：在抓資料或呼叫任何子 Agent 前確認執行環境。

只檢查環境與必要檔案是否就緒，不修改任何資料；收盤價是否公布仍由資料階段與
帳本 ``settle`` 判定（``waiting_for_close_data`` 時不建立新決策）。外部指令都有
逾時，失敗訊息只記錄狀態，不保存帳號識別資訊。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from etf_agent.virtual_account import VirtualAccountError, VirtualAccountRepository


DEFAULT_MIN_FREE_BYTES = 2 * 1024 ** 3
AGENT_CLIS = ("claude", "codex", "opencode")
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
            "artifacts/data/research_snapshot_latest.json",
            "artifacts/ledger/%s/account_snapshot_latest.json" % account_id,
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


def effective_research_agent_cli(agent_cli: str, research_agent_cli: Optional[str] = None) -> str:
    """多空研究員實際使用的 CLI：未指定時與主 CLI 相同（主 CLI 是 codex 時研究員也用 codex，隔離由研究階段輸出暫存保證）；明示時照指定。"""
    if research_agent_cli is not None:
        return research_agent_cli
    return agent_cli


def check_codex(runner: CommandRunner, which: Callable[[str], Optional[str]], executable: str = "codex") -> PreflightCheck:
    """Codex 登入檢查。"""
    if which(executable) is None:
        return PreflightCheck("codex", False, "找不到 %s 指令" % executable)
    try:
        completed = _run(runner, [executable, "login", "status"])
    except (OSError, subprocess.TimeoutExpired) as error:
        return PreflightCheck("codex", False, "無法讀取登入狀態：%s" % error)
    if completed.returncode != 0:
        return PreflightCheck("codex", False, "Codex CLI 未登入或登入狀態不可用（請執行 codex login）")
    return PreflightCheck("codex", True, "已登入")


def check_codex_ephemeral(runner: CommandRunner, which: Callable[[str], Optional[str]], executable: str = "codex") -> PreflightCheck:
    """Codex 當多空研究員的前置條件：``codex exec`` 須支援 ``--ephemeral``（隔離任務不把 session 紀錄寫到磁碟）。

    隔離本身由研究階段輸出暫存保證（時序隔離），不是權限隔離：Codex 唯讀沙箱只擋寫、不擋讀，仍可讀整個專案，
    包含舊 run 的多空輸出。"""
    detail = "多空隔離由研究階段輸出暫存保證（多空輸出在兩方全部完成前不落地到磁碟）；Codex 仍可讀整個專案，包含舊 run 的多空輸出，這是時序隔離、不是權限隔離"
    if which(executable) is None:
        return PreflightCheck("research_agent_cli", False, "找不到 %s 指令；%s" % (executable, detail))
    try:
        completed = _run(runner, [executable, "exec", "--help"])
    except (OSError, subprocess.TimeoutExpired) as error:
        return PreflightCheck("research_agent_cli", False, "無法確認 codex exec 是否支援 --ephemeral：%s" % error)
    if completed.returncode != 0 or "--ephemeral" not in completed.stdout:
        return PreflightCheck("research_agent_cli", False, "此版本 codex exec 不支援 --ephemeral，Codex 研究員會把 session 紀錄寫到磁碟而破壞隔離；請升級 Codex 或改用 claude／opencode")
    return PreflightCheck("research_agent_cli", True, detail)


def check_opencode(runner: CommandRunner, which: Callable[[str], Optional[str]], executable: str = "opencode") -> PreflightCheck:
    if which(executable) is None:
        return PreflightCheck("opencode", False, "找不到 opencode 指令")
    try:
        completed = _run(runner, [executable, "auth", "list", "--pure"])
    except (OSError, subprocess.TimeoutExpired) as error:
        return PreflightCheck("opencode", False, "無法檢查 OpenCode 登入狀態：%s" % error)
    # 不保存憑證路徑、提供者帳戶或使用者識別；只保留 CLI 回報的數量狀態。
    output = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", completed.stdout)
    if completed.returncode != 0 or re.search(r"\b[1-9]\d* credentials\b", output) is None:
        return PreflightCheck("opencode", False, "OpenCode 未找到可用憑證（請先執行 opencode auth login）")
    return PreflightCheck("opencode", True, "已設定憑證")


def check_account(root: Path, account_id: str) -> PreflightCheck:
    account_root = root / "artifacts" / "ledger"
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
    agent_cli: str = "claude",
    codex_executable: str = "codex",
    opencode_executable: str = "opencode",
    research_agent_cli: Optional[str] = None,
) -> List[PreflightCheck]:
    """依序執行全部檢查並回傳結果；不在第一個失敗就停，方便一次看到所有問題。

    ``research_agent_cli`` 是多頭／空頭研究員使用的 CLI，預設與 ``agent_cli`` 相同（含 codex）。
    Codex 唯讀沙箱無法強制拒讀，Codex 研究員的多空隔離改由研究階段輸出暫存保證
    （時序隔離，Codex 仍可讀整個專案含舊 run），並要求 ``codex exec`` 支援 ``--ephemeral``。
    研究員 CLI 與主 CLI 不同時，兩者的登入都要通過。
    """
    root = Path(root)
    checks = []
    if not skip_data:
        # 資料階段經 ./start.sh 在 Docker 內執行；--skip-data 時不需要。
        checks.append(check_docker(runner, which))
    if agent_cli not in AGENT_CLIS:
        raise ValueError("不支援的 Agent CLI：%s" % agent_cli)
    research_cli = effective_research_agent_cli(agent_cli, research_agent_cli)
    if research_cli not in AGENT_CLIS:
        raise ValueError("不支援的研究員 Agent CLI：%s" % research_cli)
    login_checks = {
        "claude": lambda: check_claude(runner, which, claude_executable),
        "codex": lambda: check_codex(runner, which, codex_executable),
        "opencode": lambda: check_opencode(runner, which, opencode_executable),
    }
    checks.append(login_checks[agent_cli]())
    if research_cli != agent_cli:
        checks.append(login_checks[research_cli]())
    if research_cli == "codex":
        checks.append(check_codex_ephemeral(runner, which, codex_executable))
    checks += [
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
