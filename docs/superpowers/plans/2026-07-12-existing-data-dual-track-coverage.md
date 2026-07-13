# Existing-Data Dual-Track Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Make every analysis path supported by current authoritative data discoverable, executable, completeness-checked, and useful in verified answers, then validate it through the fixed eight questions and a platform-wide coverage matrix.

**Architecture:** Add versioned question-family and diagnostic obligations to the existing ClickHouse runtime binding contract. The real LLM still proposes intent and route; local obligation resolution reconciles that proposal, and the analysis compiler executes every independently available dataset path while preserving exact gaps for excluded inputs. Register the existing paid-success facts as a PostgreSQL snapshot/release, preserve partial verified evidence in the Answer Package, and audit both fixed and platform coverage through ConversationAgentCore.

**Tech Stack:** Python 3.12, dataclasses, PyYAML, ClickHouse/clickhouse-connect, PostgreSQL/psycopg 3, LangGraph 0.6.11, real OpenAI-compatible LLM provider, unittest/pytest, Ruby contract/schema loaders, Next.js Gateway.

**Design:** `docs/superpowers/specs/2026-07-12-existing-data-dual-track-coverage-design.md` at commit `3a14ff77`.

## Global Constraints

- Use only the existing paid-order archive, existing ClickHouse paid-success tables, active market/gameplay/external-event releases, existing analysis assets, and existing local eval artifacts.
- Do not synthesize `payment_attempt`, `internal_operation_event`, user-level cross-source joins, or a historical evaluation artifact.
- Do not add a rule for one evaluation sentence, case id, or observed LLM response. Business-language triggers must represent a stable intent class and compile through versioned contracts.
- High-value business intent, route, explanation, answer synthesis, and final audit nodes use the real LLM. Do not add local narrative, business-intent, or claim fallbacks.
- Provider subprocess retry stays centralized at three attempts. LangGraph node wrappers and business-node implementations do not retry an LLM task; each business node invokes the shared client once. A typed verifier repair may invoke one explicit repair task and hard-verify once, without a surrounding node retry. Only an explicit positive `WAJE_LLM_TIMEOUT_SECONDS` permits a kill; use `300` seconds for real evaluation. Do not set `max_tokens`.
- The shared LLM client may normalize reviewed terminology but must not fill missing, empty, wrong-typed, or unlocalized high-value narrative fields with local templates. Production/live intent binding accepts explicit request-bound material or valid real-LLM material only; missing/illegal family, metric, pattern family, scope, time window, or target claim fails typed instead of receiving a local default. Fixture helpers stay explicitly outside the production/live path.
- Permission, SQL safety, contract legality, query completeness, claim provenance, and the evidence verifier remain hard boundaries. Final LLM audit wording and style findings remain warning-only.
- ClickHouse stores analytical facts. PostgreSQL stores contracts, snapshots, releases, runs, evidence, provenance, assets, and audit records. Do not add a PostgreSQL product page.
- Every supportable claim requires a ContextManifest, ReuseDecision, evidence ref, result ref, artifact ref, memory ref, and persisted trusted provenance.
- All real debugging and evaluation runs through ConversationAgentCore or the Gateway API. Clarification resumes the original topic.
- Fixed evaluation context remains `as_of=2026-06-03T12:00:00+01:00`, target `2026-06-02`, previous day `2026-06-01`, rolling baseline `2026-05-26..2026-06-01`, same weekday `2026-05-26`, pattern history from `2026-01-01`, and anomaly history from `2026-05-03`.
- `artifacts/` remains ignored and local. Missing environment variables or inputs are recorded with owner and impact; they are never reported as passing.
- Each task starts with a general failure-class test, confirms red, implements the contract-level fix, runs targeted and regression tests, receives independent review, and ends with one commit.
- Clarification resume resolves the prior AnalysisContract and actual accepted
  choice through immutable PostgreSQL authority. Run-request JSON cannot
  authorize carried gaps. The resolver verifies exact payload/signature/run
  ownership and returns a persisted clarification-outcome ref; carried
  `affected_capabilities` are exact-scoped to the prior canonical gap intersected
  with the persisted accepted choice, plus `analysis_contract`. Choice-scoped
  degraded capabilities remain AnalysisContract requirements even when omitted
  from the resumed graph; unrelated capabilities remain removed. Malformed
  mappings fail closed.
- Ready-sibling degradation requires authoritative dependency proof from the
  capability binding. Material gaps with empty `affected_capabilities` are
  global and cannot trigger automatic omission. Claim intents and ceilings are
  partitioned by capability: an omitted gap blocks only its affected claim and
  evidence chains, while a ready sibling may persist claims solely through its
  own complete binding, results, completeness, evidence refs, and verifier
  provenance. Shared claim-type labels never grant cross-capability authority.

---

### Task 1: Versioned Analysis Obligation Contract

**Files:**
- Create: `bi_agent/runtime/analysis_obligations.py`
- Modify: `contracts/runtime/clickhouse-analysis-bindings.yaml`
- Modify: `bi_agent/runtime/runtime_contract_registry.py`
- Test: `tests/phase4/test_analysis_obligations.py`
- Test: `tests/phase4/test_analysis_contract_compiler.py`

**Interfaces:**
- Consumes: `RuntimeContractRegistry.metric_ids`, public capability cards, and the eight public question families in `recipe_registry.py`.
- Produces: `ObligationRequest`, `ObligationResolution`, and `resolve_analysis_obligations(request, registry) -> ObligationResolution`.
- Produces `ObligationRequest.from_intent(question_family, question_families, target_metric, bound_context) -> ObligationRequest`.
- Produces registry accessors `question_family_obligation(question_family)`, `diagnostic_obligation(tag)`, and `order_capabilities(capabilities)`.

- [x] **Step 1: Write failing contract and resolver tests**

Add tests that require complete coverage, conditional activation, stable ordering,
unknown-reference rejection, and no sentence/case-id fields:

```python
def test_obligation_registry_covers_every_recipe_and_public_capability(self):
    registry = RuntimeContractRegistry.from_path(RUNTIME_BINDING_PATH)
    recipes = load_recipe_registry()
    self.assertEqual(set(registry.question_family_ids), set(recipes))
    referenced = {
        capability
        for family in registry.question_family_ids
        for capability in registry.question_family_obligation(family)["required_capabilities"]
    }
    self.assertTrue(referenced.issubset(set(public_capability_ids())))

def test_resolver_adds_contract_required_and_conditional_capabilities(self):
    result = resolve_analysis_obligations(
        ObligationRequest(
            question_families=("segment_or_factor_attribution",),
            diagnostic_tags=("factor_topk",),
            target_metrics=("paid_amount",),
            requested_dimensions=("channel",),
            baselines=("previous_day",),
            context_sources=(),
            claim_intents=("segment_contribution_or_mix_shift",),
        ),
        canonical_registry(),
    )
    self.assertEqual(
        result.required_capabilities,
        ("data_quality_profile", "segment_contribution", "joint_attribution", "answer_verify"),
    )
    self.assertIn("market_channel_context", result.conditional_capabilities)

def test_obligation_contract_rejects_eval_specific_keys(self):
    payload = load_contract(RUNTIME_BINDING_PATH)
    payload["question_family_obligations"]["paid_amount_change_explanation"]["case_id"] = "fixed-eight"
    with self.assertRaisesRegex(ValueError, "runtime_obligation_eval_specific_key"):
        RuntimeContractRegistry(payload)
```

- [x] **Step 2: Run tests and confirm red**

Run:

```bash
/tmp/waje-bi-v2-py312/bin/python3 -m pytest \
  tests/phase4/test_analysis_obligations.py \
  tests/phase4/test_analysis_contract_compiler.py -q
```

Expected: FAIL because the obligation sections, registry accessors, and resolver do not exist.

- [x] **Step 3: Add reviewed obligation sections**

Extend `clickhouse-analysis-bindings.yaml` with these schemas and populate all
eight public question families and every diagnostic tag currently represented
by the compiler:

```yaml
question_family_obligations:
  paid_amount_change_explanation:
    required_capabilities: [data_quality_profile, driver_decomposition, answer_verify]
    conditional_rules:
      - {condition: baselines_present, add: [compare_periods]}
      - {condition: dimensions_present, add: [segment_contribution]}
      - {condition: multiple_dimensions_present, add: [joint_attribution]}
      - {condition: event_context_requested, add: [event_evidence]}
    independent_capabilities: [market_health_compare, gameplay_activity_context, event_evidence]
    minimum_publishable_evidence: [verified_observation, verified_trust_boundary]
    missing_contract_owner: analysis_contract_owner
    degradation_policy: {missing_required_input: explicit_gap, missing_optional_input: omit_with_reason}

diagnostic_obligations:
  factor_topk:
    required_capabilities: [segment_contribution, joint_attribution]
    condition: dimensions_present
```

Do not include evaluation text, case ids, expected answers, or fixed turn indexes.

- [x] **Step 4: Implement registry validation and obligation resolution**

Create the focused module:

```python
@dataclass(frozen=True)
class ObligationRequest:
    question_families: tuple[str, ...]
    diagnostic_tags: tuple[str, ...]
    target_metrics: tuple[str, ...]
    requested_dimensions: tuple[str, ...]
    baselines: tuple[str, ...]
    context_sources: tuple[str, ...]
    claim_intents: tuple[str, ...]

    @classmethod
    def from_intent(
        cls,
        *,
        question_family: str,
        question_families: Sequence[str],
        target_metric: str,
        bound_context: Mapping[str, Any],
    ) -> "ObligationRequest":
        requirements = bound_context.get("analysis_requirements") or {}
        families = tuple(dict.fromkeys((question_family, *question_families)))
        return cls(
            question_families=tuple(item for item in families if item),
            diagnostic_tags=tuple(requirements.get("diagnostic_tags") or ()),
            target_metrics=tuple(requirements.get("target_metrics") or (target_metric,)),
            requested_dimensions=tuple(requirements.get("requested_dimensions") or ()),
            baselines=tuple(requirements.get("baselines") or ()),
            context_sources=tuple(requirements.get("context_sources") or ()),
            claim_intents=tuple(requirements.get("claim_intents") or ()),
        )

@dataclass(frozen=True)
class ObligationResolution:
    required_capabilities: tuple[str, ...]
    conditional_capabilities: tuple[str, ...]
    independent_capabilities: tuple[str, ...]
    minimum_publishable_evidence: tuple[str, ...]
    mutations: tuple[Mapping[str, str], ...]

def resolve_analysis_obligations(
    request: ObligationRequest,
    registry: RuntimeContractRegistry,
) -> ObligationResolution:
    required: list[str] = []
    conditional: list[str] = []
    independent: list[str] = []
    evidence: list[str] = []
    mutations: list[Mapping[str, str]] = []
    for family in request.question_families:
        contract = registry.question_family_obligation(family)
        required.extend(contract["required_capabilities"])
        independent.extend(contract["independent_capabilities"])
        evidence.extend(contract["minimum_publishable_evidence"])
        for rule in contract["conditional_rules"]:
            if obligation_condition_matches(rule["condition"], request):
                conditional.extend(rule["add"])
    for tag in request.diagnostic_tags:
        contract = registry.diagnostic_obligation(tag)
        if obligation_condition_matches(contract["condition"], request):
            required.extend(contract["required_capabilities"])
    ordered_required = registry.order_capabilities(required)
    ordered_conditional = registry.order_capabilities(conditional)
    for capability in (*ordered_required, *ordered_conditional):
        mutations.append({
            "action": "obligation_required",
            "capability": capability,
        })
    return ObligationResolution(
        required_capabilities=ordered_required,
        conditional_capabilities=ordered_conditional,
        independent_capabilities=registry.order_capabilities(independent),
        minimum_publishable_evidence=tuple(dict.fromkeys(evidence)),
        mutations=tuple(mutations),
    )
```

Condition evaluation must use an explicit allowlist:
`baselines_present`, `dimensions_present`, `multiple_dimensions_present`,
`components_present`, `event_context_requested`, `anomaly_review_requested`,
and `trust_review_requested`. Unknown conditions fail loading.

- [x] **Step 5: Run validation and regression tests**

```bash
ruby tools/contracts/validate-contracts.rb
/tmp/waje-bi-v2-py312/bin/python3 -m pytest \
  tests/phase4/test_analysis_obligations.py \
  tests/phase4/test_analysis_contract_compiler.py \
  tests/phase4/test_recipe_registry_and_compiler.py -q
```

Expected: all pass; every obligation reference is a public capability supported by the stated question family.

- [x] **Step 6: Independent review and commit**

Review for contract completeness, deterministic ordering, unknown condition rejection, and absence of eval-specific fields.

```bash
git add contracts/runtime/clickhouse-analysis-bindings.yaml \
  bi_agent/runtime/runtime_contract_registry.py \
  bi_agent/runtime/analysis_obligations.py \
  tests/phase4/test_analysis_obligations.py \
  tests/phase4/test_analysis_contract_compiler.py
git commit -m "feat: define analysis capability obligations"
```

---

### Task 2: Register Existing Paid-Success Facts

**Files:**
- Create: `tools/data/register_existing_paid_success_snapshot.py`
- Create: `tests/phase4/test_paid_success_snapshot_registration.py`
- Modify: `contracts/runtime/clickhouse-analysis-bindings.yaml` to declare `paid_order_success` as a required-release dataset with the canonical single-member `exact_dataset_set` authority contract.
- Modify: `contracts/sources/paid-order-detail.source.yaml` only if the live inspection proves a reviewed field list or checksum is missing; do not change accepted business semantics.
- Reuse: `tools/data/source_loader_common.py`
- Reuse: `bi_agent/conversation/postgres_store.py`

**Interfaces:**
- Consumes the existing archive, reviewed source contract, existing ClickHouse physical table, and `PostgresConversationStore.publish_dataset_snapshot_release()`.
- Produces `ExistingPaidSuccessInspection`, `inspect_existing_paid_success()`, `build_paid_success_snapshot_payload()`, and `register_existing_paid_success_snapshot()`.
- Produces `PaidSuccessRegistrationResult(release_ref, snapshot_refs, dataset_ids, authority_record_ref)`.
- Produces one `paid_order_success` DatasetSnapshot and one immutable atomic release; it does not create payment-attempt coverage.
- The canonical release validator accepts exactly the single `paid_order_success` member and fails closed for a missing or extra member.

- [x] **Step 1: Write failing inspection and publication tests**

```python
def test_inspector_requires_reviewed_schema_count_range_and_success_semantics(self):
    inspection = inspect_existing_paid_success(
        FakeClickHouseClient(valid_paid_success_rows()),
        archive_path=archive_fixture,
        physical_table="paid_order_success_clean_20240101_20260704",
        source_contract=paid_source_contract(),
    )
    self.assertEqual(inspection.watermark, "2026-07-04")
    self.assertEqual(inspection.row_count, 41_234_677)
    self.assertIn("paid_amount_ngn", inspection.schema_fields)
    self.assertTrue(inspection.ready_to_publish)

def test_registration_fails_closed_on_contract_or_table_mismatch(self):
    for mutation in ("archive_checksum", "schema", "row_count", "date_range", "duplicate_key"):
        with self.subTest(mutation=mutation), self.assertRaisesRegex(
            PaidSuccessRegistrationError, mutation
        ):
            inspect_existing_paid_success(
                mutated_client(mutation),
                archive_path=archive_fixture,
                physical_table="paid_order_success_clean_20240101_20260704",
                source_contract=paid_source_contract(),
            )

def test_registration_publishes_one_atomic_release_without_payment_attempt(self):
    store = InMemoryConversationStore()
    result = register_existing_paid_success_snapshot(store, valid_inspection())
    self.assertEqual(result.dataset_ids, ("paid_order_success",))
    self.assertNotIn("payment_attempt", store.dataset_snapshots)

def test_paid_success_canonical_release_membership_is_single_member_and_exact(self):
    self.assertEqual(
        canonical_dataset_release_members("paid_order_success"),
        ("paid_order_success",),
    )
```

- [x] **Step 2: Run tests and confirm red**

```bash
/tmp/waje-bi-v2-py312/bin/python3 -m pytest \
  tests/phase4/test_paid_success_snapshot_registration.py -q
```

Expected: FAIL with missing registration module.

- [x] **Step 3: Complete the canonical paid-success release authority contract**

Declare `paid_order_success` with `requires_release: true` and
`release_membership: {policy: exact_dataset_set, dataset_ids: [paid_order_success]}`
using the existing canonical release schema. This corrects the original plan,
which invoked the release validator without defining membership for this
dataset. Keep `payment_attempt` outside the release; this task does not create
or imply attempt coverage.

- [x] **Step 4: Implement read-only ClickHouse inspection**

The implementation issues aggregate-only queries for schema, row count, min/max
business date, null critical fields, amount bounds, and duplicate dedup keys.
Represent the immutable result as:

```python
@dataclass(frozen=True)
class ExistingPaidSuccessInspection:
    archive_sha256: str
    physical_table: str
    schema_fields: tuple[str, ...]
    schema_fingerprint: str
    row_count: int
    date_range: tuple[str, str]
    watermark: str
    rows_content_hash: str
    source_checksums: tuple[tuple[str, str], ...]
    validation_errors: tuple[str, ...]

    @property
    def ready_to_publish(self) -> bool:
        return not self.validation_errors
```

`rows_content_hash` must come from deterministic aggregate fingerprints over the
existing fact table; the tool must not read 41 million rows into Python.

- [x] **Step 5: Build and publish the immutable snapshot**

Build a complete payload compatible with existing release authority:

```python
def build_paid_success_snapshot_payload(
    inspection: ExistingPaidSuccessInspection,
    *,
    snapshot_id: str,
    load_revision: str,
    loaded_at: str,
) -> dict[str, Any]:
    return {
        "snapshot_ref": f"snapshot:{snapshot_id}:{load_revision}:paid_order_success",
        "dataset_id": "paid_order_success",
        "physical_table": inspection.physical_table,
        "watermark": inspection.watermark,
        "schema_fingerprint": inspection.schema_fingerprint,
        "schema_fields": list(inspection.schema_fields),
        "contract_ref": "contracts/sources/paid-order-detail.source.yaml@0.2",
        "permission_scopes": ["analyst"],
        "loaded_at": loaded_at,
        "status": "active",
        "evidence_state": "claim_ready",
        "reconciliation_status": "not_applicable",
        "logical_snapshot_id": snapshot_id,
        "load_revision": load_revision,
        "rows_content_hash": inspection.rows_content_hash,
        "snapshot_id": snapshot_id,
        "source_load_manifest_ref": f"source-load:{snapshot_id}:{load_revision}",
        "runtime_binding_ref": "contracts/runtime/clickhouse-analysis-bindings.yaml@1",
        "source_checksums": dict(inspection.source_checksums),
        "row_count": inspection.row_count,
        "date_range": list(inspection.date_range),
        "no_data_partitions": [],
        "no_data_partition_windows": [],
    }
```

Use `dataset_snapshot_release_lock()`, validate the payloads before writing, and
call `publish_dataset_snapshot_release()` once. Re-running an identical release
is idempotent; any immutable-field drift fails.

- [x] **Step 6: Add CLI dry-run and publish modes**

CLI arguments:

```text
--archive /Users/luka/Downloads/dapan_pay_data.zip
--physical-table paid_order_success_clean_20240101_20260704
--snapshot-id paid-order-detail-20240101-20260704
--load-revision accepted-20260705
--dry-run | --publish
```

Dry-run prints only non-secret validation metadata. Publish requires
`WAJE_RUNTIME_DATABASE_URL` and exits nonzero on any validation failure.

- [x] **Step 7: Run tests and available real dry-run**

```bash
/tmp/waje-bi-v2-py312/bin/python3 -m pytest \
  tests/phase4/test_paid_success_snapshot_registration.py \
  tests/phase4/test_dataset_release_authority.py \
  tests/phase7/test_conversation_persistence.py -q
set -a; source /Users/luka/work/waje-bi-v2/.env; set +a
/tmp/waje-bi-v2-py312/bin/python3 tools/data/register_existing_paid_success_snapshot.py \
  --archive /Users/luka/Downloads/dapan_pay_data.zip \
  --physical-table paid_order_success_clean_20240101_20260704 \
  --snapshot-id paid-order-detail-20240101-20260704 \
  --load-revision accepted-20260705 --dry-run
```

Expected: tests pass; live dry-run either reports `ready_to_publish=true` or records exact mismatch, owner `payment_contract_owner`, and impact. Do not publish after a failed dry-run.

- [x] **Step 8: Publish after a passing dry-run, review, and commit**

```bash
/tmp/waje-bi-v2-py312/bin/python3 tools/data/register_existing_paid_success_snapshot.py \
  --archive /Users/luka/Downloads/dapan_pay_data.zip \
  --physical-table paid_order_success_clean_20240101_20260704 \
  --snapshot-id paid-order-detail-20240101-20260704 \
  --load-revision accepted-20260705 --publish
git add tools/data/register_existing_paid_success_snapshot.py \
  tests/phase4/test_paid_success_snapshot_registration.py \
  contracts/runtime/clickhouse-analysis-bindings.yaml \
  contracts/sources/paid-order-detail.source.yaml
git commit -m "feat: register existing paid success authority"
```

If the real dry-run fails, commit the general validator and tests, leave the
runtime release unpublished, and record the exact owner/impact in the final
audit.

---

### Task 3: Contract-Driven Route Reconciliation

**Files:**
- Modify: `bi_agent/runtime/compiler.py`
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Modify: `bi_agent/runtime/recipe_registry.py`
- Modify: `bi_agent/runtime/llm_prompts.py`
- Test: `tests/phase4/test_recipe_registry_and_compiler.py`
- Test: `tests/phase4/test_llm_workflow.py`
- Test: `tests/phase7/test_agent_core_bridge.py`

**Interfaces:**
- Consumes `ObligationRequest` and `resolve_analysis_obligations()` from Task 1.
- Produces `reconcile_analysis_route(requested, route, intent, registry) -> (capabilities, route)`.
- Preserves `MutationRecord` entries with reasons `obligation_required`, `obligation_conditional`, `unsupported_question_family`, and `obligation_conflict`.

- [x] **Step 1: Write failing omission, idempotence, and no-overfit tests**

```python
def test_compiler_adds_missing_contract_obligations_from_typed_intent(self):
    compiled = compile_graph(
        question_family="segment_or_factor_attribution",
        question_families=("segment_or_factor_attribution",),
        target_metric="paid_amount",
        requested_nodes=("data_quality_profile",),
        bound_context={
            "analysis_requirements": {
                "requested_dimensions": ["channel", "game"],
                "baselines": ["previous_day"],
                "diagnostic_tags": ["factor_topk"],
            }
        },
    )
    self.assertTrue({"segment_contribution", "joint_attribution", "answer_verify"}.issubset(
        set(compiled.mutations.accepted_graph)
    ))

def test_route_reconciliation_is_idempotent_and_question_text_independent(self):
    first = reconcile_analysis_route(("data_quality_profile",), typed_route(), typed_intent(), registry())
    second = reconcile_analysis_route(first[0], first[1], typed_intent(), registry())
    self.assertEqual(first, second)
    self.assertNotIn("question_text", first[1]["obligation_resolution"])
```

Add parametrized tests over all public question families and all diagnostic
obligation tags. Add a negative test proving two paraphrases with identical
typed intent compile to the same graph.

- [x] **Step 2: Run tests and confirm red**

```bash
/tmp/waje-bi-v2-py312/bin/python3 -m pytest \
  tests/phase4/test_recipe_registry_and_compiler.py \
  tests/phase4/test_llm_workflow.py -k "obligation or route_reconciliation" -q
```

Expected: FAIL because the compiler still relies on local family constants and diagnostic bundles.

- [x] **Step 3: Replace hard-coded enablement with obligation resolution**

Remove `PHASE6_ENABLED_FAMILY_REQUIREMENTS`, `REVENUE_DIAGNOSTIC_BUNDLES`, and
`REVENUE_DIAGNOSTIC_FAMILIES` as policy sources. `compile_graph()` receives the
canonical runtime registry and calls:

```python
resolution = resolve_analysis_obligations(
    ObligationRequest.from_intent(
        question_family=question_family,
        question_families=question_families,
        target_metric=target_metric,
        bound_context=bound_context or {},
    ),
    runtime_registry,
)
proposed_graph = _dedupe(
    (*base_proposed_graph, *resolution.required_capabilities, *resolution.conditional_capabilities)
)
```

Capability order remains deterministic. Every auto-add or rejection becomes a
mutation record; no mutation is hidden in prompt normalization.

- [x] **Step 4: Reconcile the real LLM route before graph compilation**

In `langgraph_workflow.py`, replace `_reconcile_route_metric_capabilities()`
with the general reconciler. The LLM prompt asks for typed diagnostic tags and
analysis requirements, but does not instruct the model to reproduce the local
obligation set.

Hard-bound question family, metric, scope, permission, and fixed clock remain
local compiler inputs. Ambiguous materially different routes go through the
existing clarification contract.

For composite question-family intents, resolve each diagnostic tag against
each family's reviewed support set, then union the base and applicable
diagnostic obligations in registry order. A diagnostic that applies to one
family must not invalidate another family's base obligations. Reject a tag
that applies to none of the persisted families with an explicit mutation;
open clarification only when deterministic partitioning still leaves multiple
materially different contract-valid routes. Add regressions proving that an
incompatible diagnostic cannot suppress required, conditional, or independent
family obligations.

- [x] **Step 5: Run targeted and full compiler regressions**

```bash
/tmp/waje-bi-v2-py312/bin/python3 -m pytest \
  tests/phase4/test_recipe_registry_and_compiler.py \
  tests/phase4/test_llm_workflow.py \
  tests/phase7/test_agent_core_bridge.py -q
```

Expected: all pass; all public family obligations are present after compilation; two paraphrases with the same typed intent yield identical accepted graphs.

- [x] **Step 6: Independent review and commit**

```bash
git add bi_agent/runtime/compiler.py bi_agent/runtime/langgraph_workflow.py \
  bi_agent/runtime/recipe_registry.py bi_agent/runtime/llm_prompts.py \
  tests/phase4/test_recipe_registry_and_compiler.py \
  tests/phase4/test_llm_workflow.py tests/phase7/test_agent_core_bridge.py
git commit -m "feat: reconcile routes with analysis obligations"
```

---

### Task 4: Independently Executable Current-Data Paths

**Files:**
- Modify: `bi_agent/runtime/analysis_contract_compiler.py`
- Modify: `bi_agent/runtime/analysis_runtime.py`
- Modify: `bi_agent/runtime/capability_execution.py`
- Modify: `bi_agent/runtime/runtime_persistence.py`
- Test: `tests/phase4/test_analysis_contract_compiler.py`
- Test: `tests/phase4/test_capability_execution.py`
- Test: `tests/phase7/test_analysis_runtime_persistence.py`

**Interfaces:**
- Consumes independently executable capabilities from `ObligationResolution`.
- Produces query contracts and capability plans for available market, gameplay, and event paths even when the paid or payment-attempt path is source-unbound.
- Persists gaps only against affected capabilities and claim types.

- [x] **Step 1: Write failing source-isolation tests**

```python
def test_missing_paid_source_does_not_erase_available_market_gameplay_and_event_queries(self):
    outcome = compile_analysis_contract(
        proposal=current_data_multi_source_proposal(),
        accepted_capabilities=(
            "driver_decomposition",
            "market_health_compare",
            "gameplay_activity_context",
            "event_evidence",
        ),
        catalog=catalog_without_paid_but_with_current_releases(),
        registry=canonical_registry(),
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        permission_scope="analyst",
        release_resolver=current_release_resolver(),
    )
    intents = {item.query_intent for item in outcome.query_contracts}
    self.assertIn("daily_metric_baselines", intents)
    self.assertIn("gameplay_activity_probe", intents)
    self.assertIn("event_context_probe", intents)
    self.assertNotIn("component_driver_scan", intents)
    paid_gap = next(g for g in outcome.analysis_contract.contract_gaps if g.dataset_id == "paid_order_success")
    self.assertEqual(set(paid_gap.affected_capabilities), {"driver_decomposition"})
```

Add variants for a missing gameplay release, missing external event release, and
permission-limited channel release. Each variant must preserve unrelated query
contracts.

- [x] **Step 2: Run tests and confirm red**

```bash
/tmp/waje-bi-v2-py312/bin/python3 -m pytest \
  tests/phase4/test_analysis_contract_compiler.py \
  tests/phase4/test_capability_execution.py -k "independent or source_isolation" -q
```

Expected: FAIL where global dependency resolution or gap scoping removes available paths.

- [x] **Step 3: Scope dependencies and gaps per capability**

Introduce a focused internal projection:

```python
@dataclass(frozen=True)
class CapabilityDependencySet:
    capability_id: str
    metric_ids: tuple[str, ...]
    dimension_ids: tuple[str, ...]
    dataset_ids: tuple[str, ...]
    context_source_ids: tuple[str, ...]
```

Build query contracts per `CapabilityDependencySet`. A failed dependency set
emits gaps for that capability and does not remove snapshots or bindings from
another set. Deduplicate semantic-equivalent query contracts only after each
set has been evaluated.

- [x] **Step 4: Execute and persist ready/degraded bindings independently**

`AnalysisRuntime.execute()` returns one binding outcome per capability. Ready
and contract-allowed degraded bindings persist even when a sibling binding is
blocked. The run-level status aggregates without discarding successful result,
completeness, binding, and evidence refs.

- [x] **Step 5: Run persistence and workflow regressions**

```bash
/tmp/waje-bi-v2-py312/bin/python3 -m pytest \
  tests/phase4/test_analysis_contract_compiler.py \
  tests/phase4/test_capability_execution.py \
  tests/phase7/test_analysis_runtime_persistence.py \
  tests/phase4/test_llm_workflow.py -q
```

Expected: all pass; source-unbound paid paths coexist with persisted ready market/gameplay/event bindings.

- [x] **Step 6: Independent review and commit**

```bash
git add bi_agent/runtime/analysis_contract_compiler.py \
  bi_agent/runtime/analysis_runtime.py bi_agent/runtime/capability_execution.py \
  bi_agent/runtime/runtime_persistence.py \
  tests/phase4/test_analysis_contract_compiler.py \
  tests/phase4/test_capability_execution.py \
  tests/phase7/test_analysis_runtime_persistence.py
git commit -m "feat: isolate current data capability execution"
```

---

### Task 5: Current Metric, Dimension, and Window Query Closure

**Files:**
- Modify: `contracts/runtime/clickhouse-analysis-bindings.yaml`
- Modify: `bi_agent/runtime/clickhouse_query_compiler.py`
- Modify: `bi_agent/runtime/query_completeness.py`
- Create: `bi_agent/runtime/current_data_coverage.py`
- Test: `tests/phase4/test_current_data_query_coverage.py`
- Test: `tests/phase4/test_clickhouse_query_compiler.py`
- Test: `tests/phase4/test_query_completeness.py`

**Interfaces:**
- Produces `CurrentDataCoverageCase` and `current_data_coverage_cases(registry) -> tuple[CurrentDataCoverageCase, ...]`.
- Every case describes dataset, metric, dimension set, query family, required windows, expected claim ceiling, and supported/degraded state.
- Generates typed query contracts only from reviewed source adapters and schema fields.

- [x] **Step 1: Write a generated closure test**

```python
def test_every_supported_current_data_case_compiles_and_has_completeness_contract(self):
    registry = canonical_registry()
    for case in current_data_coverage_cases(registry):
        with self.subTest(case=case.case_id):
            if case.expected_state == "supported":
                compiled = compile_clickhouse_query(case.query_contract, case.snapshots, registry=registry)
                self.assertTrue(compiled.sql_text.startswith("SELECT"))
                self.assertNotIn("now(", compiled.sql_text.lower())
                self.assertEqual(
                    tuple(case.query_contract.result_shape.required_window_ids),
                    tuple(case.query_contract.window_refs),
                )
            else:
                self.assertTrue(case.gap_type)
                self.assertTrue(case.owner)
```

The generated set must cover each currently registered source adapter, each
query family referenced by an obligation, overall/channel source pairs, and all
fixed window roles.

- [x] **Step 2: Run tests and confirm red**

```bash
/tmp/waje-bi-v2-py312/bin/python3 -m pytest \
  tests/phase4/test_current_data_query_coverage.py \
  tests/phase4/test_clickhouse_query_compiler.py \
  tests/phase4/test_query_completeness.py -q
```

Expected: FAIL on missing coverage enumerator and any currently unclosed reviewed adapter/query pair.

- [x] **Step 3: Implement coverage enumeration**

Build cases from registry contracts rather than hard-coded evaluation questions:

```python
@dataclass(frozen=True)
class CurrentDataCoverageCase:
    case_id: str
    dataset_ids: tuple[str, ...]
    metric_ids: tuple[str, ...]
    dimension_ids: tuple[str, ...]
    query_family: str
    required_window_ids: tuple[str, ...]
    expected_state: str
    claim_ceiling: str
    gap_type: str = ""
    owner: str = ""
```

Unsupported contract combinations stay in the case list as explicit gaps.

- [x] **Step 4: Close only schema-backed query adapters**

Add or correct source adapters for fields already present in active source
contracts. Cover market overall/channel, gameplay overall/channel, external
events, and the registered paid-success snapshot. Do not map gameplay payment
aliases to paid facts and do not invent payment-attempt fields.

Each new query family declares result shape, unique key, grain, source fields,
window policy, reconciliation expectation, and provider bounds.

- [x] **Step 5: Validate completeness and reconciliation**

Every supported case must enforce execution success, snapshots, required
fields/windows, complete days, unique keys, provider non-truncation, and any
required overall/channel reconciliation. Contract-allowed partial context must
remain `partial/degraded`; it cannot be marked `complete/ready`.

- [x] **Step 6: Run contract, query, and completeness regressions**

```bash
ruby tools/contracts/validate-contracts.rb
/tmp/waje-bi-v2-py312/bin/python3 -m pytest \
  tests/phase4/test_current_data_query_coverage.py \
  tests/phase4/test_clickhouse_query_compiler.py \
  tests/phase4/test_query_completeness.py \
  tests/phase4/test_market_dashboard_ingestion.py \
  tests/phase4/test_gameplay_event_ingestion.py -q
```

- [x] **Step 7: Independent review and commit**

```bash
git add contracts/runtime/clickhouse-analysis-bindings.yaml \
  bi_agent/runtime/clickhouse_query_compiler.py \
  bi_agent/runtime/query_completeness.py \
  bi_agent/runtime/current_data_coverage.py \
  tests/phase4/test_current_data_query_coverage.py \
  tests/phase4/test_clickhouse_query_compiler.py \
  tests/phase4/test_query_completeness.py
git commit -m "feat: close current data query coverage"
```

---

### Task 6: Preserve Partial Verified Answers, Reuse, and Clarification State

**Files:**
- Modify: `bi_agent/runtime/answer_package.py`
- Modify: `bi_agent/runtime/langgraph_workflow.py`
- Modify: `bi_agent/runtime/analysis_assets.py`
- Modify: `bi_agent/conversation/agent_core.py`
- Test: `tests/phase4/test_workflow_artifacts_answer.py`
- Test: `tests/phase4/test_llm_workflow.py`
- Test: `tests/phase7/test_analysis_assets.py`
- Test: `tests/phase7/test_agent_core_bridge.py`

**Interfaces:**
- Produces a typed `available_evidence_brief` containing verified claims, unresolved obligations, scoped gaps, omitted factors, and next actions.
- Preserves verified claim provenance and exact ReuseDecision refs through final delivery.
- Persists an accepted degraded-route decision so the same source gap does not reopen clarification in the resumed topic.
- Persists a signed source-material authority envelope in the source run request and resolves it with the immutable AnalysisContract and clarification outcome. The envelope preserves ordered families and targets plus explicit components, dimensions, context sources, claim intents, scope, and canonicalized time-window material without reverse-engineering compiler dependency closure. Explicit canonical intent baselines remain separate from reviewed route baselines; terminal and nonterminal resume share one closed canonicalizer for stable aliases and typed shapes. Mixed candidates retain every mapped route baseline independent of input order. Previous-day narratives accept exact aliases and closed metric-suffix labels only; bare ranges, conjunctions, alternatives, and other composites cannot imply a canonical baseline. Typed window and lag parameters are binding and cannot be masked by a direct id. When selected time-window baselines and candidates coexist, every selected id must be a subset of the authoritative candidate list. Unknown, contradictory, partially overlapping, or unreviewed forms fail closed. Terminal resume rejects signed time-window or canonical-baseline drift and overwrites mutable baseline candidates with reviewed signed route ids, preserving route expansion without relabeling it as original intent. The source AnalysisContract projects exact target-day semantics, canonical `as_of`, business timezone, permission scope, and every resolved fixed-window bound into the signed envelope; narrative `time_window` is never used to guess compiler target material. Before AnalysisRuntime compilation, resume exact-validates every repeated signed material axis and rebuilds the final proposal from signed families, targets, components, dimensions, reviewed route baselines, context-source roles, claim intents, diagnostic tags, scope, target, clock, and permission material. It rejects role elevation, role reduction, and caller drift in `as_of`, target, previous-day, rolling-seven-day, same-weekday, pattern-history, or anomaly-history bounds. Current LLM and clarification payloads may contribute only non-material suggestions.
- Persists a closed execution-material projection from the actual source AnalysisRuntimeRequest, runtime registry, AnalysisContract, QueryContracts, and CapabilityExecutionPlans. It signs filters, grain, explicit dataset requirements, metric/dimension dataset overrides, requested-context-source aliases, source accepted graph, registry version/digest, run-mode authority class, QueryContract semantic signatures, snapshot membership, and exact query-owner capability ids derived from required, optional, and validation query refs. Resume rebuilds those compiler inputs exactly; the accepted graph may differ only for capabilities named by the signed degradation choice. Compile and execute each require the current query signature/snapshot set to equal the signed source set projected through the validated current ready graph. A shared query remains required while any signed owner remains. Mapping-like or scalar values cannot be reinterpreted as exact sequence axes. The signed projection and source compiler use the same strict grain semantics, so leading/trailing whitespace fails before source compilation and cannot be normalized only while signing. Reuse candidates and attempted signatures stay unsigned run metadata and grant no material authority.
- Gap-action route pruning preserves the complete signed claim-intent and context-source request until the terminal proposal passes exact authority validation. The compiler alone partitions that request by the remaining capability graph and emits typed gaps for unsupported portions; route helpers cannot silently delete signed material axes before compilation.
- Publishes successful verified result refs as non-authoritative conversation
  candidates, then lets AnalysisRuntime validate the persisted query, rows,
  snapshots/releases, permission, windows, schema fingerprint, completeness,
  and binding chain against the current compiled contract. Only a validated
  current-run cache materialization becomes `ReuseDecision=reuse`; mismatches
  are recorded as rerun decisions and execute normally.

- [x] **Step 1: Write failing partial-value and resume tests**

```python
def test_verified_market_claim_survives_unbound_paid_and_event_paths(self):
    package = build_answer_package_from_state(partial_verified_state())
    self.assertEqual(package["delivery_claim_ids"], ["claim:market:1"])
    self.assertIn("result:market:1", package["delivery_evidence_refs"])
    self.assertIn("payment_attempt", package["final_explanation"]["explanation"])
    self.assertNotIn("all sources unavailable", package["final_answer"])

def test_accepted_degraded_route_resumes_once_and_does_not_reask_same_gap(self):
    first = core.run_message(
        thread_id="thread-existing-data-route",
        user_message="分析昨天付费金额变化，并保留现有大盘证据。",
        analysis_context=fixed_analysis_context(),
    )
    resumed = core.run_message(
        thread_id="thread-existing-data-route",
        user_message=first["clarification"]["recommended_assumption"]["option"],
        analysis_context=fixed_analysis_context(),
    )
    self.assertEqual(first["topic_id"], resumed["topic_id"])
    self.assertEqual(resumed["status"], "completed")
    self.assertEqual(resumed["clarification"], None)
```

Add tests that reject reuse when any contract signature, source release, window,
permission scope, schema fingerprint, or completeness state differs.

The positive platform reuse case may change the requested primary baseline only
when the newly compiled physical QueryContract still has the same exact semantic
signature and the prior result covers every required fixed window and field.
Canonicalize fixed-window set ordering before query signing, regenerate the
current AnalysisContract, binding, claim, and answer, and never reuse the prior
claim itself. A changed required-window set is a negative rerun case. Normalize
stable full-sample business aliases before compiling the signature, without
adding case-id or sentence-specific rules.

Bind that positive eval to `market_health_compare` on the existing
`market_dashboard` release. Both turns declare the same metric, full-sample
scope, permission, release membership, and fixed-window set, with baseline
priority reversed. Keep the unavailable-as-of `paid_order_success` /
`compare_periods` route as an explicit typed blocker. Required reuse passes only
when the run-matched internal `admin_audit` contains one exact ReuseDecision and
the conversation store resolves its source result to a signed published
candidate owned by a completed earlier run in the same evaluated thread and
topic. The reviewer validates that candidate's result-ref record and signature,
the source authoritative query chain, and a distinct current AnalysisContract
whose binding is ready and whose reports are complete and analysis-ready. It
also proves one candidate signature across the signed candidate, ReuseDecision,
and current QueryExecutionRecord cache metadata; resolves the current run owner
from the store against the evaluated run/thread/topic; and requires every
current QueryContract to be owned by the accepted current AnalysisContract. It
then proves the current result, capability binding, expected dataset provenance,
row hash/count, release, and permission identity. Add rejection tests for a
nested or unpublished marker, same-run/cross-topic/non-prior lineage, each
candidate-signature layer, current store-owner drift, integrity-clean current
QueryContract-owner drift, degraded current binding, non-ready current
completeness, and each tampered ref.

- [x] **Step 2: Run tests and confirm red**

```bash
/tmp/waje-bi-v2-py312/bin/python3 -m pytest \
  tests/phase4/test_workflow_artifacts_answer.py \
  tests/phase7/test_analysis_assets.py \
  tests/phase7/test_agent_core_bridge.py -k "partial or reuse or clarification" -q
```

Expected: FAIL where generic blocked output replaces available verified evidence or the same gap reopens clarification.

- [x] **Step 3: Build a typed available-evidence brief**

Before final synthesis, project only authority-backed facts:

```python
def build_available_evidence_brief(
    *,
    verified_claims: Sequence[Mapping[str, Any]],
    capability_bindings: Sequence[Mapping[str, Any]],
    contract_gaps: Sequence[Mapping[str, Any]],
    obligation_resolution: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "verified_claims": [dict(item) for item in verified_claims],
        "verified_capabilities": sorted({
            str(item["capability_id"])
            for item in capability_bindings
            if item.get("status") in {"ready", "degraded"}
        }),
        "unresolved_obligations": [
            str(item) for item in obligation_resolution.get("unresolved", ())
        ],
        "omitted_factors": [
            str(item.get("dataset_id") or item.get("gap_id"))
            for item in contract_gaps
        ],
        "business_next_actions": [
            str(action)
            for item in contract_gaps
            for action in item.get("repair_options", ())
        ],
    }
```

The real final-summary LLM consumes this brief. Local code validates that all
verified claims and limitations survive; it does not write the business narrative.

- [x] **Step 4: Tighten ReuseDecision and clarification persistence**

Reuse exact assets and results only after all signature fields match. Persist
the accepted obligation/degradation choice in the clarification outcome and
include it in the resumed analysis proposal, accepted graph, Answer Package,
ContextManifest, and verifier inputs.

At the source run's clarification boundary, persist a versioned, exact-shape,
signed material-authority envelope under PostgreSQL `analysis_runs.request`.
Bind it to the source run, thread, and topic. Keep the source intent's primary
and ordered families, primary and ordered targets, explicit components,
dimensions, baselines, context sources, claim intents, and scope separate from
the exact route material slots, including diagnostic requirement tags. The
envelope also includes exact typed local diagnostic rejection history as signed
route-control metadata. The PostgreSQL/InMemory resume
authority resolvers must validate and return this stored envelope together with
the signed AnalysisContract and clarification outcome. Resume must compare all
mutable context copies with the envelope and fail closed on drift.

Do not reconstruct explicit material axes from compiled AnalysisContract
dependency fields. In particular, `scope.requested_metric_ids` may contain
formula or capability dependency closure, while dataset requirements do not
preserve whether a dataset was an explicit context request. Add regression
tests for an empty explicit component list with a non-empty driver dependency
closure and for a metric-source dataset that also has a context role.

Cross-check only the reversible contract overlap: ordered question families,
ordered targets, and material scope must match the signed material envelope in
the shared authority validator and compiler defense-in-depth path. Add separate
negative tests for each mismatch while retaining the dependency-closure and
dual-role positive cases above.

For ordinary `inherit_current` follow-ups, persist a completed source run's
signed material-authority envelope and the exact authoritative signed
AnalysisContract copy in the existing `analysis_runs.request` JSON only after
delivery verification and runtime-record persistence both succeed. Add matching
InMemory/PostgreSQL completed-authority resolvers with no migration. They must
reject failed or waiting runs, owner/status drift, mutable request-copy drift,
invalid contract or material signatures, and material/contract overlap drift.
The completion finalizer must atomically add exactly one immutable
`completed_material_authority_recorded` audit event whose canonical payload
binds the full material authority plus contract ref/signature and both digests.
Exact replay is idempotent; duplicate or conflicting events fail closed. The
resolver requires one event and exact event/request/authority equality,
including when request material was altered and re-signed.
Permit first publication only from `running_workflow`; reject a historical
`completed` row that lacks the immutable event. A `completed` row may enter the
finalizer only for unique exact replay. Preserve one explicit
`run_status_changed(status=completed)` audit event in the same InMemory update
or PostgreSQL transaction, and prove replay adds neither audit event again.

Stage deep-copied run and audit state in the InMemory finalizer and swap only
after all validation and event appends succeed; inject an audit-append failure
and prove no partial state escapes. In PostgreSQL, lock run, AnalysisContract,
and authority-event rows with separate `SELECT ... FOR UPDATE` statements so a
nullable `LEFT JOIN` side is never locked, then persist request authority,
status, and both events in that transaction. Require InMemory/PostgreSQL typed
missing-reason parity and reject an embedded contract signature that differs
from the authoritative contract column. The embedded signature is mandatory;
reject a missing value instead of filling it from the column. Add exact replay,
duplicate-event, and second-audit-write rollback tests for PostgreSQL.

Carry completed material authority privately on `WorkflowRunResult`. Require
the carrier for every successful result containing an AnalysisRuntime result or
persisted runtime records, and finalize it only after delivery, Answer Package,
asset, and runtime-record persistence have crossed their existing boundaries.
A missing or malformed carrier must fail the run and must not create completed
follow-up authority.

Publish result reuse candidates only after completed-authority finalization
succeeds. Add a RED proving finalizer failure with authoritative runtime records
and a valid carrier leaves zero result refs. Treat candidate publication as a
derived follow-up index: a post-finalizer index failure must preserve the
completed run and claim authority, recover the store write transaction before a
typed `followup_index_publication_failed` audit, and return a best-effort warning
with exact recovery/audit error detail if either reporting boundary is
unavailable. Add order, recovery-failure, and audit-failure tests; do not weaken
ConversationRuntime's one-bad-ref fail-closed rule to hide orphan candidates.
Expose one store recovery method: InMemory implements a no-op and PostgreSQL
calls `rollback()` so the typed audit can run after an aborted write.

ConversationRuntime must separate claim/result reuse from intent-material
continuity. A validated same-topic result whose decision is `rerun` solely for
`semantic_scope_mismatch` remains excluded from public `reuse_candidates`, but
may supply private `prior_topic_material_context` when snapshot, contract, and
permission checks have already passed. Results rejected for snapshot, contract,
or permission mismatch remain excluded. Add general RED cases spanning several
semantic mutations and assert that the claim path still reruns while the intent
provider receives the completed signed metric, scope, time window, and ordered
prior baselines as context only.

ConversationRuntime may derive private `prior_topic_material_context` only from
already validated material-context candidates whose completed source authorities
also pass the store resolver. Validate every ref, collapse duplicate refs from
one run, and merge multiple source runs only when their canonical material
projections are identical. Conflicts fail typed independent of candidate order;
latest-wins is prohibited. Add a ContextManifest material ref as
`context_only`, `can_support_claims=false`.
For each forwarded ref, require canonical equality with the store's indexed
candidate authority before using its source run. Cross-check candidate,
AnalysisContract, and execution-material permission scopes and authorize the
current role before provider invocation. One invalid ref invalidates the whole
same-run group. Preflight every completed-authority value as a Mapping before
any field access and preserve
`prior_topic_completed_authority_shape_invalid` for all non-Mapping values.

At Workflow entry, revalidate that private context and its signed contract
before invoking `business_intent`. Project target metric, scope, time window,
and ordered prior baselines only into the prompt's `bound_business_context`.
Keep the provider's complete intent output mandatory: do not copy those axes to
the public request, overwrite provider output, infer them from topic summaries,
or add a local narrative/intent fallback.
Require the provider to copy an exact canonical bound axis when the current
question does not replace it, and to return a complete canonical replacement
when it does. Null or empty required axes fail the existing LLM contract.
Cover completed persistence,
failed/waiting rejection, InMemory/PostgreSQL parity, tamper/owner/status and
contract drift, multiple refs for one run, identical/conflicting multi-run
order independence, provider-preflight failure, absence of topic-summary
fallback, and a two-turn reversed-baseline follow-up.
With private prior context present, fail closed before provider invocation when
the request repeats any non-empty top-level material axis: `question_family`,
`pattern_family`, `pattern_params`, `target_claim`, `target`, `target_metric`,
`scope`, `time_window`, `baseline`, or `baseline_candidates`. Assert zero
provider calls and the stable contract failure type for representative axes.

Declare `execution_material: dict[str, Any]` as a first-class WorkflowState
channel so the validated compile/runtime projection survives LangGraph node
boundaries into query-gap persistence. Add a cross-node RED proving the waiting
package receives the non-empty value and retain negative resume tests for
missing or tampered execution material. Do not infer or rebuild it from the
AnalysisContract and do not relax `require_execution_material=True`.

For the execution query projection, add general RED cases for two signed source
queries becoming zero or one current query while both owners remain ready, plus
the exact two-query positive case. Sign `owner_capability_ids` by resolving every
source CapabilityExecutionPlan required/optional slot and validation ref back to
its QueryContract id. Reject unknown refs, ownerless queries, and owners outside
the source accepted graph. At resume, require every query with at least one
retained owner and allow deletion only when all owners were removed by the
validated signed degradation choice. Cover a shared-query owner case. Also
reject `dict` and mapping-proxy values on dataset-requirement and requested-
context-source sequence axes so iterable keys never become authority.

Execution-material canonicalization must be identical to the compiler's source
semantics. Add a RED case for `grain=" window_id "`: the source compile must
reject it before dimension binding rather than producing an unsupported-grain
gap that the signing layer later strips away. Use one strict canonicalizer at
the compiler entry and signing projection; never repair this only in the signed
envelope.

Apply the same ordered family, ordered target, and material-scope validation to
the current terminal-resume proposal. Reject a terminal clarification payload
that changes any of those signed axes; allow an exact repeat and preserve the
existing non-terminal clarification behavior. Cover direct compiler drift and
workflow clarification-choice drift with independent failing tests.

Do not trust `clarification_resume_context.analysis_route` as the source of
obligation rejection history. At source waiting persistence, project only the
local resolver's exact rejected records into the signed route-control section.
On resume, restore trusted history from that section into workflow state and
ignore any mutation history carried by the mutable prior route. Test legal
history preservation, forged prior-route injection, unknown fields/reasons,
duplicate records, and exactly-once mutation-ledger projection.

- [x] **Step 5: Run workflow and delivery regressions**

```bash
/tmp/waje-bi-v2-py312/bin/python3 -m pytest \
  tests/phase4/test_workflow_artifacts_answer.py \
  tests/phase4/test_llm_workflow.py \
  tests/phase7/test_analysis_assets.py \
  tests/phase7/test_agent_core_bridge.py -q
```

- [x] **Step 6: Independent review and commit**

```bash
git add bi_agent/runtime/answer_package.py bi_agent/runtime/langgraph_workflow.py \
  bi_agent/runtime/analysis_assets.py bi_agent/conversation/agent_core.py \
  tests/phase4/test_workflow_artifacts_answer.py \
  tests/phase4/test_llm_workflow.py tests/phase7/test_analysis_assets.py \
  tests/phase7/test_agent_core_bridge.py
git commit -m "feat: preserve available verified analysis value"
```

---

### Task 7: Runtime Coverage Audit

**Files:**
- Create: `bi_agent/runtime/coverage_audit.py`
- Create: `tools/phase7/audit_existing_data_coverage.py`
- Create: `tests/phase7/test_existing_data_coverage_audit.py`
- Modify: `docs/phase-7-live-conversation-eval.md`

**Interfaces:**
- Consumes `RuntimeContractRegistry`, `CurrentDataCoverageCase`, and a PostgreSQL-backed `RuntimeEvidenceResolver`/snapshot release resolver.
- Produces `audit_existing_data_coverage(registry, snapshot_records, release_resolver, as_of, permission_scope) -> dict`.
- CLI writes a local JSON artifact with per-cell state, owner, impact, and next action.

- [x] **Step 1: Write failing matrix-state tests**

```python
def test_coverage_audit_reports_supported_degraded_and_excluded_cells(self):
    audit = audit_existing_data_coverage(
        canonical_registry(),
        snapshot_records=current_snapshot_records(),
        release_resolver=current_release_resolver(),
        as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
        permission_scope="analyst",
    )
    self.assertEqual(audit["cells"]["market_health_compare:market_dashboard"]["state"], "executable")
    self.assertEqual(audit["cells"]["event_evidence:external_event"]["state"], "executable")
    self.assertEqual(audit["cells"]["driver_decomposition:payment_attempt"]["state"], "source_unbound")
    self.assertEqual(audit["cells"]["event_evidence:internal_operation_event"]["owner"], "data_operations_owner")
```

- [x] **Step 2: Run tests and confirm red**

```bash
/tmp/waje-bi-v2-py312/bin/python3 -m pytest \
  tests/phase7/test_existing_data_coverage_audit.py -q
```

Expected: FAIL with missing coverage audit module.

- [x] **Step 3: Implement deterministic coverage audit**

States are exactly:

```python
COVERAGE_STATES = (
    "executable",
    "degraded",
    "source_unbound",
    "contract_partial",
    "permission_blocked",
    "snapshot_unavailable_as_of",
)
```

Each cell includes question families, capability, datasets, metrics, dimensions,
windows, evidence types, claim ceiling, current release refs, state, owner,
impact, and next action. Sort all output deterministically.

- [x] **Step 4: Add PostgreSQL-backed CLI**

```bash
set -a; source /Users/luka/work/waje-bi-v2/.env; set +a
/tmp/waje-bi-v2-py312/bin/python3 tools/phase7/audit_existing_data_coverage.py \
  --as-of 2026-06-03T12:00:00+01:00 \
  --permission-scope analyst \
  --out artifacts/phase7/existing-data-coverage/coverage.json
```

The CLI is read-only and never prints credentials.

- [x] **Step 5: Run tests and local audit**

Expected: command exits zero when the audit is structurally valid even if some
cells are source-unbound; hard resolver or contract integrity failures exit nonzero.

- [x] **Step 6: Independent review and commit**

```bash
git add bi_agent/runtime/coverage_audit.py \
  tools/phase7/audit_existing_data_coverage.py \
  tests/phase7/test_existing_data_coverage_audit.py \
  docs/phase-7-live-conversation-eval.md
git commit -m "feat: audit current data analysis coverage"
```

---

### Task 8: Dual-Track Evaluation Contracts

**Files:**
- Create: `evals/phase7/existing_data_coverage_scenarios.yaml`
- Modify: `evals/phase7/conversation_scenarios.yaml`
- Modify: `tools/phase7/run_live_conversation_system_test.py`
- Modify: `tools/phase7/review_analysis_contract_eval.py`
- Test: `tests/phase7/test_agent_core_bridge.py`
- Test: `tests/phase7/test_existing_data_coverage_audit.py`
- Modify: `docs/phase-7-live-conversation-eval.md`

**Interfaces:**
- Adds `--suite fixed-eight|platform-current-data` and obligation-aware expectation review.
- Produces `review_case_obligations(turn_record, registry) -> dict[str, Any]` for both suites.
- Every scenario declares structured intent, required/conditional capabilities, expected dataset states, allowed claim ceiling, and terminal boundary; answer wording remains unconstrained except existing hard-boundary text.
- Produces separate raw, runtime-review, quality-review, and coverage-summary artifacts.

- [x] **Step 1: Write failing suite and obligation-review tests**

```python
def test_platform_suite_covers_every_public_question_family_and_current_dataset_role(self):
    cases = load_cases("evals/phase7/existing_data_coverage_scenarios.yaml")
    self.assertEqual({case["question_family"] for case in cases}, set(load_recipe_registry()))
    self.assertTrue({"paid_order_success", "market_dashboard", "market_dashboard_channel", "gameplay", "gameplay_channel", "external_event"}.issubset(
        {dataset for case in cases for dataset in case["expected_dataset_states"]}
    ))

def test_expectation_review_uses_obligation_contract_not_sentence_specific_lists(self):
    review = review_case_obligations(turn_result(), canonical_registry())
    self.assertEqual(review["missing_required_capabilities"], [])
    self.assertNotIn("final_answer_contains", review)
```

- [x] **Step 2: Run tests and confirm red**

```bash
/tmp/waje-bi-v2-py312/bin/python3 -m pytest \
  tests/phase7/test_agent_core_bridge.py \
  tests/phase7/test_existing_data_coverage_audit.py -k "platform_suite or obligation" -q
```

- [x] **Step 3: Create the platform scenario matrix**

Use real user wording plus structured expectation packages. Include at least one
case for each question family, one for each current dataset role, one permission
boundary, one contract-allowed partial context, one reuse case, and one
clarification-resume case. The scenario file cannot embed expected LLM prose.

- [x] **Step 4: Make fixed-eight expectation review obligation-aware**

The fixed eight questions remain unchanged. Required capabilities come from the
obligation resolver over the persisted ordered canonical family set. Typed
scenario requirements remain authored expectations and are reported against
the derived set without entering hard authority. Missing excluded inputs
produce expected typed gaps; missing current-data obligations fail strict
acceptance.

The persisted set may contain a primary business family plus orthogonal trust,
evidence, or data-quality families. Resolve all entries, union capability
obligations with registry ordering and stable de-duplication, and retain
per-family capability provenance. Report authored and persisted family sets
separately. Unknown, malformed, or duplicate entries fail closed. More than one
distinct canonical family is valid and is not ambiguous by itself.

Hard acceptance is derived from persisted authority for every required
capability. Only `executed`, `degraded`, or `blocked` are terminal outcomes;
`blocked` requires a typed contract gap whose `affected_capabilities` contains
the exact capability. Accepted-graph membership alone is not authority, and
`unobserved` or `missing_route` fail the case.
The compiled contract scope persists requested metric and dimension IDs even
when their bindings cannot be produced. Evaluation validates blocked gaps with
the complete compiler-owned gap-ID grammar and binds each object ID to those
persisted requested IDs or successful bindings; marker-prefix checks are not
accepted as authority.
When the runtime has not materialized a capability-binding record, evaluation
may derive `executed` or `degraded` from the run-matched persisted admin audit.
It must validate the capability contract ref/signature and every required plan
slot through exact query-contract signatures, succeeded result refs, and linked
completeness reports. Runtime summary flags and client-package fallbacks remain
non-authoritative.
Resolve the evaluated AnalysisContract from the conversation store by exact run
owner and require one signed persisted contract. Keep the local artifact path
and payload mandatory for replay, but accept either its exact embedded contract
or its exact `analysis_runtime_persistence.analysis_contract_ref` as the
cross-check shape. Do not require one Answer Package nesting layout and do not
derive the contract id from the run-id format. Missing resolver, missing or
ambiguous rows, run/ref/signature/content drift, artifact failures, and
client-only fallback all fail closed. Add RED coverage for completed/resumed
packages that retain only the persisted contract ref, plus every negative
boundary above.
If the coverage audit resolves expected capability states, strict acceptance
uses the exact capability-state/outcome pairs. Dataset-level observed state and
typed-gap aggregates remain visible for compatibility and reporting, but do not
gate that case; this prevents a partial sibling capability on the same dataset
from masking an executed required capability. Cases without capability-state
authority retain the dataset-level gate.
Terminal-boundary resolution uses the same mode. With capability-state
authority, resolve only from those scoped states and the reviewed authored
boundary; dataset aggregate state cannot promote an unrelated partial sibling
into `contract_allowed_partial`. Dataset-only legacy cases retain the previous
boundary resolver.

Treat scenario `expected_dataset_states` as reviewed authored hypotheses and
matrix-role declarations, not as current runtime authority. Before reviewing a
real fixed or platform turn, resolve the applicable capability/dataset cells
from the same PostgreSQL snapshot/release authority and fixed `as_of` used by
the coverage audit. Report authored, authority-resolved, and actually observed
states separately. Hard acceptance compares runtime observations with the
authority-resolved states and fails closed when authority cannot resolve a
declared dataset role; a stale authored hypothesis is a contract-review finding
and cannot turn an unavailable-as-of release into an executable obligation. If
a role has authority cells but no reviewed capability or question-family link,
use the most conservative state across those cells and report the ambiguous
role; never select an optimistic cell to satisfy the authored hypothesis.

- [x] **Step 5: Extend review output**

Each suite review reports:

```json
{
  "obligation_coverage": {"required": 0, "executed": 0, "degraded": 0, "missing": 0},
  "dataset_coverage": {},
  "runtime_correctness": {},
  "answer_quality": {},
  "final_answer_audit_coverage": {},
  "clarification_resume": {},
  "reuse_coverage": {}
}
```

Quality fields remain nonblocking; runtime correctness and obligation coverage
remain hard acceptance dimensions.

- [x] **Step 6: Run deterministic suite tests**

```bash
/tmp/waje-bi-v2-py312/bin/python3 -m pytest \
  tests/phase7/test_agent_core_bridge.py \
  tests/phase7/test_existing_data_coverage_audit.py \
  tests/phase4/test_llm_workflow.py -q
```

- [x] **Step 7: Independent review and commit**

```bash
git add evals/phase7/existing_data_coverage_scenarios.yaml \
  evals/phase7/conversation_scenarios.yaml \
  tools/phase7/run_live_conversation_system_test.py \
  tools/phase7/review_analysis_contract_eval.py \
  tests/phase7/test_agent_core_bridge.py \
  tests/phase7/test_existing_data_coverage_audit.py \
  docs/phase-7-live-conversation-eval.md
git commit -m "test: define dual-track current data evaluation"
```

---

### Task 9: Real Evaluation, Quality Comparison, and Delivery Audit

**Files:**
- Modify: `docs/phase-7-live-conversation-eval.md`
- Modify: `docs/superpowers/plans/2026-07-12-existing-data-dual-track-coverage.md` checkbox status only.
- Local only: `artifacts/phase7/existing-data-coverage/`
- Local only: `artifacts/phase7/existing-data-fixed-eight-run-1/`
- Local only: `artifacts/phase7/existing-data-fixed-eight-run-2/`
- Local only: `artifacts/phase7/existing-data-platform-run-1/`

**Interfaces:**
- Consumes all Task 1-8 contracts, runtime behavior, and eval suites.
- Produces the final local coverage audit, two fixed-eight artifacts, one platform artifact, three quality reviews, a baseline comparison, and a durable delivery audit.
- Resolves real-eval query, binding, verified-claim, and trusted-provenance authority from the conversation store's PostgreSQL runtime-evidence resolver. A process-local query resolver is insufficient for persisted claim review and must fail explicitly when the store resolver is unavailable.

- [x] **Step 1: Run final automated verification**

```bash
set -a; source /Users/luka/work/waje-bi-v2/.env; set +a
ruby tools/contracts/validate-contracts.rb
ruby tools/runtime/load-conversation-runtime-schema.rb
/tmp/waje-bi-v2-py312/bin/python3 -m compileall -q bi_agent tools/phase7
/tmp/waje-bi-v2-py312/bin/python3 -m pytest tests/phase4 tests/phase7 tests/phase8 -q
npm run build
git diff --check
```

Expected: contract, schema, compile, test, build, and diff checks pass. Record exact counts.

- [x] **Step 2: Generate the runtime coverage audit**

```bash
/tmp/waje-bi-v2-py312/bin/python3 tools/phase7/audit_existing_data_coverage.py \
  --as-of 2026-06-03T12:00:00+01:00 \
  --permission-scope analyst \
  --out artifacts/phase7/existing-data-coverage/coverage.json
```

- [x] **Step 3: Run fixed-eight evaluation twice with real services**

```bash
export WAJE_LLM_TIMEOUT_SECONDS=300
/tmp/waje-bi-v2-py312/bin/python3 tools/phase7/run_live_conversation_system_test.py \
  --suite fixed-eight --case paid_amount_revenue_diagnostics_8_question_set \
  --real-llm --real-clickhouse --strict-quality \
  --artifact-dir artifacts/phase7/existing-data-fixed-eight-run-1
/tmp/waje-bi-v2-py312/bin/python3 tools/phase7/run_live_conversation_system_test.py \
  --suite fixed-eight --case paid_amount_revenue_diagnostics_8_question_set \
  --real-llm --real-clickhouse --strict-quality \
  --artifact-dir artifacts/phase7/existing-data-fixed-eight-run-2
```

Wait for every high-value LLM node. A nonzero strict exit is retained and
explained; do not convert it to success.

- [x] **Step 4: Run the platform current-data suite**

```bash
/tmp/waje-bi-v2-py312/bin/python3 tools/phase7/run_live_conversation_system_test.py \
  --suite platform-current-data --real-llm --real-clickhouse --strict-quality \
  --artifact-dir artifacts/phase7/existing-data-platform-run-1
```

- [x] **Step 5: Review and compare all artifacts**

```bash
/tmp/waje-bi-v2-py312/bin/python3 tools/phase7/review_analysis_contract_eval.py \
  artifacts/phase7/existing-data-fixed-eight-run-1/paid_amount_revenue_diagnostics_8_question_set.json \
  --baseline /Users/luka/.codex/worktrees/cd52/waje-bi-v2/artifacts/phase7/live-conversation-fixed-analysis-contracts-run-1/paid_amount_revenue_diagnostics_8_question_set.json \
  --out artifacts/phase7/existing-data-fixed-eight-run-1/review.json
/tmp/waje-bi-v2-py312/bin/python3 tools/phase7/review_analysis_contract_eval.py \
  artifacts/phase7/existing-data-fixed-eight-run-2/paid_amount_revenue_diagnostics_8_question_set.json \
  --baseline artifacts/phase7/existing-data-fixed-eight-run-1/paid_amount_revenue_diagnostics_8_question_set.json \
  --out artifacts/phase7/existing-data-fixed-eight-run-2/review.json
```

Run the same tool for each platform case artifact. Confirm every score uses a
run-id-matched internal final-LLM audit.

- [x] **Step 6: Audit acceptance and remaining gaps**

The final audit records:

- current-data obligation execution/degradation/missing counts;
- runtime-verified questions and result refs;
- dataset, metric, dimension, and window coverage;
- clarification resume and reuse outcomes;
- quality score deltas;
- excluded input gaps with owner/impact/next action;
- any new runtime capability gap with a general contract-level repair; and
- confirmation that artifacts remain untracked.

- [x] **Step 6a: Close the production/live LLM runtime policy review**

Add general failure-class tests, then enforce all of these as one runtime
boundary:

- high-value narrative and claim fields are never filled from
  `NARRATIVE_FALLBACKS` or another local template;
- production/live material intent cannot silently default or rewrite family,
  metric, pattern family, scope, time window, or target claim;
- production/live business-intent input and prompt expose the shared baseline
  registry's closed `allowed_baseline_ids` plus business labels and semantics;
  baseline candidates must be an explicit array of exact string ids copied from
  that vocabulary in user priority order, must exclude the target window, and
  use `[]` when no reviewed baseline is requested. The local compatibility
  boundary continues canonicalizing reviewed aliases and typed shapes, while
  duplicate, unknown, conflicting, nested, and wrong-container values fail
  before intent is stored;
- business-intent input exposes the reviewed `allowed_scope_types` (currently
  `full_sample` for the existing-data public path), the prompt requires exact
  machine scope ids, and production/live rejects scope values outside that
  closed set after generic alias canonicalization. Segment and dimension intent
  remains in requested dimensions and filters;
- `ambiguous_slots` lists only still-unbound material decisions; target metric,
  scope, and time window already fixed by canonical output or bound context
  cannot be marked ambiguous in the same response. This is an LLM output
  coherence instruction, while advisory list containers still fail closed;
- every clarification-producing surface imports one neutral
  `tell the agent to do differently` contract constant. LLM surfaces receive it
  in delimited input and mark it as exempt from Chinese narrative translation;
  structured conversation options use the exact final label and a Chinese
  description. Boundary decisions that need input contain exactly one question,
  2-3 business options, the exact final escape, and a recommendation copied from
  a business option; all other boundary statuses carry `[]`. Translation,
  paraphrase, case or whitespace changes, relocation, aliases, and local fallback
  fail closed while business option wording remains Chinese;
- each workflow business node calls the shared LLM client once, while the
  provider retains the sole three-attempt retry loop; and
- provider subprocess IPC must be received and drained before child join, with
  success/error/abnormal-exit cleanup, a bounded receiver cleanup join, one
  receive-and-join deadline, and child termination only after expiry of an
  explicitly configured positive timeout; and
- terminal explanation and final-summary validation fail typed after that one
  node call, while delivery verifier repair has at most one explicit repair
  call followed by one hard re-verification.

Run focused RED/GREEN tests, the complete LLM workflow suite, provider-client
tests, and the affected conversation and obligation regressions. This repair
must cover runtime classes and cannot key on an eval sentence, case id, or one
provider response.

- [ ] **Step 7: Final independent code and artifact review**

The reviewer checks the complete Task 1-9 diff, real artifacts, authority chains,
contract coverage, absence of sentence-specific rules, and documentation
accuracy. Resolve every P0-P2 issue and any P3 that can affect acceptance.

- [x] **Step 8: Commit Task 9**

```bash
git add docs/phase-7-live-conversation-eval.md \
  docs/superpowers/plans/2026-07-12-existing-data-dual-track-coverage.md
git commit -m "docs: audit existing data dual-track coverage"
```

Do not add `artifacts/`.

## Final Handoff

Report:

- Task 1-9 commit hashes;
- exact test, contract, schema, build, coverage-audit, and eval commands/results;
- fixed Run 1/Run 2 and platform artifact paths;
- obligation and dataset coverage changes from the prior baseline;
- quality score changes;
- published paid-success snapshot/release refs, or exact validation blocker;
- remaining excluded-input and capability gaps with owner, impact, and next action;
- independent review outcome; and
- confirmation that no evaluation-specific runtime rule or artifact was committed.
