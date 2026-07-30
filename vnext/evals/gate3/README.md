# Gate 3 Behavior-first Evaluation Corpus

## Purpose

This corpus tests whether a Business Analysis Agent can handle real, unfamiliar business questions.
It is authored before Gate 3 production implementation and cannot depend on runtime classes, action
names, tool sequences, SQL shapes, or one canonical AnalysisFrame.

The evaluation unit is an `EvaluationEpisode`:

```text
natural user conversation
+ business world and data conditions
+ hidden evaluator truth
+ decision stakes
+ acceptable outcome envelope
+ forbidden outcomes
+ counterfactual siblings
+ behavior and trace graders
```

Contract, identity, calendar, persistence, and publication tests remain necessary conformance suites.
They do not replace episode-level product evaluation.

## Authoring rules

1. Start from a business decision and a plausible user conversation.
2. Define the business world independently of any WAJE implementation.
3. Preserve multiple defensible measurement designs when the question permits them.
4. State observable properties of a good outcome; do not prescribe one action or tool sequence.
5. Give every episode counterfactual siblings that change one material factor at a time.
6. Separate evaluator-only hidden truth from information available to the Agent.
7. Use deterministic graders for hard invariants and calibrated model/human graders for professional
   judgment.
8. Keep `contract_supported`, allowed boundaries, and claim ceilings under reviewed catalog
   authority.
9. A run manifest may select or repeat episodes; it cannot weaken their expectations.
10. Evaluation failures enter the regression corpus before any runtime-guardrail decision.

Every base episode has at least three counterfactual roles:

| Role | Minimal mutation | Expected behavior |
|---|---|---|
| `meaning_preserving` | wording, order, or irrelevant context | preserve decision and measurement meaning |
| `measurement_changing` | metric, population, time, unit, denominator, exposure, or decision goal | revise the affected measurement identity |
| `boundary_changing` or `interaction_changing` | contract/coverage/evidence availability or a material user correction | revise disposition, claim ceiling, or accepted authority |

The siblings are evaluated as relations, not as three extra golden answers. They detect semantic
overreaction, semantic blindness, and evidence-boundary escape.

## Layout

```text
gate3/
├── evaluation-episode.schema.json
├── gate3-eval-policy.json
├── grader-rubric.json
├── candidates/
│   ├── real_expert_episodes.json
│   ├── generated_failure_episodes.json
│   ├── adversarial_conversation_episodes.json
│   └── root_counterfactual_anchor_episodes.json
├── catalog/
│   └── gate3-authoring-candidates.json
└── coverage-ledger.json
```

`candidates/` preserves independent authoring provenance. The merged authoring catalog remains
non-executable while any review or source gap is open. Business and measurement review later
promote selected Episodes into versioned development/calibration partitions. A protected held-out
set must live outside model and prompt development context; the repository stores only its manifest,
policy version, and content hashes.

## Evaluation layers

| Layer | Question answered |
|---|---|
| Product behavior | Does the Agent understand the decision, design a defensible measurement, investigate adaptively, and respect evidence limits? |
| Authority/trust conformance | Can semantic drift, stale state, evidence mismatch, or unsafe publication cross a hard boundary? |
| Implementation | Do codecs, stores, resolvers, providers, retries, and projections satisfy their local contracts? |

Launch readiness requires all three. Passing implementation tests cannot compensate for a failed
business episode.

## Dataset lifecycle

1. `authoring`: independently drafted episodes and worlds.
2. `reviewed`: business and measurement reviewers approve outcome envelopes.
3. `calibration`: human labels calibrate semantic/model graders.
4. `development`: visible regression cases used during implementation.
5. `held_out`: protected cases used for model/prompt/release comparison.
6. `production_mined`: reviewed, redacted cases derived from real traces after launch.

No case becomes a hard runtime rule automatically. Promotion requires a recurring, generalizable
failure pattern plus business and engineering ownership.

## Source protocol

| Source pool | Accepted provenance |
|---|---|
| `real_user_language` | redacted interview record, pilot conversation, or production trace reference |
| `expert_business_case` | named business/measurement authoring review |
| `historical_failure` | failure reconstruction with a durable incident or eval reference |
| `generated_business_world` | controlled world authored before the expected Agent behavior |
| `adversarial_conversation` | independent red-team authoring |

Generated natural language remains a candidate even when it sounds realistic. The validator rejects
`real_user_language` without interview/trace provenance and a `source_trace_ref`.

Review status is backed by durable attestations. `fully_reviewed` requires distinct business-owner
and measurement-reviewer references, review-record references, and the reviewed content hash.
Changing an Episode after review invalidates the old attestation for promotion.

The checked-in authoring catalog currently represents the authoring checkpoint. Eight authentic
user-wording seeds are preserved from the 2026-07-30 paid-amount question set; their fitted business
worlds and expectations remain candidates. The coverage ledger is the machine-readable readiness
statement. `policy_ready=false` continues to block G3.1 until source/review registries, double review,
grader calibration, protected partitions, and the remaining adversarial closures are complete.

## Runner projection

The catalog contains both Agent inputs and evaluator-only truth. A runner must construct input
incrementally:

- inject each user message only at its declared interaction point;
- expose `provided_to_agent` conditions directly;
- expose semantic- or data-discoverable conditions only through the corresponding governed
  inspection/probe surface;
- keep evaluator-only conditions, hidden business truth, provenance, acceptable outcomes, forbidden
  outcomes, siblings, and grading rules outside Agent context;
- give graders the frozen Episode and complete trace after the run.

Leaking future turns or evaluator truth invalidates the run even when the final answer looks correct.

## Commands

From `vnext/`:

```bash
npm run generate:eval-ledger:gate3
npm run check:evals:gate3
npm run check:evals:gate3:policy-ready
```

Generation is an explicit authoring action. Normal checks are read-only and fail if the ledger is
stale. The policy-ready command is the Gate entry check and currently fails on the checked-in open
findings, missing artifacts, authentic-source gap, and review gap.
