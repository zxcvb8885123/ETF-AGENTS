"""主辦方 DataPack 讀取、帳本對帳與賣出上限檢查。

DataPack 是外部下載的不受信任檔案：欄位採嚴格白名單，數值不得是布林或非有限數，
`null` 與空陣列語意不同（見 game_docs/DataPack_規格書_v4.3.md），一律不互相代替。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from etf_agent.core import parse_aware_time

from .dplan import DPlanError


SCHEMA_VERSIONS = {"3.0"}
MAX_BYTES = 5_000_000
# 帳本與主辦方都用官方成交均價結算，現金只會有手續費取整等小差異；以 nav_tradable 比率容忍，
# 實際差額寫入對帳結果，供依真實資料包校準。
DEFAULT_CASH_TOLERANCE_RATE = Decimal("0.0005")
TOP_FIELDS = {"schema_version", "doc_type", "team_id", "trade_date", "as_of_date", "generated_at",
              "missing", "prev_day_orders", "account", "holdings", "compliance"}
ORDER_FIELDS = {"outcome", "void_reasons", "excluded_codes"}
ACCOUNT_FIELDS = {"cash", "cash_ratio_tradable", "nav_tradable", "aum", "receivable_cash", "holdings_count"}
HOLDING_FIELDS = {"code", "shares", "receivable_shares", "close_price", "tradable_weight"}
COMPLIANCE_FIELDS = {"violations", "notices", "incidents"}
INCIDENT_FIELDS = {"date", "check_code", "status", "message"}
MISSING = {"prev_settlement", "prev_close"}
OUTCOMES = {"none", "unknown", "voided", "executed"}
VOID_REASONS = {"insufficient_funds", "oversell", "no_price", "compliance_fail",
                "dplan_reference_chain", "dplan_decision_coverage", "dplan_round_lot"}
VOID_LABELS = {"insufficient_funds": "資金不足", "oversell": "賣超持股", "no_price": "缺成交均價",
               "compliance_fail": "合規檢查未通過", "dplan_reference_chain": "引用鏈不完整",
               "dplan_decision_coverage": "決策覆蓋不完整", "dplan_round_lot": "非整股"}
CODE_RE = re.compile(r"^[0-9A-Z]{4,6}$")


class DataPackError(DPlanError):
    """DataPack 格式、時間或內容不符規格。"""


def _fields(value: object, allowed: set, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DataPackError("%s 必須是物件" % name)
    if set(value) != allowed:
        extra, lack = sorted(set(value) - allowed), sorted(allowed - set(value))
        raise DataPackError("%s 欄位不符規格（多出 %s；缺少 %s）" % (name, extra or "無", lack or "無"))
    return value


def _number(value: object, name: str, *, nullable: bool = False, integer: bool = False, minimum: Optional[Decimal] = None) -> Optional[Decimal]:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataPackError("%s 必須是數值" % name)
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise DataPackError("%s 不是有效數值" % name) from error
    if not number.is_finite():
        raise DataPackError("%s 必須是有限數值" % name)
    if integer and number != number.to_integral_value():
        raise DataPackError("%s 必須是整數" % name)
    if minimum is not None and number < minimum:
        raise DataPackError("%s 不得小於 %s" % (name, minimum))
    return number


def _date(value: object, name: str, *, nullable: bool = False) -> Optional[str]:
    if value is None and nullable:
        return None
    try:
        if isinstance(value, str) and len(value) == 10:
            return date.fromisoformat(value).isoformat()
    except ValueError:
        pass
    raise DataPackError("%s 必須是 YYYY-MM-DD" % name)


def _strings(value: object, name: str, allowed: Optional[set] = None) -> List[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value) or len(set(value)) != len(value):
        raise DataPackError("%s 必須是不重複的字串陣列" % name)
    if allowed is not None and set(value) - allowed:
        raise DataPackError("%s 含規格外的值：%s" % (name, sorted(set(value) - allowed)))
    return list(value)


def parse_datapack(payload: object, read_at: datetime) -> Dict[str, object]:
    """依規格檢查並回傳原樣內容；任何不一致都拒絕，不推測補值。"""
    pack = _fields(payload, TOP_FIELDS, "DataPack")
    if pack["doc_type"] != "DataPack":
        raise DataPackError("doc_type 必須是 DataPack")
    if pack["schema_version"] not in SCHEMA_VERSIONS:
        raise DataPackError("不支援的 DataPack schema_version：%s" % pack["schema_version"])
    if not isinstance(pack["team_id"], str) or not pack["team_id"].strip():
        raise DataPackError("team_id 不得為空")
    trade_day = _date(pack["trade_date"], "trade_date")
    as_of = _date(pack["as_of_date"], "as_of_date", nullable=True)
    generated = parse_aware_time(pack["generated_at"], "generated_at", error=DataPackError)
    if generated > read_at:
        raise DataPackError("generated_at 晚於讀取時間")
    missing = _strings(pack["missing"], "missing", MISSING)
    orders = _fields(pack["prev_day_orders"], ORDER_FIELDS, "prev_day_orders")
    if orders["outcome"] not in OUTCOMES:
        raise DataPackError("prev_day_orders.outcome 不合規格")
    reasons = _strings(orders["void_reasons"], "prev_day_orders.void_reasons", VOID_REASONS)
    if (orders["outcome"] == "voided") != bool(reasons):
        raise DataPackError("只有 voided 時 void_reasons 才可非空，且 voided 必須至少一項")
    _strings(orders["excluded_codes"], "prev_day_orders.excluded_codes")
    unsettled = "prev_settlement" in missing
    if unsettled:
        if pack["account"] is not None or pack["holdings"] is not None or as_of is not None:
            raise DataPackError("missing 含 prev_settlement 時 account、holdings、as_of_date 必須為 null")
    else:
        account = _fields(pack["account"], ACCOUNT_FIELDS, "account")
        for field in ("cash", "nav_tradable", "aum", "receivable_cash"):
            _number(account[field], "account.%s" % field, minimum=Decimal(0))
        _number(account["cash_ratio_tradable"], "account.cash_ratio_tradable", nullable=True)
        if missing and account["cash_ratio_tradable"] is not None:
            raise DataPackError("missing 非空時 cash_ratio_tradable 必須為 null")
        count = _number(account["holdings_count"], "account.holdings_count", integer=True, minimum=Decimal(0))
        if not isinstance(pack["holdings"], list):
            raise DataPackError("已有結算資料時 holdings 必須是陣列")
        codes = set()
        held = 0
        for index, row in enumerate(pack["holdings"]):
            row = _fields(row, HOLDING_FIELDS, "holdings[%d]" % index)
            if not isinstance(row["code"], str) or not CODE_RE.fullmatch(row["code"]) or row["code"] in codes:
                raise DataPackError("holdings[%d].code 無效或重複" % index)
            codes.add(row["code"])
            shares = _number(row["shares"], "holdings[%d].shares" % index, integer=True, minimum=Decimal(0))
            receivable = _number(row["receivable_shares"], "holdings[%d].receivable_shares" % index, integer=True, minimum=Decimal(0))
            if not shares and not receivable:
                raise DataPackError("holdings[%d] 的 shares 與 receivable_shares 不得同為 0" % index)
            _number(row["close_price"], "holdings[%d].close_price" % index, nullable=True, minimum=Decimal(0))
            _number(row["tradable_weight"], "holdings[%d].tradable_weight" % index, nullable=True, minimum=Decimal(0))
            held += 1 if shares else 0
        if count != held:
            raise DataPackError("account.holdings_count 與 shares>0 的持股檔數不一致")
        if as_of is None and (pack["holdings"] or orders["outcome"] != "none"):
            raise DataPackError("as_of_date 為 null 且非結算未產生時，只能是第一個交易日的空手帳戶")
    if as_of is not None and as_of >= trade_day:
        raise DataPackError("as_of_date 必須早於 trade_date")
    compliance = _fields(pack["compliance"], COMPLIANCE_FIELDS, "compliance")
    _number(compliance["violations"], "compliance.violations", integer=True, minimum=Decimal(0))
    _number(compliance["notices"], "compliance.notices", integer=True, minimum=Decimal(0))
    if not isinstance(compliance["incidents"], list):
        raise DataPackError("compliance.incidents 必須是陣列")
    for index, row in enumerate(compliance["incidents"]):
        row = _fields(row, INCIDENT_FIELDS, "compliance.incidents[%d]" % index)
        _date(row["date"], "compliance.incidents[%d].date" % index)
        if row["status"] not in {"warn", "fail"} or not isinstance(row["check_code"], str) or not isinstance(row["message"], str):
            raise DataPackError("compliance.incidents[%d] 內容不合規格" % index)
    return dict(pack)


def load_datapack(path: Path, read_at: Optional[datetime] = None) -> Dict[str, object]:
    """讀取並驗證一個 DataPack 檔；回傳內容、原始位元組雜湊與讀取時間。"""
    read_at = read_at or datetime.now(timezone.utc)
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise DataPackError("找不到 DataPack 檔案：%s" % path)
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise DataPackError("DataPack 檔案超過大小限制")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DataPackError("DataPack 不是有效的 UTF-8 JSON：%s" % path) from error
    return {"pack": parse_datapack(payload, read_at), "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "path": str(path.resolve()), "read_at": read_at.isoformat()}


def find_datapack(directory: Path, team_id: str, trade_date: str, read_at: Optional[datetime] = None) -> Dict[str, object]:
    """在資料夾內找出本隊、本交易日的 DataPack；同日多份時取 generated_at 最新者。"""
    directory = Path(directory)
    candidates, problems = [], []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            loaded = load_datapack(path, read_at)
        except DataPackError as error:
            problems.append("%s：%s" % (path.name, error))
            continue
        pack = loaded["pack"]
        if pack["team_id"] == team_id and pack["trade_date"] == trade_date:
            candidates.append((parse_aware_time(pack["generated_at"], "generated_at", error=DataPackError), loaded))
    if not candidates:
        hint = "；無法讀取的檔案：%s" % "、".join(problems) if problems else ""
        raise DataPackError("%s 找不到 %s、交易日 %s 的 DataPack。請到參賽者後台「文件繳交」頁下載資料包，放進此資料夾%s"
                            % (directory, team_id, trade_date, hint))
    return max(candidates, key=lambda item: item[0])[1]


def load_universe(path: Path) -> Dict[str, str]:
    """官方交易池代碼 → 本專案代號（上市 .TW、上櫃 .TWO）。"""
    with Path(path).open(encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    suffix = {"TWSE": ".TW", "TPEX": ".TWO"}
    mapping = {}
    for row in rows:
        if row.get("market") not in suffix:
            raise DataPackError("交易池市場別無法對應：%s" % row.get("market"))
        mapping[row["symbol"]] = row["symbol"] + suffix[row["market"]]
    return mapping


def sellable_shares(pack: Mapping[str, object]) -> Dict[str, int]:
    """可賣股數：只看 shares；應收配股（receivable_shares）賽期內不可賣。"""
    if pack["holdings"] is None:
        raise DataPackError("DataPack 尚無結算持股，不能決定可賣股數")
    return {row["code"]: int(row["shares"]) for row in pack["holdings"] if int(row["shares"]) > 0}


def reconcile(loaded: Mapping[str, object], account_snapshot: Mapping[str, object], *, team_id: str, trade_date: str,
              universe: Mapping[str, str], cash_tolerance_rate: Decimal = DEFAULT_CASH_TOLERANCE_RATE) -> Dict[str, object]:
    """比對帳本快照與 DataPack；status 為 matched、mismatch 或 not_ready，呼叫端只在 matched 時續行。"""
    pack = loaded["pack"]
    result: Dict[str, object] = {
        "status": "matched", "datapack_sha256": loaded["raw_sha256"], "datapack_path": loaded["path"],
        "read_at": loaded["read_at"], "team_id": pack["team_id"], "trade_date": pack["trade_date"],
        "as_of_date": pack["as_of_date"], "generated_at": pack["generated_at"],
        "prev_day_orders": dict(pack["prev_day_orders"]), "violations": pack["compliance"]["violations"],
        "problems": [], "warnings": [],
    }
    problems, warnings = result["problems"], result["warnings"]
    if pack["team_id"] != team_id:
        problems.append("DataPack 隊伍代號 %s 與設定 %s 不同" % (pack["team_id"], team_id))
    if pack["trade_date"] != trade_date:
        problems.append("DataPack 交易日 %s 與本次目標交易日 %s 不同" % (pack["trade_date"], trade_date))
    if "prev_settlement" in pack["missing"]:
        problems.append("主辦方前一交易日結算尚未產生（missing 含 prev_settlement），請稍後重新下載資料包")
    if problems:
        result["status"] = "not_ready"
        return result
    if "prev_close" in pack["missing"]:
        warnings.append("主辦方收盤價尚未匯入（missing 含 prev_close），上傳後部分檢核會標為未驗")
    orders = pack["prev_day_orders"]
    if orders["outcome"] == "voided":
        warnings.append("前一交易日委託整批作廢：%s" % "、".join(VOID_LABELS[r] for r in orders["void_reasons"]))
    if orders["excluded_codes"]:
        warnings.append("前一交易日被排除、未成交的代號：%s" % "、".join(orders["excluded_codes"]))
    if orders["outcome"] == "unknown":
        warnings.append("前一交易日委託結果尚未判定")
    violations = int(pack["compliance"]["violations"])
    if violations:
        warnings.append("累計違規 %d 天（取消資格依據），請到後台「合規狀態」頁確認" % violations)
    for row in pack["holdings"]:
        if int(row["receivable_shares"]):
            warnings.append("%s 有應收配股 %d 股，賽期內不可賣、不計權重" % (row["code"], int(row["receivable_shares"])))
    official: Dict[str, int] = {}
    for code, shares in sellable_shares(pack).items():
        symbol = universe.get(code)
        if symbol is None:
            problems.append("DataPack 持股 %s 不在官方交易池，無法對應" % code)
            continue
        official[symbol] = shares
    ledger = {str(row["symbol"]): int(row["shares"]) for row in account_snapshot.get("positions", []) if int(row["shares"]) > 0}
    differences = []
    for symbol in sorted(set(official) | set(ledger)):
        if official.get(symbol, 0) != ledger.get(symbol, 0):
            differences.append({"symbol": symbol, "ledger_shares": ledger.get(symbol, 0), "datapack_shares": official.get(symbol, 0)})
    result["share_differences"] = differences
    # 主辦方以 nav_tradable 與交易所前日收盤價換算 target_weight 的股數；D-Plan 匯出時沿用同一組分母與價格。
    result["nav_tradable"] = str(pack["account"]["nav_tradable"])
    result["close_prices"] = {
        universe[row["code"]]: str(row["close_price"]) for row in pack["holdings"]
        if row["close_price"] is not None and int(row["shares"]) > 0 and row["code"] in universe
    }
    ledger_cash = Decimal(str(account_snapshot["cash"]))
    official_cash = Decimal(str(pack["account"]["cash"]))
    tolerance = Decimal(str(pack["account"]["nav_tradable"])) * cash_tolerance_rate
    result["cash"] = {"ledger": str(ledger_cash), "datapack": str(official_cash), "difference": str(ledger_cash - official_cash),
                      "tolerance": str(tolerance), "tolerance_rate": str(cash_tolerance_rate)}
    if differences:
        problems.append("持股股數與 DataPack 不一致：%s" % "；".join(
            "%s 帳本 %d／資料包 %d" % (d["symbol"], d["ledger_shares"], d["datapack_shares"]) for d in differences))
    if abs(ledger_cash - official_cash) > tolerance:
        problems.append("現金差 %s 元，超過容忍 %s 元（帳本 %s／資料包 %s）" % (ledger_cash - official_cash, tolerance, ledger_cash, official_cash))
    if problems:
        result["status"] = "mismatch"
    result["sellable_shares"] = sellable_shares(pack)
    return result


def check_dplan_against_datapack(plan: Mapping[str, object], pack: Mapping[str, object]) -> List[str]:
    """上傳前檢查 D-Plan：同隊同日、賣出不超過可賣股數、每檔持股都有交代。"""
    errors = []
    if plan.get("team_id") != pack["team_id"]:
        errors.append("D-Plan team_id %s 與 DataPack %s 不同" % (plan.get("team_id"), pack["team_id"]))
    if plan.get("trade_date") != pack["trade_date"]:
        errors.append("D-Plan trade_date %s 與 DataPack %s 不同" % (plan.get("trade_date"), pack["trade_date"]))
    if pack["holdings"] is None:
        return errors + ["DataPack 尚無結算持股（missing 含 prev_settlement），請稍後重新下載"]
    errors += check_sells(plan.get("orders", []), sellable_shares(pack))
    covered = [row.get("ticker") for row in list(plan.get("decisions", [])) + list(plan.get("no_trade_decisions", []))]
    for code in sorted(sellable_shares(pack)):
        if code not in covered:
            errors.append("DataPack 持股 %s 沒有出現在 decisions 或 no_trade_decisions" % code)
    return errors


def check_sells(orders: Sequence[Mapping[str, object]], sellable: Mapping[str, int]) -> List[str]:
    """每檔賣出合計不得超過 DataPack 的 shares。"""
    totals: Dict[str, int] = {}
    for row in orders:
        if str(row.get("side", "")).upper() == "SELL":
            ticker = str(row.get("ticker", ""))
            totals[ticker] = totals.get(ticker, 0) + int(row.get("shares", 0))
    return ["%s 賣出 %d 股超過 DataPack 可賣 %d 股" % (ticker, shares, sellable.get(ticker, 0))
            for ticker, shares in sorted(totals.items()) if shares > sellable.get(ticker, 0)]


def reconcile_account(root: Path, account_id: str, datapack_path: Path,
                      cash_tolerance_rate: Decimal = DEFAULT_CASH_TOLERANCE_RATE) -> Dict[str, object]:
    """不跑 Agent，直接用帳戶最新快照與指定 DataPack 對帳；交易日取 DataPack 自己的 trade_date。"""
    root = Path(root)
    loaded = load_datapack(datapack_path)
    try:
        profile = json.loads((root / "config" / "competition_profile.json").read_text(encoding="utf-8"))
        account = json.loads((root / "artifacts" / "ledger" / account_id / "account_snapshot_latest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DataPackError("無法讀取隊伍設定或帳戶 %s 的最新快照：%s" % (account_id, error)) from error
    result = reconcile(loaded, account, team_id=profile.get("team_id"), trade_date=loaded["pack"]["trade_date"],
                       universe=load_universe(root / "data" / "official_universe.csv"), cash_tolerance_rate=cash_tolerance_rate)
    result["account_id"] = account_id
    return result
