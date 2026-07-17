# Launch Eval Plan

Status: design input for launch acceptance  
Scope: expectation packages, compiler acceptance, verifier rules, and failure attribution. Runtime, SQL, final table structure, and frontend implementation stay out.

## Purpose

Launch eval checks whether WAJE BI v2 can take real user wording, compile an accepted graph, preserve contract limits, produce evidence-bound claim groups, and fail visibly when a path is unsupported.

The eval input is [expectation-packages.yaml](/Users/luka/work/waje-bi-v2/evals/launch/expectation-packages.yaml:1). It lives outside `contracts/`, so the Postgres mirror remains scoped to the 21 contract YAML files.

## Case Shape

Each package contains:

- natural user wording
- expected question family and optional merged families
- intent, scope, baseline, grain, and business object when relevant
- required, optional, and forbidden capabilities
- expected compiler action: `accept`, `auto_repair`, `targeted_repair`, `degrade`, `block`
- expected support records with `business_evidence_state`, `data_contract_state`, `evidence_type`, `strength`, and `wording_limit`
- expected visual blocks
- verifier checks
- failure attribution labels

The package is an expectation contract for eval. It does not assert a final business answer, and it does not promote any `missing_contract`, `unsupported_grain`, or `out_of_scope_for_now` path. Fixed restricted-output and source-connection boundaries are evaluated separately from ledger state.

## Coverage

| Question family | Case | Main boundary |
| --- | --- | --- |
| `paid_amount_change_explanation` | `LE-002` | formula contribution, baseline repair, growth-operation gaps |
| `pattern_explanation` | `LE-001` | full-sample month-phase regression, cumulative-value block, candidate mechanism wording |
| `business_object_impact_review` | `LE-003` | recharge activity missing contracts and control/exposure limits |
| `revenue_health_review` | `LE-004` | health risk wording, component gaps, payment-quality gaps |
| `segment_or_factor_attribution` | `LE-005` | channel/payment contribution, contract-backed aggregate geo/device analysis, fixed raw-identifier and sparse-output boundary |
| `anomaly_or_black_swan_review` | `LE-006` | black-swan candidate wording and raw external ingestion block |
| `custom_baseline_comparison` | `LE-007` | explicit first-ten-days baseline and time semantics |
| `data_quality_or_evidence_review` | `LE-008` | trust boundary, missing evidence refs, restricted-output visibility |

Source pools are covered by `real_user_questions`, `historical_failure_cases`, and `matrix_generated_boundary_cases`.

## Eval Layers

| Layer | Run when | Required pass |
| --- | --- | --- |
| Smoke | prompt, compiler, synthesizer, verifier, or orchestration change | Representative cases for all compiler outcomes pass structural checks. |
| Affected slice | capability, contract, ledger, semantic-query, or verifier policy change | Cases referencing changed support ids or backlog refs pass. |
| Full acceptance | release candidate, model/provider change, major prompt change | All launch expectation packages pass. |

## Verifier Rule Summary

Claim group acceptance requires:

- question family and scope match the expectation package
- baseline is bound, inferred with recorded assumption, or clarified
- every claim has evidence refs
- evidence refs match allowed `evidence_type`, `strength`, and `wording_limit`
- visual blocks bind to evidence refs and do not imply stronger evidence
- degraded, blocked, skipped, restricted-output, or source-access paths are visible when material
- candidate mechanisms, contextual evidence, and missing contracts keep their wording limits

Failure conditions:

- wrong question family
- wrong baseline or missing baseline on comparative claims
- missing evidence refs
- unsupported grain hidden from Answer Package
- restricted-output leak or raw identifier output
- missing contract hidden or promoted
- candidate mechanism written as confirmed cause
- visualization overstates evidence

## Guardrail Promotion

Eval failure labels do not become runtime guardrails automatically. Promotion requires human validation, business/engineering owner review, severity, frequency, and generalizability assessment. After any promotion, rerun the affected eval slice.
