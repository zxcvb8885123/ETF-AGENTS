#!/usr/bin/env python3
"""Probe allowlisted official sources and validate the competition universe."""

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.data import (  # noqa: E402
    MarketDataDatabase,
    SourceHealthProbe,
    UniverseLoader,
    UniverseValidator,
    load_probe_definitions,
    write_json_artifact,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="探測官方資料來源並驗證 150 檔競賽交易池"
    )
    parser.add_argument(
        "--config", type=Path, default=ROOT / "config" / "data_sources.json"
    )
    parser.add_argument(
        "--universe", type=Path, default=ROOT / "data" / "official_universe.csv"
    )
    parser.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "artifacts" / "source-feasibility",
    )
    parser.add_argument("--json", action="store_true", help="將完整結果輸出為 JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        universe = UniverseLoader().load(args.universe)
        if not universe:
            raise ValueError("官方交易池是空的")
        definitions = load_probe_definitions(config)
        if not definitions:
            raise ValueError("沒有設定任何 allowlist 來源探測端點")

        report = SourceHealthProbe().run(definitions, universe)
        validation_config = config.get("universe_validation", {})
        if not isinstance(validation_config, dict):
            raise ValueError("universe_validation 必須是物件")
        successor_candidates = validation_config.get("successor_candidates", {})
        if not isinstance(successor_candidates, dict):
            raise ValueError("successor_candidates 必須是物件")
        validation = UniverseValidator().validate(
            universe, report, successor_candidates=successor_candidates
        )

        database = MarketDataDatabase(args.database)
        database.initialize()
        with database.connect() as connection:
            database.replace_competition_universe(connection, universe)
        database.save_source_feasibility_report(report)
        database.save_universe_validation(validation)

        report_path = args.output_dir / ("source-report-%s.json" % report.report_id)
        validation_path = args.output_dir / (
            "universe-validation-%s.json" % validation.validation_id
        )
        write_json_artifact(report_path, report.as_dict())
        write_json_artifact(validation_path, validation.as_dict())
    except Exception as error:
        if args.json:
            print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        else:
            print("探測失敗：%s" % error, file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "source_report": report.as_dict(),
                    "universe_validation": validation.as_dict(),
                    "artifacts": {
                        "source_report": str(report_path),
                        "universe_validation": str(validation_path),
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for probe in report.probes:
            print(
                "%s：%s，%d 筆，交易池 %d/%d"
                % (
                    probe.source_id,
                    probe.status,
                    probe.row_count,
                    probe.universe_covered,
                    probe.universe_total,
                )
            )
            for error in probe.errors:
                print("ERROR  %s" % error)
        print(
            "交易池：%s；可交易 %d／不可交易 %d／不一致 %d／總計 %d"
            % (
                validation.status,
                validation.tradable_count,
                validation.not_tradable_count,
                validation.mismatch_count,
                validation.total_count,
            )
        )
        for item in validation.instruments:
            if item.status != "tradable":
                print(
                    "QUALITY  %s %s %s%s"
                    % (
                        item.symbol,
                        item.status,
                        item.reason_code,
                        (
                            " successor_candidate=" + item.successor_candidate
                            if item.successor_candidate
                            else ""
                        ),
                    )
                )
        print("來源報告：%s" % report_path)
        print("交易池驗證：%s" % validation_path)
    return 0 if validation.usable else 2


if __name__ == "__main__":
    raise SystemExit(main())
