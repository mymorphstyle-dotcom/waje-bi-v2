from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from functools import lru_cache
import hashlib
import json
from typing import Any, Iterable, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class DatasetSnapshot:
    snapshot_ref: str
    dataset_id: str
    physical_table: str
    watermark: str
    schema_fingerprint: str
    schema_fields: tuple[str, ...]
    contract_ref: str
    permission_scopes: tuple[str, ...]
    loaded_at: str
    status: str
    evidence_state: str = "claim_ready"
    reconciliation_status: str = "not_applicable"
    reconciliation_ref: str = ""
    logical_snapshot_id: str = ""
    load_revision: str = ""
    release_ref: str = ""
    authority_record_ref: str = ""
    rows_content_hash: str = ""
    snapshot_id: str = ""
    source_load_manifest_ref: str = ""
    runtime_binding_ref: str = ""
    source_checksums: tuple[tuple[str, str], ...] = ()
    row_count: int = -1
    date_range: tuple[str, ...] = ()
    no_data_partitions: tuple[str, ...] = ()
    no_data_partition_windows: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DatasetSnapshotImmutableProjection:
    snapshot_ref: str
    dataset_id: str
    physical_table: str
    watermark: str
    schema_fingerprint: str
    schema_fields: tuple[str, ...]
    contract_ref: str
    permission_scopes: tuple[str, ...]
    loaded_at: str
    evidence_state: str
    reconciliation_status: str
    reconciliation_ref: str
    logical_snapshot_id: str
    load_revision: str
    release_ref: str
    rows_content_hash: str
    snapshot_id: str = ""
    source_load_manifest_ref: str = ""
    runtime_binding_ref: str = ""
    source_checksums: tuple[tuple[str, str], ...] = ()
    row_count: int = -1
    date_range: tuple[str, ...] = ()
    no_data_partitions: tuple[str, ...] = ()
    no_data_partition_windows: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_checksums"] = dict(self.source_checksums)
        return payload


@dataclass(frozen=True)
class DatasetReleaseAuthorityRecord:
    release_ref: str
    authority_record_ref: str
    digest: str
    logical_snapshot_id: str
    load_revision: str
    member_projections: tuple[DatasetSnapshotImmutableProjection, ...]
    integrity_errors: tuple[str, ...] = ()

    @property
    def snapshot_refs(self) -> tuple[str, ...]:
        return tuple(item.snapshot_ref for item in self.member_projections)

    @property
    def dataset_ids(self) -> tuple[str, ...]:
        return tuple(item.dataset_id for item in self.member_projections)

    @property
    def physical_tables(self) -> tuple[str, ...]:
        return tuple(item.physical_table for item in self.member_projections)

    @property
    def rows_content_hashes(self) -> tuple[str, ...]:
        return tuple(item.rows_content_hash for item in self.member_projections)

    @property
    def schema_fingerprints(self) -> tuple[str, ...]:
        return tuple(item.schema_fingerprint for item in self.member_projections)

    @property
    def evidence_states(self) -> tuple[str, ...]:
        return tuple(item.evidence_state for item in self.member_projections)

    @property
    def reconciliation_statuses(self) -> tuple[str, ...]:
        return tuple(item.reconciliation_status for item in self.member_projections)

    @property
    def reconciliation_refs(self) -> tuple[str, ...]:
        return tuple(item.reconciliation_ref for item in self.member_projections)

    def to_dict(self) -> dict[str, Any]:
        return {
            "release_ref": self.release_ref,
            "authority_record_ref": self.authority_record_ref,
            "digest": self.digest,
            "logical_snapshot_id": self.logical_snapshot_id,
            "load_revision": self.load_revision,
            "member_projections": [
                item.to_dict() for item in self.member_projections
            ],
            "snapshot_refs": self.snapshot_refs,
            "integrity_errors": self.integrity_errors,
        }


class DatasetReleaseResolver(Protocol):
    def resolve_dataset_release(
        self,
        release_ref: str,
    ) -> DatasetReleaseAuthorityRecord: ...


class DatasetCatalog:
    def __init__(
        self,
        snapshots: Iterable[DatasetSnapshot] = (),
        *,
        release_resolver: DatasetReleaseResolver | None = None,
    ) -> None:
        self._snapshots = tuple(snapshots)
        self._release_resolver = release_resolver

    def resolve(
        self,
        dataset_id: str,
        *,
        as_of: datetime,
        permission_scope: str,
        evidence_states: tuple[str, ...] = ("claim_ready",),
        release_resolver: DatasetReleaseResolver | None = None,
    ) -> DatasetSnapshot:
        _validate_evidence_states(evidence_states)
        eligible = [
            candidate
            for candidate in self.as_of_candidates(
                dataset_id,
                as_of=as_of,
                evidence_states=evidence_states,
                release_resolver=release_resolver,
            )
            if permission_scope in candidate[1].permission_scopes
        ]
        if not eligible:
            raise KeyError(f"dataset_snapshot_unavailable:{dataset_id}")
        return max(eligible, key=lambda candidate: (candidate[0], candidate[1].snapshot_ref))[1]

    def as_of_candidates(
        self,
        dataset_id: str,
        *,
        as_of: datetime,
        evidence_states: tuple[str, ...] = ("claim_ready",),
        release_resolver: DatasetReleaseResolver | None = None,
    ) -> tuple[tuple[datetime, DatasetSnapshot], ...]:
        _validate_evidence_states(evidence_states)
        as_of_utc = _aware_utc(as_of, field="as_of")
        eligible = []
        for item in self._snapshots:
            if (
                item.dataset_id != dataset_id
                or item.status != "active"
                or item.evidence_state not in evidence_states
                or not _snapshot_has_release_authority(
                    item,
                    release_resolver or self._release_resolver,
                )
            ):
                continue
            loaded_at_utc = _parse_datetime(item.loaded_at)
            if loaded_at_utc <= as_of_utc:
                eligible.append((loaded_at_utc, item))
        return tuple(eligible)

    def common_watermark(self, dataset_ids: tuple[str, ...]) -> date:
        watermarks = []
        for dataset_id in dataset_ids:
            candidates = [
                date.fromisoformat(item.watermark)
                for item in self._snapshots
                if item.dataset_id == dataset_id and item.status == "active"
                and item.evidence_state == "claim_ready"
                and _snapshot_has_release_authority(item, self._release_resolver)
            ]
            if not candidates:
                raise KeyError(f"dataset_snapshot_unavailable:{dataset_id}")
            watermarks.append(max(candidates))
        return min(watermarks)

    def snapshots(self) -> tuple[DatasetSnapshot, ...]:
        return self._snapshots


def dataset_snapshot_release_ref(
    logical_snapshot_id: str,
    load_revision: str,
    snapshot_refs: Iterable[str],
) -> str:
    payload = {
        "logical_snapshot_id": str(logical_snapshot_id),
        "load_revision": str(load_revision),
        "snapshot_refs": sorted(str(ref) for ref in snapshot_refs),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "dataset-release:sha256:" + hashlib.sha256(encoded).hexdigest()


def build_dataset_release_authority_record(
    payloads: Sequence[Mapping[str, Any]],
) -> DatasetReleaseAuthorityRecord:
    normalized, logical_id, revision, release_ref = (
        validate_dataset_snapshot_release_payloads(payloads)
    )
    members = tuple(
        sorted(
            (immutable_dataset_snapshot_projection(item) for item in normalized),
            key=lambda item: item.snapshot_ref,
        )
    )
    content = {
        "release_ref": release_ref,
        "logical_snapshot_id": logical_id,
        "load_revision": revision,
        "member_projections": [item.to_dict() for item in members],
    }
    digest = _canonical_digest(content)
    record = DatasetReleaseAuthorityRecord(
        release_ref=release_ref,
        authority_record_ref=f"dataset-release-authority:sha256:{digest}",
        digest=digest,
        logical_snapshot_id=logical_id,
        load_revision=revision,
        member_projections=members,
    )
    return replace(
        record,
        integrity_errors=dataset_release_authority_integrity_errors(record),
    )


def dataset_release_authority_record_from_mapping(
    value: Mapping[str, Any],
) -> DatasetReleaseAuthorityRecord:
    members_value = value.get("member_projections")
    if not isinstance(members_value, (tuple, list)):
        raise ValueError("dataset_release_authority_member_projections")
    members = tuple(
        immutable_dataset_snapshot_projection(item)
        for item in members_value
        if isinstance(item, Mapping)
    )
    if len(members) != len(members_value):
        raise ValueError("dataset_release_authority_member_projections")
    record = DatasetReleaseAuthorityRecord(
        release_ref=str(value.get("release_ref") or ""),
        authority_record_ref=str(value.get("authority_record_ref") or ""),
        digest=str(value.get("digest") or ""),
        logical_snapshot_id=str(value.get("logical_snapshot_id") or ""),
        load_revision=str(value.get("load_revision") or ""),
        member_projections=members,
        integrity_errors=tuple(str(item) for item in value.get("integrity_errors") or ()),
    )
    errors = list(dataset_release_authority_integrity_errors(record))
    stored_refs = tuple(str(item) for item in value.get("snapshot_refs") or ())
    if stored_refs != record.snapshot_refs:
        errors.append("dataset_release_authority_stored_membership")
    return replace(record, integrity_errors=tuple(dict.fromkeys(errors)))


def dataset_release_authority_integrity_errors(
    record: DatasetReleaseAuthorityRecord,
) -> tuple[str, ...]:
    if type(record) is not DatasetReleaseAuthorityRecord:
        return ("dataset_release_authority_type",)
    errors: list[str] = []
    if (
        len(record.member_projections) != 2
        or any(
            type(item) is not DatasetSnapshotImmutableProjection
            for item in record.member_projections
        )
    ):
        errors.append("dataset_release_authority_member_count")
    if tuple(sorted(record.snapshot_refs)) != record.snapshot_refs:
        errors.append("dataset_release_authority_member_order")
    if set(record.dataset_ids) != {"market_dashboard", "market_dashboard_channel"}:
        errors.append("dataset_release_authority_dataset_set")
    expected_release_ref = dataset_snapshot_release_ref(
        record.logical_snapshot_id,
        record.load_revision,
        record.snapshot_refs,
    )
    if record.release_ref != expected_release_ref:
        errors.append("dataset_release_authority_release_ref")
    content = {
        "release_ref": record.release_ref,
        "logical_snapshot_id": record.logical_snapshot_id,
        "load_revision": record.load_revision,
        "member_projections": [
            item.to_dict() for item in record.member_projections
        ],
    }
    expected_digest = _canonical_digest(content)
    if record.digest != expected_digest:
        errors.append("dataset_release_authority_digest")
    if record.authority_record_ref != f"dataset-release-authority:sha256:{expected_digest}":
        errors.append("dataset_release_authority_record_ref")
    if record.integrity_errors:
        errors.extend(record.integrity_errors)
    return tuple(dict.fromkeys(errors))


def snapshot_matches_release_authority(
    snapshot: DatasetSnapshot,
    record: DatasetReleaseAuthorityRecord,
) -> bool:
    try:
        index = record.snapshot_refs.index(snapshot.snapshot_ref)
    except ValueError:
        return False
    return (
        snapshot.release_ref == record.release_ref
        and snapshot.authority_record_ref == record.authority_record_ref
        and immutable_dataset_snapshot_projection(snapshot)
        == record.member_projections[index]
    )


def immutable_dataset_snapshot_projection(
    value: DatasetSnapshot | Mapping[str, Any],
) -> DatasetSnapshotImmutableProjection:
    if isinstance(value, DatasetSnapshot):
        payload = value.to_dict()
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        raise TypeError("dataset_snapshot_projection_type")
    source_checksums = payload.get("source_checksums") or {}
    if isinstance(source_checksums, Mapping):
        checksum_pairs = tuple(
            sorted(
                (str(key), str(checksum))
                for key, checksum in source_checksums.items()
            )
        )
    elif isinstance(source_checksums, (tuple, list)):
        try:
            checksum_pairs = tuple(
                sorted((str(key), str(checksum)) for key, checksum in source_checksums)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("dataset_snapshot_projection_source_checksums") from exc
    else:
        raise ValueError("dataset_snapshot_projection_source_checksums")
    row_count = payload.get("row_count", -1)
    if isinstance(row_count, bool) or not isinstance(row_count, int):
        raise ValueError("dataset_snapshot_projection_row_count")
    return DatasetSnapshotImmutableProjection(
        snapshot_ref=str(payload.get("snapshot_ref") or ""),
        dataset_id=str(payload.get("dataset_id") or ""),
        physical_table=str(payload.get("physical_table") or ""),
        watermark=str(payload.get("watermark") or ""),
        schema_fingerprint=str(payload.get("schema_fingerprint") or ""),
        schema_fields=_projection_string_tuple(payload.get("schema_fields")),
        contract_ref=str(payload.get("contract_ref") or ""),
        permission_scopes=_projection_string_tuple(
            payload.get("permission_scopes")
        ),
        loaded_at=str(payload.get("loaded_at") or ""),
        evidence_state=str(payload.get("evidence_state") or "claim_ready"),
        reconciliation_status=str(
            payload.get("reconciliation_status") or "not_applicable"
        ),
        reconciliation_ref=str(payload.get("reconciliation_ref") or ""),
        logical_snapshot_id=str(payload.get("logical_snapshot_id") or ""),
        load_revision=str(payload.get("load_revision") or ""),
        release_ref=str(payload.get("release_ref") or ""),
        rows_content_hash=str(payload.get("rows_content_hash") or ""),
        snapshot_id=str(payload.get("snapshot_id") or ""),
        source_load_manifest_ref=str(
            payload.get("source_load_manifest_ref") or ""
        ),
        runtime_binding_ref=str(payload.get("runtime_binding_ref") or ""),
        source_checksums=checksum_pairs,
        row_count=row_count,
        date_range=_projection_string_tuple(payload.get("date_range")),
        no_data_partitions=_projection_string_tuple(
            payload.get("no_data_partitions")
        ),
        no_data_partition_windows=_projection_string_tuple(
            payload.get("no_data_partition_windows")
        ),
    )


def _projection_string_tuple(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, (tuple, list)) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError("dataset_snapshot_projection_string_sequence")
    return tuple(value)


@lru_cache(maxsize=None)
def canonical_dataset_requires_release(dataset_id: str) -> bool:
    from bi_agent.runtime.runtime_contract_registry import (
        CANONICAL_RUNTIME_BINDINGS_PATH,
        RuntimeContractRegistry,
    )

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    try:
        return registry.dataset(dataset_id).get("requires_release") is True
    except KeyError:
        return False


def _snapshot_has_release_authority(
    snapshot: DatasetSnapshot,
    resolver: DatasetReleaseResolver | None,
) -> bool:
    if not canonical_dataset_requires_release(snapshot.dataset_id):
        return True
    if resolver is None or not snapshot.release_ref or not snapshot.authority_record_ref:
        return False
    try:
        record = resolver.resolve_dataset_release(snapshot.release_ref)
    except (KeyError, TypeError, ValueError):
        return False
    return (
        not dataset_release_authority_integrity_errors(record)
        and snapshot_matches_release_authority(snapshot, record)
    )


def _canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_dataset_snapshot_release_payloads(
    payloads: Sequence[Mapping[str, Any]],
) -> tuple[tuple[dict[str, Any], ...], str, str, str]:
    normalized = tuple(dict(payload) for payload in payloads)
    if len(normalized) != 2:
        raise ValueError("dataset_snapshot_release_dataset_set")
    dataset_ids = {str(item.get("dataset_id") or "") for item in normalized}
    if dataset_ids != {"market_dashboard", "market_dashboard_channel"}:
        raise ValueError("dataset_snapshot_release_dataset_set")
    snapshot_refs = tuple(str(item.get("snapshot_ref") or "") for item in normalized)
    if any(not ref for ref in snapshot_refs) or len(set(snapshot_refs)) != 2:
        raise ValueError("dataset_snapshot_release_snapshot_refs")
    logical_ids = {str(item.get("logical_snapshot_id") or "") for item in normalized}
    revisions = {str(item.get("load_revision") or "") for item in normalized}
    release_refs = {str(item.get("release_ref") or "") for item in normalized}
    if len(logical_ids) != 1 or "" in logical_ids:
        raise ValueError("dataset_snapshot_release_logical_snapshot")
    if len(revisions) != 1 or "" in revisions:
        raise ValueError("dataset_snapshot_release_revision")
    if len(release_refs) != 1 or "" in release_refs:
        raise ValueError("dataset_snapshot_release_ref")
    logical_id = next(iter(logical_ids))
    revision = next(iter(revisions))
    expected_release_ref = dataset_snapshot_release_ref(
        logical_id,
        revision,
        snapshot_refs,
    )
    if next(iter(release_refs)) != expected_release_ref:
        raise ValueError("dataset_snapshot_release_ref")
    allowed_evidence = {"claim_ready", "context_only", "blocked"}
    allowed_reconciliation = {
        "matched", "mismatch", "incomplete", "not_comparable", "not_applicable"
    }
    for item in normalized:
        if item.get("status") not in {"active", "no_data"}:
            raise ValueError("dataset_snapshot_release_status")
        if item.get("evidence_state") not in allowed_evidence:
            raise ValueError("dataset_snapshot_release_evidence_state")
        if item.get("reconciliation_status") not in allowed_reconciliation:
            raise ValueError("dataset_snapshot_release_reconciliation_status")
        try:
            date.fromisoformat(str(item.get("watermark") or ""))
        except ValueError as exc:
            raise ValueError("dataset_snapshot_release_watermark") from exc
        if not str(item.get("physical_table") or ""):
            raise ValueError("dataset_snapshot_release_physical_table")
        if not str(item.get("schema_fingerprint") or ""):
            raise ValueError("dataset_snapshot_release_schema")
        row_hash = str(item.get("rows_content_hash") or "")
        if len(row_hash) != 64 or any(character not in "0123456789abcdef" for character in row_hash):
            raise ValueError("dataset_snapshot_release_rows_hash")
    return normalized, logical_id, revision, expected_release_ref


def _parse_datetime(value: str) -> datetime:
    return _aware_utc(
        datetime.fromisoformat(value.replace("Z", "+00:00")),
        field="loaded_at",
    )


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"timezone_aware_required:{field}")
    return value.astimezone(timezone.utc)


def _validate_evidence_states(value: tuple[str, ...]) -> None:
    allowed = {"claim_ready", "context_only", "blocked"}
    if (
        not isinstance(value, tuple)
        or not value
        or any(item not in allowed for item in value)
    ):
        raise ValueError("dataset_evidence_states_invalid")
