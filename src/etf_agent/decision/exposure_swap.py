"""Agent 主導曝險階段 2、3：換股提名的 ``purpose: exposure`` 配對（``position_sizing.agent_swap.exposure`` 版本閘門）。

設計見 ``docs/plans/agent_led_exposure_plan.md`` 第 3.2～3.4、4.1、6 節。閘門只由人設定、策略反思不得改（``agent_swap`` 前綴已在
``FORBIDDEN_PREFIXES``）；沒有 ``exposure`` 欄位的舊 Policy／舊提名封存／舊 run 逐位元維持原行為。

- **v1（階段 2，影子）**：提名每對多一個列舉 ``purpose``（``thesis``／``exposure``），輸出多一份 ``lower_conviction_review``；
  ``purpose: exposure`` 的換出依據是程式算的曝險事實編號 ``review:<代號>:exposure``（取自已由 Finalizer 重算的
  ``team_inputs.exposure_facts``，附該股行情 evidence_id），不要求第一輪已採納；提名「高信心持股」讓位前必須逐檔交代
  中、低信心持股（``lower_conviction_review``），且高信心配對排在所有中、低信心曝險配對之後。**配置完全不讀 ``exposure`` 配對。**
- **v2（階段 3，正式）**：配置依風險 Agent 的 ``exposure_view`` 取用 ``exposure`` 配對。``keep_defensive`` 時一對都不成交、
  換出者不賣；``start_rebuild``／``rebuild`` 時最多取用 ``max_pairs_by_view`` 對映的對數（人設的硬上限，不得高於
  ``agent_swap.max_swaps_per_run``，且 ``start_rebuild`` 不得高於 ``rebuild``），超過從清單尾端取消。
  ``thesis`` 配對不受 ``exposure_view`` 影響。程式不挑人、不排序、不補挑；``exposure_view`` 由 ``apply_trade_decision`` 綁進 Policy，
  Finalizer 以 team_inputs 的現金姿態重算比對。

程式只驗結構（有沒有交代、順序、編號對不對），不判斷理由好不好；換手、現金、單股、產業、檔數、持有天數、冷卻、一換一與
減碼日略過都由既有配置步驟處理，不放寬。
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .exposure_facts import EXPOSURE_VIEWS, EXPOSURE_VIEWS_WITH_PROBE
from .fields import DecisionToolError, reject_unknown_fields
from .quality_dip_swap import nomination_purposes

EXPOSURE_SWAP_V1 = "v1"  # 階段 2：影子（配置不讀 exposure 配對）
EXPOSURE_SWAP_V2 = "v2"  # 階段 3：正式（配置依 exposure_view 取用 exposure 配對）
EXPOSURE_SWAP_VERSIONS = (EXPOSURE_SWAP_V1, EXPOSURE_SWAP_V2)
EXPOSURE_SWAP_FIELDS = {"version", "max_pairs_by_view", "view"}
REBUILD_VIEWS = ("start_rebuild", "rebuild")
OFFENSE_CAP_VIEWS = ("probe",) + REBUILD_VIEWS  # position_sizing.offense 啟用時 max_pairs_by_view 的鍵（多一級 probe）
KEEP_DEFENSIVE = "keep_defensive"
PURPOSE_THESIS = "thesis"
PURPOSE_EXPOSURE = "exposure"
PURPOSES = (PURPOSE_THESIS, PURPOSE_EXPOSURE)
LOWER_REVIEW_RESPONSES = ("nominated", "kept")
LOWER_REVIEW_FIELDS = {"symbol", "response", "reason"}
LOWER_CONVICTIONS = ("medium", "low")
HIGH_CONVICTION = "high"
EXPOSURE_BASIS_KIND = "exposure"
EXPOSURE_BASIS_SUFFIX = ":exposure"
EXPOSURE_BASIS_NOTE = (
    "曝險事實編號 review:<代號>:exposure 是程式算的該股曝險事實（權重、beta、上行／下行捕捉、最近一個交易池上漲日該股與交易池的報酬），"
    "附該股行情的 evidence_id；只能放進 purpose=exposure 那一對的 sell_basis_ids，不可放進 purpose=thesis 或任何 buy_basis_ids，"
    "也不要求第一輪已採納。組合層級的數字（exposure_facts 區塊）仍是背景、不可引用。"
)
EXPOSURE_SWAP_NOTE = (
    "purpose=exposure 的配對：為了提高市場參與度讓出名額，是否取用由風險 Agent 的 exposure_view 與程式的硬性界線決定，"
    "你只提名「若要回補，依序用哪幾對」。每一對的 reason 必須寫明為什麼是這檔讓位（而不是別檔），以及放掉它的代價。"
    "若要提名第一輪信心為 high 的持股，必須先在 lower_conviction_review 逐檔交代清單中的 medium／low 持股"
    "（nominated＝已在本次清單以 exposure 配對讓位、kept＝不讓位且附一句理由），且高信心持股的 exposure 配對必須排在"
    "所有 medium／low 持股的 exposure 配對之後；沒有提名高信心持股時 lower_conviction_review 必須是空陣列。"
    "purpose=thesis 的配對規則不變：需要換出股已採納的空頭論點或持股檢視事實。"
)


def exposure_swap_gate(sizing: object) -> Optional[Mapping[str, object]]:
    """``position_sizing.agent_swap.exposure`` 閘門；沒有（或 sizing／agent_swap 不是物件）時回 None。"""
    swap = sizing.get("agent_swap") if isinstance(sizing, Mapping) else None
    gate = swap.get("exposure") if isinstance(swap, Mapping) else None
    return gate if isinstance(gate, Mapping) else None


def exposure_basis_id(symbol: str) -> str:
    return "review:%s%s" % (str(symbol).upper(), EXPOSURE_BASIS_SUFFIX)


def is_exposure_basis_id(identifier: object) -> bool:
    return isinstance(identifier, str) and identifier.startswith("review:") and identifier.endswith(EXPOSURE_BASIS_SUFFIX)


# ---------------------------------------------------------------- 閘門

def validate_exposure_swap_gate(sizing: Mapping[str, object], errors: List[str]) -> None:
    """形狀、值域與版本相依。前提（agent_swap、exposure_view 同時存在）由 GATE_PREREQUISITES 檢查。

    v1：只有 version；v2：另需 ``max_pairs_by_view``（start_rebuild／rebuild 各一個 0～agent_swap.max_swaps_per_run 的整數，
    start_rebuild 不得高於 rebuild），且 agent_swap 必須是 v2（配置才讀提名）。``view`` 由 ``apply_trade_decision`` 綁定，只用於 v2。"""
    swap = sizing["agent_swap"]
    gate = swap["exposure"]
    prefix = "position_sizing.agent_swap.exposure"
    if not isinstance(gate, Mapping):
        errors.append("%s 必須是物件" % prefix)
        return
    reject_unknown_fields(gate, EXPOSURE_SWAP_FIELDS, prefix, errors)
    version = gate.get("version")
    if version not in EXPOSURE_SWAP_VERSIONS:
        errors.append("%s.version 只允許 %s" % (prefix, "／".join(EXPOSURE_SWAP_VERSIONS)))
        return
    if version == EXPOSURE_SWAP_V1:
        for field in ("max_pairs_by_view", "view"):
            if field in gate:
                errors.append("%s.%s 只用於 version=v2（v1 影子配置不讀曝險配對）" % (prefix, field))
        return
    if swap.get("version") != "v2":
        errors.append("%s.version=v2 只允許用於 agent_swap.version=v2（配置才讀提名）" % prefix)
    caps = gate.get("max_pairs_by_view")
    limit = swap.get("max_swaps_per_run")
    # position_sizing.offense 啟用時 max_pairs_by_view 多一級 probe（probe 不得高於 start_rebuild）；沒有 offense 時鍵集與舊版完全相同。
    offense = isinstance(sizing.get("offense"), Mapping)
    cap_views = OFFENSE_CAP_VIEWS if offense else REBUILD_VIEWS
    if not isinstance(caps, Mapping) or set(caps) != set(cap_views):
        errors.append("%s.max_pairs_by_view 必須剛好有 %s" % (prefix, "、".join(cap_views)))
    else:
        valid = True
        for view in cap_views:
            value = caps[view]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or (
                isinstance(limit, int) and not isinstance(limit, bool) and value > limit
            ):
                errors.append("%s.max_pairs_by_view.%s 必須是 0～agent_swap.max_swaps_per_run 的整數（只決定誰先、不放寬現行上限）" % (prefix, view))
                valid = False
        if valid and caps["start_rebuild"] > caps["rebuild"]:
            errors.append("%s.max_pairs_by_view.start_rebuild 不得高於 rebuild" % prefix)
        if valid and offense and caps["probe"] > caps["start_rebuild"]:
            errors.append("%s.max_pairs_by_view.probe 不得高於 start_rebuild" % prefix)
    domain = EXPOSURE_VIEWS_WITH_PROBE if offense else EXPOSURE_VIEWS
    if "view" in gate and gate["view"] not in domain:
        errors.append("%s.view 只允許 %s（只能由 pipeline 綁定風險 Agent 的 exposure_view；probe 只在 Policy 有 offense 時允許）" % (prefix, "／".join(domain)))


def exposure_swap_binding(sizing: object) -> Optional[Dict[str, object]]:
    """配置用的曝險配對綁定。沒有閘門或 agent_swap 不是 v2（配置不讀提名）回 None。

    回傳 ``{"version", "purposes", "view", "caps"}``：``purposes`` 與 ``nominated_pairs`` 同序；v1（影子）的 ``view``／``caps`` 為
    None（配置略過全部 exposure 配對）；v2 的 ``view`` 必須已綁定，否則拋錯，不靜默當作 keep_defensive。"""
    gate = exposure_swap_gate(sizing)
    swap = sizing.get("agent_swap") if isinstance(sizing, Mapping) else None
    if gate is None or not isinstance(swap, Mapping) or swap.get("version") != "v2":
        return None
    pairs = swap.get("nominated_pairs")
    if not isinstance(pairs, list):
        raise DecisionToolError("DecisionPolicy 啟用 agent_swap v2，但尚未綁定換股提名（nominated_pairs／nomination_ref）")
    purposes = [str(pair.get("purpose")) for pair in pairs]
    allowed = nomination_purposes(sizing)  # agent_swap.quality_dip 啟用時多 quality_dip；沒有時就是 PURPOSES
    if any(purpose not in allowed for purpose in purposes):
        raise DecisionToolError("agent_swap.exposure 啟用時 nominated_pairs 每對必須有 purpose（%s）" % "／".join(allowed))
    if gate.get("version") == EXPOSURE_SWAP_V1:
        return {"version": EXPOSURE_SWAP_V1, "purposes": purposes, "view": None, "caps": None}
    view = gate.get("view")
    if view not in EXPOSURE_VIEWS_WITH_PROBE:
        raise DecisionToolError("DecisionPolicy 啟用 agent_swap.exposure v2，但尚未綁定風險 Agent 的 exposure_view")
    caps = {name: int(value) for name, value in dict(gate["max_pairs_by_view"]).items()}  # type: ignore[arg-type]
    return {"version": EXPOSURE_SWAP_V2, "purposes": purposes, "view": str(view), "caps": caps}


def bind_exposure_view(
    sizing: Mapping[str, object], cash_stance: Mapping[str, object], effective_view: Optional[str] = None,
) -> Dict[str, object]:
    """``apply_trade_decision`` 用：v2 把風險 Agent 的 ``exposure_view`` 綁進 ``agent_swap.exposure.view``；回傳新的 agent_swap 物件。

    v1（影子）與沒有閘門回傳原物件的拷貝，不綁定。``effective_view``（position_sizing.offense 啟用）是程式壓回後的有效等級（可為 probe），
    綁進 view 取代風險 Agent 的原始列舉；沒有 offense 時為 None，行為與舊版相同。"""
    swap = dict(sizing["agent_swap"])  # type: ignore[arg-type]
    gate = exposure_swap_gate(sizing)
    if gate is None or gate.get("version") != EXPOSURE_SWAP_V2:
        return swap
    view = cash_stance.get("exposure_view") if effective_view is None else effective_view
    domain = EXPOSURE_VIEWS if effective_view is None else EXPOSURE_VIEWS_WITH_PROBE
    if view not in domain:
        raise DecisionToolError("Policy 啟用 agent_swap.exposure v2，現金姿態必須給 exposure_view（%s）" % "／".join(domain))
    swap["exposure"] = {**dict(gate), "view": view}
    return swap


# ---------------------------------------------------------------- 曝險事實編號

def exposure_basis_by_symbol(bundle: Mapping[str, object], facts: Mapping[str, object]) -> Dict[str, Dict[str, object]]:
    """每檔持股的曝險事實（可引用）：只讀已由 Finalizer 整份重算的 ``team_inputs.exposure_facts`` 與 bundle 的行情 evidence_id。

    缺該股行情 evidence_id 的持股沒有曝險事實（不補值、不能以曝險理由提名）。"""
    prices = {
        str(row["symbol"]).upper(): row.get("source_evidence_id")
        for row in bundle["snapshot"]["latest_prices"]  # type: ignore[index]
    }
    up_day = facts.get("up_day") if isinstance(facts.get("up_day"), Mapping) else {}
    available = isinstance(up_day, Mapping) and up_day.get("status") == "available"
    result: Dict[str, Dict[str, object]] = {}
    for row in facts.get("holdings", []):  # type: ignore[union-attr]
        if not isinstance(row, Mapping):
            continue
        symbol = str(row["symbol"]).upper()
        evidence = prices.get(symbol)
        if not isinstance(evidence, str) or not evidence:
            continue
        result[symbol] = {
            "id": exposure_basis_id(symbol),
            "kind": EXPOSURE_BASIS_KIND,
            "evidence_ids": [evidence],
            "facts": {
                "weight": row.get("weight"), "beta": row.get("beta"), "up_capture": row.get("up_capture"),
                "down_capture": row.get("down_capture"), "sector": row.get("sector"),
                "up_day_date": up_day.get("date") if available else None,
                "pool_up_day_return": up_day.get("pool_return") if available else None,
                "up_day_return": row.get("up_day_return") if available else None,
            },
        }
    return result


# ---------------------------------------------------------------- 提名驗證用的純函式

def validate_lower_conviction_review(
    review: object,
    pairs: Sequence[Tuple[str, str, str]],
    lower_symbols: Sequence[str],
    high_symbols: Sequence[str],
    sold: Set[str],
    texts: List[Tuple[str, object]],
    errors: List[str],
) -> None:
    """高信心持股讓位的順序約束（計畫第 3.4 節）。``pairs`` 是依 Agent 先後的 (換入, 換出, purpose)。

    - 沒有提名高信心持股的 exposure 配對：``lower_conviction_review`` 必須是空陣列。
    - 有：必須剛好涵蓋每一檔可提名的 medium／low 持股各一次；``nominated`` 必須是某個 exposure 配對的換出者、``kept`` 不得出現在
      任何配對的換出者且必須附理由；高信心 exposure 配對必須排在所有 medium／low exposure 配對之後。
    程式只驗結構，不判斷 ``kept`` 的理由好不好、也不替 Agent 決定該先讓哪一檔。"""
    high = set(high_symbols)
    exposure_sells = [sell for _, sell, purpose in pairs if purpose == PURPOSE_EXPOSURE]
    high_indexes = [index for index, (_, sell, purpose) in enumerate(pairs) if purpose == PURPOSE_EXPOSURE and sell in high]
    if not isinstance(review, list):
        errors.append("lower_conviction_review 必須是陣列（沒有提名高信心持股的曝險配對時為空陣列）")
        return
    if not high_indexes:
        if review:
            errors.append("沒有提名高信心持股的 exposure 配對時，lower_conviction_review 必須是空陣列")
        return
    lower_indexes = [
        index for index, (_, sell, purpose) in enumerate(pairs) if purpose == PURPOSE_EXPOSURE and sell not in high
    ]
    if lower_indexes and max(lower_indexes) > min(high_indexes):
        errors.append("高信心持股的 exposure 配對必須排在所有 medium／low 持股的 exposure 配對之後（先處理中、低信心，再談高信心）")
    required = [str(symbol) for symbol in lower_symbols]
    seen: List[str] = []
    for index, entry in enumerate(review):
        prefix = "lower_conviction_review[%d]" % index
        if not isinstance(entry, Mapping):
            errors.append("%s 必須是物件" % prefix)
            continue
        reject_unknown_fields(entry, LOWER_REVIEW_FIELDS, prefix, errors)
        symbol, response = entry.get("symbol"), entry.get("response")
        if not isinstance(symbol, str) or not symbol.strip():
            errors.append("%s.symbol 必須是非空字串" % prefix)
            continue
        seen.append(symbol)
        if response not in LOWER_REVIEW_RESPONSES:
            errors.append("%s.response 必須是 %s" % (prefix, "／".join(LOWER_REVIEW_RESPONSES)))
        elif response == "nominated" and symbol not in exposure_sells:
            errors.append("%s 標 nominated 的 %s 必須是某一對 purpose=exposure 的 sell_symbol" % (prefix, symbol))
        elif response == "kept" and symbol in sold:
            errors.append("%s 標 kept 的 %s 不得出現在任何一對的 sell_symbol" % (prefix, symbol))
        reason = entry.get("reason")
        if response == "kept" and not (isinstance(reason, str) and reason.strip()):
            errors.append("%s 標 kept 必須附一句理由（reason 不得為空）" % prefix)
        elif isinstance(reason, str) and reason.strip():
            texts.append((prefix + ".reason", reason))
        elif not isinstance(reason, str):
            errors.append("%s.reason 必須是字串" % prefix)
    repeated = sorted({symbol for symbol in seen if seen.count(symbol) > 1})
    if repeated:
        errors.append("lower_conviction_review 每檔只能交代一次：%s" % ", ".join(repeated))
    missing = sorted(set(required) - set(seen))
    extra = sorted(set(seen) - set(required))
    if missing:
        errors.append("lower_conviction_review 漏交代可提名的 medium／low 持股：%s" % ", ".join(missing))
    if extra:
        errors.append("lower_conviction_review 含不需交代的股票：%s" % ", ".join(extra))


__all__ = [
    "EXPOSURE_BASIS_NOTE", "EXPOSURE_SWAP_NOTE", "EXPOSURE_SWAP_V1", "EXPOSURE_SWAP_V2", "PURPOSES", "PURPOSE_EXPOSURE",
    "PURPOSE_THESIS", "bind_exposure_view", "exposure_basis_by_symbol", "exposure_basis_id", "exposure_swap_binding",
    "exposure_swap_gate", "is_exposure_basis_id", "validate_exposure_swap_gate", "validate_lower_conviction_review",
]
