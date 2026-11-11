"""DecisionContext：單一 DecisionInputBundle 的已驗證查詢視圖，含證據歸屬與證據種類。

證據種類（evidence kind）由程式依來源判定，不由 Agent 填寫：

- ``price``：Snapshot 最新行情（``latest_prices``）。
- ``financial``：財報與已核對現金流量表文件；``profile``：公司基本資料（產業代碼）。
- ``revenue``：月營收文件。
- ``material_event``：重大訊息公告。
- ``news``：已核准個股新聞與情緒資料包的來源證據。
- ``market``：市場層級（全市場新聞情緒、恐懼貪婪成分、隔夜市場、程式判定的市場狀態），沒有 symbol。
- ``account``／``status``：帳戶快照與交易狀態記錄。

``evidence_policy = typed_evidence_v1``（版本閘門）時，個股 claim／finding 必須至少引用一個屬於該股票且種類
對映的證據，市場層級或帳戶證據只能作補充；沒有閘門的舊 bundle 維持「引用存在且屬該股」的原行為。
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence, Set

from .fields import DecisionToolError
from .input_validator import DecisionInputValidator, EVIDENCE_POLICIES, EVIDENCE_POLICY_FIELD


# 個股 claim／finding 可宣告的種類；每種只允許對映的證據種類作主要依據。
CLAIM_KINDS = ("price", "financial", "revenue", "material_event", "news")
# 各 claim_kind 可引用的證據種類（主要依據必須是其中屬於該股票者）。
CLAIM_KIND_EVIDENCE: Dict[str, Set[str]] = {
    "price": {"price"},
    "financial": {"financial", "profile"},
    "revenue": {"revenue"},
    "material_event": {"material_event"},
    "news": {"news"},
}
# 任何種類的 claim 都可額外引用（只能作補充、不能單獨成立）的證據種類。
SUPPLEMENTARY_EVIDENCE_KINDS = frozenset({"account", "market", "status"})
# 程式依交易池廣度判定的市場狀態（MomentumResult.regime_assessment）在共同輸入中的市場層級證據編號；
# 它由 price_series 確定性重算，只在 typed_evidence_v1 下註冊，只供現金姿態引用。
MARKET_REGIME_EVIDENCE_ID = "market-regime:pool-breadth"
_DOCUMENT_KINDS = {
    "financial_statement": "financial",
    "cash_flow_statement": "financial",
    "company_profile": "profile",
    "monthly_revenue": "revenue",
    "material_event": "material_event",
}


def untradable_symbols(bundle: Mapping[str, object]) -> Set[str]:
    """明確交易狀態包中非 allowed 的標的（與 CompetitionGuard 的 TRADABLE 規則同源）；沒有狀態包時回空集合。

    配置（``AllocationOrderEngine``）與換股提名（``swap_nomination``）共用這一個實作。"""
    status_bundle = bundle.get("trading_status_bundle")
    assessment = bundle.get("tradability_assessment")
    if not isinstance(status_bundle, Mapping) or not isinstance(assessment, Mapping):
        return set()
    return {
        str(item.get("symbol", "")).upper()
        for item in assessment.get("symbols", [])
        if isinstance(item, Mapping) and item.get("symbol") and str(item.get("state", "unknown")) != "allowed"
    }


def evidence_policy(bundle: Mapping[str, object]) -> Optional[str]:
    """讀取 bundle 的證據種類政策；沒有欄位為舊行為（None），未知值 fail-closed。"""
    policy = bundle.get(EVIDENCE_POLICY_FIELD)
    if policy is None:
        return None
    if policy not in EVIDENCE_POLICIES:
        raise DecisionToolError("不支援的證據種類政策：%s" % policy)
    return str(policy)


class DecisionContext:
    """Validated lookup view over one DecisionInputBundle."""

    def __init__(self, bundle: Mapping[str, object]):
        errors = DecisionInputValidator(bundle).validate()
        if errors:
            raise DecisionToolError("DecisionInputBundle 驗證失敗：" + "；".join(errors))
        self.bundle = dict(bundle)
        self.snapshot = dict(bundle["snapshot"])
        self.bundle_id = str(bundle["bundle_id"])
        self.snapshot_id = str(bundle["snapshot_id"])
        self.decision_cutoff = str(bundle["decision_cutoff"])
        self.bundle_hash = str(bundle["bundle_sha256"])
        self.price_series = {
            str(item["symbol"]).upper(): dict(item)
            for item in bundle["price_series"]
        }
        self.universe = set(self.price_series)
        account = dict(bundle["account_snapshot"])
        self.account = account
        self.held_symbols = {
            str(item["symbol"]).upper()
            for item in account.get("positions", [])
            if isinstance(item, Mapping) and int(item.get("shares", 0)) > 0
        }
        self.evidence_policy = evidence_policy(bundle)
        self.overnight_evidence_ids: Set[str] = set()
        # 市場層級證據：隔夜市場、全市場情緒（新聞或恐懼貪婪成分）與市場狀態；後兩者只在 typed_evidence_v1 下註冊。
        self.market_evidence_ids: Set[str] = set()
        # 全市場情緒通道（typed_evidence_v1 下才重建；現金姿態一致性護欄用），舊 bundle 為 None。
        self.market_sentiment: Optional[Dict[str, object]] = None
        self.evidence_kinds: Dict[str, str] = {}
        self.evidence_symbols = self._build_evidence_symbols()

    def _register(self, result: Dict[str, Optional[str]], evidence_id: str, symbol: Optional[str], kind: str) -> None:
        if evidence_id in result:
            raise DecisionToolError("evidence_id 跨輸入來源重複：%s" % evidence_id)
        result[evidence_id] = symbol
        self.evidence_kinds[evidence_id] = kind

    def _build_evidence_symbols(self) -> Dict[str, Optional[str]]:
        result: Dict[str, Optional[str]] = {}
        for collection, default_kind in (("documents", None), ("latest_prices", "price")):
            for item in self.snapshot.get(collection, []):
                if isinstance(item, Mapping) and item.get("source_evidence_id"):
                    evidence_id = str(item["source_evidence_id"])
                    symbol = str(item.get("symbol", "")).upper() or None
                    previous = result.get(evidence_id)
                    if previous is not None and previous != symbol:
                        raise DecisionToolError("evidence_id 對應多個股票：%s" % evidence_id)
                    result[evidence_id] = symbol
                    kind = default_kind or _DOCUMENT_KINDS.get(str(item.get("document_type")), str(item.get("document_type") or "document"))
                    self.evidence_kinds.setdefault(evidence_id, kind)
        for perception in self.bundle.get("perception_inputs", []):
            if not isinstance(perception, Mapping):
                continue
            data_bundle = perception.get("bundle", {})
            if not isinstance(data_bundle, Mapping):
                continue
            for evidence in data_bundle.get("source_evidence", []):
                if isinstance(evidence, Mapping) and evidence.get("evidence_id"):
                    self._register(result, str(evidence["evidence_id"]), str(evidence.get("symbol", "")).upper() or None, "news")
        news = self.bundle.get("company_news_input", {})
        for item in news.get("items", []) if isinstance(news, Mapping) else []:
            self._register(result, str(item["evidence_id"]), str(item["symbol"]).upper(), "news")
        # 隔夜市場背景是市場層級證據（symbol=None）；只有現金姿態可引用，股票層級引用一律拒絕。
        overnight = self.bundle.get("overnight_market_input", {})
        for component in (overnight.get("result", {}).get("components") or []) if isinstance(overnight, Mapping) else []:
            if isinstance(component, Mapping) and component.get("evidence_id"):
                evidence_id = str(component["evidence_id"])
                self._register(result, evidence_id, None, "market")
                self.overnight_evidence_ids.add(evidence_id)
                self.market_evidence_ids.add(evidence_id)
        status_bundle = self.bundle.get("trading_status_bundle", {})
        if isinstance(status_bundle, Mapping):
            for record in status_bundle.get("records", []):
                if isinstance(record, Mapping) and record.get("evidence_id"):
                    self._register(result, str(record["evidence_id"]), str(record.get("symbol", "")).upper() or None, "status")
        account_evidence_id = str(self.account["source_evidence_id"])
        if account_evidence_id in result:
            raise DecisionToolError(
                "account evidence_id 與研究證據重複：%s" % account_evidence_id
            )
        result[account_evidence_id] = None
        self.evidence_kinds[account_evidence_id] = "account"
        if self.evidence_policy is not None:
            self._register_market_evidence(result)
        return result

    def _register_market_evidence(self, result: Dict[str, Optional[str]]) -> None:
        """typed_evidence_v1：把全市場情緒的證據與市場狀態編號註冊為市場層級證據（symbol=None、kind=market）。

        舊 bundle 不呼叫，evidence_symbols 與舊版完全相同。"""
        from .market_scope import market_sentiment_channel

        channel = market_sentiment_channel(self.bundle)
        self.market_sentiment = channel
        ids: List[str] = []
        for finding in channel.get("findings", []):
            if isinstance(finding, Mapping):
                ids.extend(str(value) for value in finding.get("evidence_ids", []) if value)
        fear_greed = channel.get("fear_greed")
        if isinstance(fear_greed, Mapping):
            for component in fear_greed.get("components", []):
                if isinstance(component, Mapping) and component.get("evidence_id"):
                    ids.append(str(component["evidence_id"]))
        for evidence_id in dict.fromkeys(ids):
            self._register(result, evidence_id, None, "market")
            self.market_evidence_ids.add(evidence_id)
        self._register(result, MARKET_REGIME_EVIDENCE_ID, None, "market")
        self.market_evidence_ids.add(MARKET_REGIME_EVIDENCE_ID)

    def validate_evidence(
        self,
        evidence_ids: Sequence[str],
        symbol: str,
        prefix: str,
        errors: List[str],
        kind: Optional[str] = None,
    ) -> None:
        """個股 claim／finding 的引用檢查。``kind`` 是 Agent 宣告的 claim_kind／finding_kind（typed_evidence_v1 才有）。"""
        symbol = symbol.upper()
        for evidence_id in evidence_ids:
            linked_symbol = self.evidence_symbols.get(evidence_id)
            if evidence_id not in self.evidence_symbols:
                errors.append("%s 引用不存在：%s" % (prefix, evidence_id))
            elif evidence_id in self.overnight_evidence_ids:
                errors.append("%s 隔夜市場證據只能用於現金姿態，不得支持個股主張：%s" % (prefix, evidence_id))
            elif (
                evidence_id == str(self.account["source_evidence_id"])
                and symbol not in self.held_symbols
            ):
                errors.append("%s 帳戶證據只能引用於目前持股：%s" % (prefix, evidence_id))
            elif linked_symbol is not None and linked_symbol != symbol:
                errors.append("%s 引用不屬於 %s：%s" % (prefix, symbol, evidence_id))
        if self.evidence_policy is None:
            return
        self._validate_typed_evidence(evidence_ids, symbol, prefix, errors, kind)

    def _validate_typed_evidence(
        self, evidence_ids: Sequence[str], symbol: str, prefix: str, errors: List[str], kind: Optional[str]
    ) -> None:
        """typed_evidence_v1：至少一個屬於該股票的證據；宣告種類時主要依據的種類必須對映，其餘只能是補充種類。"""
        known = [evidence_id for evidence_id in evidence_ids if evidence_id in self.evidence_symbols]
        own = [evidence_id for evidence_id in known if self.evidence_symbols.get(evidence_id) == symbol]
        if known and not own:
            errors.append(
                "%s 只引用市場層級或帳戶證據，無法單獨支持 %s 的主張；請補上屬於該股票的證據，或撤回／降級這個主張"
                % (prefix, symbol)
            )
        if kind is None:
            return
        if kind not in CLAIM_KIND_EVIDENCE:
            errors.append("%s 種類必須是 %s" % (prefix, "／".join(CLAIM_KINDS)))
            return
        allowed = CLAIM_KIND_EVIDENCE[kind]
        primary = [evidence_id for evidence_id in own if self.evidence_kinds.get(evidence_id) in allowed]
        if known and not primary:
            errors.append(
                "%s 宣告的種類 %s 需要至少一個該股票的 %s 證據作主要依據；請改宣告與證據相符的種類，或改引用相符的證據"
                % (prefix, kind, "／".join(sorted(allowed)))
            )
        for evidence_id in known:
            evidence_kind = self.evidence_kinds.get(evidence_id)
            if evidence_kind not in allowed and evidence_kind not in SUPPLEMENTARY_EVIDENCE_KINDS:
                errors.append(
                    "%s 證據 %s 的種類是 %s，與宣告的 %s 不對映；請拆成各自種類的主張，或移除這個引用"
                    % (prefix, evidence_id, evidence_kind, kind)
                )
