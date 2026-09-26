"""逐檔輸出的子 Agent 分批執行與確定性合併。

每位 Agent 讀取相同的完整輸入，只負責自己那批股票；合併時要求每批只含本批股票、
全部批次聯集剛好等於交易池，缺漏、越界或重複一律拒絕，不以合併補值。
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Sequence

from etf_agent.core import canonical_sha256


DEFAULT_BATCH_SIZE = 50


class BatchMergeError(ValueError):
    """分批輸出無法安全合併。"""


def universe_batches(symbols: Sequence[str], batch_size: int = DEFAULT_BATCH_SIZE) -> List[Dict[str, object]]:
    """依代號排序後固定切批；批次內容與雜湊可由同一交易池重算。"""
    if batch_size < 1:
        raise ValueError("batch_size 必須至少為 1")
    ordered = sorted({str(symbol).upper() for symbol in symbols})
    if len(ordered) != len(symbols):
        raise ValueError("交易池股票不得重複")
    batches = []
    for index, start in enumerate(range(0, len(ordered), batch_size)):
        members = ordered[start:start + batch_size]
        batches.append(
            {
                "batch_index": index,
                "batch_count": (len(ordered) + batch_size - 1) // batch_size,
                "symbols": members,
                "batch_sha256": canonical_sha256(members),
            }
        )
    return batches


def merge_batch_items(
    batches: Sequence[Mapping[str, object]],
    outputs: Sequence[Sequence[Mapping[str, object]]],
    label: str,
) -> List[Dict[str, object]]:
    """合併逐批 items；每批必須剛好覆蓋自己的股票，合併結果依代號排序。"""
    if len(batches) != len(outputs):
        raise BatchMergeError("%s 批次數與輸出數不一致" % label)
    merged: Dict[str, Dict[str, object]] = {}
    for batch, items in zip(batches, outputs):
        expected = set(batch["symbols"])
        seen: List[str] = []
        for item in items:
            symbol = str(item.get("symbol", "")).upper()
            if symbol not in expected:
                raise BatchMergeError("%s 第 %d 批含非本批股票：%s" % (label, batch["batch_index"], symbol))
            if symbol in merged or symbol in seen:
                raise BatchMergeError("%s 股票重複：%s" % (label, symbol))
            seen.append(symbol)
            merged[symbol] = dict(item)
        missing = sorted(expected - set(seen))
        if missing:
            raise BatchMergeError("%s 第 %d 批未覆蓋：%s" % (label, batch["batch_index"], ", ".join(missing)))
    return [merged[symbol] for symbol in sorted(merged)]
