import { readdir, readFile, stat as fsStat } from "fs/promises";
import path from "path";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type JsonObject = Record<string, any>;

const phase6ArtifactRoot = path.join(process.cwd(), "artifacts", "phase-6", "live-question-family");
const fallbackArtifactRoot = path.join(process.cwd(), "artifacts", "phase-5", "live-node-system", "20260707-v31-prompt-audit-r2");

const familyLabel: Record<string, string> = {
  pattern_explanation: "模式解释",
  paid_amount_change_explanation: "付费金额变化解释",
  business_object_impact_review: "业务对象影响评估",
  segment_or_factor_attribution: "分群或因素归因",
  revenue_health_review: "收入健康评估",
  anomaly_or_black_swan_review: "异常或突发因素评估",
  custom_baseline_comparison: "自定义基线对比",
  data_quality_or_evidence_review: "数据质量或证据评估",
};

const caseMeta: Record<string, { label: string; question?: string; expectedStatus?: string }> = {
  full_month_start_vs_mid_end: {
    label: "全量月初 vs 月中/月末",
    question: "全量样本看，2024-01到2026-06每个月月初1-10号付费金额是否高于月中和月末？",
    expectedStatus: "degraded",
  },
  full_month_boundary_vs_mid: {
    label: "全量月边界 vs 月中",
    question: "全量样本看，2024-01到2026-06每个月月边界窗口（1-8号或26号以后）相比月中是否更高？",
    expectedStatus: "degraded",
  },
  full_thu_fri_vs_mon_sun: {
    label: "全量周四/周五 vs 周一/周日",
    question: "全量样本看，周四/周五的付费金额是否稳定高于周一/周日？",
    expectedStatus: "degraded",
  },
  full_rolling_28_day_growth: {
    label: "全量 28 日滚动增长",
    question: "全量样本看，28日滚动付费金额是否呈持续增长模式？",
    expectedStatus: "passed",
  },
  full_2026_q2_vs_q1: {
    label: "全量 2026 Q2 vs Q1",
    question: "2026年Q2相比Q1，日均付费金额有没有明显抬升？",
    expectedStatus: "passed",
  },
  full_month_end_vs_mid: {
    label: "全量月末 vs 月中",
    question: "全量样本看，2024-01到2026-06每个月月末21号以后付费金额是否高于月中？",
    expectedStatus: "degraded",
  },
  full_wajespecial_vs_other_by_month: {
    label: "WajeSpecial vs 其他渠道",
    question: "全量样本看，WajeSpecial渠道的月度日均付费金额是否稳定高于其他渠道合计？",
    expectedStatus: "degraded",
  },
  driver_q2_vs_q1_paid_users: {
    label: "Q2 vs Q1 贡献拆解",
    question: "2026年Q2相比Q1付费金额提升，主要是付费用户数增加还是单付费用户金额提升带来的？",
    expectedStatus: "passed",
  },
  full_weekend_vs_workday: {
    label: "全量周末 vs 工作日",
    question: "全量样本看，周末付费金额是否稳定高于工作日？",
    expectedStatus: "degraded",
  },
  full_december_vs_november: {
    label: "全量 12 月 vs 11 月",
    question: "全量样本看，12月相比11月的日均付费金额是否更高？",
    expectedStatus: "passed",
  },
  full_q2_vs_q1_by_year: {
    label: "全量逐年 Q2 vs Q1",
    question: "全量样本看，每年Q2相比Q1的日均付费金额是否都有抬升？",
    expectedStatus: "degraded",
  },
};

const todos = [
  { id: "intent", label: "理解业务问题和边界" },
  { id: "route", label: "设计并验收分析路径" },
  { id: "data", label: "确认数据口径和安全" },
  { id: "capability", label: "执行证据路径" },
  { id: "answer", label: "生成并审计答案" },
];

export async function GET() {
  const replays = await readAllReplays();
  return NextResponse.json({ replays });
}

async function readAllReplays() {
  const artifactRoot = await resolveArtifactRoot();
  const files = await listFiles(artifactRoot);
  const debugFiles = files.filter((file) => /(?:_eval|final_node_debug_summary)\.json$/.test(path.basename(file)));
  const answerPackages = files.filter((file) => {
    const directory = path.basename(path.dirname(file));
    return path.basename(file) === "answer_package.json" && /^(phase4-real-|phase5-node-debug-)/.test(directory);
  });
  const replayGroups = await Promise.all([
    ...debugFiles.map((file) => readDebugArtifactReplays(file, artifactRoot)),
    ...answerPackages.map((file) => readAnswerPackageReplay(file, artifactRoot).then((replay) => [replay])),
  ]);
  return replayGroups
    .flat()
    .filter(Boolean)
    .sort((left, right) => (right.generatedAt ?? 0) - (left.generatedAt ?? 0));
}

async function readAnswerPackageReplay(filePath: string, artifactRoot: string) {
  const artifact = JSON.parse(await readFile(filePath, "utf8"));
  const caseId = String(artifact.run_id ?? path.basename(path.dirname(filePath))).replace(/^(phase4-real-|phase5-node-debug-)/, "");
  const meta = caseMeta[caseId] ?? { label: caseId };
  const summary = sectionPayload(artifact, "summary");
  const evidence = sectionPayload(artifact, "evidence").evidence ?? [];
  const finalExplanation = summary.final_explanation ?? {};
  const status = summary.claims?.length ? "passed" : finalExplanation.status || "degraded";
  const llmCalls = artifact.admin_audit?.llm_calls ?? [];
  const businessThreads = traceBusinessThreads(llmCalls);
  const checkpoints = artifact.checkpoint_events ?? [];
  const events = timelineEvents(artifact, summary, evidence, finalExplanation, status, llmCalls, checkpoints);
  const timing = replayTiming(events);
  const nodes = graphNodes(artifact, summary, evidence, finalExplanation, status, llmCalls, checkpoints);
  const traceEvidence = evidence.map(traceEvidenceItem);
  const traceClaims = traceClaimsFromSummary(summary);
  const summaryCards = answerStats(summary, evidence, finalExplanation, status, artifact);
  const answer = answerPayload(summary, evidence, finalExplanation, status, traceClaims, traceEvidence, summaryCards);

  return {
    id: `answer-package:${path.relative(artifactRoot, filePath)}`,
    label: `${meta.label} · 完整运行`,
    question: meta.question ?? "",
    expectedStatus: meta.expectedStatus ?? status,
    status,
    runId: artifact.run_id,
    todos,
    events,
    summaryCards,
    businessThreads,
    traceClaims,
    traceEvidence,
    messages: traceMessages(meta.question ?? "", nodes, answer),
    answer,
    timing,
    generatedAt: Date.parse(artifact.checkpoint_events?.at?.(-1)?.finished_at ?? "") || 0,
    processSummary: {
      checkpointCount: artifact.checkpoint_events?.length ?? 0,
      llmCallCount: llmCalls.length,
      acceptedGraph: artifact.accepted_graph ?? [],
      verifierStatus: artifact.admin_audit?.verifier?.status ?? "unknown",
      sourceArtifact: path.relative(artifactRoot, filePath),
      debugStage: "完整运行",
      nodes,
    },
  };
}

function traceMessages(question: string, nodes: JsonObject[], answer: JsonObject) {
  return [
    {
      id: "user-question",
      role: "user",
      text: question,
      title: "用户问题",
    },
    ...nodes.map((node) => ({
      id: `node-message-${node.id}`,
      role: node.owner === "LLM" ? "assistant" : "tool",
      title: node.label,
      text: node.summary,
      nodeId: node.id,
    })),
    {
      id: "final-answer",
      role: "assistant",
      title: "最终回答",
      text: answer.answerText,
    },
  ];
}

function traceBusinessThreads(llmCalls: JsonObject[]) {
  const intent = llmCalls.find((call) => call.task === "business_intent")?.structured_output ?? {};
  const primary = String(intent.primary_question_family ?? intent.question_family ?? "");
  const families = Array.from(
    new Set(
      [
        ...(Array.isArray(intent.question_families) ? intent.question_families : []),
        primary,
        ...(Array.isArray(intent.secondary_question_families) ? intent.secondary_question_families : []),
      ]
        .map((item) => String(item || "").trim())
        .filter(Boolean),
    ),
  );
  return families.map((family) => ({
    label: family === primary ? "主业务线" : "旁路业务线",
    value: familyLabel[family] ?? family,
  }));
}

async function readDebugArtifactReplays(filePath: string, artifactRoot: string) {
  const artifact = JSON.parse(await readFile(filePath, "utf8"));
  const rows = Array.isArray(artifact.rows) ? artifact.rows : [];
  const stage = debugStageLabel(filePath);
  return rows
    .map((row: JsonObject, index: number) => debugReplay(row, artifact, filePath, stage, index, artifactRoot))
    .filter(Boolean);
}

async function resolveArtifactRoot() {
  const explicitLatest = path.join(phase6ArtifactRoot, "latest");
  if (await directoryExists(explicitLatest)) return explicitLatest;
  const phase6Latest = await newestChildDirectory(phase6ArtifactRoot);
  if (phase6Latest) return phase6Latest;
  return fallbackArtifactRoot;
}

async function newestChildDirectory(root: string) {
  try {
    const entries = await readdir(root, { withFileTypes: true });
    const directories = entries
      .filter((entry) => entry.isDirectory())
      .map((entry) => path.join(root, entry.name))
      .sort()
      .reverse();
    return directories[0] ?? "";
  } catch {
    return "";
  }
}

async function directoryExists(root: string) {
  try {
    return (await fsStat(root)).isDirectory();
  } catch {
    return false;
  }
}

async function listFiles(root: string): Promise<string[]> {
  const entries = await readdir(root, { withFileTypes: true });
  const nested = await Promise.all(
    entries.map(async (entry) => {
      const fullPath = path.join(root, entry.name);
      if (entry.isDirectory()) return listFiles(fullPath);
      return [fullPath];
    }),
  );
  return nested.flat();
}

function debugReplay(row: JsonObject, artifact: JsonObject, filePath: string, stage: string, index: number, artifactRoot: string) {
  const caseId = String(row.case_id ?? row.caseId ?? "").trim();
  if (!caseId) return undefined;
  const meta = caseMeta[caseId] ?? { label: caseId };
  const status = debugStatus(row, filePath);
  const events = debugTimelineEvents(row, stage, status);
  const timing = replayTiming(events);
  const relativePath = path.relative(artifactRoot, filePath);

  return {
    id: `debug:${relativePath}:${caseId}:${index}`,
    label: `${meta.label} · ${stage}`,
    question: row.question ?? meta.question ?? "",
    expectedStatus: meta.expectedStatus ?? status,
    status,
    runId: `${path.basename(path.dirname(filePath))}:${caseId}`,
    todos,
    events,
    timing,
    generatedAt: Number(artifact.generated_at_epoch ?? 0) * 1000 || Date.parse(row.llm_audit?.finished_at ?? row.audit?.finished_at ?? "") || 0,
    processSummary: {
      checkpointCount: row.checkpoint_events?.length ?? 0,
      llmCallCount: row.llm_audit || row.audit ? 1 : 0,
      acceptedGraph: row.accepted_graph ?? row.compiled_accepted_graph ?? [],
      verifierStatus: row.verifier?.status ?? row.semantic_audit?.audit_status ?? row.route_after_hard_verify ?? row.route_after_next_action ?? "debug",
      sourceArtifact: relativePath,
      debugStage: stage,
    },
  };
}

function debugTimelineEvents(row: JsonObject, stage: string, status: string) {
  const events: JsonObject[] = [];
  const call = row.llm_audit ?? row.audit;
  if (call) {
    events.push(llmConversationEvent(call, 0, {
      label: stage,
      node: nodeForTask(call.task ?? taskForArtifactStage(stage)),
      duration_ms: call.duration_ms,
      started_at: call.started_at,
      finished_at: call.finished_at,
    }));
  } else {
    const output = debugOutput(row);
    if (output) events.push(debugAssistantEvent(row, output, stage));
  }

  const toolEvent = debugToolEvent(row, stage);
  if (toolEvent) events.push(toolEvent);

  const answer = debugAnswerEvent(row, status);
  if (answer) events.push(answer);
  if (!events.length) events.push(debugAssistantEvent(row, row, stage));
  return events;
}

function debugAssistantEvent(row: JsonObject, output: JsonObject, stage: string) {
  const task = taskForArtifactStage(stage);
  return {
    id: `debug-${row.case_id}-${stage}`,
    kind: "assistant",
    todoId: todoIdForTask(task),
    label: stage,
    node: nodeForTask(task),
    text: businessDisplayText(businessTextForCall({ task, structured_output: output })),
    durationMs: 0,
    audit: { task, structured_output: output },
  };
}

function debugToolEvent(row: JsonObject, stage: string) {
  const tools = compact([
    ...(row.executed_capabilities ?? []).map((item: string) => tool(capabilityLabel(item), "completed", "已执行")),
    ...(row.accepted_graph ?? []).map((item: string) => tool(capabilityLabel(item), "completed", "已接受")),
    row.primary_evidence ? tool(capabilityLabel(row.primary_evidence.capability_id ?? row.primary_evidence.capability), "completed", capabilitySummary(row.primary_evidence), row.primary_evidence) : null,
    row.evidence_brief ? tool("证据简报", "completed", evidenceBriefSummary(row.evidence_brief), row.evidence_brief) : null,
    row.verifier ? tool("答案边界校验", row.verifier.errors?.length ? "blocked" : "completed", `${statusLabel(row.verifier.status)} · ${(row.verifier.warnings ?? []).length} 个警告`, row.verifier) : null,
    row.semantic_audit ? tool("语义一致性检查", row.semantic_audit.issues?.length ? "blocked" : "completed", `${statusLabel(row.semantic_audit.audit_status)} · ${(row.semantic_audit.issues ?? []).length} 个问题`, row.semantic_audit) : null,
  ]);
  if (!tools.length) return undefined;
  return {
    id: `debug-tools-${row.case_id}-${stage}`,
    kind: "tool_group",
    todoId: todoIdForTask(taskForArtifactStage(stage)),
    title: stage,
    completedTitle: `${stage}已完成`,
    summary: tools.map((item) => item.label).slice(0, 3).join(" · "),
    tools,
    audit: row,
  };
}

function debugAnswerEvent(row: JsonObject, status: string) {
  const text = row.final_summary_text ?? row.summary_text ?? row.answer_text ?? row.final_explanation?.explanation;
  if (!text) return undefined;
  const claim = row.draft_claims?.[0] ?? row.claims?.[0] ?? {};
  const numbers = claim.numbers ?? row.primary_evidence?.numeric_facts ?? row.primary_evidence?.typed_payload ?? {};
  return {
    id: `debug-answer-${row.case_id}`,
    kind: "answer",
    todoId: "answer",
    answer: {
      status,
      answerText: businessDisplayText(String(text)),
      claims: row.draft_claims ?? row.claims ?? [],
      limitations: (row.evidence_brief?.limitations ?? row.final_explanation?.limitations ?? []).map(limitationLabel),
      repairPath: businessDisplayText(row.final_explanation?.repair_path ?? ""),
      stats: [
        stat("中位提升", percent(numbers.median_uplift)),
        stat("方向命中率", percent(numbers.direction_ratio)),
        stat("周期数", String(numbers.comparable_periods ?? "n/a")),
        stat("结果", statusLabel(status)),
      ],
      evidence: (row.evidence ?? row.public_evidence ?? []).map((entry: JsonObject) => {
        const capability = entry.capability_id ?? entry.capability;
        return {
          capability,
          label: capabilityLabel(capability),
          detail: capabilitySummary(entry),
          strength: entry.strength ?? "unknown",
          limitations: (entry.limitations ?? []).map(limitationLabel),
        };
      }),
    },
  };
}

function debugStatus(row: JsonObject, filePath = "") {
  const route = String(row.terminal_route ?? row.route_after_next_action ?? row.route_after_hard_verify ?? row.final_explanation?.status ?? "");
  if (route.includes("degrad")) return "degraded";
  if (filePath.includes("degraded")) return "degraded";
  if (route.includes("block")) return "blocked";
  if (route.includes("fail")) return "failed";
  return "passed";
}

function debugOutput(row: JsonObject) {
  return (
    row.llm_output ??
    row.boundary_output ??
    row.confirm_output ??
    row.analysis_route_output ??
    row.data_coverage_output ??
    row.next_action_output ??
    row.evidence_interpretation ??
    row.semantic_audit ??
    row.final_explanation ??
    row.verifier
  );
}

function debugStageLabel(filePath: string) {
  const file = path.basename(filePath);
  const dir = path.basename(path.dirname(filePath));
  const labels: Record<string, string> = {
    business_intent_llm_eval: "理解业务意图",
    boundary_decision_llm_eval: "判断问题边界",
    confirm_understanding_llm_eval: "确认业务理解",
    analysis_route_llm_eval: "设计分析路线",
    accept_analysis_route_eval: "验收分析路线",
    inspect_schema_eval: "确认数据口径",
    validate_runtime_binding_eval: "验收数据绑定",
    data_coverage_interpretation_eval: "解释数据覆盖",
    execute_capabilities_eval: "执行证据路径",
    reduce_evidence_eval: "整理证据简报",
    next_action_eval: "判断下一步",
    interpret_evidence_eval: "解释证据含义",
    synthesize_answer_eval: "生成业务答案",
    semantic_audit_eval: "语义一致性检查",
    semantic_audit_after_repair_eval: "修复后语义检查",
    answer_repair_eval: "修复业务答案",
    hard_verify_answer_eval: "答案边界校验",
    degraded_explanation_eval: "生成降级说明",
    final_business_summary_eval: "整理最终业务总结",
    final_node_debug_summary: "全链路调试总结",
  };
  const stem = file.replace(/\.json$/, "");
  return labels[stem] ?? dir;
}

function taskForArtifactStage(stage: string) {
  if (stage.includes("意图")) return "business_intent";
  if (stage.includes("边界") && !stage.includes("答案")) return "boundary_decision";
  if (stage.includes("业务理解")) return "confirm_understanding";
  if (stage.includes("分析路线")) return "analysis_route";
  if (stage.includes("数据覆盖")) return "data_coverage_interpretation";
  if (stage.includes("下一步")) return "next_action";
  if (stage.includes("证据含义")) return "evidence_interpretation";
  if (stage.includes("业务答案")) return "answer_synthesis";
  if (stage.includes("语义")) return "semantic_audit";
  if (stage.includes("降级")) return "degraded_explanation";
  if (stage.includes("最终")) return "final_business_summary";
  return "analysis_route";
}

function sectionPayload(artifact: JsonObject, sectionId: string) {
  return artifact.sections?.find((section: JsonObject) => section.section_id === sectionId)?.payload ?? {};
}

function timelineEvents(
  artifact: JsonObject,
  summary: JsonObject,
  evidence: JsonObject[],
  finalExplanation: JsonObject,
  status: string,
  llmCalls: JsonObject[],
  checkpoints: JsonObject[],
) {
  const events: JsonObject[] = [];
  const remainingCalls = llmCalls.map((call, index) => ({ call, index }));
  const pushed = new Set<string>();

  for (const checkpoint of checkpoints) {
    if (checkpoint.llm) {
      const item = consumeCallForCheckpoint(remainingCalls, checkpoint);
      if (item) events.push(llmConversationEvent(item.call, item.index, checkpoint));
      continue;
    }

    if (checkpoint.node === "accept_analysis_route" && !pushed.has("route")) {
      pushed.add("route");
      events.push(routeToolEvent(artifact, checkpoints));
    }
    if (checkpoint.node === "validate_runtime_binding" && !pushed.has("data")) {
      pushed.add("data");
      events.push(dataToolEvent(artifact, checkpoints));
    }
    if (checkpoint.node === "reduce_evidence" && !pushed.has("capability")) {
      pushed.add("capability");
      events.push(capabilityToolEvent(artifact, evidence, checkpoints));
    }
    if (checkpoint.node === "hard_verify_answer" && !pushed.has("verifier")) {
      pushed.add("verifier");
      events.push(verifierToolEvent(artifact, checkpoints));
    }
    if (checkpoint.node === "persist_artifact" && !pushed.has("answer")) {
      pushed.add("answer");
      events.push(answerEvent(summary, evidence, finalExplanation, status, checkpoints));
    }
  }

  return events.filter(Boolean);
}

function llmConversationEvent(call: JsonObject, index: number, node?: JsonObject) {
  return {
    id: `llm-${index}-${call.task}`,
    kind: "assistant",
    todoId: todoIdForTask(call.task),
    label: node?.label ?? labelForTask(call.task),
    node: node?.node ?? nodeForTask(call.task),
    text: businessDisplayText(businessTextForCall(call)),
    durationMs: eventDurationMs(node) || eventDurationMs(call),
    startedAt: node?.started_at ?? call.started_at ?? "",
    finishedAt: node?.finished_at ?? call.finished_at ?? "",
    audit: auditForCall(call, node),
  };
}

function routeToolEvent(artifact: JsonObject, checkpoints: JsonObject[]) {
  const related = checkpointsFor(checkpoints, ["accept_analysis_route", "clarification_policy_gate", "rebind_after_clarification"]);
  const clarificationGate = related.find((checkpoint) => checkpoint.node === "clarification_policy_gate");
  return {
    id: "tool-route",
    kind: "tool_group",
    todoId: "route",
    title: "确认分析路径",
    completedTitle: "分析路径已确认",
    summary: `${artifact.accepted_graph?.length ?? 0} 条证据路径已纳入本次分析`,
    tools: [
      ...(clarificationGate ? [tool("澄清策略门禁", "completed", reasonLabel(clarificationGate.route ?? clarificationGate.status), clarificationGate)] : []),
      ...compact((artifact.proposed_graph ?? []).map((node: string) => tool(capabilityLabel(node), "completed", "模型提议", { capability: node }))),
      ...compact(
        (artifact.rejected_or_degraded_mutations ?? []).map((mutation: JsonObject) =>
          tool(capabilityLabel(mutation.capability), "completed", mutationDetail(mutation), mutation),
        ),
      ),
    ],
    durationMs: totalDurationMs(related),
    startedAt: firstStartedAt(related),
    finishedAt: lastFinishedAt(related),
    audit: {
      proposed_graph: artifact.proposed_graph ?? [],
      accepted_graph: artifact.accepted_graph ?? [],
      rejected_or_degraded_mutations: artifact.rejected_or_degraded_mutations ?? [],
      checkpoints: related,
    },
  };
}

function dataToolEvent(artifact: JsonObject, checkpoints: JsonObject[]) {
  const related = checkpointsFor(checkpoints, ["inspect_schema", "validate_runtime_binding"]);
  return {
    id: "tool-data",
    kind: "tool_group",
    todoId: "data",
    title: "验证数据口径和安全边界",
    completedTitle: "数据口径和安全边界已验证",
    summary: "查询安全、数据绑定、权限边界均已检查",
    tools: (artifact.admin_audit?.validator_results ?? []).map((result: JsonObject) =>
      tool(validatorLabel(result.validator), result.ok ? "completed" : "blocked", reasonLabel(result.reason), result),
    ),
    durationMs: totalDurationMs(related),
    startedAt: firstStartedAt(related),
    finishedAt: lastFinishedAt(related),
    audit: {
      validator_results: artifact.admin_audit?.validator_results ?? [],
      checkpoints: related,
    },
  };
}

function capabilityToolEvent(artifact: JsonObject, evidence: JsonObject[], checkpoints: JsonObject[]) {
  const related = checkpointsFor(checkpoints, ["execute_capabilities", "reduce_evidence", "execute_joint_attribution"]);
  return {
    id: "tool-capabilities",
    kind: "tool_group",
    todoId: "capability",
    title: "生成证据",
    completedTitle: "证据路径已执行",
    summary: evidenceSummary(evidence),
    tools: evidence.map((entry) =>
      tool(capabilityLabel(entry.capability), "completed", capabilitySummary(entry), {
        capability: entry.capability,
        strength: entry.strength,
        limitations: entry.limitations ?? [],
        evidence_ref: entry.evidence_ref,
      }),
    ),
    durationMs: totalDurationMs(related),
    startedAt: firstStartedAt(related),
    finishedAt: lastFinishedAt(related),
    audit: {
      accepted_graph: artifact.accepted_graph ?? [],
      evidence,
      checkpoints: related,
    },
  };
}

function verifierToolEvent(artifact: JsonObject, checkpoints: JsonObject[]) {
  const verifier = artifact.admin_audit?.verifier ?? {};
  const semantic = artifact.admin_audit?.semantic_audit ?? {};
  const related = checkpointsFor(checkpoints, ["sanitize_answer", "hard_verify_answer"]);
  return {
    id: "tool-verifier",
    kind: "tool_group",
    todoId: "answer",
    title: "校验答案边界",
    completedTitle: "答案边界已校验",
    summary: `答案边界：${statusLabel(verifier.status)} · 语义一致性：${statusLabel(semantic.audit_status)}`,
    tools: [
      tool("语义一致性检查", "completed", `${statusLabel(semantic.audit_status)} · ${(semantic.issues ?? []).length} 个问题`),
      tool("答案边界校验", verifier.errors?.length ? "blocked" : "completed", `${statusLabel(verifier.status)} · ${(verifier.warnings ?? []).length} 个警告`),
      tool("保存答案和审计记录", "completed", "本轮回答和审计记录已保存"),
    ],
    durationMs: totalDurationMs(related),
    startedAt: firstStartedAt(related),
    finishedAt: lastFinishedAt(related),
    audit: { semantic_audit: semantic, verifier, checkpoints: related },
  };
}

function answerEvent(summary: JsonObject, evidence: JsonObject[], finalExplanation: JsonObject, status: string, checkpoints: JsonObject[]) {
  const related = checkpointsFor(checkpoints, ["persist_artifact"]);
  const traceEvidence = evidence.map(traceEvidenceItem);
  const traceClaims = traceClaimsFromSummary(summary);
  return {
    id: "answer",
    kind: "answer",
    todoId: "answer",
    durationMs: totalDurationMs(related),
    startedAt: firstStartedAt(related),
    finishedAt: lastFinishedAt(related),
    answer: answerPayload(summary, evidence, finalExplanation, status, traceClaims, traceEvidence),
  };
}

function answerPayload(
  summary: JsonObject,
  evidence: JsonObject[],
  finalExplanation: JsonObject,
  status: string,
  claims = traceClaimsFromSummary(summary),
  traceEvidence = evidence.map(traceEvidenceItem),
  stats = answerStats(summary, evidence, finalExplanation, status),
) {
  return {
    status,
    answerText: businessDisplayText(
      summary.final_business_summary ||
      summary.answer_text ||
      finalExplanation.explanation ||
      "当前证据不足，不能发布主业务结论。",
    ),
    claims,
    limitations: (summary.limitations ?? []).map(limitationLabel),
    repairPath: businessDisplayText(finalExplanation.repair_path ?? ""),
    stats,
    evidence: traceEvidence,
  };
}

function traceClaimsFromSummary(summary: JsonObject) {
  return (summary.claims ?? []).map((claim: JsonObject) => ({
    text: String(claim.text ?? ""),
    scope: claim.scope ?? "",
    timeWindow: claim.time_window ?? "",
    numbers: claim.numbers ?? {},
    evidenceRefs: claim.evidence_refs ?? [],
  }));
}

function traceEvidenceItem(entry: JsonObject) {
  const capability = entry.capability_id ?? entry.capability;
  return {
    capability,
    label: capabilityLabel(capability),
    detail: capabilitySummary(entry),
    strength: entry.strength ?? "unknown",
    limitations: (entry.limitations ?? []).map(limitationLabel),
    evidenceRef: entry.evidence_ref,
  };
}

function graphNodes(
  artifact: JsonObject,
  summary: JsonObject,
  evidence: JsonObject[],
  finalExplanation: JsonObject,
  status: string,
  llmCalls: JsonObject[],
  checkpoints: JsonObject[],
) {
  const remainingCalls = llmCalls.map((call, index) => ({ call, index }));
  return checkpoints.map((checkpoint, index) => {
    const call = checkpoint.llm ? consumeCallForCheckpoint(remainingCalls, checkpoint) : undefined;
    return {
      id: `${index}-${checkpoint.node}`,
      index: index + 1,
      node: checkpoint.node,
      label: nodeDisplayLabel(checkpoint.node, checkpoint.label),
      owner: checkpoint.llm ? "LLM" : "本地系统",
      status: checkpoint.status ?? "completed",
      route: routeLabel(checkpoint.route ?? ""),
      durationMs: eventDurationMs(checkpoint),
      startedAt: checkpoint.started_at ?? "",
      finishedAt: checkpoint.finished_at ?? "",
      summary: nodeSummary(checkpoint, call?.call, artifact, summary, evidence, finalExplanation, status),
      audit: nodeAudit(checkpoint, call?.call, artifact, evidence),
    };
  });
}

function nodeSummary(
  checkpoint: JsonObject,
  call: JsonObject | undefined,
  artifact: JsonObject,
  summary: JsonObject,
  evidence: JsonObject[],
  finalExplanation: JsonObject,
  status: string,
) {
  if (call) return businessDisplayText(businessTextForCall(call));
  switch (checkpoint.node) {
    case "clarification_policy_gate":
      return `澄清策略：${reasonLabel(checkpoint.route ?? checkpoint.status)}。`;
    case "accept_analysis_route":
      return `接受 ${artifact.accepted_graph?.length ?? 0} 条证据路径：${(artifact.accepted_graph ?? []).map(capabilityLabel).join("、") || "无"}。`;
    case "inspect_schema":
      return "确认当前运行使用聚合数据口径，未暴露原始明细。";
    case "validate_runtime_binding":
      return `完成 ${artifact.admin_audit?.validator_results?.length ?? 0} 项数据和安全校验。`;
    case "execute_capabilities":
      return `执行证据路径：${evidence.map((entry) => capabilityLabel(entry.capability_id ?? entry.capability)).join("、") || "无"}。`;
    case "reduce_evidence":
      return evidenceSummary(evidence);
    case "hard_verify_answer":
      return `答案边界校验：${statusLabel(artifact.admin_audit?.verifier?.status)}。`;
    case "sanitize_answer":
      return "收敛为有边界的业务表述，避免越过证据强度。";
    case "persist_artifact":
      return `保存本轮回答和审计记录，最终状态：${statusLabel(status)}。`;
    default:
      return summary.final_business_summary || finalExplanation.explanation || "节点已完成。";
  }
}

function nodeAudit(checkpoint: JsonObject, call: JsonObject | undefined, artifact: JsonObject, evidence: JsonObject[]) {
  if (call) return auditForCall(call, checkpoint);
  const common = {
    node: checkpoint.node,
    node_label: checkpoint.label,
    status: checkpoint.status,
    route: checkpoint.route,
    duration_ms: checkpoint.duration_ms,
  };
  if (checkpoint.node === "validate_runtime_binding") {
    return { ...common, validator_results: artifact.admin_audit?.validator_results ?? [] };
  }
  if (checkpoint.node === "execute_capabilities" || checkpoint.node === "reduce_evidence") {
    return {
      ...common,
      accepted_graph: artifact.accepted_graph ?? [],
      evidence: evidence.map((entry) => ({
        capability: entry.capability_id ?? entry.capability,
        strength: entry.strength,
        limitations: entry.limitations ?? [],
        evidence_ref: entry.evidence_ref,
      })),
    };
  }
  if (checkpoint.node === "hard_verify_answer") {
    return { ...common, verifier: artifact.admin_audit?.verifier ?? {} };
  }
  return common;
}

function answerStats(
  summary: JsonObject,
  evidence: JsonObject[],
  finalExplanation: JsonObject,
  status: string,
  artifact?: JsonObject,
) {
  const cards: { label: string; value: string; detail?: string }[] = [];
  const claimNumbers = Object.assign({}, ...(summary.claims ?? []).map((claim: JsonObject) => claim.numbers ?? {}));
  const primaryEvidence = evidence[0] ?? {};
  const payload = primaryEvidence.typed_payload ?? {};
  const numeric = { ...payload, ...(primaryEvidence.numeric_facts ?? {}), ...claimNumbers };

  addNumberCard(cards, "付费金额变化", numeric.amount_delta_ratio, percent);
  addNumberCard(cards, "单付费用户金额贡献", numeric.unit_value_share, percent);
  addNumberCard(cards, "付费用户数贡献", numeric.volume_share, percent);
  addNumberCard(cards, "中位变化", numeric.median_uplift, percent);
  addNumberCard(cards, "方向一致比例", numeric.direction_consistency_ratio ?? numeric.direction_ratio, percent);
  addNumberCard(cards, "达到阈值比例", numeric.materiality_hit_ratio, percent);
  addNumberCard(cards, "可比周期", numeric.comparable_periods, (value) => `${Math.round(Number(value))} 个`);
  addNumberCard(cards, "数据行数", numeric.row_count, (value) => `${Math.round(Number(value))} 行`);

  cards.push({
    label: "证据路径",
    value: `${evidence.length}`,
    detail: evidence.map((entry) => capabilityLabel(entry.capability_id ?? entry.capability)).join("、") || "无",
  });
  cards.push({
    label: "校验状态",
    value: statusLabel(status),
    detail: artifact?.admin_audit?.verifier?.status
      ? `答案边界校验${statusLabel(artifact.admin_audit.verifier.status)}`
      : statusLabel(finalExplanation.status),
  });

  return cards.slice(0, 4);
}

function addNumberCard(
  cards: { label: string; value: string; detail?: string }[],
  label: string,
  value: unknown,
  formatter: (value: unknown) => string,
) {
  const numberValue = Number(value);
  if (!Number.isFinite(numberValue)) return;
  cards.push({ label, value: formatter(numberValue) });
}

function businessTextForCall(call: JsonObject) {
  const output = call.structured_output ?? {};
  const displaySummary = businessDisplayText(String(output.display_summary ?? "")).trim();
  if (displaySummary) return displaySummary;
  switch (call.task) {
    case "business_intent":
      return compact([
        output.status_message,
        output.target_claim ? `本轮要回答的是：${output.target_claim}。` : "",
        `分析范围 ${scopeLabel(output.scope)}，观察窗口 ${output.time_window ?? "unknown"}。`,
      ]).join(" ");
    case "boundary_decision":
      return compact([
        output.decision_summary,
        output.recommended_assumption ? `系统按这个假设继续：${jsonInline(output.recommended_assumption)}。` : "",
      ]).join(" ");
    case "confirm_understanding":
      return compact([
        output.status_message || "已确认本次业务理解。",
        acceptedAssumptionsText(output.accepted_assumptions),
      ]).join(" ");
    case "analysis_route":
      return compact([
        output.route_summary,
        output.expected_evidence?.length ? `计划收集的证据包括：${output.expected_evidence.join("；")}。` : "",
        output.decision_summary,
      ]).join(" ");
    case "data_coverage_interpretation":
      return compact([output.business_impact, output.decision_summary]).join(" ");
    case "next_action":
      return output.decision_summary || `下一步进入 ${output.next_action ?? "后续分析"}。`;
    case "evidence_interpretation":
      return compact([output.interpretation, output.evidence_boundary, output.decision_summary]).join(" ");
    case "causal_audit":
      return compact([
        output.publishable_wording,
        output.answer_guidance,
        output.main_risks?.length ? `需要保留的边界：${output.main_risks.join("；")}。` : "",
      ]).join(" ");
    case "answer_synthesis":
    case "answer_repair":
      return output.answer_text || "已生成答案草稿。";
    case "final_business_summary":
      return output.summary_text || "已整理最终业务总结。";
    case "semantic_audit":
      return compact([
        output.issues?.length
          ? `审计发现回答里有需要收敛的表述：${output.issues.map(issueText).join("；")}。`
          : "回答和证据边界一致，可以继续。",
      ]).join(" ");
    case "degraded_explanation":
      return compact([output.explanation, output.repair_path ? `修复路径：${output.repair_path}` : ""]).join(" ");
    default:
      return jsonInline(output);
  }
}

function auditForCall(call: JsonObject, node?: JsonObject) {
  return {
    task: call.task,
    node: node?.node ?? nodeForTask(call.task),
    node_label: node?.label ?? labelForTask(call.task),
    model: call.model,
    prompt_version: call.prompt_version,
    started_at: call.started_at ?? node?.started_at ?? "",
    finished_at: call.finished_at ?? node?.finished_at ?? "",
    duration_ms: call.duration_ms ?? node?.duration_ms ?? 0,
    node_duration_ms: node?.duration_ms ?? null,
    usage: call.usage ?? {},
    structured_output: call.structured_output ?? {},
    message_count: Array.isArray(call.messages) ? call.messages.length : 0,
    messages_preview: messagePreviews(call.messages),
  };
}

function messagePreviews(messages: unknown) {
  if (!Array.isArray(messages)) return [];
  return messages.map((message) => {
    const item = message as JsonObject;
    const content = String(item.content ?? "");
    return {
      role: item.role ?? "",
      content: content.length > 1200 ? `${content.slice(0, 1200)}...` : content,
    };
  });
}

function labelForTask(task: string) {
  return (
    {
      business_intent: "理解用户业务意图",
      boundary_decision: "判断问题边界",
      confirm_understanding: "确认本次业务理解",
      analysis_route: "设计分析路线",
      data_coverage_interpretation: "解释数据覆盖影响",
      next_action: "判断下一步分析动作",
      evidence_interpretation: "解释证据和业务含义",
      causal_audit: "审阅因果和业务含义",
      answer_synthesis: "生成业务答案草稿",
      semantic_audit: "语义审计答案",
      answer_repair: "按校验反馈修答案",
      final_business_summary: "整理最终业务回答",
      degraded_explanation: "生成降级说明",
      blocked_explanation: "生成阻断说明",
    } as Record<string, string>
  )[task] ?? task;
}

function nodeDisplayLabel(node: string, fallback?: unknown) {
  return (
    {
      understand_business_intent: "理解用户业务意图",
      decide_question_boundary: "判断问题边界是否清楚",
      clarification_policy_gate: "判断是否需要追问",
      confirm_business_understanding: "确认本次业务理解",
      design_analysis_route: "设计分析路径",
      accept_analysis_route: "验收分析路径",
      inspect_schema: "确认数据口径",
      validate_runtime_binding: "校验数据和安全边界",
      interpret_data_coverage: "解释数据覆盖影响",
      execute_capabilities: "执行证据路径",
      reduce_evidence: "整理证据简报",
      decide_next_action: "判断下一步动作",
      interpret_evidence: "解释证据含义",
      synthesize_answer: "生成业务答案",
      causal_audit: "审阅因果和业务含义",
      semantic_audit: "检查答案语义边界",
      repair_answer: "修正业务答案",
      sanitize_answer: "收敛答案表述",
      hard_verify_answer: "校验答案边界",
      final_business_summary: "整理最终业务回答",
      persist_artifact: "保存回答和审计记录",
    } as Record<string, string>
  )[node] ?? String(fallback ?? labelForTask(taskForNode(node)));
}

function capabilityLabel(value: unknown) {
  return (
    {
      data_quality_check: "数据完整性检查",
      data_quality_profile: "数据质量概览",
      pattern_scan: "模式强度检验",
      compare_periods: "期间对比",
      compare_period_phases: "周期内阶段对比",
      rolling_window_compare: "滚动窗口对比",
      weekday_calendar_compare: "星期日历对比",
      event_window_compare: "事件窗口对比",
      formula_decompose: "指标构成检查",
      driver_decomposition: "贡献拆解",
      segment_contribution: "分群贡献拆解",
      outlier_contribution: "异常贡献拆解",
      event_evidence: "事件解释线索",
      segment_bridge: "分群一致性检查",
      outlier_scan: "异常周期检查",
      joint_attribution: "组合归因检查",
      answer_verify: "答案边界校验",
    } as Record<string, string>
  )[String(value)] ?? String(value ?? "证据路径");
}

function validatorLabel(value: unknown) {
  return (
    {
      sql_safety: "查询安全检查",
      runtime_binding: "数据绑定检查",
      permission: "权限边界检查",
    } as Record<string, string>
  )[String(value)] ?? String(value ?? "校验项");
}

function todoIdForTask(task: string) {
  if (["business_intent", "boundary_decision", "confirm_understanding"].includes(task)) return "intent";
  if (["analysis_route"].includes(task)) return "route";
  if (["data_coverage_interpretation"].includes(task)) return "data";
  if (["next_action", "evidence_interpretation", "answer_synthesis", "semantic_audit", "answer_repair", "degraded_explanation"].includes(task)) return "answer";
  return "capability";
}

function nodeForTask(task: string) {
  return (
    {
      business_intent: "understand_business_intent",
      boundary_decision: "decide_question_boundary",
      clarification_question: "generate_clarification",
      confirm_understanding: "confirm_business_understanding",
      analysis_route: "design_analysis_route",
      route_repair: "repair_analysis_route",
      data_coverage_interpretation: "interpret_data_coverage",
      next_action: "decide_next_action",
      promotion_direction: "promotion_direction",
      evidence_interpretation: "interpret_evidence",
      answer_synthesis: "synthesize_answer",
      semantic_audit: "semantic_audit",
      answer_repair: "repair_answer",
      final_business_summary: "final_business_summary",
      degraded_explanation: "generate_degraded_explanation",
      blocked_explanation: "generate_blocked_explanation",
    } as Record<string, string>
  )[task] ?? task;
}

function taskForNode(node: string) {
  return (
    {
      understand_business_intent: "business_intent",
      decide_question_boundary: "boundary_decision",
      generate_clarification: "clarification_question",
      confirm_business_understanding: "confirm_understanding",
      design_analysis_route: "analysis_route",
      repair_analysis_route: "route_repair",
      interpret_data_coverage: "data_coverage_interpretation",
      decide_next_action: "next_action",
      promotion_direction: "promotion_direction",
      interpret_evidence: "evidence_interpretation",
      synthesize_answer: "answer_synthesis",
      semantic_audit: "semantic_audit",
      repair_answer: "answer_repair",
      final_business_summary: "final_business_summary",
      generate_degraded_explanation: "degraded_explanation",
      generate_blocked_explanation: "blocked_explanation",
    } as Record<string, string>
  )[node] ?? node;
}

function capabilitySummary(entry: JsonObject) {
  const payload = entry.typed_payload ?? {};
  const capability = entry.capability_id ?? entry.capability;
  if (["pattern_scan", "compare_periods", "compare_period_phases", "rolling_window_compare", "weekday_calendar_compare", "event_window_compare"].includes(String(capability))) {
    return compact([
      `证据强度 ${strengthLabel(entry.strength)}`,
      `中位提升 ${percent(entry.median_uplift ?? entry.numeric_facts?.median_uplift ?? payload.median_uplift)}`,
      `方向命中率 ${percent(entry.direction_ratio ?? entry.numeric_facts?.direction_ratio ?? payload.direction_ratio)}`,
      `${entry.comparable_periods ?? entry.numeric_facts?.comparable_periods ?? payload.comparable_periods ?? "n/a"} 个周期`,
      ...(entry.limitations ?? []).map(limitationLabel),
    ]).join(" · ");
  }
  if (["data_quality_check", "data_quality_profile"].includes(String(capability))) {
    return compact([`证据强度 ${strengthLabel(entry.strength)}`, `${entry.numeric_facts?.row_count ?? payload.row_count ?? "n/a"} 行`]).join(" · ");
  }
  return compact([`证据强度 ${strengthLabel(entry.strength)}`, ...(entry.limitations ?? []).map(limitationLabel)]).join(" · ");
}

function evidenceSummary(evidence: JsonObject[]) {
  const pattern = evidence.find((entry) => ["pattern_scan", "compare_periods", "compare_period_phases", "rolling_window_compare", "weekday_calendar_compare", "event_window_compare"].includes(String(entry.capability_id ?? entry.capability)));
  if (!pattern) return `${evidence.length} 条证据路径`;
  return `模式证据 ${strengthLabel(pattern.strength)} · 中位提升 ${percent(pattern.median_uplift ?? pattern.numeric_facts?.median_uplift ?? pattern.typed_payload?.median_uplift)}`;
}

function evidenceBriefSummary(brief: JsonObject) {
  return compact([
    brief.pattern_status ? `证据状态 ${strengthLabel(brief.pattern_status)}` : "",
    brief.pattern_established === false ? "未支持主结论" : brief.pattern_established === true ? "支持主结论" : "",
    ...(brief.limitations ?? []).map(limitationLabel),
  ]).join(" · ") || "证据边界已整理";
}

function issueText(value: unknown) {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return String(value ?? "");
  const item = value as JsonObject;
  return item.issue_description ?? item.description ?? item.message ?? item.text ?? jsonInline(value);
}

function acceptedAssumptionsText(value: unknown) {
  if (Array.isArray(value) && value.length) return `接受假设：${value.join("；")}`;
  if (typeof value === "string" && value.trim()) return `接受假设：${value}`;
  return "";
}

function tool(label: string, status: string, detail: string, audit?: JsonObject) {
  return { label, status, detail, audit };
}

function mutationDetail(mutation: JsonObject) {
  return `${actionLabel(mutation.action)}：${reasonLabel(mutation.reason)}`;
}

function actionLabel(value: unknown) {
  return (
    {
      auto_added: "自动补充",
      accepted: "已接受",
      rejected: "已拒绝",
      degraded: "已降级",
    } as Record<string, string>
  )[String(value)] ?? String(value ?? "已记录");
}

function reasonLabel(value: unknown) {
  const text = String(value ?? "");
  const labels: Record<string, string> = {
    phase4_draft_binding: "草稿绑定口径",
    aggregate_only: "仅使用聚合数据",
    low_risk_assumption: "采用低风险推荐假设继续",
    clear: "无需追问",
    needs_question: "需要追问",
    cannot_answer: "当前无法回答",
    synthesize_answer: "进入答案生成",
    continue_evidence: "继续补充证据",
    degrade: "给出有边界的结论",
    blocked: "当前阻断",
    ok: "已通过",
    select_only: "只读 SELECT 已通过",
    aggregate_select_only: "聚合 SELECT 已通过",
  };
  if (labels[text]) return labels[text];
  if (text.startsWith("required_pattern_path:")) {
    return `${patternFamilyLabel(text.slice("required_pattern_path:".length))}必需证据路径`;
  }
  return text;
}

function routeLabel(value: unknown) {
  const text = String(value ?? "");
  if (!text) return "";
  return reasonLabel(text);
}

function statusLabel(value: unknown) {
  return (
    {
      passed: "已通过",
      degraded: "已降级",
      blocked: "已阻断",
      failed: "失败",
      needs_revision: "需要修订",
      fail: "失败",
      n_a: "无",
      "n/a": "无",
      unknown: "未知",
    } as Record<string, string>
  )[String(value)] ?? String(value ?? "未知");
}

function scopeLabel(value: unknown) {
  return (
    {
      full_sample: "全样本",
      all_users: "全体用户",
      default: "默认口径",
    } as Record<string, string>
  )[String(value)] ?? String(value ?? "当前口径");
}

function businessDisplayText(value: string) {
  return Object.entries({
    pattern_explanation: "模式解释",
    paid_amount: "付费金额",
    payment_amount: "付费金额",
    full_sample: "全样本",
    data_quality_check: "数据完整性检查",
    pattern_scan: "模式强度检验",
    answer_verify: "答案边界校验",
    formula_decompose: "指标构成检查",
    driver_decomposition: "贡献拆解",
    segment_contribution: "分群贡献拆解",
    outlier_contribution: "异常贡献拆解",
    event_evidence: "事件解释线索",
    segment_bridge: "分群一致性检查",
    outlier_scan: "异常周期检查",
    joint_attribution: "组合归因检查",
    synthesize_answer: "生成答案",
    coverage_status: "数据覆盖状态",
    clear: "清楚",
    sufficient: "充足",
    weekly: "周度模式",
    rolling: "滚动窗口模式",
    custom_baseline: "自定义基线",
    intra_period: "周期内模式",
    no_event_contract_or_matches: "未接入事件解释证据",
    below_materiality_floor: "未达到业务重要性阈值",
    weak_direction: "方向稳定性不足",
  }).reduce((text, [token, label]) => text.replace(new RegExp(`\\b${token}\\b`, "g"), label), value);
}

function patternFamilyLabel(value: string) {
  return (
    {
      weekly: "周度模式",
      rolling: "滚动窗口模式",
      custom_baseline: "自定义基线",
      intra_period: "周期内模式",
      event_relative: "事件相对窗口",
      lag_recovery: "滞后/恢复",
    } as Record<string, string>
  )[value] ?? value;
}

function limitationLabel(value: unknown) {
  return (
    {
      no_event_contract_or_matches: "未接入事件解释证据",
      below_materiality_floor: "未达到业务重要性阈值",
      weak_direction: "方向稳定性不足",
      missing_formula_component: "缺少公式组成项",
      sparse_cell: "分组样本稀疏",
      raw_identifier_present: "包含原始标识符，已受限",
      sample_size_unverified: "样本量未验证",
    } as Record<string, string>
  )[String(value)] ?? String(value ?? "证据限制");
}

function strengthLabel(value: unknown) {
  return (
    {
      high: "强",
      medium: "中",
      low: "弱",
      insufficient: "不足",
      unknown: "未知",
    } as Record<string, string>
  )[String(value)] ?? String(value ?? "未知");
}

function stat(label: string, value: string) {
  return { label, value };
}

function percent(value: unknown) {
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? `${(numberValue * 100).toFixed(1)}%` : "n/a";
}

function jsonInline(value: unknown) {
  return JSON.stringify(value);
}

function compact<T>(items: T[]) {
  return items.filter(Boolean);
}

function consumeCallForCheckpoint(remaining: { call: JsonObject; index: number }[], checkpoint: JsonObject) {
  const task = taskForNode(checkpoint.node);
  const index = remaining.findIndex((item) => item.call.task === task);
  if (index < 0) return undefined;
  return remaining.splice(index, 1)[0];
}

function checkpointsFor(checkpoints: JsonObject[], nodeNames: string[]) {
  const wanted = new Set(nodeNames);
  return checkpoints.filter((checkpoint) => wanted.has(checkpoint.node));
}

function eventDurationMs(value?: JsonObject) {
  const duration = Number(value?.durationMs ?? value?.duration_ms);
  return Number.isFinite(duration) && duration > 0 ? duration : 0;
}

function totalDurationMs(events: JsonObject[]) {
  return roundMs(events.reduce((total, event) => total + eventDurationMs(event), 0));
}

function firstStartedAt(events: JsonObject[]) {
  return events.find((event) => event.started_at)?.started_at ?? "";
}

function lastFinishedAt(events: JsonObject[]) {
  return [...events].reverse().find((event) => event.finished_at)?.finished_at ?? "";
}

function replayTiming(events: JsonObject[]) {
  const actualDurationMs = totalDurationMs(events);
  return {
    actualDurationMs,
    playbackDurationMs: actualDurationMs > 0 ? Math.min(10000, actualDurationMs) : 0,
  };
}

function roundMs(value: number) {
  return Math.round(value * 1000) / 1000;
}
