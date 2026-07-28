# P9 Case B controlled multi-Agent review

Date: 2026-07-24

Status: accepted for the P9 development baseline.

## Scope and authority result

The live question was:

`全量样本看，2026年6月1日付费金额为什么上涨？`

Both paths resolved the user-confirmed previous-day baseline, the same pinned
release and snapshot authority, and the same normalized accepted Plan:

`b1c6678effd6007ad856a4a3d534ac7c65b6e811e3935dcbdd018aa7b6e34823`

The recorded-plan control reuses an accepted DeepSeek raw plan proposal only
when the semantic input projection matches. Snapshot or semantic drift rejects
the replay. This removes planner variance from the A/B comparison without
injecting SQL, query results, Evidence, Claims, or publication rows.

| Path | Run | PlanRevision | Queries | Tasks | Evidence | Verified claims | Children |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| True single | `run-4ae696ccc54b453c916a0ec7` | `plan-revision-7a081a68b316a18cf453e564` | 33 | 21 | 22 | 25 | 0 |
| Controlled multi | `run-0f545b3483ce24e9feeeb2b1` | `plan-revision-324b68781a93158aa89a0fb3` | 33 | 21 | 22 | 25 | 2 |

Each path produced one narrative, one publication and one customer payload.
The parent retained Plan, Evidence, Claim, Publication, Delivery and customer
authority. The two children received only allowlisted, customer-safe parent
materials and had no BI tool, query, evidence or publication capability.

## Business answer and evidence boundary

The target-day paid amount was 308,240,309, compared with 304,142,630 on the
previous day: an increase of 4,097,679, or 1.35%.

The accepted formula decomposition attributed +5,172,408 to single-payment
amount, -1,155,821 to payment frequency and +81,092 to paid users. Material
segment signals included WajeSpecial at +9,189,445, Infinix X669 at -1,929,244,
and the greater-than-100,000 amount tier at -3,845,000.

External events remain temporal candidates and business context. The available
records do not support causal attribution. Internal operation and advertising
details are unavailable, payment-failure stage/retry/latency evidence is
unavailable, and payment-to-bet linkage remains observational.

The post-delivery human review retained the publication:

- evaluation ref:
  `insight-quality-evaluation:sha256:7036f29d70beab17e14c60336d433485e75b914e6786caaf36140319368e6646`;
- scores: explanation 4, novelty 4, decision usefulness 3, competing
  hypotheses 4, uncertainty 4, actionability 3;
- advisory findings: distinguish paid users from payment frequency more
  consistently, reduce repetition, and assign clearer action ownership;
- no narrative revision was requested or created.

## Repetition diagnosis and synthesis contract

The accepted children used disjoint axes and shared no exact source refs. The
repetition entered later: the parent narrative context carried each complete
child mini-report, including title, summary, findings and limitation refs, next
to the same sealed parent materials. Provenance verification kept claims safe,
but the writer had only advisory prose asking it to consolidate.

The current contract keeps complete child artifacts in the durable audit chain
and projects only typed incremental findings into narrative context. Each delta
has a finding kind, preferred block role, text and source refs; it may be
omitted and may appear at most once. Canonical identity removes exact duplicate
deltas. Open-text semantic similarity is not guessed by local keyword or fuzzy
matching.

Replaying the two persisted Case B child artifacts through this projection
reduced the parent-facing child payload from 4,484 to 3,220 bytes, a 28.19%
reduction. The old artifacts contained ten distinct findings and no exact
duplicates, so the measured saving comes from removing repeated report framing
and metadata. Final prose quality still requires a fresh live narrative run;
this payload replay alone does not claim that all paraphrastic repetition has
disappeared.

A fresh Standard Pack attempt on 2026-07-24 stopped before analysis with
`agent_required_action_argument_authority_invalid`: `model_turns=0`, no BI
task and no publication. That pre-analysis tool-discovery failure is tracked
separately and is not counted as evidence for or against the delta projection.

## Recovery and idempotency

Fault injection sent `SIGKILL` to the parent worker for
`run-8b166768126a7925d50455ca` while two child attempts were running. Recovery
advanced both child leases from epoch 1 to epoch 2 while preserving their stable
investigation and child-run identities.

The two orphaned epoch-1 attempts have no fabricated terminal events. Each
child accepted exactly one epoch-2 Provider attempt and one candidate artifact.
The recovered parent completed with one narrative, publication, customer
payload and delivery attempt.

## Follow-up and run-scoped replay

Two successful follow-ups read the existing publication through the bounded
artifact projection:

- the primary offset answer completed in 5.69 seconds and identified payment
  frequency at -1,155,821;
- the evidence-boundary answer completed in 7.91 seconds and preserved the
  external-event, internal-operation and payment-process limits.

No follow-up created another BI run, query or capability attempt. The artifact
reader returns bounded publication and claim projections even when the complete
artifact exceeds a tool-response safety budget.

Run replay now scopes messages, thread state and event cursor to the requested
run. A later follow-up can no longer replace the selected run's customer
snapshot or terminal state. The live persisted-publication acceptance passed
for `run-0f545b3483ce24e9feeeb2b1`.

## Latency diagnosis

The true-single path took 337.998 seconds:

| Stage | Seconds |
| --- | ---: |
| Queue before first Provider call | 55.717 |
| Intent and clarification | 34.683 |
| Clarification decision and resume gap | 35.972 |
| Continuation startup and plan materialization | 2.736 |
| Query, capability and Evidence | 95.229 |
| Claim and recommendation authority | 53.679 |
| Local post-authority processing | 12.458 |
| Narrative writer | 34.011 |
| Verification, publication and delivery tail | 13.513 |

The controlled-multi path took 363.812 seconds:

| Stage | Seconds |
| --- | ---: |
| Queue before first Provider call | 33.115 |
| Intent and clarification | 24.382 |
| Clarification decision and resume gap | 13.542 |
| Continuation startup and plan materialization | 2.716 |
| Query, capability and Evidence | 91.442 |
| Claim and recommendation authority | 64.211 |
| Local post-authority processing | 7.239 |
| Controlled-investigation critical path | 56.334 |
| Narrative writer | 56.847 |
| Verification, publication and delivery tail | 13.953 |

Child calls overlap, so their individual durations cannot be added as serial
wall time. The 480-second first-answer ceiling passed in both accepted paths,
but it is only a release guard.

The persisted Provider profile contains 10 terminal calls for true-single and
14 for controlled-multi. Recorded input/output sizes were 605,586/27,220 bytes
and 722,774/46,039 bytes respectively. The current DeepSeek audit records do not
contain authoritative token-usage fields, so this review does not estimate or
invent token counts. The single path retried one invalid clarification response.
The multi path retried one unapproved investigation source proposal and one
numeric-conflict child result.

The long-running planner sample
`run-5ae3fe554f7f9af6e3d2906e` sent a Provider request and then produced no
terminal Provider event. The worker recovered the run 543.063 seconds later.
`WAJE_LLM_TIMEOUT_SECONDS=0` meant the Provider layer had no positive deadline.
A comparable accepted planner request completed in 16.719 seconds, and an
operator replay of the same roughly 44.9 KB input completed in 14.471 seconds.
The evidence classifies the long sample as a Provider request without terminal
event or configured deadline. It does not support treating plan compilation
itself as a normal eight-minute operation.

Observed latency contributors remain separately actionable:

- queue and clarification-resume scheduling gaps;
- 33 queries and 21 capability tasks on the critical path;
- large semantic-verification and narrative inputs;
- one invalid clarification response;
- one unapproved child source proposal and one child numeric conflict;
- missing positive Provider deadline for the hung sample.

## Defects found and repaired

- Run replay previously mixed a selected run's publication with the latest
  thread messages and head; it now uses run-bound message and operation keys.
- The artifact tool could hide an oversized publication completely; it now
  returns bounded publication and claim projections.
- The migration ledger could advance while an existing advisory-quality table
  retained an older physical shape. Migration v17 verifies and repairs the
  missing columns and backfills only from immutable row payloads.
- Workbench now presents typed parent and child cards instead of raw controlled
  investigation JSON.

Model contract rejections remain visible as typed Provider attempts. Data gaps
remain explicit limitations. Human business-quality findings remain advisory.

## Evidence

- [A/B comparison](../../artifacts/phase9/case-b-ab-v20.md)
- [Latency diagnosis](../../artifacts/phase9/case-b-latency-v20.md)
- [Fault recovery](../../artifacts/phase9/case-b-multi-v15/fault-recovery.md)
- [Live acceptance](../../artifacts/phase9/case-b-multi-v18/acceptance/daily_paid_amount_change_2026_06_01-run-0f545b3483ce24e9feeeb2b1.json)
- [True-single final record](../../artifacts/phase9/case-b-single-v20/final.json)
- [Controlled-multi final record](../../artifacts/phase9/case-b-multi-v18/final.json)
- [Workbench screenshot](../../output/playwright/p9/workbench-parent-child-final.png)
- [Customer desktop screenshot](../../output/playwright/p9/customer-desktop.png)
- [Customer mobile screenshot](../../output/playwright/p9/customer-mobile.png)

No commit or push was created for this review.
