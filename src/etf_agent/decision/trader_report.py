"""以已驗證 TradeDecision 呈現交易整合結果，不重貼多空報告或論點全文。"""

from datetime import timedelta, timezone
import re
from typing import Mapping

from etf_agent.core import parse_aware_time

from .contracts import DecisionToolError
from .trader import TradeDecisionValidator


INTENT_LABELS = {
    "buy": "買進", "add": "加碼", "hold": "續抱", "trim": "減碼",
    "exit": "出場", "forced_exit": "受規則要求出場", "no_trade": "暫不交易",
}
CONVICTION_LABELS = {"high": "高", "medium": "中", "low": "低", None: "不適用"}
GRADE_LABELS = {
    "strong": "強", "moderate": "中", "weak": "弱", "none": "無",
    "high": "高", "medium": "中", "low": "低",
}


def _readable_text(value: str, claim_roles: Mapping[str, str]) -> str:
    """只處理識別碼、等級用字與標點，不改寫理由或數值。"""
    text = value
    for claim_id in sorted(claim_roles, key=len, reverse=True):
        token = re.escape(claim_id) + r"(?![A-Za-z0-9_-])"
        # 括號中的 ID 是索引；獨立作主詞的 ID 則保留兩方歸屬。
        text = re.sub(r"[（(]\s*" + token + r"\s*[）)]", "", text)
        text = re.sub(r"[,，、]\s*" + token + r"\s*(?=[）)])", "", text)
        label = "多頭研究" if claim_roles[claim_id] == "bull" else "風險分析"
        text = re.sub(token, label, text)
    text = re.sub(r"\b(strong|moderate|weak|none|high|medium|low)\b", lambda m: GRADE_LABELS[m[0]], text)
    text = re.sub(r"(?<!\d),(?!\d)", "，", text)
    text = re.sub(r"(?<!\d):(?![/\d])", "：", text)
    text = text.replace(";", "；").replace("?", "？").replace("(", "（").replace(")", "）")
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return text.strip()


def _paragraphs(text: str) -> str:
    """依完整句分段；不截斷句子、不摘要或刪除條件。"""
    sentences = re.split(r"(?<=[。！？])\s*", text)
    paragraphs = []
    current = []
    for sentence in sentences:
        if not sentence:
            continue
        if current and (len(current) >= 3 or len("".join(current)) + len(sentence) > 160):
            paragraphs.append("".join(current))
            current = []
        current.append(sentence)
    if current:
        paragraphs.append("".join(current))
    return "\n\n".join(paragraphs)


def render_trader_report(
    bundle: Mapping[str, object], momentum: Mapping[str, object],
    debate: Mapping[str, object], trade: Mapping[str, object],
) -> str:
    """保留原判斷，只將識別碼、等級與長段落轉成閱讀版。"""
    errors = TradeDecisionValidator(bundle, momentum, debate).validate(trade)
    if errors:
        raise DecisionToolError("交易整合結果驗證失敗：" + "；".join(errors))
    cutoff = parse_aware_time(bundle["decision_cutoff"], "decision_cutoff", error=DecisionToolError)
    cutoff_text = cutoff.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    claim_roles = {
        claim["claim_id"]: packet["role"]
        for packet in debate["packets"] for item in packet["items"] for claim in item["claims"]
    }
    lines = [
        "# 交易整合結果", "",
        "資料截止：%s（台北時間）。" % cutoff_text, "",
        "以下為交易 Agent 的意圖；是否成為正式候選，仍須下游風險、配置與 Guard 驗證。", "",
    ]
    for item in trade["items"]:
        lines.extend([
            "## %s" % item["symbol"], "",
            "**結論：%s｜買進信心：%s**" % (
                INTENT_LABELS[item["intent"]], CONVICTION_LABELS[item.get("conviction")],
            ), "", "**為什麼這樣決定：**", "",
            _paragraphs(_readable_text(item["rationale"], claim_roles)), "",
        ])
        for field, label in (
            ("unresolved_questions", "還需要確認"),
            ("invalidation_conditions", "何時需要重估"),
        ):
            lines.extend(["**%s：**" % label, ""])
            lines.extend(["- %s" % _readable_text(text, claim_roles) for text in item[field]] or ["無。"])
            lines.append("")
    return "\n".join(lines) + "\n"
