"""最終交付契約。"""

VERSION = "1.0"
REQUIRED = frozenset("decision_input momentum debate intent policy proposal scenario guard risk_review revision_history decision team_inputs".split())


class DeliveryError(ValueError):
    """交付無法安全完成。"""
