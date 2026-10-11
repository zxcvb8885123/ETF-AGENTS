"""只載入經 manifest 及完整決策鏈驗證的封存。"""

from etf_agent.competition import load_decision_run
from etf_agent.decision import DecisionInputValidator, DecisionResultValidator, MomentumResultValidator
from .contracts import DeliveryError, REQUIRED


def verify_inputs(artifacts):
    missing = REQUIRED - set(artifacts)
    if missing:
        raise DeliveryError("決策封存缺少：" + "、".join(sorted(missing)))
    b = artifacts["decision_input"]
    errors = DecisionInputValidator(b).validate()
    if errors:
        raise DeliveryError("決策輸入驗證失敗：" + "；".join(errors))
    errors = MomentumResultValidator(b).validate(artifacts["momentum"])
    validator = DecisionResultValidator(b, artifacts["policy"], artifacts["momentum"],
        artifacts["debate"], artifacts["intent"], artifacts["team_inputs"])
    errors.extend(validator.validate(*(artifacts[k] for k in
        ("proposal", "scenario", "guard", "risk_review", "revision_history", "decision"))))
    # Finalizer 可以將無效輸入重建為 rejected；報告不得將它認證成有效研究。
    errors.extend(validator.finalizer.validate_inputs(*(artifacts[k] for k in
        ("proposal", "scenario", "guard", "risk_review", "revision_history"))))
    if errors:
        raise DeliveryError("決策重建失敗：" + "；".join(errors))


def load_verified(repository, run_id):
    artifacts = load_decision_run(str(repository), run_id)
    verify_inputs(artifacts)
    return artifacts
