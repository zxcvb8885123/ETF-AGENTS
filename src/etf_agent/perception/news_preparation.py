"""新聞候選內容與時間核對；不自動核准來源，不建立正式市場情緒。"""

import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

from etf_agent.core import canonical_sha256, parse_aware_time
from .contracts import PerceptionToolError


class NewsMetadataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.metadata, self.scripts, self.parts = {}, [], []
        self.in_json = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            if key and attrs.get("content"):
                self.metadata.setdefault(key.lower(), attrs["content"])
        if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self.in_json, self.parts = True, []

    def handle_data(self, data):
        if self.in_json:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.in_json:
            try:
                self.scripts.append(json.loads("".join(self.parts)))
            except ValueError:
                pass
            self.in_json = False


def extract_news_metadata(html: str) -> dict:
    parser = NewsMetadataParser()
    parser.feed(html)
    articles = []
    def visit(value):
        if isinstance(value, list):
            for item in value: visit(item)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if isinstance(types, list) and any(t in {"NewsArticle", "Article", "ReportageNewsArticle"} for t in types):
                articles.append(value)
            for item in value.values():
                if isinstance(item, (list, dict)): visit(item)
    for script in parser.scripts: visit(script)
    article = next((a for a in articles if isinstance(a.get("articleBody"), str)), articles[0] if articles else {})
    published = article.get("datePublished") or parser.metadata.get("article:published_time")
    normalized, issue = None, "原頁未提供帶時區的發布時間"
    if published:
        try:
            normalized = parse_aware_time(published, "published_at", error=PerceptionToolError).isoformat()
            issue = None
        except PerceptionToolError:
            issue = "原頁發布時間無法確認時區，保留原字串"
    body = article.get("articleBody")
    description = article.get("description") or parser.metadata.get("og:description") or parser.metadata.get("description")
    return {"published_at": normalized, "published_at_raw": published, "time_issue": issue,
            "headline": article.get("headline") or parser.metadata.get("og:title"),
            "article_body": body if isinstance(body, str) else None,
            "description": description if isinstance(description, str) else None}


def prepare_news_candidates(rows: list, captures: list, decision_cutoff: str) -> dict:
    cutoff = parse_aware_time(decision_cutoff, "decision_cutoff", error=PerceptionToolError)
    pages = {capture["url"]: capture for capture in captures}
    grouped = {}
    for row in rows:
        available = parse_aware_time(row["available_at"], "available_at", error=PerceptionToolError)
        if available > cutoff:
            raise PerceptionToolError("新聞候選晚於截止時間")
        parsed = urlsplit(row["link"])
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise PerceptionToolError("新聞連結格式無效")
        url = urlunsplit(parsed._replace(fragment=""))
        key = (row["stock_id"], url)
        grouped.setdefault(key, []).append(row)
    items = []
    for (stock, url), versions in sorted(grouped.items()):
        versions.sort(key=lambda row: (parse_aware_time(row["available_at"], "available_at"), row["id"]))
        row, page = versions[-1], pages.get(url, {})
        flags = ["來源使用權待確認，禁止正式情緒計算"]
        metadata = {}
        if page.get("status") == "fetched":
            observed = parse_aware_time(page["observed_at"], "observed_at", error=PerceptionToolError)
            if observed > cutoff:
                raise PerceptionToolError("補抓原頁晚於截止時間")
            if page.get("html_sha256") != canonical_sha256(page.get("html")):
                raise PerceptionToolError("原頁內容雜湊不一致")
            metadata = extract_news_metadata(page["html"])
            if metadata["published_at"] and parse_aware_time(metadata["published_at"], "published_at") > observed:
                flags.append("原頁發布時間晚於擷取時間，排除補抓內容")
                metadata = {}
            elif metadata.get("time_issue"):
                flags.append(metadata["time_issue"])
        else:
            flags.append("原頁擷取失敗或未擷取，僅能檢查已存標題")
        description = metadata.get("article_body") or metadata.get("description") or ""
        normalized_title = re.sub(r"\s+", "", row["title"].split(" - ")[0])
        # 同內容的跨站轉載亦去重；短的樣板摘要不能作內容識別。
        if len(description) >= 120:
            content_id = canonical_sha256({"stock": stock, "text": re.sub(r"\s+", "", description)})
        elif len(normalized_title) >= 20:
            content_id = canonical_sha256({"stock": stock, "headline": normalized_title, "source_day_raw": row["source_time_raw"][:10]})
        else:
            content_id = canonical_sha256({"link": url})
        items.append({"item_id": "news-candidate-" + str(row["id"]), "symbol": stock + ".TW",
                      "title": row["title"], "link": url, "publisher": row["publisher"],
                      "candidate_ids": [r["id"] for r in versions],
                      "candidate_content_hashes": [r["content_sha256"] for r in versions],
                      "canonical_content_id": content_id,
                      "source_time_raw": row["source_time_raw"], "candidate_available_at": row["available_at"],
                      "page_observed_at": page.get("observed_at"), "capture_status": page.get("status", "not_attempted"),
                      "page_metadata": metadata, "license_status": "unverified", "quality_flags": flags})
    return {"schema_version": "news-candidate-preparation-1.0", "decision_cutoff": decision_cutoff,
            "status": "diagnostic_only", "input_row_count": len(rows), "items": items,
            "formal_sentiment_available": False}
