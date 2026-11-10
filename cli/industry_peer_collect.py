#!/usr/bin/env python3
"""產業比較 C1：收集 t187ap14 各產業 EPS 統計、建立代碼↔名稱表、逐檔核對交易池。

只收集與稽核，不進決策。退出碼：0 全部來源完成；2 部分來源失敗或被跳過（降級，不中斷每日流程）；
1 全部來源失敗或設定錯誤。
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.core import parse_aware_time  # noqa: E402
from etf_agent.data.database import MarketDataDatabase  # noqa: E402
from etf_agent.data.industry_peer import (  # noqa: E402
    IndustryPeerCollector,
    IndustryPeerError,
    IndustryPeerRepository,
    build_industry_name_map,
    load_peer_sources,
    verify_industry_name_map,
    verify_pool_against_official,
)
from etf_agent.data.universe import UniverseLoader  # noqa: E402


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(args) -> int:
    if args.decision_cutoff is not None:
        parse_aware_time(args.decision_cutoff, "decision_cutoff", error=IndustryPeerError)
    sources = load_peer_sources(args.config)
    universe = UniverseLoader().load(args.universe)
    sector = json.loads(args.sector_classification.read_text(encoding="utf-8"))
    repository = IndustryPeerRepository(MarketDataDatabase(args.database))
    outcomes = IndustryPeerCollector(repository, sources).collect(args.decision_cutoff)
    now = datetime.now(timezone.utc).isoformat()
    # 核對與對照表讀資料庫內最新的已保存列（今日某端點失敗時沿用較早的已保存列，並在 sources 標明）。
    peer_rows = repository.latest_rows(as_of=args.decision_cutoff or now)
    source_info = [item.as_dict() for item in outcomes]
    name_map = build_industry_name_map(
        universe, sector["sector_by_symbol"], peer_rows, str(sector["version"]), source_info
    )
    verify_industry_name_map(name_map)
    verification = verify_pool_against_official(repository.database, universe, peer_rows, args.decision_cutoff or now)
    market_counts = {}
    for row in peer_rows:
        market_counts[str(row["market"])] = market_counts.get(str(row["market"]), 0) + 1
    failed = [item for item in outcomes if item.status != "stored"]
    status = "completed" if not failed else ("failed" if len(failed) == len(outcomes) else "partial")
    summary = {
        "schema_version": "1.0",
        "generated_at": now,
        "mode": "collect_and_audit_only",
        "enters_decision": False,
        "status": status,
        "sources": source_info,
        "stored_company_counts": dict(sorted(market_counts.items())),
        "name_map": {"version": name_map["version"], "status": name_map["status"],
                     "mapped_codes": {m: len(v) for m, v in name_map["mapping"].items()},
                     "conflicts": name_map["conflicts"],
                     "cross_market_name_differences": name_map["cross_market_name_differences"],
                     "unmapped_pool_symbols": len(name_map["unmapped_pool_symbols"])},
        "verification": {key: verification[key] for key in
                         ("as_of", "total", "verified", "unavailable", "not_comparable", "value_mismatch", "reason_counts")},
        "verification_by_symbol": verification["by_symbol"],
    }
    write_json(args.report, summary)
    write_json(args.map_output, name_map)
    print(json.dumps({key: summary[key] for key in ("status", "stored_company_counts", "name_map", "verification")},
                     ensure_ascii=False, indent=2))
    if status == "failed":
        return 1
    return 0 if status == "completed" else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=ROOT / "var/etf_agent.db")
    parser.add_argument("--universe", type=Path, default=ROOT / "data/official_universe.csv")
    parser.add_argument("--sector-classification", type=Path, default=ROOT / "data/sector_classification.json")
    parser.add_argument("--config", type=Path, default=ROOT / "config/industry_peer_sources.json")
    parser.add_argument("--report", type=Path, default=ROOT / "artifacts/data/industry_peer_latest.json")
    parser.add_argument("--map-output", type=Path, default=ROOT / "artifacts/data/industry_name_map_latest.json",
                        help="代碼↔名稱表輸出；要升格為版本化靜態產物時改指向 data/industry_name_map.json")
    parser.add_argument("--decision-cutoff", help="歷史截止時間；抓取時間晚於它的資料拒絕")
    args = parser.parse_args()
    try:
        return run(args)
    except (IndustryPeerError, OSError, ValueError, KeyError) as error:
        print("產業比較收集停止：%s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
