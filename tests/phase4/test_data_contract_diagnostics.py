import unittest

from bi_agent.runtime.data_contract_diagnostics import diagnose_contract_gaps


class DataContractDiagnosticsTest(unittest.TestCase):
    def test_field_exists_but_contract_missing_is_contract_absent(self):
        diagnostics = diagnose_contract_gaps(
            contract_gaps=("payment_status_contract_missing",),
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
            contract_gaps=("duplicate_order_contract_missing",),
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
            contract_gaps=("high_value_user_contract_missing",),
            available_fields=("user_id", "paid_amount_ngn"),
            contract_fields=("user_id",),
            permission_denied_fields=("user_id",),
            unsupported_grains=(),
        )

        self.assertEqual(diagnostics[0]["status"], "permission_blocked")
        self.assertEqual(diagnostics[0]["claim_effect"], "block_sensitive_detail_claim")

    def test_unsupported_grain_is_distinct_from_no_data(self):
        diagnostics = diagnose_contract_gaps(
            contract_gaps=("gameplay_contract_missing",),
            available_fields=("gameplay_id", "paid_amount_ngn"),
            contract_fields=("gameplay_id",),
            permission_denied_fields=(),
            unsupported_grains=("gameplay_id",),
        )

        self.assertEqual(diagnostics[0]["status"], "unsupported_grain")
        self.assertEqual(diagnostics[0]["data_presence"], "field_present")
        self.assertIn("聚合粒度", diagnostics[0]["repair_path"])


if __name__ == "__main__":
    unittest.main()
