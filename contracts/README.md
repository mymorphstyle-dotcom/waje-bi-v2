# WAJE BI v2 Contract Sources

This directory stores reviewable contract source files for WAJE BI v2. These files define business meaning, evidence boundaries, capability eligibility, and launch acceptance inputs before any runtime table design is finalized.

## Contract Boundary

- `contracts/ssot/` stores source snapshots and mechanical extracts from the factor SSOT.
- `contracts/source-templates/` stores source file templates and mechanical extracts used for contract review.
- `contracts/sources/` stores draft or accepted source contracts that bind real source fields to WAJE metric semantics.
- `contracts/metrics/` stores metric identity, formula paths, time semantics, supported grains, and known gaps.
- `contracts/dimensions/` stores dimension meanings, supported grains, fixed output-safety rules, and review status.
- `contracts/events/` stores event and business object definitions such as campaigns, product versions, holidays, payday, policy, weather, and external context.
- `contracts/assumptions/` stores reviewed static or semi-static assumptions with owner, source, valid window, refresh rule, and wording limit.
- `contracts/backlog/` stores missing contracts and upgrade paths.
- `contracts/ledger/` stores factor master records and factor-capability support records.
- `contracts/capabilities/` stores capability cards used by the graph compiler, LLM planner context, verifier, and launch acceptance.

Contract files are repo source artifacts. Runtime mirrors, database tables, API payload schemas, and query execution contracts are designed in later phases.

## Shared Vocabularies

### `business_evidence_state`

This state describes what kind of business conclusion a factor/capability/question intersection can support.

- `quantifiable`: can support a scoped numeric contribution, delta, decomposition, or ranking.
- `candidate_mechanism`: can support a plausible business mechanism with visible evidence limits.
- `contextual_evidence`: can provide relevant background or timing context, with limited explanatory weight.
- `insufficient`: current data, coverage, method, or stability cannot support the intended claim.
- `unsupported_grain`: evidence may support another grain, while the requested grain is not supported.
- `out_of_scope`: the factor is outside the current product scope.

### `data_contract_state`

This state describes why WAJE BI can or cannot execute an analysis path.

- `contract_backed`: backed by a maintained metric, dimension, event, or source pipeline contract.
- `evidence_linked`: connected to a source, manual event record, external evidence, or reviewed static assumption.
- `static_assumption`: represented as a reviewed assumption with owner, source, valid window, refresh rule, and wording limit.
- `missing_contract`: relevant to the business question, but no usable contract exists yet.
- `unsupported_grain`: contract exists for another grain, but the requested grain is not supported.
- `out_of_scope_for_now`: explicitly deferred from the current production baseline.

### `evidence_type`

This field describes the support behind a claim.

- `accounting_contribution`: formula, bridge, decomposition, or segment delta that can be recomputed.
- `statistical_association`: recurring pattern, correlation, lag relationship, or stability evidence.
- `candidate_mechanism`: business mechanism with temporal, structural, or contextual alignment.
- `causal_evidence`: controlled, counterfactual, treated/control, or intervention-style evidence.
- `insufficient`: path exists conceptually, but evidence does not support the claim.

### `strength`

This field describes how strong the evidence is for the current dataset, scope, and question.

- `high`: stable across the required scope, robust to required checks, and materially relevant.
- `medium`: directionally stable or materially relevant, with visible limits or weaker controls.
- `low`: weak, sparse, unstable, or only partially aligned; useful for follow-up context.
- `insufficient`: cannot support the proposed business wording.

### `wording_limit`

This field constrains final answer language.

- `quantified`: may state scoped numeric amount, rate, contribution, or ranking.
- `stable_pattern`: may state pattern existence, recurrence, magnitude, and exceptions.
- `candidate`: may state plausible mechanism or candidate explanation with evidence boundary.
- `context`: may mention background, timing overlap, or supporting context.
- `insufficient`: must state that evidence is not enough and name the limiting path.
- `blocked`: must state the restricted-output, contract, grain, or scope block.

## Claim Boundary Rules

- Every final claim must bind to a metric, scope, grain, baseline, evidence reference, `evidence_type`, `strength`, and `wording_limit`.
- `causal_evidence` requires an approved design or contract that supports causal wording.
- `static_assumption` can support candidate or context wording. It cannot lift a claim to confirmed cause by itself.
- `missing_contract` requires a backlog record before the path can appear as a known gap.
- Raw identifiers, restricted detail, and sparse aggregates are governed by one fixed output-safety policy for every normal user. They never create user-specific data capability tiers.
- Service-account or source-connection authorization may block source availability. It is an operational data-access condition and cannot be converted into a user-role choice.
- `unsupported_grain` must name the supported grain and the requested grain that is blocked.
- `out_of_scope_for_now` must include a business reason and launch impact.

## Review Rule

Business owners approve business meaning, claim boundary, and wording. Data/engineering owners approve contract state, grain support, source-connection access, fixed output-safety policy, and execution feasibility. Runtime execution must read accepted contract sources or their approved mirror, not ad hoc notes.
