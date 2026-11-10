#!/usr/bin/env python3
"""資料不足時的確定性補抓入口；不執行分析或交易 Agent。"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_agent.core.artifact_store import ImmutableRunStore
from etf_agent.core import parse_aware_time
from etf_agent.data.corporate import CorporateDataCollector, OfficialCorporateProvider
from etf_agent.data.database import MarketDataDatabase
from etf_agent.data.fundamental_repair import (
    DEFAULT_SCHEDULED_MAX_SYMBOLS,
    FundamentalRepairProvider,
    OfficialCompanyProfileProvider,
    redact_secret,
    run_scheduled_repair,
)
from etf_agent.data.supplemental_sources import FinMindFundamentalProvider
from etf_agent.data.snapshot import DataAgentService
from etf_agent.data.universe import UniverseLoader

# 每日收集器已在用的官方損益表端點（來源代號、URL、業別）。補抓的核對基準必須涵蓋全部，
# 否則該業別沒有基準可比，FinMind 去年同期一律被跳過。
OFFICIAL_INCOME_SOURCES = (
    ("TWSE_MOPS_INCOME_CI", "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci", "ci"),
    ("TPEX_MOPS_INCOME_CI", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci", "ci"),
    ("TWSE_MOPS_INCOME_MIM", "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_mim", "mim"),
    ("TWSE_MOPS_INCOME_FH", "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_fh", "fh"),
    ("TWSE_MOPS_INCOME_BASI", "https://openapi.twse.com.tw/v1/opendata/t187ap06_L_basi", "basi"),
    ("TPEX_MOPS_INCOME_BD", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_bd", "bd"),
)


def run_scheduled(args, database, universe, anchors, anchor_failures):
    """排程退出碼：0 全部完成或已有同期文件；2 部分未通過核對或超過上限（預期、降級）；1 額度或憑證錯誤。"""
    token = os.environ.get("FINMIND_TOKEN", "")
    summary = run_scheduled_repair(database, universe, anchors, FinMindFundamentalProvider(token),
                                   args.max_symbols, args.database, token)
    summary["official_anchor_failures"] = [redact_secret(item, token) for item in anchor_failures]
    summary["generated_at"] = datetime.now(timezone.utc).isoformat()
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    if summary["status"] == "aborted":
        return 1
    return 2 if summary["status"] == "partial" or anchor_failures else 0


def main():
    parser = argparse.ArgumentParser(description="補抓官方當期與 FinMind 去年同期累計財報")
    parser.add_argument("--database", type=Path, default=ROOT / "var/etf_agent.db")
    parser.add_argument("--universe", type=Path, default=ROOT / "data/official_universe.csv")
    parser.add_argument("--decision-cutoff", help="歷史截止時間；新抓資料晚於它時拒絕補入")
    parser.add_argument("--symbols", nargs="+", help="僅補抓指定交易池股票，例如 1101.TW 2330.TW")
    parser.add_argument("--output-root", type=Path, default=ROOT / "artifacts/fundamental_repairs")
    parser.add_argument("--scheduled", action="store_true",
                        help="排程模式：只補『官方最新期別尚無去年同期文件』的股票，只寫共用資料庫，不建 Snapshot 與輸出目錄")
    parser.add_argument("--max-symbols", type=int, default=DEFAULT_SCHEDULED_MAX_SYMBOLS,
                        help="排程模式每輪最多呼叫 FinMind 的檔數（預設 %(default)s）")
    parser.add_argument("--report", type=Path, help="排程模式摘要 JSON 的寫入路徑（覆寫最新一份，不含金鑰）")
    args = parser.parse_args()
    if args.decision_cutoff is not None:
        parse_aware_time(args.decision_cutoff, "decision_cutoff")
    universe = UniverseLoader().load(args.universe)
    allowed = {item.symbol for item in universe}
    if args.symbols and not set(args.symbols) <= allowed:
        raise ValueError("指定股票不在交易池")
    database = MarketDataDatabase(args.database)
    anchors = []
    anchor_failures = []
    stored_documents = 0
    # 逐一收集每個官方損益表端點；只掛一般業（ci）時上櫃與金融業永遠沒有核對基準而被靜默跳過。
    # 單一端點失敗只記警告並換下一個，不中止整輪，否則一個端點掛掉就什麼都補不到。
    for source, url, industry in OFFICIAL_INCOME_SOURCES:
        provider = OfficialCorporateProvider(source, url, source.split("_")[0], "financial_statement",
                                             "income_statement", industry, retries=1)
        try:
            collected = CorporateDataCollector(database, [provider]).collect(universe)
        except Exception as error:  # noqa: BLE001 - 端點層級降級，原因逐一回報
            anchor_failures.append("%s：%s" % (source, error))
            continue
        stored_documents += collected.stored_documents
        if collected.failures:
            anchor_failures.extend("%s：%s" % (source, item) for item in collected.failures)
            continue
        anchors.extend(item for item in collected.records if item.document.symbol in allowed)
    if args.symbols:
        anchors = [item for item in anchors if item.document.symbol in set(args.symbols)]
    if not anchors:
        raise ValueError("官方核對基準不可用；停止補抓：" + "；".join(anchor_failures or ["沒有符合的股票"]))
    if args.scheduled:
        return run_scheduled(args, database, universe, anchors, anchor_failures)
    profiles = CorporateDataCollector(database, [OfficialCompanyProfileProvider()]).collect(universe)
    repair = CorporateDataCollector(database, [FundamentalRepairProvider(anchors, args.decision_cutoff)]).collect(universe)
    cutoff = args.decision_cutoff or datetime.now(timezone.utc).isoformat()
    snapshot = DataAgentService(database).build_snapshot(cutoff, require_prices=False,
                                                       require_universe_validation=False).as_dict()
    summary = {"run_id": repair.run_id, "decision_cutoff": cutoff,
               "official_documents": stored_documents,
               "anchor_symbols": len(anchors), "anchor_failures": anchor_failures,
               "company_profile_documents": profiles.stored_documents,
               "prior_documents": repair.stored_documents, "warnings": repair.warnings,
               "failures": list(repair.failures) + list(profiles.failures) + anchor_failures,
               "mode": "fundamental_research_only",
               "downstream_agents_run": False, "historical_backfill_permitted": False}
    output = ImmutableRunStore(args.output_root, schema_version="1.0").save(
        repair.run_id, {"snapshot": snapshot, "summary": summary})
    print(json.dumps({**summary, "output": str(output)}, ensure_ascii=False, indent=2))
    return 1 if repair.failures or repair.warnings or not repair.records else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as error:
        print("補抓停止：%s" % redact_secret(error, os.environ.get("FINMIND_TOKEN", "")), file=sys.stderr)
        raise SystemExit(1)
