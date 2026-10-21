#!/usr/bin/env python3
"""固定封存輸入，另存技術分析師精簡摘要試跑；不建立 Decision run。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.automation.agent_runner import AgentTask, CodexAgentRunner  # noqa: E402
from etf_agent.automation.daily_pipeline import ANALYST_SCHEMA, _SHARED_RULES  # noqa: E402
from etf_agent.decision import AnalystReportValidator, build_analyst_brief, seal_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    bundle = json.loads((args.run_dir / "decision_input.json").read_text(encoding="utf-8"))
    momentum = json.loads((args.run_dir / "momentum.json").read_text(encoding="utf-8"))
    original = json.loads((args.run_dir / "analyst_technical.json").read_text(encoding="utf-8"))
    brief = build_analyst_brief(bundle, momentum, "technical", compact_technical=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    runner = CodexAgentRunner(ROOT)
    previous = {item["symbol"]: item for item in original["items"]}
    comparisons = []
    for index in range(3):
        symbols = [row["symbol"] for row in brief["symbols"][index * 50:(index + 1) * 50]]
        batch = {**brief, "batch": {"batch_index": index, "batch_count": 3, "symbols": symbols},
                 "symbols": brief["symbols"][index * 50:(index + 1) * 50]}
        path = args.output_dir / ("brief_technical_b%d.json" % index)
        path.write_text(json.dumps(batch, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        prompt = (
            "你是 $technical-analyst。先讀 %s，再讀本批輸入摘要 %s。只輸出本批 %d 檔股票（%s），每檔出現一次。%s"
            % (ROOT / "tradeagent/analysis/technical/technical-analyst/SKILL.md", path, len(symbols), "、".join(symbols), _SHARED_RULES)
        )
        call = runner.run(AgentTask("technical_b%d" % index, prompt, ANALYST_SCHEMA), args.output_dir)
        partial = seal_report(batch["report_envelope"], call.output["items"])
        errors = AnalystReportValidator(bundle, "technical", symbols=symbols).validate(partial)
        if errors:
            raise RuntimeError("技術報告未通過原始輸入 Validator：" + "；".join(errors[:5]))
        rows = call.output["items"]
        (args.output_dir / ("technical_b%d_result.json" % index)).write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        changed = [row["symbol"] for row in rows if row["outlook"] != previous[row["symbol"]]["outlook"]]
        comparisons.append({"batch": index, "old_bytes": (args.run_dir / ("brief_technical_b%d.json" % index)).stat().st_size,
                            "new_bytes": path.stat().st_size, "validated": True, "outlook_changed_symbols": changed,
                            "new_unknown_symbols": [row["symbol"] for row in rows if row["outlook"] == "unknown" and previous[row["symbol"]]["outlook"] != "unknown"]})
    result = {"snapshot_id": bundle["snapshot_id"], "decision_cutoff": bundle["decision_cutoff"], "method": "Codex 研究限定試跑；非正式 Decision run，非配對確定性品質證明", "batches": comparisons}
    (args.output_dir / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
