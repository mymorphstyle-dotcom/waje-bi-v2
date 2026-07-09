# Task 1 Architecture Completion Report

## Scope

Task 1 adds runtime diagnostics that distinguish data availability from contract coverage, then carries those diagnostics into the Answer Package admin audit.

Implemented files:

- `bi_agent/runtime/data_contract_diagnostics.py`
- `bi_agent/runtime/answer_package.py`
- `bi_agent/runtime/langgraph_workflow.py`
- `tests/phase4/test_data_contract_diagnostics.py`
- `tests/phase4/test_workflow_artifacts_answer.py`

## TDD Record

### RED

Command:

```bash
python3 -m unittest tests.phase4.test_data_contract_diagnostics
```

Observed failure:

```text
ModuleNotFoundError: No module named 'bi_agent.runtime.data_contract_diagnostics'
```

Command:

```bash
python3 -m unittest tests.phase4.test_workflow_artifacts_answer.WorkflowArtifactsAnswerTest.test_answer_package_keeps_contract_gap_diagnostics_in_admin_audit
```

Observed failure:

```text
TypeError: build_answer_package() got an unexpected keyword argument 'contract_gap_diagnostics'
```

### GREEN

Commands:

```bash
python3 -m unittest tests.phase4.test_data_contract_diagnostics
python3 -m unittest tests.phase4.test_workflow_artifacts_answer.WorkflowArtifactsAnswerTest.test_answer_package_keeps_contract_gap_diagnostics_in_admin_audit
python3 -m unittest tests.phase4.test_data_contract_diagnostics tests.phase4.test_workflow_artifacts_answer
```

Observed result:

```text
Ran 24 tests in 0.792s

OK
```

## What Changed

### 1. Added contract-gap classifier

`bi_agent/runtime/data_contract_diagnostics.py` introduces:

- `diagnose_contract_gaps(...)`
- status classification for:
  - `data_absent`
  - `contract_absent`
  - `contract_partial`
  - `permission_blocked`
  - `unsupported_grain`
- normalized diagnostic payload fields:
  - `gap_id`
  - `status`
  - `data_presence`
  - `contract_presence`
  - `owner`
  - `repair_path`
  - `claim_effect`

The helper keeps decisions deterministic and scoped to hard boundaries: field presence, explicit contract coverage, permission-denied fields, and unsupported grains.

### 2. Wired diagnostics into Answer Package admin audit

`bi_agent/runtime/answer_package.py` now accepts:

```python
contract_gap_diagnostics: Optional[Sequence[Mapping[str, Any]]] = None
```

It normalizes the value and persists it under:

```python
answer_package["admin_audit"]["contract_gap_diagnostics"]
```

### 3. Wired workflow state to produce diagnostics from compiler runtime plan

`bi_agent/runtime/langgraph_workflow.py` now:

- derives `contract_gaps` from `compiler_runtime_plan["row_shapes"][*]["contract_gaps"]`
- derives `available_fields` from schema fields and loaded rows
- accepts contract coverage from:
  - `request["contract_fields"]`
  - or `request["contract_registry_records"]`
- accepts hard-boundary inputs from:
  - `request["permission_denied_fields"]`
  - `request["unsupported_grains"]`
- materializes the result once and passes it into `build_answer_package(...)`

This keeps diagnostics attached to runtime/compiler context and avoids adding a second audit-only pathway.

## Test Coverage Added

### New helper tests

`tests/phase4/test_data_contract_diagnostics.py` covers:

- field present + contract missing -> `contract_absent`
- field missing -> `data_absent`
- permission denial outranks missing contract -> `permission_blocked`
- unsupported grain stays distinct from no data -> `unsupported_grain`

### Existing Answer Package test expanded

`tests/phase4/test_workflow_artifacts_answer.py` now verifies:

- `contract_gap_diagnostics` is retained in `admin_audit`

## Constraint Check

- No new dependency added
- No local business-answer template added
- No keyword routing used for business judgment
- Hard-boundary classification stays local and deterministic
- LLM retry behavior unchanged
- `artifacts/` behavior unchanged

## Commit

Created after GREEN verification.

## Reviewer Follow-up TDD Record

### RED

Command:

```bash
python3 -m unittest tests.phase4.test_data_contract_diagnostics tests.phase4.test_workflow_artifacts_answer.WorkflowArtifactsAnswerTest.test_contract_gap_diagnostics_use_request_available_fields
```

Observed failure:

```text
FAIL: test_unknown_string_gap_stays_unknown_without_explicit_fields
AssertionError: 'data_absent' != 'unknown'

FAIL: test_explicit_gap_mapping_uses_declared_fields
AssertionError: 'data_absent' != 'contract_absent'

FAIL: test_contract_gap_diagnostics_use_request_available_fields
AssertionError: 'data_absent' != 'contract_absent'
```

### GREEN

Commands:

```bash
python3 -m unittest tests.phase4.test_data_contract_diagnostics tests.phase4.test_workflow_artifacts_answer.WorkflowArtifactsAnswerTest.test_available_fields_for_contract_diagnostics_ignores_projected_rows tests.phase4.test_workflow_artifacts_answer.WorkflowArtifactsAnswerTest.test_contract_gap_diagnostics_use_request_available_fields
python3 -m unittest tests.phase4.test_data_contract_diagnostics tests.phase4.test_workflow_artifacts_answer
```

Observed result:

```text
Ran 29 tests in 0.659s

OK
```
