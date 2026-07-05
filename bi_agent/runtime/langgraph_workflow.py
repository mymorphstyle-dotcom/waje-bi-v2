from dataclasses import dataclass
import warnings
from typing import Any, Optional, TypedDict

from langchain_core._api.deprecation import LangChainPendingDeprecationWarning

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL.*",
)
warnings.filterwarnings(
    "ignore",
    category=LangChainPendingDeprecationWarning,
)
from langgraph.graph import END, StateGraph

from bi_agent.capabilities.data_quality_check import data_quality_check
from bi_agent.capabilities.event_evidence import event_evidence
from bi_agent.capabilities.formula_decompose import formula_decompose
from bi_agent.capabilities.joint_attribution import joint_attribution
from bi_agent.capabilities.outlier_scan import outlier_scan
from bi_agent.capabilities.pattern_scan import scan_pattern
from bi_agent.capabilities.segment_bridge import segment_bridge
from bi_agent.runtime.answer_package import build_answer_package
from bi_agent.runtime.artifacts import persist_artifact, to_jsonable
from bi_agent.runtime.compiler import compile_graph
from bi_agent.runtime.sql_safety import validate_select_only


WORKFLOW_NODES = (
    "intent_binding",
    "compile_graph",
    "inspect_schema",
    "validate_runtime_binding",
    "execute_capabilities",
    "synthesize_draft_answer",
    "answer_verify",
    "persist_artifact",
)
NON_RETRYABLE_FAILURE_TYPES = frozenset(
    {"business", "evidence", "permission", "contract", "sql"}
)


class WorkflowState(TypedDict, total=False):
    request: dict[str, Any]
    run_id: str
    checkpoint_events: list[dict[str, Any]]
    intent: dict[str, Any]
    compiled_graph: Any
    schema: dict[str, Any]
    sql_text: str
    sql_hash: str
    validator_results: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    draft_claims: list[dict[str, Any]]
    verifier: dict[str, Any]
    answer_package: dict[str, Any]
    artifact_path: str


@dataclass(frozen=True)
class WorkflowRunResult:
    status: str
    run_id: str
    answer_package: Optional[dict[str, Any]] = None
    artifact_path: str = ""
    failure_reason: str = ""
    checkpoint_events: tuple[dict[str, Any], ...] = ()


class WorkflowFailure(Exception):
    def __init__(self, message: str, *, failure_type: str = "technical"):
        super().__init__(message)
        self.failure_type = failure_type


def run_pattern_workflow(request: Optional[dict[str, Any]] = None) -> WorkflowRunResult:
    request = dict(request or {})
    state: WorkflowState = {
        "request": request,
        "run_id": request.get("run_id") or "phase4-draft",
        "checkpoint_events": [],
        "validator_results": [],
    }

    try:
        output = _build_workflow().invoke(state)
    except WorkflowFailure as exc:
        return WorkflowRunResult(
            status="failed",
            run_id=state["run_id"],
            failure_reason=str(exc),
            checkpoint_events=tuple(state["checkpoint_events"]),
        )
    except Exception as exc:
        return WorkflowRunResult(
            status="failed",
            run_id=state["run_id"],
            failure_reason=f"langgraph_execution_failed:{exc}",
            checkpoint_events=tuple(state["checkpoint_events"]),
        )

    return WorkflowRunResult(
        status="draft",
        run_id=output["run_id"],
        answer_package=output["answer_package"],
        artifact_path=output["artifact_path"],
        checkpoint_events=tuple(output["checkpoint_events"]),
    )


def _build_workflow():
    graph = StateGraph(WorkflowState)
    for node in WORKFLOW_NODES:
        graph.add_node(node, _retrying_node(node, globals()[f"_{node}"]))
    graph.set_entry_point(WORKFLOW_NODES[0])
    for current, next_node in zip(WORKFLOW_NODES, WORKFLOW_NODES[1:]):
        graph.add_edge(current, next_node)
    graph.add_edge(WORKFLOW_NODES[-1], END)
    return graph.compile()


def _retrying_node(node_name, func):
    def run(state: WorkflowState) -> WorkflowState:
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            event = _checkpoint(state, node_name, attempt)
            try:
                result = func(state)
                event["status"] = "completed"
                return result
            except WorkflowFailure as exc:
                event["status"] = "failed"
                event["failure_type"] = exc.failure_type
                event["reason"] = str(exc)
                if (
                    exc.failure_type not in NON_RETRYABLE_FAILURE_TYPES
                    and attempt < max_attempts
                ):
                    event["status"] = "retrying"
                    continue
                raise
            except Exception as exc:
                event["status"] = "failed"
                event["failure_type"] = "technical"
                event["reason"] = str(exc)
                if attempt < max_attempts:
                    event["status"] = "retrying"
                    continue
                raise WorkflowFailure(str(exc), failure_type="technical")
        return state

    return run


def _intent_binding(state: WorkflowState) -> WorkflowState:
    request = state["request"]
    if request.get("force_langgraph_failure"):
        raise RuntimeError("forced_langgraph_failure")
    _maybe_force_node_failure(state, "intent_binding")
    state["intent"] = {
        "question_family": request.get("question_family", "pattern_explanation"),
        "target_metric": request.get("target_metric", "paid_amount"),
        "pattern_family": request.get("pattern_family", "intra_period"),
        "scope": request.get("scope", "full_sample"),
        "time_window": request.get("time_window", "2024-01..2026-05"),
        "requested_nodes": tuple(request.get("requested_nodes", ("pattern_scan",))),
    }
    return state


def _compile_graph(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "compile_graph")
    intent = state["intent"]
    compiled = compile_graph(
        question_family=intent["question_family"],
        target_metric=intent["target_metric"],
        pattern_family=intent["pattern_family"],
        requested_nodes=intent["requested_nodes"],
    )
    if compiled.status == "rejected":
        raise WorkflowFailure("graph_compile_rejected", failure_type="contract")
    state["compiled_graph"] = compiled
    return state


def _inspect_schema(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "inspect_schema")
    sql_text = state["request"].get(
        "sql_text",
        "SELECT month, phase, sum(amount) AS amount "
        "FROM paid_order_detail GROUP BY month, phase",
    )
    state["sql_text"] = sql_text
    state["schema"] = {
        "table": "paid_order_detail",
        "fields": ("month", "phase", "amount"),
        "grain": "month_phase",
    }
    return state


def _validate_runtime_binding(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "validate_runtime_binding")
    sql_result = validate_select_only(state["sql_text"], aggregate=True)
    state["sql_hash"] = sql_result.query_hash
    validator_results = [
        {
            "validator": "sql_safety",
            "ok": sql_result.ok,
            "reason": sql_result.reason,
            "sql_hash": sql_result.query_hash,
        },
        {
            "validator": "runtime_binding",
            "ok": True,
            "reason": "phase4_draft_fixture",
        },
        {
            "validator": "permission",
            "ok": True,
            "reason": "aggregate_only",
        },
    ]
    state["validator_results"] = validator_results
    if not sql_result.ok:
        raise WorkflowFailure(sql_result.reason, failure_type="sql")
    return state


def _execute_capabilities(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "execute_capabilities")
    rows = state["request"].get("rows") or _default_pattern_rows()
    query_ref = (state["sql_hash"],)
    evidence = []
    compiled = state["compiled_graph"]
    capabilities = tuple(compiled.mutations.accepted_graph)

    if "data_quality_check" in capabilities:
        evidence.append(
            data_quality_check(
                rows,
                required_fields=("month", "phase", "amount"),
                result_refs=query_ref,
            )
        )
    if "pattern_scan" in capabilities:
        evidence.append(
            scan_pattern(
                rows,
                pattern_family=state["intent"]["pattern_family"],
                target_phase="start",
                materiality_floor=0.03,
                result_refs=query_ref,
                evidence_ref="pattern_scan:intra_period",
            )
        )
    if "formula_decompose" in capabilities:
        evidence.append(
            formula_decompose(
                [{"formula_id": "paid_amount", "components": ("paid_amount",)}],
                available_components=("paid_amount",),
                result_refs=query_ref,
            )
        )
    if "event_evidence" in capabilities:
        evidence.append(event_evidence(state["request"].get("events", ()), result_refs=query_ref))
    if "segment_bridge" in capabilities:
        segment = segment_bridge(
            state["request"].get(
                "segments",
                ({"segment": "full_sample", "amount": 1.0, "n": 100},),
            ),
            result_refs=query_ref,
        )
        evidence.append(segment)
    else:
        segment = None
    if "outlier_scan" in capabilities:
        evidence.append(outlier_scan(rows, result_refs=query_ref))
    if "joint_attribution" in capabilities:
        evidence.append(joint_attribution(segment_evidence=segment, result_refs=query_ref))

    state["evidence"] = [_evidence_dict(item, state) for item in evidence]
    return state


def _synthesize_draft_answer(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "synthesize_draft_answer")
    pattern = _evidence_by_ref(state["evidence"])["pattern_scan:intra_period"]
    payload = pattern["typed_payload"]
    state["draft_claims"] = state["request"].get(
        "draft_claims",
        [
            {
                "text": "Month-start paid amount is consistently higher than mid/end in the full sample.",
                "evidence_refs": ["pattern_scan:intra_period"],
                "numbers": {"median_uplift": payload["median_uplift"]},
                "scope": state["intent"]["scope"],
                "time_window": state["intent"]["time_window"],
            }
        ],
    )
    return state


def _answer_verify(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "answer_verify")
    package = _build_answer_package_from_state(state)
    verifier = package["admin_audit"]["verifier"]
    if verifier["errors"]:
        raise WorkflowFailure("answer_verify_failed", failure_type="evidence")
    state["verifier"] = verifier
    return state


def _persist_artifact(state: WorkflowState) -> WorkflowState:
    _maybe_force_node_failure(state, "persist_artifact")
    _current_event(state)["status"] = "completed"
    package = _build_answer_package_from_state(state)
    artifact_path = persist_artifact(
        package,
        artifact_root=state["request"].get("artifact_root", "artifacts/phase-4"),
    )
    state["answer_package"] = package
    state["artifact_path"] = artifact_path
    return state


def _build_answer_package_from_state(state: WorkflowState) -> dict[str, Any]:
    compiled = state["compiled_graph"]
    return build_answer_package(
        run_id=state["run_id"],
        draft_claims=state["draft_claims"],
        evidence=state["evidence"],
        checkpoint_events=state["checkpoint_events"],
        proposed_graph=compiled.mutations.proposed_graph,
        accepted_graph=compiled.mutations.accepted_graph,
        rejected_or_degraded_mutations=compiled.mutations.records,
        validator_results=state["validator_results"],
        sql_text=state["sql_text"],
        sql_hash=state["sql_hash"],
        artifact_audit={"path": "answer_package.json", "draft_only": True},
    )


def _checkpoint(
    state: WorkflowState,
    node_name: str,
    attempt: int,
) -> dict[str, Any]:
    event = {"node": node_name, "attempt": attempt, "status": "running"}
    state["checkpoint_events"].append(event)
    return event


def _current_event(state: WorkflowState) -> dict[str, Any]:
    return state["checkpoint_events"][-1]


def _maybe_force_node_failure(state: WorkflowState, node_name: str) -> None:
    forced = state["request"].get("force_failure")
    if not forced or forced.get("node") != node_name:
        return
    failure_type = forced.get("failure_type", "technical")
    raise WorkflowFailure(f"forced_{failure_type}_failure:{node_name}", failure_type=failure_type)


def _evidence_dict(item: Any, state: WorkflowState) -> dict[str, Any]:
    evidence = to_jsonable(item)
    payload = dict(evidence.get("typed_payload", {}))
    payload["scope"] = state["intent"]["scope"]
    payload["time_window"] = state["intent"]["time_window"]
    evidence["typed_payload"] = payload
    return evidence


def _evidence_by_ref(evidence: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["evidence_ref"]: item for item in evidence}


def _default_pattern_rows() -> list[dict[str, Any]]:
    rows = []
    year = 2024
    month = 1
    while (year, month) <= (2026, 5):
        month_key = f"{year}-{month:02d}"
        rows.extend(
            [
                {"month": month_key, "phase": "start", "amount": 120},
                {"month": month_key, "phase": "mid", "amount": 100},
                {"month": month_key, "phase": "end", "amount": 100},
            ]
        )
        month += 1
        if month == 13:
            year += 1
            month = 1
    return rows
