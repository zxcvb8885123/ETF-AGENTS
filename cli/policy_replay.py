#!/usr/bin/env python3
"""便宜版回測：用已封存 Decision run 的 Agent 輸出，換 Policy 以確定性程式多日滾動回放（零 token）。

只讀決策庫與行情資料庫（唯讀開啟，不觸發遷移），結果寫在 artifacts/evaluation/replays/。
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.evaluation import (  # noqa: E402
    EvaluationError, REPLAY_ROOT, build_replay, compare_replays, read_replay, render_comparison, save_replay, verify_replay,
)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--decisions", type=Path, default=ROOT / "artifacts" / "decisions", help="決策庫（預設 artifacts/decisions）")
    parser.add_argument("--database", type=Path, default=ROOT / "var" / "etf_agent.db", help="行情資料庫（唯讀開啟）")


def _run_one(args, policy_path: Path, replay_id=None):
    body, artifacts = build_replay(
        ROOT, policy_path, decision_root=args.decisions, database=args.database,
        as_of=args.as_of or datetime.now(timezone.utc).isoformat(), start=args.start, end=args.end, run_ids=args.runs,
        allow_gaps=args.allow_gaps, initial_cash=args.initial_cash, log=print,
    )
    saved_id, run_dir = save_replay(ROOT, body, artifacts, replay_id)
    return saved_id, run_dir, body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="依日期範圍或指定 run 逐日回放（不呼叫 Agent）")
    run.add_argument("--policy", type=Path, default=ROOT / "config" / "decision_policy.json", help="策略樣板 JSON")
    run.add_argument("--compare-policy", type=Path, help="第二個策略樣板；同一批 run 再回放一次並產生比較")
    run.add_argument("--start", help="目標時段日起（YYYY-MM-DD，台北）")
    run.add_argument("--end", help="目標時段日迄（YYYY-MM-DD，台北）")
    run.add_argument("--runs", nargs="*", help="指定 Decision run ID（省略時用日期範圍）")
    run.add_argument("--as-of", help="只用此時間前抓到的官方價格（含時區 ISO；預設現在）")
    run.add_argument("--allow-gaps", action="store_true", help="有官方交易日卻沒有決策 run 時不停止，持股以收盤估值並標為缺日")
    run.add_argument("--initial-cash", help="改用全現金起點（預設取第一個 run 封存的帳戶快照）")
    run.add_argument("--replay-id", help="自訂回放 ID（預設 replay-<UTC 時間>）")
    _common(run)
    verify = commands.add_parser("verify", help="以報告記錄的輸入重算整段回放並比對")
    verify.add_argument("--replay-id", required=True)
    verify.add_argument("--policy", type=Path, help="改用指定策略樣板檔重算（預設用報告記錄的路徑）")
    _common(verify)
    compare = commands.add_parser("compare", help="比較兩個已封存回放（同一批 run、同一 as_of）")
    compare.add_argument("--replay-a", required=True)
    compare.add_argument("--replay-b", required=True)
    args = parser.parse_args()
    try:
        if args.command == "run":
            replay_id, run_dir, body = _run_one(args, args.policy, args.replay_id)
            output = {"replay_id": replay_id, "status": body["status"], "metrics": body["metrics"],
                      "report": str(run_dir.parent / ("%s.md" % replay_id))}
            if args.compare_policy:
                other_id, other_dir, other = _run_one(args, args.compare_policy, "%s-b" % replay_id if args.replay_id else None)
                left, _ = read_replay(ROOT, replay_id)
                right, _ = read_replay(ROOT, other_id)
                comparison = compare_replays(left, right)
                path = ROOT / REPLAY_ROOT / ("compare-%s-vs-%s" % (replay_id, other_id))
                path.with_suffix(".json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                path.with_suffix(".md").write_text(render_comparison(comparison), encoding="utf-8")
                output["compare"] = {"replay_id_b": other_id, "status_b": other["status"], "report": str(path.with_suffix(".md"))}
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0 if body["status"] == "completed" else 2
        if args.command == "verify":
            result = verify_replay(ROOT, args.replay_id, policy_template_path=args.policy, decision_root=args.decisions,
                                   database=args.database, log=print)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["valid"] else 1
        left, _ = read_replay(ROOT, args.replay_a)
        right, _ = read_replay(ROOT, args.replay_b)
        comparison = compare_replays(left, right)
        path = ROOT / REPLAY_ROOT / ("compare-%s-vs-%s" % (args.replay_a, args.replay_b))
        path.with_suffix(".json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        path.with_suffix(".md").write_text(render_comparison(comparison), encoding="utf-8")
        print(json.dumps({"metrics": comparison["metrics"], "order_differences": len(comparison["order_differences"]),
                          "report": str(path.with_suffix(".md"))}, ensure_ascii=False, indent=2))
        return 0
    except (EvaluationError, OSError, ValueError, KeyError) as error:
        print("回放停止：%s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
