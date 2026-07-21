from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from bi_agent.runtime.plan_authority import (
    AuthorityContext,
    PlanAuthorityContractError,
)
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry


def resolve_latest_authority_context(
    *,
    run_attempt_id: str,
    actual_as_of: datetime,
    runtime_registry: RuntimeContractRegistry,
    snapshot_records: Sequence[Mapping[str, Any]],
    release_resolver: Any,
) -> AuthorityContext:
    """Resolve one immutable latest-active release context for a run attempt."""

    if not isinstance(runtime_registry, RuntimeContractRegistry):
        raise PlanAuthorityContractError("authority_context_registry_invalid")
    if not isinstance(actual_as_of, datetime) or actual_as_of.tzinfo is None:
        raise PlanAuthorityContractError("authority_context_actual_as_of_invalid")
    resolve_release = getattr(release_resolver, "resolve_dataset_release", None)
    if not callable(resolve_release):
        raise PlanAuthorityContractError("authority_context_release_resolver_missing")
    if isinstance(snapshot_records, (str, bytes)) or not isinstance(
        snapshot_records, Sequence
    ):
        raise PlanAuthorityContractError("authority_context_snapshot_records_invalid")

    as_of_utc = actual_as_of.astimezone(timezone.utc)
    active_by_ref: dict[str, Mapping[str, Any]] = {}
    for record in snapshot_records:
        if not isinstance(record, Mapping):
            raise PlanAuthorityContractError(
                "authority_context_snapshot_records_invalid"
            )
        dataset_id = _required_string(
            record.get("dataset_id"), "authority_context_snapshot_records_invalid"
        )
        if dataset_id not in set(runtime_registry.dataset_ids):
            continue
        snapshot_ref = _required_string(
            record.get("snapshot_ref"), "authority_context_snapshot_records_invalid"
        )
        loaded_at = _loaded_at(record.get("loaded_at"))
        if str(record.get("status") or "") != "active" or loaded_at > as_of_utc:
            continue
        existing = active_by_ref.get(snapshot_ref)
        if existing is not None and dict(existing) != dict(record):
            raise PlanAuthorityContractError("authority_context_snapshot_ref_conflict")
        active_by_ref[snapshot_ref] = record

    release_cache: dict[str, Any] = {}

    def resolved_release(release_ref: str) -> Any:
        if release_ref in release_cache:
            return release_cache[release_ref]
        try:
            release = resolve_release(release_ref)
        except KeyError as exc:
            raise PlanAuthorityContractError(
                f"authority_context_release_unavailable:{release_ref}"
            ) from exc
        if str(getattr(release, "release_ref", "") or "") != release_ref or tuple(
            getattr(release, "integrity_errors", ()) or ()
        ):
            raise PlanAuthorityContractError(
                f"authority_context_release_integrity_failed:{release_ref}"
            )
        members = tuple(getattr(release, "member_projections", ()) or ())
        if not members:
            raise PlanAuthorityContractError(
                f"authority_context_release_membership_invalid:{release_ref}"
            )
        member_refs = {
            str(getattr(member, "snapshot_ref", "") or "") for member in members
        }
        if "" in member_refs or any(
            ref not in active_by_ref
            or str(active_by_ref[ref].get("release_ref") or "") != release_ref
            for ref in member_refs
        ):
            raise PlanAuthorityContractError(
                f"authority_context_release_not_active:{release_ref}"
            )
        release_cache[release_ref] = release
        return release

    coverage: list[dict[str, Any]] = []
    selected_release_refs: set[str] = set()
    selected_snapshot_refs: set[str] = set()
    for dataset_id in runtime_registry.dataset_ids:
        contract = runtime_registry.dataset(dataset_id)
        if contract.get("requires_release") is not True:
            coverage.append(
                _unavailable_coverage(
                    dataset_id,
                    availability="missing_contract",
                    reason="immutable_release_contract_missing",
                )
            )
            continue

        dataset_records = tuple(
            record
            for record in active_by_ref.values()
            if str(record.get("dataset_id") or "") == dataset_id
        )
        if not dataset_records:
            coverage.append(
                _unavailable_coverage(
                    dataset_id,
                    availability="unavailable",
                    reason="active_release_unavailable",
                )
            )
            continue
        if any(not str(record.get("release_ref") or "") for record in dataset_records):
            raise PlanAuthorityContractError(
                f"authority_context_active_snapshot_unreleased:{dataset_id}"
            )

        candidates: list[tuple[datetime, str, Any]] = []
        for release_ref in sorted(
            {str(record["release_ref"]) for record in dataset_records}
        ):
            release = resolved_release(release_ref)
            members = tuple(
                member
                for member in release.member_projections
                if str(member.dataset_id) == dataset_id
            )
            if not members:
                raise PlanAuthorityContractError(
                    f"authority_context_release_dataset_missing:{dataset_id}"
                )
            candidate_loaded_at = max(
                _loaded_at(active_by_ref[str(member.snapshot_ref)].get("loaded_at"))
                for member in members
            )
            candidates.append((candidate_loaded_at, release_ref, release))

        _, release_ref, release = max(candidates, key=lambda item: (item[0], item[1]))
        members = tuple(
            member
            for member in release.member_projections
            if str(member.dataset_id) == dataset_id
        )
        evidence_states = {str(member.evidence_state) for member in members}
        if not evidence_states.issubset({"claim_ready", "context_only"}):
            coverage.append(
                _unavailable_coverage(
                    dataset_id,
                    availability="unavailable",
                    reason="release_evidence_state_unavailable",
                )
            )
            continue
        availability = (
            "context_only" if "context_only" in evidence_states else "claim_ready"
        )
        member_snapshot_refs = tuple(
            sorted(str(member.snapshot_ref) for member in members)
        )
        coverage.append(
            {
                "dataset_id": dataset_id,
                "availability": availability,
                "release_ref": release_ref,
                "snapshot_refs": member_snapshot_refs,
                "limitation_ref": (
                    f"limitation:context-only:{dataset_id}"
                    if availability == "context_only"
                    else None
                ),
            }
        )
        selected_release_refs.add(release_ref)
        selected_snapshot_refs.update(member_snapshot_refs)

    return AuthorityContext.create(
        run_attempt_id=run_attempt_id,
        actual_as_of=as_of_utc,
        release_refs=tuple(sorted(selected_release_refs)),
        snapshot_refs=tuple(sorted(selected_snapshot_refs)),
        dataset_coverage=tuple(coverage),
        contract_versions={
            "runtime_bindings": runtime_registry.contract_version,
            "runtime_bindings_digest": runtime_registry.source_payload_digest,
        },
    )


def _unavailable_coverage(
    dataset_id: str, *, availability: str, reason: str
) -> dict[str, Any]:
    return {
        "dataset_id": dataset_id,
        "availability": availability,
        "release_ref": None,
        "snapshot_refs": (),
        "limitation_ref": f"limitation:{reason}:{dataset_id}",
    }


def _required_string(value: Any, error: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PlanAuthorityContractError(error)
    return value


def _loaded_at(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise PlanAuthorityContractError("authority_context_snapshot_loaded_at_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PlanAuthorityContractError(
            "authority_context_snapshot_loaded_at_invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise PlanAuthorityContractError("authority_context_snapshot_loaded_at_invalid")
    return parsed.astimezone(timezone.utc)
