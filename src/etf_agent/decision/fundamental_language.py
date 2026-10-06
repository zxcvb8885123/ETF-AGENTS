"""把指標缺口轉為有期間、報表與欄位的中文說明；不改寫數值或分類。"""

import re
import json

from etf_agent.core import parse_decimal


NEEDS = {
    "operating_margin_pct": ("income_statement", ("revenue", "operating_profit"), False),
    "liabilities_to_assets_pct": ("balance_sheet", ("total_liabilities", "total_assets"), False),
    "revenue_yoy_pct": ("income_statement", ("revenue",), True),
    "net_income_yoy_pct": ("income_statement", ("net_income",), True),
    "operating_margin_pp_yoy": ("income_statement", ("revenue", "operating_profit"), True),
}
NAMES = {"revenue": "營業收入", "operating_profit": "營業利益", "net_income": "稅後淨利",
         "total_liabilities": "負債總額", "total_assets": "資產總額"}
STATEMENTS = {"income_statement": "損益表", "balance_sheet": "資產負債表"}
OUTLOOK_LABELS = {"positive": "正面", "neutral": "中性", "negative": "負面", "unknown": "中性"}


def period_label(year, quarter, kind):
    if kind == "cumulative_to_quarter":
        return "%s年%s" % (year, {1: "第一季", 2: "上半年", 3: "前三季", 4: "全年"}[quarter])
    return "%s年第二季末" % year if kind == "point_in_time" and quarter == 2 else "%s年第%s季" % (year, quarter)


def explain_metric_gap(metric, documents, symbol):
    """只說分析輸入缺少什麼，不宣稱公司未公告財報。"""
    match = re.fullmatch(r"(\d{4})Q([1-4])", str(metric.get("period", "")))
    spec = NEEDS.get(metric.get("metric_key"))
    if match is None or spec is None:
        return ["此項財務指標無法計算。需要確認報表期間及計算欄位。"]
    year, quarter = int(match[1]), int(match[2])
    statement_type, fields, comparative = spec
    relevant = [d["financial_statement"] for d in documents
                if d.get("symbol") == symbol and isinstance(d.get("financial_statement"), dict)
                and d["financial_statement"].get("statement_type") == statement_type
                and d["financial_statement"].get("fiscal_quarter") == quarter]
    current = next((s for s in relevant if s.get("fiscal_year") == year), None)
    kind = (current or {}).get("period_kind", "cumulative_to_quarter" if statement_type == "income_statement" else "point_in_time")
    if metric.get("reason_code") == "NONPOSITIVE_COMPARISON_BASE":
        return ["%s的%s為零或負數。不能計算一般年增率。" % (period_label(year - 1, quarter, kind), NAMES[fields[0]])]
    reasons = {"CURRENCY_MISMATCH": "比較欄位的幣別不同。不能直接計算。",
               "NONPOSITIVE_DENOMINATOR": "計算分母為零或負數。不能計算此比率。"}
    if metric.get("reason_code") in reasons:
        return [reasons[metric["reason_code"]]]
    if str(metric.get("reason_code", "")).startswith("UNSUPPORTED_INDUSTRY"):
        return ["尚未接入此業別的財報欄位及計算方法。不能套用一般業公式。"]
    gaps = []
    for target in ([year, year - 1] if comparative else [year]):
        statement = next((s for s in relevant if s.get("fiscal_year") == target), None)
        label = period_label(target, quarter, (statement or {}).get("period_kind", kind))
        if statement is None:
            gaps.append("目前分析摘要未提供%s%s。原始資料是否已取得，需由資料端確認。" % (label, STATEMENTS[statement_type]))
            continue
        missing = [NAMES[field] for field in fields
                   if statement.get("facts", {}).get(field, {}).get("value_status") != "provided"]
        if missing:
            doc = next((d for d in documents if d.get("symbol") == symbol and d.get("financial_statement") == statement), {})
            try:
                raw = json.loads(str(doc.get("body", "{}")))
            except (ValueError, TypeError):
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            if statement.get("industry") in {"fh", "basi", "bd", "ins"}:
                if "net_income" in fields and "稅後淨利" in missing:
                    field = "本期稅後淨利（淨損）"
                    try:
                        parse_decimal(raw.get(field), field, reject_bool=True)
                    except ValueError:
                        pass
                    else:
                        gaps.append("%s原始損益表已有「%s」欄位。此欄位尚未接入分析摘要。" % (label, field))
                        missing.remove("稅後淨利")
                if any(name in missing for name in ("營業收入", "營業利益")):
                    if statement.get("facts", {}).get("net_revenue", {}).get("value_status") == "provided":
                        gaps.append("%s的金融業淨收益已接入。不能直接套用一般業的營業收入及營業利益率公式。" % label)
                    else:
                        gaps.append("%s為金融業損益表。金融業專用欄位及比較方法尚未接入。" % label)
                    missing = [name for name in missing if name not in {"營業收入", "營業利益"}]
            if missing:
                gaps.append("目前分析摘要的%s%s未提供%s欄位。原始欄位是否存在，需由資料端確認。" % (label, STATEMENTS[statement_type], "、".join(missing)))
    return gaps or ["報表已有部分欄位。期間、單位或比較條件仍不符合計算要求。"]


def chinese_gap_explanations(metrics, documents, symbol):
    result = []
    for metric in metrics:
        if metric.get("status") == "available":
            continue
        for text in explain_metric_gap(metric, documents, symbol):
            if text not in result:
                result.append(text)
    merged = {}
    other = []
    for text in result:
        match = re.fullmatch(r"(目前分析摘要的.+未提供)(.+)欄位。原始欄位是否存在，需由資料端確認。", text)
        if match:
            fields = merged.setdefault(match[1], [])
            for field in match[2].split("、"):
                if field not in fields:
                    fields.append(field)
        else:
            other.append(text)
    return [prefix + "、".join(fields) + "欄位。原始欄位是否存在，需由資料端確認。" for prefix, fields in merged.items()] + other


def render_fundamental_report(report, *, title="基本面分析報告"):
    """對外三個標籤；無資料另列不判斷狀態，引用留在原始 JSON。"""
    lines = ["# " + title, "", "| 股票 | 判斷 |", "|---|---|"]
    for item in report["items"]:
        label = OUTLOOK_LABELS[item["outlook"]]
        status = "（資料不足，暫不判斷）" if item["outlook"] == "unknown" else ""
        lines.append("| %s | %s%s |" % (item["symbol"].split(".")[0], label, status))
    for item in report["items"]:
        lines.extend(["", "## " + item["symbol"].split(".")[0], "",
                     "**判斷：%s。**" % OUTLOOK_LABELS[item["outlook"]], ""])
        if item["outlook"] == "unknown":
            lines.extend(["資料不足，暫不判斷。此處中性只作暫列標籤。", ""])
        reasons, cautions = [], []
        for finding in item["findings"]:
            text = finding["text"]
            if text.startswith("判斷："):
                remainder = text.split("。", 1)[1] if "。" in text else ""
                if remainder:
                    lines.extend([remainder, ""])
            elif text.startswith("需注意："):
                cautions.append(text[len("需注意："):])
            else:
                reasons.append(text[len("理由："):] if text.startswith("理由：") else text)
        for heading, texts in (("理由", reasons), ("需注意", cautions)):
            if texts:
                lines.extend(["**%s：**" % heading, ""])
                lines.extend("- " + text for text in texts)
                lines.append("")
        if item["data_gaps"]:
            lines.extend(["", "**資料限制：**", ""])
            sentences = [sentence + "。" for text in item["data_gaps"] for sentence in text.split("。") if sentence]
            lines.extend("- " + text for text in dict.fromkeys(sentences))
    return "\n".join(lines) + "\n"
