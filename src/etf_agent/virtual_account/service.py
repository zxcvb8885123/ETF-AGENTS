"""可重建、逐日續接的競賽虛擬帳本。"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from etf_agent.core import canonical_sha256, content_sha256, parse_aware_time
from etf_agent.decision.contracts import DecisionInputValidator
from etf_agent.decision.finalization import DecisionRepository, DecisionResultValidator
from etf_agent.ledger import AccountLedger, ExecutionSimulator


RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class VirtualAccountError(ValueError):
    """虛擬帳戶契約或狀態轉移無效。"""


def parse_time(value: object, field: str) -> datetime:
    return parse_aware_time(value, field, error=VirtualAccountError)


def _read(path: Path, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VirtualAccountError("無法讀取%s：%s" % (label, path)) from error
    if not isinstance(value, Mapping):
        raise VirtualAccountError("%s 必須是 JSON 物件" % label)
    return value


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class VirtualAccountRepository:
    """以不可變 run 保存狀態，latest 僅指向已驗證狀態。"""

    def __init__(self, root: Path, account_id: str):
        if not RUN_RE.fullmatch(account_id):
            raise VirtualAccountError("account_id 格式不合法")
        self.root = Path(root).resolve() / account_id
        self.runs = self.root / "runs"

    def save(self, run_id: str, artifacts: Mapping[str, object], state: Mapping[str, object]) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".write.lock"
        try:
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise VirtualAccountError("另一個執行正在更新此虛擬帳戶") from error
        try:
            return self._save_locked(run_id, artifacts, state)
        finally:
            os.close(lock_fd)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def _save_locked(self, run_id: str, artifacts: Mapping[str, object], state: Mapping[str, object]) -> Path:
        if not RUN_RE.fullmatch(run_id):
            raise VirtualAccountError("run_id 格式不合法")
        self.runs.mkdir(parents=True, exist_ok=True)
        final = self.runs / run_id
        if final.is_symlink():
            raise VirtualAccountError("run 目錄不得是符號連結")
        file_hashes = {
            name: canonical_sha256(value) for name, value in sorted(artifacts.items())
        }
        manifest: Dict[str, object] = {
            "schema_version": "1.0", "account_id": self.root.name,
            "run_id": run_id, "artifacts": file_hashes,
        }
        manifest["manifest_sha256"] = canonical_sha256(manifest)
        if final.exists():
            self.verify(run_id)
            existing = _read(final / "manifest.json", "manifest")
            if dict(existing) != manifest:
                raise VirtualAccountError("同一 run_id 已有不同內容，拒絕覆寫")
            return final
        current = self.latest(optional=True)
        if current is None:
            if int(state["sequence"]) != 0 or state.get("parent_state_id") is not None:
                raise VirtualAccountError("首個帳戶狀態必須是 sequence 0 的 genesis")
        elif (
            int(state["sequence"]) != int(current["state"]["sequence"]) + 1
            or state.get("parent_state_id") != current["state"].get("state_id")
        ):
            raise VirtualAccountError("帳本狀態必須直接續接最新父狀態，且 sequence 連續")
        temp = Path(tempfile.mkdtemp(prefix="." + run_id + ".", dir=str(self.runs)))
        try:
            for name, value in artifacts.items():
                if Path(name).name != name or name in {"manifest.json", "latest.json"}:
                    raise VirtualAccountError("artifact 名稱不合法")
                _write(temp / (name + ".json"), value)
            _write(temp / "manifest.json", manifest)
            os.replace(temp, final)
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            raise
        self.verify(run_id)
        current = self.latest(optional=True)
        if current is None or int(state["sequence"]) > int(current["state"]["sequence"]):
            index_path = self.root / "latest.json"
            fd, temporary = tempfile.mkstemp(prefix=".latest-", dir=str(self.root))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"run_id": run_id, "state_sha256": state["content_sha256"]}, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                os.replace(temporary, index_path)
            except Exception:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        return final

    def verify(self, run_id: str) -> Path:
        if not RUN_RE.fullmatch(run_id):
            raise VirtualAccountError("run_id 格式不合法")
        path = self.runs / run_id
        if path.is_symlink():
            raise VirtualAccountError("run 目錄不得是符號連結")
        manifest = _read(path / "manifest.json", "manifest")
        if manifest.get("run_id") != run_id or manifest.get("account_id") != self.root.name:
            raise VirtualAccountError("manifest 身分不一致")
        if manifest.get("manifest_sha256") != content_sha256(manifest, "manifest_sha256"):
            raise VirtualAccountError("manifest 雜湊錯誤")
        expected = {"manifest.json"}
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise VirtualAccountError("manifest 缺少 artifacts")
        for name, digest in artifacts.items():
            if not RUN_RE.fullmatch(str(name)):
                raise VirtualAccountError("artifact 名稱不合法")
            target = path / (str(name) + ".json")
            if target.is_symlink():
                raise VirtualAccountError("artifact 不得是符號連結")
            expected.add(target.name)
            payload = _read(target, str(name))
            if canonical_sha256(payload) != digest:
                raise VirtualAccountError("artifact 雜湊錯誤：%s" % name)
        actual = {item.name for item in path.iterdir() if item.is_file()}
        if actual != expected:
            raise VirtualAccountError("run 檔案集合與 manifest 不一致")
        state = _read(path / "state.json", "state")
        if state.get("content_sha256") != content_sha256(state, "content_sha256"):
            raise VirtualAccountError("VirtualAccountState 雜湊錯誤")
        state_seed = dict(state)
        state_seed.pop("content_sha256", None)
        state_id = state_seed.pop("state_id", None)
        if state_id != "virtual-state:" + canonical_sha256(state_seed)[:20]:
            raise VirtualAccountError("VirtualAccountState state_id 與內容不一致")
        return path

    def latest(self, optional: bool = False) -> Optional[Mapping[str, object]]:
        index_path = self.root / "latest.json"
        if not index_path.exists():
            if optional:
                return None
            raise VirtualAccountError("虛擬帳戶尚未初始化")
        index = _read(index_path, "latest index")
        run_id = str(index.get("run_id", ""))
        run_dir = self.verify(run_id)
        state = _read(run_dir / "state.json", "state")
        if index.get("state_sha256") != state.get("content_sha256"):
            raise VirtualAccountError("latest index 與帳戶狀態不一致")
        return {"run_id": run_id, "state": state, "run_dir": run_dir}


class VirtualAccountService:
    def __init__(self, repository: VirtualAccountRepository):
        self.repository = repository

    def initialize(self, rules_path: Path, account_id: str, started_at: str) -> Mapping[str, object]:
        if account_id != self.repository.root.name:
            raise VirtualAccountError("account_id 必須與 Repository 帳戶路徑一致")
        rules_bytes = rules_path.read_bytes()
        rules = _read(rules_path, "競賽規則")
        capital = Decimal(str(rules.get("initial_capital_twd")))
        if not capital.is_finite() or capital <= 0:
            raise VirtualAccountError("initial_capital_twd 必須為正有限數值")
        parse_time(started_at, "started_at")
        if self.repository.root.exists() and self.repository.latest(optional=True):
            latest = self.repository.latest()
            genesis = _read(self.repository.runs / "genesis" / "genesis.json", "genesis")
            if genesis.get("rules_sha256") != hashlib.sha256(rules_bytes).hexdigest():
                raise VirtualAccountError("已初始化帳戶的規則版本不同，不得重新注資或改寫 genesis")
            return {"reused": True, "state": latest["state"]}
        genesis: Dict[str, object] = {
            "schema_version": "1.0", "account_id": account_id, "currency": "TWD",
            "initial_capital": str(capital), "started_at": started_at,
            "rules_sha256": hashlib.sha256(rules_bytes).hexdigest(),
            "rules_version": rules.get("version", "unspecified"),
        }
        genesis["genesis_id"] = "virtual-genesis:" + canonical_sha256(genesis)[:20]
        state = self._state(
            account_id=account_id, sequence=0, parent_state_id=None, as_of=started_at,
            settled_cash=capital, unsettled_cash=Decimal(0), pending_settlements=[],
            positions=[], nav=capital, provenance={"type": "genesis", "genesis_id": genesis["genesis_id"], "rules_sha256": genesis["rules_sha256"]},
        )
        self.repository.save("genesis", {"genesis": genesis, "state": state}, state)
        return {"reused": False, "genesis": genesis, "state": state}

    def prepare_day(self, snapshot_path: Path, run_id: str, corporate_actions: Optional[List[Mapping[str, object]]] = None) -> Mapping[str, object]:
        current = self.repository.latest()
        snapshot = _read(snapshot_path, "ResearchSnapshot")
        if snapshot.get("usable") is not True:
            raise VirtualAccountError("ResearchSnapshot 未通過品質閘門")
        if not isinstance(snapshot.get("snapshot_id"), str) or not snapshot["snapshot_id"].strip():
            raise VirtualAccountError("ResearchSnapshot 缺少 snapshot_id")
        cutoff = snapshot.get("decision_cutoff")
        cutoff_time = parse_time(cutoff, "decision_cutoff")
        state = current["state"]
        if cutoff_time <= parse_time(state["as_of"], "state.as_of"):
            raise VirtualAccountError("本日 decision_cutoff 必須晚於前次帳戶狀態")
        trade_date = snapshot.get("latest_trade_date")
        if not isinstance(trade_date, str):
            raise VirtualAccountError("Snapshot 缺少 latest_trade_date")
        try:
            date.fromisoformat(trade_date)
        except ValueError as error:
            raise VirtualAccountError("Snapshot.latest_trade_date 格式錯誤") from error
        ledger = self._ledger(state)
        before_settlements = list(ledger.pending_settlements)
        ledger.settle(trade_date)
        actions = list(corporate_actions or [])
        for action in actions:
            if not isinstance(action, Mapping) or parse_time(action.get("available_at"), "corporate_action.available_at") > cutoff_time:
                raise VirtualAccountError("公司行動缺少時間證據或晚於 cutoff")
        ledger.apply_actions(actions)
        prices = self._snapshot_prices(snapshot)
        marked = ledger.snapshot(prices, trade_date)
        account_snapshot = {
            "account_id": self.repository.root.name, "available_at": cutoff,
            "valuation_at": cutoff, "source_evidence_id": "virtual-account:" + canonical_sha256({"state": state["content_sha256"], "snapshot": canonical_sha256(snapshot)})[:24],
            "cash": marked["cash"], "settled_cash": marked["settled_cash"],
            "unsettled_cash": marked["unsettled_cash"], "nav": marked["nav"],
            "positions": [{"symbol": row["symbol"], "shares": row["shares"], "average_cost": row["cost_basis"]} for row in marked["positions"]],
        }
        prepared_state = self._state(
            account_id=self.repository.root.name, sequence=int(state["sequence"]) + 1,
            parent_state_id=state["state_id"], as_of=cutoff,
            settled_cash=Decimal(marked["settled_cash"]), unsettled_cash=Decimal(marked["unsettled_cash"]),
            pending_settlements=ledger.pending_settlements,
            positions=ledger.snapshot(prices, trade_date)["positions"], nav=Decimal(marked["nav"]),
            provenance={"type": "prepared", "parent_run_id": current["run_id"], "snapshot_id": snapshot.get("snapshot_id"), "snapshot_sha256": canonical_sha256(snapshot), "trade_date": trade_date, "account_snapshot": account_snapshot, "settlements_before": before_settlements, "corporate_actions": actions},
        )
        self.repository.save(run_id, {"snapshot": snapshot, "state": prepared_state, "account_snapshot": account_snapshot, "corporate_actions": {"items": actions}}, prepared_state)
        return {"state": prepared_state, "account_snapshot": account_snapshot, "run_id": run_id}

    def apply_decision(self, decision_repository: Path, decision_run_id: str, execution_market_path: Path, close_market_path: Path, settlement_date: str, run_id: str) -> Mapping[str, object]:
        prepared = self.repository.latest()
        state = prepared["state"]
        if state.get("provenance", {}).get("type") != "prepared":
            raise VirtualAccountError("請先為本交易日建立並封存 prepare-day 狀態")
        dpath = DecisionRepository(decision_repository).verify(decision_run_id)
        decision_files = {p.stem: _read(p, p.stem) for p in dpath.glob("*.json") if p.name != "manifest.json"}
        required = {"decision_input", "policy", "momentum", "debate", "intent", "proposal", "scenario", "guard", "risk_review", "revision_history", "decision"}
        if required - set(decision_files):
            raise VirtualAccountError("Decision run 缺少完整重建輸入")
        bundle = decision_files["decision_input"]
        input_errors = DecisionInputValidator(bundle).validate()
        if input_errors:
            raise VirtualAccountError("DecisionInputBundle 驗證失敗：" + "；".join(input_errors))
        expected_account = state["provenance"].get("account_snapshot")
        if bundle.get("account_snapshot") != expected_account:
            raise VirtualAccountError("Decision run 使用的 AccountSnapshot 與本次 prepare 狀態不一致")
        verifier = DecisionResultValidator(bundle, decision_files["policy"], decision_files["momentum"], decision_files["debate"], decision_files["intent"])
        decision_errors = verifier.validate(decision_files["proposal"], decision_files["scenario"], decision_files["guard"], decision_files["risk_review"], decision_files["revision_history"], decision_files["decision"])
        if decision_errors:
            raise VirtualAccountError("DecisionResult 重建驗證失敗：" + "；".join(decision_errors))
        decision = decision_files["decision"]
        cutoff_time = parse_time(state["as_of"], "state.as_of")
        market = _read(execution_market_path, "ExecutionMarketData")
        market_time = parse_time(market.get("available_at"), "execution_market.available_at")
        if market_time <= cutoff_time:
            raise VirtualAccountError("模擬成交資料必須在決策 cutoff 後才可得")
        parse_time(market.get("execution_at"), "execution_market.execution_at")
        if market_time > parse_time(market["execution_at"], "execution_market.execution_at"):
            raise VirtualAccountError("模擬成交資料晚於 execution_at")
        close_market = _read(close_market_path, "CloseMarketData")
        close_time = parse_time(close_market.get("available_at"), "close_market.available_at")
        if close_time <= parse_time(market["execution_at"], "execution_market.execution_at"):
            raise VirtualAccountError("日終行情必須晚於模擬成交時間")
        if close_market.get("price_basis") != "unadjusted" or market.get("price_basis") != "unadjusted":
            raise VirtualAccountError("模擬成交與日終行情必須是未還原價格")
        if decision.get("status") not in {"approved", "no_trade", "rejected"}:
            raise VirtualAccountError("DecisionResult.status 不合法")
        try:
            execution_date = date.fromisoformat(str(settlement_date))
            trade_date = date.fromisoformat(str(state["provenance"].get("trade_date")))
        except ValueError as error:
            raise VirtualAccountError("settlement_date／trade_date 格式錯誤") from error
        if execution_date < trade_date:
            raise VirtualAccountError("settlement_date 不得早於交易日")
        ledger = self._ledger(state)
        rules = bundle["rules"]
        assumptions = {
            "lot_size": rules["lot_size"], "commission_rate": rules["commission_rate"],
            "sell_tax_rate": rules["sell_tax_rate"], "minimum_commission": rules["minimum_commission"],
            "reuse_sell_proceeds": bool(rules["reuse_sell_proceeds"]),
        }
        simulator = ExecutionSimulator(assumptions)
        execution = simulator.run(decision, market, ledger.buying_power(bool(rules["reuse_sell_proceeds"])), market["execution_at"])
        ledger.apply_fills(execution, settlement_date, bool(rules["reuse_sell_proceeds"]))
        close_quotes = close_market.get("quotes")
        if not isinstance(close_quotes, Mapping):
            raise VirtualAccountError("CloseMarketData.quotes 必須是物件")
        close_prices = {symbol: row.get("close_price") for symbol, row in close_quotes.items() if isinstance(row, Mapping)}
        close_trade_date = str(close_market.get("trade_date", trade_date.isoformat()))
        try:
            close_day = date.fromisoformat(close_trade_date)
        except ValueError as error:
            raise VirtualAccountError("CloseMarketData.trade_date 格式錯誤") from error
        if close_day < trade_date:
            raise VirtualAccountError("CloseMarketData.trade_date 不得早於決策 Snapshot 交易日")
        state_snapshot = ledger.snapshot(close_prices, close_trade_date)
        next_state = self._state(
            account_id=self.repository.root.name, sequence=int(state["sequence"]) + 1,
            parent_state_id=state["state_id"], as_of=close_market["available_at"],
            settled_cash=Decimal(state_snapshot["settled_cash"]), unsettled_cash=Decimal(state_snapshot["unsettled_cash"]),
            pending_settlements=ledger.pending_settlements, positions=state_snapshot["positions"], nav=Decimal(state_snapshot["nav"]),
            provenance={"type": "close", "parent_run_id": prepared["run_id"], "decision_run_id": decision_run_id, "decision_status": decision["status"], "execution_sha256": execution["content_sha256"], "execution_at": market["execution_at"], "trade_date": close_trade_date, "settlement_date": str(settlement_date), "close_market_sha256": canonical_sha256(close_market)},
        )
        transition = {"schema_version": "1.0", "parent_state_id": state["state_id"], "decision_run_id": decision_run_id, "decision_status": decision["status"], "execution": execution, "close_ledger": state_snapshot, "state_sha256": next_state["content_sha256"]}
        transition["content_sha256"] = canonical_sha256(transition)
        self.repository.save(run_id, {"decision_run": {"run_id": decision_run_id, "manifest_sha256": _read(dpath / "manifest.json", "Decision manifest")["manifest_sha256"]}, "execution_market": market, "close_market": close_market, "transition": transition, "state": next_state}, next_state)
        return {"run_id": run_id, "state": next_state, "transition": transition}

    @staticmethod
    def _snapshot_prices(snapshot: Mapping[str, object]) -> Dict[str, object]:
        rows = snapshot.get("latest_prices")
        if not isinstance(rows, list) or not rows:
            raise VirtualAccountError("Snapshot 缺少 latest_prices")
        prices = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise VirtualAccountError("Snapshot latest_prices 格式錯誤")
            symbol = str(row.get("symbol", "")).upper()
            price = row.get("analysis_close_price")
            if not symbol or price is None or symbol in prices:
                raise VirtualAccountError("Snapshot 股票價格缺漏或重複")
            prices[symbol] = price
        return prices

    @staticmethod
    def _ledger(state: Mapping[str, object]) -> AccountLedger:
        ledger = AccountLedger({
            "settled_cash": state["settled_cash"], "unsettled_cash": state["unsettled_cash"],
            "positions": [{"symbol": row["symbol"], "shares": row["shares"], "cost_basis": row["cost_basis"]} for row in state["positions"]],
        })
        ledger.pending_settlements = [dict(item) for item in state.get("pending_settlements", [])]
        if sum((Decimal(str(item["amount"])) for item in ledger.pending_settlements), Decimal(0)) != ledger.unsettled_cash:
            raise VirtualAccountError("未交割明細合計與 unsettled_cash 不一致")
        return ledger

    @staticmethod
    def _state(account_id: str, sequence: int, parent_state_id: Optional[str], as_of: str, settled_cash: Decimal, unsettled_cash: Decimal, pending_settlements: List[Mapping[str, object]], positions: List[Mapping[str, object]], nav: Decimal, provenance: Mapping[str, object]) -> Dict[str, object]:
        if settled_cash < 0 or nav <= 0:
            raise VirtualAccountError("虛擬帳戶現金或 NAV 非法")
        body: Dict[str, object] = {
            "schema_version": "1.0", "account_id": account_id, "sequence": sequence,
            "parent_state_id": parent_state_id, "as_of": as_of,
            "settled_cash": str(settled_cash), "unsettled_cash": str(unsettled_cash),
            "pending_settlements": [dict(item) for item in pending_settlements],
            "positions": [dict(item) for item in positions], "nav": str(nav),
            "provenance": dict(provenance),
        }
        body["state_id"] = "virtual-state:" + canonical_sha256(body)[:20]
        body["content_sha256"] = content_sha256(body)
        return body
