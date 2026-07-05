# Failure Attribution Taxonomy

Status: launch eval taxonomy  
Scope: labels for evaluation triage. These labels do not promote runtime guardrails on their own.

## Business Failure Types

| Label | Meaning | Example |
| --- | --- | --- |
| `wrong_question_family` | The answer compiled into the wrong business family. | Month-start pattern is treated as ordinary period-over-period change. |
| `wrong_scope` | The answer uses a broader, narrower, or different scope than the question. | Local segment exception is written as full-sample conclusion. |
| `wrong_baseline` | Baseline is missing, incompatible, or materially different from the user's intent. | First-ten-days comparison uses full month. |
| `missed_key_factor` | A contract-supported factor or required branch is omitted. | Payment method mix is ignored in a segment attribution question. |
| `over_strong_weak_evidence` | Wording exceeds evidence strength. | External event context is written as confirmed cause. |
| `hidden_data_gap` | Missing contract, unsupported grain, permission, or freshness limit is hidden. | Recharge activity impact is stated without exposure/control contract. |
| `misleading_visualization` | Visual block implies a stronger or different claim than evidence supports. | Ranking chart suggests causal attribution from candidate mechanisms. |
| `unsupported_main_conclusion` | Main conclusion depends on a blocked, missing, or insufficient path. | Net impact claim without event/control evidence. |
| `permission_leak` | Answer or visual output exposes forbidden identifier or fine-grain sensitive data. | Raw user id, IP, or device id appears in output. |

## System Responsibility Points

| Label | Responsible boundary | Typical fix target |
| --- | --- | --- |
| `LLM_reasoner` | Intent binding, candidate graph proposal, target claim drafting | Prompt or repair context changes after review. |
| `graph_compiler` | Accepted graph validation, mutation, degrade/block policy | Compiler lint or decision table. |
| `semantic_compiler` | Semantic query intent, metric/dimension/window binding | Semantic contract and query planning rules. |
| `capability_execution` | Capability implementation output quality once runtime exists | Capability tests and evidence payload checks. |
| `capability_API` | Capability input/output contract mismatch | Capability card or API contract. |
| `evidence_reducer` | Evidence aggregation, conflict handling, and claim support grouping | Evidence reducer policy. |
| `answer_synthesizer` | Draft answer text and claim grouping | Synthesizer prompt or Answer Package mapping. |
| `answer_verifier` | Final claim, number, wording, evidence, scope, and visual checks | Verifier rules. |
| `visualization_planner` | Visual block choice and evidence binding | Visualization semantic policy. |
| `permission_policy` | Field sensitivity, masking, role, sparse-cell, and output rules | Permission policy and enforcement. |

## Promotion Rule

An eval failure can become a runtime guardrail only after:

- human validation confirms the failure
- business owner and engineering owner agree on severity and action
- the failure is frequent or generalizable enough for runtime enforcement
- the fix target is assigned to code, contract, ledger, verifier, prompt, or visualization policy
- the affected eval slice passes after the change

One-off failures remain eval debt or prompt/expectation review items.
