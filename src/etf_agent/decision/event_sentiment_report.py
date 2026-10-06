"""將已驗證的合併分析報告呈現為繁體中文，不新增結論。"""

from typing import Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from etf_agent.core import parse_aware_time

from .analysts import AnalystReportValidator, EVENT_SENTIMENT_VERSION, EVENT_MARKET_VERSION
from .contracts import DecisionToolError
from .event_financial_context import event_financial_context


LABELS = {"positive": "正面", "neutral": "中性", "negative": "負面", "unknown": "資料不足，無法判斷"}
IMPORTANCE = {"high": "高", "medium": "中", "low": "低", "unknown": "資料不足"}
GAPS = {
    "NO_LICENSED_SENTIMENT_SOURCE": "本次分析未收到通過來源與時間核對的市場情緒資料，無法判斷市場反應。",
    "NO_UNAMBIGUOUS_SENTIMENT_DIRECTION": "市場情緒缺少可用資料或方向不一致，無法判斷。",
    "NO_MATERIAL_EVENT_IN_WINDOW": "本次輸入沒有這檔股票的重大訊息，無法判斷事件方向。",
}


def render_event_sentiment_report(
    bundle: Mapping[str, object], report: Mapping[str, object],
    symbols: Optional[Sequence[str]] = None,
) -> str:
    """呈現前重驗同源、時間、覆蓋與情緒聚合；批次範圍需明確傳入。"""
    if report.get("schema_version") not in {EVENT_SENTIMENT_VERSION, EVENT_MARKET_VERSION}:
        raise DecisionToolError("中文事件與市場報告需要格式 2.1 或 2.2")
    errors = AnalystReportValidator(bundle, "event", symbols).validate(report)
    if errors:
        raise DecisionToolError("合併報告驗證失敗：" + "；".join(errors))
    if report.get("schema_version") == EVENT_MARKET_VERSION:
        return _render_market_scoped(report)
    documents = {doc["source_evidence_id"]: doc for doc in bundle["snapshot"]["documents"]}
    financial = event_financial_context(bundle)
    lines = ["# 事件與市場情緒分析報告", "",
             "資料截止：%s。" % report["decision_cutoff"], "",
             "依此截止時間的已驗證輸入分析。無事件代表本次輸入未提供，不能解讀為公司沒有事件。", "",
             "| 股票 | 事件看法 | 市場情緒 | 綜合看法 |",
             "|---|---|---|---|"]
    for item in report["items"]:
        lines.append("| %s | %s | %s | %s |" % (item["symbol"], LABELS[item["event_outlook"]],
                                                    LABELS[item["sentiment"]["outlook"]], LABELS[item["outlook"]]))
    for item in report["items"]:
        lines.extend(["", "## %s" % item["symbol"], "", "### 分析解讀", ""])
        lines.extend("- " + finding["text"] for finding in item["findings"])
        if not item["findings"]:
            lines.append("資料不足，暫無可引用的方向判斷。")
        if financial.get(item["symbol"]):
            lines.extend(["", "### 已接入的財報背景", ""])
            for statement in financial[item["symbol"]]:
                title = "損益表" if statement["statement_type"] == "income_statement" else "資產負債表"
                lines.append("- %s，第 %s 年第 %s 季，期間終止：%s。" % (title, statement["fiscal_year"], statement["fiscal_quarter"], statement["period_end"]))
                for fact in statement["facts"]:
                    lines.append("  - %s：%s；幣別 %s，原值乘數 %s。" % (fact["source_field"], fact["value"], fact["currency"], fact["unit_multiplier"]))
                lines.append("  - 報表範圍：%s。" % ("尚未確認合併或個別口徑" if statement["reporting_scope"] == "unknown" else statement["reporting_scope"]))
        lines.extend(["", "### 公告事實與重要程度", ""])
        if not item["events"]:
            lines.append("本次輸入未提供重大訊息。")
        for event in item["events"]:
            doc = documents[event["evidence_id"]]
            lines.append("- **重要程度：%s。** %s" % (IMPORTANCE[event["materiality"]], event["summary"]))
            title = " ".join(str(doc["title"]).replace("\\r\\n", " ").replace("\\r", " ").split())
            url = str(doc.get("source_url") or "")
            source = "[%s](%s)" % (title, url) if url.startswith(("https://", "http://")) else title
            dates = [parse_aware_time(doc[key], key, error=DecisionToolError).astimezone(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M")
                     for key in ("published_at", "available_at")]
            lines.append("  來源：%s；發布：%s；資料可得：%s（台北時間）。" % (source, *dates))
        lines.extend(["", "### 市場情緒", ""])
        lines.extend("- " + finding["text"] for finding in item["sentiment"]["findings"])
        if not item["sentiment"]["findings"]:
            lines.append("資料不足，無法判斷市場反應。")
        lines.extend(["", "### 資料限制", ""])
        gaps = list(dict.fromkeys(GAPS.get(gap, gap) for gap in [*item["data_gaps"], *item["sentiment"]["data_gaps"]]))
        lines.extend("- " + gap for gap in gaps)
        if not gaps:
            lines.append("本份輸入沒有列出資料缺口。")
    lines.extend(["", "本報告未執行多空研究、交易及風險流程。標為高的事件仍待後續深入研究。", ""])
    return "\n".join(lines)


def _render_market_scoped(report):
    market = report["market_sentiment"]
    label = LABELS[market['outlook']]
    if market['outlook'] == 'unknown' and market['data_gaps'] and all('使用範圍尚未確認' in gap for gap in market['data_gaps']):
        label = '來源使用範圍尚未確認，正式結果暫不提供'
    lines = ["# 公司事件與整體市場情緒報告", "", "資料截止：%s。" % report["decision_cutoff"], "",
             "## 整體市場情緒", "", "**結果：%s。**" % label, ""]
    lines.extend("- " + row["text"] for row in market["findings"])
    lines.extend("- " + gap for gap in market["data_gaps"])
    lines.extend(["", "## 公司事件", "", "| 公司 | 事件結果 | 主要理由 |", "|---|---|---|"])
    for item in report["items"]:
        reason = item["findings"][0]["text"] if item["findings"] else "本次未提供可判讀的公司事件。"
        lines.append("| %s | %s | %s |" % (item["symbol"], LABELS[item["outlook"]], reason.replace("|", "／").replace("\n", " ")))
    for item in report["items"]:
        lines.extend(["", "### %s" % item["symbol"], "", "需注意：", ""])
        lines.extend("- " + row["text"] for row in item["findings"][1:])
        lines.extend(["", "資料限制：", ""])
        lines.extend("- " + GAPS.get(gap, gap) for gap in dict.fromkeys(item["data_gaps"]))
        if not item["data_gaps"]:
            lines.append("本份輸入未列出資料缺口。")
    lines.extend(["", "全市場情緒只給一次；公司結果只解讀公司事件，兩者不合成分數。逐則公告與引用證據保留在同源 JSON。", "",
                  "本報告未執行多空研究、交易或風險流程；高重大事件仍需另行深入研究。", ""])
    return "\n".join(lines)
