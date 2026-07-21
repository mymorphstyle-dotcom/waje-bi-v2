from __future__ import annotations

from bi_agent.runtime.clickhouse_runtime import ClickHouseRuntime
from bi_agent.runtime.dataset_catalog import (
    DatasetCatalog,
    DatasetReleaseAuthorityRecord,
    DatasetReleaseResolver,
    DatasetSnapshot,
    DatasetSnapshotImmutableProjection,
    dataset_release_authority_integrity_errors,
)
from bi_agent.runtime.evidence_authority import (
    RowsPayloadLoader,
    RuntimeEvidenceAuthority,
    RuntimeEvidenceResolver,
    RuntimeEvidenceWriter,
)
from bi_agent.runtime.plan_authority import AuthorityContext
from bi_agent.runtime.query_executor import ClickHouseQueryExecutor
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
    runtime_registry_integrity_error,
)


class AnalysisRuntimeContractError(ValueError):
    pass


class AnalysisRuntime:
    """Typed physical-analysis services used by the authoritative workflow."""

    registry: RuntimeContractRegistry
    executor: ClickHouseQueryExecutor
    release_resolver: DatasetReleaseResolver
    evidence_authority: RuntimeEvidenceAuthority
    evidence_resolver: RuntimeEvidenceResolver
    rows_loader: RowsPayloadLoader
    evidence_writer: RuntimeEvidenceWriter

    def __init__(
        self,
        *,
        registry: RuntimeContractRegistry,
        executor: ClickHouseQueryExecutor,
        release_resolver: DatasetReleaseResolver,
        evidence_authority: RuntimeEvidenceAuthority,
    ) -> None:
        registry_error = runtime_registry_integrity_error(registry)
        if registry_error:
            raise AnalysisRuntimeContractError(registry_error)
        if not callable(getattr(release_resolver, "resolve_dataset_release", None)):
            raise AnalysisRuntimeContractError(
                "analysis_runtime_release_resolver_invalid"
            )
        executor.bind_runtime_registry(registry)
        self.registry = registry
        self.executor = executor
        self.release_resolver = release_resolver
        self.evidence_authority = evidence_authority
        self.evidence_resolver = evidence_authority
        self.rows_loader = evidence_authority.rows_loader
        self.evidence_writer = evidence_authority._runtime_writer()

    @classmethod
    def from_environment(
        cls,
        store: DatasetReleaseResolver,
    ) -> AnalysisRuntime:
        registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
        authority = RuntimeEvidenceAuthority(runtime_registry=registry)
        executor = ClickHouseQueryExecutor(
            ClickHouseRuntime.from_env(),
            evidence_resolver=authority,
            rows_loader=authority.rows_loader,
            evidence_writer=authority._runtime_writer(),
            release_resolver=store,
            runtime_registry=registry,
        )
        return cls(
            registry=registry,
            executor=executor,
            release_resolver=store,
            evidence_authority=authority,
        )

    def catalog_for_authority_context(
        self,
        authority_context: AuthorityContext,
    ) -> DatasetCatalog:
        return pinned_dataset_catalog(
            authority_context,
            release_resolver=self.release_resolver,
        )


def pinned_dataset_catalog(
    authority_context: AuthorityContext,
    *,
    release_resolver: DatasetReleaseResolver,
) -> DatasetCatalog:
    if type(authority_context) is not AuthorityContext:
        raise AnalysisRuntimeContractError("analysis_runtime_authority_context_invalid")
    context = AuthorityContext.from_dict(authority_context.to_dict())
    resolve_release = getattr(release_resolver, "resolve_dataset_release", None)
    if not callable(resolve_release):
        raise AnalysisRuntimeContractError("analysis_runtime_release_resolver_invalid")

    requested_refs = set(context.snapshot_refs)
    snapshots: dict[str, DatasetSnapshot] = {}
    resolved_release_refs: set[str] = set()
    for release_ref in context.release_refs:
        try:
            release = resolve_release(release_ref)
        except KeyError as exc:
            raise AnalysisRuntimeContractError(
                f"authority_context_release_unavailable:{release_ref}"
            ) from exc
        if not isinstance(release, DatasetReleaseAuthorityRecord):
            raise AnalysisRuntimeContractError(
                f"authority_context_release_record_invalid:{release_ref}"
            )
        if (
            release.release_ref != release_ref
            or dataset_release_authority_integrity_errors(release)
        ):
            raise AnalysisRuntimeContractError(
                f"authority_context_release_integrity_failed:{release_ref}"
            )
        release_member_refs = set(release.snapshot_refs)
        if release_member_refs != requested_refs.intersection(release_member_refs):
            raise AnalysisRuntimeContractError(
                f"authority_context_release_snapshot_closure_mismatch:{release_ref}"
            )
        resolved_release_refs.add(release_ref)
        for member in release.member_projections:
            if member.snapshot_ref not in requested_refs:
                continue
            if member.snapshot_ref in snapshots:
                raise AnalysisRuntimeContractError(
                    f"authority_context_snapshot_release_ambiguous:{member.snapshot_ref}"
                )
            snapshots[member.snapshot_ref] = _snapshot_from_release_member(
                member,
                release=release,
            )

    if resolved_release_refs != set(context.release_refs):
        raise AnalysisRuntimeContractError("authority_context_release_closure_mismatch")
    missing = tuple(sorted(requested_refs - set(snapshots)))
    if missing:
        raise AnalysisRuntimeContractError(
            "authority_context_snapshot_unavailable:" + ",".join(missing)
        )
    _validate_context_dataset_closure(context, snapshots)
    return DatasetCatalog(
        (snapshots[ref] for ref in context.snapshot_refs),
        release_resolver=release_resolver,
    )


def _snapshot_from_release_member(
    member: DatasetSnapshotImmutableProjection,
    *,
    release: DatasetReleaseAuthorityRecord,
) -> DatasetSnapshot:
    if member.release_ref != release.release_ref:
        raise AnalysisRuntimeContractError(
            f"authority_context_snapshot_release_mismatch:{member.snapshot_ref}"
        )
    # The context pins the release's point-in-time visibility. Current publication
    # status may later become superseded without changing the immutable member.
    return DatasetSnapshot(
        snapshot_ref=member.snapshot_ref,
        dataset_id=member.dataset_id,
        physical_table=member.physical_table,
        watermark=member.watermark,
        schema_fingerprint=member.schema_fingerprint,
        schema_fields=member.schema_fields,
        contract_ref=member.contract_ref,
        loaded_at=member.loaded_at,
        status="active",
        evidence_state=member.evidence_state,
        reconciliation_status=member.reconciliation_status,
        reconciliation_ref=member.reconciliation_ref,
        logical_snapshot_id=member.logical_snapshot_id,
        load_revision=member.load_revision,
        release_ref=member.release_ref,
        authority_record_ref=release.authority_record_ref,
        rows_content_hash=member.rows_content_hash,
        snapshot_id=member.snapshot_id,
        source_load_manifest_ref=member.source_load_manifest_ref,
        runtime_binding_ref=member.runtime_binding_ref,
        source_checksums=member.source_checksums,
        row_count=member.row_count,
        date_range=member.date_range,
        no_data_partitions=member.no_data_partitions,
        no_data_partition_windows=member.no_data_partition_windows,
    )


def _validate_context_dataset_closure(
    context: AuthorityContext,
    snapshots: dict[str, DatasetSnapshot],
) -> None:
    for coverage in context.dataset_coverage:
        expected_refs = set(coverage["snapshot_refs"])
        if not expected_refs:
            continue
        release_ref = str(coverage["release_ref"])
        actual_refs = {
            snapshot.snapshot_ref
            for snapshot in snapshots.values()
            if snapshot.dataset_id == coverage["dataset_id"]
            and snapshot.release_ref == release_ref
        }
        if actual_refs != expected_refs:
            raise AnalysisRuntimeContractError(
                f"authority_context_dataset_snapshot_closure_mismatch:{coverage['dataset_id']}"
            )


__all__ = [
    "AnalysisRuntime",
    "AnalysisRuntimeContractError",
    "pinned_dataset_catalog",
]
