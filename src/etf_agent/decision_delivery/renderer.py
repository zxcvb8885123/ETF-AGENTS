"""固定模板中文閱讀版；所有判斷引用原文。"""

import json
from .tools import cell as _cell, percent, TAIPEI
from etf_agent.core import parse_aware_time
from etf_agent.decision.trader_report import INTENT_LABELS, CONVICTION_LABELS

TEMPLATE_MARKER = "<!-- report_template: 3 -->"
TEMPLATE_MARKER_V2 = "<!-- report_template: 2 -->"
# AccountSnapshot 沒有幣別欄位；DecisionPolicy 驗證只接受 TWD，競賽金額亦以新台幣元計。
DEFAULT_CURRENCY = "TWD（新台幣元）"
STATUS = {"approved": "決策核准，待人工檢視", "no_trade": "決策通過，本日無委託", "rejected": "決策遭拒絕，不提供可執行委託"}


def cell(value):
    # 純文字呈現：跳脫 HTML／Markdown 控制字元，但引號保持原樣，避免 &quot; 出現在閱讀版。
    return _cell(value, quote=False)


def render_report(report):
    return _render(report, 3)


def render_report_v2(report):
    """第 2 版樣板：幣別缺值顯示「未提供」、估值時間保留原時區，只用來驗證舊交付。"""
    return _render(report, 2)


def _account_lines(account, template):
    if template == 2:
        currency, valuation = account.get("currency"), account.get("valuation_at")
    else:
        currency = account.get("currency") or DEFAULT_CURRENCY
        valuation = account.get("valuation_at")
        if valuation is not None:
            valuation = parse_aware_time(valuation, "valuation_at").astimezone(TAIPEI).isoformat() + "（台北時間）"
    return ["金額單位：%s。配置比例分母為配置引擎的預估 NAV；不是 D-Plan 目標權重。" % cell(currency), "",
        "決策前 NAV：%s；現金：%s；估值時間：%s。" % (cell(account.get("nav")), cell(account.get("cash")), cell(valuation)), ""]


def _render(report, template):
    p, risk = report["provenance"], report["risk_summary"]
    marker = TEMPLATE_MARKER if template == 3 else TEMPLATE_MARKER_V2
    lines = [marker, "", "# 最終決策報告", "", "**%s**" % STATUS[report["decision_status"]], "",
        "交易日：%s｜資料截止：%s（台北時間）" % (cell(p["trade_date"]), cell(parse_aware_time(p["decision_cutoff"], "decision_cutoff").astimezone(TAIPEI).isoformat())), "",
        "決策 run：%s" % cell(p["decision_run_id"]), "",
        "本報告已通過決策鏈重建；預計持倉、價格、現金與費稅均非成交結果。D-Plan 狀態另見交付狀態。", "",
        "## 帳戶與預計配置", "", *_account_lines(report["account_before"], template)]
    portfolio = report["portfolio_after_estimate"]
    if portfolio is not None:
        lines += ["預估 NAV：%s；現金：%s（%s）；持股：%d 檔。" % (cell(portfolio["nav"]), cell(portfolio["cash"]), percent(portfolio["cash_weight"]), len(portfolio["positions"])), "",
            "預估手續費：%s；交易稅：%s。" % (cell(report["order_totals"]["total_commission"]), cell(report["order_totals"]["total_tax"])), ""]
    lines += ["## 最終委託（尚未成交）", "", "| 股票 | 方向 | 股數 | 張數 | 參考價 | 預估價 | 金額 | 手續費 | 交易稅 |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for o in report["orders"]:
        lines.append("| " + " | ".join(cell(v) for v in (o["symbol"], "買進" if o["side"] == "buy" else "賣出", *(o[k] for k in ("shares", "lots", "reference_price", "estimated_price", "gross_amount", "commission", "tax")))) + " |")
    if not report["orders"]:
        lines += ["", "無最終委託。"]
    lines += ["", "## 最終持倉與逐檔決策", ""]
    for row in report["decision_rows"]:
        item, position = row["intent"], row["after"]
        # 未持有且無最終委託的標的列於附錄，避免主文塞滿交易池。
        if not _in_main(row):
            continue
        lines += ["### " + cell(row["symbol"]), "",
            "決策前股數：%s；預計股數：%s；預計配置：%s。" % (cell((row["before"] or {}).get("shares", 0)),
                "不適用（決策拒絕）" if report["decision_status"] == "rejected" else cell((position or {}).get("shares", 0)), percent(position["weight"]) if position else "不適用"), ""]
        lines += _intent(item)
        lines += ["最終結果：%s。" % {"buy": "買進", "sell": "賣出", "no_order": "無委託（不等同原意圖為不交易，請對照風控修正及配置限制）", "rejected": "拒絕"}[row["final_action"]], ""]
    stance = risk["cash_stance"]
    # 持股廣度是 2026-10-26 後新增的欄位；舊封存沒有它，輸出維持原樣。
    breadth = "；持股廣度：%s" % cell({"concentrated": "集中", "balanced": "平衡", "diversified": "分散"}.get(stance["breadth"])) if "breadth" in stance else ""
    lines += ["## 風控與修正", "", "現金姿態：%s%s；修正次數：%s。" % (cell({"aggressive": "積極", "neutral": "中性", "defensive": "防守"}.get(stance["level"])), breadth, risk["revision_count"]), ""]
    for entry in risk["revisions"]:
        review = entry["review"]
        lines += ["- %s：%s" % (cell({"approve": "核准", "reject": "拒絕", "revise": "要求修正"}.get(review["decision"])), cell(review["rationale"]))]
        for action in review["revision_actions"]:
            lines += ["  - 結構化修正：" + cell(json.dumps(action, ensure_ascii=False))]
    lines += ["", "### 未解問題", ""] + ["- " + cell(x) for x in risk["review"]["unresolved_questions"]]
    lines += ["", "### 規則檢查", "", "| 規則 | 結果 | 檢查明細 |", "| --- | --- | --- |"]
    for check in risk["guard"]["checks"]:
        lines += ["| %s | %s | %s |" % (cell(check["rule_id"]), "通過" if check["passed"] else "未通過", _details(check["details"]))]
    lines += ["", "### 配置限制", ""]
    lines += _constraints(risk["constraint_flags"]) or ["無額外配置限制。"]
    lines += ["", "完整壓力情境與原始引用請見同次交付的 report.json。", ""]
    lines += ["## 研究資料缺口", "", "以下依分析師與缺口內容合併；未提供或 unavailable 不表示中性。", ""]
    for name, analyst in report["analyst_reports"].items():
        gaps = {}
        for item in analyst.get("items", []):
            for gap in item.get("data_gaps") or []:
                gaps.setdefault(gap, []).append(item.get("symbol"))
        for gap, symbols in gaps.items():
            lines += ["- %s：%s（%d 檔）：%s" % (cell(name), cell(gap), len(symbols), "、".join(cell(x) for x in symbols))]
        if "market_sentiment" in analyst:
            lines += ["- 全市場情緒原始通道：" + cell(json.dumps(analyst["market_sentiment"], ensure_ascii=False))]
    # 主文已列出的標的不在附錄重複。
    rest = [row for row in report["decision_rows"] if not _in_main(row)]
    lines += ["", "## 附錄：其餘交易池原意圖（未持有、無委託，非最終委託）", "", "共 %d 檔；已列於上方的標的不重複。" % len(rest), ""]
    for row in rest:
        lines += ["### " + cell(row["symbol"]), ""] + _intent(row["intent"])
    lines += ["## 引用與價格時間", "",
        "引用共 %d 筆，逐筆來源、內容／抓取／可得時間與原始回應識別請見同次交付的 report.json 的 `evidence_index`。" % len(report["evidence_index"]), "", "| 來源 | 筆數 |", "| --- | --- |"]
    counts = {}
    for evidence in report["evidence_index"]:
        counts[evidence.get("source")] = counts.get(evidence.get("source"), 0) + 1
    lines += ["| %s | %d |" % (cell(source), count) for source, count in sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))]
    lines += ["", "### 參考價格", "", "| 股票 | 交易日 | 收盤價 | 來源 | 引用 |", "| --- | --- | --- | --- | --- |"]
    lines += ["| %s | %s | %s | %s | %s |" % tuple(cell(price.get(k)) for k in ("symbol", "trade_date", "close_price", "source", "source_evidence_id")) for price in report["reference_prices"]]
    lines += ["", "報告識別：%s；內容雜湊：%s。" % (cell(report["report_id"]), report["content_sha256"]), ""]
    return "\n".join(lines)


def _intent(item):
    if item is None:
        return ["原交易理由未提供，無法補寫。", ""]
    result = ["原交易意圖：%s；買進信心：%s。" % (INTENT_LABELS[item["intent"]], CONVICTION_LABELS[item.get("conviction")]), "", cell(item["rationale"]), ""]
    for key, title in (("unresolved_questions", "未解問題"), ("invalidation_conditions", "重估條件")):
        result.extend("- %s：%s" % (title, cell(value)) for value in item.get(key, []))
    return result + [""]


def _in_main(row):
    return bool(row["before"] or row["after"] or row["order"])


def _details(details):
    """規則檢查明細：空值寫「無」，其餘攤成可讀文字；完整內容仍在 report.json。"""
    if not details:
        return "無"
    if isinstance(details, dict):
        return cell("；".join("%s：%s" % (key, _scalar(value)) for key, value in details.items()))
    return cell("共 %d 項（見 report.json）" % len(details)) if isinstance(details, list) else cell(details)


def _scalar(value):
    if isinstance(value, (list, dict)):
        return "無" if not value else json.dumps(value, ensure_ascii=False)
    return value


def _constraints(flags):
    """同一限制代碼與說明的標的合併為一行。"""
    groups, other = {}, []
    for flag in flags:
        if isinstance(flag, dict) and set(flag) == {"symbol", "code", "detail"}:
            groups.setdefault((flag["code"], flag["detail"]), []).append(flag["symbol"])
        else:
            other.append("- " + cell(json.dumps(flag, ensure_ascii=False)))
    return ["- %s（%s，%d 檔）：%s" % (cell(detail), cell(code), len(symbols), "、".join(cell(x) for x in symbols)) for (code, detail), symbols in groups.items()] + other


def renderer_for(markdown):
    """已封存的閱讀版依自身樣板版本重建；沒有版本標記的是第 1 版。"""
    from .renderer_v1 import render_report_v1
    if markdown.startswith(TEMPLATE_MARKER):
        return render_report
    return render_report_v2 if markdown.startswith(TEMPLATE_MARKER_V2) else render_report_v1
