"""持股權重事實（``position_sizing.held_weight_budget`` v2，docs/plans/agent_autonomy_plan.md 階段 4，只由人設定）。

``held_weight_budget`` 的預算算法不變：已持有的加碼候選若現有權重已高於信心份額，目標權重就是現有權重、不建買單（RISK_HELD_WEIGHT）。
問題是交易 Agent 不知道這件事。本模組讓交易 Agent 的簡報附上每檔持股的事實，**只提示、不改預算**：

- ``current_weight``：本次決策的持股權重（與配置引擎相同的 NAV 口徑，``exposure.holding_weights``）。
- ``weight_limit``／``buy_cap_weight``：單股上限與買進當日的封頂（``stock_limit_headroom`` 啟用時 0.97、否則 0.995）。
- ``high_min_weight``／``at_or_above_high_min``：只有 ``limit_scaled_conviction`` 啟用時才有；high 的份額至少為 α × 單股上限
  （不超過封頂），現有權重已不低於它時，加碼不會比現狀更重——真正的份額仍由本次全部 buy／add 的信心、預算與廣度決定。

**限制（明說，不假裝精確）**：交易 Agent 在風險 Agent 之前執行，現金緩衝、持股廣度與其他候選的信心都還不知道，所以
medium／low 的份額與 high 的實際份額在簡報時算不出來；本模組只給事實與「high 至少」的下限，不預估份額。

事實只讀 bundle 與 base／final Policy 中相同的欄位（max_stock_weight、special_weight_limits、stock_limit_headroom、
limit_scaled_conviction），不讀任何 Agent 輸出；整份另存為 ``team_inputs.held_weight_facts``（選配鍵，不升版本），Finalizer 整份重算比對。
背景、``citable=false``、沒有 evidence_id。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Mapping, Optional

from etf_agent.core import canonical_sha256

from .contracts import DecisionToolError, decimal_value, reject_unknown_fields
from .exposure import holding_weights
from .sizing import (
    HELD_WEIGHT_BUDGET_V2, LIMIT_HEADROOM, LIMIT_HEADROOM_BUY, STOCK_LIMIT_HEADROOM, held_weight_facts_gate, limit_scaled_alpha,
)

HELD_WEIGHT_FACTS_KEY = "held_weight_facts"  # team_inputs 的選配鍵與簡報區塊鍵：與 Policy 閘門雙向同時存在
HELD_WEIGHT_FACTS_SCHEMA_VERSION = "1.0"
QUANTUM = Decimal("0.000001")
FACTS_FIELDS = {"schema_version", "version", "facts_id", "as_of", "note", "holdings"}
FACTS_NOTE = (
    "以下是程式算的持股權重事實（背景、沒有 evidence_id、不可引用）：current_weight 是持股現有權重；"
    "add 的目標權重是 max(現有權重, 信心份額)，現有權重已不低於份額時加碼不會成交（程式不建買單、留 RISK_HELD_WEIGHT 旗標）。"
    "信心份額由本次全部 buy／add 的信心、現金緩衝與持股廣度決定，簡報時算不出來；high_min_weight 只是 high 份額的下限"
    "（limit_scaled_conviction 啟用才有），at_or_above_high_min 為 true 表示即使給 high，加碼也未必比現狀更重。"
    "這些事實不改變預算、不改變意圖的驗證；要不要加碼仍由你依多空論點決定。"
)


def _q(value: Decimal) -> str:
    return str(value.quantize(QUANTUM, rounding=ROUND_HALF_UP))


def build_held_weight_facts(bundle: Mapping[str, object], policy: Mapping[str, object]) -> Dict[str, object]:
    """逐檔持股的權重事實；沒有 held_weight_budget=v2 或缺資料時拒絕（不補值）。"""
    sizing = policy.get("position_sizing")
    if not held_weight_facts_gate(sizing):
        raise DecisionToolError("DecisionPolicy 沒有 position_sizing.held_weight_budget=%s，不應建立持股權重事實" % HELD_WEIGHT_BUDGET_V2)
    weights, _, _ = holding_weights(bundle)
    alpha = limit_scaled_alpha(sizing)  # type: ignore[arg-type]
    headroom = LIMIT_HEADROOM_BUY if sizing.get("stock_limit_headroom") == STOCK_LIMIT_HEADROOM else LIMIT_HEADROOM  # type: ignore[union-attr]
    default_limit = decimal_value(policy["max_stock_weight"], "DecisionPolicy.max_stock_weight")
    special = policy.get("special_weight_limits") or {}
    holdings: List[Dict[str, object]] = []
    for symbol in sorted(weights):
        limit = decimal_value(special[symbol], "special_weight_limits.%s" % symbol) if symbol in special else default_limit
        cap = limit * headroom
        floor: Optional[Decimal] = None if alpha is None else min(alpha * limit, cap)
        holdings.append({
            "symbol": symbol,
            "current_weight": _q(weights[symbol]),
            "weight_limit": _q(limit),
            "buy_cap_weight": _q(cap),
            "high_min_weight": None if floor is None else _q(floor),
            "at_or_above_high_min": None if floor is None else weights[symbol] >= floor,
        })
    body: Dict[str, object] = {
        "schema_version": HELD_WEIGHT_FACTS_SCHEMA_VERSION,
        "version": HELD_WEIGHT_BUDGET_V2,
        "as_of": str(bundle["decision_cutoff"]),
        "note": FACTS_NOTE,
        "holdings": holdings,
    }
    body["facts_id"] = "held-weight-facts:" + canonical_sha256(body)[:20]
    return body


def validate_held_weight_facts(
    bundle: Mapping[str, object], policy: Mapping[str, object], team: Mapping[str, object], errors: List[str],
) -> None:
    """以 bundle 與 Policy 整份重算 ``team_inputs.held_weight_facts``；缺少、多餘欄位或不一致即拒絕。"""
    prefix = "team_inputs.held_weight_facts"
    facts = team.get(HELD_WEIGHT_FACTS_KEY)
    if not isinstance(facts, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    if not held_weight_facts_gate(policy.get("position_sizing")):
        errors.append("%s 只能用於啟用 position_sizing.held_weight_budget=v2 的 Policy" % prefix)
        return
    reject_unknown_fields(facts, FACTS_FIELDS, prefix, errors)
    try:
        expected = build_held_weight_facts(bundle, policy)
    except (DecisionToolError, KeyError, TypeError, ValueError) as error:
        errors.append("%s 無法以共同輸入重算：%s" % (prefix, error))
        return
    if dict(facts) != expected:
        errors.append("%s 與重算結果不同，拒絕改寫" % prefix)


def held_weight_brief(facts: Mapping[str, object]) -> Dict[str, object]:
    """交易 Agent 簡報：頂層 ``background``（說明與時間，``citable=false``）與逐檔 ``by_symbol``。"""
    return {
        "background": {"citable": False, "note": facts["note"], "as_of": facts["as_of"]},
        "by_symbol": {
            str(row["symbol"]): {key: row[key] for key in row if key != "symbol"}
            for row in facts["holdings"]  # type: ignore[union-attr]
        },
    }


__all__ = [
    "HELD_WEIGHT_FACTS_KEY", "build_held_weight_facts", "held_weight_brief", "validate_held_weight_facts",
]
