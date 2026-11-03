"""一頁自包含 HTML 報告：只排版既有 JSON 的字串，不重算任何數字。

- 純標準庫；inline SVG 與 inline style；不含 script、外連資源與 ``url(``；頁首帶 CSP meta（``default-src 'none'``）。
- 所有來自報告的文字一律 ``html.escape``。
- 圖是衍生品：NAV 點數不足或沒有可畫的分組平均時顯示「無圖」，不補值。
- 頁尾印 JSON 的 content_sha256 與重算驗證指令；HTML 本身不進 manifest，可隨時由 JSON 重新產生。
"""

from __future__ import annotations

import html
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
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
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
        "<title>%s</title><style>%s</style></head><body><header class=\"hero\"><div class=\"inner\"><h1>%s</h1><p class=\"lead\">%s</p></div></header><main>%s<footer>%s</footer></main></body></html>\n"
        % (CSP, esc(title), _STYLE, esc(title), lead, "".join(sections), footer)
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
    head = "".join("<th>%s</th>" % esc(header) for header in headers)
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
def render_replay_html(report: Mapping[str, object]) -> str:
    metrics = report.get("metrics") or {}
    template = (report.get("provenance") or {}).get("policy_template") or {}
    inputs = report.get("inputs") or {}
    replay_id = report.get("replay_id", "未封存")
    lead = "%s 策略樣板 <code>%s</code>（content_sha256 <code>%s</code>）；%d 個 Decision run；as_of %s；狀態 %s。不呼叫 Agent，只重算確定性程式。" % (
        _notice(report), esc(template.get("policy_id")), esc(template.get("content_sha256")), len(inputs.get("run_ids") or []), esc(inputs.get("as_of")), esc(report.get("status")))
    sections: List[str] = []
    annualized = metrics.get("annualized_return") or {}
    sharpe = metrics.get("sharpe") or {}
    sections.append("<h2>績效</h2>" + _table(("項目", "數值"), [
        [("起始／期末 NAV", False), ("%s ／ %s" % (_text(metrics.get("start_nav")), _text(metrics.get("end_nav"))), True)],
        [("完成成交日", False), (esc(metrics.get("trading_days")), True)],
        [("總報酬", False), (_pct(metrics.get("total_return")), True)],
        [("年化報酬", False), ("%s%s" % (_pct(annualized.get("value")), "" if annualized.get("reliable") else "（樣本不足，不可靠）") if annualized.get("status") == "available"
                               else "無（%s）" % esc(annualized.get("reason")), True)],
        [("最大回撤", False), (_pct(metrics.get("max_drawdown")), True)],
        [("Sharpe", False), (esc(sharpe.get("value")) if sharpe.get("status") == "available" else "unavailable（%s）" % esc(sharpe.get("reason")), True)],
        [("換手率", False), (_pct(metrics.get("turnover_rate")), True)],
        [("總費稅", False), (_text(metrics.get("total_fees_and_taxes")), True)],
        [("成交筆數", False), (esc(metrics.get("fill_count")), True)],
    ]))
    if int(metrics.get("trading_days") or 0) < 20:
        sections.append("<p class=\"notice\">只有 %s 個成交日：以上數字沒有統計意義，只證明回放流程可跑。</p>" % esc(metrics.get("trading_days")))
    points = [(str(row.get("trade_date")), value) for row in report.get("nav_series") or [] for value in [_decimal(row.get("nav"))] if value is not None]
    sections.append("<h2>NAV</h2>" + line_chart_svg(points, "日終 NAV"))
    day_rows = []
    for day in report.get("days") or []:
        execution = day.get("execution") or {}
        policy = day.get("policy") or {}
        stance = _text(policy.get("cash_stance"), "-")
        if policy.get("stance_raised_to_floor"):
            stance += "（回撤下限提高自 %s）" % esc(policy.get("archived_cash_stance"))
        day_rows.append([
            (esc(day.get("index")), True), (_text(day.get("run_id"), "（缺日）"), False), (_text(day.get("trade_date"), "待官方價格"), False),
            (esc(day.get("status")), False), (_text(day.get("decision_status"), "-"), False), (stance, False),
            (esc(len(day.get("orders") or [])), True), (esc(len(execution.get("fills") or [])), True),
            (esc(len(execution.get("unfilled_orders") or [])), True), (_text((day.get("close") or {}).get("nav"), "-"), True),
        ])
    sections.append("<h2>逐日</h2>" + _table(("#", "run", "成交日", "狀態", "決策", "現金姿態", "訂單", "成交", "未成交", "日終 NAV"), day_rows))
    notes = []
    for day in report.get("days") or []:
        if day.get("pending_reason"):
            notes.append("第 %s 天（%s）：成交待定：%s" % (day.get("index"), day.get("run_id") or "缺日", day["pending_reason"]))
        if (day.get("guard") or {}).get("failed_rules"):
            notes.append("第 %s 天（%s）：Guard 未通過：%s（回放不重現風險 Agent 修正，當日不下單）" % (
                day.get("index"), day.get("run_id") or "缺日", "、".join(str(rule) for rule in day["guard"]["failed_rules"])))
        if (day.get("policy") or {}).get("available_at_overridden"):
            notes.append("第 %s 天（%s）：策略樣板版本晚於該日 cutoff，回放把 available_at 提前到 cutoff" % (day.get("index"), day.get("run_id") or "缺日"))
    if report.get("not_replayed_run_ids"):
        notes.append("之後未回放的 run：%s" % "、".join(str(item) for item in report["not_replayed_run_ids"]))
    if notes:
        sections.append("<h3>備註</h3>" + _list(notes))
    runs = []
    for item in (report.get("provenance") or {}).get("runs") or []:
        runs.append([(esc(item.get("run_id")), False), (esc(item.get("decision_cutoff")), False), (esc(item.get("session_day")), False),
                     (esc(item.get("session_day_basis")), False), ("<code>%s</code>" % esc(item.get("manifest_sha256")), False),
                     (esc("、".join(str(x) for x in item.get("superseded_run_ids") or [])) or "-", False)])
    sections.append("<h2>使用的 Decision run</h2>" + _table(("run", "cutoff", "目標時段日", "依據", "manifest", "同日較早略過"), runs))
    sections.append("<h2>限制</h2>" + _list(report.get("limitations") or []))
    return _page("便宜版回測：%s" % replay_id, lead, sections,
                 _footer(report.get("content_sha256"), "cli/policy_replay.py verify --replay-id %s" % replay_id))


# ---------------------------------------------------------------- grid
_GROUP_LABELS = {"by_conviction": "信心等級（buy／add）", "by_intent": "交易意圖", "by_bull_strength": "多頭強度",
                 "by_bear_strength": "空頭強度", "by_net_strength": "多空強度差（多－空）"}


def _beat_text(beat: Mapping[str, object]) -> str:
    if not beat or beat.get("rate") is None:
        return "無"
    return "%s（%s／%s，持平 %s）" % (_pct(beat.get("rate")), esc(beat.get("wins")), esc(beat.get("n")), esc(beat.get("ties")))


def render_grid_html(grid: Mapping[str, object]) -> str:
    inputs = grid.get("inputs") or {}
    provenance = grid.get("provenance") or {}
    grid_id = grid.get("grid_id", "未封存")
    lead = "%s %d 個決策日、%d 列（run × symbol）；基準 <code>%s</code>；as_of %s；provenance_class <code>%s</code>。零 token，只讀已封存 Decision run 與官方成交均價。" % (
        _notice(grid), len(grid.get("days") or []), len(grid.get("rows") or []), esc(inputs.get("benchmark")), esc(inputs.get("as_of")), esc(provenance.get("provenance_class")))
    sections: List[str] = []
    sections.append("<h2>口徑</h2>" + _kv([
        ("成交價", esc(provenance.get("price_basis"))), ("日曆", esc(provenance.get("calendar_basis"))),
        ("最少完整決策日", esc(provenance.get("required_decision_days"))), ("單日最低覆蓋", esc(provenance.get("min_day_coverage"))),
        ("超額（池）", "個股報酬 − 同日交易池平均"), ("超額（基準）", "個股報酬 − 基準 ETF 同窗口報酬；基準缺列或除權息的日子留空"),
        ("贏過比例", "超額 &gt; 0 的筆數 ÷ 超額 ≠ 0 的筆數；剛好 0 另計持平"),
    ]))
    for horizon, entry in sorted((grid.get("horizons") or {}).items(), key=lambda pair: int(pair[0]) if str(pair[0]).isdigit() else 0):
        sections.append("<h2>%s 日</h2>" % esc(horizon))
        bench_days = entry.get("benchmark_days") or {}
        sections.append(_chips([
            ("狀態", esc(entry.get("status"))), ("完整決策日", esc(entry.get("complete_days"))), ("筆數", esc(entry.get("n_obs"))),
            ("排除（無價／除權息）", "%s／%s" % (esc((entry.get("excluded_obs") or {}).get("no_price")), esc((entry.get("excluded_obs") or {}).get("ex_rights")))),
            ("基準可用／缺列／除權息", "%s／%s／%s" % (esc(bench_days.get("available")), esc(bench_days.get("unavailable")), esc(bench_days.get("ex_rights")))),
            ("待答案", esc("、".join("%s：%s" % pair for pair in sorted((entry.get("pending_days") or {}).items()))) or "無"),
        ]))
        if entry.get("status") != "ok":
            sections.append("<p class=\"notice\">完整決策日 %s 少於 %s：只給天數與筆數，不給平均與贏過比例。</p>" % (
                esc(entry.get("complete_days")), esc(provenance.get("required_decision_days"))))
            continue
        conviction = entry.get("by_conviction") or {}
        sections.append(bar_chart_svg(
            [(name, _decimal(row.get("mean_excess_vs_pool")), _decimal(row.get("mean_excess_vs_benchmark"))) for name, row in conviction.items()],
            "%s 日：信心等級的平均超額" % horizon))
        for key, label in _GROUP_LABELS.items():
            groups = entry.get(key) or {}
            rows = []
            for name, row in groups.items():
                rows.append([(esc(name), False), (esc(row.get("n_obs")), True), (_pct(row.get("mean_return")), True),
                             (_pct(row.get("mean_excess_vs_pool")), True), (_beat_text(row.get("beat_pool") or {}), True),
                             (esc(row.get("n_obs_benchmark")), True), (_pct(row.get("mean_excess_vs_benchmark")), True),
                             (_beat_text(row.get("beat_benchmark") or {}), True)])
            block = _table(("等級", "筆數", "平均報酬", "平均超額（池）", "贏過池", "筆數（基準）", "平均超額（基準）", "贏過基準"), rows)
            if key in ("by_conviction", "by_intent"):
                sections.append("<h3>%s</h3>" % esc(label) + block)
            else:
                sections.append("<details><summary>%s（展開）</summary>%s</details>" % (esc(label), block))
    day_rows = []
    for day in grid.get("days") or []:
        cells = [(esc(day.get("entry_day")), False), (esc(day.get("run_id")), False), (esc(day.get("n_items")), True)]
        for horizon in sorted((day.get("horizons") or {}), key=lambda h: int(h) if str(h).isdigit() else 0):
            cell = day["horizons"][horizon] or {}
            if cell.get("status") == "complete":
                cells.append(("→%s 池 %s；基準 %s（%s）" % (esc(cell.get("exit_day")), _pct(cell.get("pool_mean")),
                                                       _pct(cell.get("benchmark_return")), esc(cell.get("benchmark_status"))), False))
            else:
                cells.append((esc(cell.get("status")), False))
        day_rows.append(cells)
    horizon_names = sorted({h for day in grid.get("days") or [] for h in (day.get("horizons") or {})}, key=lambda h: int(h) if str(h).isdigit() else 0)
    sections.append("<h2>決策日</h2>" + _table(("成交日 T", "run", "股票數", *("%s 日" % esc(h) for h in horizon_names)), day_rows))
    row_cells = []
    for row in grid.get("rows") or []:
        cells = [(esc(row.get("entry_day")), False), (esc(row.get("symbol")), False), (esc(row.get("intent")), False),
                 (_text(row.get("conviction"), "-"), False), ("%s／%s（%s）" % (esc(row.get("bull_strength")), esc(row.get("bear_strength")), esc(row.get("net_strength"))), False)]
        for horizon in horizon_names:
            cell = (row.get("horizons") or {}).get(horizon) or {}
            if cell.get("status") == "complete":
                cells.append(("%s｜池 %s｜基準 %s" % (_pct(cell.get("return")), _pct(cell.get("excess_vs_pool")),
                                                   _pct(cell.get("excess_vs_benchmark")) if cell.get("excess_vs_benchmark") is not None else "無"), True))
            else:
                cells.append((esc(cell.get("status")), False))
        row_cells.append(cells)
    sections.append("<h2>每筆決策</h2><details><summary>展開 %d 列（報酬｜超額對池｜超額對基準）</summary>%s</details>" % (
        len(row_cells), _table(("成交日 T", "股票", "意圖", "信心", "多／空（差）", *("%s 日" % esc(h) for h in horizon_names)), row_cells)))
    runs = [[(esc(item.get("run_id")), False), (esc(item.get("decision_cutoff")), False), (esc(item.get("entry_day")), False),
             ("<code>%s</code>" % esc(item.get("manifest_sha256")), False), (esc("、".join(str(x) for x in item.get("superseded_run_ids") or [])) or "-", False)]
            for item in provenance.get("runs") or []]
    sections.append("<h2>使用的 Decision run</h2>" + _table(("run", "cutoff", "成交日 T", "manifest", "同日較早略過"), runs))
    selection = provenance.get("selection") or {}
    extras = ["cutoff 後尚無官方交易日：%s" % ("、".join(selection.get("entry_day_unresolved") or []) or "無"),
              "範圍外的決策日：%s" % ("、".join(selection.get("out_of_range_entry_days") or []) or "無"),
              *("未納入 %s：%s" % (item.get("run_id"), item.get("reason")) for item in selection.get("skipped") or [])]
    sections.append("<h3>挑選紀錄</h3>" + _list(extras))
    sections.append("<h2>限制</h2>" + _list(grid.get("limitations") or []))
    return _page("回測格子：%s" % grid_id, lead, sections, _footer(grid.get("content_sha256"), "cli/agent_eval.py grid-verify --grid-id %s" % grid_id))


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
    lead = "%s 評測集 %s；每份重跑 %s 次；範圍：%s。共 %s 次，失敗 %s 次；估算用量 $%s。「相同」只表示結論一樣，不代表對錯。" % (
        _notice(report), esc(report.get("suite")), esc(report.get("repeats")), "只到分析師" if report.get("stop_after") == "analysts" else "完整決策鏈",
        esc(overall.get("runs")), esc(overall.get("failed_runs")), esc(overall.get("estimated_usage_usd")))
    sections: List[str] = []
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
        answer_rows.append([("-", True), ("-", False), ("還沒有任何考卷的股價到齊", False), ("0", True), ("0", True), ("待答案", True)])
    sections.append("<h2>準不準（之後的實際股價）</h2><p>看好＝分析師正面、交易 buy／add、買單；看壞＝負面、trim／exit、賣單。命中＝看好的贏過交易池平均、看壞的輸給平均（官方成交均價）。</p>"
                    + _table(("天數", "對象", "項目", "完整決策日", "筆數", "命中率"), answer_rows))
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
    body = {key: value for key, value in report.items() if key != "generated_at"}
    return _page("Agent 評測報告：%s" % eval_id, lead, sections,
                 _footer(canonical_sha256(body), "cli/agent_eval.py report --eval-id %s" % eval_id, "此雜湊是 report.json（不含 generated_at）的 canonical SHA-256。"))
