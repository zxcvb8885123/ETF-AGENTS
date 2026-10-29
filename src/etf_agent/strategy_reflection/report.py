"""每週反思報告（繁體中文 Markdown）。

報告由程式產生，每週都有，不依賴 Agent：樣本不足時照樣列出每個診斷指標的天數、筆數與門檻，
並明寫「樣本不足，不下結論」。Agent 的摘要、觀察與提案只是附在後面的**解讀**，不是程式算出的事實。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from .contracts import POLICY_ROOT

TAIPEI = timezone(timedelta(hours=8))
REPORT_DIR = "reports"
_STATUS_LABEL = {"ok": "可參考", "insufficient_sample": "樣本不足，不下結論", "unavailable": "資料不足"}


def report_path(root: Path, now: datetime) -> Path:
    return Path(root) / POLICY_ROOT / REPORT_DIR / ("weekly-%s.md" % now.astimezone(TAIPEI).date().isoformat())


def _status(entry: Mapping[str, object]) -> str:
    return _STATUS_LABEL.get(str(entry.get("status")), str(entry.get("status")))


def _analyst_lines(analyst: Mapping[str, object]) -> List[str]:
    names = {"technical": "技術分析師", "fundamental": "基本面分析師", "event": "事件分析師",
             "intent": "交易意圖（買進加碼看好、減碼出場看壞）", "orders": "實際委託方向"}
    lines = []
    for name in ("technical", "fundamental", "event", "intent", "orders"):
        horizons = analyst.get(name)
        if not isinstance(horizons, Mapping):
            continue
        for horizon in sorted(horizons):
            entry = horizons[horizon]
            detail = "%s｜完整決策日 %s、筆數 %s" % (_status(entry), entry.get("complete_days"), entry.get("n"))
            if entry.get("status") == "ok":
                detail += "｜命中率 %s、看好者平均超額 %s、看壞者平均超額 %s" % (
                    entry.get("hit_rate"), entry.get("up_mean_excess"), entry.get("down_mean_excess"))
            lines.append("- %s %s 日：%s" % (names[name], horizon.lstrip("h"), detail))
    return lines or ["- 尚無已封存的決策可評分。"]


def _sell_lines(sell: Mapping[str, object]) -> List[str]:
    lines = []
    for horizon in sorted(sell):
        entry = sell[horizon]
        detail = "%s｜完整決策日 %s、賣單 %s 筆" % (_status(entry), entry.get("complete_days"), entry.get("n"))
        if entry.get("status") == "ok":
            detail += "｜賣出後平均超額 %s、賣完又跑贏交易池的比例 %s" % (entry.get("mean_excess_after_sell"), entry.get("regret_rate"))
        lines.append("- 賣出之後 %s 日：%s" % (horizon.lstrip("h"), detail))
    return lines or ["- 尚無賣出紀錄。"]


_NUMBERS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二", "十三", "十四"]
_STATE = {"on_track": "照理由走（至今未虧）", "within_tolerance": "短期回檔，在容忍範圍內", "needs_review": "請回頭檢查買進理由",
          "ex_rights": "含除權息，不算報酬", "unavailable": "尚無成交均價"}
_CUT = 220


_VERDICT = {"intact": "理由仍成立", "weakened": "理由轉弱", "broken": "理由已被推翻", "unknown": "無法判斷"}
_COND = {"triggered": "已觸發", "not_triggered": "未觸發", "unknown": "無法判斷"}


def _thesis_section(thesis: Optional[List[Mapping[str, object]]], week: object, review: Mapping[str, Mapping[str, object]]) -> List[str]:
    lines = ["## 零、買進的理由，現在還成立嗎？", ""]
    if isinstance(week, Mapping) and week.get("status") == "ok":
        lines.append("- 本週（最近 %s 個決策日）：買進 %s 筆、賣出 %s 筆。" % (week["days"], week["buy_orders"], week["sell_orders"]))
    if not thesis:
        return lines + ["- 目前沒有可對照買進理由的持股。", ""]
    lines.append("- 狀態只依價格事實判斷；短期波動可接受，虧損達容忍度或觸發虧損警示才標「請回頭檢查」。理由本身是否還成立由你（或交易 Agent）判斷。")
    if review:
        counts: Dict[str, int] = {}
        for item in review.values():
            counts[str(item["verdict"])] = counts.get(str(item["verdict"]), 0) + 1
        lines.append("- Agent 論點檢視（只供參考，不影響交易）：" + "、".join("%s %d 檔" % (_VERDICT[k], counts[k]) for k in _VERDICT if k in counts))
    rank = {"broken": 0, "weakened": 1, "unknown": 2, "intact": 3}
    for row in sorted(thesis, key=lambda r: (rank.get(str(review.get(str(r["symbol"]).upper(), {}).get("verdict", "unknown")), 2),
                                             str(r["status"]) != "needs_review", str(r["symbol"]))):
        head = "- **%s**｜%s｜%s 買進（%s／%s），預估成交價 %s" % (
            row["symbol"], _STATE.get(str(row["status"]), row["status"]), row["entry_day"], row["entry_intent"],
            row.get("entry_conviction") or "—", row["entry_price"])
        if "return_since_entry" in row:
            head += "｜持有 %s 個交易日、至今 %s、期間最大回落 %s（容忍 %s）" % (row["days_held"], row["return_since_entry"], row["max_drawdown_since_entry"], row["tolerance"])
        lines.append(head)
        if row["intents_since"] or row["alerts_since"]:
            lines.append("  - 買進後交易意圖：%s；警示：%s" % (
                "、".join("%s %s 次" % item for item in row["intents_since"].items()) or "無",  # type: ignore[union-attr]
                "、".join("%s %s 次" % item for item in row["alerts_since"].items()) or "無"))  # type: ignore[union-attr]
        text = row.get("text") or {}
        reason = str(text.get("rationale") or "").replace("\n", " ")  # type: ignore[union-attr]
        lines.append("  - 當初理由：%s" % (reason[:_CUT] + ("…" if len(reason) > _CUT else "")))
        verdict = review.get(str(row["symbol"]).upper())
        if verdict is not None:
            lines.append("  - Agent 檢視：**%s**｜%s（引用 %s）" % (_VERDICT[str(verdict["verdict"])], verdict["summary"], "、".join(verdict["finding_ids"]) or "無"))  # type: ignore[index]
        for index, condition in enumerate(text.get("invalidation_conditions", [])):  # type: ignore[union-attr]
            status = verdict["conditions"][index]["status"] if verdict is not None and index < len(verdict["conditions"]) else None  # type: ignore[index]
            lines.append("  - 重估條件：%s%s" % (condition, "（%s）" % _COND[status] if status else ""))
    return lines + [""]


def render_weekly_report(
    now: datetime,
    brief: Mapping[str, object],
    reflection: Mapping[str, object],
    apply_result: Optional[Mapping[str, object]] = None,
    notes: Optional[List[str]] = None,
    history: Optional[List[Mapping[str, object]]] = None,
    thesis: Optional[List[Mapping[str, object]]] = None,
    review: Optional[Mapping[str, Mapping[str, object]]] = None,
    lessons: Optional[Mapping[str, object]] = None,
    current_lessons: Optional[List[str]] = None,
) -> str:
    diagnostics = brief.get("diagnostics") if isinstance(brief.get("diagnostics"), Mapping) else {}
    days, need = brief["decision_days"], brief["required_decision_days"]
    lines = [
        "# 策略反思週報",
        "",
        "產生時間：%s（台北時間）。決策日 %s／%s，%s。" % (
            now.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M"), days, need,
            "命中率類統計樣本已夠，可提出旋鈕提案" if brief["gate_open"] else "報告照常產生；命中率類統計樣本還不夠，所以本週不依它調整旋鈕"),
        "",
        "> 本報告的數字由程式依已封存決策與官方成交價計算；Agent 的摘要只是解讀。樣本少時任何比例都可能只是雜訊。",
        "",
    ]
    for note in notes or []:
        lines.extend(["- %s" % note])
    if notes:
        lines.append("")
    lines.extend(["## 一、這週發生什麼", ""])
    lines.append("- 反思狀態：%s。" % ("有提案" if reflection["status"] == "proposed" else "只觀察，沒有提案"))
    lines.append("- 摘要：%s" % reflection["summary"])
    if apply_result is not None:
        lines.append("- 套用結果：%s" % ("已套用 " + "、".join(apply_result.get("knobs", [])) if apply_result.get("applied") else "未套用（%s）" % apply_result.get("reason")))
    lines.append("")
    lines.extend(_thesis_section(thesis, diagnostics.get("week"), review or {}))  # type: ignore[arg-type]
    lines.extend(["## 二、分析師與交易判斷準不準", ""])
    lines.extend(_analyst_lines(diagnostics.get("analyst", {})))  # type: ignore[arg-type]
    lines.extend(["", "## 三、賣出之後股票又怎麼走（賣太早？）", ""])
    lines.extend(_sell_lines(diagnostics.get("sell", {})))  # type: ignore[arg-type]
    turnover = diagnostics.get("turnover", {})
    lines.extend(["", "## 四、換手與費稅拖累", ""])
    if isinstance(turnover, Mapping) and turnover.get("status") == "ok":
        lines.append("- 決策日 %s 天，平均每日成交金額占 NAV %s、費稅占 NAV %s，賣單 %s 筆。" % (
            turnover.get("days"), turnover.get("mean_turnover_rate"), turnover.get("mean_fee_drag"), turnover.get("sell_orders")))
    else:
        lines.append("- 資料不足。")
    review = diagnostics.get("review", {})
    lines.extend(["", "## 五、停損／停利警示與交易 Agent 的回應", ""])
    if isinstance(review, Mapping) and review.get("status") == "ok":
        lines.append("- 有持股檢視的決策日 %s 天，警示共 %s 次。" % (review.get("days_with_review"), review.get("alert_total")))
        for flag, counts in (review.get("alerts") or {}).items():  # type: ignore[union-attr]
            lines.append("  - %s：%s" % (flag, "、".join("%s %s 次" % (intent, n) for intent, n in counts.items())))
        lines.append("- 警示後的股價走勢要等樣本夠了才能評估，目前只列次數。")
    else:
        lines.append("- 尚無 team_inputs 2.4 以後的決策日，還沒有警示紀錄。")
    stance, breadth = diagnostics.get("stance", {}), diagnostics.get("breadth", {})
    lines.extend(["", "## 六、現金姿態與持股廣度分布", ""])
    lines.append("- 現金姿態：%s" % (stance.get("days") if isinstance(stance, Mapping) and stance.get("status") == "ok" else "無紀錄"))
    lines.append("- 持股廣度：%s" % (breadth.get("days") if isinstance(breadth, Mapping) and breadth.get("status") == "ok" else "無紀錄"))
    lines.append("- 事後對錯要等報酬樣本夠了才評估；目前只是分布。")
    bench = diagnostics.get("benchmark", {})
    lines.extend(["", "## 七、與基準比較", ""])
    if isinstance(bench, Mapping) and bench.get("status") == "ok":
        names = {"0050_buy_and_hold": "0050 買入持有", "universe_equal_weight": "150 檔等權買入持有"}
        for key, item in sorted(bench["benchmarks"].items()):  # type: ignore[union-attr]
            lines.append("- %s（對齊 %s 個交易日）：帳戶累積 %s、基準累積 %s、超額 %s、相對最大回落 %s" % (
                names.get(key, key), item["aligned_days"], item["account_cumulative_return"],
                item["benchmark_cumulative_return"], item["excess_return"], item["relative_drawdown"]))
        for key, item in sorted(bench["benchmarks"].items()):  # type: ignore[union-attr]
            if "week_excess_return" in item:
                lines.append("- 本週（最近 5 個交易日）對 %s：帳戶 %s、基準 %s、超額 %s" % (
                    names.get(key, key), item["week_account_return"], item["week_benchmark_return"], item["week_excess_return"]))
        lines.append("- 上面的累積數字是開帳以來；開帳不滿一週時沒有「本週」數字。樣本只有幾天時只是雜訊。")
    else:
        lines.append("- %s" % (bench.get("reason") if isinstance(bench, Mapping) else "資料不足。"))
    lines.extend(["", "## 〇、經驗教訓（回頭看過去買進後，給交易 Agent 的做法建議）", ""])
    if current_lessons:
        lines.extend("- 目前生效：%s" % text for text in current_lessons)
    else:
        lines.append("- 目前沒有生效的教訓。")
    if lessons and lessons.get("status") == "proposed":
        lines.extend("- 本週新提案（週六 10:00 套用，套用後試用 5 個交易日，明確變差自動還原）：%s" % text for text in lessons["texts"])  # type: ignore[union-attr]
    elif lessons:
        lines.append("- 本週沒有新教訓：%s" % lessons.get("reason", lessons.get("status")))
    results = [entry for entry in (history or []) if entry.get("action") == "trial_result"]
    lines.extend(["", "## 八、套用後的試用觀察結果", ""])
    if results:
        label = {"kept": "保留（沒有明確變差）", "reverted": "已還原（明確變差）", "extended": "延長觀察", "kept_unverified": "保留（看不出差別，且已過影子回放）"}
        for entry in results[-5:]:
            evaluation = entry.get("evaluation") or {}
            lines.append("- %s：%s｜套用前 %s 日、後 %s 日%s" % (
                str(entry.get("settled_at", ""))[:10], label.get(str(entry.get("result")), entry.get("result")),
                evaluation.get("before_days"), evaluation.get("after_days"),
                ("｜平均日超額變化 %s（門檻 %s）" % (evaluation.get("delta"), evaluation.get("threshold"))) if evaluation.get("delta") else ""))
    else:
        lines.append("- 還沒有任何套用後的試用結果。")
    lines.extend(["", "## 九、目前策略旋鈕", ""])
    for path, value in sorted((brief.get("knobs") or {}).items()):  # type: ignore[union-attr]
        lines.append("- `%s` = %s" % (path, value))
    if reflection["status"] == "proposed":
        lines.extend(["", "## 十、Agent 的解讀與提案", ""])
        for item in reflection["observations"]:  # type: ignore[union-attr]
            lines.append("- 觀察：%s（依據 %s）" % (item["text"], "、".join(item["evidence_ids"])))
        for item in reflection["proposals"]:  # type: ignore[union-attr]
            lines.append("- 提案：`%s` %s → %s。%s（依據 %s）" % (
                item["knob"], item["current"], item["proposed"], item["rationale"], "、".join(item["evidence_ids"])))
    number = 0
    for index, line in enumerate(lines):  # 章節編號由程式依序重編，新增章節不必改其他標題
        if line.startswith("## "):
            number += 1
            lines[index] = "## %s、%s" % (_NUMBERS[number - 1], line.split("、", 1)[1])
    return "\n".join(lines) + "\n"


def write_weekly_report(root: Path, now: datetime, text: str) -> Path:
    path = report_path(root, now)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
