# Phase 7 dual-track live conversation evaluation

The evaluator has two explicit suites. `fixed-eight` preserves the reviewed
eight business questions and their fixed clock. `platform-current-data` uses a
question-family matrix that covers every public family, every current dataset
role, permission denial, contract-allowed partial evidence, result reuse, and
clarification resume on the original topic.

Both suites use real user wording with structured expectations. Required
capabilities are resolved from the canonical obligation registry and augmented
only by typed scenario requirements. Excluded inputs are expected typed gaps;
an absent current-data obligation is a hard failure. Expected model prose and
sentence-fragment assertions are outside the contract.

Observed dataset states, claim strength, claim ceiling, and terminal outcome
come from each turn's internal runtime authority chain. The evaluator does not
copy expected states into observed results. Expected excluded inputs must be
present as matching typed runtime gaps. `--cases` is a legacy explicit-file
mode and cannot be combined with `--suite`; unknown, cross-suite, and empty
case selections return a typed nonzero error before Core initialization.

The production revenue-diagnostics evaluation runs the eight reviewed business
questions with a fixed analysis clock. Every initial turn and clarification
resume receives the same context:

- `as_of`: `2026-06-03T12:00:00+01:00`
- target: `2026-06-02`
- previous day: `2026-06-01`
- rolling seven-day baseline: `2026-05-26` through `2026-06-01`
- same weekday last week: `2026-05-26`
- recurring-pattern history start: `2026-01-01`
- anomaly history start: `2026-05-03`

The case requires `paid_order_success`, `payment_attempt`, `market_dashboard`,
`gameplay`, and `external_event`. A missing dataset stays visible as
`missing_required_dataset:<dataset_id>`; the source owner must load and register
the snapshot. The harness does not synthesize source availability.

## Runtime correctness

For real ClickHouse runs, the harness reads the runtime authority through the
conversation store's PostgreSQL evidence resolver. The process-local query
resolver cannot stand in for persisted verified-claim and provenance authority.
Client validator flags and bare
query hashes do not satisfy the review. The AnalysisContract is resolved by the
exact run owner and its persisted signature. The local Answer Package remains a
required replay artifact and must carry either an exact embedded contract or an
exact `analysis_runtime_persistence.analysis_contract_ref`; presentation shape
alone never becomes authority. Missing or ambiguous store rows and any
run/ref/signature/content drift fail the review. Each turn verifies the
persisted chain:

`CapabilityBinding -> QueryContract -> QueryResult -> Completeness -> Snapshot`

Required queries must be succeeded, complete, and ready. Snapshot watermarks
must cover the contract windows and include the query permission scope. Typed
result refs begin with `result:`; legacy refs, partial reports, missing joins,
and incomplete bindings fail the runtime review. Persisted verified claims must
link the returned runtime ContextManifest to evidence, results, artifacts,
memory, and a ReuseDecision.

## Answer-quality scorecard

`tools/phase7/review_analysis_contract_eval.py` emits four 1-5 dimensions for
each turn and their case average:

| Score | Rubric |
| --- | --- |
| 1 | Missing or contradicted by runtime authority. |
| 2 | Present with a material final-audit warning. |
| 3 | Usable baseline answer with limited depth or follow-up value. |
| 4 | Strong answer with a minor evidence or presentation limitation. |
| 5 | Direct, insightful, actionable, and fully evidence-disciplined. |

The dimensions are directness, insight, actionability, and evidence discipline.
The scorecard consumes the real final-audit warnings and risk flags already in
the artifact. These scores and risk markers are advisory: they never rewrite
the answer, change `blocks_display`, or cause a process failure solely because
of style.

## Commands and artifacts

Run the fixed case twice with real services:

```bash
python3 tools/phase7/run_live_conversation_system_test.py \
  --suite fixed-eight \
  --case paid_amount_revenue_diagnostics_8_question_set \
  --real-llm --real-clickhouse --strict-quality \
  --artifact-dir artifacts/phase7/live-conversation-fixed-analysis-contracts-run-1

python3 tools/phase7/run_live_conversation_system_test.py \
  --suite fixed-eight \
  --case paid_amount_revenue_diagnostics_8_question_set \
  --real-llm --real-clickhouse --strict-quality \
  --artifact-dir artifacts/phase7/live-conversation-fixed-analysis-contracts-run-2
```

Run the platform matrix with:

```bash
python3 tools/phase7/run_live_conversation_system_test.py \
  --suite platform-current-data --real-llm --real-clickhouse --strict-quality \
  --artifact-dir artifacts/phase7/platform-current-data
```

Each case writes separate `.raw.json`, `.runtime-review.json`,
`.quality-review.json`, and `.coverage-summary.json` views alongside the
combined compatibility artifact. Obligation and runtime findings determine
hard acceptance. Quality scores remain advisory.
The runtime view includes both the ClickHouse review and obligation review for
every turn. Clarification coverage counts only declared clarification cases
that complete on the original topic. Reuse coverage additionally requires an
exact persisted `reuse` decision and inherited topic continuity. The decision
must come from the run-matched internal `admin_audit`; nested claim or section
markers do not count. The reviewer resolves the current capability binding and
query chain, then verifies distinct exact source/current result refs, the
current QueryContract, the authoritative cache-source marker, and the
scenario's expected capability and dataset provenance. The positive platform
case uses `market_health_compare` on `market_dashboard` with identical metric,
scope, permission, release membership, and fixed-window membership across both
turns; only baseline priority changes. The unavailable-as-of
`paid_order_success` / `compare_periods` path remains a typed blocker.

Review either artifact and optionally compare it with a baseline:

```bash
python3 tools/phase7/review_analysis_contract_eval.py ARTIFACT.json \
  --baseline BASELINE.json --out REVIEW.json
```

Artifacts remain local under `artifacts/`. Environment or source failures must
record the exact missing variable or input, owner, and impact. Runtime
correctness findings remain hard acceptance boundaries; wording and style
scores remain nonblocking.

## 2026-07-11 delivery audit

Both fixed-clock runs used the commands above with the real provider,
ClickHouse, and PostgreSQL. Each run reached a terminal `completed` state for
all eight questions after automatically resuming any clarification on the
original topic. Each terminal response contained a user-visible answer.
`--strict-quality` returned exit status 1 for both runs because the persisted
runtime authority exposed missing source bindings; the harness did not turn
those boundaries into synthetic passes.

| Result | Run 1 | Run 2 | Delta |
| --- | ---: | ---: | ---: |
| completed terminal answers | 8/8 | 8/8 | 0 |
| runtime-verified turns | none | Q8 | +1 |
| authoritative result refs on verified turns | 0 | 3 | +3 |
| final LLM audit coverage | 8/8 | 8/8 | 0 |
| directness | 5.00 | 4.75 | -0.25 |
| insight | 5.00 | 5.00 | 0.00 |
| actionability | 4.62 | 3.88 | -0.74 |
| evidence discipline | 1.00 | 1.25 | +0.25 |
| all claims traceable | yes | yes | no regression |
| all required queries complete | no | no | no regression |
| all capabilities bound | no | no | no regression |

Both final-code runs include their run-matched internal artifact paths while
the client-facing Answer Package remains scrubbed. The review tool requires an
internal Answer Package with the same run id, a complete quality gate, and an
actual `final_answer_audit` LLM call; an absent or mismatched audit scores 1 and
reports `final_answer_audit_unavailable`. Run 1 produced no accepted capability
binding. Run 2 verifies Q8 with three result refs against `market_dashboard`
and `market_dashboard_channel` through the PostgreSQL evidence resolver.

Artifacts:

- `artifacts/phase7/live-conversation-fixed-analysis-contracts-run-1/paid_amount_revenue_diagnostics_8_question_set.json`
- `artifacts/phase7/live-conversation-fixed-analysis-contracts-run-1/review.json`
- `artifacts/phase7/live-conversation-fixed-analysis-contracts-run-1/source-input-gaps.json`
- `artifacts/phase7/live-conversation-fixed-analysis-contracts-run-2/paid_amount_revenue_diagnostics_8_question_set.json`
- `artifacts/phase7/live-conversation-fixed-analysis-contracts-run-2/review.json`

The requested historical baseline artifact was absent, so the quality delta is
Run 2 versus Run 1. The missing-input ledger assigns the payment snapshots to
`data_operations_owner` and `payment_contract_owner`, the maintained internal
operation events to `data_operations_owner`, and the historical baseline to
`eval_owner`. These source gaps prevent paid-amount, payment-attempt, and full
operation-event claims from crossing the hard evidence boundary. They do not
hide the available market-dashboard results or weaken claim traceability.

The fixed expectation review also exposed route-coverage variance: depending
on the real LLM proposal, some turns omitted required segment, joint,
high-value-user, or outlier capability paths before source execution. The
owner is `bi_agent_runtime_owner` together with `eval_owner`; the impact is a
strict acceptance failure and reduced diagnostic depth, while the authority
boundary still prevents unsupported claims. The next general fix is a reviewed
question-family capability-obligation contract plus compiler reconciliation,
followed by the same fixed eval. No rule should key on one of the eight
sentences or on one observed model response.

## Runtime existing-data coverage audit

Generate the read-only, authority-backed coverage matrix with a fixed business
clock. The output stays local and source-unbound cells are valid report rows.
Resolver or contract-integrity failures return a nonzero status.

```bash
set -a; source /Users/luka/work/waje-bi-v2/.env; set +a
/tmp/waje-bi-v2-py312/bin/python3 tools/phase7/audit_existing_data_coverage.py \
  --as-of 2026-06-03T12:00:00+01:00 \
  --permission-scope analyst \
  --out artifacts/phase7/existing-data-coverage/coverage.json
```

## 2026-07-12 existing-data dual-track delivery audit

The final-code evaluation used real LLM, ClickHouse, and PostgreSQL services
through `ConversationAgentCore`, with `WAJE_LLM_TIMEOUT_SECONDS=300` and no
max-token override. Both fixed runs and the platform suite returned strict
status 1. The status is retained because runtime and obligation acceptance did
not pass.

The fixed-clock coverage audit succeeded at
`2026-06-03T12:00:00+01:00`: 1 cell was executable, 1 contract-partial, 4
degraded, 25 source-unbound, and 25 snapshot-unavailable-as-of. The accepted
paid-success release
`dataset-release:sha256:398c7f54befc152d280ad43311a25751fb187b05e6faba45812d1b25935c8557`
was loaded after the audit clock, so it cannot establish historical
transaction-time availability for that clock. `data_operations_owner` must
either publish historical authority at or before the clock or explicitly use a
current-authority audit clock. The same owner must publish an authoritative
`payment_attempt` release before payment-quality claims can execute.

| Result | Fixed Run 1 | Fixed Run 2 | Delta |
| --- | ---: | ---: | ---: |
| obligation routes required | 50 | 50 | 0 |
| obligation routes accepted | 18 | 21 | +3 |
| authority-backed executed routes | 0 | 0 | 0 |
| authority-backed degraded routes | 0 | 0 | 0 |
| unobserved accepted routes | 18 | 21 | +3 |
| missing routes | 32 | 29 | -3 |
| runtime-verified turns | 0/8 | 0/8 | 0 |
| authoritative result refs | 0 | 0 | 0 |
| claim-traceable turns | all | all | no regression |
| completed run-matched final audits | 8/8 | 8/8 | 0 |
| required reuse outcomes | 7 | 7 | 0 |
| passed reuse outcomes | 0 | 0 | 0 |

The eval now keeps authored roles, authority-resolved roles, and observed
runtime states separate. At the fixed clock the authority-resolved fixed-suite
expectations are `paid_order_success=snapshot_unavailable_as_of`,
`payment_attempt=source_unbound`, channel/gameplay context `degraded`,
`external_event=snapshot_unavailable_as_of`,
`internal_operation_event=source_unbound`, and the requested
`source_reconciliation:market_dashboard` path `contract_partial`. Required
queries were not all complete and required capabilities were not all bound. No
verified ClickHouse result ref crossed the final runtime boundary.

The eight platform cases covered 9 turns. In aggregate they required 32
obligation routes, accepted 28, left 28 unobserved, and reported 4 missing
routes. All 9 run-matched final audits were available. One case,
`platform_quality_clarification_resume`, passed runtime correctness,
obligation acceptance, and clarification resume. The declared persisted reuse
check passed 0/1. No authority-backed executed result ref was returned.

Independent artifact review found run-id-matched internal final-LLM audits for
8/8 turns in each fixed run and 9/9 platform turns. Fixed Run 1 scored
directness 5.00, insight 5.00, actionability 5.00, and evidence discipline
1.00; relative to the historical baseline, actionability improved by 0.38 and
the other dimensions were unchanged. Fixed Run 2 scored 5.00, 4.62, 3.50, and
1.25; relative to Run 1 the deltas were 0.00, -0.38, -1.50, and +0.25. These
scores are risk-only LLM audit output. Runtime, contract, provenance, evidence,
permission, and verifier checks remain hard boundaries.

The terminal clarification and persistence defects discovered during real eval
were fixed generically: accepted evidence gaps now terminate with a zero-claim
boundary answer; queryless and metric-backed boundary outcomes require a
structured, owner-assigned, repairable gap; unresolved metric ambiguity must
match canonical registry sources and compiler-authenticated target refs; and an
empty thread's first runnable turn is forced to a real topic. Final real runs
contain no failed turn. Reuse remains 0/7 in both fixed runs and 0/1 in the
platform suite because the runtime did not produce a reusable result under the
fixed-clock evidence boundaries.

Remaining gaps are explicit. `data_operations_owner` owns historical
transaction-time authority for paid-success and an authoritative
`payment_attempt` source; impact: paid and payment-quality analysis cannot
execute at the fixed clock; next action: publish reviewed historical authority
or choose a later audit clock. `analysis_contract_owner` owns the
`source_reconciliation:market_dashboard` contract-partial path; impact: the
only claim-ready market release cannot satisfy the requested reconciliation;
next action: complete the reviewed query/evidence contract. `data_quality_owner`
owns gameplay and channel context-only releases; impact: they can constrain
claims but cannot support the configured claim ceiling; next action: review
evidence readiness. `bi_agent_runtime_owner` owns missing-route and reuse
coverage; impact: supported obligations are frequently accepted without a
persisted executed/degraded outcome; next action: compile every required
authority-resolved obligation and persist terminal outcomes. No repair keys on
an eval sentence, case id, or individual model output.

Final local artifacts:

- `artifacts/phase7/existing-data-coverage/coverage.json`
- `artifacts/phase7/existing-data-fixed-eight-run-1/paid_amount_revenue_diagnostics_8_question_set.json`
- `artifacts/phase7/existing-data-fixed-eight-run-2/paid_amount_revenue_diagnostics_8_question_set.json`
- `artifacts/phase7/existing-data-platform-run-1/`

All evaluation artifacts remain local and untracked.
