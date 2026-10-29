"""入選排序：同一信心等級內，由交易 Agent 的第二輪一次看全部候選後決定先後。

交易 Agent 分批執行、各批看不到其他批的候選，所以信心等級只有 high／medium／low 三級，同等級候選的
先後不能由它決定。本模組讓另一個 Agent 在所有批次合併後一次看到全部 buy／add 候選，**只在同等級內**
排先後並附理由；它不改信心、不新增事實、不輸出任何數字（先後就是陣列順序）。

程式把順序轉成 ``position_sizing.rank_by_symbol``（每個等級內從 1 開始），配置時排序鍵為
（信心等級、排序名次、代號）；沒有排序（舊 policy）時沿用原本的（信心、支持數、selection_tiebreak）規則。
Finalizer 以交易決策重建驗證：排序必須剛好涵蓋每個等級的 buy／add 股票，不多不少。
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence

from .contracts import (
    DecisionToolError,
    artifact_content_sha256,
    reject_unknown_fields,
    required_string,
    validate_forbidden_keys,
)
from .sizing import CONVICTION_LEVELS
from .trader import SIZED


SELECTION_RANKING_VERSION = "1.0"
RANKING_POLICY_VERSION = "v1"
ENVELOPE_FIELDS = {
    "schema_version", "ranking_id", "bundle_id", "snapshot_id", "decision_cutoff", "bundle_hash",
    "trade_decision_id", "trade_decision_sha256", "status", "tiers", "errors", "content_sha256",
}
TIER_FIELDS = {"conviction", "ordered"}
ENTRY_FIELDS = {"symbol", "reason"}
PROGRAM_REASON = "該信心等級只有一檔候選，由程式直接排定。"


def candidates_by_conviction(trade_decision: Mapping[str, object]) -> Dict[str, List[str]]:
    """交易決策中 buy／add 的股票，依信心等級分組；組內依代號排序以得到確定的順序。"""
    tiers: Dict[str, List[str]] = {level: [] for level in CONVICTION_LEVELS}
    for item in trade_decision.get("items", []):
        if isinstance(item, Mapping) and item.get("intent") in SIZED and item.get("conviction") in tiers:
            tiers[str(item["conviction"])].append(str(item["symbol"]).upper())
    return {level: sorted(symbols) for level, symbols in tiers.items() if symbols}


def needs_agent(trade_decision: Mapping[str, object]) -> bool:
    """只有某個等級有兩檔以上候選時才有「先後」可排；否則不呼叫 Agent。"""
    return any(len(symbols) > 1 for symbols in candidates_by_conviction(trade_decision).values())


def ranking_envelope(bundle: Mapping[str, object], trade_decision: Mapping[str, object], ranking_id: str) -> Dict[str, object]:
    return {
        "schema_version": SELECTION_RANKING_VERSION,
        "ranking_id": ranking_id,
        "bundle_id": bundle["bundle_id"],
        "snapshot_id": bundle["snapshot_id"],
        "decision_cutoff": bundle["decision_cutoff"],
        "bundle_hash": bundle["bundle_sha256"],
        "trade_decision_id": trade_decision["decision_id"],
        "trade_decision_sha256": trade_decision["content_sha256"],
    }


def seal_selection_ranking(envelope: Mapping[str, object], tiers: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    ranking = {
        **dict(envelope),
        "status": "completed",
        "tiers": [
            {"conviction": tier["conviction"], "ordered": [dict(entry) for entry in tier["ordered"]]}
            for tier in tiers
        ],
        "errors": [],
    }
    ranking["content_sha256"] = artifact_content_sha256(ranking)
    return ranking


def program_ranking(envelope: Mapping[str, object], trade_decision: Mapping[str, object]) -> Dict[str, object]:
    """沒有任何等級有兩檔以上候選時，由程式直接封存排序（不呼叫 Agent）。"""
    tiers = [
        {"conviction": level, "ordered": [{"symbol": symbol, "reason": PROGRAM_REASON} for symbol in symbols]}
        for level, symbols in candidates_by_conviction(trade_decision).items()
    ]
    return seal_selection_ranking(envelope, tiers)


class SelectionRankingValidator:
    """排序必須綁定同一份交易決策，並剛好涵蓋每個信心等級的 buy／add 股票。"""

    def __init__(self, bundle: Mapping[str, object], trade_decision: Mapping[str, object]):
        self.bundle = dict(bundle)
        self.trade = dict(trade_decision)
        self.expected = candidates_by_conviction(trade_decision)

    def validate(self, ranking: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        if not isinstance(ranking, Mapping):
            return ["SelectionRanking 必須是物件"]
        validate_forbidden_keys(ranking, "SelectionRanking", errors)
        reject_unknown_fields(ranking, ENVELOPE_FIELDS, "SelectionRanking", errors)
        try:
            expected = ranking_envelope(self.bundle, self.trade, str(ranking.get("ranking_id")))
        except KeyError as error:
            return errors + ["SelectionRanking 無法建立信封：缺少 %s" % error]
        for field, value in expected.items():
            if ranking.get(field) != value:
                errors.append("SelectionRanking.%s 與交易決策不一致" % field)
        try:
            required_string(ranking, "ranking_id")
        except DecisionToolError as error:
            errors.append("SelectionRanking.%s" % error)
        if ranking.get("status") != "completed":
            errors.append("只有 status=completed 的 SelectionRanking 可進入配置")
        if ranking.get("errors") != []:
            errors.append("SelectionRanking.errors 必須為空陣列")
        if ranking.get("content_sha256") != artifact_content_sha256(ranking):
            errors.append("SelectionRanking.content_sha256 與內容不一致")
        tiers = ranking.get("tiers")
        if not isinstance(tiers, list):
            return errors + ["SelectionRanking.tiers 必須是陣列"]
        seen_levels: List[str] = []
        for index, tier in enumerate(tiers):
            prefix = "tiers[%d]" % index
            if not isinstance(tier, Mapping):
                errors.append("%s 必須是物件" % prefix)
                continue
            reject_unknown_fields(tier, TIER_FIELDS, prefix, errors)
            level = tier.get("conviction")
            if level not in CONVICTION_LEVELS:
                errors.append("%s.conviction 必須是 %s" % (prefix, "／".join(CONVICTION_LEVELS)))
                continue
            seen_levels.append(str(level))
            ordered = tier.get("ordered")
            if not isinstance(ordered, list):
                errors.append("%s.ordered 必須是陣列" % prefix)
                continue
            symbols: List[str] = []
            for position, entry in enumerate(ordered):
                label = "%s.ordered[%d]" % (prefix, position)
                if not isinstance(entry, Mapping):
                    errors.append("%s 必須是物件" % label)
                    continue
                reject_unknown_fields(entry, ENTRY_FIELDS, label, errors)
                symbols.append(str(entry.get("symbol", "")).upper())
                try:
                    required_string(entry, "reason")
                except DecisionToolError as error:
                    errors.append("%s.%s" % (label, error))
            if len(symbols) != len(set(symbols)):
                errors.append("%s 股票不得重複" % prefix)
            wanted = set(self.expected.get(str(level), []))
            if set(symbols) != wanted:
                missing = sorted(wanted - set(symbols))
                extra = sorted(set(symbols) - wanted)
                if missing:
                    errors.append("%s 缺少該信心等級的候選：%s" % (prefix, ", ".join(missing)))
                if extra:
                    errors.append("%s 含不屬於該信心等級的股票：%s" % (prefix, ", ".join(extra)))
        if len(seen_levels) != len(set(seen_levels)):
            errors.append("SelectionRanking.tiers 信心等級不得重複")
        for level in sorted(set(self.expected) - set(seen_levels)):
            errors.append("SelectionRanking 缺少信心等級 %s 的排序" % level)
        return errors


def rank_by_symbol(ranking: Mapping[str, object]) -> Dict[str, int]:
    """每個信心等級內從 1 開始的名次；只比較同等級，跨等級的名次沒有意義。"""
    result: Dict[str, int] = {}
    for tier in ranking.get("tiers", []):
        for position, entry in enumerate(tier["ordered"], start=1):
            result[str(entry["symbol"]).upper()] = position
    return result


def apply_selection_ranking(policy: Mapping[str, object], ranking: Mapping[str, object]) -> Dict[str, object]:
    """把排序名次綁進已套用交易決策的 policy；呼叫端重新封存驗證。沒有啟用 selection_ranking 的 policy 拒絕。"""
    sizing = policy.get("position_sizing")
    if not isinstance(sizing, Mapping) or sizing.get("selection_ranking") != RANKING_POLICY_VERSION:
        raise DecisionToolError("DecisionPolicy 未啟用 position_sizing.selection_ranking")
    if "conviction_by_symbol" not in sizing:
        raise DecisionToolError("排序必須在交易決策與現金姿態綁定之後套用")
    result = dict(policy)
    result["position_sizing"] = {
        **dict(sizing),
        "rank_by_symbol": rank_by_symbol(ranking),
        "selection_ranking_id": ranking["ranking_id"],
        "selection_ranking_sha256": ranking["content_sha256"],
    }
    result.pop("content_sha256", None)
    return result


def selection_ranking_brief(
    trade_decision: Mapping[str, object],
    debate: Mapping[str, object],
    analyst_reports: Mapping[str, Mapping[str, object]],
    momentum: Mapping[str, object],
) -> List[Dict[str, object]]:
    """排序 Agent 的候選資料：交易 Agent 的判斷、多空強度、三位分析師方向與程式算好的動能與波動。"""
    strengths = {
        str(packet["role"]): {str(item["symbol"]).upper(): item.get("strength") for item in packet["items"]}
        for packet in debate["packets"]
    }
    outlooks = {
        name: {str(item["symbol"]).upper(): item.get("outlook") for item in report.get("items", [])}
        for name, report in analyst_reports.items()
    }
    momentum_by_symbol = {str(item.get("symbol", "")).upper(): item for item in momentum.get("items", [])}
    rows: List[Dict[str, object]] = []
    for item in trade_decision.get("items", []):
        if not isinstance(item, Mapping) or item.get("intent") not in SIZED:
            continue
        symbol = str(item["symbol"]).upper()
        measured = momentum_by_symbol.get(symbol, {})
        rows.append({
            "symbol": symbol,
            "intent": item["intent"],
            "conviction": item["conviction"],
            "trader_rationale": item["rationale"],
            "invalidation_conditions": item.get("invalidation_conditions", []),
            "bull_strength": strengths.get("bull", {}).get(symbol),
            "bear_strength": strengths.get("bear", {}).get(symbol),
            "analyst_outlooks": {name: values.get(symbol) for name, values in outlooks.items()},
            "return_20d": measured.get("return_20d"),
            "atr_14_pct": measured.get("atr_14_pct"),
        })
    return sorted(rows, key=lambda row: (CONVICTION_LEVELS.index(str(row["conviction"])), row["symbol"]))
