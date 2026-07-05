# Phase 3 Semantic Evidence Prep

Status: design input from Phase 1 contracts, Phase 2 compiler prep, and launch eval package  
Scope: semantic query and evidence layer requirements only. No SQL, final table structure, runtime service API, or frontend implementation.

## Phase 3 Inputs

Semantic query and evidence design must consume:

- metric contract: metric identity, formula paths, time semantics, supported grains, baseline compatibility, quality guards, materiality policy
- dimension contracts: business meaning, supported grains, permission-limited fields, known gaps
- event contracts: event identity, event window, source status, affected scope, unsupported grains
- static assumptions: owner, source, valid window, refresh rule, wording limit
- missing-contract backlog: gap id, affected factor groups, capabilities, question families, launch impact, upgrade path
- factor ledger: factor group, data contract state, business evidence state, allowed evidence types, allowed wording limits, known gaps
- capability support: support id, question family, capability, grain, claim type, evidence state, contract state, evidence type, strength, wording limit
- accepted graph: bound intent, nodes, params, dependencies, mutation log, disabled/degraded/blocked paths
- launch eval packages: representative expectation cases and failure labels

## Semantic Query Layer Responsibilities

The semantic query layer prepares executable semantic requests for later query execution. It must:

- bind metric, scope, grain, time range, baseline, filters, and dimension/event references to reviewed contracts
- reject raw SQL or physical schema requests from LLM context
- preserve contract version pins and current-data snapshot basis
- enforce supported grains and permission handoff before query execution
- distinguish metric windows, cumulative values, event windows, and baseline windows
- emit query intent artifacts that can be audited without exposing final SQL in design docs

Semantic query output draft:

| Field | Required | Notes |
| --- | --- | --- |
| `semantic_query_id` | yes | Stable id for evidence trace. |
| `accepted_graph_node_id` | yes | Origin node. |
| `metric` | yes | Contract-backed metric id. |
| `scope` | yes | Business scope and filters. |
| `grain` | yes | Requested and accepted grain. |
| `time_window` | yes | Timezone, inclusive/exclusive boundaries, date basis. |
| `baseline_window` | when comparative | Same boundary fields as target window. |
| `dimension_refs` | when segmented | Dimension contract refs and permission state. |
| `event_refs` | when event-linked | Event/assumption/source refs and wording limit. |
| `contract_versions` | yes | Pinned artifacts. |
| `current_data_snapshot` | yes | Snapshot and freshness basis. |
| `permission_handoff` | yes | Visible output grain, masked/blocked fields, sparse-cell rule refs. |
| `quality_guards` | yes | Metric and capability guards that must be checked. |

## Evidence Envelope

Every capability result must enter the Answer Package through an evidence envelope.

| Field | Required | Notes |
| --- | --- | --- |
| `evidence_ref` | yes | Stable ref for verifier and claim groups. |
| `semantic_query_id` | yes when query-backed | Links to semantic query intent. |
| `capability` | yes | One of the eight capability ids. |
| `support_id` | yes when available | Capability support record. |
| `typed_payload` | yes | Capability-specific payload name. |
| `metric` | yes | Target metric. |
| `scope` | yes | Evidence scope. |
| `grain` | yes | Evidence grain. |
| `baseline` | when comparative | Baseline used. |
| `evidence_type` | yes | Phase 1 vocab. |
| `strength` | yes | Phase 1 vocab. |
| `wording_limit` | yes | Phase 1 vocab. |
| `contract_versions` | yes | Pinned contracts. |
| `current_data_snapshot` | yes | Snapshot and freshness basis. |
| `limitations` | yes | Backlog, permission, unsupported grain, quality limits. |
| `numeric_reconciliation` | when numeric | Reconciliation status and residual boundary. |
| `quality_handoff` | yes | Data quality evidence refs or pending checks. |
| `permission_handoff` | yes | Output permission decision. |

## Evidence Ledger

The evidence ledger is a run artifact concept, not a final table design. It records:

- evidence refs produced by each accepted graph node
- semantic query ids and contract pins
- numeric reconciliation status for formula, bridge, attribution, and comparison claims
- degraded, blocked, skipped, and permission-limited path refs
- verifier status and required answer repairs

It supports audit, eval comparison, and Answer Package generation. Final persistence shape waits for Phase 3/4 implementation design.

## Numeric Reconciliation

Numeric claim groups require:

- metric identity and formula path from contract
- target and baseline windows with matching semantics
- component coverage
- residual and reconciliation status
- materiality threshold when used for pattern/anomaly/health wording
- limitation if a component contract, grain, or permission path is missing

Formula contribution cannot be written as causal impact. Segment contribution cannot be globalized beyond its scope.

## Data Quality Handoff

Semantic evidence must carry data quality results or required checks for:

- metric identity
- completeness and duplicate policy
- source snapshot and freshness
- timezone and date boundary
- cumulative-value guard
- materiality threshold applicability
- permission and sparse-cell limits
- missing contracts and unsupported grains

Destructive quality issues block dependent claim paths. Local limitations degrade the affected claim groups.

## Permission Handoff

Permission handoff must include:

- requested output grain
- accepted visible grain
- blocked fields
- masking or aggregation requirement
- sparse-cell threshold requirement when available
- limitation refs such as `pii_dimension_output_limit`

Raw user id, IP, and device id output is blocked. Individual-user claims are blocked until a later reviewed policy allows them.

## Answer Package Handoff

The semantic/evidence layer hands these to Answer Package generation:

- accepted graph summary
- evidence envelopes by claim group
- semantic query refs without raw SQL
- disabled/degraded/blocked path records
- contract pins and current-data snapshot
- numeric reconciliation results
- data quality and permission handoff
- visual block constraints
- verifier required checks

The synthesizer can draft only inside these constraints. The verifier remains the final gate for evidence refs, numbers, scope, baseline, wording, degraded paths, and visual blocks.

## ClickHouse And Postgres Boundary

Postgres remains the reviewed contract mirror and run/evidence metadata boundary. ClickHouse is the analytical query execution boundary when Phase 3 starts implementation planning.

This prep does not define final table structures, physical fields, SQL, indexes, materialized views, or service APIs. Phase 3 implementation planning should derive those from the semantic query and evidence requirements above.
