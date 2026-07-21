# Failure Attribution Taxonomy

Status: current launch-eval taxonomy

These labels guide triage and do not promote runtime guardrails by themselves.

## Business failure types

| Label | Meaning | Example |
|---|---|---|
| `wrong_question_family` | Intent binds to the wrong business family. | Month-start pattern is treated as ordinary period change. |
| `wrong_scope` | A claim uses a broader, narrower, or different scope. | Local segment evidence becomes a full-sample conclusion. |
| `wrong_baseline` | The material comparison differs from the accepted decision. | First-ten-day comparison uses a full month. |
| `missed_key_factor` | A contract-supported required obligation or material branch is absent. | Payment-method mix is omitted from segment attribution. |
| `required_obligation_not_published` | Settlement closes a `user_required` obligation, but verifier-accepted required blocks do not carry its status-appropriate claim and limitation handles. | A required data-quality boundary exists in settlement and disappears from the customer answer. |
| `over_strong_weak_evidence` | Claim or narrative wording exceeds the evidence ceiling. | Event context is written as confirmed cause. |
| `hidden_data_gap` | A missing contract, source, grain, freshness, or safe-output limit is hidden. | Activity impact is stated without exposure/control support. |
| `misleading_visualization` | A visual implies a stronger or different claim. | Ranking suggests causal attribution from candidate evidence. |
| `unsupported_main_conclusion` | The main conclusion depends on a blocked or insufficient path. | Net impact is published without control evidence. |
| `restricted_output_leak` | Customer output exposes unsafe raw or sparse detail. | A raw user or device identifier appears. |

## System responsibility points

| Label | Responsible boundary | Typical fix target |
|---|---|---|
| `intent_planner_llm` | Intent binding, issue tree, auxiliary axes, hypotheses, priorities | Typed prompt/schema or model-routing review |
| `plan_admission_compiler` | Mandatory obligations, deterministic admission, plan closure, degrade/block policy | Goal, admission, or plan contract |
| `query_contract_compiler` | Metric, dimension, scope, window, release, SQL, and grain binding | Semantic/query contract |
| `capability_execution` | Computation or typed `CapabilityOutcome` quality | Capability implementation and tests |
| `capability_API` | Capability input/output contract mismatch | Capability card or adapter contract |
| `claim_settlement` | Evidence classification, support edges, ceilings, obligation coverage | Evidence taxonomy or settlement policy |
| `narrative_material_projection` | Provider-visible claim/material authority and mandatory publication requirements | Projection derivation, opaque handle closure, or required-block contract |
| `narrative_writer` | Business synthesis over the supplied durable material projection | Writer prompt/schema or projection contract after evidence review |
| `answer_completion_authority` | Claim and block veto decisions | Claim or publication verifier contract |
| `publication_projection` | Accepted-block selection, visual binding, fixed customer projection | Projection or visualization policy |
| `publication_flow` | Final customer claim/limitation closure and publication transaction binding | Publication hard gate or transaction contract |
| `restricted_output_policy` | Field sensitivity, raw detail, sparse cells, customer safety | Fixed safety policy and enforcement |
| `source_access_policy` | Source authorization and branch availability | Source policy and dependency isolation |

## Escape-point attribution

Assign root cause to the earliest boundary that had enough authority to prevent
the failure. Record a later hard gate as the escape detector when it correctly
rejects the invalid result.

The failed Case B attempt labeled `verified-03` is the reference example. Claim
settlement had valid `user_required` coverage, while the writer-facing projection
did not declare that mandatory closure. Typed fact binding and block verification
passed; `PublicationFlow` then detected the missing customer-payload coverage.
Attribute the business failure to `required_obligation_not_published`, the root
responsibility to `narrative_material_projection`, and the escape detection to
`publication_flow`. This artifact is diagnostic failure evidence and does
not count toward live acceptance.

The failed Case B attempt labeled `verified-04` is the reference example for
`focused_repair_authority_duplicated`. Accepted sibling blocks already had typed
identity and writer provenance, while the focused provider contract asked the
model to emit them again with rejected targets. Attribute the root
responsibility to `narrative_workflow`, the provider-output rejection to the
focused writer scope validator, and the missing customer-visible failure state
to Agent Core terminal persistence. The current contract keeps provider output
target-only, merges typed blocks deterministically, and persists the five-field
safe operational-failure projection for Gateway.

The failed Case B attempt labeled `verified-05` is the reference example for
`provider_validator_materializer_contract_drift`. A recommendation-only
`direction` block used valid projected handles and was legal under the writer
prompt, yet a stricter typed constructor rejected it after the durable provider
call had succeeded. Attribute the root responsibility jointly to the narrative
authority-handle grammar and writer provider validator, with materialization as
the escape detector. The correction must be a shared structural contract; role
keywords or output-specific exceptions remain prohibited.

## Promotion rule

A finding can become a runtime guardrail only after human validation, joint
business and engineering ownership, generalizability review, and assignment to
the correct code, contract, verifier, prompt, or projection boundary. One-off
findings remain eval debt or expectation-review items.
