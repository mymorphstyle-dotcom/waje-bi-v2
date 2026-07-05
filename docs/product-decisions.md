# WAJE BI v2 Product Decisions

This file records confirmed product and architecture decisions from planning discussions.

## PRD Interview Log

### 2026-07-04: `paid_amount` Currency Policy

- `paid_amount` analysis uses a unified report-currency basis.
- The report currency is Nigerian Naira (`NGN`).
- This confirms the business currency basis for metric comparison, contribution, pattern, anomaly, and amount-tier analysis.
- System raw amount data for the current runtime is provided in NGN. Exchange-rate conversion, original-currency validation, and cross-currency claims are out of scope for this phase and should be handled in a later contract review.

### 2026-07-05: `paid_amount` Refund And Reversal Policy

- `paid_amount` uses gross successful paid amount for operating analysis.
- Refunds, chargebacks, cancellations, and reversals are not proxied from nearby fields in this phase.
- Without dedicated adjustment contracts, they can appear only as missing-contract or scope limitations.
- This keeps payment behavior, payment-chain, pattern, and operating-review analysis anchored on successful payment amount; gross `paid_amount` claims do not wait for adjustment contracts.
- Data/engineering still needs separate adjustment contracts for adjustment source fields, event time, order linkage, adjustment amount, and reporting window before net revenue, adjusted revenue, or adjustment-risk claims can be quantified.

### 2026-07-04: `paid_amount` Timezone And Day Boundary

- `paid_amount` analysis uses Nigerian local business time: `Africa/Lagos`.
- Business-day windows use `[00:00, 24:00)` in `Africa/Lagos`.
- Month-start, month-phase, holiday, payday, hourly, event-window, and anomaly analysis should use this local business-day boundary.
- Runtime business-date binding uses `支付完成时间` converted to `Africa/Lagos`; the raw `日期` column is not the authority for `paid_amount` business-day analysis.
- If `支付完成时间` has no timezone marker, parse it as `Africa/Lagos` local time; if it carries UTC or offset, parse the explicit timezone first, then convert to `Africa/Lagos`.
- Runtime must compare the derived `Africa/Lagos` date with raw `日期`; mismatches create data-quality warning or block based on claim impact.
- Data/engineering still needs runtime validator and snapshot-pin enforcement before executable time-window claims can be quantified.

### 2026-07-05: `paid_amount` Materiality Policy

- Initial materiality policy is accepted from the 2026 H1 paid-order profile.
- The policy is grain-aware: hourly, daily, 3-day, 7-day, 14-day, 30-day, calendar-month, quarter, and custom-window questions use their own comparable-window logic.
- Calendar-month claims use the 30-day proxy with low-confidence wording until more months exist. Quarter claims stay descriptive on the current dataset.
- Runtime uses thresholds as claim-strength and display-priority gates.
- Movement below `reportable_movement` cannot enter the main conclusion, main driver list, or anomaly wording; it can appear only as low-priority background when the user asks for detail.
- Movement at or above `reportable_movement` can be reported as a visible change. Movement at or above `material_driver` can enter the primary explanation candidate set. Movement at or above `strong_anomaly` is required for strong anomaly wording.
- The LLM cannot override materiality gates. If business context suggests the threshold may be inappropriate, runtime can clarify, degrade wording, or record a follow-up, but the verifier remains the publish authority.
- Future business feedback or new distributions should create a new materiality policy version.

### 2026-07-04: Payday Dimension Boundary

- Payday is a universal business dimension with the default monthly window `25..30`.
- This payday window applies across relevant WAJE analysis scenarios.
- Phase 1 keeps one payday dimension and does not split it into additional payday subtypes.
- Payday can enter candidate-mechanism ranking wherever the question and evidence path make it relevant.
- Strong cause wording still depends on evidence strength and verifier checks.

### 2026-07-05: Amount Bucket Policy

- Business owner and data owner accepted the NGN amount bucket policy after profiling `paid_order_success_clean`.
- Confirmed buckets: `<=500`, `501-1000`, `1001-2000`, `2001-5000`, `5001-10000`, `10001-20000`, `20001-50000`, `50001-100000`, `>100000`.
- Amount-tier mix and value-structure claims can use the reviewed bucket policy for the current NGN paid_amount source.
- Future package strategy or material distribution changes should create a new policy version.

### 2026-07-05: Real Data Intake Confirmations

- `分包渠道` is the current semantic `channel` dimension.
- Payment latency fields do not block paid_amount total, trend, channel, time-window, or amount-bucket analysis. Latency-specific analysis must carry coverage limits.
- Payday, month-start, month-end, holiday, hourly, weekly, quarterly, and arbitrary-window analyses are candidate pattern families. Compiler, capability, and verifier design must stay generic and parameterized.

### 2026-07-05: Formula Component And Source Coverage Boundary

- `付费日活` maps to 大盘 `日活历史付费人数`.
- Under this definition, paid-order detail can support paid users, paid order count, payment frequency, average paid amount per payment, initiated payment count, and payment success rate for the accepted 2026 H1 source snapshot.
- If a question needs the SSOT `付费人数 / 付费日活` component, use 大盘 `付费人数 / 日活历史付费人数`.
- Current 大盘 data can provide aggregate daily DAU, new users, registration, first-pay, paid users, paid amount, profit, withdrawal aggregates, and aggregate `投放成本`.
- Current 大盘分包渠道 data can provide channel-day versions of the same dashboard metrics using the filename-prefix channel rule.
- For v2 funnel components, 大盘 `注册率/首充率/新增付费率` are the business metrics `注册率/首次付费率/新增首日付费率`.
- 大盘 remains useful for day and channel-day formula components. Paid-order detail is required for joint analysis, order-level dedup/status repair, payment method/latency diagnostics, amount bucket paths, and aggregate user flags/counts.
- Current gameplay data can provide gameplay users, penetration, rounds, bet count, bet amount, average bet amount, system rake rate, and gameplay profit. Gameplay channel uses the filename-prefix channel rule.
- Current external-event workbook can provide contextual evidence for macro/fx, payday, sports, power, network, media policy, weather, social stability, and holiday events. It supports candidate/context wording only.
- Current competitor ranking CSV can provide daily competitor-ranking context for 2024-01-01 through 2026-06-07. It supports candidate/context wording only.
- Unless new source information is provided, the following remain unavailable: 投放预算、出价、campaign 消耗、素材 CTR/CVR、SEO/GEO 排名、用户推荐活动、服务器稳定性、Grafana、支付事故、产品更新、首充礼包、充值活动、refund/reversal/chargeback/cancellation, gameplay icon exposure/click, gameplay paid rate, gameplay payment amount, gameplay payment frequency, gameplay single-payment amount, and payment-order-to-gameplay linkage. 返奖率 is deferred for now.

### 2026-07-05: Campaign Spend, Exposure, And Control Policy

- Without campaign spend, exposure, and control contracts, campaign and paid-growth evidence can enter answers only as context, candidate mechanism, or missing-contract limitation.
- 大盘 aggregate `投放成本` may be used as background context when its date and scope match the analysis, but it cannot proxy campaign spend, exposure, control, ROI, ROAS, CPA, net impact, or confirmed campaign impact.
- Runtime must block ROI, ROAS, CPA, net impact, causal lift, and confirmed spend-impact wording until maintained campaign/operation event records, spend, exposure, affected scope, and control/comparison contracts exist.

### 2026-07-05: Gameplay Coverage And Linkage Policy

- Current gameplay data should be used where it is directly covered: gameplay users, penetration, rounds, bet count, bet amount, average bet amount, system rake rate, GGR/gameplay profit, and filename-derived channel-day context.
- Available gameplay fields can support gameplay activity, betting-structure, GGR, stable-pattern, and candidate-mechanism explanations when date, gameplay, service scope, channel mapping, sparse-cell, and permission gates pass.
- Missing gameplay fields must not be guessed from adjacent metrics. Runtime blocks gameplay paid_amount attribution, gameplay paid rate, gameplay paid amount, gameplay payment frequency, gameplay single-payment amount, icon exposure/click funnel, and payment-order-to-gameplay linkage until dedicated contracts exist.
- Per-user betting or GGR indicators may be described as gameplay activity or betting-value context only; they cannot be relabeled as gameplay payment ARPU or paid_amount contribution.

### 2026-07-04: External Context Claim Boundary

- External environment, competitors, policy, weather, sports, social events, and black-swan candidates default to `contextual_evidence` or `candidate_mechanism`.
- They can enter candidate explanation ranking when the question and evidence path make them relevant.
- Confirmed cause wording is blocked by default.
- Stronger claims require accepted external evidence contracts, affected scope, confidence fields, event windows, and stronger supporting evidence.

### 2026-07-04: Raw External Evidence Ingestion Boundary

- Raw external crawling is excluded from the launch baseline.
- The current version does not connect AnySearch or other live external evidence connectors.
- Runtime should not use ad hoc web/news/forum/media crawling as direct evidence.
- If a user asks for extra external information, the answer should state that external connector support is a later-phase AnySearch-style integration item; the request can be recorded as a missing external-evidence need, not used as evidence in the current run.
- Future AnySearch-like external evidence connectors can be added after source contracts, provenance, refresh, permission, affected-scope, confidence, and verifier wording rules are reviewed.
- Until then, external context should come from reviewed event/evidence records or manual event records.

### 2026-07-04: Sensitive Identifier Permission Boundary

- User ID, IP, and device ID may be used for aggregate analysis, internal data-quality checks, and deduplication.
- Answers and visualizations must not output raw user IDs, raw IPs, or raw device IDs.
- Individual-user claims are blocked in the WAJE BI v2 baseline.
- Data/engineering still needs to enforce field sensitivity tags, masking, role access, sparse-cell thresholds, audit requirements, and verifier checks.

### 2026-07-04: `paid_amount` Payment Status And Dedup Policy

- `paid_amount` counts only final successful paid orders.
- The amount field is `支付成功金额`.
- The candidate dedup key is `订单ID`; each order should contribute at most one final successful record.
- Failed, pending, processing, and retry-only records stay outside `paid_amount`.
- Those non-success records can still support payment success rate, latency, failure-path, and anomaly analysis.
- Data/engineering confirmed source watermark, no status backfill, and current source-contract acceptance for the 2026-07-04 export.

### 2026-07-04: Current Data Snapshot Policy

- First runtime uses the accepted 2026-07-04 export snapshot by default.
- That snapshot covers 2026-01-01 through 2026-06-30; answers using it must state data cutoff: 2026-06-30.
- Later data updates do not rewrite prior answer artifacts.
- Updated data should produce a new run or new artifact version when the analysis is rerun.
- Prior Answer Package artifacts remain readable and auditable as old-snapshot artifacts. Opening an old artifact should show its snapshot id and cutoff, and runtime must not silently refresh its conclusions with newer data.
- The 2026-07-04 snapshot has no late-arriving records or status backfill recorded for this snapshot.

### 2026-07-04: Source Template And Real Data Binding

- `付费订单明细模板.xlsx` remains a candidate source template and review input in Phase 1.
- First real paid-order detail data was received and profiled as `data/raw/2026-01-01_2026-06-30.csv`.
- The first real-data source contract is accepted for the 2026-01-01 through 2026-06-30 snapshot.
- Dev Postgres contract mirror is initialized for contract artifacts; future snapshots must bind to versioned source contracts.

### 2026-07-05: `paid_amount` Source Precedence

- `dashboard` means `经营大盘` / 大盘 source.
- `paid_order_detail` is the primary fact source for `paid_amount` in the first runtime.
- 大盘 is first cut to the `paid_order_detail` requested window, then only the actual overlapping dates can support auxiliary formula components, trend cross-checks, and structure explanations.
- If 大盘 covers `< 80%` of the requested `paid_order_detail` window after this cut-off, 大盘 auxiliary formula paths can only appear as context.
- Runtime must not extrapolate or fill 大盘 missing dates to match the `paid_order_detail` window.
- If 大盘 `付费金额`, `付费人数`, or `日活历史付费人数` conflicts with values derived from `paid_order_detail`, runtime uses `paid_order_detail` for the main quantified `paid_amount` conclusion and records the difference as a data-quality warning.
- 大盘 cannot override the main `paid_amount` quantified conclusion.
- Overlap reconciliation thresholds compare the relevant overlapping date window:
  - Difference `<= 3%` or amount difference `<= 10M NGN`: keep 大盘 auxiliary formula path with data-quality warning.
  - Difference `> 3%` and amount difference `> 10M NGN`: 大盘 path can only appear as context and cannot enter `primary_formula` or `auxiliary_formulas` scoring.
  - Difference `> 10%` or amount difference `> 30M NGN`: block 大盘 auxiliary formula paths and explain that source reconciliation gap.

### 2026-07-04: Real Data First Profiling Scope

- The first pass on real data should be minimal profiling only.
- It should check field alignment, row count, time range, current-data watermark, payment status distribution, order ID uniqueness, amount and currency distribution, refund or status-backfill signals, and masked sensitive-field statistics.
- The first pass should output a review artifact before any backlog item is promoted into an accepted source contract.
- It should not publish formal business conclusions.

### 2026-07-04: Source Contract Promotion Rule

- Passing real-data profiling creates a review artifact first.
- `contract_backed` requires data owner confirmation of field meanings, source watermark, permissions, payment status enum, and `订单ID` uniqueness.
- The 2026-07-04 export completed this review path and is accepted as the current source contract.

### 2026-07-04: Real Data Joint Review Ownership

- The business owner and data owner will review real-data profiling results together.
- Joint confirmation is required for business-impacting items such as amount buckets, materiality thresholds, anomaly importance, and ambiguous field business meanings.
- Data-execution items such as source watermark, raw enum values, uniqueness, and permission enforcement remain data/engineering-owned checks.
- Confirmation order is data-execution checks first, joint business review second, then draft source contract or backlog blockers.

### 2026-07-05: Raw Identifier Scope For Operations Analysis

- Raw `IP` and `设备ID` are not required for the current operations-analysis scope.
- Confirmed permission choice: raw user ID, raw IP, and raw device ID are never shown in answers or visualizations. They may only be used internally for data-quality checks, deduplication, permission-safe joins, and aggregate analysis.
- Aggregate analysis can still use available region, device brand/model, operating system, and network type fields.
- Device-level deduplication, single-device tracking, raw-IP location checks, and device-level risk analysis are outside the current BI operations-analysis scope.

### 2026-07-05: Sparse Cell And Aggregate Fallback Policy

- Low-sample segment cells should not show detailed values, labels, ranks, amounts, counts, or raw rows in answers or visualizations.
- First-runtime sparse-cell threshold is `n < 10`: order metrics count paid orders, user metrics count distinct users.
- Runtime should roll low-sample cells up to an approved higher aggregate grain when that grain is meaningful and permission-safe.
- If a low-sample observation may help business readers understand uncertainty, the answer may mention that a similar signal was observed in a small sample, with no detailed cell output.
- Low-sample observations cannot support main conclusions, quantified claims, rankings, or causal wording.
- Candidate screening may keep noisier cells above this red line for local scoring and LLM business judgment; sample size, stability, and evidence strength still constrain promotion.
- Data/engineering still needs to enforce this threshold in permission, artifact filtering, verifier, and visualization checks.

### 2026-07-05: Role Visibility And LangGraph Runtime Baseline

- First runtime baseline uses three visibility roles: `business_reader`, `analyst`, and `data_owner_admin`.
- `business_reader` can see business conclusions, visible limitations, and permission-safe aggregate visual blocks.
- `analyst` can additionally see aggregate evidence, process summaries, path records, degraded or blocked route reasons, and non-sensitive diagnostic detail.
- `data_owner_admin` can additionally see contract state, validator outputs, audit metadata, runtime debug detail, and owner-review queues.
- No role can see raw user ID, raw IP, or raw device ID in answers or visualizations.
- Runtime stores one complete Answer Package. Artifact sections should use visibility tags such as `business_summary`, `aggregate_evidence`, `diagnostic_detail`, and `admin_audit`; runtime filters sections by role before rendering, sharing, or export.
- Artifact audit records actor, role, artifact id, action, and visible section ids for every open, share, or export action.
- Bootstrap access uses a backend allowlist and public registration is disabled in the first runtime.
- Bootstrap access starts with one default principal per role: `bootstrap_business_reader`, `bootstrap_analyst`, and `bootstrap_data_owner_admin`.
- Real identity-provider mapping can replace the allowlist when auth is connected.
- First production runtime must integrate LangGraph workflow execution. LangGraph carries visible workflow, checkpoints, branches, loops, retries, interrupts, trace, and node progress; WAJE-owned contracts, validators, evidence state, permissions, and verifier remain the BI authority.
- LangGraph node ids should link to WAJE run/node ids so product views can join workflow progress with evidence refs, path records, verifier results, and Answer Package artifacts.
- If LangGraph execution fails, runtime must fail the run or affected branch visibly and must not produce a local business-conclusion fallback. It may show failed node, reason, retry/recovery option, and preserved evidence state, but no business conclusion or action recommendation can be published from fallback logic.

### 2026-07-05: First Dataset Paid Amount Cleaning Boundary

- For `data/raw/2026-01-01_2026-06-30.csv`, `pay_success` is the paid_amount success status.
- `order_success` is a prior/non-paid status and is excluded from paid_amount.
- If the same `订单id` has both `order_success` and `pay_success`, ignore the `order_success` record for paid_amount.
- If the same `订单id` has multiple `pay_success` records, keep the latest `支付完成时间` record.
- Business-date analysis uses `支付完成时间` converted to `Africa/Lagos`, not the raw `日期` column.
- Applying this boundary gives 23,858,847 paid records and 51,172,015,308.0 NGN for intake profiling.

### 2026-07-03: Interview Method

- Interview one decision at a time.
- Each question should offer three business-facing options, with a recommended option and plain business rationale.
- After the user selects an option, record the PRD decision here before moving to the next question.
- PRD interview questions should treat question families as multi-capability business workflows. A user question can compile into a graph that combines several capabilities, runs evidence checks, and then synthesizes the answer.
- Foundational capabilities are evidence-producing actions; recipe entries and question families are business entry points that can combine capabilities.
- Avoid framing a question family as a single capability route. The interview should clarify scope, baseline, evidence needs, capability composition, degradation, and acceptance.

### 2026-07-03: `paid_amount_change_explanation`

- Primary business scenario: operating review and explanation. The product should answer why paid amount changed, what the main drivers are, how large the impact is, and where evidence is limited.
- The system should also recognize when the user's wording points to anomaly triage or activity/channel/version review, then route into the matching question family or merged graph.
- For this question family, the accepted runtime plan can combine `formula_decompose`, `pattern_scan`, `joint_attribution`, `event_evidence`, `outlier_scan`, `segment_bridge`, `data_quality_check`, and `answer_verify` according to the question's scope and evidence needs.
- The answer should synthesize multiple evidence packages by claim and scope, rather than implying that one capability alone explains the business change.
- Default graph strategy: use an operating-review full-context graph as the main spine, then let the orchestrator compile an accepted graph from user intent, scope, baseline, time semantics, available contracts, and evidence needs.
- The default spine should cover scope/baseline/time binding, data quality checks, formula decomposition, pattern checks, anomaly review, attribution, event evidence, synthesis, and answer verification as needed by the concrete question.
- User-provided hypotheses such as activity, channel, version, holiday, or abnormal day should route into the same accepted graph as targeted branches rather than replacing the operating-review spine.
- Baseline handling: when the user does not specify a comparison baseline, choose by question family. Change/rise/drop questions default to the previous equal-length window; intra-period pattern questions default to full-sample same-phase or same-day-index structure; operating review questions may run both previous equal-length and comparable-calendar baselines when useful.
- LLM can propose additional baseline candidates such as month-over-month, year-over-year, same weekday, seasonality, event-relative window, activity/holiday context, trend window, or business-context-specific baseline. Local policy, data coverage, contracts, budget, and verifier decide which candidates execute and what claim strength they can support.
- If a proposed baseline lacks enough data, comparable windows, or contract support, runtime should execute the supported baselines and record the unsupported one as a skipped or degraded path in the accepted graph and Answer Package.
- The final conclusion boundary should state when current data cannot support year-over-year, month-over-month, comparable-calendar, or another candidate baseline.
- Baseline choice should be represented in LangGraph as an explicit intent/baseline binding step. This step can emit a recommended default, multiple accepted baseline branches, or a clarification question.
- If multiple baselines are useful and affordable, the accepted graph can run them together and synthesize whether conclusions are stable across baselines. If baseline choice could change the answer materially, the graph can insert a question-tool clarification while keeping a recommended inference available.
- If executed baselines disagree, the main conclusion may use the baseline that best matches the user question, but the answer must show baseline disagreement and lower claim strength. If the disagreement would change the recommended business action, runtime should trigger clarification.
- The accepted graph and final Answer Package must record the selected or inferred baseline, any skipped baseline options, and the claim boundary created by that choice.
- First-screen answer structure should be dynamically generated from verified claim groups and a validated visualization plan. It should not use a fixed number of cards or a fixed card order.
- Stable information hierarchy: main conclusion, key quantification, main drivers, exceptions or disagreements, and evidence boundaries.
- Dynamic card selection should follow the accepted graph and evidence: baseline stability, formula contribution, attribution ranking, business object impact, pattern evidence, anomaly/black-swan review, data quality, and missing-contract limitations appear only when needed for the business answer.
- If multiple baselines support the same conclusion, the answer can merge them into a baseline-stability card. If baselines disagree, the disagreement should become visible in the first-screen answer and constrain the main conclusion wording.
- Frontend should render the verified answer plan and visualization plan. It should not infer business importance directly from raw evidence payloads.
- First-screen conclusions should be ordered by business explanatory power. The most useful explanation for the business question should appear first, whether it comes from formula decomposition, attribution, anomaly review, business object impact, baseline disagreement, pattern evidence, or data quality.
- Each conclusion should still carry evidence strength and claim boundaries so high-explanatory but weak-evidence candidates are not stated as confirmed causes.
- Default explanation coverage for `paid_amount_change_explanation`: use a complete operating explanation package covering formula decomposition, baseline stability, dimension/combination attribution, periodic or pattern evidence, business object impact, anomaly/black-swan review, data quality, and evidence boundaries.
- The accepted graph can dynamically adjust depth by budget and evidence need, but it must record which explanation types were verified, skipped, degraded, or blocked.
- If budget or timeout stops part of the graph, only completed and verifier-passed claims can be published. Unfinished paths must be recorded as skipped or degraded. If an unfinished path could change the main conclusion, the main conclusion is degraded or runtime triggers clarification; runtime must not fill the gap with guessed business conclusions.
- Execution depth should be evidence-driven and layered. The graph should first run evidence needed to establish the main business conclusion, then deepen into attribution, events, combinations, anomaly review, or additional baselines when residuals, disagreements, candidate signals, or verifier requirements justify it.
- The product should avoid fixed-depth full scans for every question and avoid pushing depth selection onto users by default.
- The graph should deepen when the main conclusion is not stable enough: large unexplained residuals, baseline disagreement, unstable main contribution, concentrated exception periods, strong event-window overlap, or verifier risk that could change the allowed claim/evidence type or wording limit.
- Weak but non-decisive signals that do not affect the main conclusion should be preserved as suggested follow-ups or limitations rather than forcing deeper execution every time.
- Degraded or insufficient evidence should be expressed inside the relevant conclusion boundary on the first screen. Users should see which explanation is quantified, which is only a candidate mechanism, and which route is blocked or unsupported.
- If data quality, missing contracts, unsupported grain, or permissions materially affect the whole answer, the first screen can also include a prominent limitation card.
- Weak or degraded evidence should not be hidden only in expanded details.
- PRD acceptance cases for `paid_amount_change_explanation` should be organized by business risk. Required risk groups include normal operating review, baseline disagreement, periodic-pattern misclassification, anomaly-dominated change, event candidate explanation, missing contract, data quality issue, over-strong causal wording, and permission-limited evidence.
- The business-risk acceptance set should map back to capability coverage and SSOT factor coverage in the launch acceptance matrix.

### 2026-07-03: Clarification And Question Tool

- Clarification should be an optional LangGraph branch, not a hard gate.
- LLM can propose clarification questions when ambiguity could change the business answer, baseline, time semantics, analysis scope, claim strength, permission path, or execution cost.
- Question tool prompts should offer a small set of business-facing options with a recommended interpretation. If the question tool is presented, it can block the current run until the user chooses an option, chooses the recommended inference, or tells the agent to do differently.
- Clarification can appear during intent binding, graph compilation, targeted graph repair, degraded-path handling, and final answer verification.
- Accepted graph state should record the clarification result as `user_selected`, `system_inferred`, or `skipped`, together with the chosen assumption and downstream evidence boundary.
- Local compiler, policy, contracts, permissions, and verifier remain authoritative. The LLM can suggest clarification options and recommendations, while local systems decide whether the resulting graph is executable and what claim strength is allowed.
- Trigger policy: use LLM judgment to propose clarification candidates, then local policy should prioritize questions that can change the main conclusion, claim boundary, scope, baseline, time semantics, permission path, or execution cost.
- Clarification UX should follow the Codex/Claude Code style: one clarification turn can include up to 3-4 short questions, each with up to 3 concrete options and one recommended option when appropriate.
- Each clarification turn should also include a fixed "tell the agent to do differently" escape option so users can override the framing, supply their own instruction, or ask the agent to proceed another way.
- The system should avoid asking for every missing parameter. Low-risk gaps should use the recommended inference and continue without opening a question tool. If a question tool is opened, one of the options should allow continuing with the recommended inference.
- Runtime should block for clarification only when ambiguity could change the business conclusion, baseline, time semantics, permission boundary, claim strength, or execution cost. Other assumptions should continue as recommended inferences and be recorded in accepted graph, Answer Package, and verifier checks.

### 2026-07-03: First Vertical Slice - Intra-Month Payment Pattern

- Slice question: `全量样本看，为什么从 2024 年 1 月开始到 2026 年 5 月结束，为什么每个月月初的付费金额都比月中/月末高一些`.
- The slice is an acceptance example for a general pattern-explanation problem domain, not a bespoke solution path for one hard-coded question.
- Product and implementation must not overfit to this single month-start case. The same design must generalize to intra-period, weekly, seasonal, event-relative, rolling-window, custom-baseline, and other recurring or anchored pattern questions.
- The system must classify this example as full-sample intra-month periodic pattern analysis.
- It must not misclassify the question as period-over-period change analysis, cost-period change, or cumulative-value analysis.
- First baseline window definition: month-start is day 1-10, mid-month is day 11-20, and month-end is day 21 through calendar month end.
- This fixed window definition is the acceptance default for the first vertical slice. Generic `pattern_scan` should still support custom windows and event-relative windows outside this case.
- Pattern existence should be judged by recurrence across enough periods, business-meaningful effect size, stability, exceptions, and data quality checks. It should not rely only on a pooled average, and it should not require every period to strictly match when explainable exceptions exist.
- `pattern_explanation` should primarily support cases where the user proposes a candidate pattern and expects validation, quantification, exceptions, and explanation.
- The system should allow bounded sibling-pattern exploration within the same dimension or pattern family. If the user asks about pattern A but evidence shows a nearby pattern B better explains the data, the answer should surface pattern B with evidence and clarify how it relates to the user's original hypothesis.
- Exploratory pattern discovery should stay bounded by budget, business relevance, candidate windows, SSOT/contracts, data quality, and claim-strength policy.
- When sibling pattern B better explains the data than the user's candidate pattern A, the first-screen answer should first respond to A, then surface B as the stronger or more precise pattern.
- The answer should explain whether B refines A, partially replaces A, or only applies to an exception scope.
- After detecting a pattern, default explanation depth should cover validation, quantification, exceptions, and candidate mechanisms.
- The answer must be written as business explanation, not as field stacking. Pattern existence, magnitude, exceptions, and mechanisms should be synthesized into readable business conclusions with inline evidence where useful.
- Stronger cause wording is allowed only when the evidence and verifier support that claim strength.
- Candidate mechanism exploration should be SSOT-first with business-neighbor expansion.
- The system should start from factors, dimensions, events, and static assumptions recorded in `付费金额影响因子分析.mm` and the factor ledger.
- When the pattern is strong but current evidence does not explain it enough, LLM can propose nearby business candidates such as payday, holiday, version, activity, or channel combinations. Ledger, contracts, permissions, and verifier decide whether those candidates can support a claim, stay as contextual evidence, or become missing-contract items.
- Reasonable mechanisms with incomplete contracts can appear as candidate mechanisms when they have valid contextual or temporal evidence.
- The answer must state the missing contract or unsupported grain that prevents stronger wording.
- Missing-contract mechanisms should not be promoted to confirmed causes.
- First production baseline for `pattern_explanation` should cover the full intended pattern problem domain rather than only the first slice or a small high-frequency subset.
- Pattern families should include intra-period, weekly, monthly, quarterly, yearly seasonality, event-relative windows, pre/post windows, lag/recovery windows, rolling windows, custom baselines, cohort-related patterns where contracts support them, and segment-level pattern comparisons where grain and permissions allow.
- Full-domain coverage still needs runtime bounds through accepted graph planning, budget, contracts, SSOT relevance, data quality, and claim-strength policy.
- Runtime pattern exploration should be bounded by the user's candidate pattern, business context, and evidence-triggered expansion.
- The graph should expand to sibling patterns, event-relative windows, rolling windows, lag/recovery, or segment-level comparisons only when evidence suggests they can change the conclusion, strengthen the explanation, explain residuals/exceptions, or clarify a user's hypothesis.
- The product should support the full pattern problem domain without scanning every pattern family on every run.
- `pattern_explanation` first-screen answer should prioritize whether the user's pattern holds, whether a more precise sibling pattern better explains the data, the business explanation, and exception boundaries.
- The answer should be a synthesized business narrative with necessary inline visual blocks, not a ranked dump of patterns or a field list.
- Pattern visualization blocks should be dynamically selected by pattern family and verified evidence.
- Examples: intra-period uses phase comparison, weekly uses weekday profile, event-relative uses event timeline, rolling uses trend band, and lag/recovery uses lag curve.
- Visualization plan should serve the business conclusion. Frontend renders the validated plan and should not independently infer which chart matters.
- Event or mechanism explanations in `pattern_explanation` should default to candidate mechanisms unless stronger evidence supports a stronger claim.
- Temporal overlap, co-movement, or business plausibility can support candidate-mechanism wording, but should not be written as confirmed causality by itself.
- Verifier should enforce claim strength based on available contracts, controls, stability, counterfactual evidence, and evidence envelopes.
- Exceptions are part of pattern explanation. The answer should state whether the overall pattern holds, which periods or segments are exceptions, why they are exceptions when evidence supports it, and whether exceptions change the main conclusion.
- Activities, holidays, data quality issues, black-swan events, or unsupported data paths can explain exceptions with appropriate claim boundaries.
- A pattern should not be rejected only because some explainable exceptions exist.
- PRD acceptance cases for `pattern_explanation` should be organized by pattern risk.
- Required risk groups include candidate pattern holds, candidate pattern does not hold, sibling pattern is stronger, explainable exceptions, event candidate mechanism, data quality issue, missing contract, misclassification as period-over-period or cumulative-value analysis, and over-strong causal wording.
- The risk set should map back to supported pattern families, capabilities, and SSOT factors in the launch acceptance matrix.
- Use `business_object_impact_review` for the PRD and recipe-entry model. Event-specific wording stays as a subtype.
- This problem family should cover impact questions where the user names a known business object and asks how it affected the target metric.
- Supported named objects can include calendar events, holidays, activities, campaigns, ad spend, product versions, operational actions, external events, metric drivers such as new users, and dimensions or segments when the question is phrased as impact review.
- If the named object is an action or event, the accepted graph should emphasize event/intervention evidence. If it is a metric driver, dimension, or segment, the graph should merge impact review with formula decomposition, segment bridge, and attribution as needed.
- Example: "最近一周投放对业务的影响如何" should route to an impact review graph with event/intervention evidence, baseline comparison, attribution, data quality, and verifier. "最近一周新增人数对业务的影响如何" should route to impact review plus metric decomposition and contribution evidence.
- Core answer for `business_object_impact_review`: whether the named object had an impact, how large the impact was, and through which business path it likely affected the target metric.
- Impact path wording must follow evidence strength. Confirmed contribution, statistical association, candidate mechanism, and insufficient evidence should be separated.
- Accepted graph should choose the primary evidence path by business object type, then merge additional capabilities as needed.
- Action or intervention objects such as activity, campaign, ad spend, product version, operation action, or push should emphasize event/intervention evidence.
- Metric-driver objects such as new users, paid users, payment success rate, order count, or average amount should emphasize formula decomposition and contribution evidence.
- Dimension or segment objects such as channel, region, device, user type, or payment method should emphasize segment bridge and attribution evidence.
- External context objects such as holidays, policy, weather, market incidents, or competitor events should emphasize contextual event evidence with careful claim boundaries.
- Baseline and control selection should be recommended by the LLM from the named object and business context, then validated by the local compiler.
- Candidate comparisons can include pre/post windows, same-period historical baseline, no-exposure or low-exposure windows, similar segments, gray/control groups, trend baseline, formula contribution baseline, or user-provided custom baselines.
- If comparison choice can materially change the conclusion, the graph can open a question tool. If the user does not have a preference, the recommended comparison proceeds and is recorded in the accepted graph and Answer Package.
- If strict control or complete intervention data is unavailable, the answer can degrade to association or candidate-impact wording when useful evidence exists.
- The answer must state what is missing, such as no-exposure controls, exposure details, gray/control split, event timing, or metric-driver contracts, and how that limits impact wording.
- Trend co-movement or temporal overlap alone should not be promoted to confirmed net impact.
- First-screen answer for `business_object_impact_review` should dynamically cover impact judgment, impact magnitude, likely business path, and comparison/control boundary.
- The answer should synthesize whether impact exists, how large it is, which path carries it, and why the evidence is strong, weak, or degraded.
- Visual blocks should be selected by object type and evidence path, such as pre/post comparison, exposure timeline, formula contribution, segment bridge, attribution ranking, or control comparison.
- PRD acceptance cases for `business_object_impact_review` should be organized by business object type and evidence risk.
- Required object groups include activity/campaign/version, ad spend or operation action, metric driver such as new users or success rate, dimension/segment such as channel or user type, and external context events.
- Required evidence-risk groups include missing controls, missing exposure details, wrong time window, correlation written as causality, no measurable impact, and impact limited to a local segment.
- The risk set should map back to capabilities, ledger support states, and claim thresholds in the launch acceptance matrix.
- Core answer for `revenue_health_review`: determine whether revenue or paid amount is healthy, where the risk or issue is, and whether the issue comes from trend, structure, funnel/payment chain, anomaly, event, segment, or data quality.
- This question family is a comprehensive business health review, not only a trend summary or anomaly scan.
- Default review scope should cover trend, structure, funnel/formula, anomaly, and data quality.
- Typical checks include paid amount trend, paid users/order count/success rate/average amount decomposition, channel/user/payment-method structure, abnormal windows, and data completeness or metric-contract issues.
- Execution depth can still be adjusted by evidence, risk, and budget.
- Health judgment should combine business targets, historical baselines, and data quality.
- If explicit targets exist, target deviation should participate in the judgment. Without targets, the system should use historical trend, year-over-year or period-over-period baselines, volatility bands, structural risk, and data-quality status.
- Health should not be judged only by simple growth direction or statistical anomaly thresholds.
- First-screen answer for `revenue_health_review` should contain health judgment, risk sources, key evidence, and suggested follow-up investigations.
- The product should provide a business judgment before charts or metric grids, while using inline evidence and visual blocks to support the judgment.
- Health scorecards or multi-metric dashboards can be supporting views, but they should not replace the agent's business conclusion.
- Revenue health risks should be grouped by business impact and evidence strength.
- Suggested groups: high risk when impact is large and evidence is strong, attention item when impact may be large but evidence is medium or local, watch item when signals are weak or localized, and data risk when data quality limits the conclusion.
- Risk ranking should not rely only on amount size or only on evidence strength.
- PRD acceptance cases for `revenue_health_review` should be organized by health risk type.
- Required risk groups include healthy revenue, unhealthy revenue, worsening trend with healthy structure, stable total with deteriorating structure, payment-chain risk, anomaly-dominated health issue, data-quality-limited judgment, and target deviation.
- The risk set should map back to metrics, capabilities, ledger support states, and claim thresholds in the launch acceptance matrix.
- Core answer for `segment_or_factor_attribution`: identify which dimensions, factors, or combinations best explain a change, difference, pattern, impact, or exception.
- The product should support both single-factor explanation and multi-factor combination attribution. It should not stop at simple ranking when combinations materially improve the explanation, and it should not start with opaque high-order search by default.
- Promotion from single-factor explanation to combination attribution should be triggered by unstable single-factor results, large residuals, meaningful within-factor differences, business mechanism hypotheses, or verifier risk.
- Combination attribution starts with at least two dimensions or factors, then promotes to higher-order combinations only when evidence and business relevance justify the added complexity.
- Attribution should start with one-dimensional candidate screening to establish business intuition, candidate pool, and residual pattern.
- Two-dimensional combinations are the default starting point once the graph enters combination attribution.
- Higher-order combinations require additional evidence, business relevance, stability, and budget support.
- Attribution candidate pool should combine SSOT-registered candidates, data-discovered candidates, and LLM/user-hypothesized candidates.
- SSOT and factor ledger are the primary source. Data discovery can add candidates from distribution shifts, structural changes, anomalies, new values, or residual patterns. LLM/user hypotheses can enter the pool only after ledger, contract, permission, and evidence checks.
- Candidate openness should not bypass verifier boundaries or claim-strength policy.
- First-screen answer for `segment_or_factor_attribution` should present actionable business explanation first, then contribution magnitude, stability, coverage, and evidence boundary.
- Attribution should not be reduced to a Top-N ranking or model score. Rankings can support the explanation but should not replace the business conclusion.
- Attribution claim strength should separate contribution or explanation from causal impact.
- The product can state quantified contribution or explained difference when segment bridge, formula decomposition, or attribution evidence supports it.
- The product should not convert contribution ranking into causal wording unless intervention, control, mechanism, or stronger causal evidence supports that claim.
- PRD acceptance cases for `segment_or_factor_attribution` should be organized by attribution risk.
- Required risk groups include one-dimensional explanation sufficient, one-dimensional explanation misleading and two-dimensional attribution required, two-dimensional explanation sufficient, higher-order combination required, local combination that cannot be generalized, sparse sample risk, permission-limited evidence, and contribution wording incorrectly promoted to causal wording.
- The risk set should map back to dimension types, capabilities, ledger states, and claim thresholds in the launch acceptance matrix.
- Core answer for `anomaly_or_black_swan_review`: determine whether the anomaly is real, where it appears, and what business or data explanation is supported.
- The graph should first rule out data quality, metric-contract, time semantics, permission, or cumulative-value issues, then identify abnormal time windows, segments, metrics, or factors, and finally test internal actions, structural changes, external shocks, and black-swan candidates.
- Anomaly detection should generalize to any supported dimension, factor, metric component, segment, time window, event window, or combination allowed by ledger, contracts, grain, permissions, and budget.
- Time anomalies, segment anomalies, metric-chain anomalies, and data anomalies are default examples, not an exhaustive list.
- Black-swan events should be treated as one candidate explanation class for anomalies.
- The system should first determine whether an anomaly exists and where it appears, then test external shocks such as policy, market, weather, competitor, platform incidents, or other black-swan candidates when relevant evidence exists.
- Large anomalies should not automatically be labeled black-swan events.
- First-screen answer for `anomaly_or_black_swan_review` should cover anomaly conclusion, affected scope, likely explanations, and ruled-out paths.
- The answer should state whether the anomaly is real, where it is concentrated, which explanations are supported or candidate-only, and which data or business routes were ruled out.
- Anomaly lists and external-event lists can support the answer, but should not replace the business conclusion.
- PRD acceptance cases for `anomaly_or_black_swan_review` should be organized by anomaly risk.
- Required risk groups include true anomaly, pseudo-anomaly or data issue, local segment anomaly, metric-chain anomaly, internal-action explanation, external black-swan candidate, unsupported black-swan misclassification, and permission or grain-limited anomaly evidence.
- The risk set should map back to capabilities, algorithms, ledger states, and claim thresholds in the launch acceptance matrix.
- Core answer for `custom_baseline_comparison`: compare the target metric against a user-specified or business-recommended baseline, then explain the difference and its business causes.
- Custom baseline comparison should not stop at numeric deltas. It should connect to formula decomposition, attribution, event evidence, anomaly review, data quality, and verifier when the user asks why the difference exists.
- User-specified baseline should take priority. If the user does not specify enough baseline detail, LLM should recommend plausible baselines such as period-over-period, year-over-year, same weekday, event-relative, target value, or similar-window comparisons.
- The graph can use question tool when baseline ambiguity could materially change the conclusion. If the user has no preference, the recommended baseline proceeds and is recorded.
- When multiple baselines produce different conclusions, baseline disagreement should be part of the first-screen conclusion boundary.
- Example framing: period-over-period decline with year-over-year strength should be explained as recent pullback rather than broad long-term weakness.
- The answer should avoid hiding material baseline disagreement only in detail views.
- PRD acceptance cases for `custom_baseline_comparison` should be organized by baseline risk.
- Required risk groups include user-specified baseline, system-recommended baseline, multiple-baseline disagreement, event-relative baseline, same-weekday or similar-window baseline, target deviation, cumulative-value misuse, wrong time semantics, and unavailable comparable window.
- The risk set should map back to baseline types, capabilities, ledger states, and claim thresholds in the launch acceptance matrix.
- Core answer for `data_quality_or_evidence_review`: determine whether a conclusion can be trusted and where data, evidence, contract, permission, or claim-strength limits apply.
- This question family should review both data quality and evidence sufficiency. It should not be limited to raw data checks or only answer-level evidence checks.
- Default review scope should cover data quality, contract coverage, permissions, evidence strength, and claim wording.
- Checks should include missing or duplicate data, cumulative-value misuse, time semantics, metric contracts, dimension/event contracts, permission limits, evidence sufficiency, and whether answer wording exceeds supported claim strength.
- First-screen answer for `data_quality_or_evidence_review` should contain trust judgment, affected scope, claims that need degradation, and recommended data or contract fixes.
- The answer should tell users which conclusion can be trusted, which evidence path is limited, and what data or contract improvement would raise claim strength.
- A checklist of checks can support the answer but should not replace the trust judgment.
- PRD acceptance cases for `data_quality_or_evidence_review` should be organized by trust risk.
- Required risk groups include trustworthy main conclusion, local claim degradation, missing contract, permission limit, cumulative-value misuse, time-semantics error, insufficient evidence, over-strong wording, and upgrade after data or contract improvement.
- The risk set should map back to data quality checks, contracts, permissions, evidence states, verifier checks, and claim thresholds in the launch acceptance matrix.
- Full PRD structure should use business question families as the main line, with technical constraints and product architecture as supporting sections or appendices.
- Main PRD sections should cover user scenarios, system behavior, answer shape, degradation, and acceptance for the eight question families.
- Supporting sections should cover SSOT/factor ledger, capability cards, graph compiler, verifier, permissions/audit/snapshots/rerun/performance/observability, UX shell, and launch eval.
- PRD technical sections should use product-contract granularity rather than final table structures or implementation schemas.
- PRD should define what ledger, capability cards, graph compiler, Answer Package, verifier, and production gates must guarantee, while leaving final database tables, API schemas, and storage layout to follow-up technical design.
- PRD review P0-1 decision: launch acceptance matrix should be fixed by adding an executable skeleton plus representative cells. Each question family needs rows with required capabilities, forbidden overclaims, key ledger states, allowed claim/evidence type, allowed strength or wording limit, expected visual blocks, and verifier checks. Do not attempt the full factor-by-factor matrix until `.mm` has been reconciled into the factor ledger.
- PRD review P0-2 decision: first vertical slice should be fixed by a complete structured expectation package that can become a regression eval. The exact expectation package content must be confirmed with the user before writing into the PRD.
- PRD review P0-3 decision: accepted graph should get a product-level lifecycle contract in the PRD. Include minimal graph/node states such as `proposed`, `accepted`, `auto_added`, `repair_requested`, `repaired`, `running`, `completed`, `degraded`, `blocked`, `skipped`, and `verified`, plus clarification outcomes such as `user_selected`, `recommended_inference_selected`, `agent_instructed_differently`, and `system_inferred`.
- PRD review P0-4 decision: each of the eight foundational capabilities should get a one-page product-contract sketch in the PRD. Each sketch should cover business use, non-use, key parameters, evidence output, lint rules, degradation rules, and typical question families, without locking final API schemas.
- PRD review P0-5 decision: Answer Package should define a minimal claim group product contract in the PRD. Each claim group should include conclusion text, scope, baseline, target metric, evidence refs, evidence type, strength, supported wording, disallowed wording, limitations, related visual blocks, and verifier status, without locking final API schema.
- PRD review P1-1 decision: factor ledger reconciliation should be written as an audit/review pipeline in the PRD: extract from `.mm`, generate review artifact, business owner reviews meaning and claim boundary, data/engineering owner reviews contracts/grain/permissions, assign support status, run missing/conflict checks, version the accepted source, and publish a runtime mirror.
- PRD review P1-2 decision: use `business_object_impact_review` as the problem-family name throughout the PRD. Event terms should remain only as business-object subtypes or visualization subtypes, such as event timelines, holidays, campaigns, versions, and external events.
- PRD review P1-3 decision: production requirements should include minimum launch gates in the PRD. Gates include permission-blocked claim behavior, audit traceability, snapshot/rerun comparability, budget skip recording, slow/failed/degraded run observability, and blocking strong conclusions when verifier fails.
- PRD review P1-4 decision: the question tool's "tell agent to do differently" escape should enter intent rebinding or targeted graph repair. The LLM can reinterpret or propose graph changes, then the local compiler validates. Accepted changes continue with mutation reason recorded; rejected changes produce a business-facing refusal, repair, or degradation explanation.
- PRD review P1-5 decision: artifacts should be in first baseline scope with a simple model. Artifacts save verified answer, visualization plan, process summary, evidence boundaries, and data/contract snapshot info; support read-only sharing with permission filtering; and allow continuing investigation from the artifact.
- PRD review P1-6 decision: launch eval should include failure attribution taxonomy by business failure type and system responsibility point. Business failures include wrong question family, wrong scope, wrong baseline, missed key factor, over-strong weak evidence, hidden data gap, misleading visualization, and unsupported main conclusion. System responsibility points include LLM reasoner, graph compiler, semantic compiler, capability API, evidence reducer, answer synthesizer, answer verifier, and visualization planner.
- Eval failures should not automatically enter runtime guardrails or optimization loops. Promotion requires human validation, dual business/engineering ownership, severity/frequency/generalizability review, and rerunning the affected eval slice after changes.
- PRD review P2-1 decision: formal PRD should use Chinese as the main language while preserving key English technical terms such as `Answer Package`, `capability card`, `accepted graph`, `claim group`, and `verifier`.
- PRD review P2-2 decision: formal PRD should include a short glossary for key terms including `accepted graph`, `capability card`, `factor ledger`, `claim group`, `Answer Package`, `evidence envelope`, `business object impact`, `question tool`, and `verifier`.
- PRD review P2-3 decision: formal PRD should reference the local demo files as UX pattern examples only: `app/page.tsx` and `app/api/langgraph/route.ts`. The PRD must state that demo details do not define production protocol, architecture, graph contract, data contract, or implementation commitments.
- PRD review P2-4 decision: formal PRD should include lightweight source traceability at major-section level, such as short notes pointing back to PRD interview decisions or product decision sections. Avoid heavy per-sentence footnotes.
- PRD P1 refinement decision: the first vertical slice expectation package should include concrete regression thresholds for month coverage, direction consistency, material uplift, exception handling, downgrade, and data-quality block behavior while keeping `pattern_scan` generic.
- PRD P1 refinement decision: capability cards should include a minimal compiler/verifier spec: required parameters, optional parameters, typed evidence payload, lint severity, degradation output, and verifier hooks.
- PRD P1 refinement decision: graph compiler behavior should be expressed as an action table for block, auto-add, degrade, targeted repair, skip, verifier repair, and human-reviewed failure promotion.
- PRD P1 refinement decision: production launch gates should include performance budgets, deployment health checks, version rollback, minimum observability fields, and launch dashboard alert categories.
- PRD P1 refinement decision: launch acceptance matrix skeleton should include representative SSOT factor groups and ledger states to cover for every question family.
- PRD signoff refinement decision: ledger statuses should use the same names as `data_contract_state`: `contract_backed`, `evidence_linked`, `static_assumption`, `missing_contract`, `permission_limited`, `unsupported_grain`, and `out_of_scope_for_now`.
- PRD signoff refinement decision: launch acceptance matrix should separate allowed claim/evidence type from allowed strength or wording limit.
- PRD signoff refinement decision: launch representative cases require `answer_verify`; `data_quality_check` is required for first-screen claims and strong claims.
- PRD signoff refinement decision: the first vertical slice should be expressed as required evidence paths for the broader `pattern_explanation` problem class. The month-start case is a regression example; sibling pattern families reuse the same evidence-path shape with their own windows, candidates, visuals, and thresholds.
- Factor ledger PRD depth: define the concept layer, review workflow, reconciliation, and runtime impact before deciding final table structure.
- Conceptually, factor ledger should record each factor's business meaning, data status, supported question families, supported capabilities, supported grain, allowed claim/evidence type, allowed strength or wording limit, known gaps, owner/review status, and upgrade path.
- Runtime should use the ledger to decide whether a factor can support a quantified claim, candidate mechanism, contextual evidence, degradation, missing-contract limitation, or out-of-scope response.
- Factor ledger support state should be expressed by factor, question family, capability, supported grain, and claim type rather than by factor alone.
- Payday should be modeled as a consistent event dimension that applies across the relevant analysis information for WAJE's initial business framing.
- Payday uses the shared `25..30` business window across relevant scenarios; evidence strength and claim precision come from tested windows, stability, and verifier checks.
- Factor ledger review should use dual ownership. Business owner reviews business meaning, explanatory validity, and claim boundaries. Data/engineering owner reviews data contracts, grain, permissions, executable capabilities, and runtime feasibility.
- `.mm` to factor ledger reconciliation: `付费金额影响因子分析.mm` remains the business source tree, and factor ledger must cover every relevant metric, factor, dimension, event, formula, and missing-contract item from that tree with an explicit runtime support status.
- Ledger status should make every SSOT node reviewable: `contract_backed`, `evidence_linked`, `static_assumption`, `missing_contract`, `permission_limited`, `unsupported_grain`, or `out_of_scope_for_now`.
- Launch acceptance requires every relevant SSOT node to have an explicit ledger status. A factor does not need to support every strong claim, but it cannot remain unknown for supported baseline question families.
- Core question families must not pass launch with invisible SSOT gaps. Unsupported or limited factors should appear as degraded paths, missing contracts, unsupported grains, permission limits, or out-of-scope baseline decisions.
- Capability cards are the business and evidence boundary context exposed to the LLM so it can propose a valid candidate capability graph.
- They should also provide validation material for the local graph compiler and answer verifier.
- First-version capability cards should define business use cases, non-use cases, parameter boundaries, evidence outputs, and lint rules.
- Capability cards should be written in business language first, with structured constraints attached.
- Business language helps the LLM choose the right capability for a user question. Structured constraints give the graph compiler and verifier stable validation material.
- Graph compiler action boundary: safety and legality issues block, evidence or contract gaps degrade, business-planning gaps trigger targeted LLM repair, and deterministic guardrails are auto-added.
- Block examples: permission failure, SQL safety risk, invalid metric contract, illegal grain/filter/window, or claim path that would require forbidden data.
- Degrade examples: missing contract, unsupported grain, weak evidence, sparse data, insufficient coverage, or contextual-only event evidence.
- Targeted LLM repair examples: missing key analysis node, conflicting recipe expectations, unclear target claim, or graph path that does not answer the user question.
- Auto-add examples: data quality check, cumulative-value guard, timezone guard, contract version pinning, permission check, completeness check, and evidence normalization.
- Graph compiler actions should be visible in the product as business-facing process events, while technical details stay in developer/debug surfaces.
- User-facing examples: "checked cumulative-value risk", "activity evidence is limited because exposure data is missing", or "permission limits prevent segment-level explanation".
- Technical ids, contract refs, evidence refs, node ids, and raw validation payloads should not be shown in the default business UI.
- Production requirements in the PRD should be written as launch gates and user-visible product effects rather than detailed implementation design.
- PRD should state that permissions constrain available conclusions, auditability makes answers traceable, data snapshots make reruns comparable, performance budgets can limit graph depth, deployment must support the baseline workflow, and observability must help locate failed or degraded runs.
- 21st Agent Elements / SDK should be treated as a candidate frontend interaction approach, not as a BI truth source.
- PRD should lock the desired experience: Codex-like agent shell with 21st Agent Elements style, thread flow, tool groups, question tool, streaming progress, answer cards, and LangGraph event rendering.
- Final SDK adoption should depend on whether it can carry WAJE's thread model, tool grouping, question tool, streaming events, auth/session boundary, permissions, evidence cards, and LangGraph integration without moving BI authority out of WAJE-owned backend systems.
- The current local demo should be treated as a UX pattern demo only. It proves interaction direction such as agent shell, todo, tool groups, question workbench, and answer cards, but it does not define production architecture, protocol, data contracts, graph shape, or implementation commitments.
- Demo details must not overfit PRD decisions. PRD and implementation should generalize from the UX pattern to the broader supported problem domains.

## Confirmed

- WAJE BI v2 is a clean-slate production baseline. The old WAJE BI project is reference material only: business definitions, sample data, failure cases, selected algorithms, and test experience.
- The end goal is to answer any retrospective question about `付费金额` influence, including formula decomposition, periodic patterns, cross-factor attribution, outliers, black-swan events, external evidence, and full quantification where data supports it.
- The first production baseline follows the full-coverage launch standard: core capability depth, workflow coverage, evidence contracts, verifier behavior, and user-facing explanation must be production-complete at launch. Core analytical gaps should not be deferred as post-baseline P0 work.
- Launch acceptance should use a two-axis matrix: user question types by SSOT factors/capabilities. A launch candidate passes only when major retrospective payment-amount question families run end to end and every SSOT factor has an explicit support status for those question families.
- SQL-first means capabilities are backed by database contracts, semantic contracts, validated queries, evidence, and verifier checks. The LLM orchestrates only through constrained skill-like capability APIs.
- `付费金额影响因子分析.mm` is the factor SSOT. Metrics, factors, formulas, dimensions, missing contracts, and routes should trace back to this tree.
- Recipes can exist as analysis skeletons, but they cannot become hard constraints. Recipe-guided planning and bottom-up LLM planning must use the same standard capability APIs.
- Standard capability APIs must be generic and parameterized across metric, grain, dimensions, filters, baselines, evidence requirements, and insight needs.
- Cross-factor attribution must support joint or pre-joint analysis over base data, including two or more dimensions. Candidate related dimensions should stay open, including outliers and black-swan events.
- The system must distinguish accounting contribution, statistical association, candidate mechanism, causal evidence, insufficient evidence, and data gaps.

## Launch Acceptance Matrix

Confirmed choice: business question families with capability tags.

- Acceptance rows should use business question families so evaluation matches real user wording.
- Each question family should carry capability tags so evaluation remains tied to executable WAJE capabilities and evidence contracts.
- Initial question families:
  - Periodic/pattern questions: month-start vs mid/end, weekly pattern, quarter/year seasonality, holiday-relative windows, event-relative patterns.
  - Change attribution questions: month-over-month, year-over-year, custom baseline, sudden rise/drop, sustained trend change.
  - Revenue health questions: payment amount health, payment funnel, formula decomposition, user/order/success-rate/amount structure.
  - Event impact questions: activity, payday, holiday, product/operation action, campaign, external event, policy/network/weather/competitor context.
  - Anomaly and black-swan questions: abnormal month/window, abnormal channel/segment, outlier factor, sudden external shock.
  - Dimension and combination attribution questions: single-factor, two-factor, and higher-order combinations such as channel x new/returning user x region.
- Capability tags should include `pattern_scan`, `formula_decompose`, `joint_attribution`, `event_evidence`, `outlier_scan`, `segment_bridge`, `data_quality_check`, and `answer_verify`.
- Each matrix cell should store two fields: `business_evidence_state` and `data_contract_state`.
- `business_evidence_state` describes what claim the factor/capability/question intersection can support, such as `quantifiable`, `candidate_mechanism`, `contextual_evidence`, `insufficient`, `permission_limited`, `unsupported_grain`, or `out_of_scope`.
- `data_contract_state` describes why the system can or cannot execute the analysis, such as `contract_backed`, `evidence_linked`, `static_assumption`, `missing_contract`, `permission_limited`, `unsupported_grain`, or `out_of_scope_for_now`.
- Acceptance pass thresholds should be defined by question family and claim type, not by one universal cell state.
- Quantified contribution or formula claims generally require `quantifiable` evidence with executable contracts; candidate mechanism claims may pass with `candidate_mechanism` plus valid event/static/evidence linkage; context-only explanations may pass as `contextual_evidence` only when wording is limited.
- Any main conclusion that would rely on `insufficient`, `missing_contract`, `unsupported_grain`, or permission-blocked evidence must fail that claim path and be degraded, omitted, or shown as a limitation.
- The matrix should verify both end-to-end business answers and factor/capability coverage.

## Launch Evaluation

Confirmed choice: real user wording plus structured expectation packages.

- Each eval case should include the user's natural-language question, expected question family, expected intent/scope, required and forbidden capabilities, key `business_evidence_state` and `data_contract_state` expectations, allowed claim/evidence type, allowed strength or wording limit, expected visual blocks, and verifier pass/fail requirements.
- Eval cases should test both LLM understanding and local execution constraints: graph compilation, capability selection, contract validation, evidence production, answer wording, visualization plan, and final verifier behavior.
- Failure-case evals should include prior WAJE BI mistakes, especially classifying the full-sample month-start question as intra-period pattern analysis rather than period-over-period change or cumulative-value analysis.
- The eval suite should be generated from the launch acceptance matrix so each question family, core capability, SSOT factor status, degraded path, and evidence-strength boundary has coverage.
- Eval samples should come from three pools: real user questions, historical failure cases, and matrix-generated boundary cases.
- Real user questions preserve natural business wording and follow-up behavior.
- Historical failure cases guard known product risks such as wrong question family, over-strong causal wording, wrong baseline, cumulative-value misuse, hidden gaps, and misleading charts.
- Matrix-generated boundary cases cover sparse combinations that real usage may not hit often, such as permission limits, missing contracts, unsupported grain, high-order attribution, weak-evidence degradation, and black-swan candidate handling.
- Structured expectation packages should be maintained through business gold standards, system-generated drafts, and human review.
- Business gold standards define question family, important factors, acceptable conclusion strength, and disallowed wording.
- The system can draft required/forbidden capabilities, scope, evidence states, visual blocks, and verifier checks from the acceptance matrix and capability ledger.
- Reviewed expectation packages become regression tests.
- Eval should run in layers: high-frequency smoke evals, relevant slice evals, and full acceptance evals.
- Prompt, graph compiler, answer synthesizer, verifier, or orchestration changes should run smoke evals.
- Capability, contract, ledger, or semantic-query changes should run the affected question-family and SSOT-factor slices.
- Release candidates, model/provider changes, and major prompt changes should run the full launch acceptance eval.
- Eval failures should be labeled with both business failure type and system responsibility point.
- Business failure types include wrong question family, wrong scope, wrong baseline, missed key factor, over-strong weak evidence, hidden data gap, misleading visualization, and unsupported main conclusion.
- System responsibility points include LLM reasoner, graph compiler, capability API, semantic compiler, evidence reducer, answer synthesizer, answer verifier, and visualization planner.
- Eval-to-guardrail promotion requires human validation.
- Guardrail promotion should use dual ownership: the business owner confirms business risk, severity, and wording boundary; the engineering owner confirms the correct system target and implementation feasibility.
- Eval failures should be aggregated by question family, SSOT factor, capability tag, business failure type, system responsibility point, severity, frequency, and generalizability.
- Only validated, generalizable, high-risk, and system-expressible failure patterns should be promoted into runtime guardrails.
- Candidate runtime guardrail targets include intent lint, graph compiler lint, data quality checks, capability parameter validation, claim verifier rules, and visualization verifier rules.
- One-off wording issues or case-specific business events should stay in eval samples unless human review confirms a broader failure pattern.
- Guardrail actions should be severity-aware: block, repair, degrade, or tighten wording/visualization.
- Security, permission, SQL safety, and invalid metric-contract risks should block execution or claims.
- Deterministic intent, baseline, time semantics, or cumulative-value mistakes can trigger targeted repair.
- Missing contracts, unsupported grain, sparse evidence, or weak external evidence should degrade the claim path and surface limitations.
- Low-risk wording or visualization risks should tighten answer/verifier/visualization output rather than stop the whole run.
- Guardrail configuration should split hard rules from business boundaries.
- Hard rules such as permissions, SQL safety, metric-contract legality, time semantics legality, and cumulative-value misuse should live in code.
- Business boundaries such as claim wording limits, question-family pass thresholds, factor evidence status, and visualization risk policies should live in versioned contracts or ledgers.
- Guardrail contract or ledger changes should trigger the relevant eval slice before release.

## First Vertical Slice

Confirmed choice: start with the full-sample month-start payment amount pattern failure case.

- The first vertical slice should use the question: `全量样本看，为什么从 2024 年 1 月开始到 2026 年 5 月结束，为什么每个月月初的付费金额都比月中/月末高一些`.
- This slice should prove the full architecture path: intent classification, scope binding, pattern scan, event evidence, joint attribution, outlier/exception handling, data quality guardrails, answer verification, and inline visualization.
- The slice must classify the question as intra-period pattern analysis, not period-over-period change analysis or cumulative-value analysis.
- This slice should become the first high-value regression case and launch demo path.

## Answer Contract

Confirmed choice: two-layer `Answer Package`.

- Backend always generates the complete package: intent, scope, plan, metrics, evidence, missing data, reproducible path, and verifier result.
- Frontend first screen shows a business narrative with inline visualization/evidence blocks for conclusion, key quantification, top candidate explanations, exceptions, and major gaps.
- Key charts, tables, comparison blocks, contribution rankings, exception lists, and evidence-strength indicators should appear inline when they are semantically needed to understand the answer.
- Deeper technical audit artifacts such as raw evidence payloads, SQL or semantic query, technical refs, and verifier internals should be retained internally for audit/debug/export, but should not appear in the ordinary user-facing answer UI.
- Confirmed scope handling: mixed-grain user questions should be split into layered scopes, and every claim in the Answer Package should bind to the scope that supports it.
- Scope examples include `full_sample_month_phase`, `segment_contribution`, `aggregate_context_event`, `exception_period`, and `data_quality_scope`.
- The verifier should prevent evidence from one scope from supporting an over-specific or over-broad claim in another scope.
- Confirmed final-answer shape for layered scopes: the first screen should use business conclusion structure with embedded visual/evidence blocks, while each claim still maps to scope, evidence, query, chart/table, and verifier result for audit.
- The first screen should not expose internal scope names as the primary reading structure, but those scope names should remain available for audit and debugging.
- The answer should separate pattern existence, quantified contribution, candidate mechanism, exception explanation, data gaps, and evidence strength.
- When analysis branches stop as insufficient or degrade to exceptions, the first screen should group findings into `supported_explanations`, `local_or_exception_explanations`, and `insufficient_or_ruled_out`.
- Tested but unsupported candidates should be visible without dominating the first screen.
- Local/exception explanations must not be worded as broad full-scope causes.
- Each grouped item should include business-readable evidence, limitations, and relevant visual blocks inline where needed.
- The product should avoid a plain text answer followed by hidden evidence as the main experience. Inline evidence blocks are part of the answer itself, not only optional expansions.
- Technical internals such as judge decision records, evidence envelopes, raw payloads, SQL, and verifier internals remain internal artifacts unless an explicitly separate developer/debug surface is built later.

Visualization plan:

- Confirmed choice: Answer Package should include a `visualization_plan`.
- The LLM may recommend visual blocks as part of answer synthesis, but every visual block must reference existing evidence and pass local validation.
- The frontend should render the validated visualization plan rather than infer business importance from raw evidence payloads.
- Visualization blocks should declare block type, evidence reference, placement, purpose, metric/scope, supported claim, and any limitations.
- Common block types include pattern comparison charts, formula contribution tables, attribution ranking tables, event-window timelines, exception lists, evidence-strength indicators, and data-gap summaries.
- The verifier should ensure visual blocks do not overstate evidence or appear under unsupported claims.
- Visual blocks can show only verifier-allowed claims and visible grain. If evidence is insufficient, show a limitation or empty state rather than a misleading chart. If permission is limited, aggregate, mask, or hide the affected visual content.

Visualization layer:

- Confirmed choice: evidence-driven analytical visualization system as a core UX module.
- Visualization is part of the answer rendering core, not an optional evidence appendix.
- The system should support dedicated semantic views for major analysis families: `pattern_view`, `formula_view`, `attribution_view`, `event_view`, `anomaly_view`, `evidence_view`, and `data_quality_view`.
- Each view should have a clear business purpose, accepted evidence inputs, default visual forms, and interaction model.
- The visualization layer should be complete enough for production BI interpretation, including trends, comparisons, contribution views, rankings, timelines, exception views, evidence strength, limitations, and data quality status.
- The product should not start as a generic drag-and-drop BI chart builder. Visuals are selected by Answer Package semantics and validated evidence, not arbitrary chart configuration.
- Users should be able to ask follow-up questions from visual blocks when they reveal a segment, event, anomaly, or gap.

Visualization view depth:

- Confirmed first production baseline: all semantic views needed by supported question types should be production-complete at launch.
- High-frequency explanation views should be especially deep: `pattern_view`, `formula_view`, `attribution_view`, and `event_view`.
- `anomaly_view`, `evidence_view`, and `data_quality_view` should still be complete enough to support production answers, trust assessment, exceptions, limitations, and follow-up questions.
- `pattern_view` should clearly show recurring, window, seasonal, rolling, or event-relative patterns and baselines.
- `formula_view` should show metric decomposition, contribution, residual, and reconciliation.
- `attribution_view` should show candidate ranking, combination attribution, promotion path, coverage, stability, and evidence boundaries.
- `event_view` should show event timelines, pre/post windows, lag/recovery, overlap, and event-related exceptions.
- `anomaly_view`, `evidence_view`, and `data_quality_view` should be visible enough to explain trust, exceptions, and limitations, without becoming separate complex workspaces in the first baseline.

Visualization follow-up interaction:

- Confirmed choice: visualization blocks are follow-up objects with semantic context.
- Each visual block should carry semantic context such as metric, scope, filters, selected dimension values, time/window, evidence refs, and suggested follow-ups.
- Users should be able to ask natural follow-up questions from a visual block, chart element, table row, segment, event, anomaly, or data gap.
- The system should translate the selected visual context into a follow-up scope for the next investigation graph.
- Follow-up scope should preserve the previous answer context while narrowing to the selected segment/window/event when appropriate.
- Visual blocks should remain answer content first, but they also act as investigation entry points.

Example:

- `full_sample_month_phase`: claim that month-start payment amount is higher in the full sample; evidence from `pattern_scan`.
- `segment_contribution`: claim that a channel/user/payment combination contributes more; evidence from `joint_attribution`.
- `aggregate_context_event`: claim that payday window overlaps with month-start uplift; evidence from `event_window_scan`.
- The final answer can synthesize these claims, but each sentence should stay within its evidence scope.

Example first-screen structure:

```text
Conclusion:
Month-start payment amount is a stable recurring intra-month pattern in the full sample.

Inline visual blocks:
- Pattern comparison chart: month-start vs mid-month vs late-month.
- Formula contribution table: paid order count vs average paid amount.
- Candidate explanation ranking: payday window, user type, channel/payment combinations.
- Exception month list: activity/anomaly windows.

Main explanations:
1. Payday window has strong temporal overlap with the month-start uplift and is a supported aggregate candidate mechanism.
2. First-payment users and selected channel/payment combinations make the pattern more pronounced.
3. Some months deviate because of activity or anomaly windows.

Evidence strength:
- Pattern existence: high
- Payday-window mechanism: candidate mechanism, medium
- Segment contribution: varies by combination
- Exception months: listed separately
```

Action recommendation policy:

- The final answer may include operational recommendations, but they must be separated from factual conclusions.
- Factual conclusions state what the evidence supports.
- Recommendations should use check, validate, monitor, or follow-up wording and must not promise causal lift, revenue gain, or strategy outcome unless causal evidence exists.

## Question Compilation

Confirmed choice: `analysis_graph`.

- Every user question compiles into an analysis graph.
- Nodes are analysis subtasks and edges are dependencies.
- Simple questions can have one node; complex questions can branch into proof, quantification, exceptions, attribution, evidence checks, synthesis, and verification.
- Recipes become graph templates.
- Bottom-up LLM reasoning also produces the same graph shape.
- Local validators check every node for capability, parameters, permissions, data contracts, and evidence output.

## Planning Granularity

Confirmed choice: dual-layer planning.

- The LLM first produces a high-level business graph.
- The LLM also proposes candidate capability nodes and parameters.
- The local planner aligns both layers, fills safe defaults, rejects invalid nodes, and records recoverable failures.
- Business nodes preserve why the analysis is being done.
- Capability nodes preserve how the analysis is executed.

## Graph Validation And Repair

Confirmed choice: explainable repair.

- The local planner keeps executable graph nodes.
- Invalid or unavailable nodes are marked as `disabled` or `degraded`.
- Each disabled or degraded node records the missing contract, invalid parameter, permission issue, or execution blocker.
- The planner can attach an explicit substitute path when a weaker but honest analysis route exists.
- Disabled, degraded, and substituted paths must appear in the `Answer Package` so the final answer cannot hide missing data.

Example:

- Original path: play icon exposure -> play click rate -> play payment rate.
- Missing contracts: `play_icon_exposure`, `play_click`.
- Substitute path: use play payment amount, play active users, and play ARPU as weaker evidence.
- Answer gap: icon exposure cannot be used as a supported explanation until the missing contracts exist.

## Capability API Granularity

Confirmed choice: medium-grained capability APIs.

- API boundaries follow independently auditable analysis actions.
- Each capability should do one complete analysis action and return structured evidence.
- The graph should not be built from tiny mechanical operations such as raw bucketing or arithmetic.
- The graph should not call large all-in-one business APIs that hide planning, data gaps, or evidence.
- Examples: `pattern_scan`, `segment_bridge`, `joint_attribution`, `event_evidence`, `outlier_scan`, `formula_decompose`, `evidence_strength`, `answer_verify`.
- Capabilities must be generic and parameterized. They should not be hard-coded to the guardrail month-start case.

Capability organization:

- Confirmed choice: three layers: foundational evidence capabilities, composite analysis subgraphs, and recipe entry templates.
- Foundational evidence capabilities are reusable, medium-grained APIs such as `pattern_scan`, `formula_decompose`, `event_evidence`, `joint_attribution`, `outlier_scan`, `data_quality_check`, and `answer_verify`.
- Composite analysis subgraphs organize multiple capabilities for recurring analysis structures, such as pattern explanation, attribution, business object impact, revenue health, anomaly review, and data quality review.
- Recipe entry templates provide common starting points for business question families, such as intra-period pattern, event-relative explanation, paid amount change explanation, and revenue health review.
- The LLM sees capability cards and recipe/subgraph templates, selects a starting recipe, and produces a candidate capability graph.
- The graph compiler validates individual capability nodes, subgraph expectations, dependencies, scope, evidence output, and allowed deviations.
- LangGraph displays the accepted runtime graph, including subgraph execution, loops, branches, and degraded paths.

Foundational evidence capability set:

- Confirmed first production baseline: keep eight foundational capability boundaries and deliver each as production-complete for its launch-supported question types.
- Full capability boundaries: `pattern_scan`, `formula_decompose`, `joint_attribution`, `event_evidence`, `outlier_scan`, `segment_bridge`, `data_quality_check`, and `answer_verify`.
- `segment_bridge` should support contribution bridge over dimensions, mix-shift explanations, and segment-level evidence boundaries needed for supported attribution questions.
- `outlier_scan` should support abnormal period/window identification, anomalous segment or factor candidates, exception tagging, and black-swan candidate alignment needed for supported retrospective questions.
- The baseline should not collapse `segment_bridge` into `joint_attribution` or `outlier_scan` into `pattern_scan`; their evidence roles stay separate.

`data_quality_check` baseline:

- Confirmed choice: BI-specific guardrails, not only generic SQL safety or completeness.
- The first production baseline should cover completeness checks, cumulative-value guards, metric contract checks, time semantics checks, grain alignment checks, scope/filter checks, sample-size checks, and permission/field coverage checks.
- It should explicitly prevent using cumulative values as period/window values.
- It should validate whether analysis uses payment initiation time, payment completion time, or another declared time semantics.
- It should verify metric identity such as initiated amount, paid/success amount, paid order count, success rate, and average paid amount.
- It should surface quality flags into evidence envelopes and Answer Package limitations.
- Data quality impact should be evaluated per affected claim. Issues that can change metric facts or the main conclusion block the affected claim; issues that only affect local explanation degrade that path and show a limitation; minor gaps remain warnings.

Composite analysis subgraphs:

- Confirmed first production baseline: include six production-ready composite subgraph boundaries.
- Subgraphs: `pattern_explanation_subgraph`, `attribution_subgraph`, `business_object_impact_subgraph`, `revenue_health_subgraph`, `anomaly_review_subgraph`, and `data_quality_subgraph`.
- `pattern_explanation_subgraph`: prove/quantify pattern, decompose metric where relevant, connect event/segment candidates, and explain exceptions.
- `attribution_subgraph`: candidate screening, lower-order attribution, promotion loop, high-order search, stability/sparsity checks, and segment-level claims.
- `business_object_impact_subgraph`: business object binding, object-specific evidence route, event windows when applicable, pre/post, lag/recovery, overlap/stability, and impact quantification.
- `revenue_health_subgraph`: payment amount health through formula paths, user/order/success-rate/amount structure, channel/payment mix, and anomalies.
- `anomaly_review_subgraph`: abnormal period/window identification, exception tagging, anomalous segment/factor candidates, candidate black-swan alignment, and evidence limits.
- `data_quality_subgraph`: missing data, timezone, cumulative-value guard, completeness, scope coverage, contract coverage, sample-size risk, and permission/data-availability limits.

Recipe entry templates:

- Confirmed first production baseline: eight recipe entry templates, with multi-recipe matching allowed.
- Recipe entries: `paid_amount_change_explanation`, `pattern_explanation`, `business_object_impact_review`, `revenue_health_review`, `segment_or_factor_attribution`, `anomaly_or_black_swan_review`, `custom_baseline_comparison`, and `data_quality_or_evidence_review`.
- A user question can match multiple recipes. The LLM should propose the combined candidate capability graph, and the graph compiler should validate merged nodes, scopes, dependencies, and duplicate/overlapping evidence routes.
- The accepted runtime graph should be one visible LangGraph execution, even when it is seeded by multiple recipes.
- Recipe entries are business starting points, not hard reports.

Examples:

- `为什么本月付费金额下降` -> `paid_amount_change_explanation`.
- `为什么每周一付费金额更高` -> `pattern_explanation`.
- `春节前后5天付费金额是否更高` -> `business_object_impact_review` + `pattern_explanation`.
- `哪个渠道影响最大` -> `segment_or_factor_attribution`.
- `这个月是不是异常` -> `anomaly_or_black_swan_review`.

Multi-recipe merge rule:

- Confirmed merge shape: claim/scope-driven graph merge.
- Multi-recipe matches should first produce target claims and scopes, then merge capability nodes by claim, scope, metric, time range, window definition, filters, and expected evidence.
- Duplicate capability nodes should be reused when they answer the same target claim under the same scope.
- Similar nodes with different scopes or target claims should remain separate.
- `data_quality_check` and other deterministic guardrails can be shared across branches when their scope covers all dependent claims.
- `joint_attribution` should merge candidate pools where possible rather than running isolated duplicate attribution loops.
- Each merged node should retain `recipe_origin` so audit/debug views can show which recipe(s) requested it.
- Conflicting recipe expectations should produce compiler lint and targeted LLM repair rather than silent graph changes.

Capability selection and orchestration principle:

- Capabilities such as `pattern_scan`, `formula_decompose`, `joint_attribution`, `event_evidence`, `outlier_scan`, and `data_quality_check` do not have a fixed global execution order.
- Their relationship is defined by the user's intent, question context, metric contract, evidence gaps, and graph dependencies.
- The LLM Reasoner should infer latent evidence needs from the question and express them as candidate capability graph nodes, including pattern existence, metric decomposition, segment contribution, event context, anomaly review, and data quality.
- The local graph compiler/validator should accept, reject, repair, or degrade candidate capability graph nodes, marking which nodes can run in parallel and which require upstream evidence.
- A capability should run when it is needed to answer or verify a potential claim, unless policy, data contracts, permissions, or budget reject it.
- Outputs can create new evidence needs, but this is graph expansion, not a hard-coded trigger chain.
- Final synthesis should combine evidence packages by scope and claim, rather than implying that one capability caused another.

Clarification on plan representation:

- WAJE should avoid a separate layer that only translates abstract evidence needs into the same tool calls the LLM could have emitted directly.
- The useful LLM output is a candidate capability graph: concrete capability calls plus business rationale, target claim, scope, dependencies, and expected evidence.
- The local layer should act as a graph compiler/validator, not a second semantic reasoner.
- The graph compiler/validator validates capability names, parameters, permissions, contracts, metric compatibility, required guardrails, dependency shape, parallelism, defaults, budget, and evidence output requirements.
- Missing latent evidence should be handled through prompts, recipe/subgraph templates, contract-driven lint rules, and regression tests, rather than relying on a thin evidence-needs mapping layer.
- Example lint rule: a `why paid_amount changed` claim should include at least one valid formula or contribution route unless the graph explicitly records why that route is skipped or degraded.

SQL compilation boundary:

- LLM should not generate executable SQL draft in the normal production path.
- The LLM should work from capability schemas, metric/dimension/event contracts, allowed parameters, and contract summaries rather than full physical database schema.
- Capability graph nodes should compile into semantic requests first, then local semantic compiler generates SQL for ClickHouse/Postgres.
- Local validators own SQL safety, metric/dimension legality, grain/window/filter legality, permissions, time range checks, cumulative-value guards, query budget, and evidence ledger requirements.
- If an analysis cannot be expressed through existing capability parameters, the system should request a capability/contract extension or mark a degraded path, instead of asking the LLM to improvise executable SQL.
- SQL text can still be exposed as evidence/debug output after local compilation.

Shared semantic query planning:

- Confirmed choice: graph-level shared semantic query plan with reusable result references.
- Before execution, the graph compiler should collect semantic needs from accepted capability nodes and merge compatible needs by scope, time range, grain, filters, metric, dimensions, windows, and baseline requirements.
- The local semantic compiler should generate shared ClickHouse/Postgres queries where possible.
- Query execution should produce `result_ref` values that capability nodes can reuse or derive from.
- Capability nodes still produce independent evidence envelopes and typed payloads, even when they share lower-level query results.
- Shared results should record contract versions, query ids, data freshness, quality flags, and dependent capability nodes.
- The system should avoid unconditional ultra-wide intermediate tables; shared plans should be demand-driven by the accepted graph.

Thread-scoped result reuse:

- Confirmed choice: investigation threads can reuse prior `result_ref` values through a thread-scoped cache with validation.
- Reusable results should be keyed or validated by thread id, run id, contract versions, data snapshot/freshness, scope, filters, grain, metric, dimensions, window definitions, baseline definitions, and semantic query hash.
- Follow-up questions should let the graph compiler search prior result refs and reuse them only when data snapshot, contract versions, permission scope, and semantic scope match. Same or narrower scope may reuse; wider or changed scope must rerun affected nodes.
- If validation fails, the query should rerun or the prior result should be marked context-only. Context-only prior results cannot support a new claim.
- Reused results should still produce new evidence envelopes for the current run/claim when they support a new answer.
- The Answer Package should record when evidence reused prior result refs.

Capability card exposure to LLM:

- LLM should receive capability cards rather than physical database schema.
- A capability card should include capability name, use cases, non-use cases, parameter schema, examples, required evidence output, and lint rules.
- Capability cards should help the LLM build candidate capability graphs and help the graph compiler validate/lint those graphs.
- Capability cards should describe business and evidence boundaries, not ClickHouse/Postgres physical table details.
- Example lint: `pattern_scan` can prove and quantify a pattern, but a `why paid_amount changed` target claim also needs a valid formula, contribution, event, anomaly, or degraded route.

Capability cards and recipe/subgraph templates:

- Capability cards define single-capability contracts: what the capability does, when to use it, when not to use it, parameters, evidence output, and lint rules.
- Recipe/subgraph templates define common multi-capability paths for analysis families.
- The LLM can choose a recipe as a starting point, then add, remove, degrade, or mutate nodes using capability cards.
- The graph compiler should validate individual nodes against capability cards and validate overall graph gaps against recipe/subgraph template expectations.
- The accepted runtime graph may diverge from the starting recipe, but all divergence should be recorded through graph mutations and visible in LangGraph.

Confirmed candidate capability graph node fields:

- `node_id`
- `capability`
- `params`
- `purpose`
- `target_claim`
- `scope`
- `depends_on`
- `expected_evidence`
- `fallback_or_degrade_rule`
- Optional fields can include `priority`, `budget_hint`, `parallel_group`, `recipe_origin`, and `mutation_reason`.

These fields should support validation, LangGraph visualization, auditability, degraded-path reporting, and final answer verification.

Confirmed graph compiler repair boundary:

- When a candidate graph misses required business analysis nodes, the graph compiler should emit a lint finding and ask the LLM for a targeted graph repair rather than silently changing the business route.
- The LLM repair should add the missing node, explain why it is unnecessary, or mark the route as degraded with a reason.
- The graph compiler may automatically add deterministic guardrail nodes or metadata when they do not change the business analysis route.
- Auto-add examples: `data_quality_check`, permission checks, contract version pinning, cumulative-value guards, timezone guards, completeness checks, and evidence-output normalization.
- The compiler should record every repair, degradation, and auto-added guardrail in the run state and Answer Package.
- This keeps LLM responsible for business planning while local systems enforce production safety and evidence completeness.

Example:

- User asks whether payment amount is higher before major holidays and why.
- The graph can run `pattern_scan(event_relative)` to check the holiday-relative pattern, `formula_decompose` to split payment amount into count/success-rate/avg-amount paths, and `event_evidence` to validate holiday windows.
- If the formula evidence shows the uplift comes mostly from paid order count, `joint_attribution` can focus on count-related dimensions.
- If `pattern_scan` fails to support the pattern, formula evidence can still be reported as exploratory or the graph can stop the causal explanation path.

Generic `pattern_scan` principle:

- `pattern_scan` should detect and quantify recurring or anchored patterns across configurable grains and windows.
- Supported pattern families should include intra-period patterns, week patterns, quarter/year seasonality, custom baseline comparisons, event-relative windows, pre/post windows, lag/recovery windows, and rolling-window patterns.
- The capability should accept parameters such as target metric, population scope, time range, time grain, phase/window definition, anchor event, comparison baseline, filters, minimum support, and quality checks.
- Month-start vs mid/late is one configuration of `pattern_scan`, not a dedicated capability.
- Confirmed implementation boundary: expose one generic `pattern_scan` contract with internal strategy selection by `pattern_family`.
- `pattern_family` examples include `intra_period`, `event_relative`, `seasonality`, `pre_post`, `lag_recovery`, `rolling_window`, and `custom_baseline`.
- Internal strategies may differ, but they must return a unified pattern evidence package so downstream attribution, verification, and answer synthesis can consume one shape.
- This avoids overfitting the public capability API to a single pattern while keeping implementation logic modular.

Confirmed pattern evidence package:

- `pattern_identity`: pattern family, target metric, scope, time range, grain, window definition, and baseline definition.
- `existence`: supported flag, strength, support count, total count, and stability.
- `quantification`: absolute delta, relative delta, contribution amount where applicable, and effect size.
- `comparison`: target windows, baseline windows, and per-period results.
- `exceptions`: weak periods, reversed periods, and outlier periods.
- `quality_checks`: completeness, timezone, missing periods, cumulative-value guard, and sample size.
- `downstream_hints`: candidate dimensions, candidate events, and recommended next capabilities.
- `evidence_refs`: query id, result table reference, contract versions, and capability version.
- All pattern families should return this shape, with unsupported fields marked explicitly rather than omitted when possible.

Examples:

- Month pattern: compare day `1..10` vs `11..20` vs `21..end` across months.
- Week pattern: compare weekdays vs weekends or Monday vs other weekdays.
- Quarter pattern: compare beginning/middle/end of quarter.
- Year pattern: compare seasonal months or yearly recurring windows.
- Event pattern: compare `T-5..T-1`, `T`, `T+1..T+5` around a major holiday or product event.
- Custom baseline: compare target window against prior period, same weekdays, same month phase, or user-defined control window.

Generic `formula_decompose` principle:

- Formula decomposition should be driven by metric contracts, not hard-coded in recipes and not invented by the LLM at runtime.
- Metric contracts should declare valid decomposition paths, required fields, grains, filters, and numerical reconciliation rules.
- Runtime should evaluate every current-data-covered decomposition path in the metric contract that passes field, grain, permission, sparse-cell, and budget gates for the question.
- The system should select a `primary_formula` after scoring the eligible paths, and keep useful `auxiliary_formulas` for supporting business interpretation or explaining disagreement.
- Formula scoring should consider explanatory power, residual reduction, fit, stability, coverage, sample size, sparse-cell risk, component contract strength, and business readability.
- `formula_decompose` should return structured evidence for every evaluated path: component level, delta, contribution, residual, fit, reconciliation status, and whether the path is primary, auxiliary, degraded, or blocked.
- Quantified decomposition uses reviewed formula components only. Residual is kept as `residual / unexplained` and is not force-allocated into reviewed components.
- If residual is high or fit is weak, runtime should try higher-order attribution or segment decomposition when contracts, permissions, sparse-cell rules, and budget allow it.
- After useful promotion loops, quantified contribution can publish when residual is `<= 10%` of total change and fit is acceptable. If residual remains `> 10%` or fit stays weak, the answer degrades to leading candidate factors plus visible unexplained residual.

Current-data-covered `paid_amount` decomposition candidates from `contracts/metrics/paid-amount.metric.yaml`:

```text
paid_dau_arpu:
  paid_amount = paid_dau * paid_dau_arpu

paid_user_arppu:
  paid_amount = paid_dau * paid_user_conversion_rate * paid_amount_per_paid_user

new_user_funnel_dashboard:
  dashboard_funnel_components = new_users, registrations, registration_rate_new_base,
  first_pay_users, first_pay_rate_new_base, same_day_new_paid_users, same_day_new_paid_rate

frequency_ticket_size:
  paid_amount = paid_user_count * payment_frequency_per_paid_user * avg_paid_amount_per_payment

region_sum:
  paid_amount = sum(paid_amount by region)

device_sum:
  paid_amount = sum(paid_amount by device_model)
```

- `paid_dau_arpu` and `frequency_ticket_size` are first-runtime quantitative candidates where current paid-order snapshot contracts support the needed components.
- `paid_user_arppu` and `new_user_funnel_dashboard` enter as dashboard daily auxiliary candidates with evidence-linked strength until dashboard field meanings and component contracts are fully reviewed.
- `region_sum` and `device_sum` enter as dimension bridge candidates when dimension, masking, sparse-cell, and permission gates allow the visible grain.
- Runtime should not limit first-runtime formula exploration to three hard-coded paths.
- First-runtime primary-claim eligibility: `paid_dau_arpu` and `frequency_ticket_size` can become quantified primary formulas when reconciliation passes, residual is `<= 10%`, and fit is acceptable.
- `paid_user_arppu` and `new_user_funnel_dashboard` can support directional or structural auxiliary explanation until dashboard component contracts are fully reviewed.
- `region_sum` and `device_sum` can support segment primary conclusions only when dimension contracts, masking, sparse-cell, permission, and visible-grain gates all pass.
- Dashboard auxiliary paths cannot override `paid_order_detail` as the main `paid_amount` fact source in overlapping dates.

Dimension bridge visible-grain policy:

- All contracted aggregate dimensions are equal primary candidates when contract, sparse-cell, masking, missing-value, and permission gates pass.
- This includes channel, payment method, amount bucket, geo/city aggregates, device brand/model, OS, and network type.
- Raw user ID, raw IP, and raw device ID remain internal-only and cannot be answer-visible dimensions.

Dimension bridge missing-value policy:

- Unknown, blank, null, missing, or unavailable dimension values remain explicit data-quality buckets in dimension bridges such as `region_sum`, `device_sum`, and channel bridge paths.
- Runtime must not drop these buckets from reconciliation and must not redistribute their amount into known buckets.
- If the missing-value bucket is material by amount or share, the answer should surface it as a data-quality limitation or attribution boundary.
- Missing-value buckets cannot be described as a real region, device, channel, or business segment.
- Missing-value impact thresholds:
  - Missing bucket `< 5%` and amount `< 30M NGN`: dimension bridge primary conclusion is allowed with warning.
  - Missing bucket `5%-20%` or amount `30M-150M NGN`: dimension bridge can support auxiliary explanation only, not global primary attribution.
  - Missing bucket `> 20%` or amount `> 150M NGN`: block that dimension bridge as a primary conclusion and state the missing-value limitation.

## Evidence Model

Confirmed choice: dual-axis evidence model.

- Every claim has an `evidence_type` and a `strength`.
- `evidence_type` describes what kind of support the claim has.
- `strength` describes how strong that support is for this dataset and question.
- This separation prevents statistical association, accounting contribution, candidate mechanism, and causal evidence from being collapsed into one confidence label.

Initial evidence types:

- `accounting_contribution`: deterministic formula, bridge, decomposition, or segment delta.
- `statistical_association`: correlation, pattern recurrence, lag association, or stability result.
- `candidate_mechanism`: plausible business mechanism with temporal or structural alignment.
- `causal_evidence`: stronger historical evidence with control, counterfactual, treated/control split, or intervention design.
- `insufficient`: route exists conceptually, but data, coverage, stability, or method quality is not enough.

Causal wording policy:

- Confirmed causal wording is allowed only when the claim has `causal_evidence` backed by experiment/control, exposure-control data, quasi-experimental design, or an owner-reviewed causal contract.
- Trend, association, formula decomposition, and dimension contribution evidence should use wording such as related to, overlaps with, candidate explanation, or possible influence path.
- The verifier should block causal wording for `accounting_contribution`, `statistical_association`, `candidate_mechanism`, contextual evidence, or `insufficient` claims.

Initial strengths:

- `high`
- `medium`
- `low`
- `insufficient`

Examples:

- Monthly-start payment amount pattern: `statistical_association` + `high` when most months repeat and robustness checks hold.
- Payday explanation: `candidate_mechanism` + `medium` when timing aligns but user income or region-level match is missing.
- Channel structure contribution: `accounting_contribution` + `low` when bridge is computable but unstable across months.

## Evidence Ledger Output Contract

Confirmed choice: unified evidence envelope plus capability-specific typed payload.

- Every capability should return an `evidence_envelope` with common audit and verifier fields.
- Capability-specific details should live in a typed `payload`.
- The evidence ledger should store or reference both the envelope and payload.
- The verifier and frontend should rely on the envelope for common behavior, then open typed payloads for details.

Common envelope fields:

- `evidence_id`
- `run_id`
- `node_id`
- `capability`
- `scope`
- `target_claim`
- `evidence_type`
- `strength`
- `supported_claims`
- `disallowed_claims`
- `contract_versions`
- `semantic_query_ref`
- `sql_ref`
- `result_ref`
- `quality_flags`
- `limitations`
- `created_at`

Typed payload examples:

- `pattern_evidence_package`
- `formula_decompose_result`
- `joint_attribution_result`
- `event_evidence_result`
- `outlier_scan_result`
- `data_quality_result`

## Joint Attribution Search

Confirmed choice: LLM-guided evidence-driven high-order exploration.

- Multi-dimensional attribution starts with lower-order dimensions for interpretability.
- The LLM decides whether to promote the search to higher-order combinations based on the business question, current residuals, observed fit, candidate mechanisms, and previous evidence.
- Residual or weak-fit findings from formula decomposition should trigger this promotion path before the answer degrades.
- Local validators enforce hard constraints: data contracts, permissions, sample size, sparse-cell limits, metric compatibility, statistical guardrails, runtime budget, and evidence output.
- Higher-order exploration has no fixed product-level dimension cap, but every promotion must record why it was attempted and what explanatory value it added.
- Runtime should stop promotion once the lowest-complexity combination explains the question with acceptable fit, residual, stability, and business readability.
- If multiple combinations fit, the main conclusion should use the lowest-complexity sufficient combination; higher-order combinations stay as auxiliary detail unless they materially change the business interpretation.
- When multiple explanations pass evidence gates, main-conclusion ranking should prefer business-actionable explanations such as channel, payment method, user type, activity, and operation event. Descriptive dimensions such as city or device model rank lower unless their explanatory power is materially stronger.
- If a lower-order combination does not fit but a higher-order combination does, the answer must describe the more specific business segment instead of over-attributing to a broad dimension.
- If no stable higher-order structure is found, the Answer Package must list what was tested and why evidence remained insufficient.

Example:

- `channel` does not explain a month-start uplift.
- `channel x payment_method` still has weak fit.
- The LLM promotes to `channel x payment_method x user_type` because residuals concentrate in first-payment users.
- A stable segment appears: one channel, one payment method, first-payment users, concentrated near payday.
- The final claim is about that specific combination, with evidence type and strength attached.

## Candidate Dimension Sources

Confirmed choice: three-layer candidate pool with unified event candidates.

- `registered`: dimensions, metrics, formulas, and event types from the SSOT and registered data contracts. These are default candidates for planning and attribution.
- `discoverable`: fields, enum values, event rows, anomaly clusters, and structural shifts discoverable from loaded data. These can become candidates when they show measurable relationship to the target.
- `hypothesized`: LLM-proposed business hypotheses or external shocks. These remain candidate mechanisms until they are connected to a data contract, event record, source evidence, or explicit missing-contract request.
- Confirmed candidate pool shape for joint attribution: local screening should use all eligible `registered` candidates, add `discoverable` candidates from profiling/anomaly scans, and allow `hypothesized` candidates only when they can be connected to event evidence, external evidence, a data contract, or a missing-contract record.

External factors and external events can be stored in different source tables or as typed records in a shared event table. The storage shape can differ by source, but analysis consumes them through a unified event candidate contract.

Examples:

- Dedicated tables: payday calendar, holiday calendar, weather incidents, electricity/network incidents, competitor events, policy changes, campaign operations, product releases.
- Shared typed event table: `event_type`, `event_time`, `event_window`, `location`, `affected_scope`, `source`, `confidence`, `metadata`.
- Candidate combinations: `month_phase x payday_window`, `channel x holiday`, `payment_method x network_incident`, `geo x weather_event`, `activity x user_type`.

The system should allow LLMs to propose external factors, then resolve them to registered or discoverable event candidates when data exists. If data does not exist, the factor remains a hypothesized mechanism with a missing contract.

## Hypothesized Candidate Evidence States

Confirmed choice: hypothesized candidates use three evidence states with a pre-launch data coverage review.

- `unresolved`: a user or LLM hypothesis exists, but it has no event record, external evidence, data contract, or agreed static assumption. It can be discussed only as an unverified hypothesis or missing evidence.
- `evidence_linked`: the hypothesis is connected to a source, manual event record, external evidence, or agreed static assumption. It can support candidate-mechanism analysis such as temporal overlap and exception explanation.
- `contract_backed`: the hypothesis is backed by a maintained data contract, event table, static dimension table, or source pipeline. It can enter local screening, window scan, and joint attribution.

Event evidence policy:

- Reviewed event records, static assumptions, or source contracts can support contextual explanation or candidate-mechanism claims.
- Without exposure/control data, quasi-experimental design, or an owner-reviewed causal contract, event evidence cannot support confirmed impact or causal wording.
- Strong time/scope match may enter auxiliary explanation. It can enter the main conclusion only when evidence type, strength, coverage, and verifier gates allow it.

Pre-launch requirement:

- Before production launch, WAJE BI v2 must review every SSOT factor and decide whether data exists, whether a static or semi-static table should be maintained, whether the factor should start as an agreed assumption, or whether it remains a missing contract.
- This review should include internal payment/order fields, user dimensions, channel/payment dimensions, geo/device dimensions, product/operation events, failure reasons, external events, and black-swan candidates.
- Each factor should have a launch status: `contract_backed`, `evidence_linked`, `static_assumption`, `missing_contract`, `permission_limited`, `unsupported_grain`, or `out_of_scope_for_now`.
- Static assumptions must record owner, source, valid window, refresh rule, allowed strength, and wording limit.

## SSOT Data Capability Ledger

Confirmed choice: full SSOT capability ledger before production launch.

- Every factor in `付费金额影响因子分析.mm` should be reviewed and recorded in a capability ledger before launch.
- The ledger should record whether data exists, data source, whether a static or semi-static table should be maintained, supported analysis capabilities, allowed claim/evidence type, allowed strength or wording limit, current gaps, upgrade path, and launch priority.
- The ledger should feed the launch acceptance matrix by marking each factor/capability/question-type intersection with both `business_evidence_state` and `data_contract_state`.
- Conceptual ledger shape should have factor master records plus capability-support records, so the SSOT factor remains stable while support varies by capability, question family, grain, and claim type.
- Concrete table structure, primary keys, and storage layout are deferred to the contract/schema design phase.
- This ledger becomes the product-facing source for missing contracts, static assumptions, and contract backlog.
- It should cover internal fields and external factors, including payment/order metrics, users, channel/payment, geo, device/environment, amount structure, product/operation events, payment failure reasons, external events, holidays, payday, competitor/policy/network/weather, and black-swan candidates.
- A factor without data can still be represented, but it must be marked as `missing_contract`, `static_assumption`, or `out_of_scope_for_now` and must not support stronger claims than its status allows.

Confirmed maintenance shape: business review sheet plus repo contract source files with automated reconciliation.

- A business-facing review sheet should be generated from the SSOT so each factor can be confirmed for data status, source, assumptions, priority, and notes.
- Confirmed entries should be represented in versioned repo source files for contracts, capability ledger records, static assumptions, and missing-contract backlog.
- Automated checks should detect factors missing from the ledger, mismatches between the business review sheet and repo sources, invalid statuses, unsupported claim/evidence types, unsupported strengths or wording limits, and missing backlog entries.
- Runtime execution should read the versioned contract/ledger sources or their Postgres runtime mirror, not the business review sheet directly.
- The business review sheet is for alignment; the repo contract source is for production execution.

Confirmed grain-aware evidence boundary:

- Every factor should declare supported analysis grain, unsupported grain, allowed claim types, and disallowed claim types.
- Evidence availability must be evaluated against the user's question grain. A factor can support aggregate analysis while being insufficient for fine-grained segment claims.
- Static or semi-static event facts should be used at the grain they validly support, even when finer-grained data is unavailable.
- The verifier should use these declarations to block over-specific claims.

Example:

```text
factor: payday
status: static_assumption
default_window: monthly 25..30
allowed_evidence: candidate_mechanism
supported_capabilities: event_window_scan, pattern_context
supported_grain: aggregate_daily, month_phase, full_sample_pattern
allowed_claims: payday-window temporal alignment, candidate business mechanism
disallowed_claims: confirmed cause without supporting evidence
upgrade_path: maintained payday calendar if the 25..30 business window changes
priority: high
```

Payday default fact:

- For the initial WAJE business context, salary/payment-day analysis should use an agreed default payday window of each month `25..30`.
- This agreed window should be materialized or represented as a `payday_calendar`-style static event contract.
- For relevant payment amount questions, the `25..30` payday window is a valid contextual event candidate and should not be treated as `missing_contract`.
- Evidence wording follows evidence strength: payday-window alignment can support a candidate mechanism or contextual explanation; confirmed cause wording requires additional support.

## Event Time Relationship Model

Confirmed choice: LLM-proposed windows plus local window scanning.

- Event contracts can define default windows.
- The LLM can propose expanded or alternative windows based on the business question and event type.
- Local capabilities scan candidate windows, compare strength, and report stability.
- The Answer Package must record tested windows, best-supported windows, unsupported windows, and missing event contracts.
- This supports lead effects, same-period effects, lag effects, and recovery effects.

Examples:

- Payday: `T-3..T-1` expectation, `T..T+3` main effect, `T+4..T+7` lag.
- Holiday: pre-holiday, holiday, and post-holiday windows.
- Network or electricity incident: during-incident and post-incident recovery windows.
- Product activity: warm-up, active period, and post-activity decay.

## Answer Verification

Confirmed choice: insight-first answer generation with post-hoc claim verification.

- The system should not force the LLM to generate the business answer only by translating structured evidence fields.
- Local tools first produce an evidence brief in business-readable form: patterns found, stronger combinations, supported explanation paths, data gaps, and anomalous periods.
- The LLM writes the business insight from the evidence brief and full evidence context.
- The LLM should output both the business answer and a structured check list of claims, numbers, scopes, evidence refs, and wording strength that need verification.
- An LLM auditor then reviews whether the natural-language answer matches the structured check list and whether any unlisted claims or stronger wording appear.
- Local hard checks validate the structured check list against evidence, scope, metric definitions, disabled/degraded paths, missing contracts, allowed claim/evidence type, allowed strength or wording limit, supported/disallowed claims, and numeric values.
- Local code should not rely on brittle natural-language number extraction for primary verification.
- LLM semantic verification handles claim extraction, wording strength, and business-language mapping; final hard constraints stay local.
- Strict mode can use a different model or provider for the audit pass when risk warrants it.
- If verification fails, the system sends targeted repair feedback to the LLM rather than rewriting the business answer locally.

Examples of repair feedback:

- "`导致` is too strong for a `candidate_mechanism + medium` claim; use wording like `有稳定重合` or `是候选解释之一`."
- "`主要来自首充用户` needs quantified contribution evidence; use wording like `在首充用户中更明显` unless contribution is measured."
- "`组合效应` requires joint attribution evidence for that combination."

## Frontend Product Shape

Confirmed choice: Codex-like Investigation Thread.

- The primary experience is an analysis thread, not a traditional BI dashboard.
- Users ask questions naturally in a thread.
- The thread progressively shows the Agent's understanding, plan, capability calls, successful paths, degraded paths, missing contracts, and final business insight.
- Analysis cards live inline in the conversation and can be expanded when needed.
- Detailed evidence, charts, queries, evidence ledger rows, and verifier results open from cards as temporary inspectors or drawers.
- Threads should be recoverable, follow-up friendly, and exportable.

Investigation artifact:

- Confirmed choice: save analysis results as shareable investigation artifacts, with optional static export.
- A thread is the working process; an artifact is the reusable result.
- Artifacts should include answer narrative, visualization plan, rendered visual blocks, process summary, evidence strength, limitations, follow-up context, and contract/data snapshot info.
- Data refresh creates a new run or artifact version. Existing artifacts keep their original snapshot/cutoff and remain readable/auditable with an old-snapshot notice.
- Users should be able to reopen an artifact, continue the investigation, share a read-only view, and export to static formats such as PDF or Markdown.
- Static export should not replace the interactive artifact because follow-up context and visual interactions are part of the product value.
- First production baseline can keep collaboration simple, but artifact persistence should be part of the product model.

Artifact sharing and permissions:

- Confirmed choice: artifacts are permission-controlled read-only objects, not public unrestricted links.
- The stored artifact is one complete Answer Package with section-level visibility tags.
- Opening an artifact should check the current user's permissions and apply visibility filtering to answer sections, visual blocks, dimensions, metrics, segments, evidence, and follow-up context.
- Rendering, sharing, and static export apply the same role filter and write audit records with actor, role, artifact id, action, and visible section ids.
- Artifacts should carry sensitivity tags, required permissions, contract/data snapshot info, and evidence refs for permission evaluation.
- If some content is not visible, the UI should show a business-readable message such as `部分细分结果因权限不可见`.
- Static exports such as PDF/Markdown should be generated according to the exporter's current permissions.
- Permission changes after artifact creation should affect future access.
- The first baseline can keep sharing simple, but permission checks must be part of the artifact model.

Runtime transparency and final-answer collapse:

- During execution, the thread should show transparent node-level progress rather than only a spinner.
- The live process should stream business-readable LangGraph node events such as `正在识别问题意图`, `正在验证周期模式`, `正在计算一维相关性`, `正在筛选候选维度`, `正在计算 channel x payment_method 组合`, `正在校验证据强度`, and `正在生成结论`.
- LLM decision nodes should expose short product-facing rationale snippets from structured decision packages, such as why a candidate combination is worth exploring or why a path is degraded.
- The UI should not expose raw hidden chain-of-thought. Visible rationale should come from deliberate structured fields such as `business_reason`, `decision_summary`, `evidence_boundary`, and `wording_limit`.
- The frontend should not receive or render the full LLM response payload for decision nodes.
- LLM decision responses should include one or more explicit frontend-display fields, such as `status_message`, `decision_summary`, `business_reason_snippet`, `next_step_label`, and `evidence_boundary_note`.
- These display fields should be concise, safe to show, and separate from internal prompts, raw provider payloads, token metadata, and hidden reasoning.
- Frontend-display fields should read like natural business narration in a Codex-like investigation thread, not rigid enum labels or mechanical status text.
- Structured decision fields remain available for system use, but user-facing display text should be phrased as a short explanation of what the agent is doing or why it is taking the next step.
- While running, the user should be able to see where LangGraph is in the workflow, which node is active, which nodes completed, which branch was taken, and which paths degraded or stopped.
- Runtime display should be an interleaved event stream that follows the actual LangGraph workflow order.
- Every LangGraph node should emit visible progress events such as `node_started`, `node_progress`, `node_completed`, `node_degraded`, or `node_failed`, with status, label, timing, and result state.
- If a node calls an LLM, the node can stream or append frontend-display fields from the LLM output into the same event stream after the call returns or reaches a display-safe step.
- A typical sequence is: `正在识别问题意图` -> LLM decision display summary -> `正在生成分析图` -> `正在计算一维相关性` -> promotion judge display summary -> `正在计算 channel x payment_method x user_type 组合`.
- Business-stage text is not a separate parallel layer; it is the product-language label or summary attached to workflow events.
- Critical branch decisions, judge decisions, degrade/stop decisions, and graph mutations should appear as chronological events with both node identity and business-readable display text.
- Example display text: `先验证这个高峰是否稳定存在，再看它是由订单数、成功率还是客单价放大的。`
- Example display text: `channel x payment_method 解释了一部分差异，但残差集中在首充用户里，我会继续检查这个组合。`
- After a complete answer is produced and verified, the thread should collapse the runtime process by default and make the first screen the business answer.
- The collapsed process remains expandable for audit, debugging, follow-up questions, and evidence inspection.

Runtime event presentation and internal record:

- Confirmed correction: ordinary users should not see technical ids such as `event_id`, `run_id`, `langgraph_node_id`, evidence ids, or result refs in the running thread.
- User-facing events should be display-first: natural narration, short status, optional progress hint, severity, and expandable business evidence.
- Internal event records can keep technical ids and refs for ordering, joins, debugging, audit, and evidence inspectors.
- The frontend may receive stable ids internally for rendering/state management, but it should not present them as visible content.
- Expanded evidence views should still use business labels first, such as capability name, scope, evidence strength, chart/table, query summary, limitations, and verifier status.
- Raw technical ids can be available only in a developer/debug inspector, not in the default product experience.
- This boundary applies to both regular LangGraph/tool nodes and LLM decision nodes.
- All node types should emit user-facing display fields and internal technical records separately.

User-facing event shape:

```text
narration
short_label
progress_hint
status
severity
expandable_evidence_summary
```

Internal record shape:

```text
event_id
thread_id
run_id
langgraph_node_id
waje_node_id
capability
scope
target_claim
refs
timing
```

Final process collapse:

- Confirmed choice: after verified final answer, collapse the detailed runtime stream into a process summary card.
- The first screen should prioritize the business answer, with the process summary shown as a compact trust/audit entry.
- The summary should include key counts and statuses such as patterns tested, formula paths run, candidate factors screened, promotion loops, supported/local/insufficient findings, degraded paths, and verifier status.
- The full chronological event stream remains expandable from the summary card.
- This avoids keeping all node events visible after completion while preserving auditability.

Completed process expanded view:

- Confirmed choice: when users expand the completed analysis process, show the full chronological business event stream by default.
- The expanded process should keep the Codex-like narrative of what the agent did and why, including key branch decisions, promotion/degrade/stop decisions, and verifier outcome.
- It should not default to technical trace fields such as node ids, run ids, SQL refs, raw payloads, or provider metadata.
- Each business event can expand to business evidence details such as key numbers, evidence strength, limitations, and chart/table summaries.
- Technical trace belongs in a developer or advanced debug view.

Example thread flow:

1. User asks the full-sample month-start payment amount question.
2. Agent states that it recognizes an intra-month periodic pattern analysis.
3. Agent shows a plan: prove pattern, quantify strength, scan exceptions, test candidate explanations.
4. Inline cards show `pattern_scan`, `event_window_scan(payday)`, and `joint_attribution` execution.
5. The final answer appears in the thread with conclusion, key quantification, top candidate explanations, and major gaps.
6. Users can expand any card for evidence and query details, or ask a follow-up to continue the investigation.

Frontend SDK research:

- The exact frontend/agent UI SDK is not decided yet.
- 21st Agent SDK is a candidate raised by the user, and technical-stack evaluation should start now.
- The evaluation should compare 21st with other viable SDK/UI/runtime options for Codex-like investigation threads, streaming tool cards, evidence inspectors, auth/session handling, observability, deployment fit, and LangGraph integration.
- The selected SDK must support WAJE's product shape rather than drive the analysis architecture.

## Technical Stack Decisions

Primary language and service boundary:

- Confirmed choice: TypeScript frontend/gateway plus Python BI Agent Core.
- TypeScript should own Next.js or equivalent frontend, thread UI, SDK integration, auth/session boundary, streaming gateway, and frontend-facing APIs.
- Python should own LangGraph execution, BI capability APIs, semantic compiler, ClickHouse/Postgres analytical access, statistical analysis, evidence reducer, and answer verifier.
- Postgres and ClickHouse remain the shared persistence/query boundary rather than passing BI state through frontend SDK abstractions.

## Semantic Contract Storage

Confirmed choice: versioned files plus Postgres runtime mirror.

- Versioned repo files are the source definitions for semantic contracts, metrics, dimensions, event contracts, capability schemas, and recipe or graph templates.
- Versioned contracts should cover things that affect answer truth or evidence boundaries: metrics/formulas, dimensions/factors, events/static assumptions, capability cards, recipe/subgraph templates, evidence-strength policy, claim wording boundaries, and visualization semantic policies.
- Postgres stores the runtime mirror: active versions, permissions, enablement flags, investigation threads, run states, evidence ledger, verifier results, and audit metadata.
- Every analysis run should pin the exact contract versions and capability versions it used.
- Runtime changes should be traceable back to versioned source definitions.
- Business-managed extensions can be added later, but production execution still needs versioning, validation, and audit records.

Example source definitions:

- `contracts/metrics/payment_amount.yaml`
- `contracts/dimensions/payment_method.yaml`
- `contracts/events/payday.yaml`
- `capabilities/pattern_scan.yaml`
- `graph_templates/intra_month_pattern.yaml`

Example Postgres runtime records:

- `contract_versions`
- `active_contracts`
- `capability_registry`
- `investigation_threads`
- `run_states`
- `evidence_ledger`
- `verifier_results`
- `permissions`

## Storage And Query Boundary

Confirmed choice: ClickHouse handles facts and analytical queries; Postgres handles semantic contracts and product state.

- ClickHouse stores fact tables, pre-aggregations, wide or pre-joined analytical tables, expanded event windows, and large group-by/window scan/query result tables.
- PostgreSQL stores semantic contracts, active contract versions, permissions, enablement flags, investigation threads, run states, evidence ledger, verifier results, and audit metadata.
- Every analysis run should link ClickHouse query ids or result references to Postgres evidence ledger and run ids.
- Analysis capabilities compile to validated queries against ClickHouse when they need fact scans, aggregation, window scanning, or multi-dimensional attribution.
- Postgres remains the source for orchestration state, access control, semantic registry, and auditable evidence references.

Runtime state and evidence persistence:

- Confirmed choice: LangGraph owns runtime checkpoint/resume/trace; Postgres owns WAJE product state and evidence ledger.
- LangGraph should manage graph state, checkpoints, branch/loop state, resume, runtime trace, and node execution progress.
- Postgres should store investigation threads, analysis runs, graph node records, capability calls, evidence ledger entries, result refs, judge decisions, verifier results, answer packages, missing contracts, permissions, and audit metadata.
- LangGraph node ids and WAJE run/node ids should be linked so product views can join runtime progress with evidence and answer records.
- Each important node execution should write WAJE-owned audit artifacts into Postgres.
- Postgres remains the product-facing source for evidence, answer packages, missing contracts, permissions, and audit queries.
- This keeps runtime replaceable while preserving WAJE's evidence assets.

Example ClickHouse analytical assets:

- `payment_orders`
- `user_activity_daily`
- `payment_daily_mv`
- `payment_joint_features`
- `event_windows_expanded`
- `attribution_query_results`

Example Postgres product records:

- `semantic_contracts`
- `active_contract_versions`
- `investigation_threads`
- `analysis_runs`
- `evidence_ledger`
- `verifier_results`
- `permissions`

## Orchestration Runtime Boundary

Confirmed choice: define framework-neutral orchestration contracts first, then use LangGraph or another runtime through adapters.

- WAJE BI owns the core contracts: `analysis_graph`, `run_state`, `capability_call`, `evidence_event`, `verifier_result`, disabled/degraded paths, and final `Answer Package`.
- Confirmed runtime boundary: LangGraph carries visible workflow execution, checkpointing, loops, retries, trace, and node progress; WAJE-owned components carry BI semantics, validation, SQL compilation, evidence strength, permission enforcement, answer verification, and accepted graph state.
- LangGraph can be used as an execution adapter for cyclic planning, checkpointing, streaming, human review, and multi-step graph execution.
- The runtime framework must not become the source of truth for business intent, semantic contracts, evidence strength, permissions, or answer verification.
- If the runtime changes later, persisted runs and evidence should still be readable through WAJE-owned contracts.
- Framework-specific state should stay behind adapter boundaries.

Component split:

- `LLM Reasoner`: understands the user's business question, proposes intent, hypotheses, analysis graph changes, candidate dimensions, promotion options, and final narrative wording.
- `Local Orchestration Controller`: deterministic WAJE service that owns run state, accepted graph state, transition rules, budgets, retries, disabled/degraded paths, and which next node is allowed to run.
- `Local Policy Engine`: deterministic or configured WAJE rules that decide whether an LLM proposal may be accepted, rejected, downgraded, or sent back for repair.
- `Local Validators`: deterministic hard gates for schema validity, permissions, metric compatibility, sample size, sparse-cell limits, runtime budget, SQL safety, and evidence output completeness.
- `Capability APIs`: local tool/service layer that compiles semantic requests into validated SQL, executes against ClickHouse/Postgres, and returns structured evidence.
- `Evidence Reducer`: local analytical summarizer that turns query results into residuals, fit, stability, segment concentration, exception lists, and evidence strength candidates.
- `Answer Verifier`: local verifier that checks final natural-language claims against evidence, scope, metric definitions, missing contracts, allowed claim/evidence type, and wording limit.
- `LangGraph Adapter`: runtime adapter that invokes nodes, persists checkpoints, resumes runs, routes loop iterations, streams progress, and handles interrupts/retries.

Authority split:

- LLM can propose graph mutations and explain why.
- Local Orchestration Controller is the only component that can accept graph mutations into run state.
- Local Validators are the only components that can approve SQL execution and hard capability gates.
- Capability APIs are the only components that can run analytical queries.
- Evidence Reducer and Answer Verifier determine what evidence can support a claim.
- LangGraph executes the accepted route; it does not decide business validity.

Graph changes happen through WAJE-owned commands such as `add_node`, `disable_node`, `degrade_node`, `promote_dimension_order`, `stop_with_insufficient_evidence`, and `request_missing_contract`.

LangGraph visibility boundary:

- Every step that needs visualization, checkpointing, replay, debugging, retry, or human review should be represented as a LangGraph node or subgraph.
- Component calls such as LLM reasoning, local policy decisions, validation, capability execution, evidence reduction, graph mutation, synthesis, and verification should all appear in the LangGraph execution trace.
- A LangGraph node can wrap a local component, but the node's business authority is still limited by that component's contract.
- Dynamic loops, including promotion to higher-order attribution, should be modeled in LangGraph as explicit subgraphs with conditional edges.
- The conditional edge can be implemented by a local routing function that reads WAJE run state and returns `continue`, `stop`, `degrade`, `repair`, or `request_missing_contract`.
- LangGraph Studio/LangSmith should be used to inspect graph structure, node execution, thread state, checkpoints, and time-travel debugging where available.

Dify Exclusion And LangGraph Workflow Principle:

- Dify should not be introduced into the WAJE BI v2 project as a dependency, runtime, workflow engine, or product module.
- LangGraph should carry the workflow orchestration and visualization role for WAJE BI v2.
- Confirmed assessment: this is the right direction as long as LangGraph owns visible execution flow and WAJE-owned components own BI-specific decisions.
- WAJE's workflow nodes are code-defined and contract-defined rather than low-code generic blocks.
- The graph should be inspectable as a product artifact: users and developers should be able to see what the Agent planned, which nodes ran, which branch was taken, where it looped, why it stopped, and which evidence each node produced.
- Loop logic such as升维 should be visible in the LangGraph graph, including conditional edges and routing outcomes.
- Business authority remains inside WAJE contracts and local components: the graph can show `promotion_policy_gate`, but the actual acceptance rule comes from WAJE policy, validators, evidence state, and budget constraints.
- WAJE should avoid a generic low-code workflow builder in the first production baseline. The first target is a visible, debuggable investigation workflow for BI analysis.

## LangGraph Graph Shape

Confirmed choice: fixed main graph plus pluggable analysis subgraphs.

- The main graph owns the investigation lifecycle: intent reasoning, planning, graph acceptance, analysis subgraph execution, synthesis, verification, repair, and finalization.
- Analysis-specific logic lives in subgraphs, such as attribution, intra-month pattern, revenue health, anomaly review, formula decomposition, event evidence, and data quality.
- Subgraphs should be visible in LangGraph, including loops, branches, degraded paths, and stop reasons.
- The main graph should stay stable enough for debugging and product UX; analysis subgraphs can evolve independently.
- LLM-generated plans select and parameterize subgraphs, but local policy and validators decide what graph state is accepted.

Working definition of recipe:

- A recipe is a reusable analysis template that describes a likely subgraph shape, candidate capabilities, default checks, common evidence requirements, and stop/repair rules for one analysis family.
- A recipe is not a fixed report and not a hard execution script.
- Recipes can seed a subgraph, but the LLM can propose additions, removals, promotion, or degraded paths through WAJE-owned graph mutation commands.
- Local policy and validators decide which recipe-seeded or LLM-proposed graph changes are accepted.
- Confirmed recipe selection shape: use a recipe registry plus LLM-proposed runtime recipe variants. The LLM selects a starting recipe and may propose a runtime variant, while local policy and validators decide whether the accepted runtime graph can diverge from the registered template.

Example:

- `intra_month_pattern` recipe seeds: prove pattern -> quantify strength/exceptions -> scan payday/weekday/holiday/activity/channel/user/payment/device/geo candidates -> synthesize evidence.
- If the evidence reducer finds residual concentration in `channel x payment_method`, the LLM can propose adding a higher-order attribution loop.
- The accepted runtime graph remains visible in LangGraph, even when it diverges from the starting recipe.

## Joint Attribution Candidate Search

Confirmed principle: broad local candidate screening should drive dimension selection for joint attribution and promotion.

- LLM should not be the only source deciding which dimension to add in a joint attribution loop.
- LLM decides the business need for deeper exploration, proposes hypotheses, and can add business-reasonable candidates to the pool.
- Local candidate screening should quickly evaluate all eligible `registered` and `discoverable` candidates, plus validated `hypothesized` candidates where data exists.
- Local scoring should rank candidates by incremental explanatory power, residual reduction, stability, coverage, sample size, sparse-cell risk, metric compatibility, and business/evidence type.
- The next dimension or combination should be selected from ranked local results, with LLM review for business plausibility and final narrative.
- Higher-order promotion should compare whether adding a dimension materially improves fit after penalizing sparsity and instability.
- If no candidate materially improves the explanation, the graph should stop or report insufficient evidence rather than keep expanding.
- Confirmed scoring shape: joint attribution candidates should use multi-objective scoring rather than a single correlation or contribution metric.
- The scoring contract should include at least incremental explanatory power, residual reduction, amount contribution, stability across periods, coverage, sample size, sparse-cell risk, metric compatibility, business plausibility, and evidence type/strength.
- The system may compute an overall ranking score, but the ranking must expose component scores so the final business answer and verifier can distinguish broad stable explanations from narrow exceptions or unstable fits.
- High-fit but low-coverage candidates should be kept as segment or exception explanations rather than promoted into broad global causes.
- Confirmed promotion decision model: use an LLM qualitative promotion judge with local hard gates.
- Local screening computes candidate evidence summaries, rankings, residual summaries, sparse/coverage warnings, tested dimensions, evidence strength, and budget state.
- The LLM promotion judge decides whether to `promote_to_higher_order`, `continue_same_order`, `stop_sufficient`, `stop_insufficient`, `degrade_to_exception`, or `request_missing_contract`, and must explain the business reason.
- Local hard gates still enforce contracts, permissions, SQL safety, sample-size red lines, sparse-cell red lines, budget, and evidence output completeness.
- Candidate-class evaluation protocols can inform the prompt, but they should not become rigid local quantitative rules for every dimension type.
- Promotion judgment quality should be improved through prompt iteration and regression tests.

Promotion judge input/output:

- Confirmed shape: full judge context input plus structured decision package output.
- Input should include user question, target claim, scope, current graph state, tested dimensions, candidate ranking summary, residual summary, evidence strength summary, coverage/sparse warnings, event context, business context, budget remaining, and previous judge decisions.
- Output should include decision, selected candidates, next capability node or graph mutation, business reason, evidence boundary, wording limit, risks, and required follow-up evidence.
- Allowed decisions include `promote_to_higher_order`, `continue_same_order`, `stop_sufficient`, `stop_insufficient`, `degrade_to_exception`, and `request_missing_contract`.
- The decision package should be recorded in run state and shown in LangGraph/debug views.
- The graph compiler validates the decision package before applying graph mutation.

Candidate pool builder:

- Confirmed choice: local candidate pool builder filters and ranks candidates before `joint_attribution` runs.
- Inputs should include target metric, target claim, scope, time range, recipe origin, event context, existing evidence, and capability ledger.
- The builder should pull eligible `registered` candidates from the SSOT capability ledger, filter by supported grain, allowed claim type, data status, contract availability, permissions, and metric compatibility.
- The builder should add `discoverable` candidates from profiling, anomaly scans, enum changes, distribution drift, and structural shifts.
- The builder should add relevant event candidates such as holidays, payday windows, activities, product releases, known incidents, and external event records when their supported grain matches the claim.
- LLM/user hypothesized candidates can enter the pool, but they must be marked by status and evidence state.
- Candidate statuses should include `eligible`, `weak`, `missing`, and `unsupported`.
- `eligible` candidates enter normal local screening; `weak` candidates can be down-weighted or reported separately; `missing` and `unsupported` candidates cannot support strong evidence paths.
- The candidate pool builder should prefer broad coverage and business relevance before high-order combination search, then let local scoring handle ranking.

Example:

- Current path: `month_phase` explains that month-start is higher, but residuals remain.
- Local screening tests eligible candidates such as `payday_window`, `weekday`, `holiday`, `activity`, `channel`, `payment_method`, `user_type`, `geo`, `device`, and known event windows.
- Results show `payment_method` has weak standalone fit, `channel` has medium standalone fit, and `channel x payment_method x user_type` has strong residual reduction with acceptable sample size.
- The graph promotes into that higher-order segment and reports the specific segment-level explanation, instead of claiming a broad channel effect.
- If `activity_window` has high fit in only a few months, the answer treats it as an exception-month explanation, not the full-period explanation.

升维 workflow example:

1. `LLM Reasoner` classifies the question and proposes a lower-order attribution graph.
2. `Local Orchestration Controller` validates the proposed graph shape and accepts an initial graph into run state.
3. `LangGraph Adapter` invokes the accepted node route and checkpoints progress.
4. `Capability APIs` compile semantic requests into validated SQL and execute the accepted analytical queries.
5. `Evidence Reducer` summarizes residuals, fit, stability, sparse segments, and current explanation gaps.
6. `LLM Reasoner` proposes whether to test a higher-order combination and returns a structured graph mutation command with business reasoning.
7. `Local Policy Engine` and `Local Validators` accept, reject, degrade, or repair that command based on hard gates and configured promotion policy.
8. `Local Orchestration Controller` writes the accepted next graph state.
9. `LangGraph Adapter` executes the accepted next route.
10. The loop stops when local stopping criteria are met: sufficient explanation, exhausted useful candidates, insufficient data, budget limit, or required missing contract.

Visible LangGraph shape for升维:

```text
intent_reasoning
  -> initial_graph_proposal
  -> graph_acceptance_gate
  -> attribution_loop
       -> candidate_query_execution
       -> evidence_reduction
       -> promotion_proposal
       -> promotion_policy_gate
       -> graph_state_mutation
       -> route_next: continue | stop | degrade | repair | request_missing_contract
  -> answer_synthesis
  -> answer_verification
  -> final_or_repair
```

Example command:

```json
{
  "command": "promote_dimension_order",
  "from_dimensions": ["channel", "payment_method"],
  "add_dimension": "user_type",
  "reason": "Residuals concentrate in first-payment users after channel/payment_method split.",
  "capability": "joint_attribution",
  "required_checks": ["sample_size", "sparse_cells", "metric_compatibility", "runtime_budget"]
}
```

Discussion note on 21st Agent SDK:

- 21st Agent SDK can be considered as an outer infrastructure layer for deployment, sandboxing, auth/token exchange, chat UI, streaming, observability, and API integration.
- If used together, the likely boundary is: 21st hosts and exposes the agent session; WAJE orchestrator owns the investigation graph; LangGraph optionally runs the internal graph; WAJE capability APIs own SQL compilation, validation, query execution, evidence, and verifier checks.
- 21st tools should call WAJE-owned capability endpoints or local service APIs, not raw SQL execution.
- WAJE run ids, evidence ids, contract versions, and verifier results should remain in WAJE/Postgres even when 21st provides the surrounding runtime infrastructure.

## Guardrail Case

The question `全量样本看，为什么从 2024 年 1 月开始到 2026 年 5 月结束，为什么每个月月初的付费金额都比月中月末高一些` must be classified as full-sample intra-month periodic pattern analysis.

Expected path:

1. Prove whether the pattern exists.
2. Quantify strength and exceptions.
3. Examine payday, weekday mix, holidays, activities, channel structure, new/returning users, payment success rate, device/region, anomalous months, and black-swan events through generic capability APIs.
4. Synthesize with evidence level, missing contracts, and reproducible path.
