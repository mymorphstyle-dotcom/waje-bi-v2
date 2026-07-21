import pytest

from bi_agent.runtime.analysis_contracts import CompletenessReport
from bi_agent.runtime.capability_execution import (
    _primary_report_accepted,
    _report_slot_failure,
)
from bi_agent.runtime.degradation_policy import (
    degraded_binding_projection_is_authorized,
    ready_binding_projection_is_authorized,
)


def _plan(*, required_mode: str = "all"):
    return {
        "required_input_slots": (
            {
                "slot_id": "primary",
                "query_contract_refs": ("query:primary",),
                "validation_query_contract_refs": (),
            },
        ),
        "optional_input_slots": (
            {
                "slot_id": "context",
                "query_contract_refs": ("query:context",),
                "validation_query_contract_refs": (),
            },
        ),
        "minimum_readiness": {"required_slots": required_mode},
        "degradation_policy": {
            "missing_optional_input": "omit_optional_component",
            "missing_required_input": "omit_path",
            "incomplete_input": "report_limitation",
        },
    }


def _binding(issue):
    return {
        "status": "degraded",
        "query_contract_refs": ("query:primary",),
        "validation_query_contract_refs": (),
        "reasons": ("diagnostic wording is not policy authority",),
        "issues": (issue,),
    }


def test_degradation_policy_requires_typed_issues_not_reason_text():
    binding = _binding(
        {
            "code": "slot_input_missing",
            "failure_class": "availability",
            "input_state": "missing",
            "slot_id": "context",
            "slot_role": "optional",
            "diagnostic": "any wording",
        }
    )
    assert degraded_binding_projection_is_authorized(_plan(), binding)

    legacy_only = {**binding, "issues": ()}
    legacy_only["reasons"] = ("missing_optional_slot:context",)
    assert not degraded_binding_projection_is_authorized(_plan(), legacy_only)


def test_degradation_policy_never_degrades_integrity_issues():
    binding = _binding(
        {
            "code": "primary_provenance_mismatch",
            "failure_class": "integrity",
            "input_state": "invalid",
            "slot_id": "context",
            "slot_role": "optional",
            "diagnostic": "query and report identities disagree",
        }
    )
    assert not degraded_binding_projection_is_authorized(_plan(), binding)


def test_degradation_policy_validates_issue_scope_without_parsing_diagnostics():
    issue = {
        "code": "empty_primary_result",
        "failure_class": "availability",
        "input_state": "incomplete",
        "slot_id": "context",
        "slot_role": "optional",
        "diagnostic": "first diagnostic",
    }
    assert degraded_binding_projection_is_authorized(_plan(), _binding(issue))
    assert degraded_binding_projection_is_authorized(
        _plan(),
        _binding({**issue, "diagnostic": "completely different diagnostic"}),
    )
    assert not degraded_binding_projection_is_authorized(
        _plan(),
        _binding({**issue, "slot_id": "undeclared"}),
    )


def test_degradation_policy_rejects_code_class_state_forgery():
    issue = {
        "code": "primary_provenance_mismatch",
        "failure_class": "availability",
        "input_state": "incomplete",
        "slot_id": "context",
        "slot_role": "optional",
        "diagnostic": "forged typed issue",
    }
    assert not degraded_binding_projection_is_authorized(
        _plan(),
        _binding(issue),
    )


def test_at_least_one_required_slot_can_degrade_a_typed_missing_peer():
    plan = _plan(required_mode="at_least_one")
    plan["required_input_slots"] = (
        *plan["required_input_slots"],
        {
            "slot_id": "peer",
            "query_contract_refs": ("query:peer",),
            "validation_query_contract_refs": (),
        },
    )
    binding = _binding(
        {
            "code": "slot_input_missing",
            "failure_class": "availability",
            "input_state": "missing",
            "slot_id": "peer",
            "slot_role": "required",
            "diagnostic": "peer input unavailable",
        }
    )
    binding["query_contract_refs"] = (
        *binding["query_contract_refs"],
        "query:context",
    )
    assert degraded_binding_projection_is_authorized(plan, binding)


def test_present_required_slot_can_degrade_typed_incomplete_evidence():
    issue = {
        "code": "accepted_incomplete_input",
        "failure_class": "boundary",
        "input_state": "incomplete",
        "slot_id": "primary",
        "slot_role": "required",
        "diagnostic": "partial evidence accepted by the capability contract",
    }
    binding = _binding(issue)
    binding["query_contract_refs"] = (
        *binding["query_contract_refs"],
        "query:context",
    )
    assert degraded_binding_projection_is_authorized(_plan(), binding)


def test_degraded_binding_must_account_for_every_unbound_slot():
    plan = _plan(required_mode="at_least_one")
    plan["required_input_slots"] = (
        *plan["required_input_slots"],
        {
            "slot_id": "peer",
            "query_contract_refs": ("query:peer",),
            "validation_query_contract_refs": (),
        },
    )
    binding = _binding(
        {
            "code": "slot_input_missing",
            "failure_class": "availability",
            "input_state": "missing",
            "slot_id": "context",
            "slot_role": "optional",
            "diagnostic": "optional context unavailable",
        }
    )
    assert not degraded_binding_projection_is_authorized(plan, binding)


def test_ready_binding_requires_full_slot_closure_and_no_issues():
    plan = _plan()
    ready = {
        "status": "ready",
        "query_contract_refs": ("query:primary", "query:context"),
        "validation_query_contract_refs": (),
        "reasons": (),
        "issues": (),
    }
    assert ready_binding_projection_is_authorized(plan, ready)
    assert not ready_binding_projection_is_authorized(
        plan,
        {**ready, "query_contract_refs": ("query:primary",)},
    )
    assert not ready_binding_projection_is_authorized(
        plan,
        {**ready, "issues": ({"unexpected": "issue"},)},
    )


def test_binding_projection_rejects_undeclared_evidence_refs():
    plan = _plan()
    ready = {
        "status": "ready",
        "query_contract_refs": (
            "query:primary",
            "query:context",
            "query:undeclared",
        ),
        "validation_query_contract_refs": (),
        "reasons": (),
        "issues": (),
    }
    assert not ready_binding_projection_is_authorized(plan, ready)

    degraded = _binding(
        {
            "code": "slot_input_missing",
            "failure_class": "availability",
            "input_state": "missing",
            "slot_id": "context",
            "slot_role": "optional",
            "diagnostic": "optional input unavailable",
        }
    )
    degraded["query_contract_refs"] = (
        *degraded["query_contract_refs"],
        "query:undeclared",
    )
    assert not degraded_binding_projection_is_authorized(plan, degraded)


def test_ready_binding_requires_current_issue_and_reason_fields():
    plan = _plan()
    ready = {
        "status": "ready",
        "query_contract_refs": ("query:primary", "query:context"),
        "validation_query_contract_refs": (),
        "reasons": (),
        "issues": (),
    }
    assert not ready_binding_projection_is_authorized(
        plan,
        {key: value for key, value in ready.items() if key != "issues"},
    )
    assert not ready_binding_projection_is_authorized(
        plan,
        {key: value for key, value in ready.items() if key != "reasons"},
    )


def _failed_report(*failure_classes: str) -> CompletenessReport:
    return CompletenessReport(
        report_ref="report:typed",
        query_contract_ref="query:typed",
        result_ref="result:typed",
        completeness_status="partial",
        analysis_readiness="blocked",
        assertion_results=(
            {
                "assertion": "typed_failure",
                "passed": False,
                "failure_reasons": ("diagnostic wording can change",),
                "failure_classes": failure_classes,
                "details": {},
            },
        ),
        failure_reasons=("diagnostic wording can change",),
        coverage_summary={},
    )


def test_binding_issue_class_comes_from_completeness_type():
    availability = _report_slot_failure(
        _failed_report("availability"),
        code="primary_report_not_ready",
        diagnostic="first wording",
    )
    assert (availability.failure_class, availability.input_state) == (
        "availability",
        "incomplete",
    )

    integrity = _report_slot_failure(
        _failed_report("availability", "result_consistency"),
        code="primary_report_not_ready",
        diagnostic="different wording",
    )
    assert (integrity.failure_class, integrity.input_state) == (
        "integrity",
        "invalid",
    )


def test_failed_completeness_assertion_without_type_is_rejected():
    with pytest.raises(
        ValueError,
        match="^completeness_assertion_failure_classes_missing$",
    ):
        _failed_report()


def test_reconciliation_failure_can_feed_only_an_explicit_boundary_binding():
    report = CompletenessReport(
        report_ref="report:reconciliation-boundary",
        query_contract_ref="query:reconciliation-boundary",
        result_ref="result:reconciliation-boundary",
        completeness_status="partial",
        analysis_readiness="blocked",
        assertion_results=(
            {
                "assertion": "execution_succeeded",
                "passed": True,
                "failure_reasons": (),
                "failure_classes": (),
                "details": {},
            },
            {
                "assertion": "dimension_reconciliation",
                "passed": False,
                "failure_reasons": ("reconciliation failed",),
                "failure_classes": ("reconciliation",),
                "details": {},
            },
        ),
        failure_reasons=("reconciliation failed",),
        coverage_summary={},
    )

    assert _primary_report_accepted(report)
