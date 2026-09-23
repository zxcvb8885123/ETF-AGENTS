"""本地帳戶匯入、時間點估值、對帳及不可變封存。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set

ACCOUNT_SCHEMA_VERSION = "1.0"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class AccountDataError(ValueError):
    """帳戶輸入、對帳或封存不符合契約。"""


def canonical_sha256(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _time(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise AccountDataError("%s 必須是含時區的時間" % field)
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise AccountDataError("%s 格式錯誤" % field) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AccountDataError("%s 必須包含時區" % field)
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise AccountDataError("%s 必須是有限數值" % field)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise AccountDataError("%s 必須是有限數值" % field) from error
    if not result.is_finite():
        raise AccountDataError("%s 必須是有限數值" % field)
    return result


def _json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AccountDataError("無法讀取 JSON：%s" % path) from error


class AccountImporter:
    """驗證版本化標準匯入格式並保留原始檔案識別。"""

    REQUIRED = {
        "schema_version", "provider", "account_id", "currency", "ledger_at",
        "valuation_at", "available_at", "reported_cash", "settled_cash",
        "unsettled_receivable", "unsettled_payable", "restricted_cash",
        "available_cash", "reported_nav", "positions", "settlements",
    }

    def import_file(self, path: Path, cutoff: str, run_id: str, execution_mode: str, approved_sources: Optional[Mapping[str, object]] = None) -> Dict[str, object]:
        if not RUN_ID_RE.fullmatch(run_id):
            raise AccountDataError("run_id 格式不合法")
        if execution_mode not in {"fixture", "official"}:
            raise AccountDataError("execution_mode 僅接受 fixture 或 official")
        payload = _json(path)
        if not isinstance(payload, Mapping):
            raise AccountDataError("帳戶檔頂層必須是 JSON 物件")
        missing = sorted(self.REQUIRED - set(payload))
        unknown = sorted(set(payload) - self.REQUIRED - {"source_exported_at", "source_url", "source_version"})
        if missing or unknown:
            raise AccountDataError("帳戶欄位缺漏=%s；未允許=%s" % (missing, unknown))
        if payload["schema_version"] != ACCOUNT_SCHEMA_VERSION:
            raise AccountDataError("schema_version 必須為 %s" % ACCOUNT_SCHEMA_VERSION)
        source_version = str(payload.get("source_version", "unspecified"))
        approval = (approved_sources or {}).get(str(payload["provider"]))
        if execution_mode == "official" and (
            not isinstance(approval, Mapping)
            or approval.get("status") != "approved"
            or source_version not in approval.get("versions", [])
        ):
            raise AccountDataError("正式模式拒絕未核准的 provider／source_version")
        for field in ("provider", "account_id", "currency"):
            if not isinstance(payload[field], str) or not payload[field].strip():
                raise AccountDataError("%s 必須是非空字串" % field)
        point_in_time = _time(cutoff, "decision_cutoff")
        available_at = _time(payload["available_at"], "available_at")
        ledger_at = _time(payload["ledger_at"], "ledger_at")
        valuation_at = _time(payload["valuation_at"], "valuation_at")
        if available_at > point_in_time or ledger_at > point_in_time or valuation_at > point_in_time:
            raise AccountDataError("帳戶資料可得時間、帳務時間及估值時間均不得晚於 decision_cutoff")
        if payload.get("source_exported_at") is not None and _time(payload["source_exported_at"], "source_exported_at") > point_in_time:
            raise AccountDataError("source_exported_at 晚於 decision_cutoff")
        for field in ("reported_cash", "settled_cash", "unsettled_receivable", "unsettled_payable", "restricted_cash", "available_cash", "reported_nav"):
            amount = _decimal(payload[field], field)
            if field not in {"unsettled_payable"} and amount < 0:
                raise AccountDataError("%s 不得為負" % field)
        if not isinstance(payload["positions"], list) or not isinstance(payload["settlements"], list):
            raise AccountDataError("positions 與 settlements 必須是陣列")
        seen: Set[str] = set()
        positions: List[Dict[str, object]] = []
        for index, row in enumerate(payload["positions"]):
            prefix = "positions[%d]" % index
            if not isinstance(row, Mapping) or set(row) != {"symbol", "shares", "average_cost"}:
                raise AccountDataError("%s 欄位必須恰為 symbol/shares/average_cost" % prefix)
            symbol = str(row["symbol"]).upper().strip()
            if not symbol or symbol in seen:
                raise AccountDataError("%s 股票代號空白或重複" % prefix)
            shares = row["shares"]
            if not isinstance(shares, int) or isinstance(shares, bool) or shares <= 0:
                raise AccountDataError("%s.shares 必須是正整數；零股保留於原檔但阻擋決策匯出" % prefix)
            cost = _decimal(row["average_cost"], prefix + ".average_cost")
            if cost <= 0:
                raise AccountDataError("%s.average_cost 必須大於 0" % prefix)
            positions.append({"symbol": symbol, "shares": shares, "average_cost": str(cost)})
            seen.add(symbol)
        settlement_ids: Set[str] = set()
        settlements: List[Dict[str, object]] = []
        for index, row in enumerate(payload["settlements"]):
            prefix = "settlements[%d]" % index
            if not isinstance(row, Mapping) or set(row) != {"settlement_id", "direction", "amount", "settlement_at", "status"}:
                raise AccountDataError("%s 欄位必須恰為 settlement_id/direction/amount/settlement_at/status" % prefix)
            sid = str(row["settlement_id"]).strip()
            if not sid or sid in settlement_ids:
                raise AccountDataError("交割識別空白或重複：%s" % prefix)
            if row["direction"] not in {"receivable", "payable"} or row["status"] not in {"pending", "settled", "cancelled"}:
                raise AccountDataError("%s direction/status 不合法" % prefix)
            amount = _decimal(row["amount"], prefix + ".amount")
            if amount <= 0:
                raise AccountDataError("%s.amount 必須大於 0" % prefix)
            _time(row["settlement_at"], prefix + ".settlement_at")
            settlements.append({"settlement_id": sid, "direction": row["direction"], "amount": str(amount), "settlement_at": row["settlement_at"], "status": row["status"]})
            settlement_ids.add(sid)
        pending_receivable = sum((_decimal(row["amount"], "settlement.amount") for row in settlements if row["direction"] == "receivable" and row["status"] == "pending"), Decimal(0))
        pending_payable = sum((_decimal(row["amount"], "settlement.amount") for row in settlements if row["direction"] == "payable" and row["status"] == "pending"), Decimal(0))
        if pending_receivable != _decimal(payload["unsettled_receivable"], "unsettled_receivable"):
            raise AccountDataError("pending 應收交割合計與 unsettled_receivable 不符")
        if pending_payable != _decimal(payload["unsettled_payable"], "unsettled_payable"):
            raise AccountDataError("pending 應付交割合計與 unsettled_payable 不符")
        raw_bytes = path.read_bytes()
        source = {
            "provider": payload["provider"], "source_version": payload.get("source_version", "unspecified"),
            "source_url": payload.get("source_url"), "source_exported_at": payload.get("source_exported_at"),
            "raw_filename": path.name, "raw_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "parser_version": "1.0", "execution_mode": execution_mode,
        }
        body = {
            "schema_version": ACCOUNT_SCHEMA_VERSION, "run_id": run_id,
            "account_id": payload["account_id"], "currency": payload["currency"],
            "ledger_at": payload["ledger_at"], "valuation_at": payload["valuation_at"],
            "available_at": payload["available_at"], "decision_cutoff": cutoff,
            "reported_cash": str(_decimal(payload["reported_cash"], "reported_cash")),
            "settled_cash": str(_decimal(payload["settled_cash"], "settled_cash")),
            "unsettled_receivable": str(_decimal(payload["unsettled_receivable"], "unsettled_receivable")),
            "unsettled_payable": str(_decimal(payload["unsettled_payable"], "unsettled_payable")),
            "restricted_cash": str(_decimal(payload["restricted_cash"], "restricted_cash")),
            "available_cash": str(_decimal(payload["available_cash"], "available_cash")),
            "reported_nav": str(_decimal(payload["reported_nav"], "reported_nav")),
            "positions": positions, "settlements": settlements, "source": source,
        }
        body["content_sha256"] = canonical_sha256(body)
        return body


class AccountValidator:
    def validate(self, bundle: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        body = dict(bundle)
        recorded = body.pop("content_sha256", None)
        if recorded != canonical_sha256(body):
            errors.append("content_sha256 與 AccountDataBundle 不一致")
        try:
            cutoff = _time(bundle.get("decision_cutoff"), "decision_cutoff")
            if _time(bundle.get("available_at"), "available_at") > cutoff:
                errors.append("帳戶資料晚於 decision_cutoff")
            if _time(bundle.get("ledger_at"), "ledger_at") > cutoff or _time(bundle.get("valuation_at"), "valuation_at") > cutoff:
                errors.append("帳務／估值時間晚於 decision_cutoff")
            if not isinstance(bundle.get("positions"), list) or not isinstance(bundle.get("settlements"), list):
                errors.append("positions/settlements 格式錯誤")
        except AccountDataError as error:
            errors.append(str(error))
        return errors


class AccountRunRepository:
    """原子封存並驗證帳戶 run 的全部檔案雜湊。"""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def save(self, run_id: str, artifacts: Mapping[str, object], raw_path: Path) -> Path:
        if not RUN_ID_RE.fullmatch(run_id):
            raise AccountDataError("run_id 格式不合法")
        self.root.mkdir(parents=True, exist_ok=True)
        final = self.root / run_id
        file_map: Dict[str, str] = {}
        for name, value in artifacts.items():
            file_map[name] = hashlib.sha256((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()).hexdigest()
        file_map["raw/" + raw_path.name] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        manifest = {"schema_version": "1.0", "run_id": run_id, "artifacts": file_map}
        manifest["manifest_sha256"] = canonical_sha256(manifest)
        if final.exists():
            self.verify(run_id)
            current = json.loads((final / "manifest.json").read_text(encoding="utf-8"))
            if current != manifest:
                raise AccountDataError("同 run_id 已封存不同內容，不可覆寫")
            return final
        temp = Path(tempfile.mkdtemp(prefix="." + run_id + ".", dir=str(self.root)))
        try:
            for name, value in artifacts.items():
                target = temp / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            raw_target = temp / "raw" / raw_path.name
            raw_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(raw_path, raw_target)
            (temp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp, final)
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            raise
        self.verify(run_id)
        return final

    def verify(self, run_id: str) -> Path:
        if not RUN_ID_RE.fullmatch(run_id):
            raise AccountDataError("run_id 格式不合法")
        run_dir = self.root / run_id
        try:
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AccountDataError("run manifest 無法讀取") from error
        body = dict(manifest)
        recorded = body.pop("manifest_sha256", None)
        if recorded != canonical_sha256(body) or manifest.get("run_id") != run_id:
            raise AccountDataError("manifest 身分或雜湊錯誤")
        expected = set(manifest.get("artifacts", {}))
        actual = {p.relative_to(run_dir).as_posix() for p in run_dir.rglob("*") if p.is_file() and p.name != "manifest.json"}
        if expected != actual:
            raise AccountDataError("run 檔案集合與 manifest 不一致")
        for name, digest in manifest["artifacts"].items():
            if hashlib.sha256((run_dir / name).read_bytes()).hexdigest() != digest:
                raise AccountDataError("artifact 雜湊錯誤：%s" % name)
        return run_dir


def reconcile(bundle: Mapping[str, object], snapshot: Mapping[str, object], max_nav_drift_rate: object) -> Dict[str, object]:
    errors = AccountValidator().validate(bundle)
    if errors:
        raise AccountDataError("帳戶資料驗證失敗：" + "；".join(errors))
    if snapshot.get("snapshot_id") is None or snapshot.get("decision_cutoff") != bundle.get("decision_cutoff"):
        raise AccountDataError("Snapshot cutoff 必須與帳戶 decision_cutoff 完全一致")
    prices: Dict[str, Decimal] = {}
    for row in snapshot.get("latest_prices", []):
        if isinstance(row, Mapping):
            symbol = str(row.get("symbol", "")).upper()
            price = row.get("analysis_close_price")
            if symbol and price is not None:
                prices[symbol] = _decimal(price, "analysis_close_price")
    positions = bundle["positions"]
    missing_prices = sorted({row["symbol"] for row in positions if row["symbol"] not in prices})
    market_value = sum((_decimal(row["shares"], "shares") * prices[row["symbol"]] for row in positions if row["symbol"] in prices), Decimal(0))
    unsettled_receivable = _decimal(bundle["unsettled_receivable"], "unsettled_receivable")
    unsettled_payable = _decimal(bundle["unsettled_payable"], "unsettled_payable")
    settled = _decimal(bundle["settled_cash"], "settled_cash")
    restricted = _decimal(bundle["restricted_cash"], "restricted_cash")
    reported_cash = _decimal(bundle["reported_cash"], "reported_cash")
    available_cash = _decimal(bundle["available_cash"], "available_cash")
    reported_nav = _decimal(bundle["reported_nav"], "reported_nav")
    calculated_cash = settled + unsettled_receivable - unsettled_payable
    calculated_nav = calculated_cash + market_value
    drift = abs(reported_nav - calculated_nav) / calculated_nav if calculated_nav > 0 else Decimal(1)
    tolerance = _decimal(max_nav_drift_rate, "max_nav_drift_rate")
    cash_difference = abs(reported_cash - calculated_cash)
    available_upper_bound = settled + unsettled_receivable - unsettled_payable - restricted
    checks = {
        "reported_cash_matches_ledger": cash_difference == 0,
        "available_cash_within_ledger": Decimal(0) <= available_cash <= max(available_upper_bound, Decimal(0)),
        "reported_nav_within_tolerance": drift <= tolerance,
        "all_positions_have_cutoff_prices": not missing_prices,
        "all_positions_in_snapshot_universe": all(row["symbol"] in prices for row in positions),
        "single_currency_twd": bundle.get("currency") == "TWD",
        "no_restricted_cash_for_current_decision_contract": restricted == 0,
        "buying_power_matches_current_decision_contract": available_cash == settled,
        "no_unsettled_funds_for_current_decision_contract": unsettled_receivable == 0 and unsettled_payable == 0,
    }
    passed = all(checks.values())
    result = {
        "schema_version": "1.0", "run_id": bundle["run_id"], "snapshot_id": snapshot["snapshot_id"],
        "decision_cutoff": bundle["decision_cutoff"], "account_content_sha256": bundle["content_sha256"],
        "snapshot_sha256": canonical_sha256(snapshot), "calculated_cash": str(calculated_cash),
        "reported_cash": str(reported_cash), "calculated_market_value": str(market_value),
        "calculated_nav": str(calculated_nav), "reported_nav": str(reported_nav),
        "nav_drift_rate": str(drift), "available_cash": str(available_cash),
        "available_cash_ledger_upper_bound": str(available_upper_bound),
        "missing_price_symbols": missing_prices, "checks": checks,
        "decision_compatible": passed and all(row["shares"] % 1000 == 0 for row in positions),
        "status": "passed" if passed else "blocked",
        "errors": [name for name, ok in checks.items() if not ok],
        "execution_mode": bundle["source"]["execution_mode"],
    }
    result["content_sha256"] = canonical_sha256(result)
    return result


def export_decision_account(bundle: Mapping[str, object], result: Mapping[str, object], snapshot: Mapping[str, object], approved_sources: Optional[Mapping[str, object]] = None) -> Dict[str, object]:
    errors = AccountValidator().validate(bundle)
    if errors or result.get("content_sha256") != canonical_sha256({k: v for k, v in result.items() if k != "content_sha256"}):
        raise AccountDataError("AccountDataBundle／ReconciliationResult 驗證失敗")
    if result.get("status") != "passed" or not result.get("decision_compatible"):
        raise AccountDataError("帳戶未通過對帳或無法無損轉入決策契約")
    if bundle.get("source", {}).get("execution_mode") != "official":
        raise AccountDataError("fixture 帳戶不得匯出為正式決策輸入")
    source = bundle.get("source", {})
    approval = (approved_sources or {}).get(source.get("provider")) if isinstance(source, Mapping) else None
    if not isinstance(approval, Mapping) or approval.get("status") != "approved" or source.get("source_version") not in approval.get("versions", []):
        raise AccountDataError("決策匯出拒絕未核准的 provider／source_version")
    if result.get("snapshot_id") != snapshot.get("snapshot_id") or result.get("decision_cutoff") != snapshot.get("decision_cutoff"):
        raise AccountDataError("帳戶、對帳結果與 Snapshot 身分不一致")
    if _decimal(bundle["reported_cash"], "reported_cash") != _decimal(bundle["settled_cash"], "settled_cash") + _decimal(bundle["unsettled_receivable"], "unsettled_receivable") - _decimal(bundle["unsettled_payable"], "unsettled_payable"):
        raise AccountDataError("現行 DecisionInputBundle 無法無損表示來源現金口徑")
    return {
        "account_id": str(bundle["account_id"]), "available_at": bundle["available_at"],
        "valuation_at": bundle["valuation_at"], "source_evidence_id": "account:" + str(bundle["content_sha256"][:24]),
        "cash": bundle["reported_cash"], "settled_cash": bundle["settled_cash"],
        "unsettled_cash": str(_decimal(bundle["unsettled_receivable"], "unsettled_receivable") - _decimal(bundle["unsettled_payable"], "unsettled_payable")),
        "nav": result["calculated_nav"],
        "positions": [{"symbol": row["symbol"], "shares": row["shares"], "average_cost": row["average_cost"]} for row in bundle["positions"]],
    }
