# Phase 3 Semantic Compiler Skeleton

Status: implementation-ready contract skeleton from Phase 1 contracts, Phase 2 graph compiler prep, Phase 3 semantic/evidence prep, and launch eval expectation packages.

Scope: field-level handoff design, semantic compiler fixtures, and minimal validation. This document does not define runtime execution, SQL, final tables, ClickHouse/Postgres physical design, LangGraph runtime, frontend UI, or new business metric semantics.

## Purpose

The semantic compiler is the contract handoff between a Phase 2 accepted graph and later capability execution. It turns accepted graph nodes into semantic query request records, records blocked or degraded request records when a path cannot honestly execute, and prepares response/evidence handoff fields for evidence envelopes and Answer Package verification.

The compiler must preserve WAJE BI truth boundaries:

- Phase 1 contracts define metric, dimension, event, assumption, factor, capability, evidence, wording, permission, and backlog semantics.
- Phase 2 accepted graph defines run intent, accepted nodes, graph mutations, disabled/degraded/blocked paths, current-data snapshot, and Answer Package constraints.
- Phase 3 semantic/evidence contracts define semantic query, evidence envelope, evidence ledger, and verifier handoff fields.
- Launch eval packages define representative acceptance expectations and failure labels.
- LLM output, LangGraph node state, recipes, and process UI are trace/display inputs only; they do not define BI truth.

## Component Boundary

| Component | Owns | Semantic compiler handoff |
| --- | --- | --- |
| Graph compiler | Accepted graph, node status, mutation log, disabled/degraded/blocked paths, clarification outcome, Answer Package constraints | Supplies the source graph for semantic query request generation. |
| Semantic compiler | Contract-level semantic query request/response skeleton and blocked/degraded query records | Produces request/response records and refs for evidence envelopes. |
| Capability execution | Runtime API, SQL compilation, validated query execution, typed payload values | Consumes only accepted semantic requests. No runtime execution is designed here. |
| Evidence reducer | Capability-specific evidence summarization, conflict handling, strength candidates | Must preserve envelope constraints and cannot raise strength beyond contracts. |
| Evidence ledger | Run-level refs linking accepted graph nodes, semantic queries, envelopes, path records, and verifier results | Conceptual fields only in Phase 3. No final table design here. |
| Answer Package and verifier | Claim groups, number/scope/baseline/visual binding, final failure decisions | Consumes evidence refs, path refs, limitations, and verifier handoff fields. |

## Accepted Graph Node To Request Mapping

Every evidence-producing or limitation-producing accepted graph node maps to one semantic query request unless the graph compiler already skipped the path with no material Answer Package effect.

| Semantic query request field | Phase 2 accepted graph source | Phase 1 contract/eval source | Rule |
| --- | --- | --- | --- |
| `semantic_query_id` | Semantic compiler local id | Eval fixture ref when present | Stable run-scoped id, referenced by responses and evidence envelopes. |
| `run_id` | Accepted graph header | None | Same run boundary as accepted graph. |
| `accepted_graph_id` | Accepted graph header | None | Required for every request. |
| `accepted_graph_node_id` | `nodes[].node_id` | None | Prevents orphan evidence. |
| `graph_version` | Accepted graph header | None | Must advance after repair or clarification. |
| `question_family` | `nodes[].question_family` | `capability-support.yaml` and launch package | Must be one of eight launch families. |
| `capability` | `nodes[].capability` | Capability card | Must match support record when `support_id` exists. |
| `support_id` | `nodes[].support_id` | `capability-support.yaml` | Required when a support record exists for accepted, degraded, or blocked paths. |
| `factor_group_id` | `nodes[].factor_group_id` | `factor-ledger.yaml` | Required when claim wording depends on factor support state. |
| `semantic_query_intent.target_claim` | `nodes[].target_claim` | Launch eval allowed claim | Must stay inside support record `claim_type`. |
| `metric_binding` | Intent binding metric | `paid-amount.metric.yaml` | Invalid metric blocks. Current baseline binds `paid_amount`. |
| `scope_binding` | Intent binding scope/filters/object | Metric, dimension, event contracts | Evidence scope can be narrower than claim scope; it cannot be broader. |
| `grain_binding` | Node params and accepted scope | Metric/dimension/support grains | Unsupported grain needs fallback path or block. |
| `time_window_binding` | Intent binding time range | Metric time semantics and snapshot policy | Requires timezone, date basis, inclusivity, and current-data basis. |
| `baseline_binding` | Intent binding baseline or clarification outcome | Metric baseline compatibility and eval expectations | Comparative claims block or repair when baseline is missing or incompatible. |
| `dimension_bindings` | Node params | `dimensions.yaml` and factor ledger | Permission and sparse-cell rules travel with each sensitive dimension. |
| `event_bindings` | Node params and named business object | `events.yaml`, assumptions, backlog | Missing event identity/control/exposure creates degraded or blocked path records. |
| `contract_version_pins_required` | Accepted graph pins | Contract artifacts | Missing deterministic pins can be auto-added; unreadable or invalid pins block. |
| `current_data_snapshot_binding` | Accepted graph snapshot | Metric source/snapshot policy | Required before execution. Future data updates create new run/artifact versions. |
| `permission_handoff` | Policy hooks and path records | Dimensions, factor limitations, backlog | Raw user id, IP, and device id output blocks. Aggregate fallback needs visible limits. |
| `data_quality_handoff` | Quality nodes and mutation log | Metric guards and `data_quality_check` card | Quality can block, degrade, or constrain wording. It cannot strengthen unrelated evidence. |
| `guard_handoff` | Lint rules and mutation log | Metric guards and capability cards | Materiality, freshness, cumulative value, grain, time boundary, and reconciliation guards must be explicit. |
| `semantic_query_status` | Node/path status | Compiler outcome matrix | Allowed values are fixture-validated. Blocked/degraded statuses need path refs. |
| `disabled_degraded_blocked_path_refs` | Phase 2 path records | Backlog/limitation refs | Required for material gaps, permission limits, unsupported grain, or out-of-scope paths. |
| `expected_evidence_contract` | Node expected evidence | Capability card and support record | Fixes typed payload, evidence type, strength, and wording limit before execution. |
| `answer_package_handoff` | Answer Package constraints | Launch eval verifier checks | Claim groups and visual blocks must consume evidence refs and visible limitations. |

## Semantic Query Request Skeleton

The request skeleton below is a design contract. Later implementation can serialize it differently, but fields with BI meaning must preserve these boundaries.

```yaml
semantic_query_request:
  semantic_query_id: sq_run_scoped_id
  run_id: run_id
  accepted_graph_id: accepted_graph_id
  accepted_graph_node_id: accepted_graph_node_id
  graph_version: graph_version
  question_family: pattern_explanation
  capability: pattern_scan
  support_id: pattern_month_phase_paid_amount
  factor_group_id: paid_amount_metric_source
  semantic_query_status: accepted | degraded | blocked | skipped | repair_requested | accepted_with_permission_limit
  semantic_query_intent:
    target_claim: recurring_pattern_existence
    claim_type: recurring_pattern_existence
    analysis_role: primary_evidence | supporting_evidence | quality_guard | candidate_context | limitation_review | verifier_input
    clarification_outcome_ref: optional
    launch_eval_case_refs: []
  bindings:
    metric:
      metric_id: paid_amount
      formula_path_id: optional
      contract_refs: []
    scope:
      requested: requested_business_scope
      accepted: accepted_business_scope
      filters: []
    grain:
      requested: requested_grain
      accepted: accepted_or_fallback_grain
      unsupported_grain_refs: []
    time_window:
      target_window_ref: target_window_ref
      timezone: Africa/Lagos
      date_basis: local_business_day
      boundary: "[00:00, 24:00)"
    baseline:
      baseline_type: optional_for_comparative_claims
      baseline_window_ref: optional_for_comparative_claims
      source: user_selected | system_inferred | accepted_graph_refs | targeted_repair_candidate | blocked_path
    dimensions: []
    events: []
  contract_version_pins_required: []
  current_data_snapshot_binding:
    source_contract: contracts/sources/paid-order-detail.source.yaml
    exported_on: "2026-07-04"
    coverage: complete_2026_january_through_june_dataset
    artifact_policy: do_not_rewrite_prior_answers_after_data_updates
  permission_handoff:
    requested_output_grain: requested_output_grain
    accepted_visible_grain: accepted_visible_grain
    blocked_fields: []
    permission_status: contract_backed | permission_limited | unsupported_grain | out_of_scope_for_now
    limitation_refs: []
    answer_visibility: first_screen | limitations | follow_up | internal_only
  data_quality_handoff:
    required_check_ids: []
    pending_quality_checks: []
    quality_flags: []
    backlog_refs: []
  guard_handoff:
    materiality_guard: {}
    freshness_guard: {}
    cumulative_value_guard: {}
    formula_reconciliation_guard: {}
    grain_guard: {}
    time_boundary_guard: {}
  disabled_degraded_blocked_path_refs: []
  expected_evidence_contract:
    typed_payload: pattern_scan_result
    evidence_type: statistical_association
    strength: insufficient
    wording_limit: insufficient
```

## Semantic Query Response Skeleton

The response skeleton is still pre-runtime. It records the semantic plan and handoff refs, not result rows.

| Response field | Required | Source | Rule |
| --- | --- | --- | --- |
| `semantic_query_id` | yes | Request | Must match one request. |
| `semantic_plan_summary` | yes | Semantic compiler | Business-readable plan summary with no raw SQL. |
| `metric_refs` | yes | Request metric binding | Must include `paid_amount` for the current baseline. |
| `source_refs` | yes | Contract pins and bindings | Contract/source paths or blocked source markers. |
| `result_shape` | yes | Capability card and support record | Describes expected typed payload shape, not data rows. |
| `numeric_reconciliation_status` | yes | Guard handoff / expected evidence | `passed`, `degraded`, `failed`, or `not_applicable`. |
| `quality_flags` | yes | Data quality handoff | Must include pending or degraded quality flags. |
| `permission_status` | yes | Permission handoff | Uses existing data contract state vocab. |
| `evidence_handoff_refs` | yes | Evidence envelope refs | Response refs must resolve to envelopes. |

## Blocked And Degraded Path Rules

Semantic compiler can emit a blocked/degraded query record without runtime execution. That record must have:

- `reason_code` from compiler lint, backlog, permission, or support state.
- `business_reason` suitable for Answer Package limitations.
- `business_evidence_state`, `data_contract_state`, `evidence_type`, `strength`, and `wording_limit` from existing vocab.
- `backlog_refs` for missing contracts or out-of-scope items.
- `limitation_refs` for permission or out-of-scope review limitations.
- `requested_vs_accepted_grain` when grain changes.
- `verifier_required_action` such as `block_claim`, `degrade_claim`, `require_limitation`, or `repair_answer`.

Routing rules:

| State | Semantic compiler action | Answer Package/verifier effect |
| --- | --- | --- |
| `missing_contract` with backlog ref | Degrade or block according to materiality and claim role | Visible limitation or blocked claim path. |
| `permission_limited` | Block raw output; allow aggregate only when reviewed policy allows | Permission limitation must be visible; leak fails verification. |
| `unsupported_grain` with fallback | Degrade to accepted grain | Scope/grain wording must name the fallback. |
| `unsupported_grain` without fallback | Block dependent query and claim path | Final claim cannot publish from that path. |
| `out_of_scope_for_now` | Block request before execution | No evidence ref from that path can support a final claim. |
| Weak evidence or sparse/comparable-window issue | Degrade strength/wording | Verifier blocks overclaim. |
| Cumulative-value misuse | Repair or block before evidence publishes | Wrong-baseline or numeric misuse fails verification. |

## Evidence Envelope Handoff

Every response that can support, limit, or block a claim hands at least one `evidence_ref` to an evidence envelope. The envelope must preserve:

- `evidence_ref`
- `semantic_query_id`
- `capability`
- `support_id`
- `typed_payload`
- `evidence_type`
- `strength`
- `wording_limit`
- `limitations`
- `numeric_reconciliation`
- `disabled_degraded_blocked_path_refs`
- `verifier_handoff`

Envelope typed payload names are taken from capability cards:

| Capability | Typed payload |
| --- | --- |
| `pattern_scan` | `pattern_scan_result` |
| `formula_decompose` | `formula_decompose_result` |
| `segment_bridge` | `segment_bridge_result` |
| `joint_attribution` | `joint_attribution_result` |
| `event_evidence` | `event_evidence_result` |
| `outlier_scan` | `outlier_scan_result` |
| `data_quality_check` | `data_quality_result` |
| `answer_verify` | `answer_verify_result` |

## Answer Package And Verifier Handoff

Claim groups consume evidence through refs, never through free text.

| Answer artifact | Binding requirement | Failure behavior |
| --- | --- | --- |
| Claim group | Required evidence refs, primary/supporting refs, scope, grain, limitations, wording limit | Missing or mismatched refs block or repair the claim. |
| Numeric statement | `evidence_ref`, metric, target window, baseline when comparative, reconciliation status | Missing reconciliation blocks quantified wording. |
| Scope statement | Evidence scope and accepted grain | Broader scope than evidence fails verification. |
| Baseline statement | Semantic query id, baseline type/window, comparability, source of assumption | Wrong or hidden baseline fails verification. |
| Visual block | Evidence refs, scope, limitations, wording limit | Misleading visual implication blocks or repairs the visual. |
| Limitation block | Path refs, backlog refs, limitation refs, owner-facing upgrade path | Hidden material gap fails verification. |

Verifier failure labels remain the existing launch taxonomy: `over_strong_weak_evidence`, `hidden_data_gap`, `wrong_baseline`, `permission_leak`, `misleading_visualization`, and `unsupported_main_conclusion`.

## Fixture Coverage

The implementation-ready fixtures live in `evals/semantic-compiler/semantic-compiler-fixtures.yaml`.

| Fixture | Launch case | Compiler outcome | Main coverage |
| --- | --- | --- | --- |
| `SC-001` | `LE-001` | `auto_repair` | Month-phase pattern, payday static assumption, timezone limitation, cumulative-value guard. |
| `SC-002` | `LE-002` | `accept` | Formula decomposition, payment-method segment bridge, numeric reconciliation. |
| `SC-003` | `LE-003` | `degrade` | Business object event evidence with missing event/exposure/control contracts. |
| `SC-004` | `LE-004` | `degrade` | Revenue health formula/anomaly paths with data-quality and component gaps. |
| `SC-005` | `LE-005` | `accept` | Supported segment attribution plus permission-limited and unsupported-grain geo/device path. |
| `SC-006` | `LE-006` | `block` | Raw external ingestion block plus context-only anomaly framing. |
| `SC-007` | `LE-007` | `targeted_repair` | Custom first-ten-days baseline, time boundary, cumulative-value, formula reconciliation repair. |
| `SC-008` | `LE-008` | `accept` | Evidence trust review, data quality, permission-sensitive identifiers. |

The fixture validator checks:

- YAML parse.
- `question_family`, `capability`, `compiler_outcome`, `evidence_type`, `strength`, and `wording_limit` enums.
- `support_id` to capability-support consistency.
- typed payload consistency with capability cards.
- backlog refs against `missing-contracts.yaml`.
- limitation refs against factor ledger review limitations.
- blocked/degraded/repair query records have path refs.
- required coverage for formula, pattern, event, segment, anomaly, data quality, permission, blocked path, static assumption, missing contract, unsupported grain, and raw external out-of-scope.

Run:

```bash
ruby tools/evals/validate-semantic-compiler-fixtures.rb
ruby tools/evals/validate-semantic-compiler-dry-run.rb
```

## Field Ownership

| Field group | Current source | Later runtime owner |
| --- | --- | --- |
| Metric identity, amount basis, formula paths, time policy, materiality | `paid-amount.metric.yaml` | Contract owner; runtime reads active version only. |
| Dimensions, supported grains, permission-sensitive fields | `dimensions.yaml`, factor ledger | Permission policy and capability execution enforce visibility. |
| Event/static assumption binding | `events.yaml`, `payday.assumption.yaml`, backlog | Event source contracts and capability execution. |
| Evidence type, strength, wording limit | `capability-support.yaml`, capability cards | Evidence reducer can lower strength; it cannot exceed support contract. |
| Snapshot/current-data basis | Metric contract and accepted graph snapshot | Runtime run state and future evidence ledger refs. |
| Missing contract/out-of-scope refs | `missing-contracts.yaml`, factor ledger limitations | Answer Package limitations and owner review workflow. |
| Query result ids, SQL, rows, runtime error details | Not in Phase 3 skeleton | Capability execution and runtime ledger in later phases. |

## Pending Owner Decisions

These decisions do not block this skeleton. The current fixtures route them through visible limitations, `pending_owner_review`, degraded paths, or blocked paths.

1. Permission policy: confirm role-level output policy, masking, sparse-cell thresholds, audit treatment, and exact aggregate fallback rules for raw user id, IP, and device id.
2. Africa/Lagos timestamp mapping: confirm which source timestamp drives business date for every source, source timezone assumptions, watermark behavior, and inclusive/exclusive boundaries beyond the accepted H1 snapshot.
3. Formula component contracts: confirm component metric contracts, source ownership, denominator semantics, residual policy, and launch-ready formula paths for decomposition beyond currently accepted support.

## Minimum Verification

The Phase 3 semantic compiler skeleton is ready for implementation planning only when these pass:

```bash
ruby tools/evals/validate-semantic-compiler-fixtures.rb
ruby tools/evals/validate-semantic-compiler-dry-run.rb
ruby tools/contracts/validate-contracts.rb
ruby tools/evals/validate-launch-evals.rb
ruby tools/runtime/load-contracts-to-postgres.rb
```
