# Phase 7 real conversation acceptance

Phase 7 business acceptance follows the same path as a user conversation:

`HTTP Gateway -> ConversationAgentCore -> LangGraph workflow -> PostgreSQL evidence authority -> ClickHouse active release -> DeepSeek -> verifier -> persisted Answer Package`

Business acceptance always uses the configured live dependencies. Fixture rows,
pre-bound SQL, pre-bound capabilities, local answer templates, dry-run workflow
results, replayed model output, and scripted LLM providers cannot establish that a
business case passed.

## Data authority

Normal questions use the current server time and the active release visible at
execution time. The user does not select an authority mode. A business date can
be queried only when the current authority chain resolves a valid release,
snapshot, permission scope, contract, and complete query result for that date.

An explicit historical `as_of` is an operator audit input. It is excluded from
normal user acceptance and must not be silently injected by an eval file or test
runner.

## Human-led case protocol

Run one natural-language question at a time. Let the real workflow decide whether
clarification is needed. A test harness must not choose an answer for the user or
advance every case automatically.

Each decision checkpoint reports business-readable state:

1. understood metric, date, direction premise, baseline, and material ambiguity;
2. active release and which requested inputs are currently supported;
3. analysis route and the maximum claim strength allowed by available evidence;
4. executed queries, completeness, actual direction, and premise correction;
5. supported factor and dimension contributions, with unavailable inputs scoped
   to their own branch;
6. raw DeepSeek answer, structured claims, provenance, verifier outcome, and the
   final publishable conclusion.

If the workflow completes several internal nodes in one run, the saved process
events and Answer Package provide the same audit trail. Human feedback still
controls whether the case continues, is corrected, or enters root-cause analysis.

## Real Gateway invocation

Start the application with PostgreSQL, ClickHouse, and DeepSeek configured, then
send exactly one question. The command polls persisted Gateway events until the
run reaches `completed`, `completed_without_workflow`,
`waiting_for_clarification`, or `failed`:

```bash
python3 tools/phase7/run_gateway_conversation_once.py \
  --base-url http://127.0.0.1:3000 \
  --question '2026年6月1日付费金额为什么上涨？主要由哪些指标变化导致？' \
  --output artifacts/phase7/human-led-q1/case-b/first-turn.json
```

When the returned run is waiting for clarification, resume only after the human
selects or rewrites an option. The resume command waits for the next persisted
checkpoint in the same way:

```bash
python3 tools/phase7/run_gateway_conversation_once.py \
  --base-url http://127.0.0.1:3000 \
  --run-id RUN_ID \
  --clarification-answer '采用前一天作为基线' \
  --selected-option-id OPTION_ID \
  --output artifacts/phase7/human-led-q1/case-b/clarification.json
```

To observe an existing run without submitting a message or clarification:

```bash
python3 tools/phase7/run_gateway_conversation_once.py \
  --base-url http://127.0.0.1:3000 \
  --run-id RUN_ID \
  --events-only \
  --output artifacts/phase7/human-led-q1/case-b/checkpoint.json
```

The default checkpoint timeout is 15 minutes. `--timeout-seconds` and
`--poll-interval-seconds` may be adjusted for a known provider latency profile.
On timeout, the command saves the events observed so far and exits nonzero.

`evals/phase7/business_question_expectations.yaml` contains natural-language
review expectations only. It provides no source rows, SQL, analysis plan, or
expected model prose.

## Acceptance boundaries

A completed status alone shows that the agent lifecycle completed. A business
conclusion is publishable only when its claims survive the current hard
boundaries:

- permission and SQL safety;
- current semantic and data contracts;
- active release and snapshot provenance;
- complete query/result bindings at the requested grain;
- evidence and claim provenance;
- verifier acceptance.

Quality review may flag wording or usefulness risks. It cannot rewrite verified
facts, grant evidence authority, or block a valid business conclusion solely for
style.

Missing optional evidence is recorded against the affected factor or auxiliary
dimension. It cannot erase verified metric direction or supported contributions
from other branches. An unavailable factor also cannot be presented as excluded,
zero-impact, or verified.

## Test layers

Pure functions and hard-boundary validators may use deterministic local vectors.
A strict scripted provider may test one explicit LLM task contract when every
response is supplied by that test and any unexpected task fails immediately.
These tests make no business-pass claim.

Workflow, Gateway, quality, and human-led business acceptance require real
PostgreSQL, ClickHouse, DeepSeek, and persisted artifacts. Historical replays are
debug evidence only.

Artifacts stay under a unique directory in `artifacts/` and remain uncommitted.
Store the original question, each human decision, checkpoint summaries, raw model
outputs, AnalysisContract, query/result/completeness references, Answer Package,
verifier output, and the final human assessment without overwriting prior runs.
