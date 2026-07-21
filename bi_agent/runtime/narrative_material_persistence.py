from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping, Sequence

from psycopg import Error as PsycopgError

from bi_agent.runtime.capability_authority import EvidenceLedgerEntry
from bi_agent.runtime.claim_authority import AuthorityBundle
from bi_agent.runtime.claim_settlement import (
    ClaimSettlement,
    validate_typed_claim_settlement,
)
from bi_agent.runtime.evidence_authority import canonical_digest, canonical_value
from bi_agent.runtime.narrative_authority import (
    PublicationFieldVisibilityPolicy,
    PublicClaimPalette,
)
from bi_agent.runtime.narrative_material_projection import (
    NarrativeMaterialProjection,
)


class NarrativeMaterialPersistenceError(ValueError):
    pass


class NarrativeMaterialPersistenceBackendError(RuntimeError):
    """Typed signal that the checkpoint store could not complete a write."""


class NarrativeMaterialPersistenceOperationalError(RuntimeError):
    def __init__(self, *, technical_detail_ref: str) -> None:
        if (
            not isinstance(technical_detail_ref, str)
            or not technical_detail_ref.startswith("technical-detail:sha256:")
            or len(technical_detail_ref.removeprefix("technical-detail:sha256:")) != 64
        ):
            raise NarrativeMaterialPersistenceError(
                "narrative_material_operational_detail_ref_invalid"
            )
        super().__init__("narrative_material_persistence_unavailable")
        self.technical_detail_ref = technical_detail_ref


@dataclass(frozen=True)
class NarrativeMaterialPersistenceResult:
    projection_ref: str
    projection_digest: str
    palette_ref: str
    run_attempt_id: str
    status: str


@dataclass(frozen=True)
class _InsertRecord:
    table: str
    identity_column: str
    conflict_columns: tuple[str, ...]
    columns: Mapping[str, Any]
    json_columns: frozenset[str] = frozenset({"payload"})


_PREFLIGHT_SQL = """
/* narrative_material_checkpoint_preflight */
SELECT
  thread.owner_id AS owner_ref,
  run.thread_id AS thread_ref,
  bundle.payload AS authority_bundle_payload,
  bundle.bundle_digest AS authority_bundle_digest,
  settlement.payload AS claim_settlement_payload,
  settlement.content_digest AS claim_settlement_digest
FROM waje_runtime.analysis_runs run
JOIN waje_runtime.investigation_threads thread
  ON thread.thread_id = run.thread_id
JOIN waje_runtime.authority_bundles bundle
  ON bundle.bundle_ref = %(authority_bundle_ref)s
 AND bundle.owner_ref = thread.owner_id
 AND bundle.run_attempt_id = run.run_id
 AND bundle.seal_state = 'sealed'
JOIN waje_runtime.claim_settlements settlement
  ON settlement.settlement_ref = %(claim_settlement_ref)s
 AND settlement.owner_ref = thread.owner_id
 AND settlement.run_attempt_id = run.run_id
WHERE run.run_id = %(run_attempt_id)s
  AND run.run_attempt_id = %(run_attempt_id)s
FOR UPDATE OF run
"""


def persist_narrative_material_projection(
    connection: Any,
    *,
    owner_ref: str,
    thread_ref: str,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
    visibility_policy: PublicationFieldVisibilityPolicy,
    palette: PublicClaimPalette,
    projection: NarrativeMaterialProjection,
    evidence_entries: Sequence[EvidenceLedgerEntry],
) -> NarrativeMaterialPersistenceResult:
    owner = _required_string(
        owner_ref,
        "narrative_material_owner_ref_invalid",
    )
    thread = _required_string(
        thread_ref,
        "narrative_material_thread_ref_invalid",
    )
    bundle, settlement, policy, checked_palette, material_projection = (
        _validated_checkpoint(
            authority_bundle=authority_bundle,
            claim_settlement=claim_settlement,
            visibility_policy=visibility_policy,
            palette=palette,
            projection=projection,
            evidence_entries=evidence_entries,
        )
    )
    try:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
            {"lock_key": f"narrative-material:{bundle.run_attempt_id}"},
        )
        row = connection.execute(
            _PREFLIGHT_SQL,
            {
                "run_attempt_id": bundle.run_attempt_id,
                "authority_bundle_ref": bundle.bundle_ref,
                "claim_settlement_ref": settlement.settlement_ref,
            },
        ).fetchone()
        if row is None:
            raise NarrativeMaterialPersistenceError(
                "narrative_material_active_chain_missing"
            )
        _validate_preflight(
            row,
            owner_ref=owner,
            thread_ref=thread,
            authority_bundle=bundle,
            claim_settlement=settlement,
        )
        statuses = tuple(
            _insert_exact(connection, record)
            for record in _checkpoint_records(
                owner_ref=owner,
                authority_bundle=bundle,
                visibility_policy=policy,
                palette=checked_palette,
                projection=material_projection,
            )
        )
        distinct_statuses = set(statuses)
        if len(distinct_statuses) != 1:
            raise NarrativeMaterialPersistenceError(
                "narrative_material_partial_checkpoint_conflict"
            )
        status = statuses[0]
        connection.commit()
        return NarrativeMaterialPersistenceResult(
            projection_ref=material_projection.projection_ref,
            projection_digest=material_projection.content_digest,
            palette_ref=checked_palette.palette_ref,
            run_attempt_id=bundle.run_attempt_id,
            status=status,
        )
    except Exception as exc:
        connection.rollback()
        if isinstance(
            exc,
            (NarrativeMaterialPersistenceBackendError, PsycopgError),
        ):
            detail_digest = canonical_digest(
                {
                    "run_attempt_id": bundle.run_attempt_id,
                    "projection_ref": material_projection.projection_ref,
                    "exception_type": type(exc).__name__,
                }
            )
            raise NarrativeMaterialPersistenceOperationalError(
                technical_detail_ref="technical-detail:sha256:" + detail_digest
            ) from exc
        raise


def _validated_checkpoint(
    *,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
    visibility_policy: PublicationFieldVisibilityPolicy,
    palette: PublicClaimPalette,
    projection: NarrativeMaterialProjection,
    evidence_entries: Sequence[EvidenceLedgerEntry],
) -> tuple[
    AuthorityBundle,
    ClaimSettlement,
    PublicationFieldVisibilityPolicy,
    PublicClaimPalette,
    NarrativeMaterialProjection,
]:
    if type(authority_bundle) is not AuthorityBundle:
        raise NarrativeMaterialPersistenceError(
            "narrative_material_authority_bundle_invalid"
        )
    bundle_manifest = {
        key: value
        for key, value in authority_bundle.to_dict().items()
        if key
        not in {
            "bundle_ref",
            "authority_namespace_ref",
            "bundle_digest",
            "seal_state",
            "sealed_at",
            "content_digest",
        }
    }
    bundle_digest = canonical_digest(bundle_manifest)
    if (
        authority_bundle.bundle_digest != bundle_digest
        or authority_bundle.content_digest != bundle_digest
        or not authority_bundle.bundle_ref.endswith(":sha256:" + bundle_digest)
        or authority_bundle.seal_state != "sealed"
    ):
        raise NarrativeMaterialPersistenceError(
            "narrative_material_authority_bundle_invalid"
        )
    try:
        settlement = validate_typed_claim_settlement(claim_settlement)
    except (AttributeError, TypeError, ValueError) as exc:
        raise NarrativeMaterialPersistenceError(
            "narrative_material_claim_settlement_invalid"
        ) from exc
    if (
        authority_bundle.authority_namespace_ref != settlement.authority_namespace_ref
        or authority_bundle.claim_settlement_ref != settlement.settlement_ref
        or authority_bundle.claim_settlement_digest != settlement.content_digest
        or authority_bundle.claim_graph_ref != settlement.claim_graph_ref
        or authority_bundle.claim_graph_digest != settlement.claim_graph_digest
        or authority_bundle.authority_mode != settlement.claim_graph.authority_mode
        or tuple(authority_bundle.obligation_coverage_refs)
        != tuple(item.coverage_ref for item in settlement.obligation_coverage)
        or tuple(authority_bundle.verified_claim_refs)
        != tuple(item.claim_ref for item in settlement.accepted_claims)
        or authority_bundle.claim_verifier_report_ref
        != settlement.claim_verifier_report_ref
    ):
        raise NarrativeMaterialPersistenceError(
            "narrative_material_bundle_settlement_closure_invalid"
        )
    if type(visibility_policy) is not PublicationFieldVisibilityPolicy:
        raise NarrativeMaterialPersistenceError(
            "narrative_material_visibility_policy_invalid"
        )
    try:
        policy = PublicationFieldVisibilityPolicy.from_dict(visibility_policy.to_dict())
    except (AttributeError, TypeError, ValueError) as exc:
        raise NarrativeMaterialPersistenceError(
            "narrative_material_visibility_policy_invalid"
        ) from exc
    if policy != visibility_policy or type(palette) is not PublicClaimPalette:
        raise NarrativeMaterialPersistenceError("narrative_material_palette_invalid")
    try:
        palette.assert_integrity(visibility_policy=policy)
    except (AttributeError, TypeError, ValueError) as exc:
        raise NarrativeMaterialPersistenceError(
            "narrative_material_palette_invalid"
        ) from exc
    if (
        palette.authority_bundle_ref != authority_bundle.bundle_ref
        or palette.authority_bundle_digest != authority_bundle.bundle_digest
        or palette.authority_mode != authority_bundle.authority_mode
        or palette.field_visibility_policy_ref != policy.policy_ref
        or palette.field_visibility_policy_digest != policy.content_digest
        or tuple(item.claim_ref for item in palette.claims)
        != tuple(authority_bundle.verified_claim_refs)
        or tuple(item.recommendation_ref for item in palette.recommendations)
        != tuple(authority_bundle.recommendation_refs)
        or tuple(item.limitation_ref for item in palette.limitations)
        != tuple(authority_bundle.limitation_refs)
    ):
        raise NarrativeMaterialPersistenceError(
            "narrative_material_palette_closure_invalid"
        )
    if type(projection) is not NarrativeMaterialProjection:
        raise NarrativeMaterialPersistenceError("narrative_material_projection_invalid")
    try:
        checked_projection = NarrativeMaterialProjection.from_dict(
            projection.to_dict(),
            palette=palette,
            claim_settlement=settlement,
            evidence_entries=evidence_entries,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise NarrativeMaterialPersistenceError(
            "narrative_material_projection_invalid"
        ) from exc
    if checked_projection != projection or (
        projection.palette_ref != palette.palette_ref
        or projection.palette_digest != palette.content_digest
        or projection.claim_settlement_ref != settlement.settlement_ref
        or projection.claim_settlement_digest != settlement.content_digest
        or projection.authority_mode != authority_bundle.authority_mode
    ):
        raise NarrativeMaterialPersistenceError(
            "narrative_material_projection_closure_invalid"
        )
    return authority_bundle, settlement, policy, palette, checked_projection


def _validate_preflight(
    row: Any,
    *,
    owner_ref: str,
    thread_ref: str,
    authority_bundle: AuthorityBundle,
    claim_settlement: ClaimSettlement,
) -> None:
    if (
        str(_field(row, "owner_ref", 0) or "") != owner_ref
        or str(_field(row, "thread_ref", 1) or "") != thread_ref
    ):
        raise NarrativeMaterialPersistenceError(
            "narrative_material_owner_scope_conflict"
        )
    if (
        canonical_value(_json_value(_field(row, "authority_bundle_payload", 2)))
        != canonical_value(authority_bundle.to_dict())
        or str(_field(row, "authority_bundle_digest", 3) or "")
        != authority_bundle.bundle_digest
        or canonical_value(_json_value(_field(row, "claim_settlement_payload", 4)))
        != canonical_value(claim_settlement.to_dict())
        or str(_field(row, "claim_settlement_digest", 5) or "")
        != claim_settlement.content_digest
    ):
        raise NarrativeMaterialPersistenceError(
            "narrative_material_upstream_authority_conflict"
        )


def _checkpoint_records(
    *,
    owner_ref: str,
    authority_bundle: AuthorityBundle,
    visibility_policy: PublicationFieldVisibilityPolicy,
    palette: PublicClaimPalette,
    projection: NarrativeMaterialProjection,
) -> tuple[_InsertRecord, ...]:
    run_attempt_id = authority_bundle.run_attempt_id
    common = {"owner_ref": owner_ref, "run_attempt_id": run_attempt_id}
    records = [
        _InsertRecord(
            table="publication_visibility_policies",
            identity_column="policy_ref",
            conflict_columns=("owner_ref", "run_attempt_id", "policy_ref"),
            columns={
                **common,
                "policy_ref": visibility_policy.policy_ref,
                "policy_id": visibility_policy.policy_id,
                "policy_revision": visibility_policy.revision,
                "content_digest": visibility_policy.content_digest,
                "payload": visibility_policy.to_dict(),
            },
        ),
        _InsertRecord(
            table="public_claim_palettes",
            identity_column="palette_ref",
            conflict_columns=("palette_ref",),
            columns={
                **common,
                "palette_ref": palette.palette_ref,
                "authority_bundle_ref": palette.authority_bundle_ref,
                "authority_bundle_digest": palette.authority_bundle_digest,
                "authority_mode": palette.authority_mode,
                "field_visibility_policy_ref": (palette.field_visibility_policy_ref),
                "field_visibility_policy_digest": (
                    palette.field_visibility_policy_digest
                ),
                "content_digest": palette.content_digest,
                "payload": palette.to_dict(),
            },
        ),
    ]
    records.extend(
        _InsertRecord(
            table="public_limitations",
            identity_column="limitation_ref",
            conflict_columns=(
                "owner_ref",
                "run_attempt_id",
                "palette_ref",
                "limitation_ref",
            ),
            columns={
                **common,
                "palette_ref": palette.palette_ref,
                "limitation_ref": limitation.limitation_ref,
                "limitation_handle": limitation.limitation_handle,
                "public_context": limitation.public_context,
                "content_digest": limitation.content_digest,
                "payload": limitation.to_dict(),
            },
            json_columns=frozenset({"payload", "public_context"}),
        )
        for limitation in palette.limitations
    )
    for claim in palette.claims:
        records.append(
            _InsertRecord(
                table="public_claims",
                identity_column="public_claim_ref",
                conflict_columns=("public_claim_ref",),
                columns={
                    **common,
                    "public_claim_ref": claim.public_claim_ref,
                    "palette_ref": palette.palette_ref,
                    "claim_ref": claim.claim_ref,
                    "claim_key_ref": claim.claim_key_ref,
                    "claim_class": claim.claim_class,
                    "content_digest": claim.content_digest,
                    "payload": claim.to_dict(),
                },
            )
        )
        records.extend(
            _InsertRecord(
                table="public_fact_descriptors",
                identity_column="fact_ref",
                conflict_columns=("fact_ref",),
                columns={
                    **common,
                    "fact_ref": fact.fact_ref,
                    "palette_ref": palette.palette_ref,
                    "public_claim_ref": claim.public_claim_ref,
                    "claim_ref": fact.claim_ref,
                    "source_material_ref": fact.source_material_ref,
                    "content_digest": fact.content_digest,
                    "payload": fact.to_dict(),
                },
            )
            for fact in claim.facts
        )
    records.extend(
        _InsertRecord(
            table="public_recommendations",
            identity_column="public_recommendation_ref",
            conflict_columns=("public_recommendation_ref",),
            columns={
                **common,
                "public_recommendation_ref": (recommendation.public_recommendation_ref),
                "palette_ref": palette.palette_ref,
                "recommendation_ref": recommendation.recommendation_ref,
                "recommendation_digest": recommendation.recommendation_digest,
                "content_digest": recommendation.content_digest,
                "payload": recommendation.to_dict(),
            },
        )
        for recommendation in palette.recommendations
    )
    records.append(
        _InsertRecord(
            table="narrative_material_projections",
            identity_column="projection_ref",
            conflict_columns=("projection_ref",),
            columns={
                **common,
                "projection_ref": projection.projection_ref,
                "palette_ref": projection.palette_ref,
                "palette_digest": projection.palette_digest,
                "claim_settlement_ref": projection.claim_settlement_ref,
                "claim_settlement_digest": projection.claim_settlement_digest,
                "content_digest": projection.content_digest,
                "payload": projection.to_dict(),
            },
        )
    )
    return tuple(records)


def _insert_exact(connection: Any, record: _InsertRecord) -> str:
    names = tuple(record.columns)
    values = tuple(
        f"%({name})s::jsonb" if name in record.json_columns else f"%({name})s"
        for name in names
    )
    params = {
        name: (
            json.dumps(canonical_value(value), sort_keys=True, separators=(",", ":"))
            if name in record.json_columns
            else value
        )
        for name, value in record.columns.items()
    }
    inserted = connection.execute(
        f"""
        INSERT INTO waje_runtime.{record.table} ({", ".join(names)})
        VALUES ({", ".join(values)})
        ON CONFLICT DO NOTHING
        RETURNING {record.identity_column}
        """,
        params,
    ).fetchone()
    if inserted is not None:
        if str(_field(inserted, record.identity_column, 0)) != str(
            record.columns[record.identity_column]
        ):
            raise NarrativeMaterialPersistenceError(
                f"narrative_material_insert_identity_conflict:{record.table}"
            )
        return "inserted"
    replay_where = " AND ".join(
        f"{name} = %({name})s" for name in record.conflict_columns
    )
    row = connection.execute(
        f"""
        /* narrative_material_exact_replay:{record.table} */
        SELECT {", ".join(names)}
        FROM waje_runtime.{record.table}
        WHERE {replay_where}
        """,
        {name: record.columns[name] for name in record.conflict_columns},
    ).fetchone()
    if row is None:
        raise NarrativeMaterialPersistenceError(
            f"narrative_material_exact_replay_missing:{record.table}"
        )
    for index, name in enumerate(names):
        stored = _field(row, name, index)
        if name in record.json_columns:
            stored = _json_value(stored)
        if canonical_value(stored) != canonical_value(record.columns[name]):
            raise NarrativeMaterialPersistenceError(
                f"narrative_material_exact_replay_conflict:{record.table}:{name}"
            )
    return "replayed"


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise NarrativeMaterialPersistenceError(error)
    return value


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise NarrativeMaterialPersistenceError(
                "narrative_material_json_invalid"
            ) from exc
    return value


def _field(row: Any, name: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    if hasattr(row, name):
        return getattr(row, name)
    return row[index]


__all__ = (
    "NarrativeMaterialPersistenceBackendError",
    "NarrativeMaterialPersistenceError",
    "NarrativeMaterialPersistenceOperationalError",
    "NarrativeMaterialPersistenceResult",
    "persist_narrative_material_projection",
)
