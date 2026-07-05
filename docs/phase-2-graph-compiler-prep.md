# Phase 2 Graph Compiler Prep

Status: design draft from Phase 1 contracts  
Scope: graph compiler contract design only. Runtime implementation, SQL, final database tables, frontend protocol, and service API shapes stay out of this document.

## Design Inputs

Only these Phase 1 artifacts define Phase 2 BI semantics:

- `contracts/metrics/paid-amount.metric.yaml`
- `contracts/dimensions/dimensions.yaml`
- `contracts/events/events.yaml`
- `contracts/assumptions/payday.assumption.yaml`
- `contracts/backlog/missing-contracts.yaml`
- `contracts/ledger/factor-ledger.yaml`
- `contracts/ledger/capability-support.yaml`
- `contracts/ledger/ssot-node-reconciliation.yaml`
- `contracts/capabilities/*.yaml`
- `docs/prd.md`
- `docs/product-decisions.md`

Recipes, LangGraph nodes, prompts, and UI process events can propose or display workflow. They cannot define metric meaning, factor support, permission, evidence strength, or final claim wording.

## Phase 1 Vocabulary

Compiler output must reuse these vocabularies without inventing new claim tiers.

`data_contract_state`:

- `contract_backed`
- `evidence_linked`
- `static_assumption`
- `missing_contract`
- `permission_limited`
- `unsupported_grain`
- `out_of_scope_for_now`

`business_evidence_state`:

- `quantifiable`
- `candidate_mechanism`
- `contextual_evidence`
- `insufficient`
- `permission_limited`
- `unsupported_grain`
- `out_of_scope`

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

Phase 1 currently blocks `causal_evidence` for `paid_amount` claims unless later contracts add stronger evidence.

## Compiler Contract

### Input Package

Field-level draft, not a runtime wire schema.

| Field | Required | Source | Notes |
| --- | --- | --- | --- |
| `compile_id` | yes | local compiler | Stable id for this compile attempt. |
| `run_id` | yes | orchestration layer | Links compile artifacts to one investigation run. |
| `question_family_candidates` | yes | intent binding | One or more Phase 1 question families. |
| `user_intent` | yes | LLM + local intent binding | Original question, target metric, scope, time range, baseline, requested grain, named business objects, filters, user hypotheses. |
| `clarification_state` | yes | question tool / local policy | Existing outcome or empty state. Low-risk inferred defaults must appear here as `system_inferred`. |
| `candidate_graph` | yes | LLM | Proposed capability nodes with no execution authority until compiler accepts them. |
| `contract_bundle` | yes | WAJE contracts | Metric, dimension, event, assumption, backlog, factor ledger, capability support, capability cards, PRD decision refs. |
| `policy_context` | yes | local policy | Permission, budget, materiality, freshness, current-data basis, sparse-cell and launch thresholds. |
| `prior_run_context` | no | run store | Reusable evidence refs, previous accepted graph, verifier findings, disabled/degraded/blocked path records. |

`candidate_graph.nodes[]` minimum:

| Field | Required | Notes |
| --- | --- | --- |
| `node_id` | yes | Candidate-local id. |
| `capability` | yes | One of Phase 1 capability ids. |
| `params` | yes | Business parameters only; no raw SQL or physical schema. |
| `purpose` | yes | Business purpose for this node. |
| `target_claim` | yes | Claim the node is meant to support. |
| `scope` | yes | Metric scope, filters, grain, time window. |
| `depends_on` | yes | Other candidate node ids or empty list. |
| `expected_evidence` | yes | Expected typed payload and evidence envelope. |
| `fallback_or_degrade_rule` | no | Proposed fallback; compiler still decides. |
| `priority` | no | Used only for budget decisions. |
| `budget_hint` | no | Advisory only. |
| `recipe_origin` | no | Trace only; recipe conflict can be repaired or skipped. |

### Contract Version Pinning

Compiler must pin every accepted graph to the exact reviewed inputs it used.

`contract_bundle.pins[]`:

| Field | Required | Notes |
| --- | --- | --- |
| `artifact_path` | yes | Relative path under repo. |
| `contract_version` | yes | From YAML when present. |
| `sha256` | yes | Content hash at compile time. |
| `review_status` | yes | Used by lint and Answer Package limitations. |
| `semantic_role` | yes | `metric`, `dimension`, `event`, `assumption`, `backlog`, `ledger`, `capability`, `prd_decision`. |

Mutation rule: missing pins are auto-added. Unknown, unreadable, or invalid contract artifacts block compile.

### Current-Data Snapshot Binding

Accepted graph must bind to the current-data basis before any evidence-producing node can run.

`current_data_snapshot`:

| Field | Required | Notes |
| --- | --- | --- |
| `analysis_basis` | yes | From `currently_available_data_at_run_time`. |
| `source_contracts` | yes | Accepted source contract refs, such as `paid_order_detail_2026_h1`. |
| `snapshot_exported_on` | yes when known | First accepted snapshot: `2026-07-04`. |
| `coverage_start` | yes when known | Contract coverage start. |
| `coverage_end` | yes when known | Contract coverage end. |
| `source_watermark` | yes when known | Used in Answer Package wording. |
| `late_arrivals_or_status_backfill` | yes when known | Existing accepted snapshot records none. |
| `freshness_status` | yes | `contract_backed`, `missing_contract`, `insufficient`, or blocked reason from existing vocab. |
| `artifact_policy` | yes | Prior answer artifacts are not rewritten after later data updates. |

Mutation rule: if snapshot metadata exists in the pinned contracts, compiler auto-adds it and records the reason. If a strong time-window claim needs missing freshness or source-time mapping, degrade through `timezone_policy` or block if no backlog ref exists.

### Policy Hooks

Hooks are local policy inputs to compiler decisions. They do not create BI truth.

| Hook | Inputs | Output into accepted graph | Default action |
| --- | --- | --- | --- |
| `permission` | user role, field sensitivity, requested output grain, permission context | `permission_status`, visible grains, blocked fields, limitation refs | Raw identifiers and permission failures block. Aggregate or masked paths may degrade when contracts allow. |
| `budget` | node priority, estimated cost, max depth, timeout, row/result budget | skipped or degraded paths with business impact | Skip low-value branches; ask question only when cost choice changes main answer or claim boundary. |
| `materiality` | metric thresholds by grain from `paid_amount.materiality_policy` | required threshold id, sparse comparable-window status | Missing reviewed threshold degrades strong pattern/anomaly/health wording. |
| `freshness` | snapshot, watermark, current-data basis, completeness | current-data limitation and verifier requirement | Stale or incomplete data degrades; destructive data-quality issue blocks dependent claim. |

## Accepted Graph Shape

Field-level draft.

| Field | Required | Notes |
| --- | --- | --- |
| `accepted_graph_id` | yes | Stable id for the accepted graph. |
| `compile_id` | yes | Links to compiler attempt. |
| `graph_version` | yes | Incremented when targeted repair or question result changes graph. |
| `question_families` | yes | Accepted families from Phase 1 list. |
| `intent_binding` | yes | Bound metric, scope, time range, baseline, grain, filters, named business objects, assumptions. |
| `contract_versions` | yes | Pinned contract artifacts. |
| `current_data_snapshot` | yes | Snapshot binding above. |
| `nodes` | yes | Accepted, auto-added, repaired, degraded, skipped, or blocked nodes. |
| `edges` | yes | Dependency and evidence flow. |
| `evidence_requirements` | yes | Required typed payloads and envelope fields per claim path. |
| `mutation_log` | yes | Full compiler action log. |
| `disabled_degraded_blocked_paths` | yes | User-visible path records for limitations and blocks. |
| `clarification_packages` | yes | Empty or one or more question tool packages. |
| `answer_package_constraints` | yes | Claim/evidence/wording constraints handed to synthesizer and verifier. |
| `verifier_handoff` | yes | Required verifier checks and blocked claim ids. |

`nodes[]`:

| Field | Required | Notes |
| --- | --- | --- |
| `node_id` | yes | Stable inside accepted graph. |
| `source_candidate_node_id` | no | Trace to LLM candidate node. |
| `status` | yes | `accepted`, `auto_added`, `repaired`, `degraded`, `blocked`, `skipped`, or `repair_requested`. |
| `capability` | yes | Phase 1 capability id. |
| `support_id` | yes when available | From `capability-support.yaml`. |
| `factor_group_id` | yes when available | From factor ledger. |
| `question_family` | yes | Phase 1 question family. |
| `params` | yes | Compiler-approved business params. |
| `scope` | yes | Metric, grain, filters, time range. |
| `depends_on` | yes | Accepted graph node ids. |
| `expected_evidence` | yes | Typed payload and envelope requirements. |
| `claim_constraints` | yes | Allowed evidence type, strength, wording limit, required limitations. |
| `policy_hooks` | yes | Permission, budget, materiality, freshness hooks applied. |
| `guardrails` | yes | Relevant lint or quality guards. |
| `mutation_ids` | yes | Log entries that created or changed the node. |

## Mutation Log

Each compiler change records why it happened and what claim boundary changed.

| Field | Required | Notes |
| --- | --- | --- |
| `mutation_id` | yes | Stable id. |
| `timestamp` | yes | Compile-time timestamp. |
| `action` | yes | `accepted`, `auto_added`, `repaired`, `degraded`, `blocked`, `skipped`, `repair_requested`, `question_requested`. |
| `node_ids` | yes | Affected nodes or empty for graph-level action. |
| `reason_code` | yes | Examples: `contract_version_unpinned`, `missing_contract_with_backlog`, `permission_failure`. |
| `source_rule_ref` | yes | Contract path, support id, capability lint id, PRD section, or backlog id. |
| `before` | no | Candidate params or claim boundary before mutation. |
| `after` | no | Accepted params or claim boundary after mutation. |
| `business_reason` | yes | User/process-visible explanation. |
| `claim_boundary_effect` | yes | Wording, strength, scope, baseline, or evidence impact. |
| `requires_question_tool` | yes | Boolean. |

Low-risk defaults can be auto-added only when this log has a deterministic reason and no material business conclusion changes.

## Disabled, Degraded, And Blocked Path Records

`disabled_degraded_blocked_paths[]`:

| Field | Required | Notes |
| --- | --- | --- |
| `path_id` | yes | Stable path id. |
| `path_status` | yes | `disabled`, `degraded`, `blocked`, or `skipped`. |
| `node_ids` | yes | Related accepted graph nodes. |
| `question_family` | yes | Phase 1 question family. |
| `capability` | yes | Capability id. |
| `factor_group_id` | yes when available | Ledger group. |
| `support_id` | yes when available | Capability support record. |
| `claim_type` | yes | From support record or target claim. |
| `grain` | yes | Requested and accepted grain. |
| `business_evidence_state` | yes | Phase 1 vocab. |
| `data_contract_state` | yes | Phase 1 vocab. |
| `evidence_type` | yes | Phase 1 vocab. |
| `strength` | yes | Phase 1 vocab. |
| `wording_limit` | yes | Phase 1 vocab. |
| `backlog_refs` | yes when gap exists | Required for `missing_contract` degrade. |
| `limitation_refs` | yes when permission/out-of-scope exists | Example: `pii_dimension_output_limit`. |
| `block_reason` | yes for blocked paths | Examples below. |
| `upgrade_path` | yes when known | From backlog or factor ledger. |
| `answer_package_visibility` | yes | `first_screen`, `limitations`, `follow_up`, or `internal_only`. |
| `business_summary` | yes | Human-readable summary. |

Hard `block_reason` examples:

- `permission_failure`
- `raw_sql_or_physical_schema_request`
- `raw_identifier_output`
- `invalid_metric_contract`
- `illegal_grain_filter_window`
- `cumulative_value_misuse`
- `missing_evidence_ref`
- `missing_contract_without_backlog_ref`
- `raw_external_ingestion_out_of_scope`

## Clarification Package

Question tool packages are compiler artifacts, even when the LLM drafts the options.

`clarification_packages[]`:

| Field | Required | Notes |
| --- | --- | --- |
| `clarification_id` | yes | Stable id. |
| `insert_point` | yes | `intent_binding`, `graph_compile`, `graph_repair`, or `final_verification`. |
| `trigger_reason` | yes | Baseline, time semantics, permission path, claim strength, scope, business object, or budget ambiguity. |
| `recommended_inference` | yes | Compiler-approved default if user accepts recommendation or no question opens. |
| `business_options` | yes | Two or three options, plus escape option. |
| `option_id` | yes per option | Stable id. |
| `option_label` | yes per option | Business wording. |
| `option_effect` | yes per option | Effect on graph, baseline, claim boundary, cost, or evidence path. |
| `escape_option` | yes | Fixed `tell the agent to do differently`. |
| `outcome` | yes when resolved | `user_selected`, `recommended_inference_selected`, `agent_instructed_differently`, or `system_inferred`. |
| `chosen_assumption` | yes when resolved | Written into accepted graph and verifier checks. |
| `mutation_ids` | yes | Log entries created by the outcome. |

Low-risk gaps use `system_inferred` and continue. If a question opens, it may block the run until resolved.

## Answer Package Constraints

Compiler produces constraints; synthesizer drafts text inside them; verifier enforces them.

`answer_package_constraints`:

| Field | Required | Notes |
| --- | --- | --- |
| `allowed_question_families` | yes | Accepted family list. |
| `intent_summary` | yes | Bound metric/scope/time/baseline/grain. |
| `contract_versions` | yes | Pins copied from accepted graph. |
| `current_data_snapshot` | yes | Snapshot statement requirements. |
| `claim_group_constraints` | yes | One record per target claim group. |
| `required_limitations` | yes | Degraded, disabled, blocked, skipped paths that must surface. |
| `required_visualization_constraints` | yes | Visual blocks must bind to evidence refs and limitations. |
| `required_verifier_checks` | yes | Capability verifier hooks and answer-level checks. |

`claim_group_constraints[]`:

| Field | Required | Notes |
| --- | --- | --- |
| `claim_group_id` | yes | Stable id. |
| `target_claim` | yes | Business claim. |
| `scope` | yes | Claim scope. |
| `baseline` | yes when comparison claim | Required for change, health, anomaly, and custom baseline claims. |
| `metric` | yes | Usually `paid_amount`. |
| `required_evidence_refs` | yes | Empty refs block final claim. |
| `allowed_evidence_type` | yes | Phase 1 vocab. |
| `allowed_strength` | yes | Phase 1 vocab. |
| `wording_limit` | yes | Phase 1 vocab. |
| `disallowed_wording` | yes | Example: confirmed cause wording for candidate mechanisms. |
| `visible_limitations` | yes | Backlog, permission, unsupported grain, or data quality boundaries. |
| `verifier_hooks` | yes | From capability cards. |

## Lint Rules

| Rule id | Trigger | Source | Default action | Record |
| --- | --- | --- | --- | --- |
| `metric_contract_required` | Target metric missing or invalid | metric contract, capability card | block | blocked path with `invalid_metric_contract`. |
| `contract_version_unpinned` | Any used contract lacks pin | compiler contract | auto repair | mutation log with pin source. |
| `current_data_snapshot_unbound` | Evidence node lacks snapshot basis | metric contract snapshot policy | auto repair when metadata exists; targeted repair or degrade when missing | mutation log and Answer Package snapshot requirement. |
| `first_screen_quality_required` | First-screen or strong claim lacks quality check | data_quality_check card | auto repair | `data_quality_check` node `auto_added`. |
| `permission_context_required` | Sensitive or permission-limited field used | dimension/factor ledger | block or degrade | permission limitation or `permission_failure`. |
| `raw_identifier_output_forbidden` | Raw user id, IP, or device id output requested | product decision, ledger limitation | block | `pii_dimension_output_limit`. |
| `raw_sql_or_physical_schema_request` | LLM or user asks compiler to emit raw SQL/schema | project boundary | block | blocked path, no repair prompt. |
| `declared_path_required` | Formula path absent from metric contract | formula_decompose card | block | unsupported formula path record. |
| `reconciliation_required` | Formula claim lacks residual/reconciliation | metric contract, formula card | block strong claim or targeted repair | mutation log and verifier hook. |
| `cumulative_value_guard` | Cumulative month-to-date used as daily or phase value | metric quality guard | block | `cumulative_value_misuse`. |
| `time_boundary_guard` | Missing timezone, inclusivity, or month-length semantics | metric quality guard | targeted repair; degrade if backlog ref limits strength | backlog `timezone_policy`. |
| `materiality_guard` | Strong pattern/anomaly/health wording lacks reviewed threshold or comparable windows | metric materiality policy | degrade | wording limit becomes `insufficient` or lower strength. |
| `missing_contract_with_backlog` | Required support exists only as backlog gap | backlog, support records | degrade | degraded path with backlog refs. |
| `missing_contract_without_backlog` | Gap has no backlog ref | data_quality_check lint | block | `missing_contract_without_backlog_ref`. |
| `unsupported_grain_with_fallback` | Requested grain unsupported but aggregate supported | dimension/factor ledger | degrade | accepted supported grain and limitation. |
| `unsupported_grain_without_fallback` | Requested grain unsupported and no safe fallback | dimension/factor ledger | block | `illegal_grain_filter_window` or unsupported grain block. |
| `raw_external_ingestion_scope` | Raw web/news/forum/media evidence requested | backlog `raw_external_evidence_ingestion_scope` | block | out-of-scope path record. |
| `candidate_pool_must_be_resolved` | Attribution candidate lacks ledger, evidence, or backlog trace | joint_attribution card | targeted repair | repair prompt limited to allowed mutation space. |
| `event_identity_required` | Business object cannot map to event, assumption, evidence, or backlog | event_evidence card | targeted repair, then block if unresolved | mutation log and question package when material. |
| `evidence_ref_required` | Claim group lacks evidence refs | answer_verify card | block | `missing_evidence_ref`. |
| `degraded_paths_visible` | Material degraded/blocked/skipped path omitted from answer constraints | answer_verify card | targeted repair | Answer Package constraints update. |

## Repair, Degrade, And Block Decision Table

| Condition | Default action | Example | Accepted graph effect |
| --- | --- | --- | --- |
| Missing deterministic guardrail | auto repair | Add `data_quality_check`, contract pins, snapshot binding, permission/freshness/materiality guards | Node or metadata `auto_added`, mutation reason recorded. |
| Missing optional low-risk parameter | auto repair | Fill current-data basis from pinned contract | `system_inferred` assumption plus mutation log. |
| Target claim unclear | targeted repair | LLM proposed pattern scan without target claim | `repair_requested`; LLM returns patch only. |
| Baseline/window ambiguity can change conclusion | question tool or targeted repair | Month-to-date vs same month phase | Clarification package or repair patch. |
| Candidate mechanism changes business route | targeted repair or question tool | Adding payday/holiday/channel mechanism | Repair stays inside allowed candidate pool; material ambiguity can ask user. |
| Missing contract has backlog ref | degrade | `product_operation_event_contracts`, `timezone_policy`, `gameplay_metric_contracts` | Path stays visible with `missing_contract` and backlog refs. |
| Missing contract has no backlog ref | block | Unregistered data source with no backlog trace | Blocked path. |
| Unsupported grain has supported aggregate fallback | degrade | Raw geo/device request downgraded to aggregate-only when allowed | Accepted fallback grain plus limitation. |
| Unsupported grain has no safe fallback | block | Raw IP/device output | Blocked path with permission limitation. |
| Permission failure | block | User asks for raw user ids or individual-user claim | Blocked path and no evidence handoff. |
| Weak, sparse, or low-materiality evidence | degrade | Pattern recurrence below strong threshold | Lower strength or `insufficient` wording. |
| Budget branch cannot affect main conclusion | skip | Extra high-order attribution after stable one-dimensional result | Skipped path with follow-up option. |
| Verifier finds over-strong wording | targeted repair, then degrade or block | Candidate mechanism written as confirmed cause | Repaired claim text or blocked claim group. |
| Evidence ref missing for final claim | block | Synthesizer claim has no evidence ref | Claim group blocked. |

## LLM Targeted Repair Boundary

The repair prompt may receive only:

- capability card summaries
- contract summaries needed for the failing nodes
- lint findings
- current accepted graph excerpt
- allowed mutation space from compiler
- question/clarification outcome when available

The repair prompt must not receive:

- raw SQL
- physical schema instructions
- unreviewed source fields as execution authority
- permission-forbidden identifiers
- authority to change contracts, ledger states, evidence strength, or wording limits
- authority to promote backlog gaps to supported paths

Repair output patch:

| Field | Required | Notes |
| --- | --- | --- |
| `patch_id` | yes | Stable id. |
| `affected_node_ids` | yes | Existing nodes only, unless compiler allowed new node ids. |
| `proposed_changes` | yes | Params, target claim, dependency, or allowed node insert/delete. |
| `reason` | yes | Business-facing reason. |
| `expected_evidence_change` | yes | Must stay inside allowed evidence/wording vocab. |
| `assumptions` | yes | Any inferred defaults for compiler review. |

Compiler re-runs all lint after repair. If the patch exceeds allowed mutation space, the patch is rejected and the path is degraded, blocked, or routed to question tool.

## Question Tool Insertion Points

| Insert point | Open when | Continue without question when |
| --- | --- | --- |
| `intent_binding` | Scope, metric, target object, baseline, or time semantics can change the business conclusion. | The default is low risk and recorded as `system_inferred`. |
| `graph_compile` | Requested grain, capability route, budget, or claim strength materially changes answer boundary. | Compiler can safely degrade and record limitation. |
| `graph_repair` | LLM repair needs user preference or user override changed intent. | Local repair has one clear contract-backed route. |
| `final_verification` | Final wording depends on an assumption that could change claim strength or business meaning. | Verifier can downgrade wording and expose limitation. |

Every opened question package includes a recommended inference and the `tell the agent to do differently` exit.

## Evidence And Verifier Handoff

Every evidence-producing node must return a typed payload inside an evidence envelope with:

- `evidence_ref`
- `typed_payload`
- `evidence_type`
- `strength`
- `wording_limit`
- `scope`
- `metric`
- `baseline` when applicable
- `contract_versions`
- `limitations`
- `disabled_degraded_blocked_path_refs`

Compiler hands verifier:

- accepted graph summary
- claim group constraints
- evidence refs by claim group
- disabled/degraded/blocked path records
- clarification outcomes and assumptions
- contract pins and current-data snapshot
- visual block constraints

Verifier must block or repair when:

- claim number, scope, baseline, or evidence ref is missing or mismatched
- wording exceeds `wording_limit`
- candidate mechanism is stated as confirmed cause
- permission-limited path is hidden
- disabled/degraded/blocked path materially affects the main answer but is omitted
- visualization implies stronger evidence than allowed

## Launch Representative Compile Cases

These cases are compile expectations, not runtime result expectations.

### 1. `pattern_explanation`: full-sample month-phase pattern

Question: `全量样本看，为什么从 2024 年 1 月开始到 2026 年 5 月结束，为什么每个月月初的付费金额都比月中/月末高一些`

Expected compile:

- Bind family `pattern_explanation`; optional merged families are allowed only when evidence need justifies them.
- Bind pattern domain as `intra-period`; month-start/mid/end windows are one instance of the generic pattern domain.
- Auto-add `data_quality_check`, contract pins, current-data snapshot binding, cumulative-value guard, materiality guard, and `answer_verify`.
- Accept `pattern_scan` for `month_phase` only if time semantics and comparable windows are explicit.
- Degrade strong pattern wording through backlog `timezone_policy` when source time mapping or current-data cutoff is insufficient.
- Add `event_evidence` for payday as `static_assumption` / `candidate_mechanism` with `wording_limit: candidate`.
- Block any cumulative month-to-date path used as phase evidence.

Required path records:

- `pattern_month_phase_paid_amount` degraded or accepted with `statistical_association`.
- `event_pattern_payday_dimension` accepted as `candidate_mechanism`, `strength: low`, `wording_limit: candidate`.
- Missing activity/campaign/server/payment-incident mechanisms degrade through backlog refs; no promotion to confirmed cause.

### 2. `paid_amount_change_explanation`: operating change review

Question: `上周付费金额为什么下降，主要驱动是什么？`

Expected compile:

- Bind `paid_amount_change_explanation`, target metric `paid_amount`, baseline from user if present or system-inferred comparable prior window.
- If baseline choice can change conclusion materially, open question tool; otherwise record `system_inferred`.
- Auto-add `data_quality_check`, snapshot, materiality, and `answer_verify`.
- Accept `formula_decompose` only on declared metric decomposition paths.
- Accept `segment_bridge` for channel/payment method where contracts support grain.
- Degrade growth-operation attribution through `product_operation_event_contracts` and `exposure_control_contracts`.

Required path records:

- Formula contribution can use `accounting_contribution` with limits from `component_contracts`.
- Marketing/campaign/spend/creative paths stay `missing_contract` and visible.
- Final wording separates contribution from cause.

### 3. `business_object_impact_review`: product or activity impact

Question: `6 月充值活动对付费金额影响多大？`

Expected compile:

- Bind `business_object_impact_review`.
- Route business object to `event_evidence`; require event identity, event window, affected scope, and comparison/control.
- If object cannot map to event, assumption, evidence-linked source, or backlog, targeted repair first; unresolved path blocks.
- Current recharge activity and product-operation events degrade through `product_operation_event_contracts` and `exposure_control_contracts`.
- No confirmed net impact wording without exposure/control or stronger evidence.

Required path records:

- `event_business_object_product_operation` degraded with `missing_contract`.
- `formula_decompose` or `segment_bridge` may provide adjacent metric evidence but cannot prove activity impact.

### 4. `revenue_health_review`: health and risk

Question: `最近 30 天付费金额健康吗，风险在哪里？`

Expected compile:

- Bind `revenue_health_review`.
- Require trend/pattern, formula or structure evidence, outlier review, data quality, and answer verification.
- Use materiality policy for 30-day rolling/month proxy wording.
- Degrade calendar-month or quarter claims according to metric materiality policy when comparable windows are sparse.
- Payment-chain risk claims degrade through `payment_status_and_dedup_contract` and incident/event gaps when needed.

Required path records:

- Health judgment must name target/baseline and limitations.
- Data risk is a visible claim group when freshness, completeness, permission, or missing contracts affect the answer.

### 5. `segment_or_factor_attribution`: segment and factor contribution

Question: `哪些渠道和支付方式解释了这段时间付费金额变化？`

Expected compile:

- Bind `segment_or_factor_attribution`.
- Accept `segment_bridge` for `acquisition_channel` and `payment_method` at supported grains.
- Use `joint_attribution` only after candidate pool resolves to ledger/support/backlog refs.
- Degrade campaign, spend, creative CTR/CVR, SEO/GEO, referral, or exposure paths through existing backlog refs.
- Block raw user/IP/device outputs and individual-user claims.

Required path records:

- Supported channel/payment method paths can use `accounting_contribution`, `strength: medium`, `wording_limit: quantified`.
- Permission-limited geo/device paths must reference `pii_dimension_output_limit` when visible output is affected.

### 6. `anomaly_or_black_swan_review`: anomaly and external context

Question: `6 月这几天是不是异常，是不是外部黑天鹅导致的？`

Expected compile:

- Bind `anomaly_or_black_swan_review`.
- Auto-add `data_quality_check` before anomaly claim.
- Require `outlier_scan` with anomaly definition or baseline; targeted repair if missing.
- Use `event_evidence` for external context only through reviewed event/evidence records.
- Raw web/news/forum/media crawling blocks through `raw_external_evidence_ingestion_scope`.
- Black-swan wording stays candidate/context unless stronger contracts and evidence exist.

Required path records:

- `outlier_black_swan_external_context` and `event_black_swan_external_context` use `candidate_mechanism`, `strength: low`, `wording_limit: context`.
- Unsupported raw external ingestion is blocked.

### 7. `custom_baseline_comparison`: explicit baseline

Question: `6 月前 10 天和 5 月前 10 天相比为什么变化？`

Expected compile:

- Bind `custom_baseline_comparison`.
- User-specified baseline wins when compatible with contracts.
- Record inclusive/exclusive window boundaries, timezone, baseline comparability, and current-data snapshot.
- Targeted repair if the candidate graph mixes cumulative month-to-date with daily/window values.
- Degrade strong baseline wording through `timezone_policy` if time semantics are unresolved.

Required path records:

- `pattern_custom_baseline_paid_amount` may run as `statistical_association` with time-boundary limitation.
- `formula_custom_baseline_order_chain` degrades when component contracts or reconciliation are insufficient.

### 8. `data_quality_or_evidence_review`: evidence trust review

Question: `这个付费金额结论证据够不够，哪些数据缺口会影响结论？`

Expected compile:

- Bind `data_quality_or_evidence_review`.
- Accept `data_quality_check` and `answer_verify`; other capabilities are optional evidence refs, not required.
- Output contract coverage, permission limits, freshness/current-data basis, missing contracts, unsupported grains, and blocked claim paths.
- Missing-contract records must link backlog refs; gaps without backlog ref block compile.
- Raw identifier output requests block.

Required path records:

- `dq_contract_coverage_review`, `dq_materiality_threshold_policy`, and `dq_permission_sensitive_identifiers` appear when relevant.
- Trust judgment must state affected claim groups and upgrade paths.

## Human Confirmation Still Required

Compiler design can proceed with degrade/block records for these. It must not promote them to supported paths:

- new business metric definitions, owner decisions, or final table structures
- SQL, runtime service APIs, or physical schema
- converting a current gap into `contract_backed`
- raw user id, IP, device id, or individual-user output permission
- refund, reversal, chargeback, cancellation, net revenue, payment-order-to-gameplay linkage
- campaign spend, bid, creative CTR/CVR, SEO/GEO, referral activity, server/Grafana/payment incident/product activity sources
- raw external crawling or AnySearch-like connector claims before reviewed connector/source/evidence contracts

