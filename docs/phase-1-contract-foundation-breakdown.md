# Phase 1 Contract Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the versioned contract foundation that lets WAJE BI v2 review factors, capabilities, evidence limits, and launch readiness before query execution.

**Architecture:** Phase 1 creates source-of-truth contract artifacts in the repo and review artifacts for business/data owners. It keeps final database tables, runtime mirror API, and query execution out of scope. The outputs feed Phase 2 graph compiler, Phase 3 semantic query/evidence, and launch acceptance.

**Tech Stack:** Versioned repo files, Markdown review docs, YAML or JSON contract sources, lightweight validation scripts in the existing project toolchain.

---

## Current Repo Fact

- The workspace has PRD and decision docs.
- SSOT source is present at `contracts/ssot/付费金额影响因子分析.mm`.
- Mechanical SSOT extract is present at `contracts/ssot/paid-amount-factors.extract.md`.
- Future paid order detail template is present at `contracts/source-templates/付费订单明细模板.xlsx`.
- Mechanical source-template extract is present at `contracts/source-templates/paid-order-detail-template.extract.md`.
- `contracts/` currently contains the SSOT source intake path, source template intake path, extract artifacts, accepted paid-order source contract, and profiled source contracts for 大盘, 玩法, and external events.

Phase 1 starts by reviewing the SSOT source snapshot and extract. Ledger work should trace back to that source.

## Scope

**In scope:**

- SSOT source intake for `付费金额影响因子分析.mm`.
- Metric contract source for `付费金额` and required formula paths.
- Dimension, event, static assumption, and missing-contract source records.
- Factor ledger concept source using factor records plus capability-support records.
- Capability card source for all eight baseline capabilities.
- Review artifact for business owner and data/engineering owner.
- Lint/reconciliation checks that catch missing SSOT nodes, invalid states, unsupported claim/evidence type, unsupported wording limit, and missing backlog records.

**Out of scope:**

- Final Postgres table design.
- Runtime mirror API schema.
- ClickHouse query planning.
- Capability execution implementation.
- Frontend review UI.

## Proposed Source Layout

This is repo source layout, not final storage design.

```text
contracts/
  README.md
  ssot/
    付费金额影响因子分析.mm
    paid-amount-factors.extract.md
  source-templates/
    付费订单明细模板.xlsx
    paid-order-detail-template.extract.md
  metrics/
    paid-amount.metric.yaml
  dimensions/
    dimensions.yaml
  events/
    events.yaml
  assumptions/
    payday.assumption.yaml
  backlog/
    missing-contracts.yaml
  ledger/
    factor-ledger.yaml
    capability-support.yaml
    ssot-node-reconciliation.yaml
  capabilities/
    pattern-scan.yaml
    formula-decompose.yaml
    joint-attribution.yaml
    event-evidence.yaml
    outlier-scan.yaml
    segment-bridge.yaml
    data-quality-check.yaml
    answer-verify.yaml
docs/reviews/
  phase-1-factor-ledger-review.md
tools/contracts/
  validate-contracts.rb
```

Default to YAML for reviewability. If the team chooses JSON later, keep the same ownership boundaries and review fields.

## Technical Design Decisions To Make First

- Contract source format: YAML by default, JSON only if strict machine validation becomes more important than manual review.
- `.mm` handling: commit original `.mm`, commit exported tree, or commit both.
- Review artifact format: Markdown table, CSV, or generated sheet export.
- Validation runner: TypeScript script under the existing project; Python can take over when BI Agent Core starts.
- Owner workflow: who approves business meaning, who approves data contract state, and how accepted source changes are reviewed.

## Issue Breakdown

### CF-01: SSOT Source Intake

**Business outcome:** The team can point to the exact factor source used for Phase 1 review.

**Deliverables:**

- [x] Add the `.mm` source or an exported snapshot under `contracts/ssot/`.
- [x] Add `contracts/ssot/paid-amount-factors.extract.md` with extracted metric, formula, factor, dimension, event, and missing-contract nodes.
- [x] Record source date, owner, and extraction method.

**Acceptance:**

- [x] Every Phase 1 ledger entry can trace back to an SSOT node or an explicit source note.
- [x] Missing source areas are listed as review gaps.

### CF-02: Contract Source Format And Vocabulary

**Business outcome:** Everyone uses the same state names and claim boundaries before ledger review starts.

**Deliverables:**

- [x] Create `contracts/README.md`.
- [x] Define shared vocabularies for `business_evidence_state`, `data_contract_state`, evidence type, strength, and wording limit.
- [x] Include the agreed `data_contract_state` values: `contract_backed`, `evidence_linked`, `static_assumption`, `missing_contract`, `unsupported_grain`, `out_of_scope_for_now`.
- [x] Define fixed restricted-output and source-connection access boundaries separately from evidence and data-contract states.

**Acceptance:**

- [x] Contract files can reference only the agreed state names.
- [x] Reviewers can distinguish data availability from business evidence strength.

### CF-03: `付费金额` Metric Contract

**Business outcome:** Paid amount explanations use one reviewed metric identity and formula boundary.

**Deliverables:**

- [x] Create `contracts/metrics/paid-amount.metric.yaml`.
- [x] Capture business meaning, formula paths, time semantics, supported grains, baseline compatibility, cumulative-value warning, and known gaps.
- [x] Include required formula components such as paid users, paid order count, success rate, average paid amount, or the approved local equivalents.

**Acceptance:**

- [x] `formula_decompose` can reference the metric contract without using physical database schema.
- [x] Cumulative-value misuse is explicitly guarded.
- [x] Unsupported formula paths are linked to missing-contract backlog.

### CF-04: Factor Master Extraction

**Business outcome:** Every SSOT factor has one reviewable business record.

**Deliverables:**

- [x] Create `contracts/ledger/factor-ledger.yaml`.
- [x] Add factor master records for payment/order metrics, user factors, channel/payment factors, geo/device/environment, product/operation events, failure reasons, holidays, payday, external events, and black-swan candidates found in SSOT.
- [x] For each factor, include business meaning, owner/review status, known gaps, and upgrade path.

**Acceptance:**

- [ ] Every extracted SSOT node is represented or explicitly marked out of Phase 1 source scope.
- [x] No factor has unknown owner/review status.

### CF-05: Capability Support Records

**Business outcome:** Runtime planning can tell which factor supports which claim type under which capability.

**Deliverables:**

- [x] Create `contracts/ledger/capability-support.yaml`.
- [x] Express support by factor, question family, capability, grain, allowed claim/evidence type, allowed strength or wording limit, and data contract state.
- [x] Cover the eight question families and eight baseline capabilities at representative launch depth.

**Acceptance:**

- [x] A factor can support aggregate analysis while being limited for segment-level analysis.
- [x] Missing contracts, fixed restricted-output and source-access limits, unsupported grains, and out-of-scope states are visible.
- [x] Launch acceptance can derive representative matrix cells from support records.

### CF-06: Dimension, Event, Assumption, And Backlog Sources

**Business outcome:** Candidate mechanisms and data gaps are explicit before graph planning.

**Deliverables:**

- [x] Create dimension source records.
- [x] Create event source records for calendar events, campaigns, product versions, operations, external events, and black-swan candidates where available.
- [x] Create `contracts/assumptions/payday.assumption.yaml` for universal 25..30 payday dimension use.
- [x] Create `contracts/backlog/missing-contracts.yaml`.
- [x] Add profiled source records for 大盘, 玩法整体/分包渠道, and old WAJE external events workbook.
- [x] Add profiled source record for old WAJE competitor ranking CSV.

**Acceptance:**

- [x] Payday supports candidate mechanism wording as a universal 25..30 dimension.
- [x] Payday wording is controlled by evidence strength; Phase 1 keeps one universal 25..30 payday dimension.
- [x] Every missing or unsupported factor has a backlog or limitation record.
- [x] User-confirmed unavailable source areas are recorded in backlog and review artifact.

### CF-07: Baseline Capability Cards

**Business outcome:** LLM planning and local compiler lint use the same capability boundaries.

**Deliverables:**

- [x] Create eight capability card source files under `contracts/capabilities/`.
- [x] Each card includes business use, non-use, required parameters, optional parameters, evidence payload, lint severity, degradation output, verifier hooks, and typical question families.
- [x] Include cards for `pattern_scan`, `formula_decompose`, `joint_attribution`, `event_evidence`, `outlier_scan`, `segment_bridge`, `data_quality_check`, and `answer_verify`.

**Acceptance:**

- [x] Cards do not expose physical database schema to the LLM.
- [x] Cards can drive graph compiler validation in Phase 2.
- [x] Cards match PRD capability names exactly.

### CF-08: Factor Review Artifact

**Business outcome:** Business and data owners can review support state without reading raw contract files.

**Deliverables:**

- [x] Create `docs/reviews/phase-1-factor-ledger-review.md`.
- [x] Include one review row per SSOT factor or factor group.
- [x] Show business meaning, data source/status, supported capabilities, supported grain, allowed claim/evidence type, allowed strength or wording limit, gaps, owner, and launch priority.

**Acceptance:**

- [x] Business owner can approve meaning and claim boundary.
- [x] Data/engineering owner can approve data contract state, grain, fixed restricted-output safety, source-connection access, and feasibility.
- [x] Review decisions can be copied back into versioned contract source.

### CF-09: Reconciliation And Lint Checks

**Business outcome:** Phase 1 prevents silent SSOT gaps before runtime exists.

**Deliverables:**

- [x] Add a lightweight validation command under `tools/contracts/`.
- [x] Check that every extracted SSOT node has a generated factor-group map or explicit out-of-scope status.
- [x] Check that every support record uses agreed states and capability names.
- [x] Check that missing-contract states have backlog entries.
- [x] Check that static assumptions have owner, source, valid window, refresh rule, and allowed wording limit.

**Acceptance:**

- [x] Validation fails on invalid ledger status.
- [x] Validation fails on missing backlog for `missing_contract`.
- [x] Validation fails on capability names outside the eight baseline capabilities.

### CF-10: Phase 1 Signoff Package

**Business outcome:** Phase 2 can build the compiler and runtime against reviewed contract artifacts.

**Deliverables:**

- [x] Add a Phase 1 signoff section to `docs/reviews/phase-1-factor-ledger-review.md`.
- [x] Summarize unresolved missing contracts, unsupported grains, fixed restricted-output/source-access limits, and out-of-scope factors.
- [x] List contract versions or source file references that Phase 2 should pin.
- [x] Summarize latest source coverage for 大盘, 玩法, external events, and explicitly unavailable factor areas.

**Acceptance:**

- [x] Every relevant SSOT node has an accepted initial generated ledger status.
- [x] Every baseline capability has a card.
- [x] Every missing or limited path is visible as backlog, limitation, or out-of-scope decision.
- [x] Phase 2 graph compiler can consume the contract source without inventing business semantics.

### CF-09 Initial Map Status

The current validation command checks parseability, enum use, capability names, missing-contract backlog links, static-assumption required fields, and complete coverage of the 142 extracted SSOT nodes in `contracts/ledger/ssot-node-reconciliation.yaml`. The generated node map is accepted as the initial map; future review can override factor group and state where keyword mapping is too coarse.

## Dependency Order

1. CF-01 SSOT Source Intake.
2. CF-02 Contract Source Format And Vocabulary.
3. CF-03 `付费金额` Metric Contract.
4. CF-04 Factor Master Extraction.
5. CF-06 Dimension, Event, Assumption, And Backlog Sources.
6. CF-05 Capability Support Records.
7. CF-07 Baseline Capability Cards.
8. CF-08 Factor Review Artifact.
9. CF-09 Reconciliation And Lint Checks.
10. CF-10 Phase 1 Signoff Package.

## First Execution Cut

Start with CF-01 through CF-04. That gives the team a source-backed factor list and shared vocabulary before capability-support mapping begins.
