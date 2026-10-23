"""比較兩次決策的結論：分析師逐檔看法、交易意圖、現金姿態與委託；只讀已驗證的輸出檔。"""

from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from .contracts import ANALYSTS, EvaluationError


_DIFF_LIMIT = 15
OPPOSITE = {("positive", "negative"), ("negative", "positive")}


def _read(path: Path) -> Optional[Mapping[str, object]]:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvaluationError("無法讀取 %s" % path) from error
    return value if isinstance(value, Mapping) else None


def load_outputs(run_dir: Path) -> Dict[str, object]:
    """從 run 目錄讀出可比較的結論；沒跑到的階段為 None（例如只評測分析師時沒有交易）。"""
    run_dir = Path(run_dir)
    result: Dict[str, object] = {}
    for name in ANALYSTS:
        report = _read(run_dir / ("analyst_%s.json" % name))
        result[name] = None if report is None else {
            str(item["symbol"]): str(item.get("outlook")) for item in report.get("items", [])
        }
    event = _read(run_dir / "analyst_event.json")
    result["event_materiality"] = None if event is None else {
        str(entry["evidence_id"]): str(entry.get("materiality"))
        for item in event.get("items", []) for entry in item.get("events", [])
    }
    intent = _read(run_dir / "intent.json")
    result["intent"] = None if intent is None else {
        str(item["symbol"]): "%s/%s" % (item.get("intent"), item.get("conviction")) for item in intent.get("items", [])
    }
    stance = _read(run_dir / "cash_stance.json")
    result["cash_stance"] = None if stance is None else {"portfolio": str(stance.get("level"))}
    decision = _read(run_dir / "decision.json")
    result["orders"] = None if decision is None else {
        str(order["symbol"]): str(order.get("side")) for order in decision.get("orders", [])
    }
    result["decision_status"] = None if decision is None else decision.get("status")
    return result


def compare_maps(left: Optional[Mapping[str, str]], right: Optional[Mapping[str, str]], *, union: bool = False) -> Optional[Dict[str, object]]:
    """兩邊都有時，計算相同比例；``union`` 用於委託（一邊有、一邊沒有也算不同）。"""
    if left is None or right is None:
        return None
    keys = sorted(set(left) | set(right)) if union else sorted(set(left) & set(right))
    if not keys:
        return {"compared": 0, "agreement": None, "opposite": 0, "differences": []}
    differences = [{"key": key, "left": left.get(key), "right": right.get(key)}
                   for key in keys if left.get(key) != right.get(key)]
    opposite = sum(1 for row in differences if (row["left"], row["right"]) in OPPOSITE)
    return {
        "compared": len(keys), "agreement": round(1 - len(differences) / len(keys), 4),
        "opposite": opposite, "differences": differences[:_DIFF_LIMIT],
    }


COMPONENTS = (*ANALYSTS, "event_materiality", "intent", "cash_stance", "orders")


def compare_outputs(left: Mapping[str, object], right: Mapping[str, object]) -> Dict[str, object]:
    return {name: compare_maps(left.get(name), right.get(name), union=name == "orders") for name in COMPONENTS}


def summarize(comparisons: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    """多組比較的平均相同比例與看法相反（正面↔負面）次數。"""
    summary = {}
    for name in COMPONENTS:
        rows = [item[name] for item in comparisons if item.get(name) and item[name]["agreement"] is not None]
        summary[name] = None if not rows else {
            "pairs": len(rows),
            "mean_agreement": round(sum(row["agreement"] for row in rows) / len(rows), 4),
            "min_agreement": min(row["agreement"] for row in rows),
            "opposite": sum(row["opposite"] for row in rows),
        }
    return summary


def pairwise_stability(outputs: Sequence[Mapping[str, object]]) -> List[Dict[str, object]]:
    """同一份輸入重跑多次時，兩兩比較結論。"""
    return [compare_outputs(left, right) for left, right in combinations(outputs, 2)]


def process_stats(agent_calls: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    """執行過程：呼叫次數、需要重試的步驟、驗證失敗、用量與耗時。"""
    retried = sorted({str(call["name"]) for call in agent_calls if int(call.get("attempt", 1)) > 1})
    failures = [{"name": call["name"], "attempt": call.get("attempt"), "validation_errors": call.get("validation_errors"),
                 "first_validation_errors": call.get("first_validation_errors", [])}
                for call in agent_calls if call.get("validation_errors")]
    return {
        "calls": len(agent_calls),
        "retried_steps": retried,
        "validation_failures": failures[:_DIFF_LIMIT],
        "estimated_usage_usd": round(sum(float(call.get("estimated_usage_usd") or 0) for call in agent_calls), 4),
        "agent_seconds": round(sum(float(call.get("duration_seconds") or 0) for call in agent_calls), 1),
    }
