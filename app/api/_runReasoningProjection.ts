import type {
  TraceCapabilityFailure,
  TraceCapabilityOutcomeStatus,
  TraceReasoning,
  TraceReasoningAnswerBlock,
  TraceReasoningClaim,
  TraceReasoningFact,
  TraceReasoningIssue,
  TraceReasoningQuery,
  TraceReasoningTask,
} from "../run-reasoning-contracts";
import type { PersistedRunReasoning } from "./_conversationStore";

type JsonRecord = Record<string, unknown>;

type ProjectedQuestionAnswer = {
  issueId: string;
  text: string;
  status: TraceReasoningIssue["status"];
  claimRefs: string[];
  limitationRefs: string[];
};

const TASK_STATUSES = new Set<TraceCapabilityOutcomeStatus>([
  "succeeded",
  "unavailable",
  "integrity_failed",
  "technical_failed",
  "skipped",
  "superseded",
]);

const PREFERRED_FACT_NAMES = [
  "business_readout",
  "direction_consistency_ratio",
  "comparable_periods",
  "median_uplift",
  "target_value",
  "baseline_value",
  "best_correlation",
  "best_sample_size",
  "share_delta",
  "paid_user_count_contribution_share",
  "amount_per_paid_user_contribution_share",
  "frequency_contribution_share",
  "amount_per_payment_contribution_share",
  "point_count",
  "event_occurrence_count",
  "covered_dataset_count",
  "country_scope_violation_count",
];

export function traceReasoningFromPersistedState(
  input: PersistedRunReasoning,
): TraceReasoning {
  const planTasks = recordArray(input.plan.capability_tasks);
  const obligations = recordArray(input.plan.claim_obligations);
  const proposalIssues = recordArray(input.plannerProposal.issue_tree);
  const proposedClaims = recordArray(
    record(input.claimSettlement.checkpoint)?.proposed_claims,
  );
  const acceptedClaims = recordArray(input.claimSettlement.accepted_claims);
  const acceptedClaimKeys = recordArray(input.claimSettlement.accepted_claim_keys);
  const coverageRows = recordArray(input.claimSettlement.obligation_coverage);
  const verifier = record(input.claimSettlement.verifier_report);
  const proposedToVerified = stringRecord(verifier?.proposed_to_verified);
  const verificationDecisions = recordArray(verifier?.verification_decisions);
  const answerBlocks = projectAnswerBlocks(input.customerPublication);
  const questionAnswers = projectQuestionAnswers(
    input.narrativeDocument,
    input.materialProjection,
  );
  const answerClaimRefs = new Set(
    answerBlocks.flatMap((block) => block.claimRefs),
  );

  const issueIdByObligation = mapIssueIdsByObligation(
    proposalIssues,
    obligations,
  );
  const outcomeByTask = new Map(
    input.taskOutcomes.map((outcome) => [
      string(outcome.task_id),
      outcome,
    ]),
  );
  const evidenceByEntryRef = new Map(
    input.evidenceEntries.map((entry) => [
      string(entry.entry_ref),
      entry,
    ]),
  );
  const queryByResultRef = new Map(
    input.queryRuns.map((query) => [
      string(query.result_ref),
      query,
    ]),
  );
  const claimKindByKey = new Map(
    acceptedClaimKeys.map((claimKey) => [
      string(claimKey.claim_key),
      string(claimKey.claim_kind),
    ]),
  );
  const coverageByClaimRef = new Map<string, string[]>();
  for (const coverage of coverageRows) {
    const obligationId = string(coverage.obligation_id);
    for (const claimRef of strings(coverage.claim_refs)) {
      const current = coverageByClaimRef.get(claimRef) ?? [];
      current.push(obligationId);
      coverageByClaimRef.set(claimRef, current);
    }
  }
  const materialClaimByRef = new Map(
    recordArray(input.materialProjection.claims).map((claim) => [
      string(claim.claim_ref),
      claim,
    ]),
  );
  const materialByHandle = new Map(
    recordArray(input.materialProjection.evidence_materials).map((material) => [
      string(material.material_handle),
      material,
    ]),
  );
  const acceptedByClaimKey = new Map(
    acceptedClaims.map((claim) => [string(claim.claim_key), claim]),
  );
  const decisionByProposedRef = new Map(
    verificationDecisions.map((decision) => [
      string(decision.subject_ref),
      decision,
    ]),
  );
  const supportEdgesByClaimKey = groupBy(
    input.supportEdges,
    (edge) => string(edge.target_claim_key),
  );

  const claims = proposedClaims.map((proposed) => {
    const proposedClaimRef = string(proposed.claim_ref);
    const claimKey = string(proposed.claim_key);
    const accepted = acceptedByClaimKey.get(claimKey);
    const verifiedClaimRef = proposedToVerified[proposedClaimRef]
      || string(accepted?.claim_ref)
      || proposedClaimRef;
    const decision = decisionByProposedRef.get(proposedClaimRef);
    const verificationStatus = string(decision?.disposition) === "accepted"
      ? "accepted"
      : string(decision?.disposition) === "vetoed"
        ? "vetoed"
        : "unsettled";
    const claimKind = claimKindByKey.get(claimKey)
      || string(record(accepted?.factual_payload)?.claim_kind)
      || string(record(proposed.factual_payload)?.claim_kind)
      || string(accepted?.claim_class)
      || string(proposed.claim_class)
      || "未登记";
    const obligationIds = unique([
      string(record(accepted?.factual_payload)?.obligation_id),
      string(record(proposed.factual_payload)?.obligation_id),
      ...(coverageByClaimRef.get(verifiedClaimRef) ?? []),
    ].filter(Boolean));
    const issueIds = unique(
      obligationIds.flatMap((obligationId) =>
        issueIdByObligation.get(obligationId) ?? []
      ),
    );
    const supportEdges = supportEdgesByClaimKey.get(claimKey) ?? [];
    const evidenceEntries = supportEdges
      .filter((edge) => string(edge.source_type) === "evidence")
      .flatMap((edge) => {
        const evidence = evidenceByEntryRef.get(string(edge.source_ref));
        return evidence ? [evidence] : [];
      });
    const evidenceRefs = unique(
      evidenceEntries.map((entry) => string(entry.evidence_ref)).filter(Boolean),
    );
    const taskIds = unique(
      evidenceEntries.map((entry) => string(entry.task_id)).filter(Boolean),
    );
    const materialClaim = materialClaimByRef.get(verifiedClaimRef);
    const facts = projectClaimFacts(materialClaim, materialByHandle);
    const answerBlockIds = answerBlocks
      .filter((block) => block.claimRefs.includes(verifiedClaimRef))
      .map((block) => block.blockId);
    const factualPayload = record(materialClaim?.verified_claim_payload)
      ?? record(accepted?.factual_payload ?? proposed.factual_payload);
    const limitationRefs = unique([
      ...strings(proposed.limitation_refs),
      ...strings(accepted?.limitation_refs),
      ...strings(decision?.limitation_refs),
    ]);
    return {
      proposedClaimRef,
      claimRef: verifiedClaimRef,
      claimKind,
      claimClass: string(accepted?.claim_class)
        || string(proposed.claim_class)
        || "未登记",
      source: string(accepted?.claim_class ?? proposed.claim_class)
          === "candidate_mechanism"
        ? "llm_proposed"
        : "runtime_derived",
      verificationStatus,
      ...(string(decision?.reason_code)
        ? { reasonCode: string(decision?.reason_code) }
        : {}),
      summary: excerpt(
        businessText(claimSummary(claimKind, factualPayload, facts)),
        260,
      ),
      taskIds,
      evidenceRefs,
      issueIds,
      facts,
      usedInAnswer: answerClaimRefs.has(verifiedClaimRef),
      answerBlockIds,
      limitationRefs,
    } satisfies TraceReasoningClaim;
  });

  const tasks = planTasks
    .map((task) => projectTask({
      task,
      outcome: outcomeByTask.get(string(task.task_id)),
      evidenceEntries: input.evidenceEntries.filter(
        (entry) => string(entry.task_id) === string(task.task_id),
      ),
      claims,
      issueIdByObligation,
      queryByResultRef,
    }))
    .sort((left, right) => left.rank - right.rank);

  const issues = proposalIssues.map((issue) => projectIssue({
    issue,
    obligations,
    tasks,
    claims,
    coverageRows,
    questionAnswers,
    issueIdByObligation,
  }));

  const taskCompleted = tasks.filter((task) =>
    ["succeeded", "unavailable", "integrity_failed", "technical_failed", "skipped"]
      .includes(task.status)
  ).length;
  const queryRefs = unique(tasks.flatMap((task) => task.resultRefs));
  const repairNotices = unique(
    (input.runNodes ?? []).flatMap((node) =>
      strings(record(node.payload)?.repair_notices)
    ),
  );
  return {
    runId: input.runId,
    businessUnderstanding: string(input.request.business_understanding)
      || string(input.request.question)
      || string(input.request.user_message),
    planRevisionId: input.planRevisionId,
    repairNotices,
    issues,
    tasks,
    claims,
    answerBlocks,
    counts: {
      taskTotal: tasks.length,
      taskCompleted,
      queryTotal: queryRefs.length,
      evidenceTotal: input.evidenceEntries.length,
      claimTotal: claims.length,
      claimUsedInAnswer: claims.filter((claim) => claim.usedInAnswer).length,
    },
  };
}

function projectTask({
  task,
  outcome,
  evidenceEntries,
  claims,
  issueIdByObligation,
  queryByResultRef,
}: {
  task: JsonRecord;
  outcome?: JsonRecord;
  evidenceEntries: JsonRecord[];
  claims: TraceReasoningClaim[];
  issueIdByObligation: Map<string, string[]>;
  queryByResultRef: Map<string, JsonRecord>;
}): TraceReasoningTask {
  const taskId = string(task.task_id);
  const rawStatus = string(outcome?.status);
  const status = TASK_STATUSES.has(rawStatus as TraceCapabilityOutcomeStatus)
    ? rawStatus as TraceCapabilityOutcomeStatus
    : outcome
      ? "unsettled"
      : "not_started";
  const resultRefs = unique(
    evidenceEntries.flatMap((entry) =>
      strings(record(entry.payload)?.result_refs)
    ),
  );
  const queries = resultRefs.map((resultRef, index) =>
    projectQuery(queryByResultRef.get(resultRef), resultRef, index)
  );
  const queryStatuses = queries.map((query) => query.status);
  const queryStatus = resultRefs.length === 0
    ? "not_run"
    : queryStatuses.every((value) => value === "completed")
      ? "completed"
      : queryStatuses.some((value) => value === "completed" || value === "limited")
        ? "partial"
        : "failed";
  const evidenceRefs = unique(
    evidenceEntries.map((entry) => string(entry.evidence_ref)).filter(Boolean),
  );
  const taskClaims = claims.filter((claim) => claim.taskIds.includes(taskId));
  const obligationIds = strings(task.supports_obligation_ids);
  const issueIds = unique(
    obligationIds.flatMap((obligationId) =>
      issueIdByObligation.get(obligationId) ?? []
    ),
  );
  const outcomePayload = record(outcome?.payload);
  const rawFailure = record(outcome?.failure);
  const businessReadout = firstBusinessReadout([
    outcomePayload,
    ...evidenceEntries.map((entry) => record(entry.payload)),
  ]);
  return {
    taskId,
    rank: integer(task.execution_rank) ?? 0,
    taskKey: string(task.task_key),
    capabilityId: string(task.capability_id),
    businessLabel: capabilityBusinessLabel(string(task.capability_id)),
    ...(businessReadout ? { businessReadout } : {}),
    status,
    queryStatus,
    queryCount: resultRefs.length,
    queries,
    resultRefs,
    evidenceRefs,
    claimRefs: taskClaims.map((claim) => claim.claimRef),
    issueIds,
    dependencyTaskIds: strings(task.dependency_task_ids),
    limitationRefs: unique([
      ...strings(outcomePayload?.limitation_refs),
      ...evidenceEntries.flatMap((entry) =>
        strings(record(entry.payload)?.limitation_refs)
      ),
    ]),
    ...(rawFailure ? { failure: projectFailure(rawFailure) } : {}),
  };
}

function projectQuery(
  query: JsonRecord | undefined,
  resultRef: string,
  index: number,
): TraceReasoningQuery {
  const contract = record(query?.query_contract);
  const queryIntent = string(contract?.query_intent);
  const executionStatus = string(query?.execution_status);
  const completeness = string(query?.completeness_status);
  const status = queryUiStatus(
    executionStatus,
    completeness,
    string(query?.analysis_readiness),
  );
  const rowCount = integer(query?.row_count);
  const completedAt = isoString(query?.created_at);
  return {
    resultRef,
    queryContractRef: string(query?.query_contract_id),
    label: queryBusinessLabel(queryIntent, index),
    status,
    ...(rowCount !== undefined ? { rowCount } : {}),
    ...(completedAt ? { completedAt } : {}),
  };
}

function projectIssue({
  issue,
  obligations,
  tasks,
  claims,
  coverageRows,
  questionAnswers,
  issueIdByObligation,
}: {
  issue: JsonRecord;
  obligations: JsonRecord[];
  tasks: TraceReasoningTask[];
  claims: TraceReasoningClaim[];
  coverageRows: JsonRecord[];
  questionAnswers: ProjectedQuestionAnswer[];
  issueIdByObligation: Map<string, string[]>;
}): TraceReasoningIssue {
  const issueId = string(issue.issue_id);
  const questionAnswer = questionAnswers.find(
    (answer) => answer.issueId === issueId,
  );
  const obligationIds = obligations
    .filter((obligation) =>
      (issueIdByObligation.get(string(obligation.obligation_id)) ?? [])
        .includes(issueId)
    )
    .map((obligation) => string(obligation.obligation_id));
  const questionClaimRefs = new Set(questionAnswer?.claimRefs ?? []);
  const issueClaims = claims.filter((claim) =>
    claim.issueIds.includes(issueId) || questionClaimRefs.has(claim.claimRef)
  );
  const acceptedClaims = issueClaims.filter(
    (claim) => claim.verificationStatus === "accepted",
  );
  const explicitlyUsedClaims = acceptedClaims.filter((claim) =>
    questionClaimRefs.has(claim.claimRef)
  );
  const finalAnswerClaims = acceptedClaims.filter((claim) => claim.usedInAnswer);
  const coverages = coverageRows.filter((coverage) =>
    obligationIds.includes(string(coverage.obligation_id))
  );
  const coverageStatuses = coverages.map((coverage) => string(coverage.status));
  let status: TraceReasoningIssue["status"];
  if (questionAnswer) status = questionAnswer.status;
  else if (finalAnswerClaims.length) status = "unbound";
  else if (acceptedClaims.length) status = "omitted";
  else if (
    coverageStatuses.some((value) => value === "unavailable")
  ) status = "unresolved";
  else status = "unresolved";
  return {
    issueId,
    parentIssueId: nullableString(issue.parent_issue_id),
    question: string(issue.question),
    targetClaimKind: string(issue.target_claim_kind),
    status,
    ...(questionAnswer?.text
      ? { answerText: excerpt(questionAnswer.text, 260) }
      : {}),
    taskIds: tasks
      .filter((task) => task.issueIds.includes(issueId))
      .map((task) => task.taskId),
    claimRefs: acceptedClaims.map((claim) => claim.claimRef),
    usedClaimRefs: (
      questionAnswer ? explicitlyUsedClaims : finalAnswerClaims
    ).map((claim) => claim.claimRef),
    limitationRefs: unique([
      ...coverages.flatMap((coverage) => strings(coverage.limitation_refs)),
      ...acceptedClaims.flatMap((claim) => claim.limitationRefs),
      ...(questionAnswer?.limitationRefs ?? []),
    ]),
  };
}

function projectQuestionAnswers(
  narrative: JsonRecord,
  materialProjection: JsonRecord,
): ProjectedQuestionAnswer[] {
  const requirementsByHandle = new Map(
    recordArray(materialProjection.publication_requirements).map(
      (requirement) => [string(requirement.requirement_handle), requirement],
    ),
  );
  const claimRefByHandle = new Map(
    recordArray(materialProjection.claims).map((claim) => [
      string(claim.claim_handle),
      string(claim.claim_ref),
    ]),
  );
  const limitationRefByHandle = new Map(
    recordArray(materialProjection.limitations).map((limitation) => [
      string(limitation.limitation_handle),
      string(limitation.limitation_ref),
    ]),
  );
  return recordArray(narrative.blocks).flatMap((block) => {
    const requirementHandles = strings(block.requirement_handles);
    if (!requirementHandles.length) return [];
    const requirements = requirementHandles.flatMap((handle) => {
      const requirement = requirementsByHandle.get(handle);
      return requirement ? [requirement] : [];
    });
    const issueIds = unique(
      requirements.map((requirement) => string(requirement.issue_ref))
        .filter(Boolean),
    );
    if (
      requirements.length !== requirementHandles.length
      || issueIds.length !== 1
    ) return [];
    const requirementStatuses = requirements.map(
      (requirement) => string(requirement.status),
    );
    const status: TraceReasoningIssue["status"] =
      requirementStatuses.every((value) => value === "unavailable")
        ? "unresolved"
        : requirementStatuses.some((value) =>
          ["mixed", "unavailable", "unresolved"].includes(value)
        )
          ? "partial"
          : "answered";
    return [{
      issueId: issueIds[0],
      text: businessText(string(block.text)),
      status,
      claimRefs: unique(
        strings(block.claim_handles)
          .map((handle) => claimRefByHandle.get(handle) ?? "")
          .filter(Boolean),
      ),
      limitationRefs: unique(
        strings(block.limitation_handles)
          .map((handle) => limitationRefByHandle.get(handle) ?? "")
          .filter(Boolean),
      ),
    }];
  });
}

function projectAnswerBlocks(
  publication: JsonRecord,
): TraceReasoningAnswerBlock[] {
  return recordArray(publication.blocks).map((block, index) => ({
    blockId: string(block.block_id) || `answer-block-${index + 1}`,
    role: string(block.role ?? block.statement_role) || "answer",
    text: string(block.text),
    claimRefs: strings(block.claim_refs),
    limitationRefs: strings(block.limitation_refs),
  }));
}

function mapIssueIdsByObligation(
  issues: JsonRecord[],
  obligations: JsonRecord[],
) {
  const result = new Map<string, string[]>();
  for (const obligation of obligations) {
    const obligationId = string(obligation.obligation_id);
    const successPolicy = record(obligation.success_policy);
    const explicitIssueRef = string(successPolicy?.issue_ref);
    const inferredIssueIds = issues
      .filter((issue) =>
        string(issue.target_claim_kind) === string(obligation.claim_kind)
      )
      .map((issue) => string(issue.issue_id));
    const issueIds = explicitIssueRef
      ? issues.some((issue) => string(issue.issue_id) === explicitIssueRef)
        ? [explicitIssueRef]
        : []
      : inferredIssueIds.length === 1
        ? inferredIssueIds
        : [];
    result.set(obligationId, issueIds);
  }
  return result;
}

function projectClaimFacts(
  materialClaim: JsonRecord | undefined,
  materialByHandle: Map<string, JsonRecord>,
): TraceReasoningFact[] {
  if (!materialClaim) return [];
  const facts = strings(materialClaim.material_handles)
    .flatMap((handle) => recordArray(materialByHandle.get(handle)?.facts))
    .map((fact) => ({
      name: string(fact.name),
      value: string(fact.value),
    }))
    .filter((fact) => fact.name && fact.value);
  const preferred = PREFERRED_FACT_NAMES.flatMap((suffix) => {
    const match = facts.find((fact) =>
      fact.name === suffix || fact.name.endsWith(`.${suffix}`)
    );
    return match ? [match] : [];
  });
  const selectedFacts = uniqueBy(
    preferred.length ? preferred : facts.filter((fact) =>
      businessFactEligible(fact.name)
    ),
    (fact) => `${fact.name}:${fact.value}`,
  ).slice(0, 4);
  return selectedFacts.map((fact) => ({
    name: businessFactLabel(fact.name),
    value: businessFactValue(fact.name, fact.value),
  }));
}

function claimSummary(
  claimKind: string,
  factualPayload: JsonRecord | undefined,
  facts: TraceReasoningFact[],
) {
  const businessReadout = string(factualPayload?.business_readout);
  if (businessReadout) return businessReadout;
  const candidateSubject = string(factualPayload?.candidate_subject);
  if (candidateSubject) return candidateSubject;
  const factText = facts.slice(0, 2)
    .map((fact) => `${fact.name}=${fact.value}`)
    .join("；");
  const label = claimKindBusinessLabel(claimKind);
  return factText ? `${label}：${factText}` : label;
}

function projectFailure(failure: JsonRecord): TraceCapabilityFailure {
  const layer = string(failure.layer);
  const integrityLevel = string(failure.integrity_level);
  const normalizedLayer = ["query", "capability", "evidence", "persistence"]
    .includes(layer)
    ? layer as TraceCapabilityFailure["layer"]
    : "capability";
  return {
    layer: normalizedLayer,
    kind: string(failure.kind) || "unknown",
    integrityLevel: [
      "expected_boundary",
      "task",
      "shared_authority",
    ].includes(integrityLevel)
      ? integrityLevel as TraceCapabilityFailure["integrityLevel"]
      : "task",
    businessBoundary: customerSafeBusinessBoundary(
      string(failure.business_boundary),
      normalizedLayer,
    ),
  };
}

function customerSafeBusinessBoundary(
  value: string,
  layer: TraceCapabilityFailure["layer"],
) {
  const boundary = value.trim();
  if (boundary && !/^[a-z][a-z0-9_.:/-]*$/i.test(boundary)) return boundary;
  return ({
    query: "这一步的查询没有形成可用结果，未进入后续结论。",
    capability: "这一步缺少当前分析所需的可用输入，未进入后续结论。",
    evidence: "这一步的结果未达到结论所需的证据要求。",
    persistence: "这一步的结果没有完成固化，未进入后续结论。",
  } as const)[layer];
}

function capabilityBusinessLabel(capabilityId: string) {
  return ({
    compare_periods: "比较目标时段与对照时段",
    market_health_compare: "比较市场整体表现",
    post_payment_behavior_compare: "比较付费后的用户行为",
    post_payment_tier_behavior: "观察不同付费层级的后续行为",
    market_channel_context: "检查市场与渠道背景",
    source_reconciliation: "核对不同数据来源",
    compare_period_phases: "检验月内阶段模式",
    rolling_window_compare: "检查滚动窗口变化",
    weekday_calendar_compare: "检查星期与日历效应",
    event_window_compare: "比较事件前后的付费表现",
    internal_operation_event_window_compare: "比较运营活动前后的付费表现",
    formula_decompose: "拆解付费金额的公式构成",
    funnel_decompose: "拆解转化漏斗",
    candidate_dimension_screen: "筛查可能的业务驱动因素",
    payment_outcome_compare: "比较支付结果",
    data_quality_profile: "检查数据质量",
    metric_coverage_profile: "检查数据覆盖范围",
    metric_timeseries: "构建每日付费金额序列",
    event_evidence: "查找外部事件线索",
    internal_operation_event_evidence: "查找内部运营活动",
    cross_source_association: "检查跨来源因素关联",
    cross_source_panel_association: "检查跨来源面板关联",
    segment_contribution: "拆解分群贡献",
    segment_breakdown: "拆解业务分群",
    segment_shift_compare: "比较分群结构变化",
    user_mix_contribution: "评估用户结构贡献",
    high_value_user_contribution: "评估高价值用户贡献",
    joint_attribution: "联合验证多个驱动因素",
    outlier_scan: "识别异常时期",
    outlier_contribution: "评估异常时期的影响",
    change_point_scan: "识别趋势转折点",
  } as Record<string, string>)[capabilityId]
    ?? capabilityId.replaceAll("_", " ");
}

function queryBusinessLabel(queryIntent: string, index: number) {
  const intentLabel = ({
    metric_timeseries: "指标时间序列",
    period_comparison: "时段对比",
    phase_comparison: "月内阶段对比",
    dimension_breakdown: "业务维度拆解",
    event_window_comparison: "事件前后对比",
    source_reconciliation: "数据来源对账",
    data_quality: "数据质量检查",
    metric_coverage: "数据覆盖检查",
    daily_metric_baselines: "每日指标基线",
    time_bucket_scan: "月内时间分段扫描",
    channel_context_probe: "渠道背景检查",
    channel_context_total_probe: "渠道总体对照",
    source_reconciliation_probe: "数据来源对账",
    data_quality_probe: "数据质量检查",
    component_driver_scan: "公式因素贡献计算",
    dimension_contribution_scan: "业务维度贡献扫描",
    high_value_scan: "高价值用户结构检查",
    association_candidate_timeseries: "候选因素时间序列",
    association_outcome_timeseries: "结果指标时间序列",
    event_context_probe: "事件背景检查",
  } as Record<string, string>)[queryIntent]
    ?? queryIntent.replaceAll("_", " ");
  return intentLabel
    ? `查询 ${index + 1} · ${intentLabel}`
    : `查询 ${index + 1}`;
}

function queryUiStatus(
  executionStatus: string,
  completenessStatus: string,
  analysisReadiness: string,
): TraceReasoningQuery["status"] {
  if (["running", "executing", "leased"].includes(executionStatus)) {
    return "running";
  }
  if (["queued", "pending", "not_started", ""].includes(executionStatus)) {
    return "waiting";
  }
  if (["succeeded", "completed", "ready"].includes(executionStatus)) {
    return (
      ["complete", "completed", "ready", "analysis_ready", ""]
        .includes(completenessStatus)
      && !["limited", "partial", "not_ready"].includes(analysisReadiness)
    )
      ? "completed"
      : "limited";
  }
  return "failed";
}

function firstBusinessReadout(values: Array<JsonRecord | undefined>) {
  for (const value of values) {
    const readout = nestedString(value, "business_readout", 0);
    if (readout) return readout;
  }
  return "";
}

function nestedString(
  value: unknown,
  targetKey: string,
  depth: number,
): string {
  if (depth > 4) return "";
  const parsed = record(value);
  if (!parsed) return "";
  const direct = string(parsed[targetKey]);
  if (direct) return direct;
  for (const child of Object.values(parsed)) {
    const nested = nestedString(child, targetKey, depth + 1);
    if (nested) return nested;
  }
  return "";
}

function businessFactLabel(name: string) {
  const suffix = name.split(".").at(-1) ?? name;
  return ({
    business_readout: "业务观察",
    direction_consistency_ratio: "目标组更高的月份占比",
    comparable_periods: "可比较月份",
    median_uplift: "中位变化",
    target_value: "目标组值",
    baseline_value: "对照组值",
    best_correlation: "最高相关系数",
    best_sample_size: "有效样本量",
    share_delta: "占比变化",
    paid_user_count_contribution_share: "付费人数贡献",
    amount_per_paid_user_contribution_share: "人均付费金额贡献",
    frequency_contribution_share: "付费频次贡献",
    amount_per_payment_contribution_share: "单次付费金额贡献",
    point_count: "数据点数",
    event_occurrence_count: "活动次数",
    covered_dataset_count: "已覆盖数据源",
    country_scope_violation_count: "地域范围异常数",
  } as Record<string, string>)[suffix] ?? suffix.replaceAll("_", " ");
}

function claimKindBusinessLabel(claimKind: string) {
  return ({
    direction: "方向判断",
    comparison: "对比结果",
    comparative_change: "金额对比",
    stability: "稳定性判断",
    baseline_stability: "稳定性判断",
    recurring_pattern_existence: "周期模式判断",
    anomaly: "异常情况",
    anomaly_observation: "异常情况",
    driver: "驱动因素",
    formula_component_contribution: "公式因素贡献",
    segment_contribution: "分群贡献",
    segment_shift: "分群结构变化",
    factor_association: "因素关联",
    candidate_mechanism: "待验证因素",
    data_quality: "数据质量",
    data_quality_boundary: "数据质量边界",
    coverage: "数据覆盖",
    contract_coverage_and_trust_boundary: "数据覆盖与可信边界",
    event_occurrence: "运营活动记录",
    event_relative_change: "活动前后变化",
  } as Record<string, string>)[claimKind] ?? businessPhrase(claimKind);
}

function businessFactEligible(name: string) {
  const normalized = name.toLowerCase();
  return ![
    "digest",
    "signature",
    "evidence_scope",
    "scope_ref",
    "contract_ref",
    "result_ref",
    "query_ref",
    "snapshot_ref",
  ].some((part) => normalized.includes(part));
}

function businessFactValue(name: string, value: string) {
  const suffix = name.split(".").at(-1) ?? name;
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return value;
  if (
    suffix.includes("ratio")
    || suffix.includes("share")
    || suffix.includes("uplift")
    || suffix.includes("delta")
  ) {
    return `${(numeric * 100).toLocaleString("zh-CN", {
      maximumFractionDigits: 2,
    })}%`;
  }
  return numeric.toLocaleString("zh-CN", {
    maximumFractionDigits: Number.isInteger(numeric) ? 0 : 2,
  });
}

function businessPhrase(value: string) {
  const phrases: Record<string, string> = {
    comparative: "对比",
    change: "变化",
    recurring: "周期",
    pattern: "模式",
    existence: "判断",
    baseline: "基准",
    stability: "稳定性",
    formula: "公式",
    component: "因素",
    contribution: "贡献",
    data: "数据",
    quality: "质量",
    boundary: "边界",
    driver: "驱动因素",
    anomaly: "异常",
    observation: "观察",
    event: "活动",
    coverage: "覆盖",
  };
  const translated = value
    .split("_")
    .map((token) => phrases[token] ?? "")
    .filter(Boolean)
    .join("");
  return translated || "业务事实";
}

function businessText(value: string) {
  return value
    .replace(/（e\d+）/gi, "")
    .replace(/\be\d+\b/gi, "")
    .replace(/\s+/g, " ")
    .trim();
}

function isoString(value: unknown) {
  if (value instanceof Date) return value.toISOString();
  if (typeof value !== "string") return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "" : parsed.toISOString();
}

function record(value: unknown): JsonRecord | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as JsonRecord
    : undefined;
}

function recordArray(value: unknown): JsonRecord[] {
  return Array.isArray(value)
    ? value.flatMap((item) => {
      const parsed = record(item);
      return parsed ? [parsed] : [];
    })
    : [];
}

function string(value: unknown) {
  return typeof value === "string" ? value : "";
}

function nullableString(value: unknown) {
  return value === null ? null : string(value) || null;
}

function strings(value: unknown) {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function stringRecord(value: unknown): Record<string, string> {
  const parsed = record(value);
  if (!parsed) return {};
  return Object.fromEntries(
    Object.entries(parsed).flatMap(([key, item]) =>
      typeof item === "string" ? [[key, item]] : []
    ),
  );
}

function integer(value: unknown) {
  return typeof value === "number" && Number.isInteger(value)
    ? value
    : typeof value === "string" && /^\d+$/.test(value)
      ? Number(value)
      : undefined;
}

function unique(values: string[]) {
  return [...new Set(values)];
}

function uniqueBy<T>(values: T[], key: (value: T) => string) {
  const seen = new Set<string>();
  return values.filter((value) => {
    const valueKey = key(value);
    if (seen.has(valueKey)) return false;
    seen.add(valueKey);
    return true;
  });
}

function groupBy<T>(values: T[], key: (value: T) => string) {
  const grouped = new Map<string, T[]>();
  for (const value of values) {
    const valueKey = key(value);
    const current = grouped.get(valueKey) ?? [];
    current.push(value);
    grouped.set(valueKey, current);
  }
  return grouped;
}

function excerpt(value: string, maxLength: number) {
  const normalized = value.replace(/\s+/g, " ").trim();
  return normalized.length <= maxLength
    ? normalized
    : `${normalized.slice(0, maxLength - 1)}…`;
}
