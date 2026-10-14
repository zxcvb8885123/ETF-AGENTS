"""最終交付服務；可獨立重跑，不呼叫 Agent。"""

from pathlib import Path
from etf_agent.core import canonical_sha256
from .builder import build_report
from .contracts import DeliveryError, VERSION
from .provider import load_verified
from .renderer import render_report
from .repository import DeliveryRepository
from .tools import cell
from .validator import validate_report


def diagnostic_markdown(status):
    lines = ["# 最終交付診斷", "", "本次未產生已驗證決策報告。", "",
        "執行識別：%s；執行開始：%s。" % (cell(status.get("run_id")), cell(status.get("started_at"))), ""]
    lines += ["- " + cell(item["message"]) for item in status["blockers"]]
    return "\n".join(lines) + "\n"


def status_footer(status):
    return "\n## 交付狀態\n\nD-Plan：%s。平台驗證：未執行。\n\n%s\n" % (
        {"blocked": "資料未齊，未產生候選", "not_requested": "未要求匯出"}[status["dplan_status"]],
        "\n".join("- " + cell(b["message"]) for b in status["blockers"]))


def report_status(report):
    blockers = [{"code": "DPLAN_CONTEXT_REQUIRED", "stage": "dplan", "message": "D-Plan 尚需有來源映射的 context、真實隊伍與核准策略，以及目前 prepare-day 帳本核對；本次只交付內部最終報告。"}]
    if report["decision_status"] == "rejected":
        blockers = [{"code": "DECISION_REJECTED", "stage": "dplan", "message": "決策被風控拒絕，不產生 D-Plan 候選。"}]
    return {"report_status": "ready", "decision_status": report["decision_status"],
        "dplan_status": "blocked", "organizer_validation": "not_run", "blockers": blockers}


def save_delivery(output_root, artifacts, reports_root=None, started_at=None):
    identity = "delivery-" + canonical_sha256(artifacts)[:32]
    repo = DeliveryRepository(output_root)
    path, view = repo.save_delivery(identity, artifacts)
    repo.verify_delivery(identity)
    published = False
    if reports_root is not None:
        if started_at is None:
            raise DeliveryError("發布 latest 必須提供本次執行 started_at")
        published = repo.publish(identity, reports_root, started_at)
    return {"delivery_id": identity, "directory": str(path.resolve()), "report_path": str(view.resolve()),
        "published": published, **artifacts["delivery_status"]}


def build_delivery(decision_repository, decision_run_id, output_root, *, reports_root=None, started_at=None):
    request = {"schema_version": VERSION, "decision_repository": str(Path(decision_repository).resolve()), "decision_run_id": decision_run_id}
    try:
        a = load_verified(decision_repository, decision_run_id)
        report = build_report(a, decision_run_id)
        markdown = render_report(report)
        validate_report(a, decision_run_id, report, markdown)
        status = report_status(report)
        artifacts = {"request": request, "report": report, "delivery_status": status,
            "validation": {"decision_rebuild": "passed", "report_rebuild": "passed", "schema_version": VERSION},
            "rendering": {"markdown": markdown + status_footer(status)}}
    except (ValueError, OSError, KeyError, TypeError) as error:
        status = {"report_status": "blocked", "decision_status": None, "dplan_status": "blocked", "organizer_validation": "not_run",
            "run_id": decision_run_id, "started_at": started_at,
            "blockers": [{"code": "DECISION_VALIDATION_FAILED", "stage": "decision", "message": str(error)}]}
        artifacts = {"request": request, "delivery_status": status,
            "validation": {"decision_rebuild": "failed", "report_rebuild": "not_run", "schema_version": VERSION},
            "rendering": {"markdown": diagnostic_markdown(status)}}
    return save_delivery(output_root, artifacts, reports_root, started_at)


def save_failure(output_root, message, run_id, *, reports_root=None, started_at=None):
    status = {"report_status": "failed", "decision_status": None, "dplan_status": "blocked", "organizer_validation": "not_run",
        "run_id": run_id, "started_at": started_at,
        "blockers": [{"code": "PIPELINE_FAILED", "stage": "pipeline", "message": str(message)}]}
    artifacts = {"request": {"schema_version": VERSION, "pipeline_run_id": run_id, "started_at": started_at},
        "delivery_status": status, "rendering": {"markdown": diagnostic_markdown(status)},
        "validation": {"decision_rebuild": "not_run", "report_rebuild": "not_run", "schema_version": VERSION}}
    return save_delivery(output_root, artifacts, reports_root, started_at)


def validate_delivery(output_root, delivery_id):
    artifacts = DeliveryRepository(output_root).verify_delivery(delivery_id)
    expected_fields = {"request", "delivery_status", "validation", "rendering"} | ({"report"} if "report" in artifacts else set())
    if set(artifacts) != expected_fields:
        raise DeliveryError("交付 artifact 集合不符合契約")
    if "report" not in artifacts:
        if (artifacts["delivery_status"]["report_status"] not in {"blocked", "failed"}
                or artifacts["delivery_status"]["decision_status"] is not None
                or artifacts["delivery_status"]["dplan_status"] != "blocked"):
            raise DeliveryError("診斷不得宣稱報告或決策已通過")
        if artifacts["rendering"]["markdown"] != diagnostic_markdown(artifacts["delivery_status"]):
            raise DeliveryError("診斷文字與狀態不一致")
        return {"valid": True, "decision_validated": False}
    request = artifacts["request"]
    a = load_verified(request["decision_repository"], request["decision_run_id"])
    validate_report(a, request["decision_run_id"], artifacts["report"], render_report(artifacts["report"]))
    if artifacts["delivery_status"] != report_status(artifacts["report"]):
        raise DeliveryError("交付狀態與決策重建不一致")
    if artifacts["validation"] != {"decision_rebuild": "passed", "report_rebuild": "passed", "schema_version": VERSION}:
        raise DeliveryError("交付驗證欄位遭修改")
    if artifacts["rendering"]["markdown"] != render_report(artifacts["report"]) + status_footer(artifacts["delivery_status"]):
        raise DeliveryError("交付閱讀版與重建不一致")
    return {"valid": True, "decision_validated": True}
