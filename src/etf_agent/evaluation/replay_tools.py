"""回放的確定性工具：NAV 績效指標與來源／版本雜湊。

全部以 Decimal 計算；樣本不足時回 ``None`` 並標示，不以近似值充數。
"""

from __future__ import annotations

import hashlib
import subprocess
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from etf_agent.core import canonical_sha256, parse_decimal

from .contracts import MIN_ANNUALIZE_SAMPLES, MIN_SHARPE_SAMPLES, TRADING_DAYS_PER_YEAR, ReplayError


_RATE = Decimal("0.00000001")


def _decimal(value: object, field: str) -> Decimal:
    return parse_decimal(value, field, error=ReplayError, reject_bool=True)


def format_rate(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return str(value.quantize(_RATE, rounding=ROUND_HALF_UP))


def daily_returns(navs: Sequence[Decimal]) -> List[Decimal]:
    if any(nav <= 0 for nav in navs):
        raise ReplayError("NAV 必須大於 0 才能計算報酬")
    return [navs[index] / navs[index - 1] - Decimal(1) for index in range(1, len(navs))]


def max_drawdown(navs: Sequence[Decimal]) -> Optional[Decimal]:
    """峰值到谷底的最大跌幅；沒有樣本時 None。"""
    if not navs:
        return None
    peak, worst = navs[0], Decimal(0)
    for nav in navs:
        peak = max(peak, nav)
        worst = max(worst, (peak - nav) / peak)
    return worst


def sharpe_ratio(returns: Sequence[Decimal]) -> Dict[str, object]:
    """年化 Sharpe（無風險利率 0、樣本標準差）；樣本少於門檻或波動為 0 時 unavailable。"""
    count = len(returns)
    if count < MIN_SHARPE_SAMPLES:
        return {"status": "unavailable", "reason": "日報酬樣本 %d 少於 %d" % (count, MIN_SHARPE_SAMPLES), "value": None}
    mean = sum(returns, Decimal(0)) / Decimal(count)
    variance = sum(((value - mean) ** 2 for value in returns), Decimal(0)) / Decimal(count - 1)
    if variance <= 0:
        return {"status": "unavailable", "reason": "日報酬沒有波動", "value": None}
    value = mean / variance.sqrt() * Decimal(TRADING_DAYS_PER_YEAR).sqrt()
    return {"status": "available", "reason": None, "value": format_rate(value)}


def nav_metrics(
    nav_series: Sequence[Mapping[str, object]],
    gross_traded: Decimal,
    commission: Decimal,
    tax: Decimal,
    fill_count: int,
) -> Dict[str, object]:
    """由每日 NAV 序列（含起點）計算總報酬、年化、最大回撤、Sharpe、換手率與費稅。"""
    navs = [_decimal(row.get("nav"), "nav_series.nav") for row in nav_series]
    returns = daily_returns(navs)
    samples = len(returns)
    total_return = navs[-1] / navs[0] - Decimal(1) if samples else None
    annualized: Dict[str, object] = {"status": "unavailable", "reliable": False, "value": None,
                                     "reason": "沒有完成任何成交日"}
    if samples:
        growth = Decimal(1) + total_return
        value = growth ** (Decimal(TRADING_DAYS_PER_YEAR) / Decimal(samples)) - Decimal(1) if growth > 0 else Decimal(-1)
        reliable = samples >= MIN_ANNUALIZE_SAMPLES
        annualized = {"status": "available", "reliable": reliable, "value": format_rate(value),
                      "reason": None if reliable else "交易日 %d 少於 %d，年化只供參考" % (samples, MIN_ANNUALIZE_SAMPLES)}
    average_nav = sum(navs, Decimal(0)) / Decimal(len(navs)) if navs else None
    return {
        "nav_points": len(navs),
        "trading_days": samples,
        "start_nav": str(navs[0]) if navs else None,
        "end_nav": str(navs[-1]) if navs else None,
        "total_return": format_rate(total_return),
        "annualized_return": annualized,
        "max_drawdown": format_rate(max_drawdown(navs)),
        "sharpe": sharpe_ratio(returns),
        "turnover_rate": format_rate(gross_traded / average_nav) if average_nav else None,
        "gross_traded": str(gross_traded.quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
        "total_commission": str(commission),
        "total_tax": str(tax),
        "total_fees_and_taxes": str(commission + tax),
        "fill_count": fill_count,
    }


def file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as error:
        raise ReplayError("無法讀取檔案：%s" % path) from error


def tree_sha256(root: Path) -> Dict[str, object]:
    """目錄雜湊：相對路徑排序後逐檔 sha256，再對清單取 canonical 雜湊；略過 __pycache__ 與隱藏檔。"""
    root = Path(root)
    if not root.is_dir():
        raise ReplayError("找不到目錄：%s" % root)
    entries = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        parts = path.relative_to(root).parts
        if any(part == "__pycache__" or part.startswith(".") for part in parts):
            continue
        entries.append({"path": "/".join(parts), "sha256": file_sha256(path)})
    return {"root": root.name, "files": len(entries), "sha256": canonical_sha256(entries)}


def policy_template_sha256(template: Mapping[str, object]) -> str:
    payload = dict(template)
    payload.pop("content_sha256", None)
    return canonical_sha256(payload)


def code_version(root: Path, engine_versions: Mapping[str, str]) -> Dict[str, object]:
    """git commit 與工作樹是否乾淨；沒有 git 時記 unavailable，不猜測。"""
    result: Dict[str, object] = {"git_commit": None, "git_dirty": None, "engine_versions": dict(engine_versions)}
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, timeout=10)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return result
    if commit.returncode == 0 and status.returncode == 0:
        result["git_commit"] = commit.stdout.strip()
        result["git_dirty"] = bool(status.stdout.strip())
    return result
