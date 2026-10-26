"""分析團隊：確定性輸入摘要、AnalystReport 契約與 Validator。

依「確定性歸程式，不確定性歸 LLM」：指標、財務比率與事件清單由程式整理；分析師
只輸出有限選項的 ``outlook``、事件 ``materiality`` 與引用證據的文字發現，不得
產生或改寫數字。情緒分析在沒有核准來源時由程式確定性輸出 unavailable。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
from typing import Dict, List, Mapping, Optional, Sequence, Set

from etf_agent.fundamentals import FundamentalMetricsCalculator, FundamentalSnapshotTools
from etf_agent.core import canonical_sha256, parse_decimal, decimal_string, parse_aware_time
from etf_agent.fundamentals.contracts import METRIC_KEYS, POLICY_VERSION, REQUIRED_STATEMENT_TYPES
from .trend_history import TechnicalTrendHistoryTools
from .fundamental_language import chinese_gap_explanations
from .event_financial_context import event_financial_context
from .cash_flow_context import cash_flow_context

from .contracts import (
    validate_forbidden_keys,
    DecisionContext,
    DecisionToolError,
    artifact_content_sha256,
    reject_unknown_fields,
    required_string,
    string_list,
)


ANALYST_SCHEMA_VERSION = "2.0"
ANALYSTS = ("technical", "fundamental", "event")
LEGACY_ANALYSTS = (*ANALYSTS, "sentiment")
EVENT_SENTIMENT_VERSION = "2.1"
EVENT_MARKET_VERSION = "2.2"
# 2.3：決策輸入含已核准的 FinMind 個股新聞時，新聞與重大訊息一起逐則分級；沒有新聞鍵的舊輸入仍為 2.2。
EVENT_NEWS_VERSION = "2.3"
MARKET_SCOPED_VERSIONS = (EVENT_MARKET_VERSION, EVENT_NEWS_VERSION)
LLM_ANALYSTS = ("technical", "fundamental", "event")
OUTLOOKS = ("positive", "negative", "neutral", "unknown")
MATERIALITY = ("high", "medium", "low", "unknown")
NO_SENTIMENT_SOURCE = "NO_LICENSED_SENTIMENT_SOURCE"
_EVENT_BODY_LIMIT = 1500
_EVENT_EXCERPT_HEAD = 500
_EVENT_EXCERPT_TAIL = 160
_MOMENTUM_FIELDS = (
    "close", "return_5d", "return_20d", "return_60d", "ma20", "ma60", "above_ma20", "above_ma60",
    "atr_14_pct", "downside_volatility_20d", "average_traded_value_20d",
)
TECHNICAL_GATE_VERSION = "1.0"
TECHNICAL_GATE_RULE = (
    "momentum 狀態為 available，且收盤同時在 MA20 與 MA60 之上、5／20／60 日報酬皆為正，"
    "或同時在兩條均線之下、三段報酬皆為負；其餘（含資料不足、訊號互相衝突）一律附完整歷史。"
)
_REVENUE_FIELDS = ("revenue_period", "yoy_pct", "mom_pct", "cumulative_yoy_pct")


def _short_number(value: object) -> object:
    """僅為閱讀縮短小數位；原始數值仍在 Snapshot／指標 artifact 中可重算。"""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return value
    return str(number.quantize(Decimal("0.01"))) if number.is_finite() else value


def technical_trend_gate(item: Mapping[str, object]) -> str:
    """只用 momentum 已算好的欄位判斷趨勢是否一致；回傳 clear_up／clear_down／mixed。"""
    if item.get("status") != "available":
        return "mixed"
    flags = (item.get("above_ma20"), item.get("above_ma60"))
    returns = [item.get(field) for field in ("return_5d", "return_20d", "return_60d")]
    if any(isinstance(flag, bool) is False for flag in flags) or any(
        isinstance(value, bool) or not isinstance(value, (int, float)) for value in returns
    ):
        return "mixed"
    if all(flags) and all(value > 0 for value in returns):
        return "clear_up"
    if not any(flags) and all(value < 0 for value in returns):
        return "clear_down"
    return "mixed"


def _symbols(bundle: Mapping[str, object]) -> List[str]:
    return sorted(
        str(row.get("symbol", "")).upper()
        for row in bundle["snapshot"].get("latest_prices", [])
        if isinstance(row, Mapping)
    )


def _documents(bundle: Mapping[str, object], document_type: str) -> Dict[str, List[Mapping[str, object]]]:
    result: Dict[str, List[Mapping[str, object]]] = {}
    for document in bundle["snapshot"].get("documents", []):
        if isinstance(document, Mapping) and document.get("document_type") == document_type and document.get("symbol"):
            if document_type == "material_event":
                cutoff = parse_aware_time(bundle["decision_cutoff"], "decision_cutoff", error=DecisionToolError)
                available = parse_aware_time(document.get("available_at"), "事件資料可得時間", error=DecisionToolError)
                published = parse_aware_time(document.get("published_at"), "事件發布時間", error=DecisionToolError)
                if available > cutoff or published > available:
                    raise DecisionToolError("事件時間超過截止時間或發布時間晚於可得時間")
            result.setdefault(str(document["symbol"]).upper(), []).append(document)
    return result


def company_news(bundle: Mapping[str, object]) -> Optional[Dict[str, List[Dict[str, object]]]]:
    """決策輸入沒有個股新聞鍵時回傳 None；有鍵但沒有新聞時回傳空 dict。"""
    if "company_news_input" not in bundle:
        return None
    from etf_agent.perception.company_news import news_by_symbol
    return news_by_symbol(bundle["company_news_input"])


def event_market_version(bundle: Mapping[str, object]) -> str:
    return EVENT_NEWS_VERSION if "company_news_input" in bundle else EVENT_MARKET_VERSION


def report_envelope(bundle: Mapping[str, object], analyst: str, report_id: str) -> Dict[str, object]:
    return {
        "schema_version": ANALYST_SCHEMA_VERSION,
        "report_id": report_id,
        "analyst": analyst,
        "bundle_id": bundle["bundle_id"],
        "snapshot_id": bundle["snapshot_id"],
        "decision_cutoff": bundle["decision_cutoff"],
        "bundle_hash": bundle["bundle_sha256"],
    }


def analyst_report_names(reports: Mapping[str, object]) -> Sequence[str]:
    """新鏈三份合併報告；保留舊四份封存輸入的原始重建路徑。"""
    if any(not isinstance(report, Mapping) for report in reports.values()):
        raise DecisionToolError("分析報告必須是物件")
    if set(reports) == set(LEGACY_ANALYSTS):
        if reports["event"].get("schema_version") != ANALYST_SCHEMA_VERSION:
            raise DecisionToolError("舊四份報告不能混用新版合併事件報告")
        return LEGACY_ANALYSTS
    if set(reports) == set(ANALYSTS):
        if reports["event"].get("schema_version") not in {EVENT_SENTIMENT_VERSION, *MARKET_SCOPED_VERSIONS}:
            raise DecisionToolError("三份報告必須包含新版事件與市場情緒報告")
        return ANALYSTS
    raise DecisionToolError("分析團隊需要技術、基本面、事件與市場情緒三份報告")


def build_analyst_brief(
    bundle: Mapping[str, object],
    momentum: Mapping[str, object],
    analyst: str,
    fundamental_metrics: Optional[Mapping[str, object]] = None,
    *, event_market_scope: bool = False, compact_technical: bool = False,
    technical_gate: bool = False,
) -> Dict[str, object]:
    """只重排共同輸入中已存在的事實與程式計算結果，供分析師閱讀。"""
    if analyst not in LLM_ANALYSTS:
        raise DecisionToolError("沒有 %s 分析師的 LLM 輸入摘要" % analyst)
    momentum_by_symbol = {
        str(item.get("symbol", "")).upper(): item
        for item in momentum.get("items", [])
        if isinstance(item, Mapping)
    }
    metrics_by_symbol = {
        str(item.get("symbol", "")).upper(): item
        for item in (fundamental_metrics or {}).get("items", [])
        if isinstance(item, Mapping)
    }
    revenues = _documents(bundle, "monthly_revenue")
    events = _documents(bundle, "material_event")
    news = company_news(bundle) if analyst == "event" and event_market_scope else None
    sentiment_by_symbol = {item["symbol"]: item for item in sentiment_report(bundle)["items"]} if analyst == "event" else {}
    financial_by_symbol = event_financial_context(bundle) if analyst == "event" else {}
    cash_flow_by_symbol = cash_flow_context(bundle) if analyst == "fundamental" else {}
    trend_tools = TechnicalTrendHistoryTools(bundle) if analyst == "technical" else None
    symbols: List[Dict[str, object]] = []
    for symbol in _symbols(bundle):
        entry: Dict[str, object] = {"symbol": symbol}
        if analyst == "technical":
            item = momentum_by_symbol.get(symbol, {})
            entry["momentum"] = {
                "status": item.get("status"),
                "evidence_ids": item.get("evidence_ids", []),
                **{field: _short_number(item[field]) if not isinstance(item.get(field), bool) else item[field]
                   for field in _MOMENTUM_FIELDS if field in item},
            }
            gate = technical_trend_gate(item) if technical_gate else "mixed"
            if gate != "mixed":
                # 趨勢一致時不附逐日歷史；完整序列仍在 DecisionInput，Validator 以原始資料驗證引用。
                entry["trend_history"] = {
                    "mode": "momentum_only", "gate": gate, "gate_version": TECHNICAL_GATE_VERSION,
                    "evidence_ids": item.get("evidence_ids", []),
                    "note": "趨勢一致，未附逐日歷史；判斷請依 momentum 欄位，不要引用未提供的歷史走勢。",
                }
            else:
                entry["trend_history"] = (trend_tools.summary_for_symbol(symbol) if compact_technical
                                          else trend_tools.for_symbol(symbol))
        elif analyst == "fundamental":
            metrics = metrics_by_symbol.get(symbol, {})
            entry["metrics"] = [
                {
                    "metric_key": metric.get("metric_key"),
                    "value": _short_number(metric.get("value")),
                    "unit": metric.get("unit"),
                    "period": metric.get("period"),
                    "comparison_period": metric.get("comparison_period"),
                    "evidence_ids": sorted(
                        {str(dep.get("source_evidence_id")) for dep in metric.get("dependencies", []) if dep.get("source_evidence_id")}
                    ),
                }
                for metric in metrics.get("metrics", [])
                if metric.get("status") == "available"
            ]
            entry["unavailable_metrics"] = [
                "%s:%s" % (metric.get("metric_key"), metric.get("reason_code"))
                for metric in metrics.get("metrics", [])
                if metric.get("status") != "available"
            ]
            entry["data_gap_explanations"] = chinese_gap_explanations(
                metrics.get("metrics", []), bundle["snapshot"].get("documents", []), symbol
            )
            entry["financial_status"] = metrics.get("status", "unavailable")
            # 策略文件：基本面看現金流。數值由程式從已核對的現金流文件計算，缺少時明確說明。
            entry["cash_flow"] = cash_flow_by_symbol.get(symbol) or {
                "status": "unavailable",
                "reason": "快照沒有本股票已核對的現金流量表（金融保險業或資料商尚未提供）",
            }
            entry["financial_facts"] = []
            for document in _documents(bundle, "financial_statement").get(symbol, []):
                statement = document.get("financial_statement", {})
                if (statement.get("industry") not in {"fh", "basi", "bd", "ins"}
                        or statement.get("statement_type") != "income_statement"
                        or "%sQ%s" % (statement.get("fiscal_year"), statement.get("fiscal_quarter")) != metrics.get("period")):
                    continue
                for key, label in (("net_income", "稅後淨利"), ("net_revenue", "淨收益")):
                    fact = statement.get("facts", {}).get(key, {})
                    if fact.get("value_status") != "provided":
                        continue
                    value = parse_decimal(fact.get("value"), key, error=DecisionToolError)
                    entry["financial_facts"].append({
                        "fact_key": key, "name": label, "value": decimal_string(value),
                        "currency": fact.get("currency"), "unit_multiplier": fact.get("unit_multiplier"),
                        "source_field": fact.get("source_field"), "fiscal_year": statement.get("fiscal_year"),
                        "fiscal_quarter": statement.get("fiscal_quarter"), "period_kind": statement.get("period_kind"),
                        "period_start": statement.get("period_start"), "period_end": statement.get("period_end"),
                        "evidence_ids": [document.get("source_evidence_id")],
                    })
            entry["source_limitations"] = sorted({
                "去年同期財報由資料商補抓。當期金額已核對官方。去年報表範圍及重編資訊尚未確認。"
                for document in bundle["snapshot"].get("documents", [])
                if document.get("symbol") == symbol
                and document.get("source") == "FINMIND_VERIFIED_PRIOR"
            })
            entry["company_profiles"] = []
            for document in _documents(bundle, "company_profile").get(symbol, []):
                if document.get("source") != "TWSE_COMPANY_PROFILE":
                    continue
                try:
                    profile = json.loads(str(document.get("body", "")))
                except (ValueError, TypeError):
                    continue
                if not isinstance(profile, Mapping) or profile.get("company_code") != symbol.split(".")[0]:
                    continue
                if not isinstance(profile.get("industry_code"), str) or not profile["industry_code"].strip():
                    continue
                entry["company_profiles"].append({"industry_code": profile["industry_code"],
                                                 "evidence_id": document.get("source_evidence_id"),
                                                 "available_at": document.get("available_at"),
                                                 "comparison_standard": None})
            entry["monthly_revenue"] = [
                {
                    "evidence_id": document.get("source_evidence_id"),
                    **{field: _short_number(document["monthly_revenue"].get(field)) if field != "revenue_period" else document["monthly_revenue"].get(field)
                       for field in _REVENUE_FIELDS},
                }
                for document in revenues.get(symbol, [])
                if isinstance(document.get("monthly_revenue"), Mapping)
            ]
        else:
            entry["financial_context"] = financial_by_symbol.get(symbol, [])
            if not event_market_scope:
                entry["sentiment"] = {key: value for key, value in sentiment_by_symbol[symbol].items() if key != "symbol"}
            entry["events"] = [
                {
                    "evidence_id": document.get("source_evidence_id"),
                    "title": document.get("title"),
                    "published_at": document.get("published_at"),
                    "available_at": document.get("available_at"),
                    "body": str(document.get("body") or "")[:_EVENT_BODY_LIMIT],
                    "body_truncated": len(str(document.get("body") or "")) > _EVENT_BODY_LIMIT,
                }
                for document in sorted(events.get(symbol, []), key=lambda doc: str(doc.get("published_at")))
            ]
            if news is not None:
                # 只有標題與媒體；可得時間是第一次抓取時間，FinMind 來源時間沒有時區，不當發布時間。
                entry["news"] = news.get(symbol, [])
        symbols.append(entry)
    brief = {
        "analyst": analyst,
        "report_envelope": {**report_envelope(bundle, analyst, "analyst-%s:%s" % (analyst, str(bundle["bundle_sha256"])[:16])),
                            "schema_version": (event_market_version(bundle) if event_market_scope else EVENT_SENTIMENT_VERSION) if analyst == "event" else ANALYST_SCHEMA_VERSION},
        "regime_assessment": momentum.get("regime_assessment") if analyst == "technical" else None,
        "symbols": symbols,
    }
    if analyst == "technical" and technical_gate:
        gated = [row["symbol"] for row in symbols if row["trend_history"].get("mode") == "momentum_only"]
        brief["technical_gate"] = {
            "version": TECHNICAL_GATE_VERSION, "rule": TECHNICAL_GATE_RULE,
            "momentum_only_symbols": gated, "full_history_symbols": len(symbols) - len(gated),
        }
    if analyst == "event" and event_market_scope:
        from .market_scope import market_sentiment_channel
        # 全市場新聞已由獨立步驟逐則標註並由程式重建；這裡只給結果，不再把原文複製進每一批。
        brief["market_sentiment"] = market_sentiment_channel(bundle)
    return brief


def compact_event_batch(brief: Mapping[str, object], bundle: Mapping[str, object]) -> tuple[dict, dict]:
    """事件逐筆全覆蓋；摘要保留首尾，完整原文另以同 Snapshot 證據索引供查閱。"""
    if brief.get("analyst") != "event":
        raise DecisionToolError("只有事件分析師輸入可以壓縮事件原文")
    members = {row["symbol"] for row in brief["symbols"]}
    source = {
        "snapshot_id": bundle["snapshot_id"],
        "decision_cutoff": bundle["decision_cutoff"],
        "events": [
            {key: document.get(key) for key in ("symbol", "source_evidence_id", "title", "published_at", "available_at", "body")}
            for document in bundle["snapshot"]["documents"]
            if document.get("document_type") == "material_event" and document.get("symbol") in members
        ],
    }
    source["content_sha256"] = canonical_sha256(source)
    output = {**brief, "symbols": []}
    for row in brief["symbols"]:
        compact = {**row, "events": []}
        for event in row["events"]:
            item = dict(event)
            body = str(item["body"])
            if len(body) > _EVENT_EXCERPT_HEAD + _EVENT_EXCERPT_TAIL:
                item["body"] = body[:_EVENT_EXCERPT_HEAD] + " …［中段請依 evidence_id 查完整來源］… " + body[-_EVENT_EXCERPT_TAIL:]
                item["brief_excerpt_truncated"] = True
            compact["events"].append(item)
        output["symbols"].append(compact)
    return output, source


def seal_report(envelope: Mapping[str, object], items: Sequence[Mapping[str, object]], status: str = "completed") -> Dict[str, object]:
    report = {**dict(envelope), "status": status, "items": [dict(item) for item in items], "errors": []}
    report["content_sha256"] = artifact_content_sha256(report)
    return report


def sentiment_unavailable_report(bundle: Mapping[str, object]) -> Dict[str, object]:
    """沒有已核准授權的情緒資料時，確定性標示全部 unknown，不以 LLM 猜測。"""
    envelope = report_envelope(bundle, "sentiment", "analyst-sentiment:%s" % str(bundle["bundle_sha256"])[:16])
    items = [
        {"symbol": symbol, "outlook": "unknown", "findings": [], "data_gaps": [NO_SENTIMENT_SOURCE]}
        for symbol in _symbols(bundle)
    ]
    return seal_report(envelope, items, status="unavailable")


def sentiment_report(bundle: Mapping[str, object]) -> Dict[str, object]:
    """將已驗證情緒轉成次級證據。"""
    from .contracts import DecisionInputValidator

    pairs = bundle.get("perception_inputs", [])
    if not pairs:
        return sentiment_unavailable_report(bundle)
    errors = DecisionInputValidator(bundle).validate()
    if errors:
        raise DecisionToolError("情緒共同輸入驗證失敗：" + "；".join(errors))
    items = []
    for symbol in _symbols(bundle):
        findings, directions = [], set()
        for pair in pairs:
            for row in pair["result"]["items"]:
                if row["symbol"].upper() != symbol or row["research_status"] != "usable_secondary":
                    continue
                sentiment = row["sentiment"]
                if sentiment["status"] == "available":
                    direction = sentiment["direction"]
                    directions.add(direction if direction in OUTLOOKS else "unknown")
                    findings.append({"finding_id": "sentiment:%s:%d" % (symbol, len(findings)),
                                     "text": "情緒方向：%s；去重樣本 %s 筆、來源 %s 個；僅作次級研究輸入。" %
                                             (direction, sentiment["item_count"], sentiment["source_count"]),
                                     "evidence_ids": sentiment["evidence_ids"]})
        outlook = next(iter(directions)) if len(directions) == 1 else "unknown"
        items.append({"symbol": symbol, "outlook": outlook, "findings": findings,
                      "data_gaps": [] if outlook != "unknown" else ["NO_UNAMBIGUOUS_SENTIMENT_DIRECTION"]})
    envelope = report_envelope(bundle, "sentiment", "analyst-sentiment:%s" % str(bundle["bundle_sha256"])[:16])
    return seal_report(envelope, items, status="completed" if any(x["findings"] for x in items) else "unavailable")


def seal_event_sentiment_report(bundle: Mapping[str, object], envelope: Mapping[str, object], items: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    """情緒通道由來源重建加入，LLM 不得覆寫情緒方向或數值。"""
    sentiment = {row["symbol"]: row for row in sentiment_report(bundle)["items"]}
    merged = []
    for item in items:
        row = dict(item)
        if "sentiment" in row:
            raise DecisionToolError("模型不得輸出或覆寫 sentiment 通道")
        source = sentiment.get(str(row.get("symbol", "")).upper(), {})
        row["sentiment"] = {key: value for key, value in source.items() if key != "symbol"}
        merged.append(row)
    return seal_report({**envelope, "schema_version": EVENT_SENTIMENT_VERSION}, merged)


def no_event_item(symbol: str, availability: Sequence[Mapping[str, object]], news_checked: bool = False) -> Dict[str, object]:
    """查詢視窗內沒有重大訊息（與核准新聞）的股票：事件看法必為 unknown、無發現，資料缺口由程式照資料庫狀態列出。"""
    gaps = ["查詢視窗內沒有重大訊息事件或核准媒體的個股新聞，無法判斷公司事件方向。" if news_checked
            else "查詢視窗內沒有重大訊息事件，無法判斷公司事件方向。"]
    for channel in availability:
        name, status = channel.get("name"), channel.get("status")
        if isinstance(name, str) and isinstance(status, str):
            gaps.append("%s：%s" % (name, status))
        elif isinstance(status, str):
            gaps.append(status)
    return {"symbol": symbol, "outlook": "unknown", "event_outlook": "unknown",
            "findings": [], "data_gaps": gaps, "events": []}


def seal_event_market_report(bundle, envelope, items):
    """公司事件逐檔交付，全市場情緒只附入一次；不沿用個股情緒。"""
    from .market_scope import market_sentiment_channel
    rows = [dict(item) for item in items]
    if any("sentiment" in row or "market_sentiment" in row for row in rows):
        raise DecisionToolError("公司項目不得輸出個股或全市場情緒通道")
    output = seal_report({**envelope, "schema_version": event_market_version(bundle)}, rows)
    output["market_sentiment"] = market_sentiment_channel(bundle)
    output["content_sha256"] = artifact_content_sha256(output)
    return output


class AnalystReportValidator:
    """檢查分析報告覆蓋全部交易池、引用歸屬正確且不含數字欄位。"""

    ENVELOPE = {
        "schema_version", "report_id", "analyst", "bundle_id", "snapshot_id", "decision_cutoff",
        "bundle_hash", "status", "items", "errors", "content_sha256",
    }
    ITEM = {"symbol", "outlook", "findings", "data_gaps"}
    FINDING = {"finding_id", "text", "evidence_ids"}
    EVENT = {"evidence_id", "materiality", "summary"}
    # 策略文件：公司事件要看「反證與尚待確認事項」。舊封存沒有這兩欄，照原樣驗證；有就必須是字串陣列。
    EVENT_REVIEW = ("counter_evidence", "pending_confirmations")

    def __init__(
        self, bundle: Mapping[str, object], analyst: str, symbols: Optional[Sequence[str]] = None
    ):
        """``symbols`` 指定時只驗證該批股票的覆蓋（逐批執行時用）；預設為全交易池。"""
        if analyst not in LEGACY_ANALYSTS:
            raise DecisionToolError("未知分析師：%s" % analyst)
        self.context = DecisionContext(bundle)
        self.bundle = dict(bundle)
        self.analyst = analyst
        self.universe = {str(symbol).upper() for symbol in (symbols if symbols is not None else _symbols(bundle))}
        self.events_by_symbol = {
            symbol: {str(document.get("source_evidence_id")) for document in documents}
            for symbol, documents in _documents(bundle, "material_event").items()
        }
        # 有核准個股新聞時，新聞與重大訊息一樣要逐則分級，也可作為公司事件方向的引用。
        for symbol, rows in (company_news(bundle) or {}).items() if analyst == "event" else ():
            self.events_by_symbol.setdefault(symbol, set()).update(row["evidence_id"] for row in rows)
        self.sentiment_by_symbol = {
            row["symbol"]: row for row in sentiment_report(bundle)["items"]
        } if analyst == "event" else {}
        self.event_financial_by_symbol = event_financial_context(bundle) if analyst == "event" else {}

    def validate(self, report: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        if self.analyst == "sentiment":
            if dict(report) != sentiment_report(self.bundle):
                errors.append("情緒分析報告與已驗證來源重建結果不一致")
        validate_forbidden_keys(report, "AnalystReport", errors)
        market_scoped = self.analyst == "event" and report.get("schema_version") in MARKET_SCOPED_VERSIONS
        reject_unknown_fields(report, self.ENVELOPE | ({"market_sentiment"} if market_scoped else set()), "AnalystReport", errors)
        expected = report_envelope(self.bundle, self.analyst, str(report.get("report_id")))
        merged = self.analyst == "event" and report.get("schema_version") == EVENT_SENTIMENT_VERSION
        if merged:
            expected["schema_version"] = EVENT_SENTIMENT_VERSION
        if market_scoped:
            from .market_scope import market_sentiment_channel
            expected["schema_version"] = event_market_version(self.bundle)
            if report.get("market_sentiment") != market_sentiment_channel(self.bundle):
                errors.append("全市場情緒與同截止時間的來源重建不一致")
        for field, value in expected.items():
            if report.get(field) != value:
                errors.append("AnalystReport.%s 與共同輸入不一致" % field)
        try:
            required_string(report, "report_id")
        except DecisionToolError as error:
            errors.append("AnalystReport.%s" % error)
        allowed_status = {"completed", "unavailable"} if self.analyst == "sentiment" else {"completed"}
        if report.get("status") not in allowed_status:
            errors.append("AnalystReport.status 必須是 %s" % "／".join(sorted(allowed_status)))
        if report.get("errors") != []:
            errors.append("AnalystReport.errors 必須為空陣列")
        if report.get("content_sha256") != artifact_content_sha256(report):
            errors.append("AnalystReport.content_sha256 與內容不一致")
        items = report.get("items")
        if not isinstance(items, list):
            return errors + ["AnalystReport.items 必須是陣列"]
        seen: List[str] = []
        finding_ids: Set[str] = set()
        for index, item in enumerate(items):
            prefix = "items[%d]" % index
            if not isinstance(item, Mapping):
                errors.append("%s 必須是物件" % prefix)
                continue
            allowed = self.ITEM | ({"events"} if self.analyst == "event" else set())
            if merged:
                allowed |= {"event_outlook", "sentiment"}
            if market_scoped:
                allowed |= {"event_outlook"}
            reject_unknown_fields(item, allowed, prefix, errors)
            symbol = str(item.get("symbol", "")).upper()
            seen.append(symbol)
            self._validate_item(item, symbol, prefix, finding_ids, errors)
            if merged:
                self._validate_merged(item, symbol, prefix, errors)
            if market_scoped:
                self._validate_company_event(item, symbol, prefix, errors)
        if len(seen) != len(set(seen)):
            errors.append("AnalystReport.items 股票不得重複")
        missing = sorted(self.universe - set(seen))
        extra = sorted(set(seen) - self.universe)
        if missing:
            errors.append("AnalystReport 未覆蓋交易池：%s" % ", ".join(missing))
        if extra:
            errors.append("AnalystReport 含本次範圍外股票：%s" % ", ".join(extra))
        return errors

    def _validate_company_event(self, item, symbol, prefix, errors):
        event_ids = self.events_by_symbol.get(symbol, set())
        financial_ids = {row["evidence_id"] for row in self.event_financial_by_symbol.get(symbol, [])}
        if item.get("event_outlook") != item.get("outlook"):
            errors.append("%s 公司結果必須與事件看法一致，不能混入市場情緒" % prefix)
        if not event_ids and item.get("outlook") != "unknown":
            errors.append("%s 無公司事件時不得形成事件方向" % prefix)
        cited = set()
        for finding in item.get("findings", []):
            if isinstance(finding, Mapping) and isinstance(finding.get("evidence_ids"), list):
                ids = {eid for eid in finding["evidence_ids"] if isinstance(eid, str)}
                cited |= ids
                if ids - event_ids - financial_ids:
                    errors.append("%s 公司事件只能引用本公司事件、核准新聞及同快照財報" % prefix)
        if item.get("outlook") in {"positive", "negative", "neutral"} and not cited.intersection(event_ids):
            errors.append("%s 公司事件結果缺少事件引用" % prefix)

    def _validate_merged(self, item: Mapping[str, object], symbol: str, prefix: str, errors: List[str]) -> None:
        source = self.sentiment_by_symbol.get(symbol, {})
        expected = {key: value for key, value in source.items() if key != "symbol"}
        if item.get("sentiment") != expected:
            errors.append("%s 情緒通道與已驗證來源重建不一致" % prefix)
        event_outlook = item.get("event_outlook")
        if event_outlook not in OUTLOOKS:
            errors.append("%s.event_outlook 必須是有效看法" % prefix)
        event_ids = self.events_by_symbol.get(symbol, set())
        if not event_ids and event_outlook != "unknown":
            errors.append("%s 無事件資料時事件看法必須為 unknown" % prefix)
        sentiment_ids = {eid for finding in source.get("findings", []) for eid in finding["evidence_ids"]}
        financial_ids = {row["evidence_id"] for row in self.event_financial_by_symbol.get(symbol, [])}
        sentiment_finding_ids = {finding["finding_id"] for finding in source.get("findings", [])}
        findings = item.get("findings", [])
        if not isinstance(findings, list):
            return
        cited = set()
        for finding in findings:
            if isinstance(finding, Mapping) and finding.get("finding_id") in sentiment_finding_ids:
                errors.append("%s 發現識別字不得與情緒通道重複" % prefix)
            if isinstance(finding, Mapping) and isinstance(finding.get("evidence_ids"), list):
                ids = {eid for eid in finding["evidence_ids"] if isinstance(eid, str)}
                cited |= ids
                if ids - event_ids - sentiment_ids - financial_ids:
                    errors.append("%s 發現只能引用本股票事件、同快照財報或已驗證情緒證據" % prefix)
        if event_outlook in {"positive", "negative", "neutral"} and not cited.intersection(event_ids):
            errors.append("%s 事件看法缺少事件引用" % prefix)
        if event_outlook == "unknown" and source.get("outlook", "unknown") == "unknown" and item.get("outlook") != "unknown":
            errors.append("%s 事件與情緒皆無方向時，綜合看法必須為 unknown" % prefix)

    def _validate_item(
        self, item: Mapping[str, object], symbol: str, prefix: str, finding_ids: Set[str], errors: List[str]
    ) -> None:
        outlook = item.get("outlook")
        if outlook not in OUTLOOKS:
            errors.append("%s.outlook 必須是 %s" % (prefix, "／".join(OUTLOOKS)))
        try:
            gaps = string_list(item, "data_gaps")
        except DecisionToolError as error:
            errors.append("%s.%s" % (prefix, error))
            gaps = []
        findings = item.get("findings")
        if not isinstance(findings, list):
            errors.append("%s.findings 必須是陣列" % prefix)
            findings = []
        if outlook == "unknown" and not gaps:
            errors.append("%s outlook=unknown 時必須說明 data_gaps" % prefix)
        if outlook in {"positive", "negative", "neutral"} and not findings:
            errors.append("%s outlook=%s 必須至少有一項 finding" % (prefix, outlook))
        for index, finding in enumerate(findings):
            finding_prefix = "%s.findings[%d]" % (prefix, index)
            if not isinstance(finding, Mapping):
                errors.append("%s 必須是物件" % finding_prefix)
                continue
            reject_unknown_fields(finding, self.FINDING, finding_prefix, errors)
            try:
                finding_id = required_string(finding, "finding_id")
                required_string(finding, "text")
                evidence = string_list(finding, "evidence_ids")
            except DecisionToolError as error:
                errors.append("%s.%s" % (finding_prefix, error))
                continue
            if finding_id in finding_ids:
                errors.append("%s.finding_id 在報告中重複：%s" % (finding_prefix, finding_id))
            finding_ids.add(finding_id)
            if not evidence:
                errors.append("%s.evidence_ids 不得為空" % finding_prefix)
            self.context.validate_evidence(evidence, symbol, finding_prefix, errors)
        if self.analyst == "event":
            self._validate_events(item.get("events"), symbol, prefix, errors)

    def _validate_events(self, events: object, symbol: str, prefix: str, errors: List[str]) -> None:
        if not isinstance(events, list):
            errors.append("%s.events 必須是陣列" % prefix)
            return
        expected = self.events_by_symbol.get(symbol, set())
        classified: List[str] = []
        for index, event in enumerate(events):
            event_prefix = "%s.events[%d]" % (prefix, index)
            if not isinstance(event, Mapping):
                errors.append("%s 必須是物件" % event_prefix)
                continue
            reject_unknown_fields(event, self.EVENT | set(self.EVENT_REVIEW), event_prefix, errors)
            for field in self.EVENT_REVIEW:
                if field in event:
                    try:
                        string_list(event, field)
                    except DecisionToolError as error:
                        errors.append("%s.%s" % (event_prefix, error))
            evidence_id = str(event.get("evidence_id", ""))
            if evidence_id not in expected:
                errors.append("%s 不是 %s 的重大訊息或核准新聞：%s" % (event_prefix, symbol, evidence_id))
            classified.append(evidence_id)
            if event.get("materiality") not in MATERIALITY:
                errors.append("%s.materiality 必須是 %s" % (event_prefix, "／".join(MATERIALITY)))
            try:
                required_string(event, "summary")
            except DecisionToolError as error:
                errors.append("%s.%s" % (event_prefix, error))
        if len(classified) != len(set(classified)):
            errors.append("%s.events 不得重複分級同一則訊息" % prefix)
        missing = sorted(expected - set(classified))
        if missing:
            errors.append("%s 未分級全部重大訊息與新聞：%s" % (prefix, ", ".join(missing)))


def compute_fundamental_metrics(bundle: Mapping[str, object]) -> Optional[Dict[str, object]]:
    """以 Snapshot 內最新的財報期別確定性計算全部股票的基本面指標；沒有財報時回傳 None。"""
    snapshot = bundle["snapshot"]
    periods = {
        (int(document["financial_statement"]["fiscal_year"]), int(document["financial_statement"]["fiscal_quarter"]))
        for document in snapshot.get("documents", [])
        if isinstance(document, Mapping) and isinstance(document.get("financial_statement"), Mapping)
    }
    if not periods:
        return None
    fiscal_year, fiscal_quarter = max(periods)
    tools = FundamentalSnapshotTools(snapshot)
    identifier = "fundamental:%s:%dQ%d" % (str(bundle["bundle_sha256"])[:16], fiscal_year, fiscal_quarter)
    fundamental_bundle = tools.build_bundle(
        {
            "request_id": identifier,
            "symbols": _symbols(bundle),
            "fiscal_year": fiscal_year,
            "fiscal_quarter": fiscal_quarter,
            "required_statement_types": list(REQUIRED_STATEMENT_TYPES),
            "metric_keys": sorted(METRIC_KEYS),
            "policy_version": POLICY_VERSION,
        },
        bundle_id=identifier,
        generated_at=str(bundle["decision_cutoff"]),
    )
    return FundamentalMetricsCalculator(tools, fundamental_bundle).compute(
        metrics_id=identifier, computed_at=str(bundle["decision_cutoff"])
    )


def high_materiality_events(report: Mapping[str, object]) -> List[Dict[str, str]]:
    """列出事件分析師標為 high 的訊息，交給既有 Fact／Bull／Bear／Adjudicator 流程。"""
    return [
        {"symbol": str(item["symbol"]), "evidence_id": str(event["evidence_id"])}
        for item in report.get("items", [])
        for event in item.get("events", [])
        if event.get("materiality") == "high"
    ]
