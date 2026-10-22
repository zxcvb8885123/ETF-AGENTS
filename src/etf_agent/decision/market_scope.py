"""全市場情緒通道；個股情緒與交易行情不能代替全市場情緒。"""


def market_sentiment_channel(bundle):
    """優先重建全市場新聞結果；新聞不可用時改用恐懼貪婪指標，兩者分開保存。"""
    if "market_news_input" in bundle:
        from etf_agent.perception.market_news import validate_pair
        result = validate_pair(bundle["market_news_input"], bundle["snapshot_id"], bundle["decision_cutoff"])
        channel = {"scope": "TW_STOCK_MARKET", "snapshot_id": bundle["snapshot_id"],
                   "decision_cutoff": bundle["decision_cutoff"], "status": result["status"],
                   "outlook": result["outlook"], "findings": result["findings"],
                   "data_gaps": result["data_gaps"]}
    else:
        channel = {
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
    # 沒有恐懼貪婪輸入的舊封存維持原樣，才能逐位元重建。
    if "market_fear_greed_input" in bundle:
        from etf_agent.perception.market_fear_greed import validate_pair as validate_fear_greed
        index = validate_fear_greed(bundle["market_fear_greed_input"], bundle["snapshot_id"], bundle["decision_cutoff"])
        channel["fear_greed"] = {key: index[key] for key in (
            "score_version", "status", "score", "band", "outlook", "components", "data_gaps", "limitations")}
        if channel["status"] != "available" and index["status"] == "available":
            channel.update(status="available", outlook=index["outlook"], findings=list(index["findings"]),
                           data_gaps=[gap for gap in channel["data_gaps"] if not gap.startswith("缺少涵蓋整體台股")])
        elif index["status"] != "available":
            channel["data_gaps"] = channel["data_gaps"] + ["恐懼貪婪指標：" + gap for gap in index["data_gaps"]]
    return channel
