"""逐日重放：Snapshot（重建可得時間）→ 帳戶快照 → 決策鏈 → 成交結算 → 報告。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional

from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.data import DataAgentService, MarketDataDatabase
from etf_agent.decision import DecisionRepository
from etf_agent.virtual_account import VirtualAccountRepository, VirtualAccountService
from etf_agent.virtual_account.daily import DailyAccountRunner

from .availability import prepare_backtest_database
from .contracts import (
    INFRASTRUCTURE_MARKERS, PRICE_AVAILABLE_TIME, ReplayError, ReplayInterrupted, ReplayRequest, TAIPEI, cutoff_for, trading_days,
)
from .report import build_report, render_markdown
from .status import build_assumed_status

ACCOUNT_ID = "backtest"


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read(path: Path) -> Dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def state_metrics(state: Mapping[str, object]) -> Dict[str, object]:
    nav = Decimal(str(state["nav"]))
    cash = Decimal(str(state["settled_cash"])) + Decimal(str(state["unsettled_cash"]))
    return {
        "as_of": state["as_of"],
        "nav": str(nav),
        "cash": str(cash),
        "cash_weight": str((cash / nav).quantize(Decimal("0.000001"))),
        "position_count": len(state.get("positions", [])),
    }


@dataclass
class ReplayPaths:
    root: Path

    @property
    def database(self) -> Path:
        return self.root / "backtest.db"

    @property
    def accounts(self) -> Path:
        return self.root / "accounts"

    @property
    def decisions(self) -> Path:
        return self.root / "decisions"

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    def day(self, day: date) -> Path:
        return self.root / "days" / day.isoformat()


class ReplayRunner:
    def __init__(
        self,
        project_root: Path,
        backtest_dir: Path,
        source_database: Path,
        request: ReplayRequest,
        calendar,
        agent_runner,
        log: Callable[[str], None] = print,
        resume: bool = False,
    ):
        self.project_root = Path(project_root)
        self.paths = ReplayPaths(Path(backtest_dir))
        self.source_database = Path(source_database)
        self.request = request
        self.calendar = calendar
        self.agent_runner = agent_runner
        self.log = log
        self.resume = resume

    # ------------------------------------------------------------------ setup
    def _setup(self, days: List[date]) -> Dict[str, object]:
        manifest_path = self.paths.manifest
        if manifest_path.exists():
            manifest = _read(manifest_path)
            if not self.resume:
                raise ReplayError("回測目錄已存在；續跑請加 --resume：%s" % self.paths.root)
            if manifest.get("start") != self.request.start.isoformat() or manifest.get("end") != self.request.end.isoformat():
                raise ReplayError("回測目錄的日期區間與本次請求不一致")
            if not (self.paths.root / "decision_policy.json").exists():
                manifest["rules_assumed_in_force"] = self._write_rules_in_force(days[0])
                _write(manifest_path, manifest)
            return manifest
        self.paths.root.mkdir(parents=True, exist_ok=True)
        reconstruction = prepare_backtest_database(self.source_database, self.paths.database, self.request.end.isoformat())
        rules = self.project_root / "config" / "competition_rules.json"
        service = VirtualAccountService(VirtualAccountRepository(self.paths.accounts, ACCOUNT_ID))
        genesis_at = "%sT17:00:00+08:00" % (days[0] - timedelta(days=1)).isoformat()
        service.initialize(rules, ACCOUNT_ID, genesis_at)
        rules_copy = self._write_rules_in_force(days[0])
        manifest = {
            "rules_assumed_in_force": rules_copy,
            "start": self.request.start.isoformat(),
            "end": self.request.end.isoformat(),
            "trading_days": [item.isoformat() for item in days],
            "reconstruction": reconstruction,
            "evidence_status": "insufficient",
        }
        _write(manifest_path, manifest)
        return manifest

    def _write_rules_in_force(self, first_day: date) -> Dict[str, object]:
        """規則、策略樣板與產業分類都是 9 月才產生；回測假設它們自區間開始前就適用。

        只調整可得時間欄位，內容不變；原始版本與時間寫入 manifest 以供稽核。
        """
        in_force = "%sT00:00:00+08:00" % (first_day - timedelta(days=1)).isoformat()
        record: Dict[str, object] = {"assumed_available_at": in_force}
        rules = _read(self.project_root / "config" / "decision_rules.json")
        record["decision_rules"] = {"original_version": rules["version"], "original_available_at": rules["available_at"]}
        _write(self.paths.root / "decision_rules.json", dict(
            rules, published_at=in_force, available_at=in_force,
            version="%s+backtest-assumed-in-force" % rules["version"]))
        policy = _read(self.project_root / "config" / "decision_policy.json")
        record["decision_policy"] = {"original_available_at": policy["available_at"]}
        _write(self.paths.root / "decision_policy.json", dict(policy, available_at=in_force))
        sector = _read(self.project_root / "data" / "sector_classification.json")
        record["sector_classification"] = {"original_version": sector["version"], "original_available_at": sector["available_at"]}
        _write(self.paths.root / "sector_classification.json", dict(sector, available_at=in_force))
        return record

    def _rules_path(self) -> Path:
        return self.paths.root / "decision_rules.json"

    # -------------------------------------------------------------------- run
    def run(self) -> Dict[str, object]:
        days = trading_days(self.calendar, self.request)
        if not days:
            raise ReplayError("區間內沒有交易日")
        self._setup(days)
        repository = VirtualAccountRepository(self.paths.accounts, ACCOUNT_ID)
        account_runner = DailyAccountRunner(
            repository, self.paths.database, self.paths.decisions, calendar=self.calendar
        )
        entries: List[Dict[str, object]] = []
        for index, day in enumerate(days):
            self.log("[replay] %s（%d/%d）" % (day.isoformat(), index + 1, len(days)))
            entry = self._run_day(day, account_runner, repository)
            broken = [text for text in entry["errors"] if any(marker in text for marker in INFRASTRUCTURE_MARKERS)]
            if broken:
                # 不寫入 day.json，也不繼續往後的日期；用量恢復後以 --resume 從這一天續跑。
                raise ReplayInterrupted("%s 因 Agent 執行環境失敗而中斷：%s（用量恢復後加 --resume 續跑）" % (day.isoformat(), broken[0]))
            entries.append(entry)
            _write(self.paths.day(day) / "day.json", entry)
        last = days[-1]
        final = account_runner.settle("%sT%s+08:00" % (last.isoformat(), PRICE_AVAILABLE_TIME.isoformat()))
        self.log("[replay] 最後一日結算：%s" % final.get("status"))
        final_state = repository.latest()["state"]
        report = build_report(
            self.request, days, entries, final, state_metrics(final_state), self._states(repository),
            self.paths.database, _read(self.paths.manifest),
            _read(self.project_root / "config" / "competition_rules.json"),
        )
        _write(self.paths.root / "report.json", report)
        (self.paths.root / "report.md").write_text(render_markdown(report), encoding="utf-8")
        return report

    def _states(self, repository: VirtualAccountRepository) -> List[Dict[str, object]]:
        """所有帳戶狀態的收盤指標，供報告重算淨值曲線。"""
        result = []
        for run_dir in sorted(repository.runs.iterdir()):
            state_path = run_dir / "state.json"
            if state_path.exists():
                state = _read(state_path)
                result.append({
                    "run_id": run_dir.name, "sequence": state["sequence"],
                    "type": state.get("provenance", {}).get("type"),
                    "trade_date": state.get("provenance", {}).get("trade_date"),
                    **state_metrics(state),
                })
        return sorted(result, key=lambda item: item["sequence"])

    def _run_day(self, day: date, account_runner: DailyAccountRunner, repository: VirtualAccountRepository) -> Dict[str, object]:
        day_dir = self.paths.day(day)
        day_dir.mkdir(parents=True, exist_ok=True)
        cutoff = cutoff_for(day)
        stamp = day.strftime("%Y%m%d")
        decision_run_id = "decision-%sT005500Z" % stamp  # 08:55 +08:00 = 00:55Z
        entry: Dict[str, object] = {"date": day.isoformat(), "cutoff": cutoff, "decision_run_id": decision_run_id, "errors": []}
        snapshot_path = day_dir / "snapshot.json"
        try:
            if not snapshot_path.exists():
                snapshot = DataAgentService(MarketDataDatabase(self.paths.database)).build_snapshot(
                    cutoff, run_id="replay-%s" % stamp, require_prices=True, require_universe_validation=False,
                )
                _write(snapshot_path, snapshot.as_dict())
            snapshot_data = _read(snapshot_path)
            entry["snapshot_id"] = snapshot_data["snapshot_id"]
            entry["latest_trade_date"] = snapshot_data.get("latest_trade_date")
            entry["document_count"] = len(snapshot_data.get("documents", []))
            if not snapshot_data.get("usable"):
                raise ReplayError("Snapshot 不可用：%s" % snapshot_data.get("quality_flags"))
            account_path = day_dir / "account_snapshot.json"
            moved = account_runner.run(snapshot_path, "prepare-%s" % stamp, account_path)
            entry["settle"] = moved["settle"]
            if moved["prepare"].get("status") not in ("prepared", "reused"):
                raise ReplayError("帳戶未能建立本日決策前快照：%s" % moved["prepare"])
            entry["pre_decision"] = state_metrics(repository.latest()["state"])
            status_bundle, assessment = build_assumed_status(snapshot_data, day)
            status_path = day_dir / "trading_status.json"
            _write(status_path, {"bundle": status_bundle, "assessment": assessment})
            entry["trading_status"] = {
                "basis": "backtest_assumed",
                "states": {name: sum(1 for item in assessment["symbols"] if item["state"] == name)
                           for name in ("allowed", "blocked", "unknown")},
            }
            self._decide(day, day_dir, snapshot_path, account_path, status_path, decision_run_id, entry)
        except Exception as error:  # noqa: BLE001 — 失敗日必須保留在時間軸，不中止整段重放
            entry["status"] = "failed"
            entry["errors"].append(str(error))
            self.log("[replay] %s 失敗：%s" % (day.isoformat(), error))
        return entry

    def _decide(self, day, day_dir, snapshot_path, account_path, status_path, decision_run_id, entry) -> None:
        repository = DecisionRepository(self.paths.decisions)
        try:
            repository.verify(decision_run_id)
            existing = True
        except Exception:  # noqa: BLE001 — 尚未封存
            existing = False
        if existing:
            decision = _read(self.paths.decisions / decision_run_id / "decision.json")
            entry.update(status="completed", decision_status=decision.get("status"), order_count=len(decision.get("orders", [])), agent_calls=0)
            return
        pipeline = DailyDecisionPipeline(
            self.project_root, day_dir, self.agent_runner, self.paths.decisions,
            log=self.log, resume=self.resume,
        )
        result = pipeline.run(
            snapshot_path, account_path, status_path,
            self._rules_path(), self.paths.database,
            self.paths.root / "decision_policy.json",
            self.paths.root / "sector_classification.json",
            decision_run_id,
        )
        entry.update(
            status="completed" if result.status == "completed" else "failed",
            decision_status=result.decision_status,
            cash_stance=result.cash_stance,
            order_count=len(result.orders),
            agent_calls=len(result.agent_calls),
            agent_estimated_usage_usd=round(sum(call.get("estimated_usage_usd", 0) for call in result.agent_calls), 4),
        )
        entry["errors"].extend(result.errors)
