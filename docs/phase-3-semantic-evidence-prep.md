# Phase 3 Semantic Evidence Prep

Status: contract-level design prep from Phase 1 contracts, Phase 2 compiler prep, and launch eval expectation packages

Scope: semantic query, evidence envelope, evidence ledger concept, Answer Package handoff, verifier/eval acceptance. No runtime implementation, SQL, final database table structure, physical ClickHouse/Postgres design, service API, or frontend work.

## Design Boundary

Phase 3 prepares the semantic/evidence layer that sits after Phase 2 accepted graph compilation and before Answer Package synthesis.

Runtime truth stays in WAJE-owned contracts, compiler, semantic compiler, capability outputs, evidence reducer, permission policy, and verifier. LLM, LangGraph, recipes, process UI, and raw result snippets can propose or display work, but they do not define BI semantics, evidence strength, permission, or final claim wording.

This document uses field-level drafts so implementation can proceed without inventing BI meaning. The fields below are contract fields, not final persistence schema.

## Phase 3 Inputs

Semantic query and evidence design must consume only reviewed or compiler-accepted sources:

- Phase 1 metric contracts: metric identity, formula paths, time semantics, supported grains, baseline compatibility, quality guards, materiality policy.
- Phase 1 dimension contracts: business meaning, supported grains, permission-limited fields, known gaps.
- Phase 1 event contracts and static assumptions: event identity, event window, source status, affected scope, unsupported grains, owner, source, valid window, refresh rule, wording limit.
- Phase 1 backlog: gap id, affected factor groups, capabilities, question families, launch impact, upgrade path.
- Phase 1 factor ledger and capability support: `factor_group_id`, `support_id`, capability, grain, claim type, `business_evidence_state`, `data_contract_state`, `evidence_type`, `strength`, `wording_limit`.
- Phase 2 accepted graph: bound intent, accepted nodes, params, dependencies, mutation log, disabled/degraded/blocked paths, clarification outcomes, Answer Package constraints.
- Launch eval packages: representative expectation cases, allowed claims, visual blocks, verifier checks, failure attribution labels.

## Shared Vocabulary

Phase 3 must reuse the existing vocabulary from `contracts/README.md`, `docs/phase-2-graph-compiler-prep.md`, and `contracts/ledger/capability-support.yaml`.

`business_evidence_state`:

- `quantifiable`
- `candidate_mechanism`
- `contextual_evidence`
- `insufficient`
- `permission_limited`
- `unsupported_grain`
- `out_of_scope`

`data_contract_state`:

- `contract_backed`
- `evidence_linked`
- `static_assumption`
- `missing_contract`
- `permission_limited`
- `unsupported_grain`
- `out_of_scope_for_now`

`evidence_type`:

- `accounting_contribution`
- `statistical_association`
- `candidate_mechanism`
- `causal_evidence`
- `insufficient`

`strength`:

- `high`
- `medium`
- `low`
- `insufficient`

`wording_limit`:

- `quantified`
- `stable_pattern`
- `candidate`
- `context`
- `insufficient`
- `blocked`

For the current `paid_amount` launch baseline, `causal_evidence` remains blocked unless later reviewed contracts add control, counterfactual, treated/control, or intervention evidence.

## Semantic Query Layer Responsibilities

The semantic query layer prepares executable semantic requests for later capability execution. It must:

- bind metric, scope, grain, time range, baseline, filters, dimensions, events, static assumptions, and candidate factors to reviewed contracts
- preserve accepted graph node linkage, contract pins, current-data snapshot, and clarification assumptions
- enforce permission, supported grain, quality, materiality, freshness, and cumulative-value guards before execution
- distinguish target windows, baseline windows, event windows, metric component windows, and cumulative values
- create blocked or degraded semantic query records when a path cannot execute honestly
- expose auditable semantic intent without raw SQL or physical schema in this design layer

### Semantic Query Contract

`semantic_query` field-level draft:

| Field | Required | Trace source | Notes |
| --- | --- | --- | --- |
| `semantic_query_id` | yes | Phase 3 local semantic layer | Stable id used by evidence envelopes and verifier handoff. |
| `run_id` | yes | Phase 2 accepted graph | Links the query to one investigation run. |
| `accepted_graph_id` | yes | Phase 2 accepted graph | Prevents orphan query records. |
| `accepted_graph_node_id` | yes | Phase 2 `nodes[].node_id` | Origin capability node. |
| `graph_version` | yes | Phase 2 accepted graph | Required after repair, clarification, degrade, or graph mutation. |
| `question_family` | yes | Phase 1 question families / eval package | One of the eight accepted families. |
| `capability` | yes | Phase 1 capability cards | One of the eight foundational capability ids. |
| `support_id` | yes when available | `capability-support.yaml` | Required for supported/degraded paths that map to support records. |
| `factor_group_id` | yes when available | `factor-ledger.yaml` | Required when factor support constrains claim wording. |
| `semantic_query_intent` | yes | Phase 2 target claim + launch eval intent | Business intent for this query. Fields below. |
| `metric_binding` | yes | `paid-amount.metric.yaml` | Metric id, decomposition path when relevant, amount basis, currency, supported grain. |
| `scope_binding` | yes | Phase 2 intent binding / PRD scope rules | Business scope, filters, population, named business object, visual scope when relevant. |
| `grain_binding` | yes | metric, dimension, event, support records | Requested grain, accepted grain, fallback grain, unsupported grain reason. |
| `time_window_binding` | yes | metric time semantics / eval intent | Target window, timezone, inclusivity, date basis, month-length handling. |
| `baseline_binding` | when comparative | Phase 2 baseline binding / eval intent | Baseline type, baseline window, comparability status, clarification or inference source. |
| `dimension_bindings` | when segmented | dimensions / factor ledger | Dimension ids, supported grains, permission state, sparse rule refs. |
| `event_bindings` | when event-linked | events / assumptions / backlog | Event ids, assumption refs, event windows, affected scope, source status, wording limit. |
| `contract_version_pins` | yes | Phase 2 `contract_bundle.pins[]` | Exact artifact path, version, hash, review status, semantic role. |
| `current_data_snapshot` | yes | metric data snapshot policy / Phase 2 binding | Source contract, exported date, coverage, watermark, freshness status, artifact policy. |
| `permission_handoff` | yes | dimensions, factor ledger, product decisions | Fields listed below. Blocks raw user id, IP, device id output. |
| `data_quality_handoff` | yes | metric quality guards / data_quality_check card | Required checks, quality refs, pending checks, destructive issues. |
| `guard_handoff` | yes | metric quality guards / Phase 2 lint rules | Materiality, freshness, cumulative-value, formula reconciliation, grain, time-boundary guards. |
| `semantic_query_status` | yes | Phase 2 node/path status | `accepted`, `degraded`, `blocked`, `skipped`, or `repair_requested`. |
| `disabled_degraded_blocked_path_refs` | yes | Phase 2 path records | Empty only when no related path limitation exists. |
| `expected_evidence_contract` | yes | capability card + support record | Required typed payload, evidence envelope, evidence type, strength, wording limit. |
| `answer_package_handoff` | yes | Phase 2 Answer Package constraints | Claim groups, visual blocks, limitations, verifier checks that must consume this query. |

`semantic_query_intent`:

| Field | Required | Trace source | Notes |
| --- | --- | --- | --- |
| `target_claim` | yes | Phase 2 candidate/accepted node | Business claim this query can support or limit. |
| `claim_type` | yes | support record / eval allowed claims | Examples: `formula_component_contribution`, `recurring_pattern_existence`, `segment_contribution_or_mix_shift`. |
| `analysis_role` | yes | capability card | `primary_evidence`, `quality_guard`, `candidate_context`, `limitation_review`, or `verifier_input`. |
| `user_hypothesis_ref` | when present | user intent / clarification | Keeps user-proposed hypotheses separate from supported evidence. |
| `recipe_origin` | when present | Phase 2 accepted node | Trace only; recipe origin does not authorize execution. |
| `clarification_outcome_ref` | when present | Phase 2 clarification package | Links `user_selected`, `recommended_inference_selected`, `agent_instructed_differently`, or `system_inferred`. |
| `launch_eval_case_refs` | when eval | launch expectation package | Used by eval verifier, never by production runtime as BI truth. |

### Binding Requirements

Metric, scope, grain, time, and baseline binding must be explicit before a query can produce evidence.

| Binding | Minimum fields | Required guard | Block/degrade behavior |
| --- | --- | --- | --- |
| Metric | `metric_id`, business name, amount basis, currency basis, formula path when relevant | `metric_contract_required` | Invalid metric contract blocks. Missing component contracts degrade or block formula claim. |
| Scope | population, filters, named object, business family, claim scope | scope/broadening check | Evidence from local scope cannot support full-scope claim. |
| Grain | requested grain, accepted grain, supported grain refs, blocked grain refs | `grain_guard`, permission, sparse-cell policy | Unsupported grain with safe fallback degrades; forbidden raw grain blocks. |
| Time window | target start/end, timezone, inclusivity, date basis, window family | `time_boundary_guard`, `current_data_snapshot_guard` | Missing time semantics triggers repair or degrade through `timezone_policy`; illegal cumulative use blocks. |
| Baseline | type, target window, baseline window, comparability, chosen/inferred/clarified source | baseline compatibility check | Wrong or missing baseline blocks comparative strong claims. |
| Contract pins | artifact path, version, hash, review status, semantic role | `contract_version_unpinned` | Missing pins auto-repair when deterministic; unreadable/invalid contracts block. |
| Current data | source contract, exported date, coverage, watermark/freshness, artifact policy | freshness/current-data guard | Stale or incomplete data degrades; destructive quality issue blocks dependent claim. |

### Permission Handoff

`permission_handoff`:

| Field | Required | Trace source | Notes |
| --- | --- | --- | --- |
| `requested_output_grain` | yes | user intent / accepted node | Example: channel, payment_method, city, raw id, claim_group. |
| `accepted_visible_grain` | yes | dimension contract / permission policy | Aggregate fallback must be explicit. |
| `blocked_fields` | yes | dimension contract / product decisions | Raw `用户ID`, `IP`, and `设备ID` output is blocked. |
| `internal_use_only_fields` | yes when used | dimension contract | Identifiers can support dedup or internal quality checks without visible output. |
| `masking_or_aggregation_required` | yes | permission policy | States visible treatment. |
| `sparse_cell_rule_refs` | yes when relevant | dimension/factor ledger | Required before fine segment claims can publish. |
| `limitation_refs` | yes when limited | factor ledger review limitations | Example: `pii_dimension_output_limit`. |
| `permission_status` | yes | `data_contract_state` vocab | Use `contract_backed`, `permission_limited`, `unsupported_grain`, or block reason. |
| `answer_visibility` | yes | Answer Package constraints | `first_screen`, `limitations`, `follow_up`, or `internal_only`. |

Permission failures block the affected semantic query and any dependent claim group. Aggregate fallback is allowed only when the dimension/factor contract and permission policy allow it.

### Data Quality Handoff

`data_quality_handoff`:

| Field | Required | Trace source | Notes |
| --- | --- | --- | --- |
| `required_check_ids` | yes | metric guards / capability card | Examples: metric identity, completeness, dedup, time semantics, cumulative-value, materiality, permission. |
| `quality_evidence_refs` | yes when checks ran | `data_quality_check` envelope | Evidence refs that downstream claims must cite or inherit. |
| `pending_quality_checks` | yes | accepted graph / compiler mutations | Pending checks block first-screen or strong claims. |
| `quality_flags` | yes | capability payload | Completeness, duplicate, source snapshot, timezone, sparse, stale, mismatch flags. |
| `destructive_quality_issues` | yes | data quality capability | Issues that invalidate dependent claim paths. |
| `backlog_refs` | yes when gap exists | `missing-contracts.yaml` | Required for missing contracts. |
| `affected_claim_group_ids` | yes when known | Answer Package constraints | Forces data limitations into claim groups. |

Data quality output cannot raise another capability's evidence strength. It can block, degrade, or constrain claim wording.

### Guard Handoff

`guard_handoff`:

| Guard | Required when | Trace source | Handoff fields |
| --- | --- | --- | --- |
| `materiality_guard` | pattern, anomaly, health, baseline movement | `paid-amount.metric.yaml` materiality policy | `threshold_grain`, `threshold_ref`, `comparable_window_status`, `materiality_status`, `degrade_reason`. |
| `freshness_guard` | every evidence-producing query | current-data snapshot policy | `freshness_status`, `source_watermark`, `coverage_start`, `coverage_end`, `artifact_policy`. |
| `cumulative_value_guard` | time/window/phase comparisons | metric quality guards / eval LE-001, LE-007 | `value_kind`, `window_value_allowed`, `blocked_reason`, `repair_required`. |
| `formula_reconciliation_guard` | formula, bridge, attribution, numeric comparison | metric decomposition paths / formula card | `reconciliation_required`, `component_coverage`, `residual_policy`, `status`. |
| `grain_guard` | segmented/event/cohort/fine-grain output | metric, dimension, support records | `requested_grain`, `accepted_grain`, `unsupported_grain_refs`, `fallback_status`. |
| `time_boundary_guard` | every time-window claim | metric timezone policy | `timezone`, `date_basis`, `inclusive_start`, `exclusive_end`, `month_length_handling`. |

### Blocked And Degraded Semantic Query Records

Every blocked or degraded semantic query must have a visible path record.

| Field | Required | Trace source | Notes |
| --- | --- | --- | --- |
| `semantic_query_id` | yes | semantic query | Query affected by the limitation. |
| `path_id` | yes | Phase 2 disabled/degraded/blocked path | Shared with Answer Package limitation refs. |
| `path_status` | yes | Phase 2 path status | `degraded`, `blocked`, `skipped`, or `disabled`. |
| `reason_code` | yes | Phase 2 lint / backlog / permission | Example: `cumulative_value_misuse`, `raw_external_ingestion_out_of_scope`. |
| `business_evidence_state` | yes | support/backlog/factor ledger | Existing vocab. |
| `data_contract_state` | yes | support/backlog/factor ledger | Existing vocab. |
| `evidence_type` | yes | support record | Existing vocab. |
| `strength` | yes | support record | Existing vocab. |
| `wording_limit` | yes | support record | Existing vocab. |
| `backlog_refs` | yes when missing/out-of-scope | backlog | Required for known gaps. |
| `limitation_refs` | yes when permission/out-of-scope | factor ledger limitations | Required for permission or raw external boundary. |
| `requested_vs_accepted_grain` | yes when grain changed | dimension/support records | Prevents hidden unsupported grain. |
| `answer_package_visibility` | yes | Phase 2 path record | First screen when material to main answer. |
| `verifier_required_action` | yes | answer_verify card | `block_claim`, `degrade_claim`, `require_limitation`, or `repair_answer`. |

## Evidence Envelope Contract

Every capability result that can support, limit, or block an Answer Package claim must enter through an evidence envelope. The envelope carries common BI boundary fields; capability-specific content stays in a typed payload.

`evidence_envelope`:

| Field | Required | Trace source | Notes |
| --- | --- | --- | --- |
| `evidence_ref` | yes | evidence layer | Stable ref used by claim groups, visual blocks, verifier, eval. |
| `run_id` | yes | Phase 2 accepted graph | Links evidence to run. |
| `accepted_graph_id` | yes | Phase 2 accepted graph | Links evidence to accepted graph version. |
| `accepted_graph_node_id` | yes | Phase 2 node | Capability node that produced or verified evidence. |
| `semantic_query_id` | yes when query-backed | semantic query | Required for query-backed analytical evidence. |
| `capability` | yes | capability card | One of the eight capability ids. |
| `support_id` | yes when available | capability support | Required when evidence maps to support matrix. |
| `factor_group_id` | yes when available | factor ledger | Required for factor-specific limits. |
| `typed_payload` | yes | capability card | Payload name, such as `pattern_scan_result`, `formula_decompose_result`, `event_evidence_result`. |
| `target_claim` | yes | accepted graph / Answer Package constraints | Business claim this evidence can support or limit. |
| `claim_type` | yes | support record / eval allowed claims | Must match support or allowed claim. |
| `metric` | yes | metric contract | Usually `paid_amount` in current baseline. |
| `scope` | yes | semantic query | Scope supported by this evidence. |
| `grain` | yes | semantic query | Evidence grain and any accepted fallback grain. |
| `baseline` | when comparative | semantic query | Baseline used by this evidence. |
| `evidence_type` | yes | support record / capability output | Existing vocab only. |
| `strength` | yes | support record / evidence reducer | Existing vocab only. |
| `wording_limit` | yes | support record / verifier policy | Existing vocab only. |
| `supported_claims` | yes | support record / capability card | Statements this evidence can support. |
| `disallowed_claims` | yes | contract / capability card | Examples: causal wording from candidate mechanism, raw-id output. |
| `contract_version_pins` | yes | Phase 2 pins | Exact contract artifacts used. |
| `current_data_snapshot` | yes | semantic query | Snapshot, coverage, freshness, artifact policy. |
| `numeric_reconciliation` | when numeric | metric/capability cards | Fields below. |
| `quality_handoff` | yes | data quality handoff | Quality refs, flags, pending or destructive checks. |
| `permission_handoff` | yes | permission handoff | Visible grain and blocked fields. |
| `limitations` | yes | path records / backlog / support | Backlog refs, limitation refs, unsupported grains, freshness, quality limits. |
| `disabled_degraded_blocked_path_refs` | yes | Phase 2 path records | Material path refs that must flow into Answer Package. |
| `verifier_handoff` | yes | answer_verify card / eval | Required checks and failure actions. |
| `answer_package_refs` | yes when assigned | Answer Package constraints | Claim groups and visual blocks that consume this evidence. |
| `created_from` | yes | semantic/evidence layer | `new_execution`, `reused_result_ref`, `quality_review`, or `verifier_result`. |

Envelope fields may reference internal query or result artifacts in implementation, but this design does not define raw SQL text, final query ids, physical table names, or persistence columns.

### Typed Payload Names

Phase 3 must keep typed payload names aligned with capability cards:

| Capability | Typed payload | Required envelope focus |
| --- | --- | --- |
| `pattern_scan` | `pattern_scan_result` | Tested windows, recurrence, effect size, stability, exceptions, quality flags. |
| `formula_decompose` | `formula_decompose_result` | Formula path, component contribution, residual, reconciliation, missing components. |
| `segment_bridge` | `segment_bridge_result` | Segment contribution, mix shift, coverage, sparse warnings, permission blocks. |
| `joint_attribution` | `joint_attribution_result` | Candidate ranking, selected combinations, residual reduction, stability, sparse/permission limits. |
| `event_evidence` | `event_evidence_result` | Event identity, tested windows, temporal alignment, affected scope, control/baseline status. |
| `outlier_scan` | `outlier_scan_result` | Outlier periods, segments, components, candidate explanations, ruled-out paths. |
| `data_quality_check` | `data_quality_result` | Contract coverage, freshness, completeness, time semantics, permission, blocked paths. |
| `answer_verify` | `answer_verify_result` | Verified, degraded, blocked claims, missing refs, wording violations, visual issues. |

### Numeric Reconciliation

`numeric_reconciliation`:

| Field | Required | Trace source | Notes |
| --- | --- | --- | --- |
| `applies` | yes | capability card | True for formula, bridge, attribution, comparison, pattern quantification when numeric. |
| `metric_id` | yes when applies | metric contract | Prevents cross-metric number reuse. |
| `formula_path_id` | when formula-backed | metric decomposition paths | Must be declared in metric contract. |
| `target_window_ref` | yes when applies | semantic query | Target window used by the number. |
| `baseline_window_ref` | when comparative | semantic query | Baseline window used by the number. |
| `component_coverage_status` | when formula/bridge | formula card / metric contract | Covered, partial, missing, or blocked component refs. |
| `residual_policy` | when formula/bridge | metric decomposition path | Existing metric residual policy. |
| `residual_status` | when formula/bridge | capability output | Required before quantified wording. |
| `materiality_status` | when movement/anomaly/pattern | metric materiality policy | Threshold id and comparable-window status. |
| `reconciliation_status` | yes when applies | capability output/verifier | `passed`, `degraded`, `failed`, `not_applicable`. |
| `limitation_refs` | when degraded/failed | backlog/path record | Required when residual, component, grain, permission, or quality limits affect wording. |

Formula contribution can support `accounting_contribution`; it cannot be written as causal impact. Segment contribution cannot be generalized beyond its evidence scope.

### Limitations

`limitations[]`:

| Field | Required | Trace source | Notes |
| --- | --- | --- | --- |
| `limitation_id` | yes | evidence layer or factor ledger | Stable within run. |
| `limitation_type` | yes | existing state vocab | `missing_contract`, `permission_limited`, `unsupported_grain`, `out_of_scope_for_now`, `insufficient`, quality/freshness reason. |
| `backlog_refs` | when missing/out-of-scope | backlog | Required for known contract gaps. |
| `limitation_refs` | when permission/out-of-scope | factor ledger | Example: `pii_dimension_output_limit`, `raw_external_ingestion_phase1_scope`. |
| `affected_scope` | yes | semantic query | Prevents hidden scope broadening. |
| `affected_claim_type` | yes | support/eval | Claim type limited or blocked. |
| `wording_effect` | yes | support record/verifier | How wording limit changes. |
| `answer_visibility` | yes | Phase 2 path record | First screen when limitation affects main conclusion. |
| `upgrade_path` | when known | backlog/factor ledger | Owner-facing fix path. |

## Evidence Ledger Concept

The evidence ledger is a run artifact concept for audit, eval comparison, verifier handoff, and Answer Package generation. It is not a final table design.

### Run-Level Ledger Header

| Field | Required | Trace source | Notes |
| --- | --- | --- | --- |
| `run_id` | yes | orchestration | Run boundary. |
| `thread_id` | yes when available | orchestration | Enables thread-scoped result reuse validation. |
| `accepted_graph_id` | yes | Phase 2 accepted graph | Accepted graph boundary. |
| `graph_version` | yes | Phase 2 accepted graph | Version after repairs/clarifications. |
| `contract_version_pins` | yes | Phase 2 pins | All contracts used by semantic queries/evidence. |
| `current_data_snapshot` | yes | metric snapshot policy | Current-data basis for the run. |
| `semantic_query_refs` | yes | semantic layer | All semantic query ids created for the run. |
| `evidence_refs` | yes | evidence envelopes | All evidence refs produced or reused. |
| `disabled_degraded_blocked_path_refs` | yes | Phase 2 path records | Paths that constrain final answer. |
| `verifier_result_refs` | yes when verification ran | `answer_verify` envelopes | Claim and visual verification results. |
| `answer_package_ref` | yes when produced | Answer Package | Final artifact handoff. |

### Ledger Entry Concept Fields

| Field | Required | Trace source | Notes |
| --- | --- | --- | --- |
| `ledger_ref` | yes | evidence ledger | Stable run-scoped ledger reference. |
| `entry_kind` | yes | evidence layer | `semantic_query`, `evidence_envelope`, `quality_handoff`, `permission_handoff`, `path_record`, `verifier_result`, `answer_handoff`. |
| `accepted_graph_node_id` | when node-linked | Phase 2 node | Links evidence back to graph. |
| `semantic_query_id` | when query-linked | semantic query | Links evidence to semantic intent. |
| `evidence_ref` | when evidence-linked | evidence envelope | Claim/visual/verifier anchor. |
| `capability` | when capability-linked | capability card | One of eight ids. |
| `support_id` | when available | capability support | Support state trace. |
| `factor_group_id` | when available | factor ledger | Factor trace. |
| `contract_version_pins` | yes | Phase 2 pins | Contract trace for this entry. |
| `numeric_reconciliation_status` | when numeric | evidence envelope | `passed`, `degraded`, `failed`, `not_applicable`. |
| `data_quality_status` | yes | data quality handoff | Passed, degraded, blocked, pending, or not applicable. |
| `permission_status` | yes | permission handoff | Visible grain and any blocked output path. |
| `path_status` | when path-linked | Phase 2 path records | Disabled, degraded, blocked, skipped. |
| `answer_package_handoff_status` | yes | Answer Package constraints | `ready`, `requires_limitation`, `requires_repair`, or `blocked`. |

Implementation may persist these concepts in Postgres later. This document only fixes the conceptual fields and trace rules.

## Answer Package And Verifier Handoff

The semantic/evidence layer hands only constrained, evidence-bound material to Answer Package synthesis. The synthesizer can draft text inside these constraints; verifier remains the final gate.

### Claim Group Consumption

`claim_group_evidence_handoff`:

| Field | Required | Trace source | Notes |
| --- | --- | --- | --- |
| `claim_group_id` | yes | Phase 2 Answer Package constraints | Target claim group. |
| `target_claim` | yes | accepted graph | Business claim. |
| `metric` | yes | evidence envelope | Metric supported by evidence. |
| `scope` | yes | evidence envelope | Must match or be narrower than claim scope. |
| `baseline` | when comparative | semantic query/evidence envelope | Required for comparative claims. |
| `required_evidence_refs` | yes | evidence ledger | Empty refs block final claim. |
| `primary_evidence_ref` | yes when claim publishes | evidence envelope | Main evidence source for wording. |
| `supporting_evidence_refs` | yes | evidence envelope | Adjacent quality, event, pattern, segment, or formula evidence. |
| `quality_evidence_refs` | yes for first-screen/strong claims | data_quality_check envelope | Quality refs that must be visible or inherited. |
| `disabled_degraded_blocked_path_refs` | yes when material | Phase 2 path records | Paths that constrain this claim. |
| `allowed_evidence_type` | yes | support/eval | Existing vocab. |
| `allowed_strength` | yes | support/eval | Existing vocab. |
| `wording_limit` | yes | support/eval | Existing vocab. |
| `numeric_reconciliation_status` | when numeric | evidence envelope | Quantified wording requires passed or explicitly limited reconciliation. |
| `limitation_refs` | yes when limited | envelope/path records | Must surface when material. |
| `verifier_required_checks` | yes | answer_verify card/eval | Exact checks to run. |

### Number, Scope, Baseline, And Visual Binding

| Artifact | Required binding | Failure action |
| --- | --- | --- |
| Numeric statement | `evidence_ref`, metric, scope, target window, baseline when comparative, reconciliation status | Missing or mismatched refs block or repair the claim. |
| Scope statement | `evidence_ref`, accepted scope, requested scope, accepted grain, limitation refs | Scope broadening blocks or downgrades. |
| Baseline statement | `semantic_query_id`, baseline type/window/comparability, clarification or inference source | Wrong/missing baseline blocks comparative claim. |
| Visual block | visual block id, block type, supported claim, evidence refs, scope, limitations, wording limit | Visual block is repaired or blocked when it implies stronger evidence. |
| Limitation card | path refs, backlog refs, limitation refs, affected claim groups, upgrade path | Material gap hidden from answer fails verification. |

### Gap And Block Routing

| State or reason | Answer Package route | Verifier behavior |
| --- | --- | --- |
| `missing_contract` with backlog ref | limitation or blocked claim path, depending on materiality | Claim can degrade; gap must name backlog/upgrade path. |
| `missing_contract` without backlog ref | blocked path | Compile or final claim blocks through `missing_contract_without_backlog_ref`. |
| `permission_limited` | visible permission limitation or blocked output path | Raw id, raw IP, raw device id, and individual-user claims block. |
| `unsupported_grain` with fallback | limitation with requested and accepted grain | Claim wording must stay at accepted grain. |
| `unsupported_grain` without safe fallback | blocked path | Dependent claim blocks. |
| `out_of_scope_for_now` | blocked path or out-of-scope limitation | Raw external crawling cannot support launch claims. |
| Weak/sparse/low-materiality evidence | degraded claim | Strength and wording limit lowered. |
| Destructive data quality issue | blocked dependent claim | Main conclusion cannot publish when issue invalidates evidence. |

### Verifier Failure Definitions

| Failure | Detect when | Failure attribution |
| --- | --- | --- |
| `overclaim` | Claim wording exceeds `wording_limit`, causal wording appears for `candidate_mechanism`, or contribution is written as cause. | `over_strong_weak_evidence`, usually `answer_verifier` or `answer_synthesizer`. |
| `hidden_data_gap` | Missing contract, unsupported grain, permission limit, freshness issue, or degraded path is absent from material claim or limitation. | `hidden_data_gap`, usually `graph_compiler`, `semantic_compiler`, `evidence_reducer`, or `answer_verifier`. |
| `wrong_baseline` | Comparative claim omits baseline, uses incompatible baseline, hides baseline disagreement, or ignores user-specified baseline. | `wrong_baseline`, usually `LLM_reasoner`, `graph_compiler`, `semantic_compiler`, or `answer_verifier`. |
| `permission_leak` | Answer or visual block exposes raw user id, raw IP, raw device id, individual-user claim, or forbidden fine-grain sensitive output. | `permission_leak`, `permission_policy` and `answer_verifier`. |
| `misleading_visualization` | Visual block implies stronger evidence, broader scope, or unsupported ranking/causality. | `misleading_visualization`, `visualization_planner` and `answer_verifier`. |
| `unsupported_main_conclusion` | Main claim depends on blocked, insufficient, out-of-scope, or unreferenced evidence path. | `unsupported_main_conclusion`, often `evidence_reducer` or `answer_verifier`. |

## Semantic/Evidence Acceptance Cases

These cases extend the existing eight launch eval packages. They do not require SQL or gold business answers.

| Case | Coverage | Semantic query expectation | Evidence envelope expectation | Verifier/eval expectation |
| --- | --- | --- | --- | --- |
| `LE-001` month-phase pattern | pattern, event, data quality, cumulative guard | `pattern_scan` query binds `paid_amount`, full sample, `month_phase`, same-month phase baseline, Africa/Lagos time boundary, current snapshot, materiality and cumulative-value guards. Payday event query binds `aggregate_payday_window_25_30`. | Pattern envelope uses `pattern_month_phase_paid_amount`, `statistical_association`, `insufficient`, `insufficient` until `timezone_policy` gap clears. Payday envelope uses `event_pattern_payday_dimension`, `candidate_mechanism`, `low`, `candidate`. | Block cumulative month-to-date evidence. Pattern, exception, and candidate mechanism claims require evidence refs and visible degraded paths. |
| `LE-002` weekly operating change | formula, segment, baseline, operation gaps | Formula query binds declared `paid_amount` decomposition path, last complete week, comparable prior week or clarification. Segment query binds supported channel/payment grain. | Formula envelope uses `formula_paid_amount_change_order_chain`, `accounting_contribution`, `medium`, `quantified`, with reconciliation. Growth ops envelope stays insufficient through backlog refs. | Contribution wording cannot become causal wording. Growth-operation gaps must surface. |
| `LE-003` recharge activity impact | event, missing contracts, control/exposure | Event semantic query attempts business object binding to product/operation event; missing event identity/exposure/control creates degraded or blocked query record. | Envelope uses `event_business_object_product_operation`, `candidate_mechanism`, `insufficient`, `insufficient`, with `product_operation_event_contracts` and `exposure_control_contracts`. | Confirmed net impact blocks. Missing event/control/exposure limits appear in claim and visual boundary. |
| `LE-004` revenue health | formula, anomaly, data quality, materiality | Health queries bind 30-day rolling/month proxy, historical/prior comparable baseline, materiality policy, freshness and permission checks. | Formula and anomaly envelopes use existing support ids with `insufficient` strength and visible component/payment/event gaps. Data quality refs are required for health judgment. | Data risk must be separate from business risk. Unsupported component or incident claims cannot drive main health conclusion. |
| `LE-005` channel/payment attribution | segment, joint attribution, permission | Segment semantic query binds channel/payment_method supported grains. Geo/device query records permission-limited or aggregate-only handoff. | Supported segment envelope uses `segment_attribution_payment_method`, `accounting_contribution`, `medium`, `quantified`. Geo/device envelope uses `joint_attribution_geo_device_permission_limited`, `insufficient`, `insufficient`, `blocked`. | Raw user id, IP, and device id never appear. Local/fine-grain permission limits are visible. |
| `LE-006` anomaly and black-swan | anomaly, event context, raw external block | Outlier query requires anomaly definition/baseline and quality-first guard. Event query can use reviewed external context only. Raw web/news/forum/media query creates blocked semantic query record. | Context envelopes use `outlier_black_swan_external_context` or `event_black_swan_external_context`, `candidate_mechanism`, `low`, `context`. Raw ingestion envelope/path uses `event_black_swan_raw_external_ingestion_scope`, `insufficient`, `insufficient`, `blocked`. | Black-swan wording stays candidate/context. Raw external ingestion cannot support any final claim or visual block. |
| `LE-007` custom first-ten-days baseline | baseline, formula, pattern, cumulative guard | User baseline query binds June 1-10 target and May 1-10 baseline, explicit inclusivity/timezone, current snapshot, cumulative-value guard. | Pattern envelope uses `pattern_custom_baseline_paid_amount`, `statistical_association`, `insufficient`, `insufficient`. Formula envelope uses `formula_custom_baseline_order_chain`, `accounting_contribution`, `insufficient`, `insufficient`, with reconciliation limitation. | Wrong or hidden baseline fails. Cumulative misuse repairs or blocks before evidence publishes. |
| `LE-008` evidence trust review | data quality, evidence refs, permission | Query can compile against accepted graph/evidence refs without new analytical query. It must bind referenced claim group, contracts, freshness, permission, and missing paths. | Data quality envelopes use `dq_contract_coverage_review`, `dq_materiality_threshold_policy`, and `dq_permission_sensitive_identifiers` with existing states. | Every claim has refs. Missing contracts, permission limits, and blocked paths are named without promoting gaps. |

## Minimum Phase 3 Verification

Before implementation planning, these checks must pass:

- `ruby tools/evals/validate-semantic-compiler-fixtures.rb`
- `ruby tools/evals/validate-semantic-compiler-dry-run.rb`
- `ruby tools/contracts/validate-contracts.rb`
- `ruby tools/evals/validate-launch-evals.rb`
- `ruby tools/runtime/load-contracts-to-postgres.rb`

The semantic compiler skeleton is detailed in `docs/phase-3-semantic-compiler-skeleton.md`; dry-run mapping is detailed in `docs/phase-3-semantic-compiler-dry-run.md`; representative fixtures live in `evals/semantic-compiler/semantic-compiler-fixtures.yaml`. The Postgres mirror remains scoped to the 21 contract YAML files under `contracts/`. Eval packages, semantic compiler fixtures, dry-run expectations, and Phase 3 docs remain design/eval artifacts outside the contract mirror.

## Pending Human Decisions

Phase 3 design can proceed with degrade/block records for these. It must not promote them to supported paths:

- final production runtime schemas, service APIs, SQL compilation details, ClickHouse/Postgres physical design
- raw user id, IP, device id, individual-user output, sparse-cell thresholds, masking and role policy enforcement
- refund, reversal, chargeback, cancellation, net revenue, adjustment-risk contracts
- complete source timestamp mapping, timezone validation, current-data watermark policy beyond the accepted snapshot
- component metric contracts for every formula path and owner-reviewed dashboard field meanings
- campaign spend, bid, creative CTR/CVR, SEO/GEO, referral, exposure/control, product activity, server/Grafana/payment incident sources
- gameplay paid_amount linkage, gameplay exposure/click, gameplay paid rate, gameplay payment amount/frequency/single-payment amount
- raw external crawling or AnySearch-like connector contracts
- future tuning of materiality thresholds when new data or business feedback requires a new policy version
