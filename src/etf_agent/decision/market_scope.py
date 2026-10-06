"""全市場情緒通道；個股情緒與交易行情不能代替全市場情緒。"""


def market_sentiment_channel(bundle):
    """優先重建全市場新聞結果，缺少成對輸入才降級。"""
    if "market_news_input" in bundle:
        from etf_agent.perception.market_news import validate_pair
        result = validate_pair(bundle["market_news_input"], bundle["snapshot_id"], bundle["decision_cutoff"])
        return {"scope": "TW_STOCK_MARKET", "snapshot_id": bundle["snapshot_id"],
                "decision_cutoff": bundle["decision_cutoff"], "status": result["status"],
                "outlook": result["outlook"], "findings": result["findings"],
                "data_gaps": result["data_gaps"]}
    return {
        "scope": "TW_STOCK_MARKET",
        "snapshot_id": bundle["snapshot_id"],
        "decision_cutoff": bundle["decision_cutoff"],
        "status": "unavailable",
        "outlook": "unknown",
        "findings": [],
        "data_gaps": [
            "缺少涵蓋整體台股、通過來源使用與發布時間核對的新聞或投資人情緒資料。",
            "現有個股新聞只代表該公司；大盤指數、成交金額及法人買賣只能作交易背景，不能直接補成情緒結論。",
        ],
    }
