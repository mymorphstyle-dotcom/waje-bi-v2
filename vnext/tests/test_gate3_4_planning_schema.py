from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SHA256_PATTERN = "^[0-9a-f]{64}$"


def load_schema(relative_path: str) -> dict[str, object]:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


class Gate34PlanningSchemaTest(unittest.TestCase):
    def test_planning_schema_is_strict_draft_2020_12(self) -> None:
        schema = load_schema("contracts/domain/planning.v1.schema.json")

        Draft202012Validator.check_schema(schema)
        self.assertEqual(
            schema["$id"],
            "urn:waje-vnext:domain:planning:v1",
        )
        self.assertEqual(
            {
                ref["$ref"].removeprefix("#/$defs/")
                for ref in schema["oneOf"]
            },
            {
                "PlanAdoptionRecord",
                "QueryBindingEnvelope",
                "ConformanceExecutionSpec",
                "LogicalExecutionAttempt",
            },
        )

        definitions = schema["$defs"]
        for record_name in (
            "PlanAdoptionRecord",
            "QueryBindingEnvelope",
            "ConformanceExecutionSpec",
            "LogicalExecutionAttempt",
        ):
            with self.subTest(record=record_name):
                record = definitions[record_name]
                self.assertFalse(record["additionalProperties"])
                self.assertEqual(
                    record["properties"]["schema_epoch"],
                    {"const": 3},
                )

    def test_planning_schema_preserves_content_addressed_ids(self) -> None:
        definitions = load_schema(
            "contracts/domain/planning.v1.schema.json"
        )["$defs"]
        scalar_fields = {
            "PlanAdoptionRecord": (
                "plan_adoption_id",
                "authority_snapshot_sha256",
                "frame_content_sha256",
                "plan_content_sha256",
                "derivation_proof_sha256",
            ),
            "QueryBindingEnvelope": (
                "query_binding_id",
                "semantic_measurement_id",
                "authority_binding_id",
                "obligation_id",
                "frame_content_sha256",
                "plan_content_sha256",
                "resolution_outcome_content_sha256",
                "obligation_content_sha256",
            ),
            "ConformanceExecutionSpec": (
                "conformance_execution_spec_id",
                "logical_execution_id",
                "obligation_id",
                "query_binding_id",
                "query_binding_content_sha256",
                "fixture_content_sha256",
            ),
            "LogicalExecutionAttempt": (
                "logical_execution_attempt_id",
                "logical_execution_id",
                "query_binding_id",
                "conformance_execution_spec_id",
                "query_binding_content_sha256",
                "execution_spec_content_sha256",
                "authority_snapshot_sha256",
            ),
        }
        for record_name, field_names in scalar_fields.items():
            for field_name in field_names:
                with self.subTest(
                    record=record_name,
                    field=field_name,
                ):
                    self.assertEqual(
                        definitions[record_name]["properties"][
                            field_name
                        ]["pattern"],
                        SHA256_PATTERN,
                    )

        adoption = definitions["PlanAdoptionRecord"]["properties"]
        for field_name in (
            "resolution_outcome_ids",
            "resolution_outcome_content_sha256s",
            "resolution_admission_content_sha256s",
            "resolution_context_sha256s",
            "resolver_input_bundle_sha256s",
            "resolution_registry_content_sha256s",
            "obligation_ids",
            "obligation_content_sha256s",
            "query_binding_ids",
            "query_binding_content_sha256s",
        ):
            with self.subTest(field=field_name):
                self.assertEqual(
                    adoption[field_name]["items"]["pattern"],
                    SHA256_PATTERN,
                )
        for field_name in (
            "resolution_outcome_ids",
            "obligation_ids",
            "query_binding_ids",
        ):
            with self.subTest(unique_identity_array=field_name):
                self.assertTrue(adoption[field_name]["uniqueItems"])
        for field_name in (
            "resolution_outcome_content_sha256s",
            "resolution_admission_content_sha256s",
            "resolution_context_sha256s",
            "resolver_input_bundle_sha256s",
            "resolution_registry_content_sha256s",
            "obligation_content_sha256s",
            "query_binding_content_sha256s",
        ):
            with self.subTest(parallel_hash_array=field_name):
                self.assertNotIn("uniqueItems", adoption[field_name])

        for field_name in (
            "query_binding_ids",
            "query_binding_content_sha256s",
        ):
            with self.subTest(optional_array=field_name):
                self.assertNotIn(
                    "minItems",
                    adoption[field_name],
                )

    def test_actions_remove_open_business_parameters(self) -> None:
        schema = load_schema("contracts/domain/actions.v3.schema.json")
        definitions = schema["$defs"]
        expected_properties = {
            "CallCapabilityPayload": {
                "task_id",
                "query_binding_id",
            },
            "RunSensitivityPayload": {
                "task_id",
                "query_binding_id",
                "sensitivity_id",
            },
            "RunProbePayload": {
                "probe_contract_ref",
                "target_authority_refs",
                "requested_output_refs",
            },
        }
        for record_name, field_names in expected_properties.items():
            with self.subTest(record=record_name):
                record = definitions[record_name]
                self.assertFalse(record["additionalProperties"])
                self.assertEqual(
                    set(record["properties"]),
                    field_names,
                )
                self.assertNotIn("parameters", record["properties"])

        injected = {
            "kind": "call_capability",
            "payload": {
                "task_id": "task-1",
                "query_binding_id": "a" * 64,
                "parameters": {
                    "window": "caller-controlled",
                },
            },
        }
        errors = tuple(
            Draft202012Validator(schema).iter_errors(injected)
        )
        self.assertTrue(errors)

    def test_authority_plan_uses_closed_obligation_bindings(self) -> None:
        definitions = load_schema(
            "contracts/domain/authority.v3.schema.json"
        )["$defs"]
        task_properties = definitions["WorkTask"]["properties"]
        plan_properties = definitions["WorkPlanRevision"]["properties"]

        self.assertTrue(
            {
                "target_estimand_ids",
                "obligation_ids",
                "query_binding_ids",
                "completion_spec_ids",
                "execution_success_policy_refs",
                "execution_degrade_policy_refs",
                "execution_stop_policy_refs",
            }.issubset(task_properties)
        )
        self.assertNotIn("target_claim_ids", task_properties)
        self.assertNotIn("success_conditions", task_properties)
        self.assertNotIn("stop_conditions", task_properties)
        self.assertIn("resolution_outcome_ids", plan_properties)


if __name__ == "__main__":
    unittest.main()
