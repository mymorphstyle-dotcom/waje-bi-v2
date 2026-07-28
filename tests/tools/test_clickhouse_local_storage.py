from __future__ import annotations

import pytest

from tools.clickhouse.local_storage import (
    BIND_SOURCE,
    DATA_TARGET,
    OpsError,
    REPRESENTATIVE_QUERY,
    VOLUME,
    compare_tables,
    main,
    mount_state,
    quoted_string,
    rate_delta,
    selected_inspect,
    validate_copy_manifest,
)


def test_mount_state_resolves_exact_data_target() -> None:
    state = mount_state(
        {
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(BIND_SOURCE),
                    "Destination": DATA_TARGET,
                }
            ]
        }
    )

    assert state.type == "bind"
    assert state.source == str(BIND_SOURCE)
    assert state.destination == DATA_TARGET


def test_selected_inspect_exposes_environment_names_without_values() -> None:
    result = selected_inspect(
        {
            "Id": "container-id",
            "Name": "/waje-bi-clickhouse",
            "Image": "image-id",
            "Config": {
                "Image": "image",
                "Env": ["SAFE=value", "PASSWORD=secret"],
            },
            "HostConfig": {},
            "Mounts": [],
        }
    )

    assert result["config"]["environment_names"] == ["PASSWORD", "SAFE"]
    assert "secret" not in str(result)


def test_table_verification_requires_same_table_set_and_row_counts() -> None:
    before = [
        {
            "database": "waje_bi",
            "table": "facts",
            "rows": 41_000_000,
            "active_parts": 4,
            "bytes_on_disk": 1_000,
        }
    ]
    after = [
        {
            "database": "waje_bi",
            "table": "facts",
            "rows": 41_000_000,
            "active_parts": 5,
            "bytes_on_disk": 980,
        }
    ]

    result = compare_tables(before, after)

    assert result["passed"] is True
    assert result["storage_deltas"][0]["active_parts_after"] == 5


def test_table_verification_fails_closed_on_row_mismatch() -> None:
    before = [
        {
            "database": "waje_bi",
            "table": "facts",
            "rows": 10,
            "active_parts": 1,
            "bytes_on_disk": 100,
        }
    ]
    after = [{**before[0], "rows": 9}]

    result = compare_tables(before, after)

    assert result["passed"] is False
    assert result["row_mismatches"] == [
        {
            "database": "waje_bi",
            "table": "facts",
            "before": 10,
            "after": 9,
        }
    ]


def test_rate_delta_uses_actual_elapsed_window() -> None:
    before = {
        "logs": [{"table": "text_log", "rows": 100, "bytes_on_disk": 1_000}]
    }
    after = {
        "logs": [{"table": "text_log", "rows": 130, "bytes_on_disk": 1_300}]
    }

    result = rate_delta(before, after, 10.0)

    assert result["deltas"][0]["rows_per_second"] == 3.0
    assert result["deltas"][0]["bytes_on_disk_per_second"] == 30.0


def test_sql_string_literal_escapes_probe_token() -> None:
    assert quoted_string("a'b\\c") == "'a\\'b\\\\c'"


def test_migration_execution_requires_explicit_downtime_ack() -> None:
    assert main(["migrate", "--execute"]) == 2
    assert main(["rollback", "--execute"]) == 2


def test_named_volume_contract_is_stable() -> None:
    assert VOLUME == "waje-bi-clickhouse-data-v3"


def valid_copy_manifest() -> dict[str, str]:
    return {
        "source_structure": "structure",
        "target_structure": "structure",
        "source_nonlink_modes": "modes",
        "target_nonlink_modes": "modes",
        "source_file_mtimes": "mtimes",
        "target_file_mtimes": "mtimes",
        "source_files": "files",
        "target_files": "files",
        "source_links": "links",
        "target_links": "links",
        "runtime_uid": "101",
        "runtime_gid": "101",
        "target_owner_mismatches": "0",
        "target_unreadable_files": "0",
        "target_unsearchable_directories": "0",
    }


def test_copy_manifest_enforces_portable_integrity_and_runtime_access() -> None:
    result = validate_copy_manifest(valid_copy_manifest())

    assert result["schema_version"] == 2
    assert result["strong_integrity"]["path_type_sha256"] == "structure"
    assert result["target_runtime_access"] == {
        "uid": 101,
        "gid": 101,
        "owner_mismatches": 0,
        "unreadable_files": 0,
        "unsearchable_directories": 0,
    }
    assert (
        result["cross_filesystem_policy"]["source_uid_gid_compared_per_path"]
        is False
    )


def test_copy_manifest_fails_closed_on_every_strong_digest() -> None:
    for field in (
        "target_structure",
        "target_nonlink_modes",
        "target_file_mtimes",
        "target_files",
        "target_links",
    ):
        values = valid_copy_manifest()
        values[field] = "mismatch"

        with pytest.raises(OpsError, match="digest mismatch"):
            validate_copy_manifest(values)


def test_copy_manifest_fails_closed_on_runtime_access_violation() -> None:
    values = valid_copy_manifest()
    values["target_unreadable_files"] = "1"

    with pytest.raises(OpsError, match="unreadable file"):
        validate_copy_manifest(values)


def test_representative_query_is_fixed_and_read_only() -> None:
    normalized = REPRESENTATIVE_QUERY.upper()

    assert normalized.startswith("SELECT")
    assert "PAID_ORDER_SUCCESS_CLEAN_20240101_20260704_V2" in normalized
    for forbidden in ("INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE", "OPTIMIZE"):
        assert forbidden not in normalized
