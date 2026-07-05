# Phase 3 Runtime Handoff

Status: handoff for later semantic compiler runtime implementation
Related: [Completion status](phase-3-completion-status.md), [Runtime readiness checklist](phase-3-runtime-readiness-checklist.md), [Semantic compiler dry-run](phase-3-semantic-compiler-dry-run.md)

## Current Artifact Boundary

Phase 3 now provides a no-SQL runtime adapter:

```bash
ruby tools/runtime/run-semantic-compiler-dry-run-artifact.rb
ruby tools/runtime/validate-semantic-compiler-dry-run-artifacts.rb
```

The adapter reads the default semantic compiler fixtures through the existing contract harness, or accepts `--input` with either a fixture YAML or harness bundle YAML. It writes local artifacts under `data/local/semantic-compiler-dry-run-artifacts/` unless `--out` is supplied. `--print` writes the artifact bundle to stdout.

Each artifact must keep this shape:

- `run_id`
- `fixture_id`
- `question_family`
- `compiler_outcome`
- `accepted_graph_input`
- `semantic_query_request`
- `semantic_query_response_skeleton`
- `evidence_envelopes`
- `answer_package_handoff`
- `path_records`
- `validation_summary`
- `contract_refs`
- `non_runtime_notice`

These artifacts are contract handoff records only. They contain no SQL, no database result rows, no capability execution, no real typed payload values, and no real business conclusion.

## Interfaces To Preserve

Future runtime implementation should consume the current bundle shape without widening business meaning:

| Interface | Current source | Runtime responsibility |
| --- | --- | --- |
| Accepted graph input | `accepted_graph_input` | Preserve node ids, statuses, support ids, factor groups, and path records. |
| Semantic query request | `semantic_query_request` | Plan only from accepted or visibly degraded request skeletons after runtime validators pass. |
| Response skeleton | `semantic_query_response_skeleton` | Replace skeleton-only result placeholders with checked result refs after execution. |
| Evidence envelope | `evidence_envelopes` | Fill typed payload values through capability output while preserving evidence type, strength, wording limit, limitations, and verifier handoff. |
| Answer Package handoff | `answer_package_handoff` | Bind final claim groups to verified evidence refs and visible limitation/path refs. |
| Validation summary | `validation_summary` | Keep contract checks and add runtime validator outcomes. |
| Contract refs | `contract_refs` | Preserve contract pins, support ids, backlog refs, and limitation refs. |

Runtime code can add execution evidence after gates pass. It should not remove path records, mutate support states, hide limitations, or upgrade wording strength without reviewed contract changes.

## Role Visibility

The first runtime baseline uses three roles:

| Role | Visible scope |
| --- | --- |
| `business_reader` | Business conclusions, visible limitations, and permission-safe aggregate visual blocks. |
| `analyst` | `business_reader` scope plus aggregate evidence, process summaries, path records, degraded or blocked route reasons, and non-sensitive diagnostic detail. |
| `data_owner_admin` | `analyst` scope plus contract state, validator outputs, audit metadata, runtime debug detail, and owner-review queues. |

Runtime stores one complete Answer Package. Artifact sections should carry visibility tags: `business_summary`, `aggregate_evidence`, `diagnostic_detail`, or `admin_audit`. Runtime filters sections by role before rendering, sharing, or export and audits actor, role, artifact id, action, and visible section ids. Old artifacts remain readable and auditable with their original snapshot/cutoff; data refresh creates a new run or artifact version and must not silently update old conclusions. Raw user ID, raw IP, and raw device ID remain invisible to all roles.

Bootstrap access starts with these seed principals:

| Role | Bootstrap users |
| --- | --- |
| `business_reader` | `bootstrap_business_reader` |
| `analyst` | `bootstrap_analyst` |
| `data_owner_admin` | `bootstrap_data_owner_admin` |

These three seed principals map one-to-one to the three runtime roles. The first runtime uses a backend allowlist and disables public registration. Extra accounts for sharing, export, or permission regression can live in test data without changing the product role model. Real identity-provider mapping can replace the allowlist when auth is connected.

## LangGraph Baseline

The first production runtime must include LangGraph workflow execution. LangGraph should carry visible workflow order, checkpoints, branches, loops, retries, interrupts, trace, and node progress. WAJE-owned contracts, validators, evidence state, permissions, and verifier remain authoritative for BI decisions.

Runtime must link LangGraph node ids to WAJE run/node ids so product views can join workflow events with accepted graph nodes, semantic queries, evidence refs, path records, verifier results, and Answer Package artifacts.

If LangGraph execution fails, runtime must fail the run or affected branch visibly and must not produce a local business-conclusion fallback. It may expose failed node, reason, retry/recovery option, and preserved evidence state, but no business conclusion or action recommendation can be published from fallback logic.

## Validators To Keep

The consolidated Phase 3 command is:

```bash
ruby tools/evals/validate-phase-3.rb
```

Runtime implementation must continue to pass:

- `ruby tools/evals/generate-semantic-compiler-dry-run.rb`
- `ruby tools/evals/run-semantic-compiler-contract-harness.rb`
- `ruby tools/runtime/run-semantic-compiler-dry-run-artifact.rb`
- `ruby tools/runtime/validate-semantic-compiler-dry-run-artifacts.rb`
- `ruby tools/evals/validate-semantic-compiler-dry-run.rb`
- `ruby tools/evals/validate-semantic-compiler-fixtures.rb`
- `ruby tools/evals/validate-launch-evals.rb`
- `ruby tools/contracts/validate-contracts.rb`
- `ruby tools/runtime/load-contracts-to-postgres.rb`
- `git diff --check`

Before query planning, runtime must add the validators listed in `docs/phase-3-runtime-readiness-checklist.md`: SQL safety, physical schema binding, timestamp/date, permission/masking/sparse-cell, numeric reconciliation, evidence/result ref integrity, Answer Package verifier, runtime artifact persistence, and contract version/snapshot pin checks.

## Gates Still Closed

The current repo can stay in these gates:

- `contract-only`
- dry-run artifact generation
- no-SQL adapter

It cannot enter these gates until the readiness checklist says the required owner and runtime checks pass:

- query planning with physical schema
- read-only query execution
- real capability execution
- typed payload value production
- Answer Package publish

## Owner-Pending Items

Runtime implementation must stop for owner input before using these paths:

- Permission enforcement details: raw identifier visible output is confirmed blocked; sparse cells use `n < 10`, roll up to approved aggregate grain, and may only be mentioned as small-sample observations; three visibility roles, three backend-allowlisted bootstrap users, section visibility tags, and artifact audit shape are confirmed. Masking enforcement still needs implementation.
- `Africa/Lagos` timestamp validation: source field is `支付完成时间`; timezone parsing policy is confirmed; first runtime pins the accepted 2026-07-04 export snapshot and states cutoff 2026-06-30; derived-date mismatches create data-quality warning or block based on claim impact.
- Currency basis: current runtime raw amount data is provided in NGN. Exchange-rate conversion, original-currency validation, and cross-currency claims are out of scope for this phase and cannot support runtime claims.
- Data quality impact: evaluate data quality per affected claim. Issues that can change metric facts or the main conclusion block the affected claim; issues that only affect local explanation degrade that path and show a limitation; minor gaps remain warnings.
- Materiality policy: accepted grain-aware thresholds gate claim strength and display priority. Below-reportable changes stay background only, reportable movement can be visible, material-driver movement can enter primary explanation candidates, and strong-anomaly wording requires the strong-anomaly threshold. LLM output cannot override this; runtime can clarify, degrade, or record a follow-up.
- Formula component contracts: formula selection and residual policy are confirmed. Runtime evaluates every current-data-covered metric-contract path, including `paid_dau_arpu`, `paid_user_arppu`, `new_user_funnel_dashboard`, `frequency_ticket_size`, `region_sum`, and `device_sum`; chooses primary and auxiliary formulas after scoring; uses reviewed components only; tries higher-order attribution when residual or fit is weak; and publishes quantified contribution only when residual is `<= 10%` after useful promotion loops. `paid_dau_arpu` and `frequency_ticket_size` can become quantified primary formulas when gates pass; dashboard paths are auxiliary until reviewed; dimension bridges need dimension/permission gates. Denominators, source ownership, and component contract coverage gate claim strength.
- Source precedence: dashboard means `经营大盘`; `paid_order_detail` is the main `paid_amount` fact source. 大盘 is cut to the requested `paid_order_detail` window and can use only actual overlapping dates for auxiliary formula components and cross-checks. If overlap coverage is `< 80%`, 大盘 paths are context only. Runtime must not extrapolate or fill 大盘 missing dates. Overlapping-date conflicts use `paid_order_detail` for main quantified conclusions. Difference `<= 3%` or `<= 10M NGN` keeps 大盘 auxiliary paths with warning; `> 3%` and `> 10M NGN` degrades 大盘 paths to context; `> 10%` or `> 30M NGN` blocks 大盘 auxiliary paths.
- Dimension visible grain: all contracted aggregate dimensions are equal primary candidates when contract, sparse-cell, masking, missing-value, and permission gates pass, including channel, payment method, amount bucket, geo/city aggregates, device brand/model, OS, and network type. Raw user ID, raw IP, and raw device ID remain internal-only.
- Dimension combination stopping: start with lower-order dimensions, promote only when fit is weak or residual is concentrated, and stop once the lowest-complexity combination has acceptable fit, residual, stability, and business readability. Higher-order combinations stay auxiliary unless they materially change the business interpretation.
- Explanation ranking: when multiple explanations pass evidence gates, prefer business-actionable explanations such as channel, payment method, user type, activity, and operation event. Descriptive dimensions such as city or device model rank lower unless their explanatory power is materially stronger.
- Action recommendations: final answers may include operational recommendations in a separate section from factual conclusions. Recommendations should use check, validate, monitor, or follow-up wording and must not promise causal lift, revenue gain, or strategy outcome unless causal evidence exists.
- Causal wording: confirmed causal wording requires `causal_evidence` backed by experiment/control, exposure-control data, quasi-experimental design, or an owner-reviewed causal contract. Trend, association, formula decomposition, and dimension contribution paths must use related/candidate/possible-influence wording.
- Event evidence: reviewed event records, static assumptions, or source contracts can support contextual explanation or candidate-mechanism claims. Without exposure/control data, quasi-experimental design, or an owner-reviewed causal contract, event evidence cannot support confirmed impact or causal wording. Strong time/scope match may enter auxiliary explanation and can enter the main conclusion only when evidence type, strength, coverage, and verifier gates allow it.
- Budget and timeout: only completed and verifier-passed claims can publish. Unfinished paths are recorded as skipped/degraded. If an unfinished path could change the main conclusion, degrade the main conclusion or trigger clarification; do not fill the gap with guessed business conclusions.
- Result reuse: follow-up questions may reuse prior result refs only when data snapshot, contract versions, permission scope, and semantic scope match. Same or narrower scope may reuse; wider or changed scope reruns affected nodes. Prior results that fail validation can remain context-only but cannot support a new claim.
- Visualization: visual blocks can show only verifier-allowed claims and visible grain. Insufficient evidence uses a limitation or empty state instead of a misleading chart; permission-limited content is aggregated, masked, or hidden.
- Clarification policy: block for user clarification only when ambiguity can change the business conclusion, baseline, time semantics, permission boundary, claim strength, or execution cost. Low-risk gaps continue with recommended inference recorded in accepted graph, Answer Package, and verifier checks.
- Baseline defaults: when the user does not specify a comparison baseline, choose by question family. Change/rise/drop questions default to the previous equal-length window; intra-period pattern questions default to full-sample same-phase or same-day-index structure; operating review questions may run previous equal-length and comparable-calendar baselines. LLM may propose month-over-month, year-over-year, same weekday, seasonality, event-relative, activity/holiday, trend, or business-context baselines; runtime executes only candidates allowed by coverage, contracts, budget, and verifier.
- Unsupported baseline candidates: execute supported baselines first; record unsupported year-over-year, month-over-month, comparable-calendar, or other candidate baselines as skipped/degraded paths in accepted graph and Answer Package, with a visible conclusion boundary.
- Baseline disagreement: if executed baselines disagree, use the baseline that best matches the user question for the main conclusion, show the disagreement, and lower claim strength. If disagreement would change the recommended business action, trigger clarification.
- Dimension bridge missing values: Unknown, blank, null, missing, or unavailable values stay as explicit data-quality buckets for reconciliation. Runtime must not drop them or redistribute them into known buckets; material missing buckets lower claim strength and cannot be described as real regions, devices, channels, or segments. Missing `< 5%` and `< 30M NGN` allows a warning; `5%-20%` or `30M-150M NGN` allows auxiliary explanation only; `> 20%` or `> 150M NGN` blocks the dimension bridge as a primary conclusion.
- Physical schema, runtime API, SQL binding, and execution environment.
- Gap promotion for any `missing_contract`, `unsupported_grain`, `permission_limited`, or `out_of_scope_for_now` path.

## Preserved Blocks

Runtime must keep these blocks unless reviewed contracts change:

- Raw user ID, IP, or device ID visible output.
- Individual-user claims in the WAJE BI v2 baseline.
- Raw external crawling, live AnySearch-like connector output, or unreviewed external feeds.
- Net revenue, adjusted revenue, or adjustment-risk claims from refund, reversal, chargeback, or cancellation data.
- Campaign spend, exposure, control, ROI, ROAS, CPA, net impact, or causal impact claims.
- Gameplay paid_amount linkage, gameplay payment ARPU, gameplay paid-rate, payment-frequency, single-payment, or icon-funnel claims. Directly covered gameplay activity, betting-structure, and GGR fields can remain context or stable-pattern evidence.
- Strong time-window claims without source timezone parsing, derived date validation, accepted snapshot pin, and visible cutoff statement.
- Quantified formula claims without component contracts and reconciliation.

## Handoff Rule

The current artifacts are suitable for runtime implementers as contract tests and local replay inputs. They cannot be used as query results, typed payload values, verified Answer Packages, or business conclusions.
