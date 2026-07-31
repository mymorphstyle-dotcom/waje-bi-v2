"""Read one completed model execution from runtime storage for Gate 3 evals."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from waje_vnext.storage.codec import encode_record
from waje_vnext.storage.ports import AuthorityStore


class RuntimeProjectionError(RuntimeError):
    """The runtime store cannot produce a complete successful execution."""


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def project_runtime_model_execution(
    store: AuthorityStore,
    *,
    logical_model_job_id: str,
    run_trace_manifest_id: str,
    execution_manifest_sha256: str,
    execution_cell_id: str,
    execution_attempt_id: str,
    evaluator_profile_ref: str,
    evaluator_profile_sha256: str,
    trace_stage_id: str,
    trace_artifact_ref: str,
    runtime_store_ref: str,
    snapshot_ref: str,
) -> dict[str, Any]:
    """Build a local self-attested projection from one atomic store read."""

    (
        job,
        requests,
        receipts,
        result,
        run_trace_manifest,
    ) = store.read_model_execution_trace_records(
        logical_model_job_id,
        run_trace_manifest_id,
    )
    if not requests:
        raise RuntimeProjectionError("model execution has no attempt requests")
    receipt_by_attempt = {
        receipt.provider_attempt_id: receipt for receipt in receipts
    }
    if len(receipt_by_attempt) != len(receipts):
        raise RuntimeProjectionError(
            "model execution has duplicate receipts for an attempt"
        )
    request_ids = {request.provider_attempt_id for request in requests}
    if len(request_ids) != len(requests):
        raise RuntimeProjectionError(
            "model execution has duplicate attempt requests"
        )
    if request_ids != set(receipt_by_attempt):
        raise RuntimeProjectionError(
            "successful model projection requires the exact receipt set"
        )
    if result is None:
        raise RuntimeProjectionError(
            "successful model projection requires a durable result"
        )
    if (
        job.case_id != run_trace_manifest.case_id
        or job.logical_model_job_id
        not in run_trace_manifest.logical_model_job_ids
    ):
        raise RuntimeProjectionError(
            "logical model job is absent from the persisted run trace"
        )
    receipt_ids = {
        receipt.provider_attempt_receipt_id for receipt in receipts
    }
    request_ids = {
        request.provider_attempt_id for request in requests
    }
    if not request_ids <= set(
        run_trace_manifest.provider_attempt_request_ids
    ):
        raise RuntimeProjectionError(
            "provider request is absent from the persisted run trace"
        )
    if not receipt_ids <= set(
        run_trace_manifest.provider_attempt_receipt_ids
    ):
        raise RuntimeProjectionError(
            "provider receipt is absent from the persisted run trace"
        )
    if result.durable_model_result_id not in (
        run_trace_manifest.durable_model_result_ids
    ):
        raise RuntimeProjectionError(
            "durable result is absent from the persisted run trace"
        )
    encoded_run_trace_manifest = encode_record(run_trace_manifest)
    attempts = [
        {
            "request": encode_record(request),
            "receipt": encode_record(
                receipt_by_attempt[request.provider_attempt_id]
            ),
        }
        for request in requests
    ]
    execution: dict[str, Any] = {
        "artifact_type": "gate3_runtime_model_execution",
        "artifact_version": "gate3.runtime-model-execution.v1",
        "execution_manifest_sha256": execution_manifest_sha256,
        "execution_cell_id": execution_cell_id,
        "execution_attempt_id": execution_attempt_id,
        "run_trace_manifest_id": run_trace_manifest.trace_manifest_id,
        "run_trace_manifest_sha256": _canonical_sha256(
            encoded_run_trace_manifest
        ),
        "evaluator_profile_ref": evaluator_profile_ref,
        "evaluator_profile_sha256": evaluator_profile_sha256,
        "source_proof": {
            "mode": "development_self_attested",
            "runtime_store_ref": runtime_store_ref,
            "snapshot_ref": snapshot_ref,
            "export_attestation_ref": None,
        },
        "logical_model_job": encode_record(job),
        "attempts": attempts,
        "durable_result": encode_record(result),
        "trace_output_binding": {
            "stage_id": trace_stage_id,
            "artifact_ref": trace_artifact_ref,
        },
    }
    execution["runtime_record_set_sha256"] = _canonical_sha256(
        {
            "logical_model_job": execution["logical_model_job"],
            "attempts": execution["attempts"],
            "durable_result": execution["durable_result"],
        }
    )
    return execution
