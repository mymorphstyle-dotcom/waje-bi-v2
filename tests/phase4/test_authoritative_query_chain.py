from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import inspect
import unittest

from bi_agent.runtime.analysis_assets import (
    build_analysis_assets,
    build_dimension_scan_reuse_contract,
    evaluate_dimension_scan_reuse,
    reusable_dimension_scan_inputs,
)
from bi_agent.runtime.answer_package import (
    AuthorityFact,
    _authority_bound_claim_projections,
    _project_claim_from_authority,
    build_answer_package,
    reverify_answer_package_for_delivery,
    verify_answer_package,
)
from bi_agent.runtime.capability_harness import execute_capability
from bi_agent.runtime.capability_models import BudgetState, CapabilityRequest
from bi_agent.runtime.analysis_contract_compiler import compile_analysis_contract
from bi_agent.runtime.analysis_contracts import (
    QueryResultEnvelope,
    ResolvedWindow,
    query_contract_signature,
)
from bi_agent.runtime.authoritative_query_chain import (
    AuthoritativeQueryChainError,
    validate_authoritative_query_chain,
)
from bi_agent.runtime.capability_execution import bind_capability_inputs
from bi_agent.runtime.evidence_authority import (
    CompletenessRecord,
    RowsRecord,
    RuntimeEvidenceAuthority,
    _record_completeness,
    _record_query_execution,
    canonical_digest,
    canonical_result_rows_hash,
)
from bi_agent.runtime.dataset_catalog import (
    DatasetCatalog,
    DatasetSnapshot,
    build_dataset_release_authority_record,
    dataset_snapshot_release_ref,
)
from bi_agent.runtime.clickhouse_runtime import ClickHouseQueryResult
from bi_agent.runtime.query_executor import ClickHouseQueryExecutor
from bi_agent.runtime.query_audit import query_audit_refs
from bi_agent.runtime.query_completeness import validate_query_result
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry
from tests.phase4.analysis_asset_fixtures import verified_dimension_scan_asset


class AuthoritativeQueryChainTest(unittest.TestCase):
    def test_sparse_gameplay_dimension_reaches_verifier_with_baseline_only_value(self):
        context = _gameplay_authority_context(
            self.registry,
            include_sparse_baseline=True,
        )
        bound = bind_capability_inputs(
            context["plan"],
            results={context["contract"].query_contract_id: context["result"]},
            reports={context["contract"].query_contract_id: context["report"]},
            evidence_authority=context["authority"],
            runtime_registry=self.registry,
            release_resolver=context["release_resolver"],
        )
        self.assertEqual((bound.status, bound.reasons), ("ready", ()))
        rows = context["result"].rows
        totals = {
            role: sum(
                Decimal(str(row["player_bet_amount"]))
                for row in rows
                if row["window_role"] == role
            )
            for role in ("target", "baseline")
        }
        self.assertEqual(totals, {"target": Decimal("501"), "baseline": Decimal("581")})
        self.assertEqual(
            sum(
                Decimal(str(row["player_bet_amount"]))
                for row in rows
                if row["window_role"] == "baseline" and row["gameplay"] == "Poker"
            ),
            Decimal("80"),
        )
        envelope = execute_capability(
            CapabilityRequest(
                run_id="run-gameplay-sparse-official",
                accepted_graph_id="graph-gameplay-sparse-official",
                graph_version=1,
                capability_id="gameplay_activity_context",
                question_family="business_object_impact_review",
                target_claim="observed_activity",
                claim_type="observed_activity",
                metric="player_bet_amount",
                scope="Nigeria",
                time_window="target+rolling7",
                baseline={"label": "rolling7"},
                target={"label": "target"},
                grain="window_gameplay",
                filters={},
                dimensions=("gameplay",),
                contract_versions={},
                role="analyst",
                budget_state=BudgetState("ordinary", 0, 50, 100),
                llm_business_reason="Use sparse gameplay activity context.",
                params={},
                bound_input=bound,
                evidence_resolver=context["authority"],
                release_resolver=context["release_resolver"],
            )
        )
        verifier = verify_answer_package(
            draft_claims=(
                {
                    "text": "Gameplay activity was observed.",
                    "claim_strength": "observed",
                    "claim_type": "observed_activity",
                    "evidence_refs": (envelope.evidence_ref,),
                },
            ),
            evidence=(envelope.to_dict(),),
            visible_limitations=envelope.limitations,
            evidence_resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=self.registry,
            release_resolver=context["release_resolver"],
        )
        self.assertEqual(verifier["status"], "passed", verifier["errors"])

    def test_official_gameplay_and_event_chains_reach_final_verifier(self):
        contexts = (
            (
                _gameplay_authority_context(self.registry),
                "gameplay_activity_context",
                "observed_activity",
                "player_bet_amount",
                ("gameplay",),
            ),
            (
                _event_authority_context(self.registry),
                "event_evidence",
                "candidate_mechanism",
                "",
                (),
            ),
        )
        for context, capability_id, claim_type, metric, dimensions in contexts:
            with self.subTest(capability_id=capability_id):
                bound = bind_capability_inputs(
                    context["plan"],
                    results={context["contract"].query_contract_id: context["result"]},
                    reports={context["contract"].query_contract_id: context["report"]},
                    evidence_authority=context["authority"],
                    runtime_registry=self.registry,
                    release_resolver=context["release_resolver"],
                )
                self.assertEqual((bound.status, bound.reasons), ("ready", ()))
                binding = context["authority"].resolve_capability_binding(
                    bound.binding_manifest_ref
                )
                chain = validate_authoritative_query_chain(
                    binding,
                    resolver=context["authority"],
                    rows_loader=context["authority"].rows_loader,
                    runtime_registry=self.registry,
                    release_resolver=context["release_resolver"],
                )
                self.assertTrue(chain.primary_results[0].observed_schema)
                self.assertTrue(chain.primary_results[0].provider_stats["summary"])
                self.assertTrue(chain.primary_reports[0].assertion_results)
                self.assertTrue(chain.primary_reports[0].coverage_summary["snapshot_watermarks"])
                envelope = execute_capability(
                    CapabilityRequest(
                        run_id=f"run-{capability_id}-official",
                        accepted_graph_id=f"graph-{capability_id}-official",
                        graph_version=1,
                        capability_id=capability_id,
                        question_family="business_object_impact_review",
                        target_claim=claim_type,
                        claim_type=claim_type,
                        metric=metric,
                        scope="Nigeria",
                        time_window="2026-06-02",
                        baseline={},
                        target={"label": "target"},
                        grain="window",
                        filters={},
                        dimensions=dimensions,
                        contract_versions={},
                        role="analyst",
                        budget_state=BudgetState("ordinary", 0, 50, 100),
                        llm_business_reason="Use authoritative ClickHouse rows.",
                        params={},
                        bound_input=bound,
                        evidence_resolver=context["authority"],
                        release_resolver=context["release_resolver"],
                    )
                )
                verifier = verify_answer_package(
                    draft_claims=(
                        {
                            "text": "Authoritative context was observed.",
                            "claim_strength": envelope.strength,
                            "claim_type": claim_type,
                            "evidence_refs": (envelope.evidence_ref,),
                        },
                    ),
                    evidence=(envelope.to_dict(),),
                    visible_limitations=envelope.limitations,
                    evidence_resolver=context["authority"],
                    rows_loader=context["authority"].rows_loader,
                    runtime_registry=self.registry,
                    release_resolver=context["release_resolver"],
                )
                self.assertEqual(verifier["status"], "passed", verifier["errors"])

    def test_gameplay_activity_fact_cannot_be_relabelled_as_payment(self):
        fact = AuthorityFact.create(
            query_contract_ref="query:gameplay:1",
            result_ref="result:gameplay:1",
            metric_id="player_bet_amount",
            value=100,
            window_id="target_day",
            window_role="target",
            observation_key="2026-06-02",
            dimensions=(),
            grain=("window_id", "gameplay"),
            value_semantics="gameplay_activity_amount",
            display_format="number",
        )
        facts = {
            "metric_ids": ("player_bet_amount",),
            "authority_facts": (fact,),
            "authority_context_facts": (),
            "grains": (("window_id", "gameplay"),),
            "target_windows": (),
            "baseline_windows": (),
        }
        with self.assertRaisesRegex(ValueError, "claim_number_field_unbound:paid_amount"):
            _project_claim_from_authority(
                {
                    "text": "Revenue was 100.",
                    "claim_strength": "observed",
                    "claim_type": "observed_activity",
                    "evidence_refs": ("gameplay:evidence",),
                    "numbers": {"paid_amount": 100},
                },
                facts,
            )

    def test_single_metric_window_comparison_projects_generic_value_and_change_fields(self):
        facts = _comparison_authority_facts()

        projected = _project_claim_from_authority(
            {
                "text": "Use authority values.",
                "claim_strength": "observed",
                "claim_type": "comparative_change",
                "evidence_refs": ("market:evidence",),
                "numbers": {
                    "target_value": "125260",
                    "baseline_value": "216820",
                    "absolute_change": "-91560",
                    "relative_change": "-0.4222857669956646065861082926",
                },
            },
            facts,
        )

        self.assertEqual(projected["target_metric"], "active_users")
        self.assertEqual(len(set(projected["fact_refs"])), 2)
        self.assertEqual(projected["numbers"]["absolute_change"], "-91560")
        self.assertEqual(
            projected["numbers"]["relative_change"],
            "-0.4222857669956646065861082926",
        )

    def test_generic_change_projection_rejects_tampering_and_ambiguity(self):
        claim = {
            "text": "Use authority values.",
            "claim_strength": "observed",
            "claim_type": "comparative_change",
            "evidence_refs": ("market:evidence",),
            "numbers": {"relative_change": "0.5"},
        }
        with self.assertRaisesRegex(ValueError, "claim_ratio_value_mismatch"):
            _project_claim_from_authority(claim, _comparison_authority_facts())

        multi_metric = dict(_comparison_authority_facts())
        multi_metric["metric_ids"] = ("active_users", "paid_users")
        with self.assertRaisesRegex(ValueError, "claim_number_field_unbound"):
            _project_claim_from_authority(claim, multi_metric)

        multiple_targets = dict(_comparison_authority_facts())
        first_target = multiple_targets["authority_facts"][0]
        multiple_targets["authority_facts"] = (
            *multiple_targets["authority_facts"],
            replace(first_target, observation_key="2026-06-03"),
        )
        with self.assertRaisesRegex(ValueError, "claim_ratio_fact_not_unique"):
            _project_claim_from_authority(claim, multiple_targets)

    def test_event_claim_projection_uses_authoritative_rows_not_typed_payload(self):
        context = _event_authority_context(self.registry)
        bound = bind_capability_inputs(
            context["plan"],
            results={context["contract"].query_contract_id: context["result"]},
            reports={context["contract"].query_contract_id: context["report"]},
            evidence_authority=context["authority"],
            runtime_registry=self.registry,
            release_resolver=context["release_resolver"],
        )
        self.assertEqual((bound.status, bound.reasons), ("ready", ()))
        envelope = execute_capability(
            CapabilityRequest(
                run_id="run-event-authority",
                accepted_graph_id="graph-event-authority",
                graph_version=1,
                capability_id="event_evidence",
                question_family="business_object_impact_review",
                target_claim="candidate_mechanism",
                claim_type="candidate_mechanism",
                metric="",
                scope="Nigeria",
                time_window="2026-06-02",
                baseline={},
                target={"label": "target"},
                grain="event_interval",
                filters={},
                dimensions=(),
                contract_versions={},
                role="analyst",
                budget_state=BudgetState("ordinary", 0, 50, 100),
                llm_business_reason="Use reviewed event rows.",
                params={"rows": ({"event_id": "forged"},)},
                bound_input=bound,
                evidence_resolver=context["authority"],
                release_resolver=context["release_resolver"],
            )
        )
        tampered = envelope.to_dict()
        tampered["typed_payload"] = {"events": ({"event_id": "forged"},)}
        claim = {
            "text": "Forged event caused the outcome.",
            "claim_strength": "observed",
            "claim_type": "candidate_mechanism",
            "evidence_refs": (envelope.evidence_ref,),
        }
        projected, errors = _authority_bound_claim_projections(
            claims=(claim,),
            accepted_indexes=(0,),
            evidence=(tampered,),
            evidence_resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=self.registry,
            release_resolver=context["release_resolver"],
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(projected), 1)
        self.assertIn("holiday_context", projected[0]["text"])
        self.assertIn(
            "authority=reviewed_workbook_pending_owner_review",
            projected[0]["text"],
        )
        self.assertIn("evidence_level=context", projected[0]["text"])
        self.assertNotIn("reviewed event", projected[0]["text"])
        self.assertNotIn("Forged", projected[0]["text"])
        self.assertTrue(projected[0]["fact_refs"])

    def test_release_resolver_is_explicit_across_authority_consumers(self):
        functions = (
            validate_authoritative_query_chain,
            bind_capability_inputs,
            build_answer_package,
            reverify_answer_package_for_delivery,
            verify_answer_package,
            build_analysis_assets,
            build_dimension_scan_reuse_contract,
            reusable_dimension_scan_inputs,
            evaluate_dimension_scan_reuse,
        )
        missing = tuple(
            function.__name__
            for function in functions
            if "release_resolver" not in inspect.signature(function).parameters
        )
        self.assertEqual(missing, ())

    def setUp(self):
        self.asset, self.context = verified_dimension_scan_asset(
            rows=(
                {
                    "window_id": "target_day",
                    "window_role": "target",
                    "observation_key": "2026-06-02",
                    "paid_amount": 10.0,
                    "amount": 10.0,
                    "channel": "A",
                },
            ),
            required_fields=("window_id", "amount", "channel"),
            resolved_windows={
                "target_day": {
                    "start_inclusive": "2026-06-02",
                    "end_exclusive": "2026-06-03",
                    "timezone": "Africa/Lagos",
                }
            },
        )
        self.resolver = self.context["evidence_resolver"]
        self.registry = RuntimeContractRegistry.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        self.binding = self.resolver.resolve_capability_binding(
            self.context["binding_manifest_ref"]
        )

    def test_valid_chain_recomputes_rows_and_completeness(self):
        chain = validate_authoritative_query_chain(
            self.binding,
            resolver=self.resolver,
            rows_loader=self.resolver.rows_loader,
            runtime_registry=self.registry,
        )

        self.assertEqual(chain.primary_results[0].row_count, 1)
        self.assertEqual(chain.primary_reports[0].analysis_readiness, "ready")

    def test_dashboard_release_resolver_flows_through_capability_and_final_verifier(self):
        context = _dashboard_authority_context(self.registry)

        missing_resolver = bind_capability_inputs(
            context["plan"],
            results={context["contract"].query_contract_id: context["result"]},
            reports={context["contract"].query_contract_id: context["report"]},
            evidence_authority=context["authority"],
            runtime_registry=self.registry,
        )
        self.assertEqual(missing_resolver.status, "blocked")
        self.assertTrue(
            any(
                "dataset_release_resolver_required" in reason
                for reason in missing_resolver.reasons
            ),
            missing_resolver.reasons,
        )

        bound = bind_capability_inputs(
            context["plan"],
            results={context["contract"].query_contract_id: context["result"]},
            reports={context["contract"].query_contract_id: context["report"]},
            evidence_authority=context["authority"],
            runtime_registry=self.registry,
            release_resolver=context["release_resolver"],
        )
        self.assertEqual(bound.status, "ready")
        validate_authoritative_query_chain(
            context["authority"].resolve_capability_binding(
                bound.binding_manifest_ref
            ),
            resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=self.registry,
            release_resolver=context["release_resolver"],
        )

        evidence = _evidence_from_bound_dashboard_input(bound)
        claim = {
            "text": "2026-06-02 的大盘付费金额为 100。",
            "claim_type": "comparative_change",
            "claim_strength": "observed",
            "evidence_refs": (evidence["evidence_ref"],),
            "numbers": {"paid_amount": 100.0},
        }
        failed = verify_answer_package(
            draft_claims=(claim,),
            evidence=(evidence,),
            visible_limitations=(),
            evidence_resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=self.registry,
        )
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(
            any(
                "query_contract_runtime_policy" in reason
                for error in failed["errors"]
                for reason in error.get("missing", ())
            ),
            failed["errors"],
        )

        passed = verify_answer_package(
            draft_claims=(claim,),
            evidence=(evidence,),
            visible_limitations=(),
            evidence_resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=self.registry,
            release_resolver=context["release_resolver"],
        )
        self.assertEqual(passed["status"], "passed", passed["errors"])

    def test_authority_projection_deduplicates_claims_that_resolve_to_one_fact(self):
        context = _dashboard_authority_context(self.registry)
        bound = bind_capability_inputs(
            context["plan"],
            results={context["contract"].query_contract_id: context["result"]},
            reports={context["contract"].query_contract_id: context["report"]},
            evidence_authority=context["authority"],
            runtime_registry=self.registry,
            release_resolver=context["release_resolver"],
        )
        evidence = _evidence_from_bound_dashboard_input(bound)
        claims = tuple(
            {
                "text": text,
                "claim_type": "comparative_change",
                "claim_strength": "observed",
                "evidence_refs": (evidence["evidence_ref"],),
                "numbers": {"paid_amount": 100.0},
            }
            for text in (
                "目标期大盘付费金额为 100。",
                "总体数据在目标期记录到付费金额 100。",
            )
        )

        projected, errors = _authority_bound_claim_projections(
            claims=claims,
            accepted_indexes=(0, 1),
            evidence=(evidence,),
            evidence_resolver=context["authority"],
            rows_loader=context["authority"].rows_loader,
            runtime_registry=self.registry,
            release_resolver=context["release_resolver"],
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(projected), 1)
        self.assertTrue(projected[0]["fact_refs"])

    def test_direct_payload_and_subclass_registries_cannot_authorize_chain(self):
        direct_payload_registry = RuntimeContractRegistry(self.registry._payload)

        class RegistrySubclass(RuntimeContractRegistry):
            pass

        subclass_registry = RegistrySubclass.from_path(
            "contracts/runtime/clickhouse-analysis-bindings.yaml"
        )
        for registry, expected in (
            (direct_payload_registry, "runtime_contract_registry_integrity"),
            (subclass_registry, "runtime_contract_registry_type_invalid"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(
                    AuthoritativeQueryChainError,
                    expected,
                ):
                    validate_authoritative_query_chain(
                        self.binding,
                        resolver=self.resolver,
                        rows_loader=self.resolver.rows_loader,
                        runtime_registry=registry,
                    )

    def test_redigested_dimension_chain_cannot_be_rebound_as_event_evidence(self):
        event = self.registry.capability_inputs("event_evidence")
        plan = dict(self.binding.plan_payload)
        plan.update(
            {
                "capability_id": "event_evidence",
                "minimum_readiness": event["minimum_readiness"],
                "degradation_policy": event["degradation_policy"],
                "supported_evidence_types": tuple(event["supported_evidence_types"]),
                "supported_claim_types": tuple(event["supported_claim_types"]),
                "maximum_claim_strength": event["maximum_claim_strength"],
                "capability_contract_signature": (
                    self.registry.capability_contract_signature("event_evidence")
                ),
                "maximum_claim_strength_rank": (
                    self.registry.maximum_claim_strength_rank(
                        event["maximum_claim_strength"]
                    )
                ),
            }
        )
        payload = dict(self.binding.binding_payload)
        payload.update(
            {
                "supported_evidence_types": tuple(event["supported_evidence_types"]),
                "supported_claim_types": tuple(event["supported_claim_types"]),
                "maximum_claim_strength": event["maximum_claim_strength"],
                "maximum_claim_strength_rank": (
                    self.registry.maximum_claim_strength_rank(
                        event["maximum_claim_strength"]
                    )
                ),
            }
        )
        forged = _resign_binding(
            self.binding,
            capability_id="event_evidence",
            capability_contract_signature=(
                self.registry.capability_contract_signature("event_evidence")
            ),
            supported_evidence_types=tuple(event["supported_evidence_types"]),
            supported_claim_types=tuple(event["supported_claim_types"]),
            maximum_claim_strength=event["maximum_claim_strength"],
            maximum_claim_strength_rank=self.registry.maximum_claim_strength_rank(
                event["maximum_claim_strength"]
            ),
            plan_payload=plan,
            binding_payload=payload,
        )

        with self.assertRaisesRegex(
            AuthoritativeQueryChainError,
            "capability_contract_plan_policy_mismatch",
        ):
            validate_authoritative_query_chain(
                forged,
                resolver=self.resolver,
                rows_loader=self.resolver.rows_loader,
                runtime_registry=self.registry,
            )

    def test_redigested_binding_cannot_expand_denormalized_claim_types(self):
        expanded = (*self.binding.supported_claim_types, "unreviewed_claim")
        payload = dict(self.binding.binding_payload)
        payload["supported_claim_types"] = expanded
        forged = _resign_binding(
            self.binding,
            supported_claim_types=expanded,
            binding_payload=payload,
        )

        with self.assertRaisesRegex(
            AuthoritativeQueryChainError,
            "capability_contract_plan_identity_mismatch",
        ):
            validate_authoritative_query_chain(
                forged,
                resolver=self.resolver,
                rows_loader=self.resolver.rows_loader,
                runtime_registry=self.registry,
            )

    def test_rows_ref_and_content_addressed_storage_ref_are_distinct(self):
        rows_record = self.resolver.resolve_rows_record(
            self.binding.rows_metadata_record_refs[0]
        )

        self.assertNotEqual(rows_record.rows_ref, rows_record.storage_ref)
        self.assertTrue(rows_record.storage_ref.startswith("rows-storage:sha256:"))
        self.assertIsNone(self.resolver.rows_loader.load_rows(rows_record.rows_ref))
        self.assertEqual(
            len(self.resolver.rows_loader.load_rows(rows_record.storage_ref)),
            rows_record.row_count,
        )

    def test_redigested_wrong_rows_count_and_unique_key_fail(self):
        original = self.resolver.resolve_rows_record(
            self.binding.rows_metadata_record_refs[0]
        )
        for field, value in (
            ("row_count", original.row_count + 1),
            ("unique_key_fields", ("window_id",)),
            ("storage_ref", f"rows-storage:sha256:{'0' * 64}"),
        ):
            with self.subTest(field=field):
                changed = replace(original, **{field: value})
                payload = {
                    "rows_ref": changed.rows_ref,
                    "rows_content_hash": changed.rows_content_hash,
                    "row_count": changed.row_count,
                    "unique_key_fields": changed.unique_key_fields,
                    "storage_ref": changed.storage_ref,
                }
                digest = canonical_digest(payload)
                changed = replace(
                    changed,
                    record_ref=f"rows-record:{changed.rows_ref}:{digest}",
                    record_digest=digest,
                    metadata_payload=payload,
                )

                class Resolver:
                    rows_loader = self.resolver.rows_loader

                    def __getattr__(_, name):
                        return getattr(self.resolver, name)

                    def resolve_rows_record(_, ref):
                        return changed if ref == changed.record_ref else self.resolver.resolve_rows_record(ref)

                forged_binding = _replace_binding_rows_record(
                    self.binding,
                    changed,
                )
                with self.assertRaises(AuthoritativeQueryChainError):
                    validate_authoritative_query_chain(
                        forged_binding,
                        resolver=Resolver(),
                        rows_loader=self.resolver.rows_loader,
                        runtime_registry=self.registry,
                    )

    def test_redigested_wrong_completeness_query_coverage_and_assertion_fail(self):
        original = self.resolver.resolve_completeness(
            self.binding.completeness_record_refs[0]
        )
        mutations = (
            {"query_contract_ref": "query:wrong"},
            {"coverage_summary": {**dict(original.report_payload["coverage_summary"]), "row_count": 2}},
            {"assertion_results": ({"assertion": "execution_succeeded", "passed": True},)},
        )
        for mutation in mutations:
            with self.subTest(mutation=tuple(mutation)):
                payload = {**dict(original.report_payload), **mutation}
                digest = canonical_digest(payload)
                changed = CompletenessRecord(
                    record_ref=f"completeness-record:{original.report_ref}:{digest}",
                    report_ref=original.report_ref,
                    query_contract_ref=str(payload["query_contract_ref"]),
                    result_ref=original.result_ref,
                    report_digest=digest,
                    report_payload=payload,
                )

                class Resolver:
                    rows_loader = self.resolver.rows_loader

                    def __getattr__(_, name):
                        return getattr(self.resolver, name)

                    def resolve_completeness(_, ref):
                        return changed if ref == changed.record_ref else self.resolver.resolve_completeness(ref)

                forged_binding = _replace_binding_completeness(
                    self.binding,
                    changed,
                )
                with self.assertRaises(AuthoritativeQueryChainError):
                    validate_authoritative_query_chain(
                        forged_binding,
                        resolver=Resolver(),
                        rows_loader=self.resolver.rows_loader,
                        runtime_registry=self.registry,
                    )


def _comparison_authority_facts():
    target = AuthorityFact.create(
        query_contract_ref="query:market:1",
        result_ref="result:market:1",
        metric_id="active_users",
        value=Decimal("125260"),
        window_id="target_day",
        window_role="target",
        observation_key="2026-06-02",
        dimensions=(),
        grain=("window_id",),
        value_semantics="raw_scalar",
        display_format="number",
    )
    baseline = AuthorityFact.create(
        query_contract_ref="query:market:1",
        result_ref="result:market:1",
        metric_id="active_users",
        value=Decimal("216820"),
        window_id="previous_day",
        window_role="baseline",
        observation_key="2026-06-01",
        dimensions=(),
        grain=("window_id",),
        value_semantics="raw_scalar",
        display_format="number",
    )
    return {
        "metric_ids": ("active_users",),
        "authority_facts": (target, baseline),
        "authority_context_facts": (),
        "grains": (("window_id",),),
        "target_windows": (),
        "baseline_windows": (),
    }


def _replace_binding_rows_record(binding, rows_record):
    payload = dict(binding.binding_payload)
    payload["rows_metadata_record_refs"] = (rows_record.record_ref,)
    payload["rows_metadata_record_digests"] = (rows_record.record_digest,)
    return _resign_binding(
        binding,
        rows_metadata_record_refs=(rows_record.record_ref,),
        rows_metadata_record_digests=(rows_record.record_digest,),
        binding_payload=payload,
    )


class _DatasetReleaseAuthorityResolver:
    def __init__(self, record):
        self.record = record

    def resolve_dataset_release(self, release_ref):
        if release_ref != self.record.release_ref:
            raise KeyError(release_ref)
        return self.record


def _gameplay_authority_context(registry, *, include_sparse_baseline=False):
    base = DatasetSnapshot(
        snapshot_ref="snapshot:gameplay:authority-e2e",
        dataset_id="gameplay",
        physical_table="gameplay_daily__b1b1b1b1b1b1b1b1",
        watermark="2026-06-02",
        schema_fingerprint="b1" * 32,
        schema_fields=tuple(registry.dataset("gameplay")["schema_fields"]),
        contract_ref="contracts/sources/gameplay.source.yaml@0.1",
        permission_scopes=("analyst",),
        loaded_at="2026-06-03T00:00:00+00:00",
        status="active",
        evidence_state="context_only",
        reconciliation_status="not_applicable",
        logical_snapshot_id="gameplay-authority-e2e",
        load_revision="gameplay-load:sha256:authority-e2e",
        rows_content_hash="b" * 64,
        snapshot_id="gameplay-authority-e2e",
        source_load_manifest_ref="load-manifest:gameplay:authority-e2e",
        runtime_binding_ref="contracts/runtime/clickhouse-analysis-bindings.yaml@1",
        source_checksums=(("gameplay.csv", "c" * 64),),
        row_count=9,
        date_range=("2026-05-26", "2026-06-02"),
    )
    channel = replace(
        base,
        snapshot_ref="snapshot:gameplay-channel:authority-e2e",
        dataset_id="gameplay_channel",
        physical_table="gameplay_channel_daily__c1c1c1c1c1c1c1c1",
        schema_fingerprint="c1" * 32,
        schema_fields=tuple(registry.dataset("gameplay_channel")["schema_fields"]),
        rows_content_hash="d" * 64,
        source_checksums=(("gameplay-channel.csv", "e" * 64),),
    )
    release_ref = dataset_snapshot_release_ref(
        base.logical_snapshot_id,
        base.load_revision,
        (base.snapshot_ref, channel.snapshot_ref),
    )
    base = replace(base, release_ref=release_ref)
    channel = replace(channel, release_ref=release_ref)
    release_record = build_dataset_release_authority_record(
        tuple(
            {**snapshot.to_dict(), "requires_release": True}
            for snapshot in (base, channel)
        )
    )
    base = replace(base, authority_record_ref=release_record.authority_record_ref)
    channel = replace(channel, authority_record_ref=release_record.authority_record_ref)
    release_resolver = _DatasetReleaseAuthorityResolver(release_record)
    outcome = compile_analysis_contract(
        run_id="run-gameplay-authority-e2e",
        proposal={
            "target_metrics": ("player_bet_amount",),
            "requested_dimensions": ("gameplay",),
            "metric_dataset_overrides": {"player_bet_amount": "gameplay"},
            "dimension_dataset_overrides": {"gameplay": "gameplay"},
            "claim_intents": ("observed_activity",),
        },
        accepted_capabilities=("gameplay_activity_context",),
        catalog=DatasetCatalog((base,), release_resolver=release_resolver),
        registry=registry,
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        permission_scope="analyst",
        release_resolver=release_resolver,
    )
    contract = next(
        item
        for item in outcome.query_contracts
        if item.query_intent == "gameplay_activity_probe"
    )
    plan = outcome.capability_plans[0]
    if include_sparse_baseline:
        rolling = ResolvedWindow(
            "rolling_7_day_baseline",
            "baseline",
            "2026-05-26..2026-06-01",
            "2026-05-26",
            "2026-06-02",
            "Africa/Lagos",
            "daily_total",
            7,
            "2026-06-01",
        )
        unsigned = replace(
            contract,
            window_refs=(*contract.window_refs, rolling.window_id),
            resolved_windows=(*contract.resolved_windows, rolling),
            result_shape=replace(
                contract.result_shape,
                required_window_ids=(*contract.window_refs, rolling.window_id),
            ),
            contract_signature="",
            query_role_ref="",
        )
        signature = query_contract_signature(unsigned)
        contract = replace(
            unsigned,
            contract_signature=signature,
            query_role_ref=f"query-role:{signature}",
        )
        plan = replace(
            plan,
            required_input_slots=(
                replace(
                    plan.required_input_slots[0],
                    required_window_ids=contract.window_refs,
                ),
            ),
        )
    rows = []
    for window in contract.resolved_windows:
        start = date.fromisoformat(window.start_inclusive)
        end = date.fromisoformat(window.end_exclusive)
        for offset in range((end - start).days):
            observed = start + timedelta(days=offset)
            value = Decimal("100.00")
            if include_sparse_baseline:
                value = (
                    Decimal("501")
                    if window.role == "target" or offset == 0
                    else Decimal("0")
                )
            rows.append(
                {
                    "window_id": window.window_id,
                    "window_role": window.role,
                    "observation_key": observed.isoformat(),
                    "gameplay": "Rummy",
                    "player_bet_amount": value,
                }
            )
        if include_sparse_baseline and window.role == "baseline":
            rows.append(
                {
                    "window_id": window.window_id,
                    "window_role": window.role,
                    "observation_key": window.start_inclusive,
                    "gameplay": "Poker",
                    "player_bet_amount": Decimal("80"),
                }
            )
    authority = RuntimeEvidenceAuthority()
    result = ClickHouseQueryExecutor(
        _FaithfulRowsRuntime(rows),
        evidence_authority=authority,
        release_resolver=release_resolver,
    ).execute(
        contract,
        {base.snapshot_ref: base},
        execution_attempt_ref="attempt:gameplay-authority-e2e",
    )
    report = validate_query_result(
        contract,
        result,
        base,
        release_resolver=release_resolver,
    )
    _record_completeness(authority, report)
    return {
        "authority": authority,
        "release_resolver": release_resolver,
        "contract": contract,
        "result": result,
        "report": report,
        "plan": plan,
        "snapshot": base,
    }


def _event_authority_context(registry):
    schema_fields = tuple(registry.dataset("external_event")["schema_fields"])
    snapshot = DatasetSnapshot(
        snapshot_ref="snapshot:external-event:authority-e2e",
        dataset_id="external_event",
        physical_table="business_events__a1a1a1a1a1a1a1a1",
        watermark="2026-06-08",
        schema_fingerprint="a1" * 32,
        schema_fields=schema_fields,
        contract_ref="contracts/sources/external-events.source.yaml@0.1",
        permission_scopes=("analyst",),
        loaded_at="2026-06-03T00:00:00+00:00",
        status="active",
        evidence_state="context_only",
        reconciliation_status="not_applicable",
        logical_snapshot_id="external-events-authority-e2e",
        load_revision="external-events-load:sha256:authority-e2e",
        rows_content_hash="e" * 64,
        snapshot_id="external-events-authority-e2e",
        source_load_manifest_ref="load-manifest:event:authority-e2e",
        runtime_binding_ref="contracts/runtime/clickhouse-analysis-bindings.yaml@1",
        source_checksums=(("events.xlsx", "f" * 64),),
        row_count=1,
        date_range=("2026-05-01", "2026-06-08"),
    )
    release_ref = dataset_snapshot_release_ref(
        snapshot.logical_snapshot_id,
        snapshot.load_revision,
        (snapshot.snapshot_ref,),
    )
    snapshot = replace(snapshot, release_ref=release_ref)
    release_record = build_dataset_release_authority_record(
        ({**snapshot.to_dict(), "requires_release": True},)
    )
    snapshot = replace(
        snapshot,
        authority_record_ref=release_record.authority_record_ref,
    )
    release_resolver = _DatasetReleaseAuthorityResolver(release_record)
    outcome = compile_analysis_contract(
        run_id="run-event-authority-e2e",
        proposal={
            "requested_context_sources": ("external_event",),
            "claim_intents": ("candidate_mechanism",),
        },
        accepted_capabilities=("event_evidence",),
        catalog=DatasetCatalog((snapshot,), release_resolver=release_resolver),
        registry=registry,
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        permission_scope="analyst",
        release_resolver=release_resolver,
    )
    contract = outcome.query_contracts[0]
    rows = tuple(
        {
            "window_id": window.window_id,
            "window_role": window.role,
            "observation_key": "event:holiday:reviewed",
            "event_count": 1,
            "source_family": "external_event",
            "event_id": "event:holiday:reviewed",
            "event_type": "holiday_context",
            "event_start_date": "2026-05-01",
            "event_end_date": "2026-06-08",
            "affected_scope": "Nigeria",
            "authority": "reviewed_workbook_pending_owner_review",
            "evidence_level": "context",
            "wording_limit": "context",
            "recurrence_kind": "",
            "recurrence_month_start": 0,
            "recurrence_day_start": 0,
            "recurrence_month_end": 0,
            "recurrence_day_end": 0,
            "payload": '{"description":"reviewed holiday"}',
        }
        for window in contract.resolved_windows
    )
    attempt_ref = "attempt:event-authority-e2e"
    authority = RuntimeEvidenceAuthority()
    result = ClickHouseQueryExecutor(
        _FaithfulRowsRuntime(rows),
        evidence_authority=authority,
        release_resolver=release_resolver,
    ).execute(
        contract,
        {snapshot.snapshot_ref: snapshot},
        execution_attempt_ref=attempt_ref,
    )
    report = validate_query_result(
        contract,
        result,
        snapshot,
        release_resolver=release_resolver,
    )
    _record_completeness(authority, report)
    return {
        "authority": authority,
        "release_resolver": release_resolver,
        "contract": contract,
        "result": result,
        "report": report,
        "plan": outcome.capability_plans[0],
        "snapshot": snapshot,
    }


class _FaithfulRowsRuntime:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def aggregate(self, sql, query_id, **kwargs):
        return self._result(sql, query_id, kwargs)

    def bounded_context(self, sql, query_id, **kwargs):
        return self._result(sql, query_id, kwargs)

    def _result(self, sql, query_id, kwargs):
        return ClickHouseQueryResult(
            ok=True,
            rows=self.rows,
            query_hash=canonical_digest({"sql": sql, "parameters": kwargs.get("parameters", {})}),
            query_id=query_id,
            provider_stats={
                "requested_settings": dict(kwargs.get("settings") or {}),
                "summary": {"read_rows": len(self.rows)},
            },
            execution_attempt_ref=kwargs.get("execution_attempt_ref", ""),
        )


def _dashboard_authority_context(registry):
    snapshot = DatasetSnapshot(
        snapshot_ref="snapshot:market-dashboard:release-e2e",
        dataset_id="market_dashboard",
        physical_table="market_dashboard_daily__schema1234567890",
        watermark="2026-06-02",
        schema_fingerprint="schema1234567890abcdef",
        schema_fields=(
            "snapshot_id",
            "load_revision",
            "business_date",
            "game",
            "paid_amount",
        ),
        contract_ref="contract:market-dashboard@1",
        permission_scopes=("analyst",),
        loaded_at="2026-06-03T00:00:00+00:00",
        status="active",
        evidence_state="claim_ready",
        reconciliation_status="matched",
        reconciliation_ref="reconciliation:market-dashboard:matched",
        logical_snapshot_id="dashboard-logical",
        load_revision="dashboard-load:sha256:release-e2e",
        rows_content_hash="a" * 64,
        snapshot_id="dashboard-logical",
        source_load_manifest_ref="load-manifest:dashboard:release-e2e",
        runtime_binding_ref="runtime-binding:market-dashboard@1",
        source_checksums=(("market_dashboard.csv", "b" * 64),),
        row_count=1,
        date_range=("2026-06-02", "2026-06-02"),
    )
    channel_snapshot = replace(
        snapshot,
        snapshot_ref="snapshot:market-dashboard-channel:release-e2e",
        dataset_id="market_dashboard_channel",
        physical_table="market_dashboard_channel_daily__schema1234567890",
        schema_fields=(*snapshot.schema_fields, "channel"),
        evidence_state="context_only",
        reconciliation_status="mismatch",
        reconciliation_ref="reconciliation:market-dashboard-channel:mismatch",
        rows_content_hash="c" * 64,
        source_load_manifest_ref="load-manifest:dashboard-channel:release-e2e",
        runtime_binding_ref="runtime-binding:market-dashboard-channel@1",
        source_checksums=(("market_dashboard_channel.csv", "d" * 64),),
    )
    release_ref = dataset_snapshot_release_ref(
        snapshot.logical_snapshot_id,
        snapshot.load_revision,
        (snapshot.snapshot_ref, channel_snapshot.snapshot_ref),
    )
    snapshot = replace(snapshot, release_ref=release_ref)
    channel_snapshot = replace(channel_snapshot, release_ref=release_ref)
    release_record = build_dataset_release_authority_record(
        tuple(
            {**item.to_dict(), "requires_release": True}
            for item in (snapshot, channel_snapshot)
        )
    )
    snapshot = replace(
        snapshot,
        authority_record_ref=release_record.authority_record_ref,
    )
    release_resolver = _DatasetReleaseAuthorityResolver(release_record)
    catalog = DatasetCatalog(
        (snapshot,),
        release_resolver=release_resolver,
    )
    outcome = compile_analysis_contract(
        run_id="run-market-dashboard-release-e2e",
        proposal={
            "target_metrics": ("paid_amount",),
            "claim_intents": ("comparative_change",),
        },
        accepted_capabilities=("market_health_compare",),
        catalog=catalog,
        registry=registry,
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        permission_scope="analyst",
        release_resolver=release_resolver,
    )
    contract = outcome.query_contracts[0]
    rows = tuple(
        {
            "window_id": window.window_id,
            "window_role": window.role,
            "observation_key": window.start_inclusive,
            "paid_amount": 100.0,
        }
        for window in contract.resolved_windows
    )
    query_hash = "hash:market-dashboard-release-e2e"
    attempt_ref = "attempt:market-dashboard-release-e2e"
    refs = query_audit_refs(
        query_hash,
        contract.contract_signature,
        contract.dataset_snapshot_refs,
        query_contract_ref=contract.query_contract_id,
        execution_attempt_ref=attempt_ref,
        rows_content_hash=canonical_result_rows_hash(
            rows,
            contract.result_shape.unique_key,
        ),
    )
    result = QueryResultEnvelope(
        query_contract_ref=contract.query_contract_id,
        query_id="clickhouse:market-dashboard-release-e2e",
        query_hash=query_hash,
        result_ref=refs.result_ref,
        execution_status="succeeded",
        rows_ref=refs.rows_ref,
        row_count=len(rows),
        completeness_report_ref=refs.completeness_report_ref,
        rows=rows,
        observed_schema={field: "String" for field in contract.result_shape.required_fields},
        observed_windows=tuple(row["window_id"] for row in rows),
        observed_grain=contract.result_shape.grain,
        source_snapshot_refs=(snapshot.snapshot_ref,),
        execution_attempt_ref=attempt_ref,
    )
    report = validate_query_result(
        contract,
        result,
        snapshot,
        release_resolver=release_resolver,
    )
    authority = RuntimeEvidenceAuthority()
    _record_query_execution(
        authority,
        contract,
        result,
        {snapshot.snapshot_ref: snapshot},
    )
    _record_completeness(authority, report)
    return {
        "authority": authority,
        "release_resolver": release_resolver,
        "contract": contract,
        "result": result,
        "report": report,
        "plan": outcome.capability_plans[0],
    }


def _evidence_from_bound_dashboard_input(bound):
    return {
        "evidence_ref": "evidence:market-dashboard-release-e2e",
        "evidence_type": "statistical_association",
        "strength": "observed",
        "wording_limit": "supported",
        "limitations": (),
        "typed_payload": {"paid_amount": 100.0},
        "capability_id": bound.capability_id,
        "analysis_contract_ref": bound.analysis_contract_ref,
        "capability_contract_ref": bound.capability_contract_ref,
        "query_contract_refs": (*bound.query_contract_refs, *bound.validation_query_contract_refs),
        "result_refs": (*bound.result_refs, *bound.validation_result_refs),
        "query_execution_record_refs": (
            *bound.query_execution_record_refs,
            *bound.validation_query_execution_record_refs,
        ),
        "query_execution_record_digests": (
            *bound.query_execution_record_digests,
            *bound.validation_query_execution_record_digests,
        ),
        "rows_metadata_record_refs": (
            *bound.rows_metadata_record_refs,
            *bound.validation_rows_metadata_record_refs,
        ),
        "rows_metadata_record_digests": (
            *bound.rows_metadata_record_digests,
            *bound.validation_rows_metadata_record_digests,
        ),
        "completeness_report_refs": (
            *bound.completeness_report_refs,
            *bound.validation_completeness_report_refs,
        ),
        "completeness_record_refs": (
            *bound.completeness_record_refs,
            *bound.validation_completeness_record_refs,
        ),
        "completeness_record_digests": (
            *bound.completeness_record_digests,
            *bound.validation_completeness_record_digests,
        ),
        "source_snapshot_refs": (
            *bound.source_snapshot_refs,
            *bound.validation_source_snapshot_refs,
        ),
        "supported_evidence_types": bound.supported_evidence_types,
        "supported_claim_types": bound.supported_claim_types,
        "maximum_claim_strength": bound.maximum_claim_strength,
        "maximum_claim_strength_rank": bound.maximum_claim_strength_rank,
        "claim_strength_taxonomy_version": bound.claim_strength_taxonomy_version,
        "input_status": bound.status,
        "input_completeness_statuses": bound.input_completeness_statuses,
        "binding_manifest_ref": bound.binding_manifest_ref,
        "binding_manifest_digest": bound.binding_manifest_digest,
    }


def _replace_binding_completeness(binding, record):
    payload = dict(binding.binding_payload)
    payload["completeness_record_refs"] = (record.record_ref,)
    payload["completeness_record_digests"] = (record.report_digest,)
    return _resign_binding(
        binding,
        completeness_record_refs=(record.record_ref,),
        completeness_record_digests=(record.report_digest,),
        binding_payload=payload,
    )


def _resign_binding(binding, **changes):
    changed = replace(binding, **changes)
    digest = canonical_digest(
        {
            "plan": changed.plan_payload,
            "binding": changed.binding_payload,
        }
    )
    return replace(
        changed,
        record_ref=f"capability-binding:{changed.capability_id}:{digest}",
        binding_digest=digest,
    )


if __name__ == "__main__":
    unittest.main()
