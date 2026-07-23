from __future__ import annotations

from pathlib import Path
import re

from tools.runtime.cutover_single_authority_schema import (
    SINGLE_AUTHORITY_MIGRATION_ID,
)


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "tools/runtime/conversation-runtime.sql"
PHASE46_MARKER = "-- vNext Phase 4-6 sealed authority, publication, and delivery"

AUTHORITY_TABLES = (
    "claim_authority_namespaces",
    "claim_keys",
    "claim_support_edges",
    "claim_revisions",
    "claim_settlement_checkpoints",
    "claim_obligation_settlement_bases",
    "claim_verification_attempts",
    "claim_verification_decisions",
    "local_boundary_authorities",
    "claim_verification_reports",
    "claim_obligation_coverages",
    "claim_graphs",
    "claim_settlements",
    "recommendation_proposals",
    "recommendation_verification_attempts",
    "recommendation_verification_decisions",
    "recommendation_records",
    "authority_bundles",
    "restricted_provider_responses",
    "publication_visibility_policies",
    "public_claim_palettes",
    "public_claims",
    "public_fact_descriptors",
    "public_recommendations",
    "public_limitations",
    "narrative_material_projections",
    "narrative_writer_attempts",
    "narrative_documents",
    "narrative_blocks",
    "narrative_fact_bindings",
    "sensitive_output_findings",
    "block_local_validation_reports",
    "block_local_issues",
    "block_verification_attempts",
    "block_vetoes",
    "block_verification_reports",
    "publication_projections",
    "publication_revisions",
    "delivery_outbox_records",
    "publication_customer_payloads",
    "delivery_attempts",
    "customer_publications",
    "narrative_attempt_requests",
    "insight_quality_evaluations",
)

GOVERNANCE_AUTHORITY_TABLES = ("guardrail_promotion_records",)


def _schema() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


def _phase46() -> str:
    schema = _schema()
    return schema[schema.index(PHASE46_MARKER) :]


def _table_body(table: str) -> str:
    phase46 = _phase46()
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS waje_runtime\.{re.escape(table)} \((.*?)\n\);",
        phase46,
        re.DOTALL,
    )
    assert match is not None, f"missing Phase 4-6 table: {table}"
    return match.group(1)


def test_phase46_declares_complete_authority_publication_and_delivery_schema():
    phase46 = _phase46()
    for table in (
        *AUTHORITY_TABLES,
        *GOVERNANCE_AUTHORITY_TABLES,
        "delivery_dispatches",
    ):
        assert f"CREATE TABLE IF NOT EXISTS waje_runtime.{table}" in phase46
    assert SINGLE_AUTHORITY_MIGRATION_ID in phase46
    assert "answer_package" not in phase46.lower()


def test_every_phase46_authority_record_is_owner_and_run_scoped_content_addressed():
    for table in AUTHORITY_TABLES:
        body = _table_body(table)
        assert "owner_ref text NOT NULL" in body, table
        assert "run_attempt_id text NOT NULL" in body, table
        assert (
            "REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE RESTRICT" in body
        ), table
        assert (
            "content_digest text NOT NULL CHECK (length(content_digest) = 64)" in body
        ), table
        assert (
            "payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object')" in body
        ), table
        assert "UNIQUE(owner_ref, run_attempt_id" in body, table
        assert "ON DELETE CASCADE" not in body, table
        assert "ON DELETE SET NULL" not in body, table


def test_customer_publication_exposes_its_payload_digest_as_a_required_column():
    body = _table_body("publication_customer_payloads")

    assert "customer_payload_digest text GENERATED ALWAYS AS (" in body
    assert "payload->>'customer_payload_digest'" in body
    phase46 = _phase46()
    assert "publication_customer_payloads_customer_payload_digest_check" in phase46
    assert "CHECK (length(customer_payload_digest) = 64)" in phase46


def test_guardrail_promotion_is_cross_run_governance_authority():
    body = _table_body("guardrail_promotion_records")
    assert "governance_scope_ref text NOT NULL" in body
    assert "run_attempt_id" not in body
    assert "\n  owner_ref text NOT NULL" not in body
    assert "UNIQUE(governance_scope_ref, promotion_ref)" in body
    assert "UNIQUE(governance_scope_ref, runtime_guardrail_ref)" in body
    assert "content_digest text NOT NULL CHECK (length(content_digest) = 64)" in body
    assert "payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object')" in body


def test_cross_record_authority_references_are_owner_run_closed_and_restricted():
    phase46 = _phase46()
    assert "FOREIGN KEY (owner_ref, run_attempt_id, claim_key)" in phase46
    assert "FOREIGN KEY (owner_ref, run_attempt_id, claim_graph_ref)" in phase46
    assert "FOREIGN KEY (owner_ref, run_attempt_id, authority_bundle_ref)" in phase46
    assert "FOREIGN KEY (owner_ref, run_attempt_id, publication_ref)" in phase46
    assert "FOREIGN KEY (owner_ref, run_attempt_id, outbox_ref)" in phase46
    assert "ON DELETE CASCADE" not in phase46
    assert "ON DELETE SET NULL" not in phase46


def test_authority_bundle_has_exactly_once_seal_and_full_upstream_manifest_binding():
    body = _table_body("authority_bundles")
    for field in (
        "execution_result_ref text NOT NULL",
        "execution_result_digest text NOT NULL",
        "claim_settlement_ref text NOT NULL",
        "claim_settlement_digest text NOT NULL",
        "claim_graph_ref text NOT NULL",
        "claim_graph_digest text NOT NULL",
        "bundle_digest text NOT NULL",
        "seal_state text NOT NULL CHECK (seal_state = 'sealed')",
    ):
        assert field in body
    assert "idx_authority_bundles_one_sealed_per_run" in _phase46()
    assert "WHERE seal_state = 'sealed'" in _phase46()


def test_public_limitation_persists_canonical_public_context_only():
    body = _table_body("public_limitations")
    assert (
        "public_context jsonb NOT NULL CHECK (jsonb_typeof(public_context) = 'object')"
        in body
    )
    assert "public_text" not in body


def test_narrative_material_projection_is_pre_provider_append_only_authority():
    body = _table_body("narrative_material_projections")
    for field in (
        "projection_ref text PRIMARY KEY",
        "palette_ref text NOT NULL",
        "palette_digest text NOT NULL",
        "claim_settlement_ref text NOT NULL",
        "claim_settlement_digest text NOT NULL",
    ):
        assert field in body
    assert "UNIQUE(owner_ref, run_attempt_id, projection_ref)" in body
    assert "UNIQUE(owner_ref, run_attempt_id, content_digest)" in body
    assert (
        "UNIQUE(owner_ref, run_attempt_id, palette_ref, claim_settlement_ref)" in body
    )
    assert "FOREIGN KEY (owner_ref, run_attempt_id, palette_ref)" in body
    assert "FOREIGN KEY (owner_ref, run_attempt_id, claim_settlement_ref)" in body
    assert "provider_ref" not in body
    assert "writer_attempt_ref" not in body
    assert "transition_id" not in body
    assert "'narrative_material_projections'" in _phase46()
    assert "CREATE TRIGGER %I_append_only BEFORE UPDATE OR DELETE" in _phase46()
    assert "ALTER TABLE waje_runtime.narrative_material_projections" not in _phase46()


def test_provider_and_publication_tables_bind_only_the_material_projection():
    for table in (
        "narrative_writer_attempts",
        "narrative_documents",
        "block_local_validation_reports",
        "publication_projections",
    ):
        body = _table_body(table)
        assert "material_projection_ref text NOT NULL" in body
        assert (
            "FOREIGN KEY (owner_ref, run_attempt_id, material_projection_ref)" in body
        )
        assert "palette_ref" not in body
    for table in ("block_local_validation_reports", "publication_projections"):
        body = _table_body(table)
        assert "material_projection_digest text NOT NULL" in body
        assert "palette_digest" not in body


def test_narrative_document_revision_lineage_uses_parent_and_mixed_block_origins():
    document = _table_body("narrative_documents")
    block = _table_body("narrative_blocks")

    assert "parent_narrative_id text" in document
    assert "FOREIGN KEY (owner_ref, run_attempt_id, parent_narrative_id)" in document
    assert "focused_retry_of_block_id" not in document
    assert "focused_retry_report_ref" not in document
    assert "PRIMARY KEY(owner_ref, run_attempt_id, narrative_id, block_id)" in block
    assert "FOREIGN KEY (owner_ref, run_attempt_id, writer_attempt_id)" in block


def test_verification_attempt_decision_and_report_identities_are_durable():
    for family in ("claim", "recommendation"):
        attempt = _table_body(f"{family}_verification_attempts")
        decision = _table_body(f"{family}_verification_decisions")
        assert "authority_input_ref text NOT NULL" in attempt
        assert "authority_input_digest text NOT NULL" in attempt
        assert "provider_ref text NOT NULL" in attempt
        assert "model_ref text NOT NULL" in attempt
        assert "raw_provider_response_ref text NOT NULL" in attempt
        assert "raw_provider_response_digest text NOT NULL" in attempt
        assert "verification_attempt_ref text NOT NULL" in decision
        assert (
            "disposition text NOT NULL CHECK (disposition IN ('accepted', 'vetoed'))"
            in decision
        )
    claim_report = _table_body("claim_verification_reports")
    block_report = _table_body("block_verification_reports")
    assert "verifier_report_ref text PRIMARY KEY" in claim_report
    assert "verification_attempt_ref text" in claim_report
    assert "verification_mode text NOT NULL CHECK" in claim_report
    assert "'semantic_verifier', 'local_boundary_authority'" in claim_report
    assert "local_boundary_authority_ref text" in claim_report
    assert "verification_mode = 'semantic_verifier'" in claim_report
    assert "verification_attempt_ref IS NOT NULL" in claim_report
    assert "local_boundary_authority_ref IS NULL" in claim_report
    assert "verification_mode = 'local_boundary_authority'" in claim_report
    assert "verification_attempt_ref IS NULL" in claim_report
    assert "local_boundary_authority_ref IS NOT NULL" in claim_report
    assert "verification_attempt_ref text NOT NULL" in block_report
    assert "verifier_report_ref text PRIMARY KEY" in block_report
    assert "verification_attempt_digest text NOT NULL" in block_report
    block_attempt = _table_body("block_verification_attempts")
    assert "provider_response_ref text NOT NULL" in block_attempt
    assert "provider_response_digest text NOT NULL" in block_attempt


def test_claim_settlement_checkpoint_and_obligation_basis_are_durable_resume_authority():
    checkpoint = _table_body("claim_settlement_checkpoints")
    assert "checkpoint_ref text PRIMARY KEY" in checkpoint
    assert "execution_result_ref text NOT NULL" in checkpoint
    assert "execution_result_digest text NOT NULL" in checkpoint
    assert "plan_revision_id text NOT NULL" in checkpoint

    basis = _table_body("claim_obligation_settlement_bases")
    assert "basis_ref text PRIMARY KEY" in basis
    assert "checkpoint_ref text NOT NULL" in basis
    assert "obligation_id text NOT NULL" in basis
    assert "UNIQUE(owner_ref, run_attempt_id, checkpoint_ref, obligation_id)" in basis
    assert "FOREIGN KEY (owner_ref, run_attempt_id, checkpoint_ref)" in basis

    settlement = _table_body("claim_settlements")
    assert "checkpoint_ref text NOT NULL" in settlement
    assert "FOREIGN KEY (owner_ref, run_attempt_id, checkpoint_ref)" in settlement


def test_publication_revision_and_outbox_constraints_support_linear_cas_and_idempotency():
    publication = _table_body("publication_revisions")
    assert "revision integer NOT NULL CHECK (revision > 0)" in publication
    assert "supersedes_publication_ref text" in publication
    assert "UNIQUE(owner_ref, run_attempt_id, revision)" in publication
    assert (
        "UNIQUE(owner_ref, run_attempt_id, supersedes_publication_ref)" in publication
    )

    outbox = _table_body("delivery_outbox_records")
    assert "idempotency_key text NOT NULL" in outbox
    assert (
        "UNIQUE(owner_ref, run_attempt_id, publication_ref, destination_ref, channel)"
        in outbox
    )
    assert "UNIQUE(owner_ref, run_attempt_id, idempotency_key)" in outbox


def test_delivery_dispatch_is_the_only_mutable_phase46_operational_table():
    phase46 = _phase46()
    dispatch = _table_body("delivery_dispatches")
    assert "dispatch_state text NOT NULL" in dispatch
    assert "lease_owner text" in dispatch
    assert "lease_epoch bigint NOT NULL DEFAULT 0" in dispatch
    assert "updated_at timestamptz NOT NULL DEFAULT now()" in dispatch
    assert "delivery_dispatches_append_only" not in phase46
    for table in (*AUTHORITY_TABLES, *GOVERNANCE_AUTHORITY_TABLES):
        assert f"'{table}'" in phase46
    assert "All records above are authority except delivery_dispatches" in phase46


def test_quality_evaluation_is_advisory_and_guardrail_promotion_requires_dual_ownership():
    quality = _table_body("insight_quality_evaluations")
    assert "advisory boolean NOT NULL CHECK (advisory)" in quality
    assert "narrative_attempt_request_ref text" in quality
    for field in (
        "rubric_ref text NOT NULL",
        "rubric_digest text NOT NULL",
        "rubric jsonb NOT NULL",
        "evaluation_case_ref text NOT NULL",
        "evaluation_case_digest text NOT NULL",
        "evaluation_case jsonb NOT NULL",
        "model_profile_ref text NOT NULL",
        "model_profile_digest text NOT NULL",
        "model_profile jsonb NOT NULL",
        "human_reasons jsonb NOT NULL",
    ):
        assert field in quality

    promotion = _table_body("guardrail_promotion_records")
    assert "generalizable_pattern_ref text NOT NULL" in promotion
    assert "business_owner_ref text NOT NULL" in promotion
    assert "system_owner_ref text NOT NULL" in promotion
    assert "human_validation_ref text NOT NULL" in promotion
    assert "CHECK (business_owner_ref <> system_owner_ref)" in promotion
