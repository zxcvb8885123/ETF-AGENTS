from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.core import parse_aware_time, parse_decimal
from .corporate_contracts import FinancialStatementFact, TAIPEI_TIMEZONE


def _parse_roc_date(value: str):
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) != 7:
        raise ValueError("無法解析民國日期：%s" % value)
    return datetime(
        int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7])
    ).date()


def _parse_roc_month(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) != 5:
        raise ValueError("無法解析民國年月：%s" % value)
    year = int(digits[:3]) + 1911
    month = int(digits[3:5])
    if not 1 <= month <= 12:
        raise ValueError("月份不正確：%s" % value)
    return "%04d-%02d" % (year, month)


def _parse_roc_year(value: str) -> int:
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) != 3:
        raise ValueError("無法解析民國年度：%s" % value)
    return int(digits) + 1911


def _parse_quarter(value: str) -> int:
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) != 1 or not 1 <= int(digits) <= 4:
        raise ValueError("季度必須介於 1 與 4：%s" % value)
    return int(digits)


def _financial_period(
    fiscal_year: int, fiscal_quarter: int, statement_type: str
) -> Tuple[Optional[str], str, str]:
    quarter_end_month = fiscal_quarter * 3
    if quarter_end_month == 12:
        period_end = date(fiscal_year, 12, 31)
    else:
        period_end = date(fiscal_year, quarter_end_month + 1, 1) - timedelta(days=1)
    if statement_type == "income_statement":
        return (
            date(fiscal_year, 1, 1).isoformat(),
            period_end.isoformat(),
            "cumulative_to_quarter",
        )
    if statement_type == "balance_sheet":
        return None, period_end.isoformat(), "point_in_time"
    raise ValueError("不支援的財報類型：%s" % statement_type)


_COMMON_INCOME_METRICS = (
    ("profit_before_tax", ("稅前淨利（淨損）",)),
    ("net_income", ("本期淨利（淨損）",)),
    ("basic_eps", ("基本每股盈餘（元）",)),
)

_COMMON_BALANCE_METRICS = (
    ("total_assets", ("資產總額", "資產總計")),
    ("total_liabilities", ("負債總額", "負債總計")),
    ("total_equity", ("權益總額", "權益總計")),
)

_INDUSTRY_FINANCIAL_METRICS = {
    ("income_statement", "ci"): (
        ("revenue", ("營業收入",)),
        ("operating_profit", ("營業利益（損失）",)),
    ),
    ("income_statement", "mim"): (
        ("revenue", ("營業收入",)),
        ("operating_profit", ("營業利益（損失）",)),
    ),
}

# 金融業原始欄位另存，不將「淨收益」改稱一般業「營業收入」。
_FINANCIAL_INDUSTRIES = {"fh", "basi", "bd", "ins"}
for _financial_industry in _FINANCIAL_INDUSTRIES:
    _INDUSTRY_FINANCIAL_METRICS[("income_statement", _financial_industry)] = (
        ("net_revenue", ("淨收益",)),
    )


def _financial_field(row, fields):
    """同義欄位同時存在時必須等值；不以欄位順序掩蓋來源衝突。"""
    present = [(field, row[field]) for field in fields
               if _text(row.get(field)) not in {"", "-", "--", "N/A", "null", "None"}]
    if not present:
        return None, None
    values = [parse_decimal(_text(value).replace(",", ""), field,
                            parse_message="%(field)s 無法解析財報數值：%(value)s")
              for field, value in present]
    if any(value != values[0] for value in values[1:]):
        raise ValueError("財報同義欄位金額衝突：%s" % "、".join(field for field, _ in present))
    return present[0][0], values[0]


def _financial_facts(
    row: Mapping[str, object],
    statement_type: str,
    industry: str,
    unit_multiplier: int,
) -> Tuple[FinancialStatementFact, ...]:
    """以明確業別 mapping 產生可比較事實，原始欄位仍完整留在文件 body。"""

    if statement_type == "income_statement":
        definitions = (
            _INDUSTRY_FINANCIAL_METRICS.get((statement_type, industry), ())
            + _COMMON_INCOME_METRICS
        )
    elif statement_type == "balance_sheet":
        definitions = _COMMON_BALANCE_METRICS
    else:
        raise ValueError("不支援的財報類型：%s" % statement_type)

    facts: List[FinancialStatementFact] = []
    for metric_key, source_fields in definitions:
        if statement_type == "income_statement" and industry in _FINANCIAL_INDUSTRIES and metric_key == "net_income":
            source_fields = ("本期稅後淨利（淨損）", "本期淨利（淨損）")
        source_field, raw_value = _financial_field(row, source_fields)
        if source_field is None:
            facts.append(
                FinancialStatementFact(
                    metric_key=metric_key,
                    source_field=None,
                    value=None,
                    value_status="not_reported",
                    currency="TWD",
                    unit_multiplier=(1 if metric_key == "basic_eps" else unit_multiplier),
                )
            )
            continue
        facts.append(
            FinancialStatementFact(
                metric_key=metric_key,
                source_field=source_field,
                value=raw_value,
                value_status="provided",
                currency="TWD",
                unit_multiplier=(1 if metric_key == "basic_eps" else unit_multiplier),
            )
        )
    return tuple(facts)


def _first_nonempty_field(
    row: Mapping[str, object], source_fields: Sequence[str]
) -> Tuple[Optional[str], Optional[object]]:
    for source_field in source_fields:
        value = row.get(source_field)
        if _text(value) not in {"", "-", "--", "N/A", "null", "None"}:
            return source_field, value
    return None, None


def _is_empty_financial_placeholder(row: Mapping[str, object]) -> bool:
    return not _text(row.get("公司代號") or row.get("SecuritiesCompanyCode")) and not _text(
        row.get("公司名稱") or row.get("CompanyName")
    )


def _parse_roc_datetime(date_value: str, time_value: str) -> datetime:
    date = _parse_roc_date(date_value)
    digits = "".join(character for character in time_value if character.isdigit()).zfill(6)
    if len(digits) != 6:
        raise ValueError("無法解析時間：%s" % time_value)
    return datetime(
        date.year,
        date.month,
        date.day,
        int(digits[:2]),
        int(digits[2:4]),
        int(digits[4:6]),
        tzinfo=TAIPEI_TIMEZONE,
    )


def _required_text(row: Dict[str, object], *keys: str) -> str:
    for key in keys:
        value = _text(row.get(key))
        if value:
            return value
    raise ValueError("缺少必要欄位：%s" % "/".join(keys))


def _required_text_by_normalized_key(row: Dict[str, object], key: str) -> str:
    for raw_key, raw_value in row.items():
        if str(raw_key).strip() == key:
            value = _text(raw_value)
            if value:
                return value
    raise ValueError("缺少必要欄位：%s" % key)


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _optional_decimal(value: object) -> Optional[Decimal]:
    text = _text(value).replace(",", "")
    if text in {"", "-", "--", "N/A", "null", "None"}:
        return None
    try:
        parsed = Decimal(text)
        if not parsed.is_finite():
            raise ValueError("數字必須為有限值：%s" % value)
        return parsed
    except InvalidOperation:
        raise ValueError("無法解析數字：%s" % value)


def _decimal_text(value: Optional[Decimal]) -> Optional[str]:
    return str(value) if value is not None else None


def _require_aware_datetime(value: str, field: str) -> datetime:
    return parse_aware_time(value, field, to_utc=False)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
