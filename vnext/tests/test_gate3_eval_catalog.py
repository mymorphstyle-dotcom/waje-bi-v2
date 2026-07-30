from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


VNEXT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VNEXT_ROOT))

from tools.validate_gate3_eval_catalog import validate_catalog  # noqa: E402


EVAL_ROOT = VNEXT_ROOT / "evals" / "gate3"
CATALOG_PATH = EVAL_ROOT / "catalog" / "gate3-authoring-candidates.json"
LEDGER_PATH = EVAL_ROOT / "coverage-ledger.json"
CANDIDATE_ROOT = EVAL_ROOT / "candidates"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class Gate3EvaluationCatalogTests(unittest.TestCase):
    def test_policy_keeps_evaluator_truth_out_of_agent_context(self) -> None:
        policy = _load_json(EVAL_ROOT / "gate3-eval-policy.json")
        run_policy = policy["run_policy"]
        self.assertFalse(run_policy["hidden_business_truth_visible_to_agent"])
        self.assertFalse(
            run_policy["future_user_messages_visible_before_injection"]
        )
        self.assertFalse(
            run_policy["evaluator_only_conditions_visible_to_agent"]
        )
        self.assertFalse(run_policy["source_provenance_visible_to_agent"])

    def test_every_checked_in_catalog_satisfies_the_episode_contract(self) -> None:
        paths = sorted(CANDIDATE_ROOT.glob("*.json")) + [CATALOG_PATH]
        self.assertGreaterEqual(len(paths), 2)
        for path in paths:
            with self.subTest(path=path.name):
                findings, report = validate_catalog(
                    path,
                    require_policy_ready=False,
                )
                self.assertEqual([], findings)
                self.assertGreater(report["episode_count"], 0)

    def test_authoring_catalog_is_exact_union_of_candidate_sources(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        catalog_by_id = {
            episode["episode_id"]: episode
            for episode in catalog["episodes"]
        }
        candidate_episodes = [
            episode
            for path in sorted(CANDIDATE_ROOT.glob("*.json"))
            for episode in _load_json(path)["episodes"]
        ]
        candidate_by_id = {
            episode["episode_id"]: episode
            for episode in candidate_episodes
        }
        candidate_ids = list(candidate_by_id)
        catalog_ids = list(catalog_by_id)
        self.assertEqual(len(candidate_episodes), len(candidate_by_id))
        self.assertEqual(len(candidate_ids), len(set(candidate_ids)))
        self.assertEqual(set(candidate_ids), set(catalog_ids))
        self.assertEqual(len(catalog_ids), len(set(catalog_ids)))
        self.assertEqual(candidate_by_id, catalog_by_id)

    def test_authoring_checkpoint_has_behavioral_breadth(self) -> None:
        findings, report = validate_catalog(
            CATALOG_PATH,
            require_policy_ready=False,
        )
        self.assertEqual([], findings)
        self.assertGreaterEqual(report["episode_count"], 36)
        self.assertGreaterEqual(report["multi_turn_episode_count"], 12)
        self.assertGreaterEqual(report["high_or_critical_risk_episode_count"], 6)
        self.assertEqual({}, report["policy_gaps"]["missing_coverage"])
        self.assertEqual({}, report["policy_gaps"]["counterfactual_role_gaps"])

    def test_coverage_ledger_matches_catalog_and_keeps_release_gaps_visible(
        self,
    ) -> None:
        findings, report = validate_catalog(
            CATALOG_PATH,
            require_policy_ready=False,
        )
        self.assertEqual([], findings)
        self.assertEqual(report, _load_json(LEDGER_PATH))
        self.assertFalse(report["policy_ready"])
        self.assertEqual(
            {"real_user_language": 6},
            report["policy_gaps"]["source_pool_gaps"],
        )
        self.assertGreater(
            report["policy_gaps"]["fully_reviewed_episode_gap"],
            0,
        )
        self.assertGreater(
            len(report["policy_gaps"]["open_adversarial_findings"]),
            0,
        )
        self.assertGreater(
            len(report["policy_gaps"]["missing_required_artifacts"]),
            0,
        )

    def test_generated_wording_cannot_claim_real_user_provenance(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = mutated["episodes"][0]
        episode["source_pool"] = "real_user_language"
        episode["provenance"]["authoring_method"] = "expert_authored"
        episode["provenance"].pop("source_trace_ref", None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "forged-real-user-catalog.json"
            path.write_text(
                json.dumps(mutated, ensure_ascii=False),
                encoding="utf-8",
            )
            findings, _ = validate_catalog(
                path,
                require_policy_ready=False,
            )
        self.assertTrue(
            any(
                "real_user_language requires interview or trace provenance"
                in finding
                for finding in findings
            )
        )
        self.assertTrue(
            any(
                "real_user_language requires source_trace_ref" in finding
                for finding in findings
            )
        )

    def test_review_status_requires_durable_independent_attestations(self) -> None:
        catalog = _load_json(CATALOG_PATH)
        mutated = copy.deepcopy(catalog)
        episode = mutated["episodes"][0]
        episode["provenance"]["review_status"] = "fully_reviewed"
        episode["dataset_partition"] = "development"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "forged-review-catalog.json"
            path.write_text(
                json.dumps(mutated, ensure_ascii=False),
                encoding="utf-8",
            )
            findings, _ = validate_catalog(
                path,
                require_policy_ready=False,
            )
        self.assertTrue(
            any(
                "requires attestations" in finding
                for finding in findings
            )
        )


if __name__ == "__main__":
    unittest.main()
