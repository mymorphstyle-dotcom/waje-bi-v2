import unittest

from bi_agent.runtime.data_contract_diagnostics import (
    contract_fields_from_records,
    diagnose_contract_gaps,
)


class DataContractDiagnosticsTest(unittest.TestCase):
    def test_field_exists_but_contract_missing_is_contract_absent(self):
        diagnostics = diagnose_contract_gaps(
            contract_gaps=(
                {
                    "gap_id": "payment_status_contract_missing",
                    "fields": ("payment_status",),
                },
            ),
            available_fields=("payment_status", "order_id", "paid_amount_ngn"),
            contract_fields=(),
            permission_denied_fields=(),
            unsupported_grains=(),
        )

        self.assertEqual(diagnostics[0]["gap_id"], "payment_status_contract_missing")
        self.assertEqual(diagnostics[0]["status"], "contract_absent")
        self.assertEqual(diagnostics[0]["data_presence"], "field_present")
        self.assertEqual(diagnostics[0]["contract_presence"], "missing")
        self.assertIn("补语义合同", diagnostics[0]["repair_path"])

    def test_field_missing_is_data_absent(self):
        diagnostics = diagnose_contract_gaps(
            contract_gaps=(
                {
                    "gap_id": "duplicate_order_contract_missing",
                    "fields": ("order_id",),
                },
            ),
            available_fields=("paid_amount_ngn",),
            contract_fields=("order_id",),
            permission_denied_fields=(),
            unsupported_grains=(),
        )

        self.assertEqual(diagnostics[0]["status"], "data_absent")
        self.assertEqual(diagnostics[0]["data_presence"], "field_missing")
        self.assertIn("补数据字段", diagnostics[0]["repair_path"])

    def test_permission_denied_wins_over_contract_missing(self):
        diagnostics = diagnose_contract_gaps(
            contract_gaps=(
                {
                    "gap_id": "high_value_user_contract_missing",
                    "fields": ("user_id",),
                },
            ),
            available_fields=("user_id", "paid_amount_ngn"),
            contract_fields=("user_id",),
            permission_denied_fields=("user_id",),
            unsupported_grains=(),
        )

        self.assertEqual(diagnostics[0]["status"], "permission_blocked")
        self.assertEqual(diagnostics[0]["claim_effect"], "block_sensitive_detail_claim")

    def test_unsupported_grain_is_distinct_from_no_data(self):
        diagnostics = diagnose_contract_gaps(
            contract_gaps=(
                {
                    "gap_id": "gameplay_contract_missing",
                    "fields": ("gameplay_id",),
                },
            ),
            available_fields=("gameplay_id", "paid_amount_ngn"),
            contract_fields=("gameplay_id",),
            permission_denied_fields=(),
            unsupported_grains=("gameplay_id",),
        )

        self.assertEqual(diagnostics[0]["status"], "unsupported_grain")
        self.assertEqual(diagnostics[0]["data_presence"], "field_present")
        self.assertIn("聚合粒度", diagnostics[0]["repair_path"])

    def test_unknown_string_gap_stays_unknown_without_explicit_fields(self):
        diagnostics = diagnose_contract_gaps(
            contract_gaps=("totally_new_contract_gap",),
            available_fields=("paid_amount_ngn",),
            contract_fields=("paid_amount_ngn",),
            permission_denied_fields=(),
            unsupported_grains=(),
        )

        self.assertEqual(diagnostics[0]["gap_id"], "totally_new_contract_gap")
        self.assertEqual(diagnostics[0]["status"], "unknown")
        self.assertEqual(diagnostics[0]["data_presence"], "field_unknown")
        self.assertNotEqual(diagnostics[0]["status"], "data_absent")

    def test_explicit_gap_mapping_uses_declared_fields(self):
        diagnostics = diagnose_contract_gaps(
            contract_gaps=(
                {
                    "gap_id": "payment_status_contract_missing",
                    "fields": ("payment_status",),
                },
            ),
            available_fields=("payment_status", "order_id"),
            contract_fields=(),
            permission_denied_fields=(),
            unsupported_grains=(),
        )

        self.assertEqual(diagnostics[0]["status"], "contract_absent")
        self.assertEqual(diagnostics[0]["data_presence"], "field_present")
        self.assertEqual(diagnostics[0]["contract_presence"], "missing")

    def test_contract_fields_from_records_collects_explicit_and_nested_fields(self):
        fields = contract_fields_from_records(
            (
                {"field": "payment_status"},
                {"field_id": "order_id"},
                {"name": "paid_amount_ngn", "fields": ("order_id", "shop_id")},
                {"field": "payment_status"},
                "skip-me",
            )
        )

        self.assertEqual(
            fields,
            ("payment_status", "order_id", "paid_amount_ngn", "shop_id"),
        )


if __name__ == "__main__":
    unittest.main()
