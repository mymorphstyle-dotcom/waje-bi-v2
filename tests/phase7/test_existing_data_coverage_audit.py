from dataclasses import replace
from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from bi_agent.runtime.current_data_coverage import current_data_coverage_cases
from bi_agent.runtime.dataset_catalog import build_dataset_release_authority_record, dataset_snapshot_release_ref
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
)


class Releases:
    def __init__(self, records):
        self.records = {record.release_ref: record for record in records}

    def resolve_dataset_release(self, release_ref):
        return self.records[release_ref]


def authority_inputs(registry):
    snapshots = {}
    for case in current_data_coverage_cases(registry):
        for snapshot in (case.snapshots or {}).values():
            snapshots.setdefault(snapshot.dataset_id, snapshot)
    selected = []
    records = []
    for dataset_id in ("paid_order_success", "market_dashboard", "market_dashboard_channel", "gameplay", "gameplay_channel", "external_event"):
        snapshot = snapshots[dataset_id]
        membership = tuple(registry.dataset(dataset_id)["release_membership"]["dataset_ids"])
        members = [snapshots[item] for item in membership]
        record = build_dataset_release_authority_record(
            tuple({**item.to_dict(), "requires_release": True} for item in members)
        )
        records.append(record)
        selected.append(replace(snapshot, authority_record_ref=record.authority_record_ref))
    return tuple(selected), Releases(records)


def releases_for(registry, snapshots):
    by_dataset = {item.dataset_id: item for item in snapshots}
    records = []
    normalized = {}
    seen = set()
    for snapshot in snapshots:
        membership = tuple(registry.dataset(snapshot.dataset_id)["release_membership"]["dataset_ids"])
        key = tuple(membership)
        if key in seen:
            continue
        seen.add(key)
        members = [by_dataset[item] for item in membership]
        release_ref = dataset_snapshot_release_ref(
            members[0].logical_snapshot_id, members[0].load_revision,
            tuple(item.snapshot_ref for item in members),
        )
        members = [replace(item, release_ref=release_ref, authority_record_ref="") for item in members]
        record = build_dataset_release_authority_record(tuple(
            {**item.to_dict(), "requires_release": True} for item in members
        ))
        records.append(record)
        normalized.update({item.dataset_id: replace(item, authority_record_ref=record.authority_record_ref) for item in members})
    return tuple(normalized[item.dataset_id] for item in snapshots), Releases(records)


def test_coverage_audit_reports_current_and_excluded_cells():
    from bi_agent.runtime.coverage_audit import audit_existing_data_coverage

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    snapshots, releases = authority_inputs(registry)
    audit = audit_existing_data_coverage(
        registry,
        snapshot_records=snapshots,
        release_resolver=releases,
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        permission_scope="analyst",
    )
    assert audit["states"] == [
        "executable", "degraded", "source_unbound", "contract_partial",
        "permission_blocked", "snapshot_unavailable_as_of",
    ]
    assert audit["cells"]["market_health_compare:market_dashboard"]["state"] == "executable"
    assert audit["cells"]["source_reconciliation:market_dashboard"]["state"] == "contract_partial"
    assert audit["cells"]["event_evidence:external_event"]["state"] == "executable"
    assert audit["cells"]["driver_decomposition:payment_attempt"]["state"] == "source_unbound"
    excluded = audit["cells"]["event_evidence:internal_operation_event"]
    assert excluded["owner"] == "data_operations_owner"
    required = {"question_families", "capability", "datasets", "metrics", "dimensions", "windows", "evidence_types", "claim_ceiling", "current_release_refs", "state", "owner", "impact", "next_action"}
    assert required <= set(excluded)
    assert list(audit["cells"]) == sorted(audit["cells"])
    assert audit["cells"]["market_health_compare:market_dashboard"]["current_releases"][0]["load_revision"]


def test_coverage_audit_distinguishes_permission_future_and_partial_contract():
    from bi_agent.runtime.coverage_audit import audit_existing_data_coverage

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    snapshots, releases = authority_inputs(registry)
    changed = []
    for snapshot in snapshots:
        if snapshot.dataset_id == "market_dashboard":
            snapshot = replace(snapshot, permission_scopes=("admin",))
        elif snapshot.dataset_id == "external_event":
            snapshot = replace(snapshot, loaded_at="2026-06-04T00:00:00+00:00")
        changed.append(snapshot)
    changed, changed_releases = releases_for(registry, changed)
    audit = audit_existing_data_coverage(
        registry, changed, changed_releases,
        datetime.fromisoformat("2026-06-03T12:00:00+01:00"), "analyst",
    )
    assert audit["cells"]["market_health_compare:market_dashboard"]["state"] == "permission_blocked"
    assert audit["cells"]["event_evidence:external_event"]["state"] == "snapshot_unavailable_as_of"
    future = audit["cells"]["event_evidence:external_event"]
    assert "advance the audit as_of" in future["next_action"]
    assert "publish" not in future["next_action"]


def test_coverage_audit_fails_closed_on_release_integrity():
    from bi_agent.runtime.coverage_audit import audit_existing_data_coverage

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    snapshots, releases = authority_inputs(registry)
    releases.records[next(iter(releases.records))] = replace(
        next(iter(releases.records.values())), integrity_errors=("digest",)
    )
    with pytest.raises(ValueError, match="coverage_release_integrity"):
        audit_existing_data_coverage(registry, snapshots, releases, datetime.fromisoformat("2026-06-03T12:00:00+01:00"), "analyst")


class EmptyStore:
    def runtime_evidence_resolver(self):
        return object()

    def list_dataset_snapshots(self):
        return ()


def test_cli_writes_structurally_valid_source_unbound_artifact(tmp_path):
    from tools.phase7.audit_existing_data_coverage import run_audit

    output = tmp_path / "coverage.json"
    result = run_audit(SimpleNamespace(
        as_of="2026-06-03T12:00:00+01:00",
        permission_scope="analyst",
        out=str(output),
    ), store=EmptyStore())
    artifact = json.loads(output.read_text())
    assert result == {"ok": True, "artifact": str(output), "summary": artifact["summary"]}
    assert artifact["cells"]["driver_decomposition:payment_attempt"]["state"] == "source_unbound"


def test_cli_maps_credential_bearing_resolver_failure_without_disclosure(tmp_path, capsys):
    from tools.phase7.audit_existing_data_coverage import main

    secret = "postgresql://alice:password@secret-db.internal/waje"

    class BrokenStore:
        def runtime_evidence_resolver(self):
            return object()

        def list_dataset_snapshots(self):
            raise RuntimeError(f"connection failed {secret} SELECT * FROM private")

    code = main(
        ["--as-of", "2026-06-03T12:00:00+01:00", "--permission-scope", "analyst", "--out", str(tmp_path / "coverage.json")],
        store_factory=lambda: BrokenStore(),
    )
    captured = capsys.readouterr()
    assert code == 1
    payload = json.loads(captured.err)
    assert payload == {
        "error_code": "coverage_database_unavailable",
        "impact": "current coverage authority could not be read",
        "ok": False,
        "owner": "runtime_operations_owner",
    }
    assert secret not in captured.err
    assert "secret-db" not in captured.err
    assert "SELECT" not in captured.err


def test_cli_maps_hard_release_integrity_failure_nonzero(tmp_path, capsys):
    from tools.phase7.audit_existing_data_coverage import main

    registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
    snapshots, _ = authority_inputs(registry)

    class BrokenAuthorityStore(EmptyStore):
        def list_dataset_snapshots(self):
            return snapshots

        def resolve_dataset_release(self, release_ref):
            raise RuntimeError("postgresql://user:password@host/db")

    code = main(
        ["--as-of", "2026-06-03T12:00:00+01:00", "--permission-scope", "analyst", "--out", str(tmp_path / "coverage.json")],
        store_factory=lambda: BrokenAuthorityStore(),
    )
    payload = json.loads(capsys.readouterr().err)
    assert code == 1
    assert payload["error_code"] == "coverage_release_authority_invalid"
    assert "password" not in json.dumps(payload)


def test_cli_maps_contract_integrity_failure_nonzero(tmp_path, capsys, monkeypatch):
    from tools.phase7 import audit_existing_data_coverage as cli

    def fail_contract(*args, **kwargs):
        raise ValueError("contract query SELECT secret_password")

    monkeypatch.setattr(cli.RuntimeContractRegistry, "from_path", fail_contract)
    code = cli.main(
        ["--as-of", "2026-06-03T12:00:00+01:00", "--permission-scope", "analyst", "--out", str(tmp_path / "coverage.json")],
        store_factory=lambda: EmptyStore(),
    )
    payload = json.loads(capsys.readouterr().err)
    assert code == 1
    assert payload["error_code"] == "coverage_runtime_contract_invalid"
    assert "SELECT" not in json.dumps(payload)
    assert "password" not in json.dumps(payload)


def test_cli_maps_artifact_path_failure_without_echoing_path(tmp_path, capsys):
    from tools.phase7.audit_existing_data_coverage import main

    secret_path = tmp_path / "secret-password-output"
    secret_path.mkdir()
    code = main(
        ["--as-of", "2026-06-03T12:00:00+01:00", "--permission-scope", "analyst", "--out", str(secret_path)],
        store_factory=lambda: EmptyStore(),
    )
    payload = json.loads(capsys.readouterr().err)
    assert code == 1
    assert payload["error_code"] == "coverage_artifact_write_failed"
    assert str(secret_path) not in json.dumps(payload)


def test_cli_maps_credential_bearing_close_failure_nonzero(tmp_path, capsys):
    from tools.phase7.audit_existing_data_coverage import main

    secret = "postgresql://closer:password@close-host.internal/waje"

    class Connection:
        def close(self):
            raise RuntimeError(f"close failed {secret} SELECT pg_terminate_backend")

    store = EmptyStore()
    store.connection = Connection()
    code = main(
        ["--as-of", "2026-06-03T12:00:00+01:00", "--permission-scope", "analyst", "--out", str(tmp_path / "coverage.json")],
        store_factory=lambda: store,
    )
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.err)["error_code"] == "coverage_database_close_failed"
    assert captured.out == ""
    assert secret not in captured.err
    assert "close-host" not in captured.err
    assert "SELECT" not in captured.err


def test_cli_sanitizes_unknown_arguments_with_credentials(capsys):
    from tools.phase7.audit_existing_data_coverage import main

    secret = "postgresql://alice:password@host.internal/waje"
    code = main(["--bogus", secret], store_factory=lambda: EmptyStore())
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.err) == {
        "error_code": "coverage_cli_arguments_invalid",
        "impact": "the coverage audit command arguments are invalid",
        "ok": False,
        "owner": "audit_operator",
    }
    assert captured.out == ""
    assert secret not in captured.err
    assert "host.internal" not in captured.err
    assert "usage:" not in captured.err


def test_cli_preserves_primary_error_when_close_also_fails(tmp_path, capsys):
    from tools.phase7.audit_existing_data_coverage import main

    class Connection:
        def close(self):
            raise RuntimeError("postgresql://close:password@close-host/db")

    class BrokenStore(EmptyStore):
        connection = Connection()

        def list_dataset_snapshots(self):
            raise RuntimeError("postgresql://read:password@read-host/db SELECT secret")

    code = main(
        ["--as-of", "2026-06-03T12:00:00+01:00", "--permission-scope", "analyst", "--out", str(tmp_path / "coverage.json")],
        store_factory=lambda: BrokenStore(),
    )
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.err)["error_code"] == "coverage_database_unavailable"
    assert "password" not in captured.err
    assert "host" not in captured.err
