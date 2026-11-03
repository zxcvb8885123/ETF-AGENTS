"""備援 D-Plan：決策鏈沒有可交付的決策時，由程式確定性產生「持股全部續抱、無委託」的候選。

比賽辦法六(十四)(1)：當日未繳交 Agent 決策報告書記警告一次，累計三次取消資格。決策被拒、D-Plan 匯出器
fail-closed 或子 Agent 逾時都會讓當天沒有 D-Plan，所以在這些情況下另外產生一份只有 ``no_trade_decisions``
的計畫當本機候選檔。這份計畫：

- 由程式依既定規則產生，不含任何 Agent 文字，``agent_metadata`` 明寫非 LLM；
- 走 ``ImmutableRunStore`` 封存（``artifacts/fallback_decisions/<交易日>/<run_id>/``），不放進 ``artifacts/decisions``，
  帳本與前次決策背景都不會把它當成 Agent 決策；
- 必須通過同一個 ``DPlanValidator`` 與比賽硬性前提（持股 20–30 檔、現金 <25%、單股上限、全在 150 檔內、
  DataPack 對帳 matched），任一不滿足就不產生、只告警，不硬產；
- 絕不自動上傳或送件；是否繳交由人決定。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from etf_agent.core import canonical_sha256, parse_decimal
from etf_agent.core.artifact_store import ImmutableRunStore
from etf_agent.core.run_layout import list_run_dirs
from etf_agent.data.trading_calendar import DECISION_DEADLINE
from etf_agent.decision.contracts import snapshot_execution_price

from .context import DPlanContextBuilder, _as_taipei, _number
from .dplan import DPlanError, DPlanValidator, check_cutoff_before_deadline
from .service import DEFAULT_OUTPUT_DIRECTORY, DPlanExportService, read_object, replace_json, write_new_json


TAIPEI = timezone(timedelta(hours=8))
FALLBACK_SCHEMA_VERSION = "fallback-dplan-1.0"
DEFAULT_FALLBACK_DIRECTORY = Path("artifacts") / "fallback_decisions"
# 備援計畫沒有呼叫任何模型：model_provider 填 other，model_version 寫明是規則程式。
FALLBACK_MODEL_PROVIDER = "other"
FALLBACK_MODEL_VERSION = "etf-agents-rule-fallback-v1-no-llm"
FALLBACK_REASONS = {
    "decision_rejected": "決策被風控拒絕",
    "dplan_blocked": "D-Plan 匯出器拒絕輸出",
    "pipeline_failed": "每日流程失敗（含子 Agent 逾時或錯誤）",
}
MIN_POSITIONS, MAX_POSITIONS = 20, 30
CASH_RATIO_LIMIT = Decimal("0.25")
STOCK_WEIGHT_LIMIT = {"2330": Decimal("0.25")}
DEFAULT_STOCK_WEIGHT_LIMIT = Decimal("0.10")
CASH_RANGE_MARGIN = Decimal("0.01")
DETAIL_MAX_CHARS = 200


class FallbackDPlanRepository(ImmutableRunStore):
    """備援決策的不可變封存；與 ``DecisionRepository`` 分開，schema_version 不同，不會被當成 Agent 決策。"""

    def __init__(self, root: Path):
        super().__init__(Path(root), schema_version=FALLBACK_SCHEMA_VERSION, error=DPlanError, dated=True)


def _clean_text(value: object, limit: int) -> str:
    """錯誤訊息進 D-Plan 文字欄位前去掉換行與多餘空白，並截斷到 limit 字。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _positions(account: Mapping[str, object]) -> Dict[str, int]:
    held: Dict[str, int] = {}
    for row in account.get("positions", []) if isinstance(account.get("positions"), list) else []:
        if not isinstance(row, Mapping):
            raise DPlanError("account_snapshot.positions 每筆必須是物件")
        symbol, shares = str(row.get("symbol", "")), int(row.get("shares", 0))
        if not re.fullmatch(r"\d{4}\.(?:TW|TWO)", symbol):
            raise DPlanError("持股代號不是 ####.TW／####.TWO：%s" % symbol)
        if shares > 0:
            held[symbol] = shares
    return held


def fallback_preconditions(
    account: Mapping[str, object], snapshot: Mapping[str, object], datapack: Mapping[str, object], *,
    team_id: str, trade_date: str, now: datetime,
) -> Dict[str, object]:
    """檢查備援計畫的比賽硬性前提；``ok`` 為 False 時列出全部不滿足的項目，呼叫端不得產生備援。

    價格與權重以 DataPack 的 ``close_prices``／``nav_tradable`` 計算（主辦方算 ``tradable_weight`` 用的分母與價格）；
    Snapshot 官方價只用來比對並記警告。
    """
    failures: List[str] = []
    warnings: List[str] = []
    metrics: Dict[str, object] = {}
    if now.tzinfo is None:
        raise DPlanError("now 必須含時區")
    deadline = datetime.combine(datetime.fromisoformat(trade_date).date(), DECISION_DEADLINE, TAIPEI)
    if now >= deadline:
        failures.append("已過交易日 %s 的交件期限 %s，備援 D-Plan 無法繳交" % (trade_date, DECISION_DEADLINE.strftime("%H:%M")))
    try:
        check_cutoff_before_deadline(snapshot.get("decision_cutoff"), trade_date)
    except DPlanError as error:
        failures.append(str(error))

    if not isinstance(datapack, Mapping) or datapack.get("status") != "matched":
        failures.append("DataPack 對帳不是 matched（%s），不能確認持股與主辦方結算一致" % (
            datapack.get("status") if isinstance(datapack, Mapping) else "缺少"))
        return {"ok": False, "failures": failures, "warnings": warnings, "metrics": metrics}
    if datapack.get("team_id") != team_id or datapack.get("trade_date") != trade_date:
        failures.append("DataPack 對帳結果的隊伍（%s）或交易日（%s）與本次不一致" % (datapack.get("team_id"), datapack.get("trade_date")))
    try:
        nav = parse_decimal(datapack.get("nav_tradable"), "DataPack nav_tradable", error=DPlanError, reject_bool=True)
        cash = parse_decimal((datapack.get("cash") or {}).get("datapack"), "DataPack cash", error=DPlanError, reject_bool=True)
    except (DPlanError, AttributeError) as error:
        failures.append("DataPack 對帳結果缺少可用的 nav_tradable／cash：%s" % error)
        return {"ok": False, "failures": failures, "warnings": warnings, "metrics": metrics}
    if nav <= 0:
        failures.append("DataPack nav_tradable 必須大於 0")
        return {"ok": False, "failures": failures, "warnings": warnings, "metrics": metrics}

    held = _positions(account)
    metrics["position_count"] = len(held)
    if not MIN_POSITIONS <= len(held) <= MAX_POSITIONS:
        failures.append("持股 %d 檔，不在 %d–%d 檔內" % (len(held), MIN_POSITIONS, MAX_POSITIONS))
    sellable = datapack.get("sellable_shares")
    if not isinstance(sellable, Mapping):
        failures.append("DataPack 對帳結果缺少 sellable_shares")
    else:
        ledger_by_ticker = {symbol[:4]: shares for symbol, shares in held.items()}
        official_by_ticker = {str(code): int(shares) for code, shares in sellable.items() if int(shares) > 0}
        if ledger_by_ticker != official_by_ticker:
            failures.append("帳本持股與 DataPack 結算持股不同，備援不得假設持股")
    try:
        eligible = set(DPlanContextBuilder._eligible_tickers(snapshot))
    except DPlanError as error:
        failures.append(str(error))
        eligible = set()
    outside = sorted(symbol[:4] for symbol in held if symbol[:4] not in eligible)
    if eligible and outside:
        failures.append("持股不在官方 150 檔交易池：%s" % "、".join(outside))

    cash_ratio = cash / nav
    metrics["cash_ratio"] = str(cash_ratio)
    metrics["nav_tradable"] = str(nav)
    if cash_ratio < 0 or cash_ratio >= CASH_RATIO_LIMIT:
        failures.append("DataPack 現金比 %.4f 不在 0–25%% 內" % cash_ratio)

    close_prices = datapack.get("close_prices") if isinstance(datapack.get("close_prices"), Mapping) else {}
    snapshot_rows = {str(row.get("symbol")): row for row in snapshot.get("latest_prices", []) if isinstance(row, Mapping)}
    weights: Dict[str, str] = {}
    for symbol, shares in sorted(held.items()):
        ticker = symbol[:4]
        raw = close_prices.get(symbol)
        if raw is None:
            failures.append("%s 缺 DataPack 官方收盤價，無法確認單一持股權重" % ticker)
            continue
        price = parse_decimal(raw, "DataPack close_price", error=DPlanError, reject_bool=True)
        if price <= 0:
            failures.append("%s DataPack 收盤價必須大於 0" % ticker)
            continue
        weight = Decimal(shares) * price / nav
        weights[ticker] = str(weight)
        limit = STOCK_WEIGHT_LIMIT.get(ticker, DEFAULT_STOCK_WEIGHT_LIMIT)
        if weight > limit:
            failures.append("%s 權重 %.4f 超過單一持股上限 %.0f%%" % (ticker, weight, limit * 100))
        row = snapshot_rows.get(symbol)
        if row is None:
            failures.append("%s 不在 Snapshot latest_prices，缺可引用的封存行情" % ticker)
        elif row.get("execution_close_price") is None:
            warnings.append("%s Snapshot 缺同日官方收盤價，觀察值引用調整價" % ticker)
        else:
            snapshot_price = snapshot_execution_price(row, "latest_prices")
            if snapshot_price != price:
                warnings.append("%s Snapshot 官方收盤價 %s 與 DataPack %s 不同；權重以 DataPack 價計算" % (ticker, snapshot_price, price))
    metrics["weights"] = weights
    return {"ok": not failures, "failures": failures, "warnings": warnings, "metrics": metrics}


def _price_evidence(snapshot: Mapping[str, object], symbols: Sequence[str]) -> Dict[str, Mapping[str, object]]:
    """每檔持股的封存價格來源：優先同日官方價的來源（TWSE／TPEx），沒有才用日線來源。"""
    rows = {str(row.get("symbol")): row for row in snapshot.get("latest_prices", []) if isinstance(row, Mapping)}
    by_id = {str(row.get("evidence_id")): row for row in snapshot.get("source_evidence", []) if isinstance(row, Mapping)}
    selected: Dict[str, Mapping[str, object]] = {}
    for symbol in symbols:
        row = rows.get(symbol)
        if row is None:
            raise DPlanError("%s 不在 Snapshot latest_prices" % symbol)
        evidence_id = row.get("execution_source_evidence_id") if row.get("execution_close_price") is not None else None
        item = by_id.get(str(evidence_id)) if evidence_id else None
        if item is None:
            item = by_id.get(str(row.get("source_evidence_id")))
        if item is None:
            raise DPlanError("%s 缺少可引用的封存價格來源" % symbol)
        selected[symbol] = item
    return selected


def build_fallback_plan(
    *, team_id: str, trade_date: str, account: Mapping[str, object], snapshot: Mapping[str, object],
    datapack: Mapping[str, object], reason: str, detail: str, started_at: object, completed_at: object,
    code_version: str,
) -> Dict[str, object]:
    """由帳戶快照、Snapshot 封存行情與已對帳 DataPack 確定性組出全部續抱的 D-Plan；呼叫前須先通過前提檢查。"""
    if reason not in FALLBACK_REASONS:
        raise DPlanError("備援原因代碼不合法：%s" % reason)
    held = _positions(account)
    symbols = sorted(held)
    nav = parse_decimal(datapack.get("nav_tradable"), "DataPack nav_tradable", error=DPlanError, reject_bool=True)
    cash_ratio = parse_decimal((datapack.get("cash") or {}).get("datapack"), "DataPack cash", error=DPlanError, reject_bool=True) / nav
    close_prices = datapack.get("close_prices") if isinstance(datapack.get("close_prices"), Mapping) else {}
    evidence = _price_evidence(snapshot, symbols)
    sources, source_by_evidence = DPlanContextBuilder._sources(evidence)
    rows = {str(row.get("symbol")): row for row in snapshot.get("latest_prices", []) if isinstance(row, Mapping)}
    reason_label = FALLBACK_REASONS[reason]
    detail_text = _clean_text(detail, DETAIL_MAX_CHARS) or "未提供細節"

    observations: List[Dict[str, object]] = [{
        "obs_id": "O1",
        "source_ref": sorted(set(source_by_evidence.values())),
        "topic": "portfolio_state",
        "statement": "本日決策鏈未產生通過驗證的交易決策（%s）；主辦方資料包結算持股 %d 檔、現金比 %.4f，"
                     "每檔持股在交易所封存行情中都有前日收盤價。此計畫由程式依既定規則產生，未呼叫任何模型。" % (
                         reason_label, len(symbols), cash_ratio),
        "values": {
            "held_positions": len(symbols),
            "cash_ratio": _number(cash_ratio, "cash_ratio"),
            "nav_tradable": _number(nav, "nav_tradable"),
        },
    }]
    inferences: List[Dict[str, object]] = [{
        "inf_id": "I1",
        "premise_refs": ["O1"],
        "logic": "本日決策鏈未產生通過驗證的交易決策（%s：%s）。依本策略既定規則「資料不足、來源不可驗證或風控條件未通過時，"
                 "維持現金或不交易，不以推測補足缺漏」，本日不新增、不加碼、不減碼、不出清任何持股，全部續抱、不送出委託。"
                 "此推論由程式依規則產生，不含模型對市場或個股的判斷。" % (reason_label, detail_text),
        "conclusion": "全部持股續抱，無委託。",
        "counter_evidence": "本日未完成多空研究與風險審查，個股層級的風險訊號未被評估；若持股中有應立即處置的事件，本計畫不會反映。",
    }]
    no_trade: List[Dict[str, object]] = []
    for symbol in symbols:
        ticker, shares = symbol[:4], held[symbol]
        row = rows[symbol]
        cited_price = snapshot_execution_price(row, "latest_prices")
        official = parse_decimal(close_prices.get(symbol), "DataPack close_price", error=DPlanError, reject_bool=True)
        weight = Decimal(shares) * official / nav
        obs_id = "O%d" % (len(observations) + 1)
        observations.append({
            "obs_id": obs_id,
            "source_ref": [source_by_evidence[str(evidence[symbol]["evidence_id"])]],
            "topic": "held_position",
            "statement": "%s 封存前日收盤價 %s 元，資料包結算持股 %d 股。" % (ticker, cited_price, shares),
            "values": {"close": _number(cited_price, "%s.close" % symbol), "held_shares": shares},
        })
        inf_id = "I%d" % (len(inferences) + 1)
        inferences.append({
            "inf_id": inf_id,
            "premise_refs": ["O1", obs_id],
            "logic": "%s 續抱：本日沒有通過驗證的交易決策，依既定規則不以推測調整部位，維持結算持股 %d 股不動"
                     "（以資料包收盤價計權重約 %.2f%%，未超過單一持股上限）。" % (ticker, shares, weight * 100),
            "conclusion": "%s 續抱，無委託。" % ticker,
            "counter_evidence": None,
        })
        no_trade.append({"ticker": ticker, "reason_refs": ["I1", inf_id], "reason": "備援續抱：程式依規則產生，未經本日模型判斷。"})

    low = max(Decimal("0"), cash_ratio - CASH_RANGE_MARGIN)
    high = min(CASH_RATIO_LIMIT, cash_ratio + CASH_RANGE_MARGIN)
    market_view = {
        "basis_refs": ["O1"],
        "logic": "備援計畫：本日決策鏈未產生通過驗證的決策（%s），程式未研判市場走向（regime 填 neutral 不代表研判結果），"
                 "操作姿態為維持既有配置、不做任何調整；委託為空，淨曝險持平，現金比維持資料包結算的 %.4f。" % (reason_label, cash_ratio),
        "regime": "neutral",
        "stance": "neutral",
        "posture": {"net_exposure_intent": "hold", "target_cash_pct_range": [_number(low, "cash_low"), _number(high, "cash_high")]},
        "counter_evidence": "未評估本日市場訊號；若隔夜市場出現重大風險事件，本計畫不會反映。",
    }
    started = _as_taipei(started_at, "run_started_at")
    completed = _as_taipei(completed_at, "run_completed_at")
    if completed < started:
        raise DPlanError("run_completed_at 不得早於 run_started_at")
    if not isinstance(code_version, str) or not re.fullmatch(r"[A-Za-z0-9_.:/\-]{1,64}", code_version):
        raise DPlanError("code_version 格式不合法")
    plan = {
        "schema_version": "4.3", "doc_type": "D-Plan", "team_id": team_id, "trade_date": trade_date,
        "sources": sources, "observations": observations, "market_view": market_view, "inferences": inferences,
        "decisions": [], "no_trade_decisions": no_trade, "orders": [],
        "agent_metadata": {
            "model_provider": FALLBACK_MODEL_PROVIDER, "model_version": FALLBACK_MODEL_VERSION,
            "run_started_at": started, "run_completed_at": completed, "code_version": code_version,
        },
    }
    errors = DPlanValidator().validate(plan)
    if errors:
        raise DPlanError("備援 D-Plan 本地驗證失敗：" + "; ".join(errors))
    return plan


class FallbackDPlanService:
    """產生、封存並落地備援 D-Plan 候選；任何前提不滿足都回傳 ``not_generated``，不硬產。"""

    def __init__(self, root: Path):
        self.root = Path(root)

    def fallback_plan_sha256s(self, trade_date: str) -> set:
        """該交易日所有已封存備援計畫的 canonical sha256；用來判斷主檔是不是備援。"""
        found = set()
        day_dir = self.root / DEFAULT_FALLBACK_DIRECTORY / trade_date
        if not day_dir.is_dir():
            return found
        for run_dir in list_run_dirs(self.root / DEFAULT_FALLBACK_DIRECTORY):
            if run_dir.parent.name != trade_date:
                continue
            try:
                manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
                found.add(str(manifest["artifacts"]["dplan"]["sha256"]))
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                continue
        return found

    def main_plan_is_fallback(self, path: Path, trade_date: str) -> Optional[bool]:
        """主檔是備援回 True、是正式決策回 False、不存在回 None；讀不懂的檔視為正式決策（不覆蓋）。"""
        if not path.exists():
            return None
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return canonical_sha256(current) in self.fallback_plan_sha256s(trade_date)

    def generate(
        self, *, daily_run: Mapping[str, object], account_path: Path, snapshot_path: Path, profile_path: Path,
        account_id: str, reason: str, detail: str, now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        """回傳 ``status`` 為 ``fallback``（已封存並落地）或 ``not_generated``（附 ``error`` 與前提失敗清單）。"""
        now = now or datetime.now(TAIPEI)
        try:
            return self._generate(daily_run=daily_run, account_path=account_path, snapshot_path=snapshot_path,
                                  profile_path=profile_path, account_id=account_id, reason=reason, detail=detail, now=now)
        except (DPlanError, OSError, ValueError, TypeError, KeyError) as error:
            return {"status": "not_generated", "reason": reason, "error": str(error), "failures": [str(error)]}

    def _generate(self, *, daily_run, account_path, snapshot_path, profile_path, account_id, reason, detail, now) -> Dict[str, object]:
        if reason not in FALLBACK_REASONS:
            raise DPlanError("備援原因代碼不合法：%s" % reason)
        if daily_run.get("rolled_back"):
            raise DPlanError("本次已還原重跑前的備案決策，原 D-Plan 主檔仍有效，不產生備援")
        profile = read_object(Path(profile_path), "competition_profile")
        team_id = profile.get("team_id")
        if not isinstance(team_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", team_id):
            raise DPlanError("competition_profile.team_id 不合法")
        if profile.get("strategy_status") != "approved":
            raise DPlanError("competition_profile 策略尚未確認，備援 D-Plan 一樣不得產生")
        trade_date = DPlanExportService.trade_date(daily_run)
        account = read_object(Path(account_path), "帳戶快照")
        snapshot = read_object(Path(snapshot_path), "ResearchSnapshot")
        if account.get("account_id") not in (None, account_id):
            raise DPlanError("帳戶快照的 account_id（%s）與本次帳戶（%s）不同" % (account.get("account_id"), account_id))
        datapack = daily_run.get("datapack")
        checks = fallback_preconditions(account, snapshot, datapack if isinstance(datapack, Mapping) else {},
                                        team_id=team_id, trade_date=trade_date, now=now)
        if not checks["ok"]:
            return {"status": "not_generated", "reason": reason, "trade_date": trade_date,
                    "error": "備援前提不滿足：" + "；".join(checks["failures"]),
                    "failures": checks["failures"], "warnings": checks["warnings"], "metrics": checks["metrics"]}
        code_version = DPlanExportService(self.root).code_version()
        plan = build_fallback_plan(
            team_id=team_id, trade_date=trade_date, account=account, snapshot=snapshot, datapack=datapack,
            reason=reason, detail=detail, started_at=daily_run.get("started_at") or now.isoformat(),
            completed_at=now.isoformat(), code_version=code_version,
        )
        run_id = "fallback-%s" % now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        record = {
            "schema_version": FALLBACK_SCHEMA_VERSION,
            "run_id": run_id,
            "fallback": True,
            "status": "no_trade",
            "producer": "deterministic_program",
            "agent_generated": False,
            "reason": reason,
            "reason_label": FALLBACK_REASONS[reason],
            "detail": _clean_text(detail, 1000),
            "team_id": team_id,
            "trade_date": trade_date,
            "account_id": account_id,
            "account_snapshot_sha256": canonical_sha256(account),
            "snapshot_id": snapshot.get("snapshot_id"),
            "decision_cutoff": snapshot.get("decision_cutoff"),
            "daily_run_id": daily_run.get("run_id"),
            "planned_decision_run_id": daily_run.get("planned_decision_run_id"),
            "datapack": {key: datapack.get(key) for key in ("status", "datapack_sha256", "as_of_date", "generated_at", "nav_tradable")},
            "positions": [{"symbol": symbol, "shares": shares} for symbol, shares in sorted(_positions(account).items())],
            "metrics": checks["metrics"],
            "warnings": checks["warnings"],
            "code_version": code_version,
            "created_at": now.isoformat(),
            "dplan_sha256": canonical_sha256(plan),
        }
        run_dir = FallbackDPlanRepository(self.root / DEFAULT_FALLBACK_DIRECTORY).save(
            run_id, {"decision": record, "dplan": plan, "account_snapshot": account, "preconditions": checks}, day=trade_date,
        )
        main_output = self.root / DEFAULT_OUTPUT_DIRECTORY / ("D-Plan_%s_%s.json" % (team_id, trade_date))
        version_output = main_output.parent / "versions" / trade_date / (
            "%s_%s_fallback.json" % (main_output.stem, now.astimezone(TAIPEI).strftime("%Y%m%d-%H%M%S")))
        if version_output.exists():
            raise DPlanError("輸出檔已存在，拒絕覆寫：%s" % version_output)
        write_new_json(version_output, plan)
        existing_is_fallback = self.main_plan_is_fallback(main_output, trade_date)
        # 主檔只在「沒有主檔」或「主檔本身就是備援」時換成備援；已有正式決策的 D-Plan 一律保留，備援只留在 versions/。
        replaced_main = existing_is_fallback is not False
        if replaced_main:
            replace_json(main_output, plan)
        return {
            "status": "fallback",
            "fallback": True,
            "reason": reason,
            "reason_label": FALLBACK_REASONS[reason],
            "detail": _clean_text(detail, 1000),
            "run_id": run_id,
            "run_dir": str(run_dir),
            "output": str(main_output if replaced_main else version_output),
            "main_output": str(main_output),
            "main_replaced": replaced_main,
            "main_kept_reason": None if replaced_main else "主檔是正式決策的 D-Plan，備援只存為 versions 候選",
            "version_output": str(version_output),
            "trade_date": trade_date,
            "position_count": checks["metrics"].get("position_count"),
            "cash_ratio": checks["metrics"].get("cash_ratio"),
            "warnings": checks["warnings"],
            "local_validation": "passed",
            "organizer_server_validation": "not_run",
            "uploaded": False,
            "requires_human_review": True,
        }


def load_fallback_run(root: Path, run_id: str) -> Dict[str, Dict[str, object]]:
    """只讀通過 manifest 驗證的備援封存。"""
    run_dir = FallbackDPlanRepository(Path(root) / DEFAULT_FALLBACK_DIRECTORY).verify(run_id)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    return {name: json.loads((run_dir / (name + ".json")).read_text(encoding="utf-8")) for name in manifest["artifacts"]}
