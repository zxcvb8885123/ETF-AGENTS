#!/usr/bin/env python3
"""只讀量測每日 run 的 Agent brief 與已封存呼叫；不把 bytes 當 token。"""

import argparse
import json
import re
from pathlib import Path


def measure(run_dir: Path) -> dict:
    pipeline = json.loads((run_dir / "pipeline.json").read_text(encoding="utf-8"))
    rows = []
    for path in sorted(run_dir.glob("brief_*.json")):
        name = path.stem.removeprefix("brief_")
        brief = json.loads(path.read_text(encoding="utf-8"))
        raw = sorted(run_dir.glob(name + "_raw_*.json"))
        attempts = [int(match.group(1)) for item in raw if (match := re.fullmatch(re.escape(name) + r"_raw_(\d+)\.json", item.name))]
        rows.append({
            "name": name,
            "input_bytes": path.stat().st_size,
            "symbol_count": len(brief.get("symbols", [])),
            "saved_attempts": sorted(attempts),
            "saved_output_count": len(raw),
        })
    envelope = {}
    if rows:
        first = json.loads((run_dir / ("brief_" + rows[0]["name"] + ".json")).read_text(encoding="utf-8"))
        envelope = first.get("report_envelope") or first.get("packet_envelope") or {}
    if not envelope and (run_dir / "decision_input.json").exists():
        envelope = json.loads((run_dir / "decision_input.json").read_text(encoding="utf-8"))
    return {
        "schema_version": "daily-brief-measurement-1.0",
        "run_id": pipeline["run_id"],
        "agent_cli": pipeline.get("agent_cli"),
        "snapshot_id": envelope.get("snapshot_id"),
        "decision_cutoff": envelope.get("decision_cutoff"),
        "measurement": "UTF-8 檔案 bytes；不是模型實際 token 或 API 費用",
        "actual_tokens": None,
        "note": "續跑的 pipeline.agent_calls 僅列當次新呼叫；saved_attempts 僅列保存的有效輸出，不能據此推論所有失敗呼叫。",
        "briefs": rows,
        "total_input_bytes": sum(row["input_bytes"] for row in rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path, help="選配：保存量測結果（不修改 run）")
    args = parser.parse_args()
    body = json.dumps(measure(args.run_dir), ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body, encoding="utf-8")
    print(body, end="")


if __name__ == "__main__":
    main()
