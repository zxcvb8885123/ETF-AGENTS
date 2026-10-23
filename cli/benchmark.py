#!/usr/bin/env python3
"""基準比較入口：`collect` 補抓基準標的（如 0050）的官方歷史行情，`compare` 計算帳戶對基準的績效。

- `collect` 只保存行情，不把基準標的加入 150 檔交易池；會執行資料庫 `initialize()`（含 schema 遷移）。
- `compare` 唯讀讀取帳本與資料庫，只用 as-of 時點已可得的官方日線；資料不足時該基準顯示 unavailable。
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.benchmark import BenchmarkError, build_benchmark_comparison  # noqa: E402
from etf_agent.data import HistoricalPriceProvider, MarketDataDatabase, RequestGate  # noqa: E402
from etf_agent.data.benchmarks import BenchmarkConfigError, collect_benchmark_history, load_benchmarks  # noqa: E402
from etf_agent.virtual_account import VirtualAccountError, VirtualAccountRepository, active_account_id  # noqa: E402

TAIPEI = timezone(timedelta(hours=8))


def _default_start(repository: VirtualAccountRepository) -> date:
    """預設從開帳月份的 1 日開始補抓（官方端點以月為單位）。"""
    genesis = repository.verify("genesis") / "genesis.json"
    started_at = json.loads(genesis.read_text(encoding="utf-8")).get("started_at")
    opened = datetime.fromisoformat(str(started_at).replace("Z", "+00:00")).astimezone(TAIPEI).date()
    return opened.replace(day=1)


def _pct(value: Optional[str]) -> str:
    return "—" if value is None else "%+.2f%%" % (float(value) * 100)


def _table(result: dict) -> str:
    lines = ["帳戶 %s｜as-of %s｜起始日 %s｜交易日 %d" % (result["account_id"], result["as_of"], result.get("start_date") or "—", result["trading_days"])]
    account = result["account"]
    lines.append("%-22s %-12s %18s %10s %10s %10s %10s" % ("項目", "最新交易日", "NAV", "累積報酬", "超額報酬", "最大回撤", "相對回撤"))
    lines.append("%-22s %-12s %18s %10s %10s %10s %10s" % ("帳戶", account["latest_trade_date"], account["nav"], _pct(account["cumulative_return"]), "—", _pct(account["max_drawdown"]), "—"))
    for item in result["benchmarks"]:
        if item["status"] != "ok":
            lines.append("%-22s unavailable：%s" % (item["label"], item["reason"]))
            continue
        latest = item["latest"] or {}
        comparison = item.get("comparison") or {}
        lines.append("%-22s %-12s %18s %10s %10s %10s %10s" % (
            item["label"], latest.get("trade_date", "—"), latest.get("nav", "—"), _pct(latest.get("cumulative_return")),
            _pct(comparison.get("excess_return")), _pct(item["max_drawdown"]), _pct(comparison.get("relative_drawdown"))))
        if comparison.get("status") == "ok":
            lines.append("    超額報酬與相對回撤取雙方都有估值的 %s（%d 個對齊點）" % (comparison["trade_date"], comparison["aligned_days"]))
        else:
            lines.append("    超額報酬 unavailable：%s" % comparison.get("reason"))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=ROOT / "artifacts" / "ledger")
    parser.add_argument("--account-id", help="預設讀取 config/active_account.json")
    parser.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db")
    parser.add_argument("--benchmarks", type=Path, default=ROOT / "config" / "benchmarks.json")
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", help="以官方個股月行情補抓已核准的基準標的；不進交易池")
    collect.add_argument("--config", type=Path, default=ROOT / "config" / "data_sources.json")
    collect.add_argument("--start", type=date.fromisoformat, help="預設為開帳月份 1 日")
    collect.add_argument("--end", type=date.fromisoformat, help="預設為今天（台北）")

    compare = commands.add_parser("compare", help="計算帳戶對各基準的 NAV、累積報酬、超額報酬、最大回撤與相對回撤")
    compare.add_argument("--rules", type=Path, default=ROOT / "config" / "decision_rules.json")
    compare.add_argument("--as-of", help="含時區的時間；預設為帳本最新狀態的 as_of")
    compare.add_argument("--output", type=Path, help="把完整 JSON 寫到此路徑")
    compare.add_argument("--json", action="store_true", help="標準輸出改印完整 JSON")

    args = parser.parse_args(argv)
    account_id = args.account_id or active_account_id(ROOT)
    repository = VirtualAccountRepository(args.repository, account_id)
    try:
        if args.command == "collect":
            config = json.loads(args.config.read_text(encoding="utf-8"))["historical_daily"]
            provider = HistoricalPriceProvider(
                config["twse_url"], tpex_url=config.get("tpex_url"), timeout_seconds=int(config["timeout_seconds"]),
                user_agent=str(config["user_agent"]),
                gate=RequestGate(min_interval_seconds=float(config.get("min_request_interval_seconds", 2.0)),
                                 backoff_seconds=[float(value) for value in config.get("throttle_backoff_seconds", (30, 120, 300))]),
            )
            start = args.start or _default_start(repository)
            end = args.end or datetime.now(TAIPEI).date()
            result, skipped = collect_benchmark_history(
                MarketDataDatabase(args.database), provider, load_benchmarks(args.benchmarks), start, end,
                retries=int(config.get("retries", 3)), progress=lambda message: print(message, file=sys.stderr, flush=True),
            )
            for item in skipped:
                print("SKIP  " + item)
            if result is None:
                print("沒有可收集的已核准基準標的")
                return 2
            print("期間：%s 至 %s；回應 %d 筆；寫入日線 %d 筆；run_id %s" % (result.start_date, result.end_date, result.fetched_payloads, result.stored_rows, result.run_id))
            for warning in result.warnings[:20]:
                print("WARN  " + warning)
            return 0
        result = build_benchmark_comparison(repository, args.database, args.rules, args.benchmarks, ROOT, args.as_of)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif result["status"] != "ok":
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(_table(result))
        return 0
    except (BenchmarkError, BenchmarkConfigError, VirtualAccountError, OSError, ValueError, KeyError) as error:
        print("基準比較失敗：%s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
