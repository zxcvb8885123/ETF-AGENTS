#!/usr/bin/env python3
"""固定封存輸入，另存事件分析師精簡摘要試跑；不建立 Decision run。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.automation.agent_runner import AgentTask, CodexAgentRunner  # noqa: E402
from etf_agent.automation.daily_pipeline import EVENT_ANALYST_SCHEMA, _SHARED_RULES  # noqa: E402
from etf_agent.decision import AnalystReportValidator, seal_event_market_report  # noqa: E402
from etf_agent.decision.analysts import compact_event_batch  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    bundle = json.loads((args.run_dir / "decision_input.json").read_text(encoding="utf-8"))
    original = json.loads((args.run_dir / "analyst_event.json").read_text(encoding="utf-8"))
    previous = {item["symbol"]: item for item in original["items"]}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    runner = CodexAgentRunner(ROOT)
    comparisons = []
    for index in range(3):
        old_path = args.run_dir / ("brief_event_b%d.json" % index)
        old_brief = json.loads(old_path.read_text(encoding="utf-8"))
        brief, source = compact_event_batch(old_brief, bundle)
        source_path = args.output_dir / ("event_source_b%d.json" % index)
        source_path.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        brief["event_source_path"] = str(source_path.resolve())
        brief["event_source_sha256"] = source["content_sha256"]
        path = args.output_dir / ("brief_event_b%d.json" % index)
        path.write_text(json.dumps(brief, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        symbols = brief["batch"]["symbols"]
        prompt = (
            "你是 $event-analyst。先讀 %s，再讀本批輸入摘要 %s。事件中段省略且不足以判斷時，按 evidence_id 核對完整來源 %s；不能猜測。只輸出本批 %d 檔股票（%s），每檔出現一次。%s"
            % (ROOT / "tradeagent/analysis/event/event-analyst/SKILL.md", path, source_path, len(symbols), "、".join(symbols), _SHARED_RULES)
        )
        call = runner.run(AgentTask("event_b%d" % index, prompt, EVENT_ANALYST_SCHEMA), args.output_dir)
        partial = seal_event_market_report(bundle, brief["report_envelope"], call.output["items"])
        errors = AnalystReportValidator(bundle, "event", symbols=symbols).validate(partial)
        if errors:
            raise RuntimeError("事件報告未通過原始輸入 Validator：" + "；".join(errors[:5]))
        rows = call.output["items"]
        (args.output_dir / ("event_b%d_result.json" % index)).write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        changed = []
        for row in rows:
            before = {event["evidence_id"]: event["materiality"] for event in previous[row["symbol"]]["events"]}
            changed.extend(event["evidence_id"] for event in row["events"] if event["materiality"] != before[event["evidence_id"]])
        comparisons.append({"batch": index, "old_bytes": old_path.stat().st_size, "new_bytes": path.stat().st_size,
                            "source_bytes_outside_brief": source_path.stat().st_size, "validated": True,
                            "materiality_changed_evidence_ids": changed,
                            "new_unknown_symbols": [row["symbol"] for row in rows if row["outlook"] == "unknown" and previous[row["symbol"]]["outlook"] != "unknown"]})
    result = {"snapshot_id": bundle["snapshot_id"], "decision_cutoff": bundle["decision_cutoff"], "method": "Codex 研究限定試跑；非正式 Decision run，非確定性品質證明", "batches": comparisons}
    (args.output_dir / "comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
