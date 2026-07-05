# Phase 3 Semantic Compiler Dry-Run Contract Test

Status: contract-level dry-run handoff for Phase 3 semantic compiler implementation.

Scope: validate accepted graph to semantic query request/response, evidence envelope, and Answer Package handoff mapping. This dry-run does not execute SQL, connect to ClickHouse/Postgres, call capability runtime, create final tables, join LangGraph runtime, produce real typed payload values, or publish business conclusions.

Runtime enablement gates and required runtime validator specs are tracked in `docs/phase-3-runtime-readiness-checklist.md`.
Persisted no-SQL artifacts and implementation handoff are tracked in `docs/phase-3-runtime-handoff.md`.

## Purpose

The dry-run contract test proves that a later semantic compiler implementation can consume a Phase 2 accepted graph and produce the Phase 3 skeleton artifacts without inventing BI semantics.

It validates:

- accepted graph nodes map to semantic query request skeletons
- semantic query request ids map to response skeletons
- response refs map to evidence envelope skeletons
- evidence refs map to Answer Package claim groups
- support ids, backlog refs, limitation refs, enums, blocked/degraded paths, and query execution boundaries are traceable
- blocked raw identifier, raw SQL, and raw external crawling paths do not produce executable query requests

It does not validate analytical correctness, numeric results, SQL generation, query planning performance, runtime retry behavior, LangGraph trace shape, or final answer wording.

## Inputs

The dry-run reads only existing design artifacts:

| Input | Provides |
| --- | --- |
| `evals/semantic-compiler/semantic-compiler-fixtures.yaml` | Accepted graph fixture, dry-run expected output refs, semantic query skeletons, response skeletons, envelope skeletons, Answer Package handoff skeletons. |
| `contracts/ledger/capability-support.yaml` | `support_id`, capability, question family, evidence type, strength, wording limit, data/business states. |
| `contracts/backlog/missing-contracts.yaml` | Backlog ids and gap states. |
| `contracts/ledger/factor-ledger.yaml` | Limitation refs such as permission and raw external boundaries. |
| `contracts/capabilities/*.yaml` | Capability typed payload names. |

## Output Boundary

Each dry-run expected output contains refs to existing fixture sections:

| Field | Meaning |
| --- | --- |
| `dry_run_id` | Stable dry-run expectation id. |
| `input.accepted_graph_node_refs` | Accepted graph nodes consumed by the dry-run. |
| `input.blocked_degraded_path_refs` | Path records supplied by graph compiler. Empty when no material path exists. |
| `output.semantic_query_request_refs` | Semantic query request skeletons expected from semantic compiler. |
| `output.semantic_query_response_refs` | Response skeletons expected from semantic compiler. |
| `output.evidence_envelope_refs` | Evidence envelope skeletons expected for downstream evidence ledger/Answer Package. |
| `output.answer_package_claim_group_refs` | Claim groups that consume the evidence refs. |
| `field_fill_policy` | Ownership split between graph compiler, semantic compiler, and later runtime. |
| `query_execution_policy` | Per-query executable boundary, runtime status placeholder, and no-SQL/no-runtime/no-result flags. |
| `mapping_assertions` | Node -> query -> response -> evidence -> claim group trace. |

The refs avoid duplicating the full fixture body. The existing fixture sections remain the field-level source for request/response/envelope/handoff skeletons.

## Field Ownership

| Field group | Filled by graph compiler | Filled by semantic compiler dry-run | Filled later by runtime/capability execution |
| --- | --- | --- | --- |
| Accepted graph nodes | yes | read only | no |
| Disabled/degraded/blocked paths | yes | read and preserve | no |
| Contract pins and snapshot refs | yes | copy into request skeleton | runtime may bind active versions when executing |
| Semantic query ids | no | yes | no |
| Request binding skeleton | no | yes | runtime may turn accepted request into executable plan |
| Response skeleton | no | yes | runtime fills real result refs after execution |
| Evidence envelope skeleton | no | yes | capability/evidence reducer fills typed payload values later |
| Answer Package handoff skeleton | graph constraints | evidence refs and path refs | verifier fills final pass/fail results later |
| SQL text, query ids, rows, typed payload values | no | no | yes, in a later phase |

Unknown runtime fields must remain `runtime_filled_later`, `pending_execution`, `degraded`, or `blocked`.

## Executable Query Boundary

Dry-run output can mark a semantic request as executable only when it is a contract-valid skeleton for later capability execution. It still carries no SQL and no real result rows.

Rules:

- `accepted` query skeletons can use `executable_query_request: true` with `runtime_status: pending_execution`.
- `degraded` query skeletons can use `executable_query_request: true` only when limitations and wording limits are visible.
- `repair_requested` query skeletons can keep the request skeleton and use `runtime_status: degraded`.
- `blocked` query skeletons must use `executable_query_request: false`, `runtime_status: blocked`, and a `block_reason`.
- raw user id, raw IP, raw device id, and unreviewed raw external content paths must use `executable_query_request: false`.
- Every dry-run query policy must keep `no_sql: true`, `no_runtime_connection: true`, and `no_real_result: true`.

## Validator

Run:

```bash
ruby tools/evals/run-semantic-compiler-contract-harness.rb
ruby tools/evals/generate-semantic-compiler-dry-run.rb
ruby tools/evals/validate-semantic-compiler-dry-run.rb
```

The generator is a deterministic contract-test helper. It reads
`evals/semantic-compiler/semantic-compiler-fixtures.yaml`, derives in-memory
dry-run outputs from the fixture sections, and compares them with
`dry_run_expected_outputs`. It writes no runtime artifact by default. Use
`--print` only to inspect the generated YAML on stdout.

The generator checks:

- generated output count is 8
- all five compiler outcomes are covered
- blocked raw identifier and raw external paths stay non-executable
- degraded query policies keep path refs and wording limits
- generated output exactly matches `dry_run_expected_outputs`

The dry-run validator checks:

- 8 fixtures have dry-run expected outputs
- dry-run input refs match accepted graph node and path ids
- dry-run output refs match semantic query request, response, evidence envelope, and Answer Package claim group ids
- every accepted graph node has a mapping assertion
- mapped query support ids match accepted graph nodes
- capability, question family, compiler outcome, evidence type, strength, wording limit, business state, and data state use existing vocab
- backlog refs resolve to `missing-contracts.yaml`
- limitation refs resolve to factor ledger review limitations
- blocked/degraded path records carry reason and business reason
- raw identifier and raw external blocked paths do not generate executable query requests

The local no-SQL artifact adapter persists the same harness bundle shape:

```bash
ruby tools/runtime/run-semantic-compiler-dry-run-artifact.rb
ruby tools/runtime/validate-semantic-compiler-dry-run-artifacts.rb
```

The adapter writes generated artifacts under `data/local/semantic-compiler-dry-run-artifacts/` by default, or to `--out DIR`. `--print` writes inspection YAML to stdout.

## Contract Harness

`tools/evals/run-semantic-compiler-contract-harness.rb` is the Phase 3
implementation handoff and contract acceptance helper. It calls the existing
dry-run generator through `--print`, parses the generated refs in memory, and
builds one semantic compiler artifact bundle per fixture. It writes no artifact
file; `--print` sends inspection YAML to stdout.

Each bundle contains:

| Field | Contract role |
| --- | --- |
| `fixture_id` | Fixture key for launch-eval traceability. |
| `question_family` | Question-family binding from capability support. |
| `compiler_outcome` | Accepted compiler outcome vocabulary. |
| `accepted_graph_input` | Launch case ref, shared contract pins, and accepted graph nodes. |
| `semantic_query_request` | Semantic query request skeletons from the fixture. |
| `semantic_query_response_skeleton` | Response skeletons for downstream runtime/evidence reducers. |
| `evidence_envelopes` | Evidence envelope skeletons with typed payload, strength, wording limit, limitations, and verifier handoff. |
| `answer_package_handoff` | Claim group bindings and required evidence refs. |
| `path_records` | Disabled/degraded/blocked path records from graph compiler fixtures. |
| `validation_summary` | Dry-run policy, mapping assertions, non-executable/degraded ids, support/backlog/limitation refs, and harness checks. |
| `non_runtime_notice` | Explicit boundary marker: no SQL, no database query, no capability execution, no real typed payload values, and no real business conclusion. |

Harness validation covers:

- all 8 fixtures produce bundles
- all five required compiler outcomes are present: `accept`, `auto_repair`, `targeted_repair`, `degrade`, `block`
- each bundle has semantic query requests or an explicit blocked reason
- blocked/raw-sensitive/raw-external paths stay non-executable
- degraded paths preserve path records, limitations, and wording limits
- evidence refs used by responses enter Answer Package claim group bindings
- support ids, backlog refs, and limitation refs resolve to existing ledgers
- `non_runtime_notice` carries the no-runtime boundary

The harness does not guarantee SQL correctness, physical schema correctness,
runtime performance, capability execution behavior, typed payload values, numeric
answers, or final business conclusions.

Future semantic compiler runtime code must keep satisfying the bundle shape,
the harness checks, the dry-run validator, and the fixture validator before any
runtime execution path is enabled.

## Fixture Coverage

| Fixture | Outcome | Dry-run coverage |
| --- | --- | --- |
| `SC-001` | `auto_repair` | Pattern request, payday static assumption, degraded timezone path. |
| `SC-002` | `accept` | Formula and segment requests with pending runtime execution. |
| `SC-003` | `degrade` | Event evidence request with missing event/exposure/control limitations. |
| `SC-004` | `degrade` | Revenue health formula/anomaly requests with quality and component gaps. |
| `SC-005` | `accept` | Supported segment request plus non-executable raw identifier/geo-device boundary. |
| `SC-006` | `block` | Non-executable raw external ingestion block plus context-only request. |
| `SC-007` | `targeted_repair` | Custom baseline repair skeleton with degraded runtime status. |
| `SC-008` | `accept` | Data quality trust review plus non-executable sensitive identifier boundary. |

## Handoff Rule

A future runtime implementation can consume these dry-run fixtures as contract tests for mapping only. Passing dry-run validation does not mean:

- a real query was planned
- a query is safe to execute
- a typed payload has real values
- an evidence envelope supports a real business conclusion
- an Answer Package can be published

Runtime implementation must add its own validators for SQL generation, execution safety, result refs, typed payload values, numeric reconciliation, evidence reduction, permission enforcement, and final answer verification.

Future semantic compiler runtime code can replace the deterministic helper's
mapping internals. It must keep passing the fixture validator and dry-run
validator before it is allowed to execute capability requests.
