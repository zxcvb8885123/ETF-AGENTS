"""Application facade for the portfolio-decision Skill CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Mapping, Optional

from .contracts import (
    DecisionInputValidator,
    DecisionToolError,
    artifact_content_sha256,
)
from .momentum import MomentumEngine, MomentumResultValidator
from .trade_intent import (
    BuyIntentPacketValidator,
    SellIntentPacketValidator,
    TradeDebateValidator,
    TradeIntentResultValidator,
    build_role_input_artifact,
)


class PortfolioDecisionApplicationService:
    """Read one input bundle and expose deterministic P0-P2 operations."""

    def __init__(self, bundle: Mapping[str, object]):
        self.bundle = dict(bundle)

    @classmethod
    def from_path(cls, path: Path) -> "PortfolioDecisionApplicationService":
        return cls(cls.read_json(path, " DecisionInputBundle"))

    @staticmethod
    def read_json(path: Path, label: str) -> Mapping[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DecisionToolError("無法讀取%s：%s" % (label, error)) from error
        if not isinstance(payload, Mapping):
            raise DecisionToolError("%s必須是 JSON 物件" % label)
        return payload

    @staticmethod
    def _write(payload: Mapping[str, object], output: Optional[Path]) -> None:
        if output is None:
            return
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def validate_input(self) -> Dict[str, object]:
        errors = DecisionInputValidator(self.bundle).validate()
        return {"valid": not errors, "errors": errors}

    def compute_momentum(self, output: Optional[Path] = None) -> Dict[str, object]:
        result = MomentumEngine(self.bundle).run()
        self._write(result, output)
        return result

    def build_role_input(
        self,
        role: str,
        momentum_path: Path,
        output: Optional[Path] = None,
    ) -> Dict[str, object]:
        momentum = self.read_json(momentum_path, " MomentumResult")
        result = build_role_input_artifact(self.bundle, momentum, role)
        self._write(result, output)
        return result

    def seal_artifact_file(
        self, input_path: Path, output: Optional[Path] = None
    ) -> Dict[str, object]:
        artifact = dict(self.read_json(input_path, " decision artifact"))
        artifact["content_sha256"] = artifact_content_sha256(artifact)
        self._write(artifact, output)
        return artifact

    def validate_momentum_file(self, path: Path) -> Dict[str, object]:
        result = self.read_json(path, " MomentumResult")
        errors = MomentumResultValidator(self.bundle).validate(result)
        return {"valid": not errors, "errors": errors}

    def validate_packet_file(
        self,
        role: str,
        momentum_path: Path,
        packet_path: Path,
        output: Optional[Path] = None,
    ) -> Dict[str, object]:
        momentum = self.read_json(momentum_path, " MomentumResult")
        packet = self.read_json(packet_path, " IntentPacket")
        validator = (
            BuyIntentPacketValidator(self.bundle, momentum)
            if role == "buy"
            else SellIntentPacketValidator(self.bundle, momentum)
        )
        errors = validator.validate(packet)
        response: Dict[str, object] = {"valid": not errors, "errors": errors}
        if not errors:
            self._write(packet, output)
            if output is not None:
                response["output"] = str(output)
        return response

    def validate_debate_file(
        self,
        momentum_path: Path,
        debate_path: Path,
        output: Optional[Path] = None,
    ) -> Dict[str, object]:
        momentum = self.read_json(momentum_path, " MomentumResult")
        debate = self.read_json(debate_path, " TradeDebateBundle")
        errors = TradeDebateValidator(self.bundle, momentum).validate(debate)
        response: Dict[str, object] = {"valid": not errors, "errors": errors}
        if not errors:
            self._write(debate, output)
            if output is not None:
                response["output"] = str(output)
        return response

    def validate_intent_file(
        self,
        momentum_path: Path,
        debate_path: Path,
        result_path: Path,
        output: Optional[Path] = None,
    ) -> Dict[str, object]:
        momentum = self.read_json(momentum_path, " MomentumResult")
        debate = self.read_json(debate_path, " TradeDebateBundle")
        result = self.read_json(result_path, " TradeIntentResult")
        errors = TradeIntentResultValidator(
            self.bundle, momentum, debate
        ).validate(result)
        response: Dict[str, object] = {"valid": not errors, "errors": errors}
        if not errors:
            self._write(result, output)
            if output is not None:
                response["output"] = str(output)
        return response
