"""回放報告的 Markdown 與兩個 Policy 的同期比較（不新增任何結論，只排版與相減）。"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Mapping, Optional

from etf_agent.core import canonical_sha256

from .contracts import REPLAY_SCHEMA_VERSION, ReplayError
from .replay_tools import format_rate


def _pct(value: Optional[str]) -> str:
    return "無" if value is None else "%.2f%%" % (Decimal(value) * 100)


def _annualized(item: Mapping[str, object]) -> str:
    if item.get("status") != "available":
        return "無（%s）" % item.get("reason")
    return "%s%s" % (_pct(str(item["value"])), "" if item.get("reliable") else "（樣本不足，不可靠）")


def _sharpe(item: Mapping[str, object]) -> str:
    return "unavailable（%s）" % item.get("reason") if item.get("status") != "available" else str(Decimal(str(item["value"])).quantize(Decimal("0.01")))


def render_replay_report(report: Mapping[str, object]) -> str:
    metrics = report["metrics"]
    template = report["provenance"]["policy_template"]
    code = report["provenance"]["code_version"]
    tradeagent = report["provenance"].get("tradeagent") or {}
    lines = [
        "# 便宜版回測（多日滾動回放）：%s" % report.get("replay_id", "未封存"), "",
        "策略樣板 `%s`（content_sha256 `%s`）；%d 個 Decision run；as_of %s；狀態 %s。不呼叫 Agent，只重算確定性程式。" % (
            template["policy_id"], template["content_sha256"], len(report["inputs"]["run_ids"]), report["inputs"]["as_of"], report["status"]),
        "",
        "- 程式版本：git `%s`%s；engine %s" % (code.get("git_commit") or "unavailable", "（工作樹有未提交修改）" if code.get("git_dirty") else "",
                                           "、".join("%s %s" % pair for pair in sorted(code["engine_versions"].items()))),
        "- tradeagent/ 目錄雜湊：`%s`（%s 個檔案）" % (tradeagent.get("sha256", "unavailable"), tradeagent.get("files", 0)),
        "- 成交口徑：%s；交割日 %s" % (report["provenance"]["execution_price_rule"], report["provenance"]["settlement_calendar_basis"]),
        "- 起始帳戶：%s，現金 %s，%d 檔持股" % (report["start_account"]["basis"], report["start_account"]["cash"], len(report["start_account"]["positions"])),
        "", "## 績效", "",
        "| 項目 | 數值 |", "| --- | --- |",
        "| 起始／期末 NAV | %s ／ %s |" % (metrics["start_nav"], metrics["end_nav"]),
        "| 完成成交日 | %d |" % metrics["trading_days"],
        "| 總報酬 | %s |" % _pct(metrics["total_return"]),
        "| 年化報酬 | %s |" % _annualized(metrics["annualized_return"]),
        "| 最大回撤 | %s |" % _pct(metrics["max_drawdown"]),
        "| Sharpe | %s |" % _sharpe(metrics["sharpe"]),
        "| 換手率（成交金額 ÷ 平均 NAV） | %s |" % _pct(metrics["turnover_rate"]),
        "| 總費稅（手續費＋證交稅） | %s（%s＋%s） |" % (metrics["total_fees_and_taxes"], metrics["total_commission"], metrics["total_tax"]),
        "| 成交筆數 | %d |" % metrics["fill_count"],
        "",
    ]
    if metrics["trading_days"] < 20:
        lines.append("只有 %d 個成交日：以上數字沒有統計意義，只證明回放流程可跑。" % metrics["trading_days"])
        lines.append("")
    lines.extend(["## 逐日", "", "| # | run | 成交日 | 狀態 | 決策 | 現金姿態 | 訂單 | 成交 | 未成交 | 日終 NAV |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"])
    for day in report["days"]:
        execution = day.get("execution") or {}
        policy = day.get("policy") or {}
        stance = policy.get("cash_stance", "-")
        if policy.get("stance_raised_to_floor"):
            stance += "（回撤下限提高自 %s）" % policy.get("archived_cash_stance")
        lines.append("| %d | %s | %s | %s | %s | %s | %d | %d | %d | %s |" % (
            day["index"], day.get("run_id") or "（缺日）", day.get("trade_date") or "待官方價格", day["status"], day.get("decision_status") or "-",
            stance, len(day.get("orders") or []), len(execution.get("fills") or []), len(execution.get("unfilled_orders") or []),
            (day.get("close") or {}).get("nav") or "-"))
    lines.append("")
    for day in report["days"]:
        notes = []
        if day.get("pending_reason"):
            notes.append("成交待定：%s" % day["pending_reason"])
        if (day.get("guard") or {}).get("failed_rules"):
            notes.append("Guard 未通過：%s（回放不重現風險 Agent 修正，當日不下單）" % "、".join(day["guard"]["failed_rules"]))
        if (day.get("policy") or {}).get("available_at_overridden"):
            notes.append("策略樣板版本晚於該日 cutoff，回放把 available_at 提前到 cutoff")
        if (day.get("policy") or {}).get("breadth_unavailable"):
            notes.append("封存的現金姿態沒有持股廣度，回放沿用填滿 max_positions 的舊行為")
        for note in notes:
            lines.append("- 第 %d 天（%s）：%s" % (day["index"], day.get("run_id") or "缺日", note))
    if report.get("not_replayed_run_ids"):
        lines.append("- 之後未回放的 run：%s" % "、".join(report["not_replayed_run_ids"]))
    lines.extend(["", "## 使用的 Decision run", ""])
    for item in report["provenance"]["runs"]:
        extra = "；同日較早的 run 已略過：%s" % "、".join(item["superseded_run_ids"]) if item["superseded_run_ids"] else ""
        lines.append("- %s：cutoff %s，目標時段日 %s（%s），manifest `%s`%s" % (
            item["run_id"], item["decision_cutoff"], item["session_day"], item["session_day_basis"], item["manifest_sha256"], extra))
    lines.extend(["", "## 限制", ""])
    lines.extend("- %s" % note for note in report["limitations"])
    lines.extend(["", "報告 content_sha256：`%s`；用 `cli/policy_replay.py verify --replay-id %s` 重算驗證。" % (
        report["content_sha256"], report.get("replay_id", "<replay_id>")), ""])
    return "\n".join(lines)


_COMPARED = (
    ("end_nav", "期末 NAV"), ("total_return", "總報酬"), ("max_drawdown", "最大回撤"), ("turnover_rate", "換手率"),
    ("total_fees_and_taxes", "總費稅"), ("fill_count", "成交筆數"), ("trading_days", "完成成交日"),
)


def _metric_value(metrics: Mapping[str, object], key: str) -> Optional[Decimal]:
    value = metrics.get(key)
    return None if value is None else Decimal(str(value))


def compare_replays(left: Mapping[str, object], right: Mapping[str, object]) -> Dict[str, object]:
    """同一批 run、同一 as_of 的兩個回放相減：績效差、逐日 NAV 差、訂單差異。不同批 run 直接拒絕。"""
    if left["inputs"]["run_ids"] != right["inputs"]["run_ids"]:
        raise ReplayError("兩個回放使用的 Decision run 不同，不能比較")
    if left["inputs"]["as_of"] != right["inputs"]["as_of"]:
        raise ReplayError("兩個回放的 as_of 不同，價格可得範圍不一樣，不能比較")
    metrics: Dict[str, object] = {}
    for key, label in _COMPARED:
        a, b = _metric_value(left["metrics"], key), _metric_value(right["metrics"], key)
        metrics[key] = {"label": label, "a": None if a is None else str(a), "b": None if b is None else str(b),
                        "diff": None if a is None or b is None else format_rate(b - a) if key not in {"fill_count", "trading_days"} else str(b - a)}
    for key in ("annualized_return", "sharpe"):
        metrics[key] = {"label": "年化報酬" if key == "annualized_return" else "Sharpe",
                        "a": left["metrics"][key].get("value"), "b": right["metrics"][key].get("value"), "diff": None}
    nav_rows: List[Dict[str, object]] = []
    for row_a, row_b in zip(left["nav_series"], right["nav_series"]):
        if row_a["trade_date"] != row_b["trade_date"]:
            raise ReplayError("兩個回放的 NAV 日期不對齊：%s 與 %s" % (row_a["trade_date"], row_b["trade_date"]))
        nav_rows.append({"trade_date": row_a["trade_date"], "a": row_a["nav"], "b": row_b["nav"],
                         "diff": str(Decimal(row_b["nav"]) - Decimal(row_a["nav"]))})
    order_diffs: List[Dict[str, object]] = []
    days_b = {day.get("run_id"): day for day in right["days"] if day.get("run_id")}
    for day_a in left["days"]:
        day_b = days_b.get(day_a.get("run_id"))
        if day_a.get("run_id") is None or day_b is None:
            continue
        orders_a = {(order["symbol"], order["side"]): int(order["shares"]) for order in day_a.get("orders") or []}
        orders_b = {(order["symbol"], order["side"]): int(order["shares"]) for order in day_b.get("orders") or []}
        only_a = sorted("%s %s %d" % (symbol, side, shares) for (symbol, side), shares in orders_a.items() if (symbol, side) not in orders_b)
        only_b = sorted("%s %s %d" % (symbol, side, shares) for (symbol, side), shares in orders_b.items() if (symbol, side) not in orders_a)
        changed = sorted("%s %s %d→%d" % (symbol, side, orders_a[key], orders_b[key])
                         for key in orders_a.keys() & orders_b.keys() for symbol, side in [key] if orders_a[key] != orders_b[key])
        if only_a or only_b or changed:
            order_diffs.append({"run_id": day_a["run_id"], "trade_date": day_a.get("trade_date"), "only_a": only_a, "only_b": only_b, "changed": changed})
    comparison = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "a": {"replay_id": left.get("replay_id"), "policy_id": left["provenance"]["policy_template"]["policy_id"],
              "policy_sha256": left["provenance"]["policy_template"]["content_sha256"], "report_sha256": left["content_sha256"]},
        "b": {"replay_id": right.get("replay_id"), "policy_id": right["provenance"]["policy_template"]["policy_id"],
              "policy_sha256": right["provenance"]["policy_template"]["content_sha256"], "report_sha256": right["content_sha256"]},
        "run_ids": list(left["inputs"]["run_ids"]), "metrics": metrics, "nav": nav_rows, "order_differences": order_diffs,
        "statistically_meaningful": int(left["metrics"]["trading_days"]) >= 20 and int(right["metrics"]["trading_days"]) >= 20,
    }
    comparison["content_sha256"] = canonical_sha256(comparison)
    return comparison


def render_comparison(comparison: Mapping[str, object]) -> str:
    a, b = comparison["a"], comparison["b"]
    lines = [
        "# 兩個 Policy 的同期回放比較", "",
        "A：`%s`（%s，policy sha `%s`）；B：`%s`（%s，policy sha `%s`）；%d 個 run。" % (
            a["policy_id"], a["replay_id"], a["policy_sha256"], b["policy_id"], b["replay_id"], b["policy_sha256"], len(comparison["run_ids"])),
        "",
    ]
    if not comparison["statistically_meaningful"]:
        lines.extend(["完成成交日少於 20 天：差異沒有統計意義，只說明兩個 Policy 在這些日子算出不同的部位。", ""])
    lines.extend(["| 項目 | A | B | B−A |", "| --- | --- | --- | --- |"])
    for key, row in comparison["metrics"].items():
        lines.append("| %s | %s | %s | %s |" % (row["label"], row["a"] if row["a"] is not None else "無",
                                               row["b"] if row["b"] is not None else "無", row["diff"] if row["diff"] is not None else "-"))
    lines.extend(["", "## 逐日 NAV", "", "| 日期 | A | B | B−A |", "| --- | --- | --- | --- |"])
    lines.extend("| %s | %s | %s | %s |" % (row["trade_date"], row["a"], row["b"], row["diff"]) for row in comparison["nav"])
    lines.extend(["", "## 訂單差異", ""])
    if not comparison["order_differences"]:
        lines.append("- 兩個 Policy 每天的訂單完全相同。")
    for item in comparison["order_differences"]:
        parts = []
        if item["only_a"]:
            parts.append("只有 A：%s" % "、".join(item["only_a"]))
        if item["only_b"]:
            parts.append("只有 B：%s" % "、".join(item["only_b"]))
        if item["changed"]:
            parts.append("張數不同：%s" % "、".join(item["changed"]))
        lines.append("- %s（%s）：%s" % (item["run_id"], item["trade_date"] or "待成交", "；".join(parts)))
    lines.extend(["", "比較 content_sha256：`%s`" % comparison["content_sha256"], ""])
    return "\n".join(lines)
