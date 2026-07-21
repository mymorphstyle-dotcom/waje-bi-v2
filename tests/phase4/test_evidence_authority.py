import math
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from decimal import Decimal
import unittest
import bi_agent.runtime.evidence_authority as evidence_authority_module

from bi_agent.runtime.analysis_contracts import (
    query_contract_signature,
)
from bi_agent.runtime.clickhouse_runtime import ClickHouseQueryResult
from bi_agent.runtime.evidence_authority import (
    EvidenceIntegrityError,
    RuntimeEvidenceAuthority,
    _record_completeness,
    _record_query_execution,
    canonical_digest,
    canonical_rows_hash,
    runtime_evidence_record_integrity_errors,
)
from bi_agent.runtime.query_audit import query_audit_refs, query_rows_ref
from bi_agent.runtime.query_completeness import validate_query_result
from bi_agent.runtime.query_executor import AggregateRowsStore, ClickHouseQueryExecutor
from tests.phase4.test_clickhouse_query_compiler import metric as reviewed_metric
from tests.phase4.test_query_completeness import (
    _PAID_RELEASE_RESOLVER,
    baseline_contract,
    complete_rows,
    paid_snapshot,
    successful_result,
)


class _RowsRuntime:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def aggregate(self, sql, query_id, **kwargs):
        return ClickHouseQueryResult(
            ok=True,
            rows=self.rows,
            query_id=query_id,
        )

    bounded_context = aggregate


class RuntimeEvidenceAuthorityTest(unittest.TestCase):
    def test_query_execution_rejects_rehashed_noncanonical_contract_signature(self):
        contract = baseline_contract(metric=reviewed_metric())
        contract = replace(
            contract,
            contract_signature=query_contract_signature(contract),
        )
        snapshot = paid_snapshot()
        result = successful_result(contract, rows=complete_rows())
        authority = RuntimeEvidenceAuthority()
        record = _record_query_execution(
            authority,
            contract,
            result,
            {snapshot.snapshot_ref: snapshot},
        )
        resigned_contract = replace(contract, contract_signature="f" * 64)
        refs = query_audit_refs(
            record.query_hash,
            resigned_contract.contract_signature,
            record.source_snapshot_refs,
            query_contract_ref=record.query_contract_ref,
            execution_attempt_ref=record.execution_attempt_ref,
            rows_content_hash=record.rows_content_hash,
        )
        result_payload = {
            **dict(record.result_payload),
            "result_ref": refs.result_ref,
            "rows_ref": refs.rows_ref,
            "completeness_report_ref": refs.completeness_report_ref,
        }
        query_contract = asdict(resigned_contract)
        record_payload = {
            "query_contract": query_contract,
            "result": result_payload,
            "rows_content_hash": record.rows_content_hash,
            "source_snapshot_record_refs": list(record.source_snapshot_record_refs),
            "source_snapshot_record_digests": list(
                record.source_snapshot_record_digests
            ),
        }
        digest = canonical_digest(record_payload)
        resigned = replace(
            record,
            record_ref=f"query-execution:{refs.result_ref}:{digest}",
            record_digest=digest,
            record_payload=record_payload,
            contract_signature=resigned_contract.contract_signature,
            query_contract=query_contract,
            contract=resigned_contract,
            result_ref=refs.result_ref,
            rows_ref=refs.rows_ref,
            completeness_report_ref=refs.completeness_report_ref,
            result_payload=result_payload,
        )

        self.assertIn(
            "query_contract_signature_invalid",
            runtime_evidence_record_integrity_errors(resigned),
        )

    def test_binding_pins_immutable_completeness_record(self):
        contract = baseline_contract()
        contract = replace(
            contract,
            contract_signature=query_contract_signature(contract),
        )
        snapshot = paid_snapshot()
        result = successful_result(contract, rows=complete_rows())
        authority = RuntimeEvidenceAuthority()
        query_record = _record_query_execution(
            authority,
            contract,
            result,
            {snapshot.snapshot_ref: snapshot},
        )
        self.assertNotIn("rows", query_record.result_payload)
        base = _record_completeness(
            authority,
            validate_query_result(contract, result, snapshot),
        )
        final = _record_completeness(
            authority,
            replace(
                validate_query_result(contract, result, snapshot),
                coverage_summary={"phase": "final"},
            ),
        )

        self.assertNotEqual(base.record_ref, final.record_ref)
        self.assertEqual(authority.resolve_completeness(base.record_ref), base)
        self.assertEqual(authority.resolve_completeness(final.record_ref), final)
        self.assertEqual(
            authority.resolve_latest_completeness(base.report_ref),
            final,
        )

    def test_public_api_is_read_only(self):
        authority = RuntimeEvidenceAuthority()

        self.assertFalse(hasattr(authority, "record_query_execution"))
        self.assertFalse(hasattr(authority, "record_completeness"))
        self.assertFalse(hasattr(authority, "record_capability_binding"))
        self.assertFalse(hasattr(authority, "_put"))
        self.assertFalse(hasattr(evidence_authority_module, "_WRITE_TOKEN"))
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "completeness_write_type_invalid",
        ):
            authority._runtime_writer().record_completeness(object())

    def test_rows_hash_is_unique_key_ordered(self):
        rows = (
            {"window_id": "target", "channel": "B", "amount": 2.0},
            {"window_id": "target", "channel": "A", "amount": 1.0},
        )

        self.assertEqual(
            canonical_rows_hash(rows, ("window_id", "channel")),
            canonical_rows_hash(tuple(reversed(rows)), ("window_id", "channel")),
        )

    def test_result_rows_hash_accepts_only_current_typed_canonicalization(self):
        rows = (
            {
                "id": "b",
                "day": date(2026, 6, 2),
                "at": datetime(2026, 6, 2, tzinfo=timezone.utc),
                "amount": Decimal("2.00"),
            },
            {
                "id": "a",
                "day": date(2026, 6, 1),
                "at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "amount": Decimal("1.00"),
            },
        )
        unique_key_fields = ("id",)
        ordered_rows = evidence_authority_module.canonical_result_rows(
            rows,
            unique_key_fields,
        )
        legacy_hash = canonical_digest(ordered_rows)
        version = evidence_authority_module.RESULT_ROWS_CANONICALIZATION_VERSION
        versioned_hash = evidence_authority_module.canonical_result_rows_hash(
            rows,
            unique_key_fields,
        )

        self.assertIsInstance(version, str)
        self.assertEqual(version, "result-rows-typed-order-v2")
        self.assertEqual(
            versioned_hash,
            canonical_digest(
                {
                    "canonicalization_version": version,
                    "rows": ordered_rows,
                }
            ),
        )
        self.assertNotEqual(versioned_hash, legacy_hash)
        self.assertEqual(
            versioned_hash,
            evidence_authority_module.canonical_result_rows_hash(
                tuple(reversed(rows)),
                unique_key_fields,
            ),
        )
        self.assertEqual(
            canonical_rows_hash(rows, unique_key_fields),
            legacy_hash,
        )
        self.assertTrue(
            evidence_authority_module.canonical_result_rows_hash_matches(
                rows,
                unique_key_fields,
                versioned_hash,
            )
        )
        self.assertFalse(
            evidence_authority_module.canonical_result_rows_hash_matches(
                rows,
                unique_key_fields,
                legacy_hash,
            )
        )
        self.assertFalse(
            evidence_authority_module.canonical_result_rows_hash_matches(
                rows,
                unique_key_fields,
                "f" * 64,
            )
        )
        self.assertFalse(
            evidence_authority_module.canonical_result_rows_hash_matches(
                rows,
                unique_key_fields,
                [],
            )
        )

    def test_executor_writer_versioned_rows_hash_preserves_preexisting_legacy_ref(self):
        class FixedHashRuntime(_RowsRuntime):
            def aggregate(self, sql, query_id, **kwargs):
                return ClickHouseQueryResult(
                    ok=True,
                    rows=self.rows,
                    query_hash="hash:fixed-result-rows-version",
                    query_id=query_id,
                )

            bounded_context = aggregate

        contract = baseline_contract(metric=reviewed_metric())
        contract = replace(
            contract,
            contract_signature=query_contract_signature(contract),
        )
        snapshot = paid_snapshot()
        rows = complete_rows()
        ordered_rows = evidence_authority_module.canonical_result_rows(
            rows,
            contract.result_shape.unique_key,
        )
        legacy_hash = canonical_digest(ordered_rows)
        legacy_rows_ref = query_rows_ref(
            "hash:fixed-result-rows-version",
            contract.contract_signature,
            contract.dataset_snapshot_refs,
            legacy_hash,
        )
        rows_store = AggregateRowsStore(
            _rows={legacy_rows_ref: ordered_rows},
        )
        authority = RuntimeEvidenceAuthority()

        result = ClickHouseQueryExecutor(
            FixedHashRuntime(rows),
            rows_store=rows_store,
            evidence_authority=authority,
            release_resolver=_PAID_RELEASE_RESOLVER,
        ).execute(
            contract,
            {snapshot.snapshot_ref: snapshot},
            execution_attempt_ref="attempt:test:result-rows-version",
        )
        execution_record = authority.resolve_query_execution(result.result_ref)
        rows_record = authority.resolve_rows(result.rows_ref)
        versioned_hash = evidence_authority_module.canonical_result_rows_hash(
            rows,
            contract.result_shape.unique_key,
        )

        self.assertEqual(result.execution_status, "succeeded")
        self.assertEqual(execution_record.rows_content_hash, versioned_hash)
        self.assertEqual(rows_record.rows_content_hash, versioned_hash)
        self.assertEqual(
            result.rows_ref,
            query_rows_ref(
                result.query_hash,
                contract.contract_signature,
                contract.dataset_snapshot_refs,
                versioned_hash,
            ),
        )
        self.assertNotEqual(result.rows_ref, legacy_rows_ref)
        self.assertEqual(rows_store.get(legacy_rows_ref), ordered_rows)
        self.assertEqual(rows_store.get(result.rows_ref), rows)

    def test_query_execution_rows_are_idempotent_across_executor_result_order(self):
        contract = baseline_contract(metric=reviewed_metric())
        contract = replace(
            contract,
            contract_signature=query_contract_signature(contract),
        )
        snapshot = paid_snapshot()
        original_rows = complete_rows()
        reordered_rows = tuple(reversed(original_rows))
        authority = RuntimeEvidenceAuthority()
        attempt_ref = "attempt:test:canonical-row-order"
        first_result = ClickHouseQueryExecutor(
            _RowsRuntime(original_rows),
            evidence_authority=authority,
            release_resolver=_PAID_RELEASE_RESOLVER,
        ).execute(
            contract,
            {snapshot.snapshot_ref: snapshot},
            execution_attempt_ref=attempt_ref,
        )
        second_result = ClickHouseQueryExecutor(
            _RowsRuntime(reordered_rows),
            evidence_authority=authority,
            release_resolver=_PAID_RELEASE_RESOLVER,
        ).execute(
            contract,
            {snapshot.snapshot_ref: snapshot},
            execution_attempt_ref=attempt_ref,
        )
        first = authority.resolve_query_execution(first_result.result_ref)
        second = authority.resolve_query_execution(second_result.result_ref)
        first_report = validate_query_result(contract, first_result, snapshot)
        second_report = validate_query_result(contract, second_result, snapshot)
        first_completeness = _record_completeness(
            authority,
            first_report,
        )
        second_completeness = _record_completeness(
            authority,
            second_report,
        )

        self.assertNotEqual(first_result.rows, second_result.rows)
        self.assertEqual(
            first_result.observed_windows,
            ("target_day", "previous_day"),
        )
        self.assertEqual(
            second_result.observed_windows,
            first_result.observed_windows,
        )
        self.assertEqual(first, second)
        expected_result_payload = first_result.to_dict()
        expected_result_payload.pop("rows", None)
        self.assertEqual(
            dict(first.result_payload),
            expected_result_payload,
        )
        self.assertEqual(first_report, second_report)
        self.assertEqual(first_completeness, second_completeness)
        self.assertEqual(
            _record_query_execution(
                authority,
                contract,
                first_result,
                {snapshot.snapshot_ref: snapshot},
            ),
            first,
        )
        self.assertEqual(
            _record_completeness(authority, first_report),
            first_completeness,
        )
        first_rows = authority.resolve_rows(first.rows_ref)
        second_rows = authority.resolve_rows(second.rows_ref)
        self.assertEqual(first_rows, second_rows)
        self.assertEqual(first_rows.storage_ref, second_rows.storage_ref)
        self.assertEqual(
            authority.rows_loader.load_rows(first_rows.storage_ref),
            evidence_authority_module.canonical_result_rows(
                original_rows,
                contract.result_shape.unique_key,
            ),
        )

    def test_canonical_row_order_preserves_typed_values_through_authority_roundtrip(
        self,
    ):
        contract = baseline_contract(metric=reviewed_metric())
        contract = replace(
            contract,
            contract_signature=query_contract_signature(contract),
        )
        snapshot = paid_snapshot()
        rows = tuple(
            {
                **row,
                "paid_amount": Decimal(str(row["paid_amount"])),
            }
            for row in complete_rows()
        )
        authority = RuntimeEvidenceAuthority()
        results = tuple(
            ClickHouseQueryExecutor(
                _RowsRuntime(ordered_rows),
                evidence_authority=authority,
                release_resolver=_PAID_RELEASE_RESOLVER,
            ).execute(
                contract,
                {snapshot.snapshot_ref: snapshot},
                execution_attempt_ref="attempt:test:typed-row-roundtrip",
            )
            for ordered_rows in (rows, tuple(reversed(rows)))
        )
        query_records = tuple(
            authority.resolve_query_execution(result.result_ref) for result in results
        )
        rows_record = authority.resolve_rows(query_records[0].rows_ref)
        loaded_rows = authority.rows_loader.load_rows(rows_record.storage_ref)
        roundtrip_report = validate_query_result(
            contract,
            replace(results[0], rows=loaded_rows),
            snapshot,
            release_resolver=_PAID_RELEASE_RESOLVER,
        )

        self.assertEqual(
            tuple(result.execution_status for result in results),
            ("succeeded", "succeeded"),
        )
        self.assertEqual(query_records[0], query_records[1])
        self.assertTrue(
            all(isinstance(row["paid_amount"], Decimal) for row in loaded_rows)
        )
        self.assertEqual(roundtrip_report.completeness_status, "complete")
        typed_rows = evidence_authority_module.canonical_result_rows(
            (
                {
                    "id": "b",
                    "day": date(2026, 6, 2),
                    "at": datetime(2026, 6, 2, tzinfo=timezone.utc),
                    "amount": Decimal("2"),
                },
                {
                    "id": "a",
                    "day": date(2026, 6, 1),
                    "at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                    "amount": Decimal("1"),
                },
            ),
            ("id",),
        )
        reversed_typed_rows = evidence_authority_module.canonical_result_rows(
            tuple(reversed(typed_rows)),
            ("id",),
        )
        self.assertEqual(typed_rows, reversed_typed_rows)
        self.assertTrue(all(isinstance(row["day"], date) for row in typed_rows))
        self.assertTrue(all(isinstance(row["at"], datetime) for row in typed_rows))
        self.assertTrue(all(isinstance(row["amount"], Decimal) for row in typed_rows))

    def test_unexpected_window_failures_are_idempotent_across_executor_result_order(
        self,
    ):
        contract = baseline_contract(metric=reviewed_metric())
        contract = replace(
            contract,
            contract_signature=query_contract_signature(contract),
        )
        snapshot = paid_snapshot()
        rows = (
            {
                "window_id": "unexpected_b",
                "window_role": "baseline",
                "observation_key": "2026-06-01",
                "paid_amount": 2.0,
            },
            {
                "window_id": "target_day",
                "window_role": "target",
                "observation_key": "2026-06-02",
                "paid_amount": 3.0,
            },
            {
                "window_id": "unexpected_a",
                "window_role": "target",
                "observation_key": "2026-06-02",
                "paid_amount": 1.0,
            },
        )
        authority = RuntimeEvidenceAuthority()
        results = tuple(
            ClickHouseQueryExecutor(
                _RowsRuntime(ordered_rows),
                evidence_authority=authority,
                release_resolver=_PAID_RELEASE_RESOLVER,
            ).execute(
                contract,
                {snapshot.snapshot_ref: snapshot},
                execution_attempt_ref="attempt:test:unexpected-window-order",
            )
            for ordered_rows in (rows, tuple(reversed(rows)))
        )
        reports = tuple(
            validate_query_result(contract, result, snapshot) for result in results
        )
        records = tuple(_record_completeness(authority, report) for report in reports)

        self.assertEqual(
            results[0].observed_windows,
            ("target_day", "unexpected_a", "unexpected_b"),
        )
        self.assertEqual(
            results[0].observed_windows,
            results[1].observed_windows,
        )
        self.assertEqual(
            reports[0].assertion_results,
            reports[1].assertion_results,
        )
        self.assertEqual(
            reports[0].failure_reasons,
            reports[1].failure_reasons,
        )
        expected_unexpected_reasons = (
            "unexpected_window:unexpected_a",
            "unexpected_window:unexpected_b",
        )
        self.assertEqual(
            tuple(
                reason
                for reason in reports[0].failure_reasons
                if reason.startswith("unexpected_window:")
            ),
            expected_unexpected_reasons,
        )
        complete_days = next(
            assertion
            for assertion in reports[0].assertion_results
            if assertion["assertion"] == "complete_window_days"
        )
        self.assertEqual(
            tuple(
                reason
                for reason in complete_days["failure_reasons"]
                if reason.startswith("unexpected_window:")
            ),
            expected_unexpected_reasons,
        )
        self.assertEqual(records[0], records[1])
        self.assertEqual(records[0].report_digest, records[1].report_digest)

    def test_canonical_result_rows_support_empty_and_digest_fallback_ordering(self):
        self.assertTrue(hasattr(evidence_authority_module, "canonical_result_rows"))
        cases = (
            ((), ("id",)),
            (
                (
                    {"value": "B", "amount": 2.0},
                    {"value": "A", "amount": 1.0},
                ),
                (),
            ),
            (
                (
                    {"id": "B", "amount": 2.0},
                    {"amount": 1.0},
                ),
                ("id",),
            ),
        )
        for rows, unique_key_fields in cases:
            with self.subTest(unique_key_fields=unique_key_fields):
                ordered = evidence_authority_module.canonical_result_rows(
                    rows,
                    unique_key_fields,
                )
                reversed_ordered = evidence_authority_module.canonical_result_rows(
                    tuple(reversed(rows)),
                    unique_key_fields,
                )
                self.assertEqual(ordered, reversed_ordered)
                self.assertEqual(
                    evidence_authority_module.canonical_result_rows_hash(
                        rows,
                        unique_key_fields,
                    ),
                    canonical_digest(
                        {
                            "canonicalization_version": (
                                evidence_authority_module.RESULT_ROWS_CANONICALIZATION_VERSION
                            ),
                            "rows": ordered,
                        }
                    ),
                )
        missing_key_rows = cases[-1][0]
        self.assertEqual(
            evidence_authority_module.canonical_result_rows(
                missing_key_rows,
                ("id",),
            ),
            evidence_authority_module.canonical_result_rows(
                missing_key_rows,
                (),
            ),
        )
        with self.assertRaisesRegex(
            EvidenceIntegrityError,
            "duplicate_unique_key",
        ):
            evidence_authority_module.canonical_result_rows(
                (
                    {"id": "same", "amount": 1.0},
                    {"id": "same", "amount": 2.0},
                ),
                ("id",),
            )

    def test_missing_and_duplicate_unique_keys_fail_closed_across_executor_order(self):
        contract = baseline_contract(metric=reviewed_metric())
        contract = replace(
            contract,
            contract_signature=query_contract_signature(contract),
        )
        snapshot = paid_snapshot()
        rows = (
            {
                "window_id": "target_day",
                "window_role": "target",
                "paid_amount": 3.0,
            },
            {
                "window_id": "target_day",
                "window_role": "target",
                "observation_key": "2026-06-02",
                "paid_amount": 1.0,
            },
            {
                "window_id": "target_day",
                "window_role": "target",
                "observation_key": "2026-06-02",
                "paid_amount": 2.0,
            },
        )
        executor_errors = []
        for ordered_rows in (rows, tuple(reversed(rows))):
            with self.subTest(ordered_rows=ordered_rows):
                with self.assertRaises(EvidenceIntegrityError) as raised:
                    ClickHouseQueryExecutor(
                        _RowsRuntime(ordered_rows),
                        evidence_authority=RuntimeEvidenceAuthority(),
                        release_resolver=_PAID_RELEASE_RESOLVER,
                    ).execute(
                        contract,
                        {snapshot.snapshot_ref: snapshot},
                        execution_attempt_ref=("attempt:test:mixed-invalid-unique-key"),
                    )
                executor_errors.append(str(raised.exception))

        self.assertEqual(executor_errors[0], executor_errors[1])
        self.assertTrue(executor_errors[0].startswith("duplicate_unique_key:"))

    def test_non_scalar_unique_keys_fail_idempotently_across_executor_order(self):
        contract = baseline_contract(metric=reviewed_metric())
        contract = replace(
            contract,
            contract_signature=query_contract_signature(contract),
        )
        snapshot = paid_snapshot()
        for invalid_key in ({"nested": "date"}, ["nested", "date"]):
            with self.subTest(invalid_key=invalid_key):
                rows = (
                    {
                        "window_id": "target_day",
                        "window_role": "target",
                        "observation_key": invalid_key,
                        "paid_amount": 1.0,
                    },
                    {
                        "window_id": "previous_day",
                        "window_role": "baseline",
                        "observation_key": "2026-06-01",
                        "paid_amount": 2.0,
                    },
                )
                executor_errors = []
                for ordered_rows in (rows, tuple(reversed(rows))):
                    with self.assertRaises(EvidenceIntegrityError) as raised:
                        ClickHouseQueryExecutor(
                            _RowsRuntime(ordered_rows),
                            evidence_authority=RuntimeEvidenceAuthority(),
                            release_resolver=_PAID_RELEASE_RESOLVER,
                        ).execute(
                            contract,
                            {snapshot.snapshot_ref: snapshot},
                            execution_attempt_ref=(
                                "attempt:test:non-scalar-unique-key:"
                                f"{type(invalid_key).__name__}"
                            ),
                        )
                    executor_errors.append(str(raised.exception))

                self.assertEqual(
                    tuple(executor_errors),
                    (
                        "unique_key_not_scalar:observation_key",
                        "unique_key_not_scalar:observation_key",
                    ),
                )

    def test_non_finite_numbers_fail_idempotently_across_executor_order(self):
        contract = baseline_contract(metric=reviewed_metric())
        contract = replace(
            contract,
            contract_signature=query_contract_signature(contract),
        )
        snapshot = paid_snapshot()
        non_finite_values = (
            math.nan,
            math.inf,
            -math.inf,
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
        )
        for index, non_finite in enumerate(non_finite_values):
            for field in ("observation_key", "paid_amount"):
                with self.subTest(index=index, field=field):
                    invalid_row = {
                        "window_id": "target_day",
                        "window_role": "target",
                        "observation_key": "2026-06-02",
                        "paid_amount": 1.0,
                    }
                    invalid_row[field] = non_finite
                    rows = (
                        invalid_row,
                        {
                            "window_id": "previous_day",
                            "window_role": "baseline",
                            "observation_key": "2026-06-01",
                            "paid_amount": 2.0,
                        },
                    )
                    executor_errors = []
                    for ordered_rows in (rows, tuple(reversed(rows))):
                        with self.assertRaises(EvidenceIntegrityError) as raised:
                            ClickHouseQueryExecutor(
                                _RowsRuntime(ordered_rows),
                                evidence_authority=RuntimeEvidenceAuthority(),
                                release_resolver=_PAID_RELEASE_RESOLVER,
                            ).execute(
                                contract,
                                {snapshot.snapshot_ref: snapshot},
                                execution_attempt_ref=(
                                    f"attempt:test:non-finite-number:{index}:{field}"
                                ),
                            )
                        executor_errors.append(str(raised.exception))

                    self.assertEqual(
                        tuple(executor_errors),
                        (
                            "canonical_number_not_finite",
                            "canonical_number_not_finite",
                        ),
                    )

    def test_mixed_canonical_row_errors_fail_idempotently_across_executor_order(self):
        contract = baseline_contract(metric=reviewed_metric())
        contract = replace(
            contract,
            contract_signature=query_contract_signature(contract),
        )
        snapshot = paid_snapshot()
        rows = (
            {
                "window_id": "target_day",
                "window_role": "target",
                "observation_key": "2026-06-02",
                "paid_amount": math.nan,
            },
            {
                "window_id": "previous_day",
                "window_role": "baseline",
                "observation_key": "2026-06-01",
                "paid_amount": {"unsupported"},
            },
        )
        expected_error = (
            "canonical_row_errors:canonical_number_not_finite,"
            "canonical_set_not_supported"
        )
        executor_errors = []
        for ordered_rows in (rows, tuple(reversed(rows))):
            with self.assertRaises(EvidenceIntegrityError) as raised:
                ClickHouseQueryExecutor(
                    _RowsRuntime(ordered_rows),
                    evidence_authority=RuntimeEvidenceAuthority(),
                    release_resolver=_PAID_RELEASE_RESOLVER,
                ).execute(
                    contract,
                    {snapshot.snapshot_ref: snapshot},
                    execution_attempt_ref="attempt:test:mixed-canonical-row-errors",
                )
            executor_errors.append(str(raised.exception))

        self.assertEqual(tuple(executor_errors), (expected_error, expected_error))
        for key_fields in ((), contract.result_shape.unique_key, ("missing",)):
            observed_errors = []
            for ordered_rows in (rows, tuple(reversed(rows))):
                with self.assertRaises(EvidenceIntegrityError) as raised:
                    evidence_authority_module.canonical_result_rows(
                        ordered_rows,
                        key_fields,
                    )
                observed_errors.append(str(raised.exception))
            self.assertEqual(
                tuple(observed_errors),
                (expected_error, expected_error),
            )
        for single_rows, expected_single_error in (
            ((rows[0],), "canonical_number_not_finite"),
            ((rows[1],), "canonical_set_not_supported"),
        ):
            for key_fields in ((), ("missing",)):
                with self.assertRaises(EvidenceIntegrityError) as raised:
                    evidence_authority_module.canonical_result_rows(
                        single_rows,
                        key_fields,
                    )
                self.assertEqual(str(raised.exception), expected_single_error)

    def test_canonical_projection_collision_fails_closed_across_executor_order(self):
        contract = baseline_contract(metric=reviewed_metric())
        contract = replace(
            contract,
            contract_signature=query_contract_signature(contract),
        )
        snapshot = paid_snapshot()
        rows = (
            {
                "window_id": "target_day",
                "window_role": "target",
                "paid_amount": Decimal("1"),
            },
            {
                "window_id": "target_day",
                "window_role": "target",
                "paid_amount": {"$decimal": "1"},
            },
        )
        executor_errors = []
        for ordered_rows in (rows, tuple(reversed(rows))):
            with self.subTest(ordered_rows=ordered_rows):
                with self.assertRaises(EvidenceIntegrityError) as raised:
                    ClickHouseQueryExecutor(
                        _RowsRuntime(ordered_rows),
                        evidence_authority=RuntimeEvidenceAuthority(),
                        release_resolver=_PAID_RELEASE_RESOLVER,
                    ).execute(
                        contract,
                        {snapshot.snapshot_ref: snapshot},
                        execution_attempt_ref=("attempt:test:row-projection-collision"),
                    )
                executor_errors.append(str(raised.exception))

        self.assertEqual(executor_errors[0], executor_errors[1])
        self.assertTrue(
            executor_errors[0].startswith("canonical_row_projection_collision:")
        )

    def test_rows_hash_rejects_nan_and_non_scalar_unique_keys(self):
        cases = (
            ({"window_id": "target", "channel": "A", "amount": math.nan},),
            ({"window_id": "target", "channel": {"nested": "A"}, "amount": 1},),
        )

        for rows in cases:
            with self.subTest(rows=rows):
                with self.assertRaises(EvidenceIntegrityError):
                    canonical_rows_hash(rows, ("window_id", "channel"))

    def test_same_authority_ref_cannot_point_to_different_rows(self):
        contract = baseline_contract()
        contract = replace(
            contract,
            contract_signature=query_contract_signature(contract),
        )
        snapshot = paid_snapshot()
        result = successful_result(contract, rows=complete_rows())
        authority = RuntimeEvidenceAuthority()
        _record_query_execution(
            authority,
            contract,
            result,
            {snapshot.snapshot_ref: snapshot},
        )
        changed = replace(
            result,
            rows=tuple(
                {**row, "paid_amount": float(row["paid_amount"]) + 1.0}
                for row in result.rows
            ),
        )

        with self.assertRaises(EvidenceIntegrityError):
            _record_query_execution(
                authority,
                contract,
                changed,
                {snapshot.snapshot_ref: snapshot},
            )


if __name__ == "__main__":
    unittest.main()
