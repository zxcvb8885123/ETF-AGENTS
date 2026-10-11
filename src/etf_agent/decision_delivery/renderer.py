"""固定模板中文閱讀版；所有判斷引用原文。"""

import json
from .tools import cell, percent, TAIPEI
from etf_agent.core import parse_aware_time
from etf_agent.decision.trader_report import INTENT_LABELS, CONVICTION_LABELS

STATUS = {"approved": "決策核准，待人工檢視", "no_trade": "決策通過，本日無委託", "rejected": "決策遭拒絕，不提供可執行委託"}


def render_report(report):
    p, risk = report["provenance"], report["risk_summary"]
    lines = ["# 最終決策報告", "", "**%s**" % STATUS[report["decision_status"]], "",
        "交易日：%s｜資料截止：%s（台北時間）" % (cell(p["trade_date"]), cell(parse_aware_time(p["decision_cutoff"], "decision_cutoff").astimezone(TAIPEI).isoformat())), "",
        "決策 run：%s" % cell(p["decision_run_id"]), "",
        "本報告已通過決策鏈重建；預計持倉、價格、現金與費稅均非成交結果。D-Plan 狀態另見交付狀態。", "",
        "## 帳戶與預計配置", "",
        "金額單位：%s。配置比例分母為配置引擎的預估 NAV；不是 D-Plan 目標權重。" % cell(report["account_before"].get("currency")), "",
        "決策前 NAV：%s；現金：%s；估值時間：%s。" % tuple(cell(report["account_before"].get(k)) for k in ("nav", "cash", "valuation_at")), ""]
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
        if not row["before"] and not position and not row["order"]:
            continue
        lines += ["### " + cell(row["symbol"]), "",
            "決策前股數：%s；預計股數：%s；預計配置：%s。" % (cell((row["before"] or {}).get("shares", 0)),
                "不適用（決策拒絕）" if report["decision_status"] == "rejected" else cell((position or {}).get("shares", 0)), percent(position["weight"]) if position else "不適用"), ""]
        lines += _intent(item)
        lines += ["最終結果：%s。" % {"buy": "買進", "sell": "賣出", "no_order": "無委託（不等同原意圖為不交易，請對照風控修正及配置限制）", "rejected": "拒絕"}[row["final_action"]], ""]
    lines += ["## 風控與修正", "", "現金姿態：%s；修正次數：%s。" % (cell({"aggressive": "積極", "neutral": "中性", "defensive": "防守"}.get(risk["cash_stance"]["level"])), risk["revision_count"]), ""]
    for entry in risk["revisions"]:
        review = entry["review"]
        lines += ["- %s：%s" % (cell({"approve": "核准", "reject": "拒絕", "revise": "要求修正"}.get(review["decision"])), cell(review["rationale"]))]
        for action in review["revision_actions"]:
            lines += ["  - 結構化修正：" + cell(json.dumps(action, ensure_ascii=False))]
    lines += ["", "### 未解問題", ""] + ["- " + cell(x) for x in risk["review"]["unresolved_questions"]]
    lines += ["", "### 規則檢查", "", "| 規則 | 結果 | 檢查明細 |", "| --- | --- | --- |"]
    for check in risk["guard"]["checks"]:
        lines += ["| %s | %s | %s |" % (cell(check["rule_id"]), "通過" if check["passed"] else "未通過", cell(json.dumps(check["details"], ensure_ascii=False)))]
    lines += ["", "### 配置限制", ""]
    lines += ["- " + cell(json.dumps(value, ensure_ascii=False)) for value in risk["constraint_flags"]] or ["無額外配置限制。"]
    lines += ["", "完整壓力情境與原始引用請見同次交付的 report.json。", ""]
    lines += ["## 研究資料缺口", "", "以下保留各分析師的資料缺口；未提供或 unavailable 不表示中性。", ""]
    for name, analyst in report["analyst_reports"].items():
        for item in analyst.get("items", []):
            if item.get("data_gaps"):
                lines += ["- %s／%s：%s" % (cell(name), cell(item.get("symbol")), cell("；".join(item["data_gaps"])))]
        if "market_sentiment" in analyst:
            lines += ["- 全市場情緒原始通道：" + cell(json.dumps(analyst["market_sentiment"], ensure_ascii=False))]
    lines += ["", "## 附錄：全交易池原意圖（非最終委託）", ""]
    for row in report["decision_rows"]:
        lines += ["### " + cell(row["symbol"]), ""] + _intent(row["intent"])
    lines += ["## 引用與價格時間", "", "原始來源及內容／抓取／可得時間如下；引用保留 ID 供稽核。", "",
        cell(json.dumps({"evidence": report["evidence_index"], "prices": report["reference_prices"]}, ensure_ascii=False, indent=2)), "",
        "報告識別：%s；內容雜湊：%s。" % (cell(report["report_id"]), report["content_sha256"]), ""]
    return "\n".join(lines)


def _intent(item):
    if item is None:
        return ["原交易理由未提供，無法補寫。", ""]
    result = ["原交易意圖：%s；買進信心：%s。" % (INTENT_LABELS[item["intent"]], CONVICTION_LABELS[item.get("conviction")]), "", cell(item["rationale"]), ""]
    for key, title in (("unresolved_questions", "未解問題"), ("invalidation_conditions", "重估條件")):
        result.extend("- %s：%s" % (title, cell(value)) for value in item.get(key, []))
    return result + [""]
