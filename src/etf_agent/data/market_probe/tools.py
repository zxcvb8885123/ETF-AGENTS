"""從原始回應重建大盤背景事實，不計算情緒。"""

import json
from datetime import date
from zoneinfo import ZoneInfo
from etf_agent.core import canonical_sha256, parse_aware_time, parse_decimal, decimal_string
from etf_agent.data.twse import parse_roc_date
from .contracts import SOURCES, MarketProbeError


def market_context_facts(captures, decision_cutoff):
    cutoff = parse_aware_time(decision_cutoff, "資料截止", error=MarketProbeError)
    latest_day = cutoff.astimezone(ZoneInfo("Asia/Taipei")).date().isoformat()
    sources, facts, failures = set(), [], []
    for capture in captures:
        source = capture["source"]
        if source not in SOURCES or source in sources:
            raise MarketProbeError("市場來源不存在或重複")
        sources.add(source)
        expected_url = SOURCES[source].format(date=date.fromisoformat(capture["requested_trade_date"]).strftime("%Y%m%d"))
        if capture["url"] != expected_url:
            raise MarketProbeError("市場來源網址不符")
        if capture["requested_trade_date"] > latest_day:
            raise MarketProbeError("查詢日期晚於資料截止")
        if parse_aware_time(capture["available_at"], "資料取得時間", error=MarketProbeError) > cutoff:
            raise MarketProbeError("市場資料取得時間晚於截止")
        if capture["raw_sha256"] != canonical_sha256(capture["raw_text"]):
            raise MarketProbeError("市場原始回應遭修改")
        if capture["status"] != "fetched":
            failures.append({"source": source, "reason": capture["error"]})
            continue
        payload = json.loads(capture["raw_text"])
        try:
            if source == "TWSE_FMTQIK":
                if not isinstance(payload, list) or not payload:
                    raise MarketProbeError("大盤成交回應沒有資料")
                for row in payload:
                    day = parse_roc_date(row["Date"])
                    values = {"加權指數": row["TAIEX"], "漲跌點數": row["Change"], "成交金額（元）": row["TradeValue"]}
                    _append(facts, source, capture, day, values, latest_day)
            elif source == "TWSE_MI_INDEX":
                if not isinstance(payload, list) or not payload:
                    raise MarketProbeError("指數回應沒有資料")
                for row in payload:
                    if row["指數"] not in {"發行量加權股價指數", "寶島股價指數", "金融保險類指數", "臺灣中型100指數", "小型股300指數"}:
                        continue
                    day = parse_roc_date(row["日期"])
                    _append(facts, source, capture, day, {row["指數"]: row["收盤指數"], row["指數"]+"漲跌百分比": row["漲跌百分比"]}, latest_day)
            else:
                if not isinstance(payload, dict) or payload.get("stat") != "OK" or not payload.get("data"):
                    raise MarketProbeError("法人買賣回應沒有可用資料")
                day = parse_roc_date(payload["date"])
                for row in payload["data"]:
                    if len(row) != 4:
                        raise MarketProbeError("法人欄位變更，需核對")
                    _append(facts, source, capture, day, {row[0]+"買進金額（元）": row[1], row[0]+"賣出金額（元）": row[2], row[0]+"買賣差額（元）": row[3]}, latest_day)
        except (KeyError, TypeError, ValueError) as exc:
            # 一個來源有格式問題時整份拒絕，不留下部分解析數值。
            facts = [fact for fact in facts if fact["source"] != source]
            failures.append({"source": source, "reason": str(exc)})
    result = {"schema_version": "market-context-probe-1.0", "decision_cutoff": decision_cutoff,
              "status": "diagnostic_only", "formal_sentiment_available": False,
              "facts": facts, "failures": failures, "captures_sha256": canonical_sha256(captures),
              "limitations": ["交易事實不等於投資人情緒；未產生市場方向。", "未將查核來源改為正式情緒已核准來源。", "本次為少量大盤資料補抓，不是完整歷史市場視窗。"]}
    result["content_sha256"] = canonical_sha256(result)
    return result


def _append(facts, source, capture, day, values, latest_day):
    if day > latest_day:
        raise MarketProbeError("資料內容日期晚於截止")
    parsed = {name: decimal_string(parse_decimal(str(value).replace(",", ""), name, error=MarketProbeError)) for name, value in values.items()}
    facts.append({"source": source, "trade_date": day, "values": parsed,
                  "evidence_id": source+":"+canonical_sha256({"raw": capture["raw_sha256"], "day": day, "values": parsed})[:20],
                  "available_at": capture["available_at"], "raw_sha256": capture["raw_sha256"]})
