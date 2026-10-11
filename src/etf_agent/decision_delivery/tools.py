"""報告的確定性格式與交易時鐘工具。"""

from datetime import timezone, timedelta
from html import escape
from etf_agent.core import parse_aware_time, parse_decimal, decimal_string
from .contracts import DeliveryError

TAIPEI = timezone(timedelta(hours=8))


def trade_date(bundle):
    status = bundle.get("trading_status_bundle") or {}
    session = status.get("target_session") or status.get("request", {}).get("target_session") or {}
    start = session.get("start")
    return parse_aware_time(start, "target_session.start", error=DeliveryError).astimezone(TAIPEI).date().isoformat() if start else None


def cell(value):
    if value is None:
        return "未提供"
    # 外部文字只能呈現為文字，避免 HTML／Markdown 偽造報告與連結。
    text = escape(str(value), quote=True)
    for token in ("\\", "`", "*", "_", "[", "]", "#", "|", "~"):
        text = text.replace(token, "\\" + token)
    return text.replace("\r", "").replace("\n", "<br>")


def percent(value):
    return decimal_string(parse_decimal(value, "比例", error=DeliveryError) * 100) + "%"
