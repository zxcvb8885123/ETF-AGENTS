#!/usr/bin/env python3
"""資料不足時的確定性補抓入口；不執行分析或交易 Agent。"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.core.artifact_store import ImmutableRunStore
from etf_agent.core import parse_aware_time
from etf_agent.data.corporate import CorporateDataCollector, OfficialCorporateProvider
from etf_agent.data.database import MarketDataDatabase
from etf_agent.data.fundamental_repair import FundamentalRepairProvider, OfficialCompanyProfileProvider
from etf_agent.data.snapshot import DataAgentService
from etf_agent.data.universe import UniverseLoader


def main():
    parser = argparse.ArgumentParser(description="補抓官方當期與 FinMind 去年同期累計財報")
    parser.add_argument("--database", type=Path, default=ROOT / "var/etf_agent.db")
    parser.add_argument("--universe", type=Path, default=ROOT / "data/official_universe.csv")
    parser.add_argument("--decision-cutoff", help="歷史截止時間；新抓資料晚於它時拒絕補入")
    parser.add_argument("--symbols", nargs="+", help="僅補抓指定交易池股票，例如 1101.TW 2330.TW")
    parser.add_argument("--output-root", type=Path, default=ROOT / "artifacts/fundamental_repairs")
    args = parser.parse_args()
    if args.decision_cutoff is not None:
        parse_aware_time(args.decision_cutoff, "decision_cutoff")
    universe = UniverseLoader().load(args.universe)
    allowed = {item.symbol for item in universe}
    if args.symbols and not set(args.symbols) <= allowed:
        raise ValueError("指定股票不在交易池")
    database = MarketDataDatabase(args.database)
    official = OfficialCorporateProvider("TWSE_MOPS_INCOME_CI", "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci",
                                         "TWSE", "financial_statement", "income_statement", "ci", retries=1)
    current = CorporateDataCollector(database, [official]).collect(universe)
    anchors = [item for item in current.records if item.document.symbol in allowed]
    if args.symbols:
        anchors = [item for item in anchors if item.document.symbol in set(args.symbols)]
    if not anchors or current.failures:
        raise ValueError("官方核對基準不可用；停止補抓")
    profiles = CorporateDataCollector(database, [OfficialCompanyProfileProvider()]).collect(universe)
    repair = CorporateDataCollector(database, [FundamentalRepairProvider(anchors, args.decision_cutoff)]).collect(universe)
    cutoff = args.decision_cutoff or datetime.now(timezone.utc).isoformat()
    snapshot = DataAgentService(database).build_snapshot(cutoff, require_prices=False,
                                                       require_universe_validation=False).as_dict()
    summary = {"run_id": repair.run_id, "decision_cutoff": cutoff,
               "official_documents": current.stored_documents,
               "company_profile_documents": profiles.stored_documents,
               "prior_documents": repair.stored_documents, "warnings": repair.warnings,
               "failures": list(repair.failures) + list(profiles.failures), "mode": "fundamental_research_only",
               "downstream_agents_run": False, "historical_backfill_permitted": False}
    output = ImmutableRunStore(args.output_root, schema_version="1.0").save(
        repair.run_id, {"snapshot": snapshot, "summary": summary})
    print(json.dumps({**summary, "output": str(output)}, ensure_ascii=False, indent=2))
    return 1 if repair.failures or repair.warnings or not repair.records else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as error:
        print("補抓停止：%s" % error, file=sys.stderr)
        raise SystemExit(1)
