"""DecisionContext：單一 DecisionInputBundle 的已驗證查詢視圖。"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence

from .fields import DecisionToolError
from .input_validator import DecisionInputValidator


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
        self.overnight_evidence_ids = set()
        self.evidence_symbols = self._build_evidence_symbols()

    def _build_evidence_symbols(self) -> Dict[str, Optional[str]]:
        result: Dict[str, Optional[str]] = {}
        for collection in ("documents", "latest_prices"):
            for item in self.snapshot.get(collection, []):
                if isinstance(item, Mapping) and item.get("source_evidence_id"):
                    evidence_id = str(item["source_evidence_id"])
                    symbol = str(item.get("symbol", "")).upper() or None
                    previous = result.get(evidence_id)
                    if previous is not None and previous != symbol:
                        raise DecisionToolError("evidence_id 對應多個股票：%s" % evidence_id)
                    result[evidence_id] = symbol
        for perception in self.bundle.get("perception_inputs", []):
            if not isinstance(perception, Mapping):
                continue
            data_bundle = perception.get("bundle", {})
            if not isinstance(data_bundle, Mapping):
                continue
            for evidence in data_bundle.get("source_evidence", []):
                if isinstance(evidence, Mapping) and evidence.get("evidence_id"):
                    evidence_id = str(evidence["evidence_id"])
                    if evidence_id in result:
                        raise DecisionToolError("evidence_id 跨輸入來源重複：%s" % evidence_id)
                    result[evidence_id] = str(evidence.get("symbol", "")).upper() or None
        news = self.bundle.get("company_news_input", {})
        for item in news.get("items", []) if isinstance(news, Mapping) else []:
            evidence_id = str(item["evidence_id"])
            if evidence_id in result:
                raise DecisionToolError("evidence_id 跨輸入來源重複：%s" % evidence_id)
            result[evidence_id] = str(item["symbol"]).upper()
        # 隔夜市場背景是市場層級證據（symbol=None）；只有現金姿態可引用，股票層級引用一律拒絕。
        overnight = self.bundle.get("overnight_market_input", {})
        for component in (overnight.get("result", {}).get("components") or []) if isinstance(overnight, Mapping) else []:
            if isinstance(component, Mapping) and component.get("evidence_id"):
                evidence_id = str(component["evidence_id"])
                if evidence_id in result:
                    raise DecisionToolError("evidence_id 跨輸入來源重複：%s" % evidence_id)
                result[evidence_id] = None
                self.overnight_evidence_ids.add(evidence_id)
        status_bundle = self.bundle.get("trading_status_bundle", {})
        if isinstance(status_bundle, Mapping):
            for record in status_bundle.get("records", []):
                if isinstance(record, Mapping) and record.get("evidence_id"):
                    evidence_id = str(record["evidence_id"])
                    if evidence_id in result:
                        raise DecisionToolError("evidence_id 跨輸入來源重複：%s" % evidence_id)
                    result[evidence_id] = str(record.get("symbol", "")).upper() or None
        account_evidence_id = str(self.account["source_evidence_id"])
        if account_evidence_id in result:
            raise DecisionToolError(
                "account evidence_id 與研究證據重複：%s" % account_evidence_id
            )
        result[account_evidence_id] = None
        return result

    def validate_evidence(
        self, evidence_ids: Sequence[str], symbol: str, prefix: str, errors: List[str]
    ) -> None:
        for evidence_id in evidence_ids:
            linked_symbol = self.evidence_symbols.get(evidence_id)
            if evidence_id not in self.evidence_symbols:
                errors.append("%s 引用不存在：%s" % (prefix, evidence_id))
            elif evidence_id in self.overnight_evidence_ids:
                errors.append("%s 隔夜市場證據只能用於現金姿態，不得支持個股主張：%s" % (prefix, evidence_id))
            elif (
                evidence_id == str(self.account["source_evidence_id"])
                and symbol.upper() not in self.held_symbols
            ):
                errors.append("%s 帳戶證據只能引用於目前持股：%s" % (prefix, evidence_id))
            elif linked_symbol is not None and linked_symbol != symbol.upper():
                errors.append("%s 引用不屬於 %s：%s" % (prefix, symbol.upper(), evidence_id))
