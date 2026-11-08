"""封存 Decision run 的自動回歸（審查 G-1）：「省略閘門的舊 run 逐位元不變」從口頭承諾變成測試。

兩層：

1. 封存 run（``artifacts/decisions/<日>/decision-*``，本機才有，CI 沒有時整組略過）：
   - 已知 run 的 decision／proposal／policy ``content_sha256`` 必須等於釘在本檔的 golden（防止封存檔被重存後「自我驗證」）；
   - 以 bundle、policy、intent 重跑 ``AllocationOrderEngine``，整份 proposal 必須與封存檔逐位元相同；
   - 以 CLI ``validate-decision`` 完整重建每個 run（含 Finalizer、team_inputs、history_ref 與前次決策），全部 ``valid``。
2. 合成 fixture（永遠執行）：以同一組固定輸入套上每個歷史閘門子集（10-27 至今的 Policy 形狀），proposal 雜湊釘在
   ``HISTORICAL_SUBSET_SHA256``；閘門路徑任何無意的行為改變都會在 CI 被抓到。
"""

import copy
import json
import os
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from etf_agent.decision import AllocationOrderEngine, artifact_content_sha256

from test_gate_prerequisites import PRODUCTION_SIZING_2026_11_08, PRODUCTION_SNAPSHOT_SHA256, anchored_sizing, sized, stance
from test_position_sizing import SIZING, sized_inputs
from test_risk_limit_gates import account

ROOT = Path(__file__).resolve().parents[2]
RUNS_ROOT = ROOT / "artifacts" / "decisions"
RUN_FILES = {
    "bundle": "decision_input.json", "momentum": "momentum.json", "debate": "debate.json", "intent": "intent.json",
    "policy": "policy.json", "proposal": "proposal.json", "scenario": "scenario.json", "guard": "guard.json",
    "review": "risk_review.json", "history": "revision_history.json", "team_inputs": "team_inputs.json",
    "decision": "decision.json",
}
# 2026-11-08 盤點的 9 個封存 run：(decision, proposal, policy) 的 content_sha256。新 run 不在表中時只做重建與 validate-decision。
ARCHIVED_RUN_SHA256 = {
    "decision-20261027T000350Z": ("6f7f7cd5c43a0da3fbc5bb384af4bd840256d1ad35143d7da70287dfc6b487cc",
                                  "791ef9a65453f410e484ed83d6cda82622a25b9167dc96a5c2d17b786be281a1",
                                  "ff9b6af1a859982495380c7de3efd89b6b446ed85e5b8138e6d4769656e9cc62"),
    "decision-20261027T231239Z": ("3861acf11853803c1b882626aecf3db7fa129d73887c4ae256db2d2c5a23b232",
                                  "60d9d2de1715332b8513fd07d60f140dc8b3fa0b4eff1f8698b3d526aac487ca",
                                  "7165da91a676fe86c584a9bccd4fbf7fbd305b8c432d9b26025e6fd8e099b560"),
    "decision-20261028T230001Z": ("79ea180c1396e3fca43a9c8f99e18838ebfd980afb5b3976967c7a19d8172833",
                                  "edac7f8d42c2caefb1ead8b2c61a12a2190ed5e25a1174457110f8901135caed",
                                  "89ee208bb61727becd083b0a62394d21c8b4b7defda3f4187956f63f3137f98a"),
    "decision-20261029T230005Z": ("0fff555abae637360d6602def198555b2311159a4fcfbfd424077d26585c882e",
                                  "83c90c5a722232544911cba96e7b377c4c472fc71b70e8434005f1499d1aba33",
                                  "da302b81b41700624bf8d575ece280aa6672022b6bf83893dc97fb9e92382bf8"),
    "decision-20261101T230005Z": ("561b2fa878c5bb80cd9c708f6d41cd15a10fab38d9d8deaeb12c68a9cafd37ba",
                                  "7ab92f15fa8b6c0ceacbf17ffa76249f93d5685081c4b1973ce83e1eaaffe176",
                                  "93917079ba125037afd2ae11a00c62f1e93a9ac8572ca385747c44b7f4232e82"),
    "decision-20261103T000604Z": ("533d38a96014791a235f97727d6a655b12ae218467c0fb3ecd3a5e2a5df39da0",
                                  "471cc4453777bc624b97ab9fd1b2db0f10f6950f293411bac9fd1f68c7d77d17",
                                  "e32b4c798397c2335562fc0b5ce36ec9f4e72e8664a9b916caaf576b8ed945b6"),
    "decision-20261103T231233Z": ("b8c03541bcbab07fe12c5ba78a0befcc3bda8f44e207ec03447c5c4203b67088",
                                  "581bdee15842b2fb35ce129165533fbcea32d2e6506e1dfb8f90e89bcfe226dd",
                                  "fe286bbdfd41f5931b19c91ce3269dc5ad4f9a0f624dd9242ca02d839ba23635"),
    "decision-20261104T230000Z": ("69bc14f415af9c383342effeac17673b5e4ce2b97d8f6b01f495a001dac49521",
                                  "13675b98af4a3305e8f9d45c914637f1f9b35095a8fae3b0056243e8412d0072",
                                  "8cdceaff5fb756db3211975e08254b9a97d4688f564cbd1f29e39a949013bf4b"),
    "decision-20261105T230005Z": ("9afb99c0e0bb324a1ffa9c1e9d4e7723eea1dc963ce30fa0ded2a59fdfac841b",
                                  "2549831f09bce318aea6a05f52d1d9cc3e3c966d02de395348abc2acbcab4509",
                                  "60882fc8fb00eab560f67aa283c62b6b6a1daa79015cf66bd1be82e7ccd9d388"),
}

# 歷史閘門子集（依封存 run 的 Policy 形狀，由舊到新；參數取正式樣板值）。固定 fixture：2317 high 新買、2330 medium 加碼、
# 現金 200 萬、neutral（grid 下為 middle）。雜湊以 2026-11-08 的程式計算，當日 9 個封存 run validate-decision 全部通過。
V2 = {"method": "conviction_volatility_v2", "volatility_exponent": "0"}
ROTATION = {"version": "v1", "max_swaps_per_run": 5, "min_new_conviction": "medium"}
FORCED = {"version": "v1", "min_new_conviction": "high", "max_swaps_per_run": 2}
EXECUTION_GATES = {
    "cash_fit_order": "v1", "cash_fit_min_fill_ratio": "0.5", "cash_ceiling_fix": "v1", "stock_limit_headroom": "v1",
    "buy_gap_reserve": "v1", "buy_gap_reserve_rate": "0.03", "insufficient_funds_policy": "void_batch",
    "sector_fit": "v1", "turnover_fit": "v1", "reentry_cooldown": {"version": "v1", "days": 3},
}
HISTORICAL_SUBSETS = {
    "2026-10-27 v1+stock_weight_fit": {"stock_weight_fit": "v1"},
    "2026-10-29 v2+held_add_priority+rotation": {**V2, "stock_weight_fit": "v1", "held_add_priority": "v1", "rotation": ROTATION},
    "2026-10-30 +selection_ranking": {
        **V2, "stock_weight_fit": "v1", "held_add_priority": "v1", "rotation": ROTATION, "selection_ranking": "v1",
    },
    "2026-11-03 +cash_buffer_floor+forced_swap": {
        **V2, "stock_weight_fit": "v1", "held_add_priority": "v1", "selection_ranking": "v1", "cash_buffer_floor": "v1",
        "rotation": {**ROTATION, "forced_swap": FORCED},
    },
    "2026-11-04 +execution gates+derisk v1": {
        **V2, "stock_weight_fit": "v1", "held_add_priority": "v1", "selection_ranking": "v1", "cash_buffer_floor": "v1",
        **EXECUTION_GATES, "drawdown_derisk": {"version": "v1", "max_derisk_fraction": "0.25"},
        "rotation": {**ROTATION, "forced_swap": {**FORCED, "confirmation": "v1"}},
    },
    "2026-11-05 derisk v3": {
        **V2, "stock_weight_fit": "v1", "held_add_priority": "v1", "selection_ranking": "v1", "cash_buffer_floor": "v1",
        **EXECUTION_GATES, "drawdown_derisk": {"version": "v3", "max_derisk_fraction": "0.25"},
        "rotation": {**ROTATION, "forced_swap": {**FORCED, "confirmation": "v1"}},
    },
    "2026-11-06 +limit_scaled+cash_ratio_range": {
        **V2, "stock_weight_fit": "v1", "held_add_priority": "v1", "selection_ranking": "v1", "cash_buffer_floor": "v1",
        **EXECUTION_GATES, "drawdown_derisk": {"version": "v3", "max_derisk_fraction": "0.25"},
        "limit_scaled_conviction": {"version": "v1", "alpha": "0.6"},
        "cash_ratio_range": PRODUCTION_SIZING_2026_11_08["cash_ratio_range"],
        "rotation": {**ROTATION, "forced_swap": {**FORCED, "confirmation": "v1"}},
    },
    "2026-11-08 production snapshot": {
        key: value for key, value in PRODUCTION_SIZING_2026_11_08.items()
        if key not in ("conviction_multipliers", "volatility_floor", "cash_buffer_by_stance")
    },
}
HISTORICAL_SUBSET_SHA256 = {
    "2026-10-27 v1+stock_weight_fit": "ccc9cdaabb7455cfce3a237de710dc3e9bbd5ada98d347add0ade69b6f00df4e",
    "2026-10-29 v2+held_add_priority+rotation": "cbbc734dee3761e279a29ab29458e879e7f2191212e938a46b745fd86f6e85cd",
    "2026-10-30 +selection_ranking": "59ff8069fc42f4d1c71602567f977900918043231f9299480e0ca664fcc42992",
    "2026-11-03 +cash_buffer_floor+forced_swap": "62b9392ac3dde03c1e6725e717ad3cf67375845f02d16a31b93a88ec88ef0e38",
    "2026-11-04 +execution gates+derisk v1": "ad8918b0a1fa3f9942d76bd639eae90f821066a3c445e19c0742e2d33c9dea95",
    "2026-11-05 derisk v3": "1f43fd94d7e3d6beb896f01055619899f8537d4dfd357e4e6bf0b5f56ea06a27",
    "2026-11-06 +limit_scaled+cash_ratio_range": "f3d81dd138dca61240f86af307625baf23a0ba283d25618fdc5f3438f6ad4fea",
    "2026-11-08 production snapshot": PRODUCTION_SNAPSHOT_SHA256,
}


def archived_runs():
    if not RUNS_ROOT.is_dir():
        return []
    runs = []
    for day in sorted(RUNS_ROOT.iterdir()):
        if not day.is_dir():
            continue
        for run in sorted(day.glob("decision-*")):
            if run.is_dir() and all((run / name).is_file() for name in RUN_FILES.values()):
                runs.append(run)
    return runs


def read(run, name):
    return json.loads((run / RUN_FILES[name]).read_text(encoding="utf-8"))


def validate_decision_cli(run):
    """同一個解譯器以子程序跑 CLI validate-decision（決策庫與帳本用 CLI 預設：artifacts/decisions、artifacts/ledger）。"""
    argv = [sys.executable, str(ROOT / "cli" / "portfolio_decision.py"), "--bundle", str(run / RUN_FILES["bundle"]), "validate-decision"]
    for name in ("momentum", "debate", "intent", "policy", "proposal", "scenario", "guard", "review", "history"):
        argv += ["--" + name, str(run / RUN_FILES[name])]
    argv += ["--team-inputs", str(run / RUN_FILES["team_inputs"]), "--input", str(run / RUN_FILES["decision"])]
    completed = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True, env=dict(os.environ), check=False)
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        payload = {"valid": False, "errors": ["輸出不是 JSON：%s %s" % (completed.stdout[-400:], completed.stderr[-400:])]}
    return completed.returncode, payload


@unittest.skipUnless(archived_runs(), "沒有封存 Decision run（artifacts/decisions），略過封存回歸")
class ArchivedRunGoldenTests(unittest.TestCase):
    """封存檔本身與配置層重建：每個 run 約 1 秒。"""

    def test_known_runs_keep_their_sealed_hashes(self):
        seen = set()
        for run in archived_runs():
            expected = ARCHIVED_RUN_SHA256.get(run.name)
            if expected is None:
                continue
            seen.add(run.name)
            with self.subTest(run=run.name):
                decision, proposal, policy = read(run, "decision"), read(run, "proposal"), read(run, "policy")
                self.assertEqual((decision["content_sha256"], proposal["content_sha256"], policy["content_sha256"]), expected)
                # 封存的 proposal 內容與自身雜湊一致（不是只有雜湊欄位沒動）。
                self.assertEqual(artifact_content_sha256(proposal), proposal["content_sha256"])
        self.assertTrue(seen, "封存目錄沒有任何已知 run")

    def test_proposal_rebuilds_bitwise_from_the_archived_inputs(self):
        for run in archived_runs():
            with self.subTest(run=run.name):
                bundle, policy, intent, proposal = read(run, "bundle"), read(run, "policy"), read(run, "intent"), read(run, "proposal")
                rebuilt = AllocationOrderEngine(bundle, policy).run(intent)
                self.assertEqual(rebuilt, proposal)


@unittest.skipUnless(archived_runs(), "沒有封存 Decision run（artifacts/decisions），略過封存回歸")
class ArchivedRunValidateDecisionTests(unittest.TestCase):
    """完整重建：Finalizer、team_inputs、名額、持股檢視、回撤峰值（帳本）、前次決策與現金 anchor（決策庫）。

    每個 run 約 8 秒，以子程序平行執行；任何一個 run 不 valid 代表「舊 run 逐位元不變」被破壞。"""

    def test_every_archived_run_is_valid_under_the_current_code(self):
        runs = archived_runs()
        workers = max(1, min(len(runs), os.cpu_count() or 2))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(validate_decision_cli, runs))
        for run, (code, payload) in zip(runs, results):
            with self.subTest(run=run.name):
                self.assertEqual(code, 0, payload)
                self.assertEqual(payload, {"valid": True, "errors": []})


class HistoricalGateSubsetTests(unittest.TestCase):
    """合成 fixture 上的歷史閘門子集 golden：不依賴 artifacts/，CI 也會跑。"""

    def world(self, subset):
        bundle, intent, _, policy = sized_inputs("high", "medium", "neutral")
        bundle = account(bundle, "2000000", 1000)
        sizing = {**copy.deepcopy(SIZING), **copy.deepcopy(subset)}
        if "method" in subset:
            sizing["conviction_multipliers"] = PRODUCTION_SIZING_2026_11_08["conviction_multipliers"]
        if "cash_ratio_range" in sizing:
            sizing = anchored_sizing(bundle, sizing)
        stance_obj = stance("neutral", "middle", "balanced")
        if "cash_ratio_range" not in sizing:
            stance_obj.pop("cash_grade")
        if "positions_by_breadth" not in sizing:
            stance_obj.pop("breadth")
        return sized(bundle, intent, stance_obj, sizing, policy)

    def test_each_historical_subset_rebuilds_its_pinned_hash(self):
        mismatches = {}
        for name, subset in HISTORICAL_SUBSETS.items():
            _, body = self.world(subset)
            if body["content_sha256"] != HISTORICAL_SUBSET_SHA256[name]:
                mismatches[name] = body["content_sha256"]
        self.assertEqual(mismatches, {})

    def test_subsets_are_monotone_in_gate_count_and_distinct(self):
        names = list(HISTORICAL_SUBSETS)
        for earlier, later in zip(names, names[1:]):
            self.assertLessEqual(len(HISTORICAL_SUBSETS[earlier]), len(HISTORICAL_SUBSETS[later]), (earlier, later))
        self.assertEqual(len(set(HISTORICAL_SUBSET_SHA256.values())), len(HISTORICAL_SUBSET_SHA256))


if __name__ == "__main__":
    unittest.main()
