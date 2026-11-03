"""月營收文件的內容識別。

官方月營收 OpenAPI 每日重新產製整份資料，列內「出表日期」（TPEx 為 ``Date``）是資料
產製日，不是公司公告時間；同一月份、同一組營收數字會因為出表日期每天變動而整列不同。
若內容雜湊含整列，相同營收就會每天多一個版本，證據的 ``content_as_of`` 也會隨之漂移。

這裡提供排除產製欄位後的比較用內容與雜湊，供收集器判斷「數字是否真的改變」。只影響
之後新抓取的判斷，不改寫資料庫既有資料列。
"""

import json
from typing import FrozenSet

from etf_agent.core import canonical_json, canonical_sha256


# 來源每日重新產製時會變動、但與營收事實無關的欄位（TWSE 為「出表日期」，TPEx 為「Date」）。
MONTHLY_REVENUE_PRODUCTION_FIELDS: FrozenSet[str] = frozenset({"出表日期", "Date"})


def comparable_monthly_revenue_body(body: str) -> str:
    """回傳去掉產製欄位後的 canonical JSON，供版本比較。

    舊版本的 body 可能是不同 separators 或鍵序的 JSON（canonical 規則曾變更），重新
    canonical 化後格式差異也不會再被當成內容變動。body 不是 JSON 物件時原樣回傳，不猜測。
    """
    try:
        row = json.loads(body)
    except ValueError:
        return body
    if not isinstance(row, dict):
        return body
    return canonical_json(
        {key: value for key, value in row.items() if key not in MONTHLY_REVENUE_PRODUCTION_FIELDS}
    )


def monthly_revenue_content_sha256(document_type: str, title: str, body: str) -> str:
    """月營收文件的內容雜湊：只含文件類型、標題與去掉產製欄位的 body。

    刻意不含 ``published_at``（首次觀測後固定沿用）與 symbol（由 source 與 external_id 決定），
    使相同數字在不同出表日期下得到相同雜湊；數字、備註、公司名稱等真的改變才不同。
    舊資料列的雜湊是以含整列的舊公式算的，比較時由呼叫端對舊列重算本雜湊，不要求一致。
    """
    return canonical_sha256(
        {
            "document_type": document_type,
            "title": title,
            "body": comparable_monthly_revenue_body(body),
        }
    )
