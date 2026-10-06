"""全量候選標題判讀與診斷；不產生 MarketPerceptionResult 或交易方向。"""

from collections import Counter
from etf_agent.core import canonical_sha256
from .contracts import PerceptionToolError


def build_news_diagnostic(prepared: dict, labels: list) -> dict:
    if prepared.get("status") != "diagnostic_only" or prepared.get("formal_sentiment_available") is not False:
        raise PerceptionToolError("新聞候選只能產生診斷結果")
    items = {item["item_id"]: item for item in prepared["items"]}
    indexed = {}
    fields = {"item_id", "symbol", "stance", "relevance", "channel", "rationale", "model_version"}
    for label in labels:
        if set(label) != fields or label.get("item_id") not in items:
            raise PerceptionToolError("標籤欄位或項目不存在")
        item_id = label["item_id"]
        if item_id in indexed or label["symbol"] != items[item_id]["symbol"]:
            raise PerceptionToolError("標籤重複或股票誤配")
        if label["stance"] not in {"positive", "negative", "neutral", "mixed"} or label["relevance"] not in {"relevant", "ambiguous", "irrelevant"}:
            raise PerceptionToolError("新聞候選標籤選項錯誤")
        if label["channel"] not in {"news", "community", "promotion", "other"}:
            raise PerceptionToolError("新聞通道錯誤")
        if any(not isinstance(label[key], str) or not label[key].strip() for key in ("rationale", "model_version")):
            raise PerceptionToolError("標籤缺少理由或模型來源")
        indexed[item_id] = dict(label)
    if set(indexed) != set(items):
        raise PerceptionToolError("必須覆蓋全部候選，不得選擇性取樣")
    if sum(len(item["candidate_ids"]) for item in items.values()) != prepared["input_row_count"]:
        raise PerceptionToolError("候選版本覆蓋數不一致")
    summaries = []
    for symbol in sorted({item["symbol"] for item in items.values()}):
        canonical = {}
        for key, item in items.items():
            if item["symbol"] != symbol: continue
            identity = item["canonical_content_id"]
            label = indexed[key]
            if identity in canonical and any(canonical[identity][field] != label[field] for field in ("stance", "relevance", "channel")):
                raise PerceptionToolError("同內容轉載標註不一致，需核對")
            canonical[identity] = label
        selected = list(canonical.values())
        news = [label for label in selected if label["channel"] == "news" and label["relevance"] == "relevant"]
        counts = dict(Counter(label["stance"] for label in news))
        summaries.append({"symbol": symbol, "candidate_rows": sum(len(item["candidate_ids"]) for item in items.values() if item["symbol"] == symbol),
                          "unique_urls": sum(item["symbol"] == symbol for item in items.values()),
                          "unique_items": len(selected), "relevant_news_items": len(news), "headline_stance_counts": counts,
                          "channel_counts": dict(Counter(label["channel"] for label in selected)),
                          "formal_sentiment_status": "unavailable"})
    result = {"schema_version": "news-diagnostic-1.0", "status": "diagnostic_only", "decision_cutoff": prepared["decision_cutoff"],
              "prepared_sha256": canonical_sha256(prepared), "labels": [indexed[key] for key in sorted(indexed)], "items": summaries,
              "label_basis": "candidate_title_only", "formal_sentiment_available": False}
    result["content_sha256"] = canonical_sha256(result)
    return result


def render_news_diagnostic(prepared: dict, result: dict) -> str:
    if result != build_news_diagnostic(prepared, result.get("labels", [])):
        raise PerceptionToolError("標題診斷結果與重算不一致")
    rows = {row["item_id"]: row for row in prepared["items"]}
    labels = {row["item_id"]: row for row in result["labels"]}
    direction = {"positive": "正面", "negative": "負面", "neutral": "中性", "mixed": "正負並存"}
    channel = {"news": "新聞", "community": "社群評論", "promotion": "廣編／企業宣傳", "other": "事件線索"}
    lines = ["# 新聞標題情緒測試報告", "", "資料截止：%s。" % prepared["decision_cutoff"], "",
             "本次覆蓋資料庫台達電與富邦金的全部候選。逐篇判讀的是已保存標題的語氣；原頁補抓用於內容與時間核對。這是候選資料診斷，正式市場情緒仍未通過來源確認，不交給交易決策。", "",
             "## 資料處理結果", "",
             "- 原始候選：%s 筆；不同連結：%s 個。" % (prepared["input_row_count"],len(prepared["items"])),
             "- 原頁讀取成功：%s 個；失敗：%s 個。" % (sum(row["capture_status"] == "fetched" for row in rows.values()),sum(row["capture_status"] != "fetched" for row in rows.values())),
             "- 帶時區的原頁發布時間：%s 個；其餘保留未確認狀態，未猜測時區。" % sum(bool(row["page_metadata"].get("published_at")) for row in rows.values()),
             "- 原頁全文、摘要與標題分別保存；沒有全文時不宣稱取得全文。", "",
             "## 相關新聞標題分布", "", "| 股票 | 去重內容 | 相關新聞 | 正面 | 中性 | 負面 | 正負並存 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for row in result["items"]:
        counts = row["headline_stance_counts"]
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (row["symbol"], row["unique_items"], row["relevant_news_items"], *[counts.get(key,0) for key in ("positive","neutral","negative","mixed")]))
    lines.extend(["", "以上只計入相關新聞；廣編、企業宣傳、社群評論及非相關文章分開保存。同一連結重複抓取及可識別的同內容轉載不重複計數；不同文章可能仍在討論同一事件，篇數不等於獨立意見數。", "",
                  "## 逐篇核對與判讀", ""])
    for symbol in sorted({row["symbol"] for row in rows.values()}):
        lines.extend(["### " + symbol, "", "| 標題 | 分類 | 語氣 | 理由 | 原頁與時間 |", "|---|---|---|---|---|"])
        for item_id,row in rows.items():
            if row["symbol"] != symbol: continue
            label=labels[item_id]
            state="已讀取原頁" if row["capture_status"]=="fetched" else "原頁拒絕／讀取失敗"
            state += "；發布時間已核對" if row["page_metadata"].get("published_at") else "；發布時間待確認"
            title=row["title"].replace("|", "／").replace("\n", " ")
            lines.append("| [%s](%s) | %s | %s | %s | %s |" % (title,row["link"],channel[label["channel"]],direction[label["stance"]],label["rationale"],state))
    lines.extend(["", "## 尚未完成", "",
                  "- 各媒體及社群內容的使用權尚未核准，保留 unverified，沒有改成 approved。",
                  "- 部分原頁只提供不帶時區的時間，或沒有可驗證發布時間；FinMind 時間未冒充原文發布時間。",
                  "- 三個公開連結回覆拒絕存取，未繞過限制；測試仍保留並標註已存標題。",
                  "- 本次只有這兩檔、資料庫已存期間的新聞線索；不宣稱覆蓋全市場、所有投資人或所有媒體。",
                  "- 分析師目標價相關報導只作標題語氣，不當成已驗證分析師共識。", ""])
    return "\n".join(lines)
