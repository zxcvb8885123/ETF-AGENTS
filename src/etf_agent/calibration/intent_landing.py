"""唯讀統計：交易 Agent 的 buy／add 意圖最後有沒有變成買單，以及哪些持股被程式賣出。

逐份讀已封存 Decision run 的 `intent.json` 與 `proposal.json`（先驗證 manifest 與雜湊），
輸出每日與合計的落地數、未落地原因分佈、旗標次數，以及「Agent 標 hold／add 卻被賣出」的清單與金額；
另依 `policy_id` 分段彙總（沒有 policy.json 的 run 歸「未記錄」），落地與未落地各依信心（high／medium／low）分層，
每段列出 hold／add 被程式賣出的金額。只描述發生了什麼，不設目標、不判斷達標與否。
只描述當時實際發生了什麼，不是績效，也不是對 LLM 判斷的前瞻驗證（證據等級 replay_reference_only）。

不寫入任何 artifact、不改 Policy，也不被 decision、automation 匯入。任何一份 run 缺檔、
雜湊不符、欄位不符或前後不一致即整批拒絕，不輸出部分結果、不補假值。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from etf_agent.core import parse_decimal
from etf_agent.core.artifact_store import ImmutableRunStore
from etf_agent.core.run_layout import list_run_dirs

from .contracts import REPLAY_EVIDENCE_GRADE, REPLAY_NOTICE, SIGNAL_INTENTS, CalibrationError, require_mapping, require_sequence

REPORT_VERSION = "intent_landing.v2"
REQUIRED_ARTIFACTS = ("intent", "proposal")
UNRECORDED = "未記錄"  # 封存沒有這個欄位（例如沒有 policy.json 或 intent 沒有信心）：不補值
NO_FLAG = "NO_FLAG"  # 沒有任何旗標、也沒有買單：例如 add 的現有權重已高於信心份額。
OVERRIDDEN_INTENTS = ("hold", "add")  # Agent 沒打算賣，卻出現賣單。
SELL_SIDE, BUY_SIDE = "sell", "buy"


def _read(path: Path) -> Mapping[str, object]:
    try:
        return require_mapping(json.loads(path.read_text(encoding="utf-8")), path.name)
    except (OSError, json.JSONDecodeError) as error:
        raise CalibrationError("無法讀取 %s" % path) from error


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CalibrationError("%s 必須是非空字串" % label)
    return value


def _amount(value: object, label: str) -> Decimal:
    amount = parse_decimal(value, label, error=CalibrationError, reject_bool=True)
    if amount < 0:
        raise CalibrationError("%s 不得為負數" % label)
    return amount


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP), "f")


def _percent(part: Decimal, whole: Decimal) -> Optional[str]:
    if whole == 0:
        return None
    return format((part * 100 / whole).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _load_run(run_dir: Path) -> Dict[str, object]:
    """驗證並整理一份 run；任何異常都拋 CalibrationError。"""
    manifest = _read(run_dir / "manifest.json")
    store = ImmutableRunStore(run_dir.parent, schema_version=str(manifest.get("schema_version")), error=CalibrationError)
    store.verify(run_dir.name)
    listed = require_mapping(manifest.get("artifacts"), "manifest.artifacts")
    for name in REQUIRED_ARTIFACTS:
        if name not in listed:
            raise CalibrationError("%s 的 manifest 缺少 %s" % (run_dir.name, name))
    intent = _read(run_dir / "intent.json")
    proposal = _read(run_dir / "proposal.json")
    label = run_dir.name
    if intent.get("status") != "completed":
        raise CalibrationError("%s 的交易意圖尚未完成" % label)
    if proposal.get("status") != "completed":
        raise CalibrationError("%s 的配置提案不是 completed，沒有可統計的訂單" % label)
    for field in ("snapshot_id", "decision_cutoff"):
        if intent.get(field) != proposal.get(field):
            raise CalibrationError("%s 的 intent 與 proposal %s 不一致" % (label, field))
    if intent.get("decision_id") != proposal.get("trade_intent_result_id"):
        raise CalibrationError("%s 的 proposal 不是由這份 intent 產生" % label)

    items: Dict[str, Mapping[str, object]] = {}
    for item in require_sequence(intent.get("items"), "%s intent.items" % label):
        item = require_mapping(item, "%s intent.items[]" % label)
        symbol = _text(item.get("symbol"), "%s intent.items[].symbol" % label)
        _text(item.get("intent"), "%s %s intent" % (label, symbol))
        if symbol in items:
            raise CalibrationError("%s 的 intent 重複出現 %s" % (label, symbol))
        items[symbol] = item
    if not items:
        raise CalibrationError("%s 的交易意圖沒有任何股票" % label)

    order_proposal = require_mapping(proposal.get("order_proposal"), "%s order_proposal" % label)
    buys: Dict[str, Mapping[str, object]] = {}
    sells: Dict[str, Mapping[str, object]] = {}
    for order in require_sequence(order_proposal.get("orders"), "%s order_proposal.orders" % label):
        order = require_mapping(order, "%s orders[]" % label)
        symbol = _text(order.get("symbol"), "%s orders[].symbol" % label)
        side = order.get("side")
        if side not in (BUY_SIDE, SELL_SIDE):
            raise CalibrationError("%s 的 %s 訂單方向不合法：%r" % (label, symbol, side))
        if symbol not in items:
            raise CalibrationError("%s 的訂單含 intent 沒有的股票：%s" % (label, symbol))
        _amount(order.get("gross_amount"), "%s %s gross_amount" % (label, symbol))
        target = buys if side == BUY_SIDE else sells
        if symbol in target:
            raise CalibrationError("%s 的 %s 出現重複的%s單" % (label, symbol, side))
        target[symbol] = order

    flags: Dict[str, List[str]] = {}
    flag_counts: Counter = Counter()
    raw_flags = proposal.get("constraint_flags")
    for flag in require_sequence(raw_flags if raw_flags is not None else [], "%s constraint_flags" % label):
        flag = require_mapping(flag, "%s constraint_flags[]" % label)
        code = _text(flag.get("code"), "%s constraint_flags[].code" % label)
        flag_counts[code] += 1
        symbol = flag.get("symbol")
        if symbol is not None:
            flags.setdefault(_text(symbol, "%s constraint_flags[].symbol" % label), []).append(code)

    policy_id: object = UNRECORDED
    if "policy" in listed:
        policy_id = _read(run_dir / "policy.json").get("policy_id")
        # 封存的 policy_id 是「策略識別＋交易決策識別」（trader.apply_trade_decision 綁定），分段只看策略識別。
        policy_id = _text(policy_id, "%s policy.policy_id" % label).split("+trade-decision:")[0] if policy_id is not None else UNRECORDED
    date = run_dir.parent.name
    return {"policy_id": policy_id, "run_id": label, "date": date, "items": items, "buys": buys, "sells": sells, "flags": flags, "flag_counts": flag_counts}


def _summarize_run(run: Mapping[str, object]) -> Dict[str, object]:
    items, buys, sells, flags = run["items"], run["buys"], run["sells"], run["flags"]
    signals = [symbol for symbol in sorted(items) if items[symbol]["intent"] in SIGNAL_INTENTS]
    landed = [symbol for symbol in signals if symbol in buys]
    reasons: Counter = Counter()
    unlanded: List[Dict[str, object]] = []
    for symbol in signals:
        if symbol in buys:
            continue
        codes = sorted(set(flags.get(symbol, []))) or [NO_FLAG]
        reasons["+".join(codes)] += 1
        unlanded.append({"symbol": symbol, "intent": items[symbol]["intent"], "conviction": items[symbol].get("conviction"), "flags": codes})
    by_conviction: Dict[str, Dict[str, int]] = {}
    for symbol in signals:
        conviction = items[symbol].get("conviction")
        bucket = by_conviction.setdefault(conviction if isinstance(conviction, str) and conviction else UNRECORDED, {"landed": 0, "unlanded": 0})
        bucket["landed" if symbol in buys else "unlanded"] += 1
    sell_total = sum((_amount(order["gross_amount"], "gross_amount") for order in sells.values()), Decimal(0))
    overrides = []
    override_total = Decimal(0)
    for symbol in sorted(sells):
        item = items[symbol]
        if item["intent"] not in OVERRIDDEN_INTENTS:
            continue
        order = sells[symbol]
        amount = _amount(order["gross_amount"], "gross_amount")
        override_total += amount
        overrides.append({
            "symbol": symbol, "agent_intent": item["intent"], "agent_conviction": item.get("conviction"),
            "order_intent": order.get("intent"), "shares": order.get("shares"), "gross_amount": _money(amount),
            "flags": sorted(set(flags.get(symbol, []))),
        })
    return {
        "date": run["date"], "run_id": run["run_id"], "policy_id": run["policy_id"],
        "buy_add_intents": len(signals), "landed": len(landed), "unlanded": len(unlanded),
        "unlanded_reasons": dict(sorted(reasons.items())),
        "unlanded_items": unlanded,
        "by_conviction": {key: by_conviction[key] for key in sorted(by_conviction)},
        "flag_counts": dict(sorted(run["flag_counts"].items())),
        "sell_orders": len(sells), "sell_gross_amount": _money(sell_total),
        "overridden_sells": overrides,
        "overridden_sell_gross_amount": _money(override_total),
        "overridden_sell_share_percent": _percent(override_total, sell_total),
    }


def _segment(days: List[Mapping[str, object]]) -> Dict[str, object]:
    """把一組每日摘要加總成彙總（全期合計與每個 policy_id 分段共用，所以分段加總必等於合計）。"""
    reasons: Counter = Counter()
    by_conviction: Dict[str, Counter] = {}
    sell_total = override_total = Decimal(0)
    for day in days:
        reasons.update(day["unlanded_reasons"])
        for conviction, counts in day["by_conviction"].items():
            by_conviction.setdefault(conviction, Counter()).update(counts)
        sell_total += Decimal(day["sell_gross_amount"])
        override_total += Decimal(day["overridden_sell_gross_amount"])
    total = sum(day["buy_add_intents"] for day in days)
    landed = sum(day["landed"] for day in days)
    return {
        "run_count": len(days), "first_date": days[0]["date"], "last_date": days[-1]["date"],
        "buy_add_intents": total, "landed": landed, "unlanded": total - landed,
        "by_conviction": {
            key: {"landed": by_conviction[key].get("landed", 0), "unlanded": by_conviction[key].get("unlanded", 0)}
            for key in sorted(by_conviction)
        },
        "unlanded_reasons": dict(sorted(reasons.items())),
        "sell_gross_amount": _money(sell_total),
        "overridden_sell_gross_amount": _money(override_total),
    }


def intent_landing_report(decisions_root: Path, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, object]:
    """統計決策庫內已封存 run（`since`／`until` 為 YYYY-MM-DD，只算該日含以後／含以前；`until` 供回歸測試固定樣本範圍）。唯讀；任何一份不合格即拒絕整批。"""
    if since is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        raise CalibrationError("since 格式必須是 YYYY-MM-DD")
    if until is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", until):
        raise CalibrationError("until 格式必須是 YYYY-MM-DD")
    run_dirs = [
        run for run in list_run_dirs(Path(decisions_root))
        if (since is None or run.parent.name >= since) and (until is None or run.parent.name <= until)
    ]
    if not run_dirs:
        raise CalibrationError("決策庫沒有符合條件的 run：%s" % decisions_root)
    days = [_summarize_run(_load_run(run_dir)) for run_dir in run_dirs]
    flag_counts: Counter = Counter()
    for day in days:
        flag_counts.update(day["flag_counts"])
    overall = _segment(days)
    segments: List[Dict[str, object]] = []
    for policy_id in dict.fromkeys(day["policy_id"] for day in days):  # 依首次出現的先後，不排序
        segments.append({"policy_id": policy_id, **_segment([day for day in days if day["policy_id"] == policy_id])})
    return {
        "report_version": REPORT_VERSION,
        "evidence_grade": REPLAY_EVIDENCE_GRADE,
        "notice": REPLAY_NOTICE,
        "landing_definition": "Agent 標 buy／add 的股票，最終有買單即算落地（不論張數是否被縮減）；未落地原因取該股在 constraint_flags 的全部代號",
        "since": since,
        "run_count": len(days),
        "totals": {
            "buy_add_intents": overall["buy_add_intents"], "landed": overall["landed"], "unlanded": overall["unlanded"],
            "by_conviction": overall["by_conviction"],
            "unlanded_reasons": overall["unlanded_reasons"],
            "flag_counts": dict(sorted(flag_counts.items())),
            "sell_gross_amount": overall["sell_gross_amount"],
            "overridden_sell_gross_amount": overall["overridden_sell_gross_amount"],
        },
        "by_policy": segments,
        "days": days,
    }
