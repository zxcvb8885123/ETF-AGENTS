"""回撤減碼的釋出順序（drawdown_derisk v5，docs/agent_autonomy_plan.md 階段 2）：交易 Agent 在入選排序之後，對「可減碼的持股」
給「若需要降低持股，先放掉誰」的先後順序與有證據的理由。

分工：Agent 只輸出代號、依據識別碼與文字（不輸出任何數字）；賣多少、單次預算、換手餘額、整張與最低持股檔數全部由
``allocation._drawdown_derisk`` 以 v4 的規則算。這份順序每天都產生，只有防守姿態且現金低於緩衝時才被配置使用；
姿態要到風險 Agent 之後才知道，所以這裡看不到、也不告訴 Agent 姿態或缺口。

本模組只含純函式與 Validator（不碰 pipeline、不呼叫 Agent），做法同 ``swap_nomination``：

- 信封與封存：``release_envelope``／``seal_release_order``／``program_release_order``；``status`` 四種：
  ``completed``（Agent 的順序）、``not_needed``（可減碼持股少於兩檔，沒有先後可排，程式封存、不呼叫 Agent）、
  ``exhausted``（驗證用盡）、``unavailable``（暫時性失敗），後三者由程式封存、``sequence`` 為空（欄位不叫 order：``validate_forbidden_keys`` 禁止訂單欄位名稱）。
- 事實：``release_facts`` 以同一份 bundle、交易決策、辯論與持股檢視確定性算出可減碼持股與每檔可引用的依據；
  簡報、``not_needed`` 判定與 Validator 共用它，Finalizer 才能整份重算。
- 可行性：順序必須剛好涵蓋可減碼持股各一次（持有、可交易、有行情、第一輪不是 exit／trim）；動態條件（被留住的出場、
  最低檔數、預算）留到配置。
- Policy 綁定：``apply_release_order`` 把順序綁進 ``drawdown_derisk.release_symbols``／``release_ref``（只在 v5）。
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence

from .contracts import (
    DecisionToolError,
    artifact_content_sha256,
    decimal_value,
    reject_unknown_fields,
    required_string,
    snapshot_execution_price,
    untradable_symbols,
    validate_forbidden_keys,
)
from .position_review import position_ids_by_symbol, review_ids_by_symbol
from .prior_decision import prior_reference_tokens
from .sizing import drawdown_derisk_v5
from .trader import REDUCING, claims_by_symbol


RELEASE_ORDER_VERSION = "1.0"
RELEASE_ORDER_KEY = "release_order"  # team_inputs 的選配鍵（不升版本；與 drawdown_derisk v5 雙向同時存在）
STATUS_COMPLETED = "completed"
STATUS_NOT_NEEDED = "not_needed"
STATUS_EXHAUSTED = "exhausted"
STATUS_UNAVAILABLE = "unavailable"
STATUSES = (STATUS_COMPLETED, STATUS_NOT_NEEDED, STATUS_EXHAUSTED, STATUS_UNAVAILABLE)
PROGRAM_STATUSES = (STATUS_NOT_NEEDED, STATUS_EXHAUSTED, STATUS_UNAVAILABLE)
ENVELOPE_FIELDS = {
    "schema_version", "release_order_id", "bundle_id", "snapshot_id", "decision_cutoff", "bundle_hash",
    "trade_decision_id", "trade_decision_sha256", "selection_ranking_id", "selection_ranking_sha256",
    "status", "sequence", "errors", "content_sha256",
}
AGENT_OUTPUT_FIELDS = {"sequence"}
ENTRY_FIELDS = {"symbol", "basis_ids", "reason"}
MAX_DEGRADED_ERRORS = 10
PROGRAM_NOTES = {
    STATUS_NOT_NEEDED: "可減碼的持股少於兩檔，沒有先後可排；若需要減碼，程式依確定性順序處理（只有一檔時順序不影響結果）。",
    STATUS_EXHAUSTED: "釋出順序的驗證在嘗試用盡後仍未通過，本次不使用交易 Agent 的順序；減碼時退回程式的確定性順序。",
    STATUS_UNAVAILABLE: "釋出順序的 Agent 呼叫在暫時性失敗重試後仍失敗，本次不使用交易 Agent 的順序；減碼時退回程式的確定性順序。",
}
RATE_QUANTUM = Decimal("0.000001")
BRIEF_NOTE = (
    "這是一份「若之後需要降低持股，先放掉誰」的先後順序：把最先願意放掉的放在陣列最前面，最後才願意放掉的放在最後。"
    "順序不是賣出指令，也不會改變你第一輪的意圖與信心；今天是否需要減碼、要賣多少，由程式與風險 Agent 的姿態稍後決定。"
    "每檔持股都必須出現一次，每檔附一句白話理由與至少一個該股自己的依據識別碼（basis 的 id）。"
    "權重、報酬、ATR 等數字只供判斷，不可改寫、也不可寫成新的數字結論。"
)


def release_envelope(
    bundle: Mapping[str, object], trade_decision: Mapping[str, object], ranking: Mapping[str, object], release_order_id: str,
) -> Dict[str, object]:
    return {
        "schema_version": RELEASE_ORDER_VERSION,
        "release_order_id": release_order_id,
        "bundle_id": bundle["bundle_id"],
        "snapshot_id": bundle["snapshot_id"],
        "decision_cutoff": bundle["decision_cutoff"],
        "bundle_hash": bundle["bundle_sha256"],
        "trade_decision_id": trade_decision["decision_id"],
        "trade_decision_sha256": trade_decision["content_sha256"],
        "selection_ranking_id": ranking["ranking_id"],
        "selection_ranking_sha256": ranking["content_sha256"],
    }


def _seal(body: Dict[str, object]) -> Dict[str, object]:
    body["content_sha256"] = artifact_content_sha256(body)
    return body


def seal_release_order(envelope: Mapping[str, object], raw: object) -> Dict[str, object]:
    """把 Agent 的原始輸出補上信封並封存為 ``completed``。

    這個函式不會因 Agent 輸出的形狀而拋錯（``build`` 必須是全函式）：任何不符的內容原樣保留，交給 Validator 回報；
    Agent 多給的頂層鍵以 ``x_`` 前綴保留（不會覆蓋信封欄位），Validator 會以「含未允許欄位」拒絕。"""
    output = raw if isinstance(raw, Mapping) else {}
    body: Dict[str, object] = {
        **dict(envelope),
        "status": STATUS_COMPLETED,
        "sequence": copy.deepcopy(output.get("sequence")),
        "errors": [],
    }
    for key in sorted(str(key) for key in output if str(key) not in AGENT_OUTPUT_FIELDS):
        body["x_%s" % key] = copy.deepcopy(output[key])
    if not isinstance(raw, Mapping):
        body["x_output_not_object"] = True
    return _seal(body)


def program_release_order(envelope: Mapping[str, object], status: str, errors: Sequence[str] = ()) -> Dict[str, object]:
    """程式封存的順序：``not_needed``（不呼叫 Agent）、``exhausted``、``unavailable``；``sequence`` 恆為空。"""
    if status not in PROGRAM_STATUSES:
        raise DecisionToolError("程式只能封存 %s" % "／".join(PROGRAM_STATUSES))
    return _seal({
        **dict(envelope),
        "status": status,
        "sequence": [],
        "errors": [str(error) for error in errors][:MAX_DEGRADED_ERRORS],
    })


def ordered_symbols(release_order: Optional[Mapping[str, object]]) -> List[str]:
    """配置可使用的順序（依 Agent 的先後）：只有 ``completed`` 才有，其餘（含 None）一律空清單。"""
    if not isinstance(release_order, Mapping) or release_order.get("status") != STATUS_COMPLETED:
        return []
    return [str(entry["symbol"]).upper() for entry in release_order.get("sequence") or [] if isinstance(entry, Mapping)]


# ---------------------------------------------------------------- 事實

def _held_shares(bundle: Mapping[str, object]) -> Dict[str, int]:
    return {
        str(position["symbol"]).upper(): int(position["shares"])
        for position in bundle["account_snapshot"]["positions"] if int(position.get("shares", 0)) > 0
    }


def _roles(debate: Mapping[str, object], position_review: Optional[Mapping[str, object]]) -> Dict[str, Dict[str, str]]:
    """每檔可引用的識別碼 → 角色（bull／bear／review／position），組法同 ``TradeDecisionValidator``。"""
    roles = claims_by_symbol(debate)
    for symbol, flags in review_ids_by_symbol(position_review).items():
        roles.setdefault(symbol, {}).update({review: "review" for review in flags})
    for symbol, identifier in position_ids_by_symbol(position_review).items():
        roles.setdefault(symbol, {})[identifier] = "position"
    return roles


def release_facts(
    bundle: Mapping[str, object],
    trade_decision: Mapping[str, object],
    debate: Mapping[str, object],
    position_review: Optional[Mapping[str, object]],
) -> Dict[str, object]:
    """釋出順序的全部確定性事實；簡報、``not_needed`` 判定與 Validator 共用。缺任何必要輸入拋 ``DecisionToolError``（fail-closed）。

    可減碼持股 = 持有、可交易、同 Snapshot 有行情、第一輪意圖不是 exit／forced_exit／trim（與配置的 v4 篩選同一規則的靜態部分；
    被留住的出場、最低檔數等動態條件到配置才知道，配置只會從這個集合取子集）。每檔可引用的依據 = 該股第一輪已採納的
    多空 claim 與持股檢視警示，加上該股的持股事實編號（``review:<代號>:position``，每檔持股都有，所以依據一定湊得出來）；
    第一輪已否決的 claim、別檔的識別碼不可引用。"""
    if not isinstance(position_review, Mapping):
        raise DecisionToolError("釋出順序需要持股檢視")
    held = _held_shares(bundle)
    prices = {str(row["symbol"]).upper() for row in bundle["snapshot"]["latest_prices"]}
    untradable = untradable_symbols(bundle)
    roles = _roles(debate, position_review)
    positions = position_ids_by_symbol(position_review)
    items = {str(item["symbol"]).upper(): item for item in trade_decision.get("items", []) if isinstance(item, Mapping)}
    releasable: List[Dict[str, object]] = []
    excluded: List[Dict[str, object]] = []
    for symbol in sorted(held):
        item = items.get(symbol, {})
        intent = str(item.get("intent", "hold"))
        reasons = [
            code for code, failed in (
                ("not_tradable", symbol in untradable),
                ("no_price", symbol not in prices),
                ("first_round_reducing", intent in REDUCING),
            ) if failed
        ]
        if reasons:
            excluded.append({"symbol": symbol, "reasons": reasons})
            continue
        basis = {
            str(claim) for claim in item.get("adopted_claim_ids", [])
            if roles.get(symbol, {}).get(str(claim)) in {"bull", "bear", "review"}
        }
        if symbol in positions:
            basis.add(positions[symbol])
        if not basis:
            raise DecisionToolError("持股 %s 沒有任何可引用的依據（缺少持股事實編號），無法建立釋出順序" % symbol)
        releasable.append({
            "symbol": symbol, "intent": intent, "conviction": item.get("conviction"),
            "rationale": str(item.get("rationale", "")), "basis_ids": sorted(basis),
        })
    return {"releasable": releasable, "excluded": excluded, "needs_agent": len(releasable) >= 2}


def needs_release_order(facts: Mapping[str, object]) -> bool:
    """可減碼持股至少兩檔才有先後可排；否則不呼叫 Agent，由程式封存 ``not_needed``。"""
    return bool(facts["needs_agent"])


# ---------------------------------------------------------------- 簡報

def release_order_brief(
    bundle: Mapping[str, object],
    trade_decision: Mapping[str, object],
    debate: Mapping[str, object],
    position_review: Mapping[str, object],
    momentum: Mapping[str, object],
) -> Dict[str, object]:
    """給交易 Agent 的簡報（全部由程式算）：每檔可減碼持股的第一輪意圖與信心、現有權重、帳面報酬、近期回落、ATR 與可引用的依據；
    另列不在順序內的持股與原因。**不附**風險 Agent 的姿態、現金缺口與減碼預算（此時尚未產生）。"""
    facts = release_facts(bundle, trade_decision, debate, position_review)
    roles = _roles(debate, position_review)
    texts: Dict[str, str] = {}
    for packet in debate.get("packets", []):
        for item in packet.get("items", []):
            for claim in item.get("claims", []):
                texts[str(claim["claim_id"])] = str(claim.get("text", ""))
    reviews = {str(item["symbol"]).upper(): item for item in position_review.get("items", []) if isinstance(item, Mapping)}
    measured = {str(item.get("symbol", "")).upper(): item for item in momentum.get("items", [])}
    latest = {str(row["symbol"]).upper(): snapshot_execution_price(row, "latest_prices") for row in bundle["snapshot"]["latest_prices"]}
    held = _held_shares(bundle)
    account = bundle["account_snapshot"]
    nav = decimal_value(account["cash"], "account_snapshot.cash") + sum(
        (Decimal(shares) * latest[symbol] for symbol, shares in held.items() if symbol in latest), Decimal(0))

    def basis_row(symbol: str, identifier: str) -> Dict[str, object]:
        role = roles[symbol][identifier]
        row: Dict[str, object] = {"id": identifier, "kind": role}
        if role in {"bull", "bear"}:
            row["text"] = texts.get(identifier, "")
        elif role == "review":
            row["text"] = "持股檢視警示：%s（程式事實，不是賣出指令）" % identifier.rsplit(":", 1)[-1]
        else:
            item = reviews.get(symbol, {})
            row["facts"] = {key: item.get(key) for key in (
                "unrealized_return", "retrace_from_high_5d", "retrace_from_high_20d", "relative_to_pool_5d", "relative_to_pool_20d") if key in item}
        return row

    holdings = []
    for row in facts["releasable"]:  # type: ignore[union-attr]
        symbol = str(row["symbol"])
        weight = None
        if nav > 0 and symbol in latest:
            weight = str((Decimal(held[symbol]) * latest[symbol] / nav).quantize(RATE_QUANTUM))
        review = reviews.get(symbol, {})
        holdings.append({
            "symbol": symbol,
            "first_round_intent": row["intent"],
            "conviction": row["conviction"],
            "trader_rationale": row["rationale"],
            "weight": weight,
            "unrealized_return": review.get("unrealized_return"),
            "retrace_from_high_5d": review.get("retrace_from_high_5d"),
            "retrace_from_high_20d": review.get("retrace_from_high_20d"),
            "atr_14_pct": measured.get(symbol, {}).get("atr_14_pct"),
            "basis": [basis_row(symbol, identifier) for identifier in row["basis_ids"]],
        })
    return {
        "note": BRIEF_NOTE,
        "holdings": holdings,
        "not_in_order": [
            {"symbol": row["symbol"], "reasons": row["reasons"]} for row in facts["excluded"]  # type: ignore[union-attr]
        ],
    }


# ---------------------------------------------------------------- Validator

def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


class ReleaseOrderValidator:
    """釋出順序必須綁定同一份交易決策與排序，且依 ``status`` 分別驗證：

    - ``not_needed``：程式以同一規則重算必須「不需要」（可減碼持股少於兩檔），形狀是固定的空清單；反過來，需要時封存 ``not_needed`` 一律拒絕。
    - ``exhausted``／``unavailable``：只在「需要」時才可能發生；形狀是固定的空清單加非空 ``errors``（Finalizer 無法證明 Agent
      真的失敗，只能核對形狀；配置退回 v4 順序並留旗標）。
    - ``completed``：Agent 的順序，必須剛好涵蓋可減碼持股各一次，每檔附自己的依據與理由。
    錯誤用中性措辭，並列「改正順序」與「補依據」兩種修法，不暗示該降級或該硬湊。
    """

    def __init__(
        self,
        bundle: Mapping[str, object],
        trade_decision: Mapping[str, object],
        ranking: Mapping[str, object],
        debate: Mapping[str, object],
        position_review: Optional[Mapping[str, object]],
        prior_context: Optional[Mapping[str, object]] = None,
    ):
        self.bundle = dict(bundle)
        self.trade = dict(trade_decision)
        self.ranking = dict(ranking)
        self.debate = debate
        self.position_review = position_review
        self.prior_tokens = prior_reference_tokens(prior_context)

    def validate(self, release_order: Mapping[str, object]) -> List[str]:
        errors: List[str] = []
        if not isinstance(release_order, Mapping):
            return ["ReleaseOrder 必須是物件"]
        validate_forbidden_keys(release_order, "ReleaseOrder", errors)
        reject_unknown_fields(release_order, ENVELOPE_FIELDS, "ReleaseOrder", errors)
        try:
            expected = release_envelope(self.bundle, self.trade, self.ranking, str(release_order.get("release_order_id")))
        except KeyError as error:
            return errors + ["ReleaseOrder 無法建立信封：缺少 %s" % error]
        for field, value in expected.items():
            if release_order.get(field) != value:
                errors.append("ReleaseOrder.%s 與交易決策或入選排序不一致" % field)
        try:
            required_string(release_order, "release_order_id")
        except DecisionToolError as error:
            errors.append("ReleaseOrder.%s" % error)
        if release_order.get("content_sha256") != artifact_content_sha256(release_order):
            errors.append("ReleaseOrder.content_sha256 與內容不一致")
        status = release_order.get("status")
        if status not in STATUSES:
            return errors + ["ReleaseOrder.status 必須是 %s" % "／".join(STATUSES)]
        try:
            facts = release_facts(self.bundle, self.trade, self.debate, self.position_review)
        except (DecisionToolError, KeyError, TypeError, ValueError) as error:
            return errors + ["ReleaseOrder 無法以共同輸入重算事實：%s" % error]
        needed = needs_release_order(facts)
        if status == STATUS_NOT_NEEDED and needed:
            errors.append("本次可減碼持股至少兩檔、有先後可排，不得封存 not_needed")
        if status != STATUS_NOT_NEEDED and not needed:
            errors.append("本次可減碼持股少於兩檔，必須由程式封存 not_needed，不得有 %s" % status)
        if status in PROGRAM_STATUSES:
            self._validate_program_shape(release_order, str(status), errors)
            return errors
        self._validate_completed(release_order, facts, errors)
        return errors

    @staticmethod
    def _validate_program_shape(release_order: Mapping[str, object], status: str, errors: List[str]) -> None:
        if release_order.get("sequence") != []:
            errors.append("status=%s 的 sequence 必須是空陣列" % status)
        problems = release_order.get("errors")
        if status == STATUS_NOT_NEEDED:
            if problems != []:
                errors.append("status=not_needed 的 errors 必須為空陣列")
        elif (not isinstance(problems, list) or not problems or len(problems) > MAX_DEGRADED_ERRORS
              or not all(_text(item) for item in problems)):
            errors.append("status=%s 的 errors 必須是 1～%d 筆非空字串" % (status, MAX_DEGRADED_ERRORS))

    def _validate_completed(self, release_order: Mapping[str, object], facts: Mapping[str, object], errors: List[str]) -> None:
        if release_order.get("errors") != []:
            errors.append("ReleaseOrder.errors 在 status=completed 必須為空陣列")
        sequence = release_order.get("sequence")
        if not isinstance(sequence, list):
            errors.append("ReleaseOrder.sequence 必須是陣列")
            return
        releasable = {str(row["symbol"]): row for row in facts["releasable"]}  # type: ignore[union-attr]
        excluded = {str(row["symbol"]): row["reasons"] for row in facts["excluded"]}  # type: ignore[union-attr]
        items = {str(item["symbol"]).upper(): item for item in self.trade.get("items", [])}
        roles = _roles(self.debate, self.position_review)
        seen: List[str] = []
        for index, entry in enumerate(sequence):
            prefix = "sequence[%d]" % index
            if not isinstance(entry, Mapping):
                errors.append("%s 必須是物件" % prefix)
                continue
            reject_unknown_fields(entry, ENTRY_FIELDS, prefix, errors)
            symbol = entry.get("symbol")
            if not _text(symbol) or symbol != str(symbol).upper():
                errors.append("%s.symbol 必須是大寫代號字串" % prefix)
                continue
            symbol = str(symbol)
            seen.append(symbol)
            if symbol not in releasable:
                if symbol in excluded:
                    why = "、".join(excluded[symbol])
                elif symbol in items:
                    why = "未持有"
                else:
                    why = "不在交易池"
                errors.append("%s 的 %s 不在可減碼持股內（%s）；請從順序中移除，或改列其他持股" % (prefix, symbol, why))
                continue
            self._check_basis(prefix, entry.get("basis_ids"), symbol, releasable[symbol], items.get(symbol, {}), roles, errors)
            if not _text(entry.get("reason")):
                errors.append("%s.reason 必須是非空字串" % prefix)
            else:
                cited = sorted(token for token in self.prior_tokens if token in str(entry["reason"]))
                if cited:
                    errors.append("%s.reason 不得引用前次決策背景（%s）；它不是本次證據" % (prefix, ", ".join(cited)))
        repeated = sorted({symbol for symbol in seen if seen.count(symbol) > 1})
        if repeated:
            errors.append("順序中每檔持股只能出現一次：%s" % ", ".join(repeated))
        missing = sorted(set(releasable) - set(seen))
        if missing:
            errors.append("順序缺少可減碼的持股：%s；每檔都必須列出一次" % ", ".join(missing))

    @staticmethod
    def _check_basis(
        prefix: str, value: object, symbol: str, row: Mapping[str, object], item: Mapping[str, object],
        roles: Mapping[str, Mapping[str, str]], errors: List[str],
    ) -> None:
        label = "%s.basis_ids" % prefix
        if not isinstance(value, list) or not value or not all(_text(identifier) for identifier in value):
            errors.append("%s 必須是非空的字串清單" % label)
            return
        if len(set(value)) != len(value):
            errors.append("%s 不得重複" % label)
        allowed = set(row["basis_ids"])  # type: ignore[arg-type]
        rejected = set(item.get("rejected_claim_ids", []))
        own = roles.get(symbol, {})
        owned_elsewhere = {identifier for other, mapping in roles.items() if other != symbol for identifier in mapping}
        for identifier in value:
            if identifier in allowed:
                continue
            if identifier in rejected:
                why = "第一輪已否決"
            elif identifier in own:
                why = "第一輪沒有採納"
            elif identifier in owned_elsewhere:
                why = "屬於其他股票"
            else:
                why = "不存在"
            errors.append(
                "%s 的 %s 不可引用（%s）：%s；請改引用該股自己已採納的 claim、持股檢視警示或持股事實編號" % (label, symbol, why, identifier))


# ---------------------------------------------------------------- Policy 綁定與 team_inputs

def _expected_binding(release_order: Mapping[str, object]) -> Dict[str, object]:
    """Policy 綁定的釋出順序：依 Agent 先後的代號與封存識別（只有 completed 才有代號）。"""
    return {
        "release_symbols": ordered_symbols(release_order),
        "release_ref": {
            "release_order_id": str(release_order["release_order_id"]), "sha256": str(release_order["content_sha256"]),
            "status": str(release_order["status"]),
        },
    }


def apply_release_order(policy: Mapping[str, object], release_order: Mapping[str, object]) -> Dict[str, object]:
    """把釋出順序綁進 Policy 的 ``drawdown_derisk``（只在 v5；做法同 ``apply_swap_nomination``）：樣板不得預填，先清掉再綁；
    沒有 v5 的 Policy 原樣回傳（配置不讀順序）。呼叫端須重算 content_sha256。"""
    if not drawdown_derisk_v5(policy.get("position_sizing")):
        return dict(policy)
    result = copy.deepcopy(dict(policy))
    bound = result["position_sizing"]["drawdown_derisk"]
    bound.pop("release_symbols", None)
    bound.pop("release_ref", None)
    bound.update(_expected_binding(release_order))
    result.pop("content_sha256", None)
    return result


def validate_team_release_order(
    bundle: Mapping[str, object],
    policy: Mapping[str, object],
    trade_decision: Mapping[str, object],
    debate: Mapping[str, object],
    team: Mapping[str, object],
    errors: List[str],
) -> None:
    """Finalizer 對 team_inputs.release_order 的整份重建驗證（信封、雜湊、事實重算）與 Policy 綁定比對。

    只讀 team_inputs 內已由其他檢查驗證過的入選排序、持股檢視與前次決策背景；缺任何一項即拒絕。"""
    release_order = team.get(RELEASE_ORDER_KEY)
    if not isinstance(release_order, Mapping):
        errors.append("team_inputs.release_order 必須是物件")
        return
    ranking, review = team.get("selection_ranking"), team.get("position_review")
    if not isinstance(ranking, Mapping) or not isinstance(review, Mapping):
        errors.append("team_inputs.release_order 需要同一份入選排序與持股檢視才能重建")
        return
    prior = team.get("prior_decision_context")
    try:
        problems = ReleaseOrderValidator(
            bundle, trade_decision, ranking, debate, review, prior_context=prior if isinstance(prior, Mapping) else None,
        ).validate(release_order)
    except (DecisionToolError, KeyError, TypeError, ValueError) as error:
        problems = ["無法重建驗證：%s" % error]
    errors.extend("team_inputs.release_order：%s" % problem for problem in problems)
    sizing = policy.get("position_sizing")
    derisk = sizing.get("drawdown_derisk") if isinstance(sizing, Mapping) else None
    if not isinstance(derisk, Mapping):
        return
    try:
        expected = _expected_binding(release_order)
    except (KeyError, TypeError, ValueError) as error:
        errors.append("team_inputs.release_order 無法比對 Policy 綁定：%s" % error)
        return
    for field, value in expected.items():
        if derisk.get(field) != value:
            errors.append("DecisionPolicy.position_sizing.drawdown_derisk.%s 與 team_inputs.release_order 不一致" % field)
