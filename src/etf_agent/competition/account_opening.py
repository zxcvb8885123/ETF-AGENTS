"""比賽帳戶維運：第一天開新帳戶（可用第一天 DataPack 核對開帳金額），以及對帳不一致時依 DataPack 重建帳本。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, Mapping, Optional

from etf_agent.core import parse_aware_time
from etf_agent.virtual_account import (
    VirtualAccountRepository, VirtualAccountService, active_account_id, write_active_account,
)

from .datapack import DataPackError, load_datapack
from .service import read_object


def _check_first_day(pack, team_id: str, capital: Decimal) -> None:
    """只接受第一個交易日的 DataPack：尚無結算、全現金、0 檔，金額等於規則本金。"""
    if pack["team_id"] != team_id:
        raise DataPackError("DataPack 隊伍代號 %s 與設定 %s 不同" % (pack["team_id"], team_id))
    if pack["as_of_date"] is not None or pack["missing"] or pack["holdings"] != [] or pack["prev_day_orders"]["outcome"] != "none":
        raise DataPackError("這不是第一個交易日的 DataPack（須 as_of_date 為 null、missing 為空、holdings 為空陣列、前日無委託）")
    account = pack["account"]
    for field in ("cash", "nav_tradable", "aum"):
        if Decimal(str(account[field])) != capital:
            raise DataPackError("DataPack account.%s=%s 與規則本金 %s 不同" % (field, account[field], capital))
    if Decimal(str(account["receivable_cash"])) != 0 or account["holdings_count"] != 0:
        raise DataPackError("第一個交易日不應有應收配息或持股")


def open_competition_account(root: Path, new_account_id: str, *, datapack_path: Optional[Path] = None,
                             rules_path: Optional[Path] = None, profile_path: Optional[Path] = None,
                             now: Optional[datetime] = None) -> Dict[str, object]:
    """建立全新帳戶並切換為使用中帳戶；舊帳戶原封保留。任何檢查失敗都不建立帳戶。"""
    root = Path(root)
    rules_path = rules_path or root / "config" / "competition_rules.json"
    profile_path = profile_path or root / "config" / "competition_profile.json"
    now = now or datetime.now(timezone.utc)
    started_at = now.isoformat()
    previous = active_account_id(root)
    if new_account_id == previous:
        raise DataPackError("新帳戶 ID 不得與目前使用中的帳戶 %s 相同" % previous)
    repository = VirtualAccountRepository(root / "artifacts" / "ledger", new_account_id)
    if repository.root.exists():
        raise DataPackError("帳戶 %s 已存在，拒絕沿用或重新注資；請換一個新 ID" % new_account_id)
    rules = read_object(rules_path, "競賽規則")
    capital = Decimal(str(rules.get("initial_capital_twd")))
    loaded = None
    if datapack_path is not None:
        team_id = read_object(profile_path, "competition_profile").get("team_id")
        loaded = load_datapack(datapack_path, now)
        _check_first_day(loaded["pack"], team_id, capital)
    opened = VirtualAccountService(repository).initialize(rules_path, new_account_id, started_at)
    write_active_account(root, new_account_id, previous_account_id=previous, opened_at=started_at,
                         opened_from_datapack_sha256=loaded["raw_sha256"] if loaded else None)
    return {"account_id": new_account_id, "previous_account_id": previous, "opened_at": started_at,
            "initial_capital": str(capital), "genesis_id": opened["genesis"]["genesis_id"],
            "datapack_checked": loaded is not None,
            "datapack_sha256": loaded["raw_sha256"] if loaded else None}


def rebase_account_to_datapack(root: Path, account_id: str, loaded: Mapping[str, object], snapshot_path: Path,
                               prepare_run_id: str, account_output: Path, universe: Mapping[str, str]) -> Dict[str, object]:
    """帳本與主辦方結算不一致時，以 DataPack 重建帳本並用同一份 Snapshot 重新建立決策前快照。

    只在 DataPack 於 Snapshot cutoff 前產生、結算日與 Snapshot 行情日相同、且目前 prepare 尚未產生決策時執行；
    舊狀態留在雜湊鏈中，不改寫、不刪除。
    """
    from etf_agent.virtual_account.daily import find_decision_run

    root = Path(root)
    pack = loaded["pack"]
    if pack["holdings"] is None or pack["account"] is None:
        raise DataPackError("DataPack 尚無結算資料，不能用來重建帳本")
    snapshot = read_object(Path(snapshot_path), "ResearchSnapshot")
    cutoff = parse_aware_time(snapshot.get("decision_cutoff"), "decision_cutoff", error=DataPackError)
    generated = parse_aware_time(pack["generated_at"], "generated_at", error=DataPackError)
    if generated >= cutoff:
        raise DataPackError("DataPack 產生時間不早於 Snapshot 截止時間，不能回填；請不加 --skip-data 重新執行以建立新的 Snapshot")
    if pack["as_of_date"] is not None and pack["as_of_date"] != snapshot.get("latest_trade_date"):
        raise DataPackError("DataPack 結算日 %s 與 Snapshot 行情日 %s 不同" % (pack["as_of_date"], snapshot.get("latest_trade_date")))
    repository = VirtualAccountRepository(root / "artifacts" / "ledger", account_id)
    current = repository.latest()
    if find_decision_run(root / "artifacts" / "decisions", current["state"]["provenance"].get("account_snapshot")) is not None:
        raise DataPackError("目前帳戶快照已有待成交的決策，不能重建；請先處理該決策")
    positions = {}
    for row in pack["holdings"]:
        if int(row["shares"]) <= 0:
            continue
        symbol = universe.get(row["code"])
        if symbol is None:
            raise DataPackError("DataPack 持股 %s 不在官方交易池，無法對應" % row["code"])
        positions[symbol] = {"shares": int(row["shares"]), "close_price": row["close_price"]}
    service = VirtualAccountService(repository)
    source = {"datapack_sha256": loaded["raw_sha256"], "team_id": pack["team_id"], "trade_date": pack["trade_date"],
              "as_of_date": pack["as_of_date"], "generated_at": pack["generated_at"],
              "cost_basis": "主辦方未提供成本，以結算收盤價代替"}
    rebased = service.rebase(positions, pack["account"]["cash"], pack["generated_at"], source, "rebase-" + prepare_run_id)
    prepared = service.prepare_day(Path(snapshot_path), prepare_run_id)
    account_output = Path(account_output)
    account_output.parent.mkdir(parents=True, exist_ok=True)
    account_output.write_text(json.dumps(prepared["account_snapshot"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"rebase_run_id": rebased["run_id"], "prepare_run_id": prepared["run_id"], "account_snapshot": prepared["account_snapshot"]}
