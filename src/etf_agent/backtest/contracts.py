"""Strict contracts and historical clock for deterministic ETF backtests."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Mapping, Sequence

from etf_agent.decision.contracts import artifact_content_sha256, canonical_sha256, parse_time


BACKTEST_SCHEMA_VERSION = "1.0"


class BacktestToolError(ValueError):
    """Raised when a historical replay cannot be proven point-in-time safe."""


def decimal_value(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise BacktestToolError("%s 必須是有限數值" % field) from error
    if not result.is_finite():
        raise BacktestToolError("%s 必須是有限數值" % field)
    return result


def required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise BacktestToolError("缺少必要字串欄位：%s" % field)
    return value.strip()


def request_sha256(request: Mapping[str, object]) -> str:
    body = dict(request)
    body.pop("content_sha256", None)
    return canonical_sha256(body)


def _reject_unknown(payload: Mapping[str, object], allowed: set, label: str, errors: List[str]) -> None:
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        errors.append("%s 含未允許欄位：%s" % (label, ", ".join(unknown)))


class BacktestRequestValidator:
    """Validate all fixed assumptions before a historical clock may start."""

    FIELDS = {
        "schema_version", "request_id", "start_date", "end_date", "timezone",
        "decision_time", "initial_account", "calendar", "data_manifest",
        "execution_assumptions", "content_sha256",
    }

    def validate(self, request: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        _reject_unknown(request, self.FIELDS, "BacktestRequest", errors)
        for field in ("schema_version", "request_id", "start_date", "end_date", "timezone", "decision_time", "content_sha256"):
            try:
                required_string(request, field)
            except BacktestToolError as error:
                errors.append(str(error))
        if request.get("schema_version") != BACKTEST_SCHEMA_VERSION:
            errors.append("BacktestRequest.schema_version 不一致")
        if request.get("timezone") != "Asia/Taipei":
            errors.append("BacktestRequest.timezone 必須為 Asia/Taipei")
        if request.get("content_sha256") != request_sha256(request):
            errors.append("BacktestRequest.content_sha256 與內容不一致")
        try:
            start = date.fromisoformat(str(request.get("start_date")))
            end = date.fromisoformat(str(request.get("end_date")))
            if start > end:
                errors.append("BacktestRequest.start_date 不得晚於 end_date")
            parsed_time = time.fromisoformat(str(request.get("decision_time")))
            if parsed_time != time(8, 55):
                errors.append("BacktestRequest.decision_time 第一版固定為 08:55")
        except ValueError:
            errors.append("BacktestRequest 日期或 decision_time 格式錯誤")
        account = request.get("initial_account")
        if not isinstance(account, Mapping):
            errors.append("initial_account 必須是物件")
        else:
            _reject_unknown(account, {"settled_cash", "unsettled_cash", "positions"}, "initial_account", errors)
            try:
                if decimal_value(account.get("settled_cash"), "initial_account.settled_cash") < 0:
                    errors.append("initial_account.settled_cash 不得為負")
                if decimal_value(account.get("unsettled_cash"), "initial_account.unsettled_cash") < 0:
                    errors.append("initial_account.unsettled_cash 不得為負")
            except BacktestToolError as error:
                errors.append(str(error))
            self._validate_positions(account.get("positions"), "initial_account.positions", errors)
        calendar = request.get("calendar")
        if not isinstance(calendar, list) or not calendar:
            errors.append("calendar 必須是非空陣列")
        else:
            previous = ""
            for index, item in enumerate(calendar):
                prefix = "calendar[%d]" % index
                if not isinstance(item, Mapping):
                    errors.append("%s 必須是物件" % prefix)
                    continue
                _reject_unknown(item, {"trade_date", "decision_cutoff", "execution_at", "close_at", "settlement_date"}, prefix, errors)
                trade_date = str(item.get("trade_date", ""))
                try:
                    date.fromisoformat(trade_date)
                    decision = parse_time(item.get("decision_cutoff"), prefix + ".decision_cutoff")
                    execution = parse_time(item.get("execution_at"), prefix + ".execution_at")
                    close = parse_time(item.get("close_at"), prefix + ".close_at")
                    settlement = date.fromisoformat(str(item.get("settlement_date")))
                    raw_decision = datetime.fromisoformat(str(item["decision_cutoff"]).replace("Z", "+00:00"))
                    raw_execution = datetime.fromisoformat(str(item["execution_at"]).replace("Z", "+00:00"))
                    raw_close = datetime.fromisoformat(str(item["close_at"]).replace("Z", "+00:00"))
                    if raw_decision.date().isoformat() != trade_date or raw_decision.time() != time(8, 55) or raw_decision.utcoffset() != timedelta(hours=8):
                        errors.append("%s.decision_cutoff 必須是交易日 08:55 +08:00" % prefix)
                    for field, value in (("execution_at", raw_execution), ("close_at", raw_close)):
                        if value.date().isoformat() != trade_date or value.utcoffset() != timedelta(hours=8):
                            errors.append("%s.%s 必須是交易日 +08:00 時點" % (prefix, field))
                    if not (decision <= execution <= close):
                        errors.append("%s 時點必須依 decision、execution、close 遞增" % prefix)
                    if settlement < date.fromisoformat(trade_date):
                        errors.append("%s.settlement_date 不得早於 trade_date" % prefix)
                except (ValueError, BacktestToolError):
                    errors.append("%s 日期或時點格式錯誤" % prefix)
                if previous and trade_date <= previous:
                    errors.append("calendar.trade_date 必須嚴格遞增")
                previous = trade_date
        manifest = request.get("data_manifest")
        if not isinstance(manifest, Mapping):
            errors.append("data_manifest 必須是物件")
        else:
            _reject_unknown(manifest, {"mode", "version", "content_sha256"}, "data_manifest", errors)
            if manifest.get("mode") not in {"fixture", "exploratory", "historical_verified"}:
                errors.append("data_manifest.mode 不合法")
            for field in ("version", "content_sha256"):
                try:
                    required_string(manifest, field)
                except BacktestToolError as error:
                    errors.append(str(error))
        assumptions = request.get("execution_assumptions")
        if not isinstance(assumptions, Mapping):
            errors.append("execution_assumptions 必須是物件")
        else:
            _reject_unknown(assumptions, {"price_source_version", "lot_size", "commission_rate", "sell_tax_rate", "minimum_commission", "reuse_sell_proceeds"}, "execution_assumptions", errors)
            if assumptions.get("lot_size") != 1000:
                errors.append("execution_assumptions.lot_size 必須為 1000")
            if assumptions.get("reuse_sell_proceeds") not in {True, False}:
                errors.append("execution_assumptions.reuse_sell_proceeds 必須是布林值")
            for field in ("price_source_version",):
                try:
                    required_string(assumptions, field)
                except BacktestToolError as error:
                    errors.append(str(error))
            for field in ("commission_rate", "sell_tax_rate"):
                try:
                    value = decimal_value(assumptions.get(field), "execution_assumptions." + field)
                    if value < 0 or value > 1:
                        errors.append("execution_assumptions.%s 必須介於 0 與 1" % field)
                except BacktestToolError as error:
                    errors.append(str(error))
            try:
                if decimal_value(assumptions.get("minimum_commission"), "execution_assumptions.minimum_commission") < 0:
                    errors.append("execution_assumptions.minimum_commission 不得為負")
            except BacktestToolError as error:
                errors.append(str(error))
        return errors

    @staticmethod
    def _validate_positions(positions: object, label: str, errors: List[str]) -> None:
        if not isinstance(positions, list):
            errors.append(label + " 必須是陣列")
            return
        seen = set()
        for index, item in enumerate(positions):
            if not isinstance(item, Mapping):
                errors.append("%s[%d] 必須是物件" % (label, index))
                continue
            _reject_unknown(item, {"symbol", "shares", "cost_basis"}, "%s[%d]" % (label, index), errors)
            symbol = item.get("symbol")
            shares = item.get("shares")
            if not isinstance(symbol, str) or not symbol.strip() or symbol.upper() in seen:
                errors.append("%s[%d].symbol 無效或重複" % (label, index))
            else:
                seen.add(symbol.upper())
            if not isinstance(shares, int) or isinstance(shares, bool) or shares <= 0 or shares % 1000:
                errors.append("%s[%d].shares 必須為正整張" % (label, index))
            try:
                if decimal_value(item.get("cost_basis"), "%s[%d].cost_basis" % (label, index)) <= 0:
                    errors.append("%s[%d].cost_basis 必須大於 0" % (label, index))
            except BacktestToolError as error:
                errors.append(str(error))


class HistoricalClock:
    """Expose only the requested, versioned trading sessions."""

    def __init__(self, request: Mapping[str, object]):
        errors = BacktestRequestValidator().validate(request)
        if errors:
            raise BacktestToolError("BacktestRequest 驗證失敗：" + "；".join(errors))
        self.request = dict(request)
        self.sessions = [
            dict(item) for item in request["calendar"]
            if request["start_date"] <= item["trade_date"] <= request["end_date"]
        ]
        if not self.sessions:
            raise BacktestToolError("請求期間沒有版本化交易日")

    def __iter__(self):
        return iter(self.sessions)
