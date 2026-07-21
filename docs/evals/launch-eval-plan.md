# Launch Eval Plan

Status: current launch-acceptance input

## Purpose

Launch eval checks whether WAJE BI v2 can bind real user wording, preserve
material decisions, admit one authoritative plan, execute evidence branches,
settle claims within their ceilings, verify model-written blocks, and deliver a
fixed customer projection. Unsupported paths must fail or degrade visibly.

The input is
[expectation-packages.yaml](/Users/luka/work/waje-bi-v2/evals/launch/expectation-packages.yaml:1).
It supplies expectations and cannot inject rows, SQL, capability tasks, model
prose, or clarification choices.

## Case shape

Each package contains:

- natural user wording;
- expected question family and optional merged families;
- intended metric, scope, baseline, grain, and business object;
- required, optional, and forbidden executable capabilities;
- required completion authorities;
- expected admission/compiler outcomes: `accept`, `auto_repair`,
  `targeted_repair`, `degrade`, and `block`;
- expected contract and evidence states;
- allowed claims and publication ceilings;
- semantic evidence records, visual expectations, verifier checks, and failure
  attribution labels.

The package does not prescribe the final business prose. It cannot promote a
`missing_contract`, `unsupported_grain`, source-unbound, or out-of-scope path.
Fixed output safety and source access remain independent hard boundaries.

## Coverage

| Question family | Case | Main boundary |
|---|---|---|
| `paid_amount_change_explanation` | `LE-002` | formula contribution, baseline repair, growth-operation gaps |
| `pattern_explanation` | `LE-001` | month-phase regression, cumulative-value block, candidate wording |
| `business_object_impact_review` | `LE-003` | operation contracts and exposure/control limits |
| `revenue_health_review` | `LE-004` | health wording, component coverage, payment-quality gaps |
| `segment_or_factor_attribution` | `LE-005` | aggregate segment support, overlap, sparse and identifier limits |
| `anomaly_or_black_swan_review` | `LE-006` | anomaly validity, candidate context, raw external-ingestion block |
| `custom_baseline_comparison` | `LE-007` | explicit first-ten-day baseline and time semantics |
| `data_quality_or_evidence_review` | `LE-008` | claim trust, missing refs, and boundary visibility |

The suite covers real user questions, historical failures, and matrix-generated
boundary cases.

## Evaluation layers

| Layer | Run when | Required pass |
|---|---|---|
| Smoke | intent, planner, admission, writer, verifier, or orchestration change | Representative cases for every admission outcome close structurally. |
| Affected slice | capability, contract, ledger, query, or completion-authority change | Cases referencing changed support IDs, authorities, or gaps pass. |
| Full acceptance | release candidate, provider/model change, major prompt change | All expectation packages and the real-wording sequence pass. |

## Claim and publication checks

Acceptance requires:

- the `IntentRevision` and `DecisionLedger` match family, metric, scope, time,
  and baseline expectations;
- `AuthorityContext` pins the active release, snapshots, coverage, and contracts;
- planner proposal, admission decisions, and `PlanRevision` close by refs and
  digests;
- every published claim has qualified evidence refs and stays within its claim
  class and publication ceiling;
- unavailable, degraded, blocked, fixed-output, and source-access paths remain
  visible when material;
- candidate mechanisms, contextual evidence, and missing contracts preserve
  their wording limits;
- every accepted `NarrativeDocument` block survives verification unchanged;
- visualization and `PublicationProjection` add no unsupported fact;
- customer delivery closes to the sealed `AuthorityBundle` and projection
  digest.

Failure includes wrong family, scope, or baseline; missing authority refs;
hidden gaps; unsupported main conclusions; candidate-to-causal promotion;
misleading visualization; unsafe output; and delivery of an unverified block.

## Guardrail promotion

Eval failures do not become runtime guardrails automatically. Promotion requires
human validation, a generalizable pattern, business and engineering ownership,
and an assigned code or contract boundary. Rerun the affected slice after any
promotion.
