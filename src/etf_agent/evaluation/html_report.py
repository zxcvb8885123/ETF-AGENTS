"""一頁自包含 HTML 報告：只排版既有 JSON 的字串，不重算任何數字。

- 純標準庫；inline SVG 與 inline style；不含 script、外連資源與 ``url(``；頁首帶 CSP meta（``default-src 'none'``）。
- 所有來自報告的文字一律 ``html.escape``。
- 圖是衍生品：NAV 點數不足或沒有可畫的分組平均時顯示「無圖」，不補值。
- 頁尾印 JSON 的 content_sha256 與重算驗證指令；HTML 本身不進 manifest，可隨時由 JSON 重新產生。
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from etf_agent.core import canonical_sha256

from .contracts import REPLAY_EVIDENCE_GRADE, REPLAY_NOTICE


def _notice(report: Mapping[str, object]) -> str:
    """每份回放類報告首段固定的提醒：回放不是對 LLM 判斷的前瞻驗證。"""
    return "%s（evidence_grade：%s）" % (esc(report.get("notice") or REPLAY_NOTICE), esc(report.get("evidence_grade") or REPLAY_EVIDENCE_GRADE))


CSP ="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
_STYLE = """
:root { color-scheme: light; --ink: #1b2233; --muted: #5b6475; --line: #e3e7ee; --soft: #f1f4f9; --card: #ffffff; --brand: #2b4c8c; --accent: #c47a1a; }
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, "Segoe UI", "Noto Sans TC", "PingFang TC", sans-serif; color: var(--ink); background: #eef1f6; line-height: 1.6; font-size: 15px; }
header.hero { background: linear-gradient(135deg, #1f3a73, #2b4c8c 60%, #3f6bb8); color: #fff; padding: 32px 24px 28px; }
header.hero .inner, main { max-width: 1120px; margin: 0 auto; }
h1 { font-size: 1.6rem; margin: 0 0 10px; letter-spacing: .02em; }
p.lead { color: #dbe5f7; margin: 0; max-width: 860px; font-size: .95rem; }
p.lead code { background: rgba(255,255,255,.16); color: #fff; }
main { padding: 8px 16px 40px; }
h2 { font-size: 1.1rem; margin: 28px 0 10px; padding-left: 12px; border-left: 4px solid var(--brand); }
h3 { font-size: .98rem; margin: 18px 0 8px; color: var(--brand); }
.tablewrap { overflow-x: auto; background: var(--card); border: 1px solid var(--line); border-radius: 10px; margin: 8px 0 14px; box-shadow: 0 1px 2px rgba(20,30,60,.05); }
table { border-collapse: collapse; width: 100%; font-size: .88rem; }
th, td { padding: 8px 12px; text-align: left; vertical-align: top; border-bottom: 1px solid var(--line); }
th { background: var(--soft); color: var(--muted); font-weight: 600; white-space: nowrap; }
tbody tr:nth-child(even) td { background: #fafbfd; }
tbody tr:hover td { background: #eef3fc; }
tbody tr:last-child td { border-bottom: 0; }
th.num, td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 6px 20px; margin: 8px 0 14px; font-size: .9rem; background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 16px; }
dt { color: var(--muted); }
dd { margin: 0; word-break: break-all; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .82em; background: var(--soft); padding: 1px 6px; border-radius: 4px; word-break: break-all; }
.notice { background: #fff6dc; border: 1px solid #ecce7a; border-left: 4px solid var(--accent); border-radius: 8px; padding: 10px 14px; margin: 10px 0; font-size: .9rem; }
.nochart { color: #7a8190; font-style: italic; background: var(--card); border: 1px dashed var(--line); border-radius: 8px; padding: 10px 14px; }
svg { width: 100%; max-width: 100%; height: auto; background: var(--card); border: 1px solid var(--line); border-radius: 10px; display: block; margin: 8px 0 14px; }
.chips { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin: 8px 0 14px; }
.chip { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; box-shadow: 0 1px 2px rgba(20,30,60,.05); }
.chip .k { color: var(--muted); font-size: .78rem; }
.chip .v { font-size: 1.15rem; font-weight: 700; margin-top: 2px; word-break: break-all; }
.pos { color: #137a4a; font-weight: 600; }
.neg { color: #c0392b; font-weight: 600; }
details { margin: 8px 0; }
summary { cursor: pointer; color: var(--brand); font-weight: 600; padding: 6px 0; }
footer { margin-top: 36px; font-size: .82rem; color: var(--muted); border-top: 1px solid var(--line); padding-top: 12px; }
ul { margin: 4px 0 10px; padding-left: 22px; }
li { margin: 2px 0; }
@media (max-width: 640px) { header.hero { padding: 22px 16px; } h1 { font-size: 1.3rem; } main { padding: 4px 12px 32px; } }
"""
_LINE = "#2b4c8c"
_BAR_POOL = "#2b4c8c"
_BAR_BENCH = "#c47a1a"
_AXIS = "#9aa1ad"


def esc(value: object) -> str:
    """所有外部文字都走這裡；None 顯示為空字串。"""
    return html.escape("" if value is None else str(value), quote=True)


def _text(value: object, empty: str = "無") -> str:
    return empty if value is None else esc(value)


def _pct(value: object) -> str:
    if value is None:
        return "無"
    try:
        return "%.2f%%" % (Decimal(str(value)) * 100)
    except (InvalidOperation, ValueError):
        return esc(value)


def _decimal(value: object) -> Optional[Decimal]:
    try:
        return None if value is None else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _page(title: str, lead: str, sections: Iterable[str], footer: str) -> str:
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-Hant\"><head><meta charset=\"utf-8\">"
        "<meta http-equiv=\"Content-Security-Policy\" content=\"%s\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>%s</title><style>%s</style></head><body><header class=\"hero\"><div class=\"inner\"><h1>%s</h1>%s</div></header><main>%s%s</main></body></html>\n"
        % (CSP, esc(title), _STYLE, esc(title), ("<p class=\"lead\">%s</p>" % lead) if lead else "", "".join(sections), ("<footer>%s</footer>" % footer) if footer else "")
    )


def _tint(text: str) -> str:
    """純百分比數字依正負上色（文字已跳脫；只在整格就是一個百分比時才加 span）。"""
    plain = text.replace("\u2212", "-")
    if not plain.endswith("%") or not plain[:-1].lstrip("-").replace(".", "", 1).isdigit():
        return text
    number = Decimal(plain[:-1])
    if number == 0:
        return text
    return "<span class=\"%s\">%s</span>" % ("pos" if number > 0 else "neg", text)


def _chips(pairs: Sequence[Tuple[str, str]]) -> str:
    return "<div class=\"chips\">%s</div>" % "".join("<div class=\"chip\"><div class=\"k\">%s</div><div class=\"v\">%s</div></div>" % (esc(k), v) for k, v in pairs)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Tuple[str, bool]]]) -> str:
    """rows 的每格是 (已跳脫文字, 是否數字靠右)。"""
    # 表頭對齊該欄內容：數字欄的表頭也靠右，否則標題與數字會錯開。
    numeric_columns = [any(len(row) > index and row[index][1] for row in rows) for index in range(len(headers))]
    head = "".join("<th%s>%s</th>" % (" class=\"num\"" if numeric_columns[index] else "", esc(header)) for index, header in enumerate(headers))
    body = "".join(
        "<tr>%s</tr>" % "".join("<td%s>%s</td>" % (" class=\"num\"" if numeric else "", _tint(text) if numeric else text) for text, numeric in row) for row in rows)
    return "<div class=\"tablewrap\"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>" % (head, body)


def _kv(pairs: Sequence[Tuple[str, str]]) -> str:
    return "<dl>%s</dl>" % "".join("<dt>%s</dt><dd>%s</dd>" % (esc(key), value) for key, value in pairs)


def _list(items: Iterable[object]) -> str:
    rows = ["<li>%s</li>" % esc(item) for item in items]
    return "<ul>%s</ul>" % "".join(rows) if rows else "<p class=\"nochart\">無</p>"


def _fmt(number: float) -> str:
    return ("%.1f" % number).rstrip("0").rstrip(".")


# ---------------------------------------------------------------- charts
def line_chart_svg(points: Sequence[Tuple[str, Decimal]], title: str, width: int = 760, height: int = 240) -> str:
    """NAV 折線；少於 2 點就「無圖」。座標只是排版，數值仍以表格為準。"""
    if len(points) < 2:
        return "<p class=\"nochart\">無圖：%s 的點數不足（%d）</p>" % (esc(title), len(points))
    values = [value for _, value in points]
    low, high = min(values), max(values)
    pad_left, pad_right, pad_top, pad_bottom = 72, 16, 16, 36
    inner_w, inner_h = width - pad_left - pad_right, height - pad_top - pad_bottom
    span = high - low if high != low else Decimal(1)

    def x_of(index: int) -> float:
        return pad_left + inner_w * index / (len(points) - 1)

    def y_of(value: Decimal) -> float:
        return pad_top + inner_h * float((high - value) / span) if high != low else pad_top + inner_h / 2

    path = " ".join("%s,%s" % (_fmt(x_of(index)), _fmt(y_of(value))) for index, (_, value) in enumerate(points))
    labels = []
    step = max(1, (len(points) - 1) // 6)
    for index in range(0, len(points), step):
        labels.append("<text x=\"%s\" y=\"%d\" font-size=\"12\" text-anchor=\"middle\" fill=\"%s\">%s</text>" % (
            _fmt(x_of(index)), height - 12, _AXIS, esc(points[index][0])))
    # inline SVG 不需要命名空間宣告，頁面因此不含任何 URL。
    return (
        "<svg viewBox=\"0 0 %d %d\" width=\"%d\" height=\"%d\" role=\"img\" aria-label=\"%s\">"
        "<title>%s</title>"
        "<line x1=\"%d\" y1=\"%d\" x2=\"%d\" y2=\"%d\" stroke=\"%s\"/>"
        "<line x1=\"%d\" y1=\"%d\" x2=\"%d\" y2=\"%d\" stroke=\"%s\"/>"
        "<text x=\"%d\" y=\"%d\" font-size=\"12\" text-anchor=\"end\" fill=\"%s\">%s</text>"
        "<text x=\"%d\" y=\"%d\" font-size=\"12\" text-anchor=\"end\" fill=\"%s\">%s</text>"
        "<polyline fill=\"none\" stroke=\"%s\" stroke-width=\"2\" points=\"%s\"/>%s</svg>"
        % (width, height, width, height, esc(title), esc(title),
           pad_left, pad_top, pad_left, pad_top + inner_h, _AXIS,
           pad_left, pad_top + inner_h, pad_left + inner_w, pad_top + inner_h, _AXIS,
           pad_left - 6, pad_top + 4, _AXIS, esc(high), pad_left - 6, pad_top + inner_h, _AXIS, esc(low),
           _LINE, path, "".join(labels))
    )


def bar_chart_svg(
    bars: Sequence[Tuple[str, Optional[Decimal], Optional[Decimal]]], title: str, width: int = 760, row_height: int = 34,
    legend: Tuple[str, str] = ("對交易池平均", "對基準"),
) -> str:
    """每組兩條橫向長條（預設：對交易池、對基準）；全部缺值就「無圖」。"""
    usable = [(label, pool, bench) for label, pool, bench in bars if pool is not None or bench is not None]
    if not usable:
        return "<p class=\"nochart\">無圖：%s 沒有可畫的分組平均</p>" % esc(title)
    magnitudes = [abs(value) for _, pool, bench in usable for value in (pool, bench) if value is not None]
    scale = max(magnitudes) or Decimal(1)
    pad_left, pad_right, pad_top = 120, 16, 28
    inner_w = width - pad_left - pad_right
    zero_x = pad_left + inner_w / 2
    height = pad_top + row_height * len(usable) + 12
    parts = ["<line x1=\"%s\" y1=\"%d\" x2=\"%s\" y2=\"%d\" stroke=\"%s\"/>" % (_fmt(zero_x), pad_top - 6, _fmt(zero_x), height - 8, _AXIS)]
    parts.append("<rect x=\"%d\" y=\"6\" width=\"10\" height=\"10\" fill=\"%s\"/><text x=\"%d\" y=\"15\" font-size=\"12\">%s</text>" % (pad_left, _BAR_POOL, pad_left + 14, esc(legend[0])))
    parts.append("<rect x=\"%d\" y=\"6\" width=\"10\" height=\"10\" fill=\"%s\"/><text x=\"%d\" y=\"15\" font-size=\"12\">%s</text>" % (pad_left + 160, _BAR_BENCH, pad_left + 174, esc(legend[1])))
    for index, (label, pool, bench) in enumerate(usable):
        top = pad_top + row_height * index
        parts.append("<text x=\"%d\" y=\"%d\" font-size=\"13\" text-anchor=\"end\">%s</text>" % (pad_left - 8, top + 20, esc(label)))
        for offset, value, color in ((2, pool, _BAR_POOL), (16, bench, _BAR_BENCH)):
            if value is None:
                continue
            length = float(abs(value) / scale) * (inner_w / 2)
            x = zero_x if value >= 0 else zero_x - length
            parts.append("<rect x=\"%s\" y=\"%d\" width=\"%s\" height=\"12\" fill=\"%s\"><title>%s</title></rect>" % (
                _fmt(x), top + offset, _fmt(length), color, esc("%s：%s" % (label, _pct(value)))))
    return "<svg viewBox=\"0 0 %d %d\" width=\"%d\" height=\"%d\" role=\"img\" aria-label=\"%s\"><title>%s</title>%s</svg>" % (
        width, height, width, height, esc(title), esc(title), "".join(parts))


def _footer(sha256: Optional[str], verify_command: str, note: str = "") -> str:
    return "<p>JSON content_sha256：<code>%s</code></p><p>重算驗證：<code>%s</code></p><p>本頁由 JSON 排版而來，不重算數字，不進 manifest；%s</p>" % (
        _text(sha256, "無"), esc(verify_command), esc(note) or "請以 JSON 為準。")


# ---------------------------------------------------------------- replay
_REPLAY_STATUS = {"pending_execution": "等收盤價", "complete": "完成", "executed": "完成"}


def render_replay_html(report: Mapping[str, object]) -> str:
    metrics = report.get("metrics") or {}
    template = (report.get("provenance") or {}).get("policy_template") or {}
    inputs = report.get("inputs") or {}
    replay_id = report.get("replay_id", "未封存")
    lead = ""
    sections: List[str] = []
    done = "已完成" if report.get("status") == "complete" else "還有成交日在等收盤價，結果先算到目前為止"
    sections.append("<h2>目前結論</h2><p>用策略設定 %s，重算 %d 個決策日（AI 當時的買賣意見不變，只重算程式配置的部分）。%s。</p>" % (
        esc(template.get("policy_id")), len(inputs.get("run_ids") or []), done))
    annualized = metrics.get("annualized_return") or {}
    sharpe = metrics.get("sharpe") or {}
    sections.append("<h2>績效</h2>" + _table(("項目", "數值"), [
        [("帳戶起始／期末淨值", False), ("%s ／ %s" % (_text(metrics.get("start_nav")), _text(metrics.get("end_nav"))), True)],
        [("完成成交日", False), (esc(metrics.get("trading_days")), True)],
        [("總報酬", False), (_pct(metrics.get("total_return")), True)],
        [("年化報酬", False), ("%s%s" % (_pct(annualized.get("value")), "" if annualized.get("reliable") else "（樣本不足，不可靠）") if annualized.get("status") == "available"
                               else "無（%s）" % esc(annualized.get("reason")), True)],
        [("最大回撤", False), (_pct(metrics.get("max_drawdown")), True)],
        [("夏普值（報酬相對波動）", False), (esc(sharpe.get("value")) if sharpe.get("status") == "available" else "無（%s）" % esc(sharpe.get("reason")), True)],
        [("換手率", False), (_pct(metrics.get("turnover_rate")), True)],
        [("總費稅", False), (_text(metrics.get("total_fees_and_taxes")), True)],
        [("成交筆數", False), (esc(metrics.get("fill_count")), True)],
    ]))
    if int(metrics.get("trading_days") or 0) < 20:
        sections.append("<p class=\"notice\">只有 %s 個成交日：以上數字沒有統計意義，只證明回放流程可跑。</p>" % esc(metrics.get("trading_days")))
    points = [(str(row.get("trade_date")), value) for row in report.get("nav_series") or [] for value in [_decimal(row.get("nav"))] if value is not None]
    sections.append("<h2>每日淨值</h2>" + line_chart_svg(points, "每日淨值"))
    day_rows = []
    for day in report.get("days") or []:
        execution = day.get("execution") or {}
        policy = day.get("policy") or {}
        stance = _text(policy.get("cash_stance"), "-")
        if policy.get("stance_raised_to_floor"):
            stance += "（回撤下限提高自 %s）" % esc(policy.get("archived_cash_stance"))
        day_rows.append([
            (esc(day.get("index")), True), (_text(day.get("trade_date"), "待官方價格"), False),
            (_REPLAY_STATUS.get(str(day.get("status")), esc(day.get("status"))), False), (_text(day.get("decision_status"), "-"), False), (stance, False),
            (esc(len(day.get("orders") or [])), True), (esc(len(execution.get("fills") or [])), True),
            (esc(len(execution.get("unfilled_orders") or [])), True), (_text((day.get("close") or {}).get("nav"), "-"), True),
        ])
    sections.append("<h2>逐日結果</h2>" + _table(("第幾天", "成交日", "狀態", "決策", "現金姿態", "訂單", "成交", "未成交", "日終淨值"), day_rows))
    notes = []
    for day in report.get("days") or []:
        if day.get("pending_reason"):
            notes.append("第 %s 天：還沒成交：%s" % (day.get("index"), day["pending_reason"]))
        if (day.get("guard") or {}).get("failed_rules"):
            notes.append("第 %s 天：風控檢查未通過：%s（重算不重現風險 Agent 的修正，當日不下單）" % (
                day.get("index"), "、".join(str(rule) for rule in day["guard"]["failed_rules"])))
        if (day.get("policy") or {}).get("available_at_overridden"):
            notes.append("第 %s 天：策略設定的版本比當天決策晚，重算時視為當天已生效" % day.get("index"))
    if report.get("not_replayed_run_ids"):
        notes.append("之後還沒重算的決策：%d 個" % len(report["not_replayed_run_ids"]))
    if notes:
        sections.append("<h3>備註</h3>" + _list(notes))
    sections.append("<h2>限制</h2>" + _list(report.get("limitations") or []))
    return _page("策略重算報告：%s" % _taipei_date(inputs.get("as_of"), replay_id), lead, sections, "")


# ---------------------------------------------------------------- grid
_GROUP_LABELS = {"by_conviction": "信心等級（buy／add）", "by_intent": "交易意圖", "by_bull_strength": "多頭強度",
                 "by_bear_strength": "空頭強度", "by_net_strength": "多空強度差（多－空）"}


_STATUS_TEXT = {"ok": "樣本足夠", "insufficient_sample": "樣本不足",
                "exit_price_pending": "還沒到期（等收盤價）", "pending": "還沒到期（等收盤價）",
                "available": "有資料", "unavailable": "沒有資料", "ex_rights": "除權息，略過"}


def _status_text(value: object) -> str:
    return esc(_STATUS_TEXT.get(str(value), value))


def _beat_text(beat: Mapping[str, object]) -> str:
    if not beat or beat.get("rate") is None:
        return "無"
    return "%s（%s／%s，持平 %s）" % (_pct(beat.get("rate")), esc(beat.get("wins")), esc(beat.get("n")), esc(beat.get("ties")))



def _taipei_date(as_of: object, fallback: object) -> str:
    """報告標題用的台北日期；解析不了就用原編號。"""
    try:
        parsed = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        return parsed.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return str(fallback)

def _hkey(h: object) -> int:
    return int(h) if str(h).isdigit() else 0


def _mean(values: Sequence[Decimal]) -> Optional[Decimal]:
    return sum(values, Decimal(0)) / len(values) if values else None


def render_grid_html(grid: Mapping[str, object]) -> str:
    """回測報告：先給結論，再給逐日結果；細節收合。買進股平均是本頁對 JSON 列的簡單平均，數字仍以 JSON 為準。"""
    inputs = grid.get("inputs") or {}
    provenance = grid.get("provenance") or {}
    grid_id = grid.get("grid_id", "未封存")
    required = esc(provenance.get("required_decision_days"))
    days = grid.get("days") or []
    rows = grid.get("rows") or []
    horizons = sorted((grid.get("horizons") or {}).items(), key=lambda pair: _hkey(pair[0]))
    horizon_names = [name for name, _ in horizons]
    benchmark = esc(inputs.get("benchmark"))
    sections: List[str] = []

    # 1. 結論
    ready = [name for name, entry in horizons if entry.get("status") == "ok"]
    if ready:
        verdict = "有 %s 日的資料已達標，可看下面的分組平均。" % "、".join(esc(name) for name in ready)
    else:
        verdict = "資料還太少（每個期間至少要 %s 個決策日到期），先不下結論，只看下面的逐日結果。" % required
    sections.append("<h2>目前結論</h2><p>共 %d 個決策日、%d 筆（每天 × 每檔股票）。%s</p>" % (len(days), len(rows), verdict))

    # 2. 各期間進度
    progress = []
    for name, entry in horizons:
        pending = sorted((entry.get("pending_days") or {}).keys())
        progress.append([(esc("%s 個交易日後" % name), False), (esc(entry.get("complete_days")), True),
                         (esc(len(pending)), True), (esc(required), True), (_status_text(entry.get("status")), False)])
    sections.append("<h2>各期間進度</h2><p>決策後過了多久才算結果。已到期的決策日要達到門檻，才會給平均與勝率。</p>" + _table(
        ("看多久之後", "已到期（天）", "還沒到期（天）", "需要（天）", "結論"), progress))

    # 3. 買進的股票 vs 交易池 vs 基準（逐日）
    for name in horizon_names:
        table_rows = []
        for day in days:
            cell = (day.get("horizons") or {}).get(name) or {}
            if cell.get("status") != "complete":
                continue
            bought = [_decimal(row["horizons"][name].get("return")) for row in rows
                      if row.get("entry_day") == day.get("entry_day") and row.get("intent") in ("buy", "add")
                      and (row.get("horizons") or {}).get(name, {}).get("status") == "complete"]
            bought = [value for value in bought if value is not None]
            average = _mean(bought)
            pool = _decimal(cell.get("pool_mean"))
            table_rows.append([
                (esc(day.get("entry_day")), False), (esc(cell.get("exit_day")), False), (esc(len(bought)), True),
                (_pct(average) if average is not None else "—", True), (_pct(pool) if pool is not None else "—", True),
                (_pct(cell.get("benchmark_return")) if cell.get("benchmark_status") == "available" else "—", True),
                (_pct(average - pool) if average is not None and pool is not None else "—", True)])
        if table_rows:
            sections.append("<h2>AI 買進的股票，%s 個交易日後</h2>" % esc(name) + _table(
                ("決策日", "結算日", "買進檔數", "買進股平均報酬", "交易池平均", benchmark, "買進股 − 池平均"), table_rows))

    # 4. 達標期間的分組平均
    for name, entry in horizons:
        if entry.get("status") != "ok":
            continue
        conviction = entry.get("by_conviction") or {}
        sections.append("<h2>決策後 %s 個交易日：分組平均</h2>" % esc(name) + bar_chart_svg(
            [(label, _decimal(row.get("mean_excess_vs_pool")), _decimal(row.get("mean_excess_vs_benchmark"))) for label, row in conviction.items()],
            "決策後 %s 日：各信心等級的平均超額" % name))
        for key, label in _GROUP_LABELS.items():
            group_rows = [[(esc(group), False), (esc(row.get("n_obs")), True), (_pct(row.get("mean_return")), True),
                           (_pct(row.get("mean_excess_vs_pool")), True), (_beat_text(row.get("beat_pool") or {}), True),
                           (_pct(row.get("mean_excess_vs_benchmark")), True), (_beat_text(row.get("beat_benchmark") or {}), True)]
                          for group, row in (entry.get(key) or {}).items()]
            block = _table(("分類", "筆數", "平均報酬", "比池平均多／少", "贏過池的比例", "比基準多／少", "贏過基準的比例"), group_rows)
            sections.append(("<h3>%s</h3>" % esc(label) + block) if key in ("by_conviction", "by_intent")
                            else "<details><summary>%s（展開）</summary>%s</details>" % (esc(label), block))

    # 5. 每檔明細（收合）
    detail = []
    for row in rows:
        cells = [(esc(row.get("entry_day")), False), (esc(row.get("symbol")), False), (esc(row.get("intent")), False), (_text(row.get("conviction"), "-"), False)]
        for name in horizon_names:
            cell = (row.get("horizons") or {}).get(name) or {}
            cells.append((_pct(cell.get("return")) if cell.get("status") == "complete" else "—", True))
        detail.append(cells)
    sections.append("<h2>每檔股票明細</h2><details><summary>展開 %d 列</summary>%s</details>" % (
        len(detail), _table(("決策日", "股票", "意圖", "信心", *("%s 日後報酬" % esc(name) for name in horizon_names)), detail)))

    # 6. 怎麼讀（收合）與限制
    sections.append("<h2>怎麼讀這份報告</h2><details><summary>展開</summary>" + _table(("名詞", "意思"), [
        [("買進股平均報酬", False), ("AI 決定買進或加碼的股票，在決策後那段時間的平均報酬", False)],
        [("交易池平均", False), ("同一天 150 檔候選股的平均報酬，當作「隨便買」的比較對象", False)],
        [(benchmark, False), ("同一段時間買 %s 的報酬" % benchmark, False)],
        [("買進股 − 池平均", False), ("大於 0 代表 AI 看好的股票比隨便買好", False)],
        [("需要（天）", False), ("至少要這麼多個決策日到期才給平均與勝率，避免用太少資料誤導", False)],
        [("注意", False), ("這只是用過去存檔的決策重算，不能證明 AI 判斷準不準；只有比賽期間的即時紀錄才算證據", False)],
    ]) + "</details>")
    skipped = [("未納入 %s：%s" % (item.get("run_id"), item.get("reason"))) for item in (provenance.get("selection") or {}).get("skipped") or []]
    if skipped:
        sections.append("<h3>未納入的決策</h3>" + _list(skipped))
    return _page("回測報告：%s" % _taipei_date(inputs.get("as_of"), grid_id), "", sections, "")


# ---------------------------------------------------------------- agent evaluation
_EVAL_LABELS = {"technical": "技術分析師看法", "fundamental": "基本面分析師看法", "event": "事件分析師看法",
                "event_materiality": "事件重要程度", "intent": "交易意圖／信心", "cash_stance": "現金姿態", "orders": "委託股票與方向"}


def _summary_table(summary: Mapping[str, object]) -> str:
    rows = []
    for name, label in _EVAL_LABELS.items():
        row = summary.get(name) if isinstance(summary, Mapping) else None
        if row:
            rows.append([(esc(label), False), (esc(row.get("pairs")), True), (_pct(row.get("mean_agreement")), True),
                         (_pct(row.get("min_agreement")), True), (esc(row.get("opposite")), True)])
        else:
            rows.append([(esc(label), False), ("0", True), ("未比較", True), ("未比較", True), ("-", True)])
    return _table(("項目", "比較組數", "平均相同", "最低相同", "正負相反"), rows)


def render_eval_html(report: Mapping[str, object]) -> str:
    overall = report.get("overall") or {}
    eval_id = report.get("eval_id", "未知")
    lead = ""
    sections: List[str] = []
    sections.append("<h2>評測概況</h2>" + _table(("項目", "內容"), [
        [("評測集", False), (esc(report.get("suite")), False)],
        [("每份考卷重跑", False), ("%s 次" % esc(report.get("repeats")), False)],
        [("範圍", False), ("只到分析師" if report.get("stop_after") == "analysts" else "完整決策鏈", False)],
        [("總共跑了", False), ("%s 次，失敗 %s 次" % (esc(overall.get("runs")), esc(overall.get("failed_runs"))), False)],
        [("估算用量", False), ("$%s" % esc(overall.get("estimated_usage_usd")), False)],
        [("注意", False), ("「相同」只表示結論一樣，不代表對錯", False)],
    ]))
    versus = overall.get("versus_reference") or {}
    stability = overall.get("stability") or {}
    bars = [(label, _decimal((versus.get(name) or {}).get("mean_agreement")), _decimal((stability.get(name) or {}).get("mean_agreement")))
            for name, label in _EVAL_LABELS.items()]
    sections.append("<h2>一致度</h2>" + bar_chart_svg(bars, "平均相同比例", legend=("與當時結論", "重跑之間")))
    sections.append("<h3>與當時結論一致度</h3>" + _summary_table(versus))
    sections.append("<h3>重跑穩定度（同一份輸入兩兩比較）</h3>" + _summary_table(stability))
    answers = report.get("answers") or {}
    answer_rows = []
    for horizon, groups in (answers.get("overall") or {}).items():
        for who, label in (("reference", "當時結論"), ("reruns", "重跑")):
            for name, row in (groups.get(who) or {}).items():
                if row.get("hit_rate") is not None:
                    rate = _pct(row["hit_rate"])
                elif row.get("status") == "insufficient_sample":
                    rate = "樣本不足（需 %s 天）" % esc(answers.get("required_decision_days"))
                else:
                    rate = "無"
                answer_rows.append([(esc(horizon), True), (label, False), (esc(_EVAL_LABELS.get(name, name)), False),
                                    (esc(row.get("complete_days")), True), (esc(row.get("n")), True), (rate, True)])
    if not answer_rows:
        answer_rows.append([("-", True), ("-", False), ("還沒有任何考卷的股價到齊", False), ("0", True), ("0", True), ("等股價", True)])
    sections.append("<h2>準不準（之後的實際股價）</h2><p>看好＝分析師正面、交易 buy／add、買單；看壞＝負面、trim／exit、賣單。命中＝看好的贏過交易池平均、看壞的輸給平均（官方成交均價）。</p>"
                    + _table(("看多久之後（天）", "對象", "項目", "完整決策日", "筆數", "命中率"), answer_rows))
    pending = []
    for case in answers.get("cases") or []:
        items = ["%s 日：%s" % (h, item.get("status")) for h, item in (case.get("horizons") or {}).items() if item.get("status") != "complete"]
        if items:
            pending.append("%s（成交日 %s）尚未有答案：%s" % (case.get("case_id"), case.get("entry_day") or "未定", "、".join(items)))
    if pending:
        sections.append(_list(pending))
    case_sections = []
    for case in report.get("cases") or []:
        notes = []
        if (case.get("policy") or {}).get("available_at_overridden"):
            notes.append("策略設定版本晚於考卷，評測時把 available_at 提前到考卷 cutoff。")
        for item in case.get("failed_runs") or []:
            notes.append("第 %s 次失敗：%s" % (item.get("repeat"), "；".join(str(e) for e in item.get("errors") or []) or "未知"))
        for item in case.get("process") or []:
            notes.append("第 %s 次：%s 次呼叫，重試 %s，驗證失敗 %s 次，$%s，Agent 時間 %s 秒%s。" % (
                item.get("repeat"), item.get("calls"), "、".join(str(s) for s in item.get("retried_steps") or []) or "無",
                len(item.get("validation_failures") or []), item.get("estimated_usage_usd"), item.get("agent_seconds"),
                "；續跑沿用 %s 份原始輸出" % item["resumed_from_attempts"] if item.get("resumed_from_attempts") is not None else ""))
        case_sections.append("<h3>%s</h3>%s" % (esc(case.get("case_id")), _list(notes)))
    sections.append("<h2>每份考卷</h2>" + "".join(case_sections))
    if report.get("resumed_at"):
        sections.append("<p class=\"notice\">本評測於 %s 續跑；只重跑未完成卷次，已完成卷次沿用原結果。</p>" % esc(report["resumed_at"]))
    return _page("評測報告：%s" % _taipei_date(report.get("generated_at"), eval_id), lead, sections, "")
