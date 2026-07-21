# Phase 7 Real Conversation Acceptance

Phase 7 business acceptance uses the production conversation path:

```text
HTTP Gateway
→ ConversationAgentCore
→ durable single-authority workflow
→ PostgreSQL authority records
→ ClickHouse active release
→ DeepSeek planner, claim reviewer, and writer
→ ClaimGraph + sealed AuthorityBundle
→ durable NarrativeMaterialProjection with opaque publication requirements
→ verified required NarrativeDocument blocks
→ PublicationFlow hard gate + PublicationProjection + delivery outbox
```

Fixture rows, injected SQL, caller-selected capability tasks, local answer
templates, replayed provider output, and scripted LLM providers cannot establish
a business acceptance pass.

## Data authority

Every new run attempt resolves the latest active release and persists one
`AuthorityContext` before the first `PlanRevision`. The context pins actual
`as_of`, release refs, snapshot refs, dataset coverage, and contract versions for
the attempt. Plan repair inherits that context. A new-data refresh creates a new
run attempt.

Normal users do not select an authority mode. Historical `as_of` remains an
operator audit input and cannot be injected by an eval case.

## Live protocol

Run one natural-language question at a time. Let the intent model and hard
contracts determine whether a material decision needs clarification. A harness
must not select an option, fabricate a decision, or advance a clarification on
the user's behalf.

At each durable checkpoint, review the corresponding authoritative projection:

1. `waiting_for_clarification`: active `IntentRevision`, unresolved material
   decision slot, typed options, and recommendation;
2. `planned`: `DecisionLedger`, `AuthorityContext`, `PlannerProposal`, admission
   outcomes, and accepted `PlanRevision` refs;
3. `evidence_ready`: terminal `CapabilityOutcome` records and `EvidenceLedger`
   refs, including branch-scoped limitations and failures;
4. `authority_sealed`: `ClaimGraph`, obligation coverage, claim-verifier report,
   sealed `user_required` obligation IDs, and immutable `AuthorityBundle`;
5. `narrative_ready`: opaque publication requirements, raw `NarrativeDocument`
   blocks, required-block coverage, and local and semantic verification reports;
6. `completed`: `PublicationProjection`, customer-safe publication payload,
   outbox identity, and delivery status.

Later checkpoints must carry references to earlier durable records. A missing
reference, digest mismatch, or broken transition closure is a failed case.

## Gateway checkpoint invocation

Start the configured Gateway without using port 3000, then submit one question:

```bash
PYTHONPATH=. uv run --python 3.12 --with-requirements requirements.txt \
  python tools/phase7/run_gateway_conversation_once.py \
  --base-url http://127.0.0.1:3107 \
  --user-id human-led-test \
  --question '2026年6月1日付费金额为什么上涨？主要由哪些指标变化导致？' \
  --output artifacts/phase7/human-led-q1/case-b/first-turn.json
```

The command returns at the first persisted checkpoint. Observe an existing run
without creating a new turn:

```bash
PYTHONPATH=. uv run --python 3.12 --with-requirements requirements.txt \
  python tools/phase7/run_gateway_conversation_once.py \
  --base-url http://127.0.0.1:3107 \
  --user-id human-led-test \
  --run-id RUN_ID \
  --events-only \
  --output artifacts/phase7/human-led-q1/case-b/checkpoint.json
```

When the run waits for clarification, submit the human's stable option ID:

```bash
PYTHONPATH=. uv run --python 3.12 --with-requirements requirements.txt \
  python tools/phase7/run_gateway_conversation_once.py \
  --base-url http://127.0.0.1:3107 \
  --user-id human-led-test \
  --run-id RUN_ID \
  --selected-option-id OPTION_ID \
  --output artifacts/phase7/human-led-q1/case-b/clarification.json
```

Free-text correction uses `--clarification-free-text`. Material correction may
create a superseding intent or run attempt; it does not mutate the accepted
decision in place.

`evals/phase7/business_question_expectations.yaml` contains user wording and
review expectations only. It cannot provide rows, SQL, plan tasks, expected
provider prose, or a preselected clarification.

## Pass and stop boundaries

A technical `completed` status does not establish business publication by
itself. A pass requires all of the following:

- current release and snapshot authority remained pinned within the run;
- every plan and execution transition closes to its persisted parent records;
- every published claim is verified within its evidence ceiling and carries
  complete evidence, decision, scope, baseline, and contract provenance;
- every accepted narrative block references only an allowed claim-material pair,
  fact, recommendation, limitation, and boundary facet in the durable
  `NarrativeMaterialProjection`, then survives block verification unchanged;
- every sealed `user_required` obligation is covered by verifier-accepted blocks
  marked `required`: `satisfied` uses a sufficiently strong claim and no coverage
  limitation; `mixed` and `contradicted` use a coverage claim plus every listed
  limitation; `unavailable` uses no claim and every listed limitation;
- a required-block veto enters focused repair; an unrepaired mandatory gap
  produces publication withholding;
- focused repair provider output and audit contain replacement targets only;
  accepted sibling blocks retain their typed identity and original writer
  provenance, and runtime merges the revision in source order;
- the `PublicationProjection` adds no fact and the outbox delivery references
  the same projection digest;
- the final `PublicationFlow` gate independently resolves the customer claim and
  limitation refs and confirms the same obligation closure;
- the customer payload contains only the fixed safe projection.

Stop the case immediately on a typed `waiting_for_clarification`, `failed`,
`publication_withheld`, `narrative_failed`, `publication_failed`, or delivery
failure result. Preserve the artifact and diagnose the general contract or
authority failure before another attempt.

The Case B artifact labeled `verified-03` is a preserved failed attempt. It
demonstrates late required-obligation closure: the writer contract omitted a
mandatory obligation and the publication hard gate detected the gap after block
verification. It predates the post-freeze launch acceptance.

The Case B artifact labeled `verified-04` is another preserved failed attempt.
It demonstrates focused-repair authority duplication: the provider was asked to
reproduce accepted siblings while repairing rejected targets, then strict scope
validation rejected the combined output. It predates the post-freeze launch
acceptance. The repaired contract keeps the provider response target-only,
performs the merge in runtime, preserves accepted block provenance, and exposes
typed post-seal failure state through Gateway when publication is unavailable.

The Case B artifact labeled `verified-05` is a preserved failed attempt. It
demonstrates provider-validator/materializer contract drift: an optional
recommendation-only `direction` block passed the writer-facing schema, then a
stricter undocumented typed-block rule rejected it after AuthorityBundle seal.
It predates the post-freeze launch acceptance. The repaired contract uses one
shared structural handle grammar for provider validation and typed
construction while leaving claim/limitation scope review to local validation
and focused repair.

Expected data gaps remain branch-scoped. An unavailable factor cannot erase
verified independent claims and cannot be represented as excluded, zero-impact,
or verified. A corrupt shared release, invalid SQL authority, broken digest, or
unsafe output boundary may block every dependent branch.

## Optional quality and wording-pair evaluation

After launch, teams may review insight quality on explanation value,
novelty, decision usefulness, competing hypotheses, uncertainty discipline, and
actionability. This review is advisory and cannot change sealed facts, block a
customer publication, or revoke a release acceptance.

Original/paraphrase pairs must preserve metric, scope, time semantics, primary
baseline, material decisions, required obligations, evidence ceilings, and main
claim direction when a wording-pair evaluation is run. Wording and analytical
emphasis may vary. Material authority drift becomes a regression input after
root-cause validation.

## Test layers and artifacts

Pure hard-boundary validators may use deterministic vectors. A scripted provider
may test one typed model contract when every expected call is explicit and any
extra call fails. Such tests make no business-pass claim.

End-to-end acceptance requires real PostgreSQL, ClickHouse, DeepSeek, Gateway,
and persisted records. Keep each run under a unique uncommitted `artifacts/`
directory. Preserve the original question, human decisions, checkpoint
projections, provider audit refs, authority record refs and digests, verifier
reports, opaque requirement handles and required-block coverage, publication
payload, and delivery outcome without overwriting prior attempts. Optional
pair-drift or quality evaluations persist as separate post-launch artifacts and
are not required for the acceptance record.
