# Gate 3 Episode Team Confirmation Log

Status: eight launch Episode business decisions and the claim-scoped mixed
case-file model are confirmed. The v4 structural repair is implemented;
physical case-file materialization and independent admission review have not
started.

This log records product-design decisions confirmed by the WAJEgame team during
the Episode-by-Episode working session. It does not replace the later
independent business-owner and measurement-reviewer approvals required by Gate
3 admission.

## G3-USER-001

Confirmed:

- The primary comparison for "yesterday paid amount changed" is the previous
  complete Africa/Lagos business day.
- The trailing seven comparable business-day average and prior-week same day
  are sensitivity baselines. Their directions are reported separately and do
  not enter the primary contribution bridge.
- A valid answer may choose any defensible decomposition, provided the
  contribution bridge reconciles to the paid-amount change and does not double
  count payer scale, frequency, amount per successful payment, first-payer
  composition, payment success rate, or channel mix.
- Reviewer scoring evaluates measurement quality, evidence, boundary handling,
  and reconciliation. It does not require one predetermined leading factor.

Repository fact corrected during the review:

- WAJEgame launch Episodes use the Africa/Lagos 00:00-24:00 business day. The
  previous 04:00 cutoff was an authoring residue.

Validation after the change:

- Gate 3 corpus generation: passed.
- Gate 3 catalog, view, and readiness structural checks: passed.
- Bootstrap tests: 150 passed, 8 skipped.

## G3-USER-002

Confirmed:

- The Agent chooses a defensible history length from the candidate periodicity
  and available complete cycles. The Episode does not impose a fixed number of
  weeks or months.
- A fixed pattern must repeat across complete cycles after activity, holiday,
  coverage, and composition effects are addressed.
- The direction and material magnitude must continue in a time holdout that was
  not used to discover the pattern.
- Insufficient evidence permits only candidate-pattern wording.
- Weekend, month-start, and evening are examples from the user. They do not
  restrict the Agent from testing other defensible periodicities.
- Gameplay activity may be reported alongside paid-amount patterns. Calling it
  a driver requires governed payment-to-gameplay linkage.

Repository facts corrected during the review:

- Current WAJEgame scope uses one Africa/Lagos business timezone across
  operating regions.
- Hourly paid-amount semantics, payment-to-gameplay attribution, and the
  governed internal-activity timeline are explicit missing contracts and
  therefore typed boundaries.

## G3-USER-003

Confirmed:

- Activity, budget, creative, version, payment outage, holiday, and external
  events are scored as separate estimands with separate evidence ceilings.
- Stable randomized rollout or another credible control may support a causal
  claim only for the population actually covered by that assignment.
- Payment outage logs can directly establish affected attempts. Lost paid
  amount additionally requires a counterfactual success rate and is reported
  as a range when that rate is uncertain.
- Fully overlapping activity and budget changes cannot receive separate causal
  contributions.
- External events remain contextual or associational unless a credible
  identification design exists.
- The final answer may contain claims of different strengths and must state the
  evidence level for each event.

## G3-USER-004

Confirmed:

- Revenue health is evaluated as a multi-axis profile. The Episode does not
  impose one universal health score or fixed threshold.
- The Agent declares its observation period and decision horizon, then
  evaluates sustainable payer-base growth, payer concentration, business
  segment concentration, activity dependency, revenue quality, and stability.
- The final answer may include an overall health judgment and the highest
  material risk when it explains the component evidence, comparison bases,
  coverage, and synthesis.
- Short-term and long-term risk priorities are reported separately when their
  rankings differ.
- Reviewer scoring evaluates measurement design, evidence coverage, component
  coherence, and whether the risk priority follows from the declared horizon.
  It does not require a predetermined score, threshold, or risk winner.

Repository facts corrected during the review:

- Current ClickHouse data supports successful-payment history, within-source
  payer cohorts, large-payer concentration, and channel structure.
- Refund/reversal maturity, activity exposure, and cross-account canonical
  payer identity remain missing contracts. Their affected health components
  must degrade locally and cannot be treated as clean or zero-risk.

## G3-USER-005

Confirmed:

- The Episode produces two layers: business slices that locate where paid
  amount changed, and behavioral mechanisms that explain how the total changed.
- Each supported dimension receives its own contribution bridge and leading
  business unit. Results from channel, geography, device, payment method, and
  other overlapping dimensions are not added together.
- The largest observed slice across dimensions may be reported when its
  overlap boundary is explicit. It cannot be presented as independent from
  other dimension leaders.
- The top three behavioral factors come from a reconciled bridge such as payer
  scale, payment frequency, amount per successful payment, final-status
  success rate, and payer composition. Interaction and residual remain visible.
- Reviewer scoring evaluates definition quality, reconciliation, overlap
  handling, coverage, and evidence. It does not require predetermined dimension
  winners or factor winners.

Repository facts corrected during the review:

- Current payment data supports channel, country/region/city, device
  brand/model, operating system, network type, and payment method, with
  dimension-specific missingness.
- `分包渠道` is the accepted channel field and does not create a separate package
  dimension.
- Gameplay sources do not provide a full-period payment-to-gameplay
  attribution contract. Gameplay contribution must degrade locally.

## G3-USER-006

Confirmed:

- The Episode makes two independent judgments: whether a material process
  anomaly occurred within the day, and what residual business impact remained
  at the end of the complete day.
- A recovered two-hour payment incident still counts as a process anomaly.
  Recovery and compensating payments are reported separately from the original
  disruption and the full-day residual.
- The Agent chooses a defensible history, scan resolution, conditional
  baseline, threshold, and multiple-scan false-positive control, then records
  them in the accepted Frame.
- Locating an anomaly in a payment channel, geography, device group, gameplay
  slice, or large-payer cohort does not by itself establish the root cause.
  Root-cause language requires mechanism or event evidence.
- Reviewer scoring evaluates baseline quality, false-positive control,
  resolution handling, source localization, recovery measurement, and evidence
  strength. It does not require one fixed anomaly method or threshold.

Repository facts corrected during the review:

- Current data supports complete-day successful-payment and final-status
  analysis.
- Auditable local-hour semantics, full-period payment-to-gameplay attribution,
  and payment-incident timelines remain missing contracts. Affected process
  and mechanism claims must degrade locally.

## G3-USER-007

Confirmed:

- The prior complete business day, the mean of the seven complete business
  days before the target, and the prior-week same weekday are three independent
  comparisons. The target day is excluded from its rolling mean.
- All three requested comparisons remain visible. They are not averaged,
  voted, or selectively dropped when their directions conflict.
- A fourth estimand evaluates deviation from a conditional normal range using
  qualified history, weekday, visible activity, business structure, coverage,
  and release maturity.
- Driver explanations are formed separately for each requested contrast and
  reconcile to that contrast. One cross-baseline factor story cannot replace
  the three bridges.
- Reviewer scoring evaluates window identity, maturity and exposure alignment,
  normal-range design, reconciliation, and conflict explanation. It does not
  require the three comparison directions to agree.

Repository fact corrected during the review:

- Current payment, final-status, market, and channel data support the three raw
  comparisons and major driver metrics. A governed internal activity timeline
  remains missing, so activity-adjusted normal ranges and cleaned rolling
  sensitivities must degrade locally.

## G3-USER-008

Confirmed:

- Evidence sufficiency is decided per claim. Valid dispositions are settled,
  provisional with a scoped gap, revoked with recomputation, and unverifiable
  because original provenance is missing.
- A local quality defect affects only claims whose metric, scope, grain,
  evidence, or applicability boundary depends on it.
- Recoverable defects re-enter the investigation loop. Recomputed results
  create new immutable EvidenceRecords and a new AnswerVersion; old records
  retain their audit history through explicit supersedes or invalidates links.
- A revoked claim cannot remain settled in the current Answer or appear as
  completed in the current Workflow projection.
- Missing provenance is reported honestly and cannot be recreated from a
  current aggregate result.
- Recalculation under an accepted deduplication policy stays within the current
  measurement design. Changing the observation unit, inclusion policy, or
  business deduplication definition creates a new AnalysisFrameRevision.
- Reviewer scoring evaluates claim binding, release maturity, payment status,
  deduplication, channel coverage, high-value-payer sensitivity, correction
  impact, and the proposed recovery path. It does not produce one global
  evidence score.

Repository facts confirmed during the review:

- Current ClickHouse sources support gross successful-payment maturity,
  final-status completeness, accepted order deduplication, transaction-carried
  channel coverage, and aggregate high-value-payer sensitivity.
- This Episode audits an existing gross paid-amount conclusion. Net-revenue
  refund/reversal questions remain a separate measurement contract.

## Post-confirmation case-file decision

Confirmed:

- source provenance and claim support are separate axes;
- one Episode may combine a frozen real snapshot, controlled synthetic fixture
  and known contract gap;
- every claim receives its own support state, disposition, applicability,
  evidence ceiling, observation requirements and reversal conditions;
- a local missing contract cannot cancel unrelated supported claims;
- the evaluator may hold hidden truth, while every fact required from the
  Agent must be reachable through an explicit inspection surface;
- Reviewer scoring remains method-neutral and judges the proposed measurement
  design, evidence use and claim boundary;
- deterministic validation owns identity, clock, source authority, scope,
  observation reachability, replay digest and fail-closed admission;
- counterfactual replay verification does not grant semantic approval;
- Gate 3 may create provisional answer observations and must not authorize
  settled or delivered publication.
