"""優質股 v3（position_sizing.agent_swap.quality_dip version=v3）：組數與份量由交易 Agent 提出、風險 Agent 裁決。

設計見 docs/quality_dip_agent_sizing_plan.md。覆蓋：

1. 閘門（Policy 驗證）：v3 形狀、組數另計（不受 agent_swap.max_swaps_per_run 限制）、需 agent_swap v2、ruling_ref／rulings 只能由 pipeline 綁定、
   nominated_pairs 的 size 與對數檢查；v1／v2 逐位元維持原行為。
2. 純函式：份量格降一格、權重上限（Decimal、不高於硬上限）、程式前置檢查、v3 綁定。
3. 換股提名（5c）：quality_dip 組數另計、size 必填且只用於 quality_dip、quality_dip_decline 的必填與禁填、封存還原、v1／v2 的事實與簡報不變。
4. 裁決：結構檢查（涵蓋、順序、small 不能再降、reason_kind、證據、防守看法的 defensive_reconciliation、背景識別碼、數字）、
   綁定（提名與現金姿態雜湊）、程式封存狀態與前置檢查重算、Policy 綁定、team_inputs 重建。
5. 配置：份量格×裁決對成權重上限、reject 與未完成裁決一律取消且換出者不賣、組數另計、不退回 v2、成交量不高於全部 approve。
6. 正式樣板回歸：config/decision_policy.json 沒有 v3 欄位，現行 v2 設定不變。

測試用的數值（組數、權重上限）只放在測試內，沒有寫進 config。
"""

import copy
import json
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.automation.agent_runner import LATER_STAGE_GLOBS
from etf_agent.automation.agent_schemas import (
    QUALITY_DIP_RULING_SCHEMA, SWAP_NOMINATION_QUALITY_DIP_SCHEMA, SWAP_NOMINATION_QUALITY_DIP_SIZED_SCHEMA,
)
from etf_agent.decision import (
    AllocationOrderEngine, DecisionToolError, ProposalValidator, artifact_content_sha256, decision_bundle_sha256, decision_policy_sha256,
)
from etf_agent.decision.finalization import QUALITY_RULING_TEAM_FIELDS, TEAM_FIELDS_BY_VERSION, build_team_inputs
from etf_agent.decision.input_validator import EVIDENCE_POLICY_FIELD, EVIDENCE_POLICY_TYPED
from etf_agent.decision.quality_dip_ruling import (
    RULING_KEY, STATUS_COMPLETED, STATUS_EXHAUSTED, STATUS_NOT_REQUIRED, STATUS_SKIPPED, STATUS_UNAVAILABLE, QualityDipRulingValidator,
    apply_quality_dip_ruling, expected_program_status, program_ruling, quality_dip_ruling_brief, quality_pairs, ruling_envelope,
    seal_quality_dip_ruling, validate_team_quality_dip_ruling,
)
from etf_agent.decision.quality_dip_swap import (
    PURPOSE_QUALITY_DIP, SIZE_GRADES, program_skip_reason, quality_dip_swap_binding, quality_dip_swap_gate, quality_dip_v3_gate,
    size_after_ruling, size_entry_weight, validate_quality_dip_swap_gate,
)
from etf_agent.decision.swap_nomination import (
    SwapNominationValidator, needs_nomination, nomination_facts, nominations_of, seal_swap_nomination, swap_nomination_brief,
)

from test_agent_swap_allocation import PAIR, exposure_bundle, four_symbol_bundle, four_symbol_items, v2_world
from test_agent_swap_policy import agent_swap_errors, errors_for
from test_exposure_rotation_e2e import codes, sold_and_bought
from test_market_index_facts import GATE as MARKET_INDEX_GATE
from test_quality_dip_swap import (
    GATE, OVERSOLD, REASON, QualityWorld, agent_flags, bind_quality, dip_facts, order_of, quality_pair, quality_sizing,
)
from test_swap_nomination import good_output

ROOT = Path(__file__).resolve().parents[2]
# 測試用的 v3 硬上限（只放測試內，不是 config 的值）：組數 1、單檔買進權重上限 20%（large＝20%、medium＝13.33%、small＝6.67%）。
V3 = dict(GATE, version="v3", max_swaps_per_run=1, max_entry_weight="0.2")
FLOOR_OK = {"status": "available", "stance_floor": "aggressive", "drawdown": "0.01", "peak_nav": "1000000", "history_length": 5}
EVIDENCE = "market-regime:pool-breadth"


def sized_pair(size="medium", **changes):
    return quality_pair(size=size, **changes)


def sized_output(pairs=None, **changes):
    value = {
        "nominations": [sized_pair()] if pairs is None else list(pairs), "lower_conviction_review": [], "thesis_hint_responses": [],
    }
    value.update(changes)
    return value


class SizedWorld(QualityWorld):
    """quality_dip v3 的提名世界（agent_swap 沿用影子 v1 夾具：提名的結構檢查不讀 agent_swap.version）。"""

    def __init__(self, gate=None, **kwargs):
        super().__init__(gate=dict(V3 if gate is None else gate), **kwargs)

    def seal(self, raw):
        return seal_swap_nomination(self.envelope, raw, True, True)

    def validate(self, raw, prior_context=None):
        return self.validator(prior_context).validate(self.seal(raw))


# ---------------------------------------------------------------- 1. 閘門

class GateTests(unittest.TestCase):
    def problems(self, sizing):
        errors: list = []
        validate_quality_dip_swap_gate(sizing, errors)
        return errors

    def test_v3_is_valid_with_agent_swap_v2_and_its_quota_is_independent_of_the_total(self):
        self.assertEqual(self.problems(quality_sizing("v3", "v2")), [])
        # 組數另計：可以高於 agent_swap.max_swaps_per_run（夾具為 2）；v2 同一個數字會被拒絕。
        self.assertEqual(self.problems(quality_sizing("v3", "v2", max_swaps_per_run=4)), [])
        self.assertTrue(self.problems(quality_sizing("v2", "v2", max_swaps_per_run=4)))
        self.assertEqual(self.problems(quality_sizing("v3", "v2", max_swaps_per_run=0)), [])
        self.assertEqual(self.problems(quality_sizing("v3", "v2", oversold=dict(OVERSOLD))), [])
        self.assertEqual(self.problems(quality_sizing("v3", "v2", turn_assessment="turn_confirmed")), [])

    def test_v3_bad_values_and_prerequisites(self):
        for label, changes in {
            "組數超過結構性範圍": {"max_swaps_per_run": 11}, "負數": {"max_swaps_per_run": -1}, "布林": {"max_swaps_per_run": True},
            "權重 0": {"max_entry_weight": "0"}, "權重非字串": {"max_entry_weight": 0.03},
            "oversold 高於本層": {"oversold": dict(OVERSOLD, max_swaps_per_run=2)}, "turn_assessment 值域外": {"turn_assessment": "maybe"},
        }.items():
            self.assertTrue(self.problems(quality_sizing("v3", "v2", **changes)), label)
        self.assertTrue(any("agent_swap.version=v2" in e for e in self.problems(quality_sizing("v3", "v1"))))
        for field in ("max_swaps_per_run", "max_entry_weight", "min_new_conviction", "min_holding_days", "min_turn_assessment"):
            sizing = quality_sizing("v3", "v2")
            del sizing["agent_swap"]["quality_dip"][field]
            self.assertTrue(self.problems(sizing), field)  # 全部數字必填、程式不給預設
        self.assertEqual([e for e in agent_swap_errors(quality_sizing("v3", "v2")) if "quality_dip" in e], [])

    def test_ruling_binding_is_pipeline_only_paired_and_v3_only(self):
        ref = {"ruling_id": "r-1", "sha256": "b" * 64, "status": "completed"}
        self.assertEqual(self.problems(quality_sizing("v3", "v2", ruling_ref=ref, rulings=["approve", "downsize", "reject"])), [])
        self.assertEqual(self.problems(quality_sizing("v3", "v2", ruling_ref=dict(ref, status="exhausted"), rulings=[])), [])
        for label, changes in {
            "只有 ref": {"ruling_ref": ref}, "只有 rulings": {"rulings": ["approve"]}, "未知狀態": {"ruling_ref": dict(ref, status="done"), "rulings": []},
            "缺欄位": {"ruling_ref": {"ruling_id": "r-1"}, "rulings": []}, "裁決值域外": {"ruling_ref": ref, "rulings": ["upgrade"]},
            "未完成卻有裁決": {"ruling_ref": dict(ref, status="unavailable"), "rulings": ["approve"]},
        }.items():
            self.assertTrue(self.problems(quality_sizing("v3", "v2", **changes)), label)
        for version in ("v1", "v2"):  # v1／v2 不得有裁決綁定
            self.assertTrue(self.problems(quality_sizing(version, "v2", ruling_ref=ref, rulings=[])), version)

    def test_nominated_pairs_carry_sizes_only_for_quality_dip_and_only_in_v3(self):
        ref = {"nomination_id": "n", "sha256": "a" * 64, "status": "completed"}

        def errors(version, pairs, **changes):
            sizing = quality_sizing(version, "v2", **changes)
            sizing["agent_swap"].update(nomination_ref=ref, nominated_pairs=pairs)
            return [e for e in agent_swap_errors(sizing) if "nominated_pairs" in e]

        qd = {"buy_symbol": "1101.TW", "sell_symbol": "2330.TW", "purpose": "quality_dip"}
        th = {"buy_symbol": "2412.TW", "sell_symbol": "2317.TW", "purpose": "thesis"}
        self.assertEqual(errors("v3", [dict(qd, size="medium"), th]), [])
        self.assertTrue(errors("v3", [qd, th]))  # quality_dip 缺 size
        self.assertTrue(errors("v3", [dict(qd, size="huge"), th]))
        self.assertTrue(errors("v3", [dict(qd, size="small"), dict(th, size="small")]))  # thesis 不得有 size
        self.assertTrue(errors("v2", [dict(qd, size="medium"), th]))  # v2 不得有 size
        self.assertEqual(errors("v2", [qd, th]), [])

    def test_the_pair_count_is_split_between_the_total_and_the_quality_quota_in_v3(self):
        ref = {"nomination_id": "n", "sha256": "a" * 64, "status": "completed"}

        def errors(version, pairs, **changes):
            sizing = quality_sizing(version, "v2", **changes)
            sizing["agent_swap"].update(nomination_ref=ref, nominated_pairs=pairs)
            return [e for e in agent_swap_errors(sizing) if "nominated_pairs" in e]

        def pair(index, purpose):
            row = {"buy_symbol": "B%d.TW" % index, "sell_symbol": "S%d.TW" % index, "purpose": purpose}
            return dict(row, size="small") if purpose == "quality_dip" else row

        two_thesis = [pair(1, "thesis"), pair(2, "thesis")]  # agent_swap.max_swaps_per_run 夾具為 2
        self.assertEqual(errors("v3", two_thesis + [pair(3, "quality_dip")]), [])  # 第三組是 quality_dip，另計
        self.assertTrue(errors("v3", two_thesis + [pair(3, "thesis")]))  # 其餘配對仍受總上限
        self.assertTrue(errors("v3", [pair(1, "quality_dip"), pair(2, "quality_dip")]))  # quality_dip 組數上限為 1
        self.assertTrue(errors("v2", two_thesis + [pair(3, "quality_dip")]))  # v2 仍合計


# ---------------------------------------------------------------- 2. 純函式

class HelperTests(unittest.TestCase):
    def test_size_after_ruling_can_only_shrink(self):
        table = {
            ("large", "approve"): "large", ("large", "downsize"): "medium", ("large", "reject"): None,
            ("medium", "approve"): "medium", ("medium", "downsize"): "small", ("medium", "reject"): None,
            ("small", "approve"): "small", ("small", "reject"): None,
        }
        for (size, ruling), expected in table.items():
            self.assertEqual(size_after_ruling(size, ruling), expected, (size, ruling))
            if expected is not None:  # 任何裁決都不會比原份量格大
                self.assertLessEqual(SIZE_GRADES.index(expected), SIZE_GRADES.index(size))
        for bad in (("small", "downsize"), ("huge", "approve"), ("large", "upgrade")):
            with self.assertRaises(DecisionToolError):
                size_after_ruling(*bad)

    def test_the_entry_weight_per_grade_is_a_decimal_fraction_never_above_the_cap(self):
        base = Decimal("0.03")
        self.assertEqual(size_entry_weight(base, "large"), base)
        self.assertEqual(size_entry_weight(base, "medium"), Decimal("0.02"))
        self.assertEqual(size_entry_weight(base, "small"), Decimal("0.01"))
        weights = [size_entry_weight(Decimal("0.2"), size) for size in ("small", "medium", "large")]
        self.assertEqual(weights, sorted(weights))
        self.assertTrue(all(isinstance(weight, Decimal) and weight <= Decimal("0.2") for weight in weights))

    def policy(self, floor=None, turn="turn_unconfirmed", **gate):
        quality = dict(V3, turn_assessment=turn, **gate)
        return {"position_sizing": {"agent_swap": {"quality_dip": quality}}, "drawdown_control": floor if floor is not None else dict(FLOOR_OK)}

    def test_program_skip_reason_mirrors_the_allocation_preconditions(self):
        self.assertIsNone(program_skip_reason(self.policy()))
        self.assertEqual(program_skip_reason(self.policy(dict(FLOOR_OK, stance_floor="defensive"))), "quality_dip_stance_floor")
        unavailable = {"status": "unavailable", "stance_floor": None, "drawdown": None, "peak_nav": None, "history_length": 0}
        self.assertEqual(program_skip_reason(self.policy(unavailable)), "quality_dip_drawdown_unavailable")
        self.assertEqual(program_skip_reason(self.policy(turn="no_turn_evidence")), "quality_dip_turn")
        self.assertEqual(program_skip_reason(self.policy(turn="turn_unconfirmed", min_turn_assessment="turn_confirmed")), "quality_dip_turn")
        for broken in (self.policy({"status": "mystery"}), self.policy(turn="maybe"), {"position_sizing": {}}):
            with self.assertRaises(DecisionToolError):  # 缺資料一律拋錯，不靜默當成通過
                program_skip_reason(broken)

    def sizing(self, **gate):
        return {"agent_swap": {
            "version": "v2", "max_swaps_per_run": 2, "min_holding_days": 5, "exposure": {"version": "v1"},
            "quality_dip": dict(V3, turn_assessment="turn_unconfirmed", **gate),
            "nominated_pairs": [
                {"buy_symbol": "1101.TW", "sell_symbol": "2330.TW", "purpose": "quality_dip", "size": "large"},
                {"buy_symbol": "2412.TW", "sell_symbol": "2317.TW", "purpose": "thesis"},
                {"buy_symbol": "2603.TW", "sell_symbol": "2881.TW", "purpose": "quality_dip", "size": "small"},
            ],
            "nomination_ref": {"nomination_id": "n", "sha256": "a" * 64, "status": "completed"},
        }}

    def test_v3_binding_aligns_sizes_and_rulings_with_the_pairs(self):
        ref = {"ruling_id": "r", "sha256": "b" * 64, "status": "completed"}
        bound = quality_dip_swap_binding(self.sizing(ruling_ref=ref, rulings=["downsize", "reject"]))
        self.assertEqual(bound["version"], "v3")
        self.assertEqual(bound["sizes"], ["large", None, "small"])
        self.assertEqual(bound["rulings"], ["downsize", None, "reject"])
        self.assertEqual((bound["cap"], bound["entry_weight"]), (1, Decimal("0.2")))
        self.assertTrue(quality_dip_v3_gate(self.sizing()))
        self.assertIsNone(quality_dip_v3_gate({"agent_swap": {"quality_dip": dict(V3, version="v2")}}))

    def test_v3_binding_refuses_anything_unbound_or_inconsistent(self):
        ref = {"ruling_id": "r", "sha256": "b" * 64, "status": "completed"}
        with self.assertRaises(DecisionToolError):
            quality_dip_swap_binding(self.sizing())  # 沒綁裁決：不靜默退回 v2
        with self.assertRaises(DecisionToolError):
            quality_dip_swap_binding(self.sizing(ruling_ref=ref, rulings=["approve"]))  # 筆數與 quality_dip 配對數不符
        with self.assertRaises(DecisionToolError):
            quality_dip_swap_binding(self.sizing(ruling_ref=dict(ref, status="not_required"), rulings=[]))  # 有配對卻說不需要裁決
        sizing = self.sizing(ruling_ref=ref, rulings=["approve", "approve"])
        del sizing["agent_swap"]["nominated_pairs"][0]["size"]
        with self.assertRaises(DecisionToolError):
            quality_dip_swap_binding(sizing)  # quality_dip 配對缺份量格
        degraded = quality_dip_swap_binding(self.sizing(ruling_ref=dict(ref, status="exhausted"), rulings=[]))
        self.assertEqual((degraded["ruling_status"], degraded["rulings"]), ("exhausted", [None, None, None]))


# ---------------------------------------------------------------- 3. 換股提名（5c）

class NominationTests(unittest.TestCase):
    def assert_rejected(self, world, raw, text):
        errors = world.validate(raw)
        self.assertTrue(any(text in error for error in errors), errors)

    def test_a_sized_quality_dip_pair_passes_and_size_is_kept_in_the_seal(self):
        world = SizedWorld()
        self.assertEqual(world.validate(sized_output()), [])
        self.assertEqual(world.seal(sized_output())["nominations"][0]["size"], "medium")

    def test_size_is_required_for_quality_dip_and_forbidden_elsewhere(self):
        world = SizedWorld()
        self.assert_rejected(world, sized_output([quality_pair()]), "size 在 purpose=quality_dip 必須是")
        self.assert_rejected(world, sized_output([sized_pair("huge")]), "size 在 purpose=quality_dip 必須是")
        thesis = dict(good_output()["nominations"][0], purpose="thesis", size="small")
        self.assert_rejected(world, sized_output([thesis]), "size 只能用於 purpose=quality_dip")

    def test_the_strict_schema_placeholders_are_restored_to_absence(self):
        world = SizedWorld()
        thesis = dict(good_output()["nominations"][0], purpose="thesis", size="")
        sealed = world.seal(sized_output(
            [thesis], quality_dip_decline={"reason_kind": "not_applicable", "reason": ""}, no_nomination_reason=""))
        self.assertNotIn("size", sealed["nominations"][0])
        self.assertNotIn("quality_dip_decline", sealed)
        errors = world.validator().validate(sealed)  # 有可提名目標又沒列 quality_dip 配對：只剩「必須填 decline」這一項
        self.assertEqual(len(errors), 1)
        self.assertIn("quality_dip_decline", errors[0])
        kept = world.seal(sized_output([sized_pair()], quality_dip_decline={"reason_kind": "market_not_ready", "reason": "x"}))
        self.assertIn("quality_dip_decline", kept)  # 非空值原樣保留，交給 Validator（有配對時不得填）

    def test_the_decline_is_required_only_when_targets_exist_and_no_quality_dip_pair_is_listed(self):
        world = SizedWorld()
        facts = world.facts()
        self.assertEqual((facts["quality_dip_nominable_buys"], facts["quality_dip_nominable_sells"]), (["1101.TW"], ["2330.TW"]))
        thesis = dict(good_output()["nominations"][0], purpose="thesis")
        self.assert_rejected(world, sized_output([thesis]), "必須填 quality_dip_decline")
        decline = {"reason_kind": "prefer_thesis_pairs", "reason": "同一對用 thesis 的依據較完整。"}
        self.assertEqual(world.validate(sized_output([thesis], quality_dip_decline=decline)), [])
        for bad, text in (
            ({"reason_kind": "other", "reason": "x"}, "reason_kind 必須是"), ({"reason_kind": "market_not_ready", "reason": " "}, "reason 必須是非空字串"),
            ({"reason_kind": "market_not_ready", "reason": "x", "extra": 1}, "含未允許欄位"), ("not an object", "必須是物件"),
        ):
            self.assert_rejected(world, sized_output([thesis], quality_dip_decline=bad), text)
        self.assert_rejected(world, sized_output(quality_dip_decline=decline), "只在")  # 有 quality_dip 配對時不得填
        no_targets = SizedWorld(facts=dip_facts(candidate=None, pattern="not_declining"))
        self.assertEqual(no_targets.facts()["quality_dip_nominable_buys"], [])
        self.assert_rejected(no_targets, sized_output([thesis], quality_dip_decline=decline), "只在")
        self.assertEqual(no_targets.validate(sized_output([thesis])), [])

    def test_the_decline_text_may_not_cite_background_identifiers(self):
        world = SizedWorld()
        thesis = dict(good_output()["nominations"][0], purpose="thesis")
        decline = {"reason_kind": "market_not_ready", "reason": "依 dip-facts:abc123 的標示"}
        self.assert_rejected(world, sized_output([thesis], quality_dip_decline=decline), "不得引用前次決策背景")

    def test_the_quality_quota_is_counted_apart_from_the_total(self):
        world = SizedWorld()
        world.policy["position_sizing"]["rotation"]["max_swaps_per_run"] = 0  # 其餘配對的名額為 0
        facts = world.facts()
        self.assertEqual((facts["pair_cap"], facts["quality_dip_pair_cap"]), (0, 1))
        self.assertTrue(needs_nomination(facts))  # 只有 quality_dip 名額時仍要呼叫 Agent
        self.assertEqual(world.validate(sized_output()), [])  # quality_dip 配對不受其餘名額牽動
        thesis = dict(good_output()["nominations"][0], purpose="thesis")
        self.assert_rejected(world, sized_output([thesis]), "不含 quality_dip）1 超過本次上限 0")
        # v2 同一個世界：名額合計，quality_dip 配對被 pair_cap=0 擋下，也不需要呼叫 Agent。
        legacy = QualityWorld()
        legacy.policy["position_sizing"]["rotation"]["max_swaps_per_run"] = 0
        self.assertFalse(needs_nomination(legacy.facts()))
        self.assertNotIn("quality_dip_pair_cap", legacy.facts())

    def test_more_quality_dip_pairs_than_the_quota_are_rejected(self):
        world = SizedWorld()
        second = sized_pair(buy_symbol="2317.TW", sell_symbol="1101.TW")
        errors = world.validate(sized_output([sized_pair(), second]))
        self.assertTrue(any("quality_dip 提名對數 2 超過本次上限 1" in error for error in errors), errors)

    def test_v1_and_v2_reject_size_and_the_decline_and_their_facts_and_brief_are_unchanged(self):
        for version in ("v1", "v2"):
            world = QualityWorld(gate=dict(GATE, version=version))
            self.assertTrue(any("size" in error for error in world.validate(sized_output())), version)
            sealed = seal_swap_nomination(world.envelope, sized_output(quality_dip_decline={"reason_kind": "market_not_ready", "reason": "x"}), True)
            self.assertTrue(any("quality_dip_decline" in error for error in world.validator().validate(sealed)), version)
            self.assertEqual(world.validate(sized_output([quality_pair()])), [] if version else None)
            for key in ("quality_dip_pair_cap", "quality_dip_nominable_buys", "quality_dip_nominable_sells"):
                self.assertNotIn(key, world.facts())
            self.assertEqual(set(world.brief()["quality_dip_swap"]), {"note", "oversold_enabled"})

    def test_the_v3_brief_adds_the_grades_and_the_quota_but_no_percentages(self):
        brief = SizedWorld().brief()
        block = brief["quality_dip_swap"]
        self.assertEqual(set(block), {"note", "oversold_enabled", "max_quality_dip_pairs", "size_grades"})
        self.assertEqual((block["max_quality_dip_pairs"], set(block["size_grades"])), (1, set(SIZE_GRADES)))
        text = json.dumps(brief, ensure_ascii=False)
        self.assertNotIn("0.2", text.replace("0.20", ""))  # 不顯示硬上限的權重數字
        self.assertNotIn("max_entry_weight", text)
        legacy = QualityWorld(gate=dict(GATE, version="v2")).brief()
        self.assertEqual({key: value for key, value in brief.items() if key != "quality_dip_swap"},
                         {key: value for key, value in legacy.items() if key != "quality_dip_swap"})

    def test_program_sealed_nominations_may_not_carry_a_decline(self):
        from etf_agent.decision.swap_nomination import STATUS_EXHAUSTED as NOMINATION_EXHAUSTED, program_nomination

        world = SizedWorld()
        sealed = dict(program_nomination(world.envelope, NOMINATION_EXHAUSTED, ["x"], True))
        sealed["quality_dip_decline"] = {"reason_kind": "market_not_ready", "reason": "x"}
        sealed["content_sha256"] = artifact_content_sha256({k: v for k, v in sealed.items() if k != "content_sha256"})
        self.assertTrue(any("不得有 quality_dip_decline" in e for e in world.validator().validate(sealed)))

    def test_the_agent_schemas_are_numberless_and_the_older_ones_are_unchanged(self):
        pair = SWAP_NOMINATION_QUALITY_DIP_SIZED_SCHEMA["properties"]["nominations"]["items"]
        self.assertEqual(pair["properties"]["size"], {"enum": ["small", "medium", "large", ""]})
        older = SWAP_NOMINATION_QUALITY_DIP_SCHEMA["properties"]["nominations"]["items"]
        self.assertNotIn("size", older["properties"])
        self.assertNotIn("quality_dip_decline", SWAP_NOMINATION_QUALITY_DIP_SCHEMA["properties"])
        for schema in (SWAP_NOMINATION_QUALITY_DIP_SIZED_SCHEMA, QUALITY_DIP_RULING_SCHEMA):
            self.assertNotIn('"type": "number"', json.dumps(schema))
            self.assertNotIn('"type": "integer"', json.dumps(schema))
        self.assertEqual(set(QUALITY_DIP_RULING_SCHEMA["required"]), {"rulings", "evidence_ids", "defensive_reconciliation"})
        self.assertEqual(QUALITY_DIP_RULING_SCHEMA["properties"]["rulings"]["items"]["properties"]["ruling"], {"enum": ["approve", "downsize", "reject"]})


# ---------------------------------------------------------------- 4. 裁決

def exposure_facts_fixture():
    return {
        "facts_id": "exposure-facts:fixture", "as_of": "2026-09-19", "portfolio": {"beta": "1.0"}, "sectors": {}, "up_day": {},
        "holdings": [{"symbol": "2330.TW", "sector": "半導體", "up_day_return": "0.01"}],
        "non_held": [{"symbol": "1101.TW", "beta": "0.9", "up_capture": "1.0", "down_capture": "0.8", "sector": "水泥"}],
        "data_gaps": [],
    }


def stance(level="neutral", view="start_rebuild", turn="turn_unconfirmed"):
    return {
        "level": level, "cash_grade": "middle", "breadth": "balanced", "rationale": "市場中性。", "evidence_ids": [EVIDENCE],
        "turn_assessment": turn, "exposure_view": view,
    }


def completed_nomination(pairs, nomination_id="nomination-1"):
    body = {
        "schema_version": "1.0", "nomination_id": nomination_id, "status": "completed", "nominations": pairs,
        "thesis_hint_responses": [], "errors": [], "lower_conviction_review": [],
    }
    body["content_sha256"] = artifact_content_sha256(body)
    return body


def pair_row(buy="1101.TW", sell="2330.TW", size="medium", purpose="quality_dip"):
    row = {"buy_symbol": buy, "sell_symbol": sell, "buy_basis_ids": ["bull-1101-1"], "sell_basis_ids": ["bear-2330-1"],
           "reason": REASON, "purpose": purpose}
    if purpose == "quality_dip":
        row["size"] = size
    return row


def ruling_entry(ruling="approve", reason_kind="none", buy="1101.TW", sell="2330.TW", reason="市場風險可接受。"):
    return {"buy_symbol": buy, "sell_symbol": sell, "ruling": ruling, "reason_kind": reason_kind, "reason": reason}


def ruling_output(entries=None, evidence=None, reconciliation="", **changes):
    value = {
        "rulings": [ruling_entry()] if entries is None else list(entries), "evidence_ids": [EVIDENCE] if evidence is None else evidence,
        "defensive_reconciliation": reconciliation,
    }
    value.update(changes)
    return value


class RulingWorld:
    """裁決的輸入：typed bundle（市場層級證據 market-regime:pool-breadth）、已綁定回撤與 turn 的 v3 Policy、含 quality_dip 配對的提名與現金姿態。"""

    def __init__(self, pairs=None, stance_value=None, floor=None, turn="turn_unconfirmed", prior_context=None):
        world = QualityWorld(gate=dict(V3))
        self.world = world
        bundle = world.bundle
        bundle[EVIDENCE_POLICY_FIELD] = EVIDENCE_POLICY_TYPED
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        self.bundle = bundle
        self.policy = copy.deepcopy(world.policy)
        self.policy["position_sizing"]["agent_swap"]["quality_dip"].update(turn_assessment=turn)
        self.policy["drawdown_control"] = dict(FLOOR_OK if floor is None else floor)
        self.nomination = completed_nomination([pair_row()] if pairs is None else pairs)
        self.stance = stance_value or stance()
        self.prior_context = prior_context
        self.exposure_facts = world.exposure_facts
        self.dip_facts = world.dip_facts

    @property
    def envelope(self):
        return ruling_envelope(self.bundle, self.nomination, self.stance, "ruling-1")

    def validator(self):
        return QualityDipRulingValidator(
            self.bundle, self.policy, self.nomination, self.stance, prior_context=self.prior_context,
            exposure_facts=self.exposure_facts, dip_facts=self.dip_facts)

    def seal(self, raw):
        return seal_quality_dip_ruling(self.envelope, raw)

    def validate(self, raw):
        return self.validator().validate(self.seal(raw))

    def program(self, status, errors=()):
        return program_ruling(self.envelope, status, errors)


class RulingValidatorTests(unittest.TestCase):
    def assert_rejected(self, world, raw, text):
        errors = world.validate(raw)
        self.assertTrue(any(text in error for error in errors), errors)

    def test_a_well_formed_ruling_passes_and_the_strict_schema_placeholders_are_dropped(self):
        world = RulingWorld()
        self.assertEqual(world.validate(ruling_output()), [])
        sealed = world.seal(ruling_output())
        self.assertNotIn("reason_kind", sealed["rulings"][0])  # approve 的 none 還原成缺席
        self.assertNotIn("defensive_reconciliation", sealed)
        self.assertEqual(sealed["status"], STATUS_COMPLETED)
        shrink = ruling_output([ruling_entry("downsize", "market_risk")])
        self.assertEqual(world.validate(shrink), [])
        self.assertEqual(world.validate(ruling_output([ruling_entry("reject", "timing", reason="市場尚未轉向。")])), [])

    def test_the_rulings_must_cover_every_quality_dip_pair_once_in_the_same_order(self):
        two = RulingWorld([pair_row(), pair_row("2317.TW", "1101.TW", "large")])
        a, b = ruling_entry(), ruling_entry(buy="2317.TW", sell="1101.TW")
        self.assertEqual(two.validate(ruling_output([a, b])), [])
        self.assert_rejected(two, ruling_output([b, a]), "代號與提名第 1 組")  # 順序不同
        self.assert_rejected(two, ruling_output([a]), "剛好涵蓋")  # 少一組
        self.assert_rejected(RulingWorld(), ruling_output([ruling_entry(), ruling_entry()]), "剛好涵蓋")  # 多一組
        self.assert_rejected(RulingWorld(), ruling_output([ruling_entry(buy="9999.TW")]), "代號與提名第 1 組")  # 代號不符
        self.assert_rejected(RulingWorld(), ruling_output([ruling_entry(ruling="upgrade")]), "ruling 必須是")  # 沒有升級的選項
        # thesis 與 exposure 配對不在裁決範圍內：只裁決 quality_dip 的那一組。
        mixed = RulingWorld([pair_row(purpose="thesis", buy="2412.TW", sell="2317.TW"), pair_row()])
        self.assertEqual([item["buy_symbol"] for item in quality_pairs(mixed.nomination)], ["1101.TW"])
        self.assertEqual(mixed.validate(ruling_output()), [])

    def test_a_small_pair_cannot_be_downsized(self):
        world = RulingWorld([pair_row(size="small")])
        self.assert_rejected(world, ruling_output([ruling_entry("downsize", "market_risk")]), "small 不能再降一格")
        self.assertEqual(world.validate(ruling_output([ruling_entry("reject", "market_risk")])), [])
        self.assertEqual(world.validate(ruling_output([ruling_entry("approve")])), [])

    def test_downsize_and_reject_need_a_reason_kind_and_every_entry_needs_a_reason(self):
        world = RulingWorld()
        self.assert_rejected(world, ruling_output([ruling_entry("downsize", "none")]), "reason_kind 在 downsize 必須是")
        self.assert_rejected(world, ruling_output([ruling_entry("reject", "luck")]), "reason_kind 在 reject 必須是")
        self.assert_rejected(world, ruling_output([ruling_entry("approve", "market_risk")]), "reason_kind 只用於 downsize／reject")
        self.assert_rejected(world, ruling_output([ruling_entry(reason="  ")]), "reason 必須是非空字串")
        entry = ruling_entry()
        del entry["reason"]
        self.assert_rejected(world, ruling_output([entry]), "reason 必須是非空字串")

    def test_evidence_must_exist_and_include_a_market_level_item(self):
        world = RulingWorld()
        self.assert_rejected(world, ruling_output(evidence=[]), "evidence_ids 必須是非空")
        self.assert_rejected(world, ruling_output(evidence=["no-such-evidence"]), "引用不存在")
        self.assert_rejected(world, ruling_output(evidence=["price-2330"]), "市場層級證據")
        self.assertEqual(world.validate(ruling_output(evidence=["price-2330", EVIDENCE])), [])
        untyped = RulingWorld()
        del untyped.bundle[EVIDENCE_POLICY_FIELD]
        untyped.bundle["bundle_sha256"] = decision_bundle_sha256(untyped.bundle)
        self.assert_rejected(untyped, ruling_output(), "typed_evidence_v1")

    def test_a_defensive_view_that_still_lets_a_pair_through_needs_the_reconciliation(self):
        for label, level, view in (("defensive", "defensive", "start_rebuild"), ("keep_defensive", "neutral", "keep_defensive")):
            world = RulingWorld(stance_value=stance(level, view))
            self.assert_rejected(world, ruling_output(), "defensive_reconciliation")
            self.assertEqual(world.validate(ruling_output(reconciliation="防守是組合層級，這組是同賣款的小額換股。")), [], label)
            self.assertEqual(world.validate(ruling_output([ruling_entry("downsize", "market_risk")], reconciliation="降一格後才放行。")), [], label)
            # 全部 reject 時不得再填；程式不因防守看法自動取消任何一組，所以全部 reject 也只是 Agent 的選擇。
            reject_all = ruling_output([ruling_entry("reject", "market_risk")])
            self.assertEqual(world.validate(reject_all), [], label)
            self.assert_rejected(world, dict(reject_all, defensive_reconciliation="不需要"), "只在防守看法下仍放行")
        calm = RulingWorld()
        self.assert_rejected(calm, ruling_output(reconciliation="多餘"), "只在防守看法下仍放行")

    def test_background_keys_identifiers_and_numbers_are_refused(self):
        prior = {"context_id": "prior-ctx-1", "source": {"run_id": "decision-20260910T000000Z", "decision_id": "d-1", "intent_id": "i-1"}}
        world = RulingWorld(prior_context=prior)
        for text in ("依 exposure_facts 的組合 beta", "turn_facts 顯示反彈", "market_index_facts 回檔", "dip_background 標示", "沿用 prior-ctx-1",
                     "前次 decision-20260910T000000Z", "引用 %s" % world.exposure_facts["facts_id"], "引用 dip-facts:abc123"):
            self.assert_rejected(world, ruling_output([ruling_entry(reason=text)]), "不得引用背景區塊或前次決策識別碼")
        self.assert_rejected(RulingWorld(stance_value=stance("defensive")), ruling_output(reconciliation="依 exposure_facts"), "不得引用背景區塊")
        self.assert_rejected(world, ruling_output([dict(ruling_entry(), weight="0.1")]), "含未允許欄位")
        entry = dict(ruling_entry(), extra=5)
        self.assert_rejected(world, ruling_output([entry]), "不得輸出任何數字")
        self.assert_rejected(world, ruling_output(extra_field="x"), "含未允許欄位")

    def test_the_binding_to_the_nomination_and_the_cash_stance_is_checked(self):
        world = RulingWorld()
        sealed = world.seal(ruling_output())
        self.assertEqual(world.validator().validate(sealed), [])
        other = RulingWorld()
        other.stance = stance("defensive")  # 現金姿態被換掉：雜湊不符
        self.assertTrue(any("cash_stance_sha256" in e for e in other.validator().validate(sealed)))
        other = RulingWorld()
        other.nomination = completed_nomination([pair_row(size="large")], "nomination-2")
        errors = other.validator().validate(sealed)
        self.assertTrue(any("nomination_sha256" in e for e in errors) and any("nomination_id" in e for e in errors), errors)
        tampered = dict(sealed, rulings=[ruling_entry("reject", "timing")])
        self.assertTrue(any("content_sha256" in e for e in world.validator().validate(tampered)))

    def test_only_v3_policies_may_carry_a_ruling(self):
        world = RulingWorld()
        world.policy["position_sizing"]["agent_swap"]["quality_dip"]["version"] = "v2"
        self.assertTrue(any("version=v3" in e for e in world.validator().validate(world.seal(ruling_output()))))


class ProgramStatusTests(unittest.TestCase):
    def test_not_required_when_the_nomination_has_no_quality_dip_pair(self):
        world = RulingWorld([pair_row(purpose="thesis", buy="2412.TW", sell="2317.TW")])
        self.assertEqual(expected_program_status(world.policy, world.nomination), (STATUS_NOT_REQUIRED, None))
        self.assertEqual(world.validator().validate(world.program(STATUS_NOT_REQUIRED)), [])
        problems = world.validator().validate(world.seal(ruling_output([])))
        self.assertTrue(any("必須由程式封存 not_required" in e for e in problems), problems)  # 需要時才呼叫的反面：不需要時 Agent 輸出一律拒絕
        for status in (STATUS_SKIPPED, STATUS_EXHAUSTED):
            self.assertTrue(world.validator().validate(world.program(status, ["x"])), status)
        # 提名失敗（exhausted／unavailable）時清單為空，裁決同樣是 not_required。
        failed = RulingWorld([])
        self.assertEqual(expected_program_status(failed.policy, failed.nomination)[0], STATUS_NOT_REQUIRED)

    def test_the_program_skips_when_a_precondition_fails_and_records_the_reason_code(self):
        cases = (
            ("stance_floor", {"floor": dict(FLOOR_OK, stance_floor="defensive")}, "quality_dip_stance_floor"),
            ("drawdown", {"floor": {"status": "unavailable", "stance_floor": None, "drawdown": None, "peak_nav": None, "history_length": 0}},
             "quality_dip_drawdown_unavailable"),
            ("turn", {"turn": "no_turn_evidence"}, "quality_dip_turn"),
        )
        for label, kwargs, code in cases:
            with self.subTest(label):
                world = RulingWorld(**kwargs)
                self.assertEqual(expected_program_status(world.policy, world.nomination), (STATUS_SKIPPED, code))
                self.assertEqual(world.validator().validate(world.program(STATUS_SKIPPED, [code])), [])
                self.assertTrue(world.validator().validate(world.program(STATUS_SKIPPED, ["other"])))  # errors 必須恰為原因代碼
                self.assertTrue(world.validator().validate(world.seal(ruling_output())))  # 不得呼叫 Agent
                self.assertTrue(world.validator().validate(world.program(STATUS_EXHAUSTED, ["x"])))

    def test_skipped_cannot_be_claimed_when_the_preconditions_hold(self):
        world = RulingWorld()
        self.assertEqual(expected_program_status(world.policy, world.nomination), (None, None))
        self.assertTrue(any("不得封存 skipped_by_program" in e for e in world.validator().validate(world.program(STATUS_SKIPPED, ["x"]))))
        self.assertTrue(any("不得封存 not_required" in e for e in world.validator().validate(world.program(STATUS_NOT_REQUIRED))))

    def test_degraded_statuses_have_a_fixed_empty_shape_with_errors(self):
        world = RulingWorld()
        for status in (STATUS_EXHAUSTED, STATUS_UNAVAILABLE):
            self.assertEqual(world.validator().validate(world.program(status, ["驗證用盡"])), [], status)
            self.assertTrue(world.validator().validate(world.program(status, [])), status)  # errors 必須 1～10 筆
            sealed = dict(world.program(status, ["x"]))
            sealed["rulings"] = [ruling_entry()]
            sealed["content_sha256"] = artifact_content_sha256({k: v for k, v in sealed.items() if k != "content_sha256"})
            self.assertTrue(world.validator().validate(sealed), status)
        with self.assertRaises(DecisionToolError):
            world.program("completed")  # 程式不能封存 completed

    def test_an_unbound_policy_stops_instead_of_guessing(self):
        world = RulingWorld()
        del world.policy["position_sizing"]["agent_swap"]["quality_dip"]["turn_assessment"]
        errors = world.validator().validate(world.program(STATUS_EXHAUSTED, ["x"]))
        self.assertTrue(any("無法重算前置檢查" in e for e in errors), errors)


class BindingAndTeamTests(unittest.TestCase):
    def test_apply_binds_the_reference_and_the_rulings_in_pair_order_and_clears_prefilled_values(self):
        world = RulingWorld([pair_row(), pair_row("2317.TW", "1101.TW", "large")])
        sealed = world.seal(ruling_output([ruling_entry("downsize", "market_risk"), ruling_entry(buy="2317.TW", sell="1101.TW")]))
        policy = copy.deepcopy(world.policy)
        policy["position_sizing"]["agent_swap"]["quality_dip"].update(ruling_ref={"ruling_id": "x", "sha256": "0" * 64, "status": "completed"}, rulings=["reject"])
        bound = apply_quality_dip_ruling(policy, sealed)
        gate = bound["position_sizing"]["agent_swap"]["quality_dip"]
        self.assertEqual(gate["rulings"], ["downsize", "approve"])
        self.assertEqual(gate["ruling_ref"], {"ruling_id": "ruling-1", "sha256": sealed["content_sha256"], "status": "completed"})
        self.assertNotIn("content_sha256", bound)  # 呼叫端重算
        self.assertEqual(policy["position_sizing"]["agent_swap"]["quality_dip"]["rulings"], ["reject"])  # 不改原物件
        degraded = apply_quality_dip_ruling(world.policy, world.program(STATUS_EXHAUSTED, ["x"]))
        self.assertEqual(degraded["position_sizing"]["agent_swap"]["quality_dip"]["rulings"], [])

    def test_policies_without_v3_are_returned_unchanged(self):
        world = RulingWorld()
        for version in ("v1", "v2"):
            policy = copy.deepcopy(world.policy)
            policy["position_sizing"]["agent_swap"]["quality_dip"]["version"] = version
            self.assertEqual(apply_quality_dip_ruling(policy, world.program(STATUS_EXHAUSTED, ["x"])), policy)
        plain = {"position_sizing": {"agent_swap": {"version": "v2"}}}
        self.assertEqual(apply_quality_dip_ruling(plain, world.program(STATUS_EXHAUSTED, ["x"])), plain)

    def team(self, world, ruling):
        return {
            "swap_nomination": world.nomination, "cash_stance": world.stance, RULING_KEY: ruling,
            "exposure_facts": world.exposure_facts, "dip_facts": world.dip_facts,
        }

    def test_the_finalizer_rebuild_checks_the_ruling_and_the_policy_binding(self):
        world = RulingWorld()
        sealed = world.seal(ruling_output())
        policy = apply_quality_dip_ruling(world.policy, sealed)
        errors: list = []
        validate_team_quality_dip_ruling(world.bundle, policy, self.team(world, sealed), errors)
        self.assertEqual(errors, [])
        # Policy 綁定被改寫（裁決或參照）：拒絕。
        for field, value in (("rulings", ["reject"]), ("ruling_ref", {"ruling_id": "ruling-1", "sha256": "0" * 64, "status": "completed"})):
            forged = copy.deepcopy(policy)
            forged["position_sizing"]["agent_swap"]["quality_dip"][field] = value
            errors = []
            validate_team_quality_dip_ruling(world.bundle, forged, self.team(world, sealed), errors)
            self.assertTrue(any("與 team_inputs.quality_dip_ruling 不一致" in e for e in errors), field)
        # 封存內容被改寫：雜湊與結構都拒絕。
        errors = []
        validate_team_quality_dip_ruling(world.bundle, policy, self.team(world, dict(sealed, rulings=[ruling_entry("reject", "timing")])), errors)
        self.assertTrue(errors)
        # 缺鍵、缺提名或缺現金姿態：拒絕。
        for broken in ({k: v for k, v in self.team(world, sealed).items() if k != RULING_KEY},
                       {k: v for k, v in self.team(world, sealed).items() if k != "swap_nomination"}):
            errors = []
            validate_team_quality_dip_ruling(world.bundle, policy, broken, errors)
            self.assertTrue(errors)

    def test_a_ruling_that_is_not_completed_cannot_be_dressed_up_with_released_rulings(self):
        world = RulingWorld()
        degraded = world.program(STATUS_EXHAUSTED, ["x"])
        policy = apply_quality_dip_ruling(world.policy, degraded)
        policy["position_sizing"]["agent_swap"]["quality_dip"]["rulings"] = ["approve"]  # 沒有完成的裁決卻綁了放行
        errors: list = []
        validate_team_quality_dip_ruling(world.bundle, policy, self.team(world, degraded), errors)
        self.assertTrue(any("rulings 與 team_inputs.quality_dip_ruling 不一致" in e for e in errors), errors)

    def test_team_inputs_2_10_requires_the_nomination_and_adds_exactly_one_key(self):
        self.assertEqual(TEAM_FIELDS_BY_VERSION["2.10"], TEAM_FIELDS_BY_VERSION["2.9"] | {RULING_KEY})
        self.assertEqual(QUALITY_RULING_TEAM_FIELDS, TEAM_FIELDS_BY_VERSION["2.10"])
        from test_team_chain import team_world

        _, _, _, _, _, _, reports, research = team_world()
        from test_trader import STANCE as TEAM_STANCE
        with self.assertRaisesRegex(DecisionToolError, "必須是物件，且須與換股提名"):
            build_team_inputs(reports, research, dict(TEAM_STANCE), quality_dip_ruling={"status": "completed"})


class RulingBriefTests(unittest.TestCase):
    def brief(self, world=None):
        world = world or RulingWorld([pair_row(), pair_row(purpose="thesis", buy="2412.TW", sell="2317.TW")])
        exposure = {"items": [
            {"symbol": "2330.TW", "weight": "0.160000", "beta": "1.100000", "up_capture": "1.000000", "down_capture": "0.900000"},
            {"symbol": "2317.TW", "weight": "0.100000", "beta": "0.800000", "up_capture": "0.900000", "down_capture": "0.900000"},
        ]}
        return world, quality_dip_ruling_brief(
            world.bundle, world.world.trade, world.world.debate, exposure, world.nomination, world.stance, exposure_facts_fixture(),
            world.dip_facts, {"data_status": "available"}, {"status": "available"}, {"version": "v2"})

    def test_the_brief_holds_only_quality_dip_pairs_and_background_and_no_other_stocks_or_percentages(self):
        world, brief = self.brief()
        self.assertEqual([(row["buy_symbol"], row["sell_symbol"], row["size"]) for row in brief["pairs"]], [("1101.TW", "2330.TW", "medium")])
        row = brief["pairs"][0]
        self.assertEqual(row["buy"]["dip"]["citable"], False)
        self.assertEqual(row["sell"]["weight"], "0.160000")
        self.assertEqual(brief["cash_stance"]["exposure_view"], "start_rebuild")
        for key in ("turn_facts", "market_index_facts", "turn_state"):
            self.assertFalse(brief[key]["citable"], key)
        self.assertFalse(brief["exposure_facts"]["citable"])
        self.assertIn(EVIDENCE, brief["market_level_evidence_ids"])
        text = json.dumps(brief, ensure_ascii=False)
        for absent in ("2412.TW", "2317.TW", "max_entry_weight", "intent", "conviction_stats"):  # thesis 配對與其他股票、上限、意圖統計都不附
            self.assertNotIn(absent, text.replace("2317.TW 的", ""))
        self.assertNotIn("0.2", text.replace("0.20", "").replace("0.200", ""))
        self.assertEqual(json.loads(text)["pairs"][0]["trader_reason"], REASON)

    def test_the_brief_does_not_change_the_cash_stance_brief_contract(self):
        # 現金姿態那次的簡報完全不變：本函式與 risk_stages.run_cash_stance 是兩個獨立呼叫，簡報鍵不含 pairs。
        from etf_agent.automation import risk_stages

        source = Path(risk_stages.__file__).read_text(encoding="utf-8")
        cash_part = source.split("def run_cash_stance")[1].split("quality_dip 裁決")[0]
        self.assertNotIn("quality_dip_ruling_brief", cash_part)
        self.assertNotIn('"pairs"', cash_part)


# ---------------------------------------------------------------- 5. 配置

def bind_v3(bundle, intent, policy, size="large", ruling="approve", status="completed", gate=None, turn="turn_unconfirmed",
            purposes=(PURPOSE_QUALITY_DIP,), sizes=None, rulings=None, assessment="market_driven", agent_swap_changes=None):
    """把 purpose／size、agent_swap.exposure v1、quality_dip v3（含 turn 與裁決綁定）與前提閘門綁進已有 nominated_pairs 的 Policy，重跑配置。"""
    bound = copy.deepcopy(policy)
    sizing = bound["position_sizing"]
    swap = sizing["agent_swap"]
    sizes = list(sizes if sizes is not None else [size if purpose == PURPOSE_QUALITY_DIP else None for purpose in purposes])
    swap["nominated_pairs"] = [
        dict(pair, purpose=purpose, **({"size": pair_size} if purpose == PURPOSE_QUALITY_DIP else {}))
        for pair, purpose, pair_size in zip(swap["nominated_pairs"], purposes, sizes)
    ]
    swap["exposure"] = {"version": "v1"}
    swap.update(agent_swap_changes or {})
    quality = dict(V3 if gate is None else gate, turn_assessment=turn)
    quality["ruling_ref"] = {"ruling_id": "r-1", "sha256": "b" * 64, "status": status}
    quality["rulings"] = list(rulings if rulings is not None else ([ruling] * sum(p == PURPOSE_QUALITY_DIP for p in purposes))) if status == "completed" else []
    swap["quality_dip"] = quality
    sizing.update(exposure_view={"version": "v1"}, market_index_facts=dict(MARKET_INDEX_GATE),
                  quality_dip_facts={"version": "v1"}, quality_dip_assessment={"version": "v1"})
    bound["content_sha256"] = decision_policy_sha256(bound)
    intent = copy.deepcopy(intent)
    for item in intent["items"]:
        if item["intent"] in ("buy", "add"):
            item["dip_assessment"] = assessment
    body = AllocationOrderEngine(bundle, bound).run(intent)
    problems = ProposalValidator(bundle, bound, intent).validate(body)
    if problems:
        raise AssertionError(problems)
    return intent, bound, body


def shares(body, symbol="2317.TW"):
    order = order_of(body, symbol, "buy")
    return order["shares"] if order else 0


class AllocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = exposure_bundle()
        cls.intent, cls.policy, cls.plain, _ = v2_world(cls.bundle, pairs=[PAIR])

    def run_v3(self, **kwargs):
        return bind_v3(self.bundle, self.intent, self.policy, **kwargs)

    def test_the_size_grade_and_the_ruling_set_the_entry_weight_cap_and_the_pair_is_still_bound_by_the_proceeds(self):
        # 基準權重 20%：large＝20%、medium＝13.33%（NAV 100 萬、2317 一張 11.2 萬，仍可買一張）、small＝6.67%（不足一張，取消）。
        _, _, body = self.run_v3(size="large", ruling="approve")
        self.assertEqual(shares(body), 1000)
        self.assertEqual(sold_and_bought(body), ({"2330.TW"}, {"2317.TW"}))
        victim = next(f for f in body["constraint_flags"] if f["code"] == "AGENT_SWAP")
        self.assertIn("買進權重上限 20.00%", victim["detail"])
        self.assertIn("交易 Agent 的份量格 large、風險 Agent 裁決 approve、套用份量格 large", victim["detail"])
        sell, buy = order_of(body, "2330.TW", "sell"), order_of(body, "2317.TW", "buy")
        self.assertLessEqual(-Decimal(buy["net_cash_impact"]), Decimal(sell["net_cash_impact"]))  # 買進不超過換出賣款
        _, _, medium = self.run_v3(size="large", ruling="downsize")  # large 降一格＝medium＝13.33%
        victim = next(f for f in medium["constraint_flags"] if f["code"] == "AGENT_SWAP")
        self.assertIn("買進權重上限 13.33%", victim["detail"])
        self.assertIn("風險 Agent 裁決 downsize、套用份量格 medium", victim["detail"])
        self.assertEqual(shares(medium), 1000)
        _, _, small = self.run_v3(size="medium", ruling="downsize")  # 降成 small＝6.67% 不足一張
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:quality_dip_entry"), agent_flags(small))
        self.assertEqual(small["order_proposal"]["orders"], [])  # 換出者不賣，不會只賣不買
        _, _, tiny = self.run_v3(size="small", ruling="approve")
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:quality_dip_entry"), agent_flags(tiny))

    def test_a_reject_cancels_the_pair_and_the_victim_is_not_sold(self):
        for size in SIZE_GRADES:
            with self.subTest(size):
                _, _, body = self.run_v3(size=size, ruling="reject")
                self.assertEqual(agent_flags(body), [("2317.TW", "AGENT_SWAP_CANCELLED:quality_dip_ruling")])
                self.assertEqual(body["order_proposal"]["orders"], [])
                self.assertEqual(sold_and_bought(body), (set(), set()))

    def test_no_ruling_can_make_the_buy_larger_than_approving_everything(self):
        largest = {size: shares(self.run_v3(size=size, ruling="approve")[2]) for size in SIZE_GRADES}
        for size in SIZE_GRADES:
            for ruling in ("approve", "downsize", "reject"):
                if size == "small" and ruling == "downsize":
                    continue  # small 不能再降（Validator 與 size_after_ruling 都拒絕）
                bought = shares(self.run_v3(size=size, ruling=ruling)[2])
                self.assertLessEqual(bought, largest[size], (size, ruling))
                self.assertLessEqual(bought, largest["large"], (size, ruling))
        with self.assertRaises(DecisionToolError):
            self.run_v3(size="small", ruling="downsize")  # 配置端也不放行不合法的組合

    def test_an_unfinished_ruling_cancels_every_quality_dip_pair_and_never_falls_back_to_v2(self):
        for status, reason in (("exhausted", "quality_dip_ruling_exhausted"), ("unavailable", "quality_dip_ruling_unavailable"),
                               ("skipped_by_program", "quality_dip_ruling_skipped")):
            with self.subTest(status):
                _, _, body = self.run_v3(status=status)
                self.assertEqual(agent_flags(body), [("2317.TW", "AGENT_SWAP_CANCELLED:%s" % reason)])
                self.assertEqual(body["order_proposal"]["orders"], [])
        # 對照：同一份提名在 v2（不經裁決）會成交，v3 沒有裁決就不成交。
        _, _, v2_body = bind_quality(self.bundle, self.intent, self.policy, [PURPOSE_QUALITY_DIP], gate=dict(GATE, max_entry_weight="0.2"))
        self.assertEqual(sold_and_bought(v2_body), ({"2330.TW"}, {"2317.TW"}))

    def test_the_v2_preconditions_still_win_first_as_a_second_line_of_defence(self):
        _, _, body = self.run_v3(turn="no_turn_evidence")
        self.assertEqual(agent_flags(body), [("2317.TW", "AGENT_SWAP_CANCELLED:quality_dip_turn")])
        _, _, body = self.run_v3(status="skipped_by_program", turn="no_turn_evidence")
        self.assertEqual(agent_flags(body), [("2317.TW", "AGENT_SWAP_CANCELLED:quality_dip_turn")])  # 前置條件的原因優先
        bundle = exposure_bundle()
        intent, policy, _, _ = v2_world(bundle, pairs=[PAIR], level="defensive", grade="middle")
        bound = copy.deepcopy(policy)
        bound["drawdown_control"] = dict(bound["drawdown_control"], stance_floor="defensive")
        _, _, body = bind_v3(bundle, intent, bound)
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:quality_dip_stance_floor"), agent_flags(body))

    def test_an_unbound_or_inconsistent_ruling_is_refused_not_ignored(self):
        _, bound, _ = self.run_v3()
        unbound = copy.deepcopy(bound)
        del unbound["position_sizing"]["agent_swap"]["quality_dip"]["ruling_ref"]
        del unbound["position_sizing"]["agent_swap"]["quality_dip"]["rulings"]
        unbound["content_sha256"] = decision_policy_sha256(unbound)
        with self.assertRaises(DecisionToolError):
            AllocationOrderEngine(self.bundle, unbound).run(self.intent)
        with self.assertRaises(DecisionToolError):
            self.run_v3(rulings=[])  # 裁決筆數與 quality_dip 配對數不符

    def test_with_v2_equal_inputs_a_large_approved_v3_pair_trades_exactly_like_v2(self):
        _, _, v2_body = bind_quality(self.bundle, self.intent, self.policy, [PURPOSE_QUALITY_DIP], gate=dict(GATE, max_entry_weight="0.2"))
        _, _, v3_body = self.run_v3(size="large", ruling="approve")
        for key in ("allocation_proposal", "order_proposal"):
            self.assertEqual(v3_body[key], v2_body[key], key)
        self.assertEqual([f["code"] for f in v3_body["constraint_flags"]], [f["code"] for f in v2_body["constraint_flags"]])

    def test_the_quality_quota_does_not_use_the_total_quota_but_the_others_still_do(self):
        limits = {"max_stock_weight": "0.2", "special_weight_limits": {s: "0.2" for s in ("1101.TW", "2317.TW", "2330.TW", "2412.TW")}}
        bundle = four_symbol_bundle(cash="736000", positions={"2330.TW": 1000, "1101.TW": 1000}, **limits)
        pairs = [("2317.TW", "2330.TW"), ("2412.TW", "1101.TW")]
        intent, policy, _, _ = v2_world(bundle, four_symbol_items(), pairs, policy_fields=limits)
        total_one = {"agent_swap_changes": {"max_swaps_per_run": 1}}
        # 第一對 quality_dip（另計）、第二對 thesis：總上限 1 只約束 thesis，所以兩對都成交（v2 的合計規則下第二對會被 swap_cap 取消）。
        _, _, both = bind_v3(bundle, intent, policy, purposes=("quality_dip", "thesis"), sizes=["large", None], rulings=["approve"],
                             gate=dict(V3, max_entry_weight="0.5"), **total_one)
        self.assertEqual(sold_and_bought(both), ({"2330.TW", "1101.TW"}, {"2317.TW", "2412.TW"}))
        # 兩對都是 thesis 時總上限 1 仍照舊：第二對被 swap_cap 取消。
        # 其餘配對仍受總上限：兩對都是 thesis 而總上限 1，Policy 驗證直接拒絕（不是靜默取消）。
        with self.assertRaisesRegex(DecisionToolError, "扣除 quality_dip 配對後不得超過 max_swaps_per_run"):
            bind_v3(bundle, intent, policy, purposes=("thesis", "thesis"), sizes=[None, None], rulings=[],
                    gate=dict(V3, max_entry_weight="0.5"), **total_one)
        # quality_dip 自己的名額為 1：兩對都是 quality_dip 同樣被 Policy 驗證拒絕。
        with self.assertRaisesRegex(DecisionToolError, "quality_dip 配對不得超過"):
            bind_v3(bundle, intent, policy, purposes=("quality_dip", "quality_dip"), sizes=["large", "large"],
                    rulings=["approve", "approve"], gate=dict(V3, max_entry_weight="0.5"), **total_one)

    def test_a_rejected_pair_only_cancels_itself(self):
        limits = {"max_stock_weight": "0.2", "special_weight_limits": {s: "0.2" for s in ("1101.TW", "2317.TW", "2330.TW", "2412.TW")}}
        bundle = four_symbol_bundle(cash="736000", positions={"2330.TW": 1000, "1101.TW": 1000}, **limits)
        intent, policy, _, _ = v2_world(bundle, four_symbol_items(), [("2317.TW", "2330.TW"), ("2412.TW", "1101.TW")], policy_fields=limits)
        _, _, body = bind_v3(bundle, intent, policy, purposes=("quality_dip", "thesis"), sizes=["large", None],
                             rulings=["reject"], gate=dict(V3, max_entry_weight="0.5"))
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:quality_dip_ruling"), agent_flags(body))
        self.assertEqual(sold_and_bought(body), ({"1101.TW"}, {"2412.TW"}))  # 否決只取消該對，其餘配對照常成交

    def test_quality_dip_pairs_are_not_eligible_for_proceeds_to_add(self):
        # proceeds_to_add 需要高信心加碼持股真的買進；v3 的 quality_dip 配對即使沒有買單也不改標流向加碼，而是取消該對（換出者不賣）。
        _, bound, body = self.run_v3(size="small", ruling="approve", agent_swap_changes={"proceeds_to_add": {"version": "v1"}})
        self.assertNotIn("AGENT_SWAP_PROCEEDS_TO_ADD", codes(body))
        self.assertEqual(body["order_proposal"]["orders"], [])


# ---------------------------------------------------------------- 6. 正式樣板回歸

class ProductionTemplateTests(unittest.TestCase):
    def test_the_live_template_runs_v4_with_the_human_filled_limits(self):
        # 2026-11-11：階段 0b 由 v2 切到 v3（組數 2、單檔買進權重 0.04，超跌 1 組、0.02）；同日再切到 v4＋ladder（docs/agent_led_offense_plan.md）。
        live = json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        sizing = live["position_sizing"]
        gate = quality_dip_swap_gate(sizing)
        self.assertEqual(gate["version"], "v4")
        self.assertIsNotNone(quality_dip_v3_gate(sizing))  # v4 是 v3 的超集，份量格與裁決同 v3
        self.assertEqual(gate["ladder"], {
            "min_pool_drawdown": "0.08", "max_dip_exposure": "0.12", "max_tranches": 8,
            "min_days_between_tranches": 2, "pause_after_further_drop": "0.05", "exposure_basis": "quality_dip_fills"})  # 2026-11-11 offense-ext-1
        self.assertEqual(sizing["exposure_view"], {"version": "v2"})
        self.assertEqual(gate["min_turn_assessment"], "no_turn_evidence")  # 使用者要連跌無轉向跡象時也能抄底（回檔深度由 ladder 把關）
        for field in ("ruling_ref", "rulings", "ladder_state", "turn_assessment"):  # pipeline 綁定欄位，正式樣板人不填
            self.assertNotIn(field, gate)
        self.assertEqual({key: gate[key] for key in ("max_swaps_per_run", "max_entry_weight")}, {"max_swaps_per_run": 2, "max_entry_weight": "0.06"})
        self.assertEqual(gate["oversold"], {"max_swaps_per_run": 1, "max_entry_weight": "0.02"})
        self.assertEqual(errors_for_live(live), [])

    def test_the_live_template_pins_entry_reserve_v1_with_the_human_filled_numbers(self):
        # 2026-11-11：開啟 agent_swap.entry_reserve（policy_id strategy-2026-11-11-entry-reserve-1；回滾樣板 2026-11-11-before-entry-reserve.json）。
        live = json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        swap = live["position_sizing"]["agent_swap"]
        self.assertEqual(swap["entry_reserve"], {"version": "v1", "max_entry_weight": "0.06", "max_nominations": 8, "swap_priority": {"version": "v1"}})
        self.assertLessEqual(swap["max_swaps_per_run"], swap["entry_reserve"]["max_nominations"])
        self.assertEqual([e for e in errors_for(live["position_sizing"]) if "entry_reserve" in e], [])
        self.assertTrue((ROOT / "config" / "policy_versions" / "2026-11-11-before-entry-reserve.json").exists())

    def test_the_live_template_pins_offense_v1_with_the_human_filled_numbers(self):
        # 2026-11-11：開啟 position_sizing.offense（policy_id strategy-2026-11-11-offense-1；回滾樣板 2026-11-11-before-offense.json）。
        live = json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        sizing = live["position_sizing"]
        self.assertEqual(sizing["offense"], {
            "version": "v1",
            "by_view": {
                "probe": {"defensive_cash_buffer": "0.10", "max_exposure_pairs": 3, "max_size": "medium", "beta_stop": "0.55"},
                "start_rebuild": {"defensive_cash_buffer": "0.08", "max_exposure_pairs": 4, "max_size": "large", "beta_stop": "0.55"},
                "rebuild": {"defensive_cash_buffer": "0.08", "max_exposure_pairs": 5, "max_size": "large", "beta_stop": "0.70"},
            },
            "offense_off_drawdown": "0.12",
            "thesis_size_cap": {"version": "v1"},  # 2026-11-11 offense-ext-1（回滾樣板 2026-11-11-before-offense-extensions.json）
            "cash_glide": {"version": "v1"},
        })
        self.assertEqual(sizing["agent_swap"]["exposure"]["max_pairs_by_view"], {"probe": 3, "start_rebuild": 4, "rebuild": 5})
        for row in sizing["rebound_participation"]["by_state"].values():
            self.assertNotIn("defensive_cash_buffer", row)  # 與 offense 互斥
        self.assertEqual([e for e in errors_for(sizing) if ("offense" in e or "max_pairs_by_view" in e) and "drawdown_control" not in e], [])  # 回撤線由完整 Policy 驗證（test_offense）
        self.assertTrue((ROOT / "config" / "policy_versions" / "2026-11-11-before-offense.json").exists())

    def test_the_researchers_may_not_read_the_ruling_outputs_and_the_skill_files_exist(self):
        for pattern in ("quality_dip_ruling*", "brief_quality_dip_ruling*", "quality_dip_ruler*"):
            self.assertIn(pattern, LATER_STAGE_GLOBS)
        for path in ("tradeagent/risk/portfolio-risk-review/references/quality-dip-ruling.md",):
            self.assertTrue((ROOT / path).exists(), path)


def errors_for_live(live):
    return [e for e in errors_for(live["position_sizing"]) if "quality_dip" in e]


if __name__ == "__main__":
    unittest.main()
