"""將 ResearchReport JSON 轉為同源 Markdown。"""

from __future__ import annotations

from typing import List, Mapping

from .contracts import DISCLAIMER, ResearchReportError


class ResearchReportMarkdownRenderer:
    """Render only fields already present in a validated ResearchReport."""

    def render(self, report: Mapping[str, object]) -> str:
        lines = [
            "# %s" % _md_text(report.get("title")),
            "",
            "- 報告 ID：`%s`" % _md_code(report.get("report_id")),
            "- 資料截止：`%s`" % _md_code(report.get("decision_cutoff")),
            "- Snapshot：`%s`" % _md_code(report.get("snapshot_id")),
            "- 狀態：`%s`" % _md_code(report.get("status")),
            "",
            "> %s" % _md_text(DISCLAIMER),
            "",
            "## 資料品質",
            "",
        ]
        quality = report.get("data_quality", {})
        if not isinstance(quality, Mapping):
            raise ResearchReportError("ResearchReport.data_quality 必須是物件")
        lines.extend(
            [
                "- 最近交易日：`%s`" % _md_code(quality.get("latest_trade_date")),
                "- 交易池數量：%s" % _md_text(quality.get("universe_size")),
                "- 最新行情覆蓋：%s" % _md_text(quality.get("latest_price_symbols")),
                "- 文件數量：%s" % _md_text(quality.get("document_count")),
                "- 品質旗標：%s"
                % _md_list(quality.get("quality_flags"), empty="無"),
                "",
                "## 研究覆蓋",
                "",
            ]
        )
        coverage = report.get("coverage", {})
        if not isinstance(coverage, Mapping):
            raise ResearchReportError("ResearchReport.coverage 必須是物件")
        lines.extend(
            [
                "- 事件研究數量：%s" % _md_text(coverage.get("event_item_count")),
                "- 市場情緒／分析師資料：`%s`"
                % _md_code(coverage.get("perception")),
                "- 公司數量：%s" % _md_text(coverage.get("company_count")),
                "",
            ]
        )
        companies = report.get("companies")
        if not isinstance(companies, list):
            raise ResearchReportError("ResearchReport.companies 必須是陣列")
        for company in companies:
            if not isinstance(company, Mapping):
                raise ResearchReportError("ResearchReport.company 必須是物件")
            lines.extend(self._render_company(company))
        lines.extend(["## 來源", ""])
        sources = report.get("sources")
        if not isinstance(sources, list):
            raise ResearchReportError("ResearchReport.sources 必須是陣列")
        if not sources:
            lines.append("- 無被引用來源。")
        for source in sources:
            if not isinstance(source, Mapping):
                raise ResearchReportError("ResearchReport.source 必須是物件")
            lines.append(
                "- `%s`｜%s｜%s｜URL `%s`"
                % (
                    _md_code(source.get("evidence_id")),
                    _md_text(source.get("source") or "未知來源"),
                    _md_text(source.get("data_type") or "未知類型"),
                    _md_code(source.get("url") or "unavailable"),
                )
            )
        lines.extend(["", "## 限制", ""])
        for limitation in report.get("limitations", []):
            lines.append("- %s" % _md_text(limitation))
        missing = report.get("missing_data", [])
        if missing:
            lines.extend(
                ["", "## 全域缺漏", "", "- %s" % _md_list(missing, empty="無")]
            )
        return "\n".join(lines).rstrip() + "\n"

    def _render_company(self, company: Mapping[str, object]) -> List[str]:
        lines = ["## %s" % _md_text(company.get("symbol")), ""]
        price = company.get("latest_price")
        if isinstance(price, Mapping):
            lines.append(
                "- 最新分析價格：%s（%s，證據 `%s`）"
                % (
                    _md_text(price.get("analysis_close_price")),
                    _md_text(price.get("trade_date")),
                    _md_code(price.get("source_evidence_id")),
                )
            )
        else:
            lines.append("- 最新分析價格：unavailable")
        events = company.get("events", [])
        if not isinstance(events, list):
            raise ResearchReportError("company.events 必須是陣列")
        lines.extend(["", "### 事件研究", ""])
        if not events:
            lines.append("- unavailable")
        for event in events:
            if not isinstance(event, Mapping):
                raise ResearchReportError("company.event 必須是物件")
            lines.extend(
                [
                    "#### %s" % _md_text(event.get("event_id")),
                    "",
                    "- 摘要：%s" % _md_text(event.get("event_summary")),
                    "- 方向／狀態：`%s`／`%s`"
                    % (
                        _md_code(event.get("direction")),
                        _md_code(event.get("research_status")),
                    ),
                    "- 影響機制：%s" % _md_text(event.get("impact_mechanism")),
                    "- 多方：%s" % _md_text(event.get("bull_thesis")),
                    "- 空方：%s" % _md_text(event.get("bear_thesis")),
                ]
            )
            adjudication = event.get("adjudication", {})
            if isinstance(adjudication, Mapping):
                lines.append(
                    "- 裁決：`%s`；%s"
                    % (
                        _md_code(adjudication.get("prevailing_case")),
                        _md_text(adjudication.get("rationale")),
                    )
                )
            facts = event.get("facts", [])
            if isinstance(facts, list) and facts:
                lines.extend(["", "| 事實 | 數值 | 單位 | 期間 | 證據 |", "| --- | ---: | --- | --- | --- |"])
                for fact in facts:
                    if isinstance(fact, Mapping):
                        lines.append(
                            "| %s | %s | %s | %s | `%s` |"
                            % (
                                _md_text(fact.get("name")),
                                _md_text(fact.get("value")),
                                _md_text(fact.get("unit")),
                                _md_text(fact.get("period")),
                                _md_code(fact.get("evidence_id")),
                            )
                        )
                lines.append("")
            lines.append(
                "- 風險：%s" % _md_list(event.get("risk_flags", []), empty="無")
            )
            lines.append(
                "- 失效條件：%s"
                % _md_list(event.get("invalidation_signals", []), empty="無")
            )
            lines.append("")
        perception = company.get("perception")
        lines.extend(["### 市場情緒與分析師共識", ""])
        if not isinstance(perception, Mapping) or perception.get("status") == "unavailable":
            lines.append("- unavailable：未提供可用的市場情緒或分析師研究結果。")
        else:
            sentiment = perception.get("sentiment", {})
            if isinstance(sentiment, Mapping):
                lines.append(
                    "- 情緒：`%s`／`%s`，樣本 %s、來源 %s"
                    % (
                        _md_code(sentiment.get("status")),
                        _md_code(sentiment.get("direction")),
                        _md_text(sentiment.get("item_count")),
                        _md_text(sentiment.get("source_count")),
                    )
                )
            lines.append(
                "- 已反映判讀：`%s`"
                % _md_code(perception.get("priced_in_assessment"))
            )
            lines.append("- 說明：%s" % _md_text(perception.get("rationale")))
            metrics = perception.get("consensus_metrics", [])
            if isinstance(metrics, list):
                for metric in metrics:
                    if isinstance(metric, Mapping):
                        revision = metric.get("revision_pct")
                        revision_text = (
                            "unavailable" if revision is None else "%s%%" % _md_text(revision)
                        )
                        lines.append(
                            "- 共識 %s %s：中位數 %s %s，貢獻者 %s，修正 %s"
                            % (
                                _md_text(metric.get("metric")),
                                _md_text(metric.get("forecast_period")),
                                _md_text(metric.get("median")),
                                _md_text(metric.get("unit")),
                                _md_text(metric.get("contributor_count")),
                                revision_text,
                            )
                        )
        lines.extend(
            [
                "",
                "### 風險與缺漏",
                "",
                "- 風險旗標：%s"
                % _md_list(company.get("risk_flags", []), empty="無"),
                "- 缺漏：%s" % _md_list(company.get("missing_data", []), empty="無"),
                "",
            ]
        )
        return lines


def _md_text(value: object) -> str:
    if value is None:
        return "unavailable"
    text = " ".join(str(value).splitlines()).strip()
    for character in ("\\", "`", "*", "_", "{", "}", "[", "]", "<", ">", "#", "|"):
        text = text.replace(character, "\\" + character)
    return text or "unavailable"


def _md_code(value: object) -> str:
    if value is None:
        return "unavailable"
    return " ".join(str(value).replace("`", "'").splitlines()).strip() or "unavailable"


def _md_list(value: object, empty: str) -> str:
    if not isinstance(value, list) or not value:
        return empty
    return "、".join(_md_text(item) for item in value)
