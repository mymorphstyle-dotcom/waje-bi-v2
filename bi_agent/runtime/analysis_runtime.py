from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from bi_agent.conversation.models import RESULT_REUSE_CANDIDATE_FIELDS
from bi_agent.runtime.analysis_contract_compiler import (
    AnalysisCompileOutcome,
    compile_analysis_contract,
)
from bi_agent.runtime.analysis_contracts import (
    AnalysisContract,
    CapabilityExecutionPlan,
    CompletenessReport,
    QueryContract,
    QueryResultEnvelope,
    analysis_contract_signature,
    query_contract_signature,
)
from bi_agent.runtime.authoritative_query_chain import (
    AuthoritativeQueryChainError,
    validate_authoritative_query_chain,
)
from bi_agent.runtime.capability_execution import (
    BoundCapabilityInput,
    bind_capability_inputs,
    capability_plan_has_executable_query_contracts,
)
from bi_agent.runtime.claim_provenance import (
    build_context_manifest_record,
    build_trusted_claim_provenance_record,
    build_verified_claim_record,
)
from bi_agent.runtime.clickhouse_query_compiler import validate_clickhouse_query_contract
from bi_agent.runtime.clickhouse_runtime import ClickHouseRuntime
from bi_agent.runtime.dataset_catalog import DatasetCatalog, DatasetSnapshot
from bi_agent.runtime.evidence_authority import (
    CapabilityBindingRecord,
    CompletenessRecord,
    EvidenceIntegrityError,
    QueryExecutionRecord,
    RowsRecord,
    RuntimeEvidenceAuthority,
    SnapshotRecord,
    canonical_value,
    canonical_digest,
    runtime_evidence_record_integrity_errors,
    snapshot_authority_record,
)
from bi_agent.runtime.query_completeness import validate_query_result, validate_query_set
from bi_agent.runtime.query_executor import ClickHouseQueryExecutor
from bi_agent.runtime.query_repair import QueryRepairDecision, plan_query_repair
from bi_agent.runtime.permission_roles import runtime_permission_scope_from_request
from bi_agent.runtime.runtime_contract_registry import (
    CANONICAL_RUNTIME_BINDINGS_PATH,
    RuntimeContractRegistry,
    runtime_registry_integrity_error,
)


_REUSE_CANDIDATE_V1_FIELDS = frozenset(RESULT_REUSE_CANDIDATE_FIELDS)

_REUSE_CANDIDATE_SEQUENCE_FIELDS = (
    "source_snapshot_refs",
    "source_snapshot_record_refs",
    "source_snapshot_record_digests",
    "source_release_refs",
    "source_release_authority_refs",
    "source_schema_fingerprints",
    "completeness_record_refs",
    "completeness_record_digests",
    "binding_record_refs",
    "binding_record_digests",
)


@dataclass(frozen=True)
class _ValidatedReuseCandidate:
    payload: Mapping[str, Any]
    source_result: QueryResultEnvelope
    source_rows: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class AnalysisRuntimeRequest:
    run_id: str
    proposal: Mapping[str, Any]
    accepted_graph: tuple[str, ...]
    as_of: datetime
    permission_scope: str
    topic_id: str = ""
    reuse_candidates: tuple[Mapping[str, Any], ...] = ()
    attempted_signatures: tuple[str, ...] = ()
    run_mode: str = "production"

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        proposal: Mapping[str, Any],
        accepted_graph: Sequence[str],
        as_of: str | datetime,
        permission_scope: str,
        topic_id: str = "",
        reuse_candidates: Sequence[Mapping[str, Any]] = (),
        attempted_signatures: Sequence[str] = (),
        run_mode: str = "production",
    ) -> "AnalysisRuntimeRequest":
        if not run_id or not isinstance(proposal, Mapping):
            raise ValueError("analysis_runtime_request_invalid")
        parsed = datetime.fromisoformat(as_of) if isinstance(as_of, str) else as_of
        if not isinstance(parsed, datetime) or parsed.tzinfo is None:
            raise ValueError("analysis_runtime_as_of_invalid")
        if permission_scope not in {"viewer", "analyst", "admin"}:
            raise PermissionError("analysis_runtime_permission_scope_invalid")
        if run_mode not in {"production", "live", "fixture"}:
            raise ValueError("analysis_runtime_run_mode_invalid")
        return cls(
            run_id=run_id,
            proposal=MappingProxyType(dict(proposal)),
            accepted_graph=tuple(dict.fromkeys(str(item) for item in accepted_graph if item)),
            as_of=parsed,
            permission_scope=permission_scope,
            topic_id=str(topic_id or ""),
            reuse_candidates=tuple(
                MappingProxyType(dict(item))
                if isinstance(item, Mapping)
                else MappingProxyType({})
                for item in reuse_candidates
            ),
            attempted_signatures=tuple(
                dict.fromkeys(str(item) for item in attempted_signatures if item)
            ),
            run_mode=run_mode,
        )


@dataclass(frozen=True)
class AnswerPackageBuildContext:
    context_owner: Mapping[str, Any]
    trusted_provenance: Mapping[str, Any]

    @classmethod
    def create(
        cls,
        *,
        request: Mapping[str, Any],
        artifact_path: str,
    ) -> "AnswerPackageBuildContext":
        original_manifest = request.get("context_manifest") or {}
        memory_refs = tuple(
            str(item.get("source_ref"))
            for item in original_manifest.get("items") or ()
            if isinstance(item, Mapping)
            and str(item.get("source_type") or "") == "memory"
            and item.get("source_ref")
        ) or (
            f"memory:context:{original_manifest.get('manifest_id') or request.get('run_id')}",
        )
        raw_reuse = tuple(request.get("reuse_decisions") or ())
        reuse = tuple(
            {
                "source_ref": str(item.get("source_ref") or item.get("ref") or ""),
                "result_ref": str(item.get("result_ref") or ""),
                "decision": str(item.get("decision") or ""),
            }
            for item in raw_reuse
            if isinstance(item, Mapping)
            and (item.get("source_ref") or item.get("ref"))
            and item.get("decision")
        ) or (
            {
                "source_ref": str(original_manifest.get("manifest_id") or request.get("run_id") or ""),
                "decision": "fresh",
            },
        )
        return cls(
            context_owner=MappingProxyType(
                {
                    "thread_id": str(request.get("thread_id") or ""),
                    "topic_id": str(request.get("topic_id") or ""),
                    "permission_context": dict(request.get("permission_context") or {}),
                }
            ),
            trusted_provenance=MappingProxyType(
                build_trusted_claim_provenance_record(
                    run_id=str(request.get("run_id") or ""),
                    artifact_refs=(artifact_path,),
                    memory_refs=memory_refs,
                    reuse_decisions=reuse,
                )
            ),
        )


@dataclass(frozen=True)
class AnalysisRuntimeResult:
    analysis_contract: AnalysisContract
    query_contracts: tuple[QueryContract, ...]
    query_results: tuple[QueryResultEnvelope, ...]
    completeness_reports: tuple[CompletenessReport, ...]
    capability_plans: tuple[CapabilityExecutionPlan, ...]
    bound_capability_inputs: Mapping[str, BoundCapabilityInput]
    repair_decisions: tuple[QueryRepairDecision, ...]
    typed_gaps: tuple[Mapping[str, Any], ...]
    persistence_records: Mapping[str, Any]
    reuse_decisions: tuple[Mapping[str, Any], ...] = ()

    @property
    def status(self) -> str:
        if any(item.action == "clarify" for item in self.repair_decisions):
            return "clarify"
        if any(bool(item.get("requires_clarification")) for item in self.typed_gaps):
            return "clarify"
        if any(item.action == "recompile" for item in self.repair_decisions):
            return "recompile"
        if any(item.status == "ready" for item in self.bound_capability_inputs.values()):
            return "ready"
        if any(
            item.status == "degraded" for item in self.bound_capability_inputs.values()
        ) or any(
            item.analysis_readiness == "degraded"
            for item in self.completeness_reports
        ):
            return "degraded"
        return "blocked"

    def to_workflow_payload(self) -> dict[str, Any]:
        rows_by_intent: dict[str, list[dict[str, Any]]] = {}
        refs_by_intent: dict[str, list[str]] = {}
        for contract, result in zip(self.query_contracts, self.query_results):
            rows_by_intent.setdefault(contract.query_intent, []).extend(
                dict(row) for row in result.rows
            )
            refs_by_intent.setdefault(contract.query_intent, []).append(result.result_ref)
        return {
            "analysis_contract": self.analysis_contract.to_dict(),
            "query_contracts": [item.to_dict() for item in self.query_contracts],
            "query_results": [item.to_dict() for item in self.query_results],
            "completeness_reports": [item.to_dict() for item in self.completeness_reports],
            "capability_execution_plans": [asdict(item) for item in self.capability_plans],
            "bound_capability_inputs": dict(self.bound_capability_inputs),
            "runtime_rows_by_intent": rows_by_intent,
            "result_refs_by_intent": refs_by_intent,
            "repair_decisions": [asdict(item) for item in self.repair_decisions],
            "typed_gaps": [dict(item) for item in self.typed_gaps],
            "reuse_decisions": [dict(item) for item in self.reuse_decisions],
            "analysis_runtime_status": self.status,
        }


def analysis_outcome_requires_route_clarification(
    outcome: AnalysisCompileOutcome,
) -> bool:
    clarification_capabilities = {
        str(capability)
        for gap in outcome.analysis_contract.contract_gaps
        if gap.requires_clarification
        for capability in gap.affected_capabilities
        if capability and capability != "analysis_contract"
    }
    if not clarification_capabilities:
        return False
    materially_unbound_capabilities = set()
    for plan in outcome.capability_plans:
        for slot in plan.required_input_slots:
            required = (
                bool(slot.get("required", True))
                if isinstance(slot, Mapping)
                else bool(slot.required)
            )
            query_refs = (
                tuple(slot.get("query_contract_refs") or ())
                if isinstance(slot, Mapping)
                else slot.query_contract_refs
            )
            validation_refs = (
                tuple(slot.get("validation_query_contract_refs") or ())
                if isinstance(slot, Mapping)
                else slot.validation_query_contract_refs
            )
            if required and not query_refs and not validation_refs:
                if plan.capability_id in clarification_capabilities:
                    materially_unbound_capabilities.add(plan.capability_id)
    return bool(materially_unbound_capabilities)


def analysis_outcome_has_executable_ready_capability(
    outcome: AnalysisCompileOutcome,
) -> bool:
    available_refs = {
        str(
            contract.get("query_contract_id")
            if isinstance(contract, Mapping)
            else contract.query_contract_id
        )
        for contract in outcome.query_contracts
    }
    return any(
        capability_plan_has_executable_query_contracts(plan, available_refs)
        for plan in outcome.capability_plans
    )


def analysis_outcome_requires_preexecution_clarification(
    outcome: AnalysisCompileOutcome,
) -> bool:
    return (
        analysis_outcome_requires_route_clarification(outcome)
        and not analysis_outcome_has_executable_ready_capability(outcome)
    )


class AnalysisRuntime:
    """Deterministic analysis-contract orchestration; it never calls an LLM."""

    def __init__(
        self,
        *,
        catalog: DatasetCatalog,
        registry: RuntimeContractRegistry,
        executor: ClickHouseQueryExecutor,
        release_resolver: Any,
        evidence_authority: RuntimeEvidenceAuthority,
        store: Any = None,
        catalog_provider: Callable[[], DatasetCatalog] | None = None,
    ) -> None:
        registry_error = runtime_registry_integrity_error(registry)
        if registry_error:
            raise ValueError(registry_error)
        self.catalog = catalog
        self.registry = registry
        self.executor = executor
        self.release_resolver = release_resolver
        self.evidence_authority = evidence_authority
        self.evidence_resolver = evidence_authority
        self.rows_loader = evidence_authority.rows_loader
        self.evidence_writer = evidence_authority._runtime_writer()
        self.store = store
        self._catalog_provider = catalog_provider

    @classmethod
    def from_environment(cls, store: Any) -> "AnalysisRuntime":
        from bi_agent.runtime.clickhouse_revenue_rows import trusted_active_dataset_snapshots

        registry = RuntimeContractRegistry.from_path(CANONICAL_RUNTIME_BINDINGS_PATH)
        authority = RuntimeEvidenceAuthority(runtime_registry=registry)
        def catalog_provider() -> DatasetCatalog:
            snapshots = trusted_active_dataset_snapshots(store, purpose="context")
            return DatasetCatalog(snapshots.values(), release_resolver=store)

        catalog = catalog_provider()
        executor = ClickHouseQueryExecutor(
            ClickHouseRuntime.from_env(),
            evidence_resolver=authority,
            rows_loader=authority.rows_loader,
            evidence_writer=authority._runtime_writer(),
            release_resolver=store,
        )
        return cls(
            catalog=catalog,
            registry=registry,
            executor=executor,
            release_resolver=store,
            evidence_authority=authority,
            store=store,
            catalog_provider=catalog_provider,
        )

    def compile(self, request: AnalysisRuntimeRequest) -> AnalysisCompileOutcome:
        return self._compile_with_catalog(request, self._active_catalog())

    def _compile_with_catalog(
        self,
        request: AnalysisRuntimeRequest,
        catalog: DatasetCatalog,
    ) -> AnalysisCompileOutcome:
        return compile_analysis_contract(
            run_id=request.run_id,
            proposal=request.proposal,
            accepted_capabilities=request.accepted_graph,
            catalog=catalog,
            registry=self.registry,
            as_of=request.as_of,
            permission_scope=request.permission_scope,
            release_resolver=self.release_resolver,
        )

    def _active_catalog(self) -> DatasetCatalog:
        return self._catalog_provider() if self._catalog_provider is not None else self.catalog

    def execute(
        self,
        request: AnalysisRuntimeRequest | Mapping[str, Any],
        proposal: Mapping[str, Any] | None = None,
        accepted_graph: Sequence[str] | None = None,
    ) -> AnalysisRuntimeResult:
        typed = (
            request
            if isinstance(request, AnalysisRuntimeRequest)
            else AnalysisRuntimeRequest.create(
                run_id=str(request.get("run_id") or ""),
                proposal=proposal or request.get("proposal") or {},
                accepted_graph=accepted_graph or request.get("accepted_graph") or (),
                as_of=(request.get("analysis_context") or {}).get("as_of")
                or request.get("as_of")
                or "",
                permission_scope=runtime_permission_scope_from_request(request),
                topic_id=str(request.get("topic_id") or ""),
                reuse_candidates=request.get("reuse_candidates") or (),
                attempted_signatures=request.get("attempted_signatures") or (),
                run_mode=str(request.get("run_mode") or "production"),
            )
        )
        catalog = self._active_catalog()
        compiled = self._compile_with_catalog(typed, catalog)
        snapshots = {item.snapshot_ref: item for item in catalog.snapshots()}
        if analysis_outcome_requires_preexecution_clarification(compiled):
            persistence = self._authority_records(
                compiled, (), {}, snapshots=snapshots
            )
            return AnalysisRuntimeResult(
                analysis_contract=compiled.analysis_contract,
                query_contracts=compiled.query_contracts,
                query_results=(),
                completeness_reports=(),
                capability_plans=compiled.capability_plans,
                bound_capability_inputs=MappingProxyType({}),
                repair_decisions=(),
                typed_gaps=tuple(
                    item.to_dict()
                    for item in compiled.analysis_contract.contract_gaps
                ),
                persistence_records=MappingProxyType(persistence),
            )
        results: list[QueryResultEnvelope] = []
        reports: list[CompletenessReport] = []
        decisions: list[QueryRepairDecision] = []
        reuse_decisions: list[Mapping[str, Any]] = []
        executed_contracts: list[QueryContract] = []
        for contract in compiled.query_contracts:
            contract_snapshots = {
                ref: snapshots[ref]
                for ref in contract.dataset_snapshot_refs
                if ref in snapshots
            }
            if len(contract_snapshots) != len(contract.dataset_snapshot_refs):
                missing = sorted(set(contract.dataset_snapshot_refs) - set(contract_snapshots))
                raise ValueError(
                    "query_contract_snapshot_unavailable:" + ",".join(missing)
                )
            validate_clickhouse_query_contract(
                contract,
                contract_snapshots,
                registry=self.registry,
                release_resolver=self.release_resolver,
            )
            validated_reuse, candidate, rerun_reason = (
                self._validated_reuse_candidate(
                    typed,
                    contract,
                    contract_snapshots,
                )
            )
            result: QueryResultEnvelope | None = None
            if validated_reuse is not None:
                try:
                    result = self.executor.materialize_reuse(
                        contract,
                        contract_snapshots,
                        source_result=validated_reuse.source_result,
                        source_rows=validated_reuse.source_rows,
                        source_result_ref=str(
                            validated_reuse.payload["result_ref"]
                        ),
                        candidate_signature=str(
                            validated_reuse.payload["candidate_signature"]
                        ),
                        release_resolver=self.release_resolver,
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    rerun_reason = (
                        "cache_materialization_invalid:"
                        + _exception_reason(exc)
                    )
            if result is None:
                result = self.executor.execute(
                    contract,
                    contract_snapshots,
                    release_resolver=self.release_resolver,
                )
            if candidate is not None:
                reused = validated_reuse is not None and not rerun_reason
                reuse_decisions.append(
                    MappingProxyType(
                        {
                            "source_ref": str(candidate.get("result_ref") or ""),
                            "result_ref": result.result_ref,
                            "decision": "reuse" if reused else "rerun",
                            "reason": (
                                "validated_authoritative_query_chain"
                                if reused
                                else str(rerun_reason or "reuse_candidate_mismatch")
                            ),
                            "can_support_claim": reused,
                            "requires_rerun": not reused,
                            "query_contract_ref": contract.query_contract_id,
                            "candidate_signature": str(
                                candidate.get("candidate_signature") or ""
                            ),
                        }
                    )
                )
            report = validate_query_result(
                contract,
                result,
                tuple(contract_snapshots.values()),
                evidence_writer=self.evidence_writer,
                release_resolver=self.release_resolver,
            )
            results.append(result)
            reports.append(report)
            executed_contracts.append(contract)
        if results:
            reports = list(
                validate_query_set(
                    tuple(executed_contracts),
                    tuple(results),
                    tuple(reports),
                    evidence_writer=self.evidence_writer,
                )
            )
        for contract, report in zip(executed_contracts, reports):
            if report.analysis_readiness != "ready":
                decisions.append(
                    plan_query_repair(contract, report, typed.attempted_signatures)
                )
        result_map = {item.query_contract_ref: item for item in results}
        report_map = {item.query_contract_ref: item for item in reports}
        bound: dict[str, BoundCapabilityInput] = {}
        for plan in compiled.capability_plans:
            bound[plan.capability_id] = bind_capability_inputs(
                plan,
                results=result_map,
                reports=report_map,
                evidence_resolver=self.evidence_resolver,
                rows_loader=self.rows_loader,
                evidence_writer=self.evidence_writer,
                runtime_registry=self.registry,
                release_resolver=self.release_resolver,
                run_mode=typed.run_mode,
            )
        persistence = self._authority_records(
            compiled, results, bound, snapshots=snapshots
        )
        gaps = tuple(item.to_dict() for item in compiled.analysis_contract.contract_gaps)
        return AnalysisRuntimeResult(
            analysis_contract=compiled.analysis_contract,
            query_contracts=compiled.query_contracts,
            query_results=tuple(results),
            completeness_reports=tuple(reports),
            capability_plans=compiled.capability_plans,
            bound_capability_inputs=MappingProxyType(bound),
            repair_decisions=tuple(decisions),
            typed_gaps=gaps,
            persistence_records=MappingProxyType(persistence),
            reuse_decisions=tuple(reuse_decisions),
        )

    def _validated_reuse_candidate(
        self,
        request: AnalysisRuntimeRequest,
        contract: QueryContract,
        snapshots: Mapping[str, DatasetSnapshot],
    ) -> tuple[
        _ValidatedReuseCandidate | None,
        Mapping[str, Any] | None,
        str,
    ]:
        if not request.reuse_candidates:
            return None, None, ""
        first_candidate: Mapping[str, Any] | None = None
        reasons: list[str] = []
        for raw_candidate in request.reuse_candidates:
            candidate = dict(raw_candidate)
            if first_candidate is None:
                first_candidate = candidate
            try:
                validated = self._validate_reuse_candidate(
                    candidate,
                    request=request,
                    contract=contract,
                    snapshots=snapshots,
                )
            except (
                AttributeError,
                AuthoritativeQueryChainError,
                EvidenceIntegrityError,
                KeyError,
                PermissionError,
                TypeError,
                ValueError,
            ) as exc:
                reasons.append(_exception_reason(exc))
                continue
            return validated, candidate, ""
        return (
            None,
            first_candidate,
            reasons[0] if reasons else "reuse_candidate_mismatch",
        )

    def _validate_reuse_candidate(
        self,
        candidate: Mapping[str, Any],
        *,
        request: AnalysisRuntimeRequest,
        contract: QueryContract,
        snapshots: Mapping[str, DatasetSnapshot],
    ) -> _ValidatedReuseCandidate:
        if set(candidate) != _REUSE_CANDIDATE_V1_FIELDS:
            raise EvidenceIntegrityError("reuse_candidate_shape_invalid")
        if str(candidate.get("schema_version") or "") != (
            "result-reuse-candidate.v1"
        ):
            raise EvidenceIntegrityError("reuse_candidate_schema_version_invalid")
        for field in _REUSE_CANDIDATE_V1_FIELDS.difference(
            _REUSE_CANDIDATE_SEQUENCE_FIELDS
        ):
            if not isinstance(candidate.get(field), str) or not candidate[field]:
                raise EvidenceIntegrityError(
                    f"reuse_candidate_scalar_invalid:{field}"
                )
        for field in _REUSE_CANDIDATE_SEQUENCE_FIELDS:
            value = candidate.get(field)
            if (
                not isinstance(value, (list, tuple))
                or isinstance(value, (str, bytes))
                or any(not isinstance(item, str) or not item for item in value)
            ):
                raise EvidenceIntegrityError(
                    f"reuse_candidate_sequence_invalid:{field}"
                )
        unsigned = dict(candidate)
        candidate_signature = str(unsigned.pop("candidate_signature") or "")
        if candidate_signature != canonical_digest(unsigned):
            raise EvidenceIntegrityError("reuse_candidate_signature_invalid")

        snapshot_fields = (
            "source_snapshot_refs",
            "source_snapshot_record_refs",
            "source_snapshot_record_digests",
            "source_release_refs",
            "source_release_authority_refs",
            "source_schema_fingerprints",
        )
        snapshot_lengths = {len(candidate[field]) for field in snapshot_fields}
        if len(snapshot_lengths) != 1 or snapshot_lengths == {0}:
            raise EvidenceIntegrityError("reuse_candidate_snapshot_cardinality")
        for left, right, reason in (
            (
                "completeness_record_refs",
                "completeness_record_digests",
                "reuse_candidate_completeness_cardinality",
            ),
            (
                "binding_record_refs",
                "binding_record_digests",
                "reuse_candidate_binding_cardinality",
            ),
        ):
            if not candidate[left] or len(candidate[left]) != len(candidate[right]):
                raise EvidenceIntegrityError(reason)

        if not request.topic_id:
            raise EvidenceIntegrityError("reuse_candidate_topic_missing")
        resolver_method = getattr(
            self.store,
            "resolve_result_candidate_authority",
            None,
        )
        if not callable(resolver_method):
            raise EvidenceIntegrityError(
                "reuse_candidate_authority_resolver_unavailable"
            )
        source_authority = resolver_method(
            result_ref=str(candidate["result_ref"]),
            topic_id=request.topic_id,
        )
        if not isinstance(source_authority, Mapping):
            raise EvidenceIntegrityError("reuse_candidate_source_authority_invalid")
        if str(source_authority.get("source_run_id") or "") != str(
            candidate["source_run_id"]
        ):
            raise EvidenceIntegrityError("reuse_candidate_source_run_mismatch")
        if str(source_authority.get("run_topic_id") or "") != request.topic_id:
            raise EvidenceIntegrityError("reuse_candidate_topic_owner_mismatch")
        if str(source_authority.get("run_status") or "") not in {
            "completed",
            "succeeded",
        }:
            raise EvidenceIntegrityError("reuse_candidate_source_run_not_complete")

        result_ref_record = _mapping_value(
            source_authority.get("result_ref_record")
        )
        result_record_expected = {
            "result_ref": str(candidate["result_ref"]),
            "topic_id": request.topic_id,
            "snapshot_id": str(candidate["runtime_snapshot_id"]),
            "contract_version": str(candidate["runtime_contract_version"]),
            "permission_scope": str(candidate["permission_scope"]),
            "semantic_scope": str(candidate["semantic_scope_signature"]),
        }
        if any(
            str(result_ref_record.get(key) or "") != expected
            for key, expected in result_record_expected.items()
        ):
            raise EvidenceIntegrityError("reuse_candidate_result_index_mismatch")

        source_request = _mapping_value(
            source_authority.get("source_run_request")
        )
        context_manifest = _mapping_value(source_request.get("context_manifest"))
        contract_versions = _mapping_value(
            context_manifest.get("contract_versions")
        )
        if str(context_manifest.get("snapshot_version") or "") != str(
            candidate["runtime_snapshot_id"]
        ):
            raise EvidenceIntegrityError("reuse_candidate_runtime_snapshot_mismatch")
        if str(contract_versions.get("runtime") or "") != str(
            candidate["runtime_contract_version"]
        ):
            raise EvidenceIntegrityError("reuse_candidate_runtime_contract_mismatch")

        source_analysis = _mapping_value(source_authority.get("analysis_contract"))
        stored_analysis_signature = str(
            source_authority.get("stored_analysis_contract_signature") or ""
        )
        computed_analysis_signature = analysis_contract_signature(source_analysis)
        if (
            str(source_analysis.get("analysis_contract_id") or "")
            != str(candidate["analysis_contract_ref"])
            or stored_analysis_signature != computed_analysis_signature
            or str(candidate["analysis_contract_signature"])
            != computed_analysis_signature
            or str(candidate["semantic_scope_signature"])
            != f"analysis-contract:sha256:{computed_analysis_signature}"
        ):
            raise EvidenceIntegrityError("reuse_candidate_analysis_authority_mismatch")

        query = self.evidence_resolver.resolve_query_execution_record(
            str(candidate["query_execution_record_ref"])
        )
        query_by_result = self.evidence_resolver.resolve_query_execution(
            str(candidate["result_ref"])
        )
        if query is None or query_by_result is None:
            raise EvidenceIntegrityError("reuse_candidate_query_record_missing")
        if query.record_ref != query_by_result.record_ref:
            raise EvidenceIntegrityError("reuse_candidate_query_record_conflict")
        if runtime_evidence_record_integrity_errors(query):
            raise EvidenceIntegrityError("reuse_candidate_query_record_invalid")
        if (
            query.record_digest != str(candidate["query_execution_record_digest"])
            or query.query_contract_ref != str(candidate["query_contract_ref"])
            or query.contract_signature
            != str(candidate["query_contract_signature"])
            or query.contract.analysis_contract_ref
            != str(candidate["analysis_contract_ref"])
            or query.result_ref != str(candidate["result_ref"])
            or query.rows_ref != str(candidate["rows_ref"])
            or query.completeness_report_ref
            != str(candidate["completeness_report_ref"])
        ):
            raise EvidenceIntegrityError("reuse_candidate_query_authority_mismatch")

        rows_record = self.evidence_resolver.resolve_rows_record(
            str(candidate["rows_record_ref"])
        )
        rows_by_ref = self.evidence_resolver.resolve_rows(str(candidate["rows_ref"]))
        if rows_record is None or rows_by_ref is None:
            raise EvidenceIntegrityError("reuse_candidate_rows_record_missing")
        if rows_record.record_ref != rows_by_ref.record_ref:
            raise EvidenceIntegrityError("reuse_candidate_rows_record_conflict")
        if (
            runtime_evidence_record_integrity_errors(rows_record)
            or rows_record.record_digest != str(candidate["rows_record_digest"])
            or rows_record.rows_ref != str(candidate["rows_ref"])
            or rows_record.rows_content_hash != str(candidate["rows_content_hash"])
            or rows_record.row_count != query.row_count
        ):
            raise EvidenceIntegrityError("reuse_candidate_rows_authority_mismatch")

        candidate_snapshot_refs = tuple(candidate["source_snapshot_refs"])
        if (
            candidate_snapshot_refs != query.source_snapshot_refs
            or tuple(candidate["source_snapshot_record_refs"])
            != query.source_snapshot_record_refs
            or tuple(candidate["source_snapshot_record_digests"])
            != query.source_snapshot_record_digests
        ):
            raise EvidenceIntegrityError("reuse_candidate_snapshot_binding_mismatch")
        for index, snapshot_ref in enumerate(candidate_snapshot_refs):
            snapshot_record = self.evidence_resolver.resolve_snapshot(snapshot_ref)
            if snapshot_record is None:
                raise EvidenceIntegrityError("reuse_candidate_snapshot_record_missing")
            if (
                runtime_evidence_record_integrity_errors(snapshot_record)
                or snapshot_record.record_ref
                != candidate["source_snapshot_record_refs"][index]
                or snapshot_record.record_digest
                != candidate["source_snapshot_record_digests"][index]
                or snapshot_record.snapshot.release_ref
                != candidate["source_release_refs"][index]
                or snapshot_record.snapshot.authority_record_ref
                != candidate["source_release_authority_refs"][index]
                or snapshot_record.snapshot.schema_fingerprint
                != candidate["source_schema_fingerprints"][index]
            ):
                raise EvidenceIntegrityError(
                    "reuse_candidate_snapshot_authority_mismatch"
                )
            release = self.release_resolver.resolve_dataset_release(
                snapshot_record.snapshot.release_ref
            )
            if (
                release.authority_record_ref
                != candidate["source_release_authority_refs"][index]
                or release.integrity_errors
                or snapshot_ref not in release.snapshot_refs
            ):
                raise EvidenceIntegrityError("reuse_candidate_release_authority_mismatch")

        completeness_records = []
        for record_ref, record_digest in zip(
            candidate["completeness_record_refs"],
            candidate["completeness_record_digests"],
        ):
            record = self.evidence_resolver.resolve_completeness(record_ref)
            if record is None:
                raise EvidenceIntegrityError(
                    "reuse_candidate_completeness_record_missing"
                )
            if (
                runtime_evidence_record_integrity_errors(record)
                or record.report_digest != record_digest
            ):
                raise EvidenceIntegrityError(
                    "reuse_candidate_completeness_authority_mismatch"
                )
            completeness_records.append(record)

        matching_result: QueryResultEnvelope | None = None
        matching_rows: tuple[Mapping[str, Any], ...] | None = None
        matching_report: CompletenessReport | None = None
        binding_records = []
        for record_ref, binding_digest in zip(
            candidate["binding_record_refs"],
            candidate["binding_record_digests"],
        ):
            binding = self.evidence_resolver.resolve_capability_binding(record_ref)
            if binding is None:
                raise EvidenceIntegrityError("reuse_candidate_binding_record_missing")
            if (
                runtime_evidence_record_integrity_errors(binding)
                or binding.binding_digest != binding_digest
            ):
                raise EvidenceIntegrityError(
                    "reuse_candidate_binding_authority_mismatch"
                )
            if binding.analysis_contract_ref != str(
                candidate["analysis_contract_ref"]
            ):
                raise EvidenceIntegrityError(
                    "reuse_candidate_binding_owner_mismatch"
                )
            chain = validate_authoritative_query_chain(
                binding,
                resolver=self.evidence_resolver,
                rows_loader=self.rows_loader,
                runtime_registry=self.registry,
                release_resolver=self.release_resolver,
            )
            binding_records.append(binding)
            chain_results = (*chain.primary_results, *chain.validation_results)
            chain_reports = (*chain.primary_reports, *chain.validation_reports)
            for source_result, source_report in zip(chain_results, chain_reports):
                if source_result.result_ref != str(candidate["result_ref"]):
                    continue
                matching_result = source_result
                matching_report = source_report
                matching_rows = chain.rows_by_ref.get(source_result.rows_ref)
        if matching_result is None or matching_rows is None or matching_report is None:
            raise EvidenceIntegrityError("reuse_candidate_binding_result_missing")
        if not any(
            item.status == "ready"
            and str(candidate["result_ref"])
            in (*item.result_refs, *item.validation_result_refs)
            for item in binding_records
        ):
            raise EvidenceIntegrityError("reuse_candidate_binding_not_ready")
        if (
            matching_result.execution_status != "succeeded"
            or matching_report.completeness_status != "complete"
            or matching_report.analysis_readiness != "ready"
            or not any(
                item.result_ref == matching_result.result_ref
                and item.report_ref == matching_report.report_ref
                for item in completeness_records
            )
        ):
            raise EvidenceIntegrityError("reuse_candidate_completeness_not_ready")
        indexed_payload = _mapping_value(result_ref_record.get("payload"))
        if canonical_digest(indexed_payload) != canonical_digest(candidate):
            raise EvidenceIntegrityError("reuse_candidate_result_index_payload_mismatch")

        if request.permission_scope != str(candidate["permission_scope"]):
            raise PermissionError("reuse_permission_scope_mismatch")
        if tuple(contract.dataset_snapshot_refs) != candidate_snapshot_refs:
            raise EvidenceIntegrityError("reuse_snapshot_ref_mismatch")
        current_snapshots = tuple(snapshots[ref] for ref in contract.dataset_snapshot_refs)
        if tuple(item.release_ref for item in current_snapshots) != tuple(
            candidate["source_release_refs"]
        ) or tuple(item.authority_record_ref for item in current_snapshots) != tuple(
            candidate["source_release_authority_refs"]
        ):
            raise EvidenceIntegrityError("reuse_release_mismatch")
        if tuple(item.schema_fingerprint for item in current_snapshots) != tuple(
            candidate["source_schema_fingerprints"]
        ):
            raise EvidenceIntegrityError("reuse_schema_fingerprint_mismatch")
        if _window_payloads(contract) != _window_payloads(query.contract):
            raise EvidenceIntegrityError("reuse_fixed_window_mismatch")
        if canonical_digest(contract.result_shape) != canonical_digest(
            query.contract.result_shape
        ):
            raise EvidenceIntegrityError("reuse_result_schema_mismatch")
        if (
            query_contract_signature(contract) != contract.contract_signature
            or contract.contract_signature
            != str(candidate["query_contract_signature"])
        ):
            raise EvidenceIntegrityError("reuse_query_signature_mismatch")
        return _ValidatedReuseCandidate(
            payload=MappingProxyType(dict(candidate)),
            source_result=matching_result,
            source_rows=tuple(dict(row) for row in matching_rows),
        )

    def _authority_records(
        self,
        compiled: AnalysisCompileOutcome,
        results: Sequence[QueryResultEnvelope],
        bound: Mapping[str, BoundCapabilityInput],
        *,
        snapshots: Mapping[str, DatasetSnapshot],
    ) -> dict[str, Any]:
        query_records = tuple(
            record
            for result in results
            if (record := self.evidence_resolver.resolve_query_execution(result.result_ref))
            is not None
        )
        rows_records = tuple(
            record
            for query in query_records
            if (record := self.evidence_resolver.resolve_rows(query.rows_ref)) is not None
        )
        snapshot_refs = tuple(dict.fromkeys(
            (
                *(ref for query in query_records for ref in query.source_snapshot_refs),
                *(
                    ref
                    for contract in compiled.query_contracts
                    for ref in contract.dataset_snapshot_refs
                ),
            )
        ))
        snapshot_records = tuple(
            record or snapshot_authority_record(snapshots[ref])
            for ref in snapshot_refs
            if ref in snapshots
            for record in (self.evidence_resolver.resolve_snapshot(ref),)
        )
        binding_records = tuple(
            record
            for item in bound.values()
            if item.binding_manifest_ref
            and (
                record := self.evidence_resolver.resolve_capability_binding(
                    item.binding_manifest_ref
                )
            )
            is not None
            and (record.result_refs or record.validation_result_refs)
        )
        completeness_refs = tuple(
            dict.fromkeys(
                (
                    *(
                        ref
                        for item in binding_records
                        for ref in (
                            *item.completeness_record_refs,
                            *item.validation_completeness_record_refs,
                        )
                    ),
                )
            )
        )
        completeness_by_ref = {
            record.record_ref: record
            for query in query_records
            if (
                record := self.evidence_authority.resolve_latest_completeness(
                    query.completeness_report_ref
                )
            )
            is not None
        }
        completeness_by_ref.update(
            {
                record.record_ref: record
                for ref in completeness_refs
                if (record := self.evidence_resolver.resolve_completeness(ref)) is not None
            }
        )
        completeness_records = tuple(
            record
            for _, record in sorted(completeness_by_ref.items())
        )
        analysis = {
            **compiled.analysis_contract.to_dict(),
            "contract_signature": analysis_contract_signature(compiled.analysis_contract),
        }
        return {
            "analysis_contract": canonical_value(analysis),
            "query_contracts": tuple(compiled.query_contracts),
            "query_execution_records": query_records,
            "rows_records": rows_records,
            "snapshot_records": snapshot_records,
            "completeness_records": completeness_records,
            "capability_binding_records": binding_records,
        }

    def build_persistence_bundle(
        self,
        result: AnalysisRuntimeResult,
        *,
        answer_package: Mapping[str, Any],
        request: Mapping[str, Any],
        artifact_path: str,
        publication_mode: str = "complete",
        publication_audit: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if publication_mode not in {"complete", "waiting_for_clarification"}:
            raise ValueError("analysis_runtime_publication_mode_invalid")
        base = dict(result.persistence_records)
        if publication_mode == "waiting_for_clarification":
            base, audit = _project_waiting_persistence_records(base)
            if publication_audit is not None:
                publication_audit.update(audit)
        bindings = tuple(base["capability_binding_records"])
        package_evidence = {
            str(item.get("binding_manifest_ref") or ""): dict(item)
            for section in answer_package.get("sections") or ()
            if isinstance(section, Mapping)
            for item in (section.get("payload") or {}).get("evidence") or ()
            if isinstance(item, Mapping) and item.get("binding_manifest_ref")
        }
        claims = tuple(
            dict(claim)
            for section in answer_package.get("sections") or ()
            if isinstance(section, Mapping)
            and (section.get("section_id") or section.get("id")) == "summary"
            for claim in (section.get("payload") or {}).get("claims") or ()
            if isinstance(claim, Mapping)
        )
        claim_evidence_refs = {
            str(ref)
            for claim in claims
            for ref in claim.get("evidence_refs") or ()
            if ref
        }
        evidence_manifests: list[dict[str, Any]] = []
        evidence_by_ref: dict[str, dict[str, Any]] = {}
        for binding in bindings:
            source = package_evidence.get(binding.record_ref, {})
            evidence_ref = str(source.get("evidence_ref") or "")
            if not evidence_ref:
                evidence_ref = f"evidence:{binding.record_ref}"
            result_refs = tuple(
                dict.fromkeys((*binding.result_refs, *binding.validation_result_refs))
            )
            completeness_refs = tuple(
                dict.fromkeys(
                    (
                        *binding.completeness_record_refs,
                        *binding.validation_completeness_record_refs,
                    )
                )
            )
            manifest = {
                **source,
                "evidence_ref": evidence_ref,
                "binding_record_ref": binding.record_ref,
                "result_refs": result_refs,
                "completeness_record_refs": completeness_refs,
                "context_manifest_ref": "",
                "supported_claim_types": binding.supported_claim_types,
                "maximum_claim_strength": binding.maximum_claim_strength,
                "maximum_claim_strength_rank": binding.maximum_claim_strength_rank,
                "claim_strength_taxonomy_version": binding.claim_strength_taxonomy_version,
            }
            evidence_manifests.append(manifest)
            evidence_by_ref[evidence_ref] = manifest
        missing_claim_evidence = claim_evidence_refs - set(evidence_by_ref)
        if publication_mode == "waiting_for_clarification":
            claims = tuple(
                claim
                for claim in claims
                if claim.get("evidence_refs")
                and not (
                    {
                        str(ref)
                        for ref in claim.get("evidence_refs") or ()
                        if ref
                    }
                    - set(evidence_by_ref)
                )
            )
            claim_evidence_refs = {
                str(ref)
                for claim in claims
                for ref in claim.get("evidence_refs") or ()
                if ref
            }
        elif missing_claim_evidence:
            raise ValueError(
                "analysis_runtime_claim_evidence_unpersistable:"
                + ",".join(sorted(missing_claim_evidence))
            )

        context_records: tuple[Mapping[str, Any], ...] = ()
        provenance_records: tuple[Mapping[str, Any], ...] = ()
        verified_claims: tuple[Mapping[str, Any], ...] = ()
        claim_links: tuple[Mapping[str, Any], ...] = ()
        if claims:
            build_context = AnswerPackageBuildContext.create(
                request=request,
                artifact_path=artifact_path,
            )
            sources = tuple(
                (
                    {"type": "evidence", "ref": evidence_ref, "can_support_claim": True}
                )
                for evidence_ref in sorted(claim_evidence_refs)
            ) + tuple(
                {
                    "type": "completeness",
                    "ref": completeness_ref,
                    "can_support_claim": True,
                }
                for evidence_ref in sorted(claim_evidence_refs)
                for completeness_ref in evidence_by_ref[evidence_ref].get(
                    "completeness_record_refs"
                )
                or ()
            )
            context = build_context_manifest_record(
                run_id=str(request.get("run_id") or ""),
                thread_id=str(build_context.context_owner.get("thread_id") or ""),
                topic_id=str(build_context.context_owner.get("topic_id") or ""),
                sources=sources,
                permission_context=(
                    build_context.context_owner.get("permission_context") or {}
                ),
                accepted_assumptions=tuple(
                    dict(item)
                    for item in (
                        (request.get("context_manifest") or {}).get(
                            "accepted_assumptions"
                        )
                        or ()
                    )
                    if isinstance(item, Mapping)
                ),
            )
            for manifest in evidence_manifests:
                manifest["context_manifest_ref"] = context["manifest_id"]
            provenance = dict(build_context.trusted_provenance)
            built_claims = tuple(
                build_verified_claim_record(
                    claim,
                    run_id=str(request.get("run_id") or ""),
                    context_manifest=context,
                    evidence_by_ref=evidence_by_ref,
                    trusted_provenance=provenance,
                )
                for claim in claims
            )
            links = tuple(
                {
                    "claim_ref": claim["claim_ref"],
                    "evidence_ref": evidence_ref,
                    "context_manifest_ref": context["manifest_id"],
                }
                for claim in built_claims
                for evidence_ref in claim["evidence_refs"]
            )
            context_records = (context,)
            provenance_records = (provenance,)
            verified_claims = built_claims
            claim_links = links
        return {
            **base,
            "evidence_manifests": tuple(evidence_manifests),
            "context_manifests": context_records,
            "trusted_provenance_records": provenance_records,
            "verified_claims": verified_claims,
            "claim_links": claim_links,
            "repair_attempts": tuple(
                {
                    "attempt_ref": f"repair:{result.analysis_contract.analysis_contract_id}:{index}",
                    "failed_signature": item.failed_signature,
                    "action": item.action,
                    "reason": item.reason,
                }
                for index, item in enumerate(result.repair_decisions, start=1)
            ),
        }


def _mapping_value(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        payload = value.to_dict()
        return dict(payload) if isinstance(payload, Mapping) else {}
    return {}


def _exception_reason(exc: BaseException) -> str:
    reason = str(exc).strip()
    return reason or type(exc).__name__


def _window_payloads(contract: QueryContract) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        canonical_value(item.to_dict())
        for item in contract.resolved_windows
    )


def _project_waiting_persistence_records(
    records: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    required = _required_waiting_persistence_records(records)
    query_contracts = required["query_contracts"]
    query_records = required["query_execution_records"]
    rows_records = required["rows_records"]
    snapshot_records = required["snapshot_records"]
    completeness_records = required["completeness_records"]
    bindings = required["capability_binding_records"]
    query_contract_refs = {
        contract.query_contract_id for contract in query_contracts
    }
    query_by_result = {record.result_ref: record for record in query_records}
    rows_by_record_ref = {record.record_ref: record for record in rows_records}
    completeness_by_record_ref = {
        record.record_ref: record for record in completeness_records
    }
    snapshot_refs = {record.snapshot_ref for record in snapshot_records}
    retained_bindings = tuple(
        binding
        for binding in bindings
        if _binding_has_complete_persistence_closure(
            binding,
            query_contract_refs=query_contract_refs,
            query_by_result=query_by_result,
            rows_by_record_ref=rows_by_record_ref,
            completeness_by_record_ref=completeness_by_record_ref,
            snapshot_refs=snapshot_refs,
        )
    )
    retained_result_refs = {
        ref
        for binding in retained_bindings
        for ref in (*binding.result_refs, *binding.validation_result_refs)
    }
    retained_rows_record_refs = {
        ref
        for binding in retained_bindings
        for ref in (
            *binding.rows_metadata_record_refs,
            *binding.validation_rows_metadata_record_refs,
        )
    }
    retained_completeness_record_refs = {
        ref
        for binding in retained_bindings
        for ref in (
            *binding.completeness_record_refs,
            *binding.validation_completeness_record_refs,
        )
    }
    all_result_refs = {record.result_ref for record in query_records}
    omitted_result_refs = tuple(sorted(all_result_refs - retained_result_refs))
    projected = {
        **dict(records),
        "query_contracts": query_contracts,
        "query_execution_records": tuple(
            record
            for record in query_records
            if record.result_ref in retained_result_refs
        ),
        "rows_records": tuple(
            record
            for record in rows_records
            if record.record_ref in retained_rows_record_refs
        ),
        "snapshot_records": snapshot_records,
        "completeness_records": tuple(
            record
            for record in completeness_records
            if record.record_ref in retained_completeness_record_refs
        ),
        "capability_binding_records": retained_bindings,
    }
    return projected, {
        "publication_mode": "waiting_for_clarification",
        "omitted_result_refs": omitted_result_refs,
        "omitted_result_count": len(omitted_result_refs),
        "retained_result_count": len(retained_result_refs),
        "owner": "analysis_runtime_persistence_owner",
        "reason": "unbound_result_chain_omitted",
    }


def _required_waiting_persistence_records(
    records: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        analysis_contract = records["analysis_contract"]
    except KeyError as exc:
        raise ValueError(
            "analysis_runtime_partial_publication_records_invalid:analysis_contract"
        ) from exc
    if not isinstance(analysis_contract, Mapping):
        raise ValueError(
            "analysis_runtime_partial_publication_records_invalid:analysis_contract"
        )
    typed_sequences = {
        "query_contracts": QueryContract,
        "query_execution_records": QueryExecutionRecord,
        "rows_records": RowsRecord,
        "snapshot_records": SnapshotRecord,
        "completeness_records": CompletenessRecord,
        "capability_binding_records": CapabilityBindingRecord,
    }
    required: dict[str, Any] = {"analysis_contract": analysis_contract}
    for field, expected_type in typed_sequences.items():
        try:
            raw = records[field]
        except KeyError as exc:
            raise ValueError(
                f"analysis_runtime_partial_publication_records_invalid:{field}"
            ) from exc
        if not isinstance(raw, (list, tuple)) or any(
            type(record) is not expected_type for record in raw
        ):
            raise ValueError(
                f"analysis_runtime_partial_publication_records_invalid:{field}"
            )
        required[field] = tuple(raw)
    return required


def _binding_has_complete_persistence_closure(
    binding: CapabilityBindingRecord,
    *,
    query_contract_refs: set[str],
    query_by_result: Mapping[str, QueryExecutionRecord],
    rows_by_record_ref: Mapping[str, RowsRecord],
    completeness_by_record_ref: Mapping[str, CompletenessRecord],
    snapshot_refs: set[str],
) -> bool:
    groups = (
        (
            binding.query_contract_refs,
            binding.result_refs,
            binding.query_execution_record_refs,
            binding.query_execution_record_digests,
            binding.rows_refs,
            binding.rows_metadata_record_refs,
            binding.rows_metadata_record_digests,
            binding.rows_content_hashes,
            binding.completeness_report_refs,
            binding.completeness_record_refs,
            binding.completeness_record_digests,
        ),
        (
            binding.validation_query_contract_refs,
            binding.validation_result_refs,
            binding.validation_query_execution_record_refs,
            binding.validation_query_execution_record_digests,
            binding.validation_rows_refs,
            binding.validation_rows_metadata_record_refs,
            binding.validation_rows_metadata_record_digests,
            binding.validation_rows_content_hashes,
            binding.validation_completeness_report_refs,
            binding.validation_completeness_record_refs,
            binding.validation_completeness_record_digests,
        ),
    )
    has_result = False
    for group in groups:
        count = len(group[1])
        if any(len(values) != count for values in group):
            return False
        for index, result_ref in enumerate(group[1]):
            has_result = True
            query = query_by_result.get(result_ref)
            rows = rows_by_record_ref.get(group[5][index])
            completeness = completeness_by_record_ref.get(group[9][index])
            if (
                query is None
                or query.query_contract_ref != group[0][index]
                or query.query_contract_ref not in query_contract_refs
                or query.record_ref != group[2][index]
                or query.record_digest != group[3][index]
                or query.rows_ref != group[4][index]
                or not set(query.source_snapshot_refs).issubset(snapshot_refs)
                or rows is None
                or rows.rows_ref != group[4][index]
                or rows.record_digest != group[6][index]
                or rows.rows_content_hash != group[7][index]
                or completeness is None
                or completeness.result_ref != result_ref
                or completeness.query_contract_ref != group[0][index]
                or completeness.report_ref != group[8][index]
                or completeness.report_digest != group[10][index]
            ):
                return False
    return has_result
