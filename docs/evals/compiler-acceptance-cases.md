# Compiler Acceptance Cases

Status: design input for Phase 2/3 handoff  
Scope: representative compile outcomes only. No runtime, SQL, final schema, or frontend work.

## Outcome Matrix

| Outcome | Representative case | Candidate graph risk | Expected mutation | Path record | Answer Package constraint |
| --- | --- | --- | --- | --- | --- |
| `accept` | `LE-005` channel/payment segment contribution | Candidate uses `segment_bridge` at supported channel and payment-method grains | Accept node with `segment_attribution_payment_method` | `contract_backed`, `quantifiable`, `accounting_contribution`, `medium`, `quantified` | Segment claim must state scope, grain, coverage, and evidence refs. |
| `auto_repair` | `LE-001` month-phase pattern | Candidate omits quality/verifier/snapshot guards | Add `data_quality_check`, `answer_verify`, contract pins, snapshot, cumulative-value and materiality guards | Mutation log records deterministic guardrail inserts | First-screen pattern claim cannot publish without quality and verifier coverage. |
| `targeted_repair` | `LE-007` custom baseline | Candidate mixes cumulative month-to-date with daily/window semantics | Request bounded patch for window semantics and baseline binding | Repair mutation references `cumulative_value_guard` and `time_boundary_guard` | Baseline windows, timezone, inclusivity, and cumulative-value decision are required. |
| `degrade` | `LE-003` recharge activity impact | Business object exists as a relevant path but lacks event/exposure/control contracts | Keep route as degraded path through backlog refs | `missing_contract`, `insufficient`, `candidate_mechanism`, `insufficient` | Impact wording stays insufficient; missing contracts appear in limitations. |
| `degrade` | Unsupported fine-grain segment fallback | Candidate asks for city/device detail while only aggregate output is allowed | Degrade to supported aggregate grain when contracts and permission policy allow | `unsupported_grain` or `permission_limited` with requested and accepted grain | Claim wording states the accepted grain and blocks broad or raw-detail wording. |
| `block` | `LE-006` raw external black-swan evidence | Candidate asks for raw web/news/forum/media crawl | Block raw external ingestion path | `out_of_scope_for_now`, `out_of_scope`, `insufficient`, `blocked` | No raw external evidence supports final claim. |
| `block` | `LE-005` raw identifier output | Candidate requests raw user/IP/device ids or individual-user claims | Block output and dependent claim path | `permission_limited`, `permission_limited`, `insufficient`, `blocked` | Raw identifiers cannot appear in answer or visual blocks. |

## State Coverage

| State | Case | Required behavior |
| --- | --- | --- |
| `contract_backed` | `LE-005`, `LE-008` | Compiler may accept the path when capability params and grain match contracts. |
| `evidence_linked` | `LE-006` | Compiler may use context/candidate wording and must block stronger cause wording. |
| `static_assumption` | `LE-001` | Compiler may use payday as candidate mechanism with assumption boundary. |
| `missing_contract` | `LE-002`, `LE-003`, `LE-004`, `LE-007` | Compiler degrades when backlog refs exist; blocks when no backlog ref exists. |
| `permission_limited` | `LE-005`, `LE-008` | Compiler blocks raw output and exposes aggregate/permission limits. |
| `unsupported_grain` | Covered through segment and baseline checks | Compiler degrades to supported grain only when contracts allow a fallback. |
| `out_of_scope_for_now` | `LE-006` | Compiler blocks raw external ingestion and records launch impact. |

## Minimal Acceptance Rules

- Candidate graph nodes use only the eight capability ids.
- Every accepted or degraded path references a support id or backlog ref when available.
- `missing_contract` with backlog ref degrades; missing backlog ref blocks.
- Permission failure, raw identifier output, raw SQL, physical schema requests, invalid metric contract, illegal window/grain, cumulative-value misuse, and missing evidence refs block.
- LLM targeted repair can patch only intent, params, target claim, dependencies, and allowed node insert/delete. It cannot change contracts, ledger states, evidence strength, or wording limits.
- Answer Package constraints include disabled/degraded/blocked paths whenever they affect first-screen or main claim wording.
