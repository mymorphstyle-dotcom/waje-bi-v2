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
Core evidence resolver backed by PostgreSQL. Client validator flags and bare
query hashes do not satisfy the review. Each turn verifies the persisted chain:

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
exact persisted `reuse` decision and inherited topic continuity.

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
