#!/usr/bin/env python3
"""啟動唯讀虛擬帳戶績效儀表板（FastAPI）。"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import uvicorn  # noqa: E402

from etf_agent.dashboard.app import create_app  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=ROOT / "artifacts" / "virtual_accounts")
    parser.add_argument("--account-id", default="ai-cup-2026")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    uvicorn.run(create_app(args.repository, args.account_id), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
