"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  History,
  LoaderCircle,
  Plus,
  X,
} from "lucide-react";

import {
  AnalysisTaskCard,
  PlannerStatusCard,
  QuestionCard,
} from "./analysis-dock";
import type {
  TraceReasoning,
  TraceReasoningClaim,
  TraceReasoningIssue,
} from "./run-reasoning-contracts";
import { ReasoningTimeline } from "./reasoning-timeline";
import {
  Conversation,
  ConversationContent,
  ConversationScrollButton,
} from "@/components/ai-elements/conversation";
import {
  Message,
  MessageContent,
  MessageResponse,
} from "@/components/ai-elements/message";
import {
  PromptInput,
  PromptInputFooter,
  PromptInputSubmit,
  PromptInputTextarea,
  PromptInputTools,
} from "@/components/ai-elements/prompt-input";

import {
  parseCustomerAnalysisSnapshot,
  parseCustomerApiError,
  parseCustomerThreadSummaries,
  type CustomerAnalysisSnapshot,
  type CustomerAnalysisState,
  type CustomerApiError,
  type CustomerMainStatus,
  type CustomerThreadSummary,
} from "./api/_customerAnalysisContract";

const ACTIVE_THREAD_KEY = "waje-active-thread:v2";
const PENDING_OPERATION_PREFIX = "waje-pending-operation:v4:";
const INITIAL_MESSAGE_SCOPE = "message:new";
const PENDING_OPERATION_TTL_MS = 24 * 60 * 60 * 1000;

type PendingOperation = {
  version: 4;
  operationId: string;
  scope: string;
  kind: "message" | "clarification" | "topic_choice" | "agent_action";
  threadHandle: string;
  actionHandle: string | null;
  message: string;
  optionKey: string | null;
  optionKeys: string[] | null;
  createdAt: string;
  expiresAt: string;
};

type ProgressConnection = "idle" | "connecting" | "live" | "reconnecting";

type CustomerObservationSet = {
  taskHandle: string;
  capability: string;
  status: "succeeded" | "unavailable";
  evidenceType: string | null;
  strength: string | null;
  wordingLimit: string | null;
  payload: Record<string, unknown>;
  resultRefs: string[];
  limitationRefs: string[];
};

type CustomerObservationResponse = {
  schemaVersion: "customer-capability-observations.v1";
  threadHandle: string;
  runHandle: string | null;
  observationSets: CustomerObservationSet[];
};

type VisibleError = CustomerApiError["error"] & {
  technicalDetailRef?: string;
};

class CustomerRequestError extends Error {
  readonly detail: VisibleError;

  constructor(payload: CustomerApiError) {
    super(payload.error.code);
    this.name = "CustomerRequestError";
    this.detail = {
      ...payload.error,
      technicalDetailRef: payload.transport.technicalDetailRef,
    };
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

async function responsePayload(response: Response) {
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new CustomerRequestError(parseCustomerApiError(payload));
  if (!isRecord(payload)) throw new Error("customer_response_invalid");
  return payload;
}

function snapshotFromPayload(payload: Record<string, unknown>) {
  return parseCustomerAnalysisSnapshot(payload.snapshot);
}

function loadStoredThreadHandle() {
  try {
    return window.localStorage.getItem(ACTIVE_THREAD_KEY)?.trim() ?? "";
  } catch {
    return "";
  }
}

function storeThreadHandle(threadHandle: string) {
  try {
    if (threadHandle) window.localStorage.setItem(ACTIVE_THREAD_KEY, threadHandle);
    else window.localStorage.removeItem(ACTIVE_THREAD_KEY);
  } catch {
    // Browser storage can be unavailable. The server snapshot remains authoritative.
  }
}

function pendingStorageKey(scope: string) {
  return `${PENDING_OPERATION_PREFIX}${scope}`;
}

function loadPendingOperation(scope: string): PendingOperation | null {
  try {
    const key = pendingStorageKey(scope);
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    const value: unknown = JSON.parse(raw);
    if (
      !isRecord(value)
      || value.version !== 4
      || typeof value.operationId !== "string"
      || typeof value.scope !== "string"
      || value.scope !== scope
      || !["message", "clarification", "topic_choice", "agent_action"].includes(String(value.kind))
      || typeof value.threadHandle !== "string"
      || typeof value.message !== "string"
      || (
        value.optionKeys !== null
        && (
          !Array.isArray(value.optionKeys)
          || value.optionKeys.some((item) => typeof item !== "string")
        )
      )
      || typeof value.createdAt !== "string"
      || typeof value.expiresAt !== "string"
      || Number.isNaN(Date.parse(value.createdAt))
      || Number.isNaN(Date.parse(value.expiresAt))
      || Date.parse(value.expiresAt) <= Date.now()
    ) {
      window.localStorage.removeItem(key);
      return null;
    }
    return value as PendingOperation;
  } catch {
    return null;
  }
}

function storePendingOperation(operation: PendingOperation) {
  try {
    window.localStorage.setItem(
      pendingStorageKey(operation.scope),
      JSON.stringify(operation),
    );
    return true;
  } catch {
    // The operation remains stable in memory for the current page lifetime.
    return false;
  }
}

function removePendingOperation(operation: PendingOperation) {
  try {
    window.localStorage.removeItem(pendingStorageKey(operation.scope));
  } catch {
    // The stale key will be reconciled against the next authoritative snapshot.
  }
}

function removePendingOperationCopies(
  operation: PendingOperation,
  threadHandle?: string,
) {
  removePendingOperation(operation);
  for (const scope of [
    INITIAL_MESSAGE_SCOPE,
    threadHandle ? `message:${threadHandle}` : "",
  ]) {
    if (!scope || scope === operation.scope) continue;
    const copy = loadPendingOperation(scope);
    if (copy?.operationId === operation.operationId) removePendingOperation(copy);
  }
}

function operationScope(
  kind: PendingOperation["kind"],
  threadHandle: string,
  actionHandle: string | null,
) {
  return kind === "message"
    ? threadHandle ? `message:${threadHandle}` : INITIAL_MESSAGE_SCOPE
    : `${kind}:${actionHandle ?? "missing"}`;
}

function stableOperation(input: Omit<
  PendingOperation,
  "version" | "operationId" | "scope" | "createdAt" | "expiresAt"
>) {
  const scope = operationScope(input.kind, input.threadHandle, input.actionHandle);
  const existing = loadPendingOperation(scope);
  if (existing) return existing;
  const createdAt = new Date();
  const operation: PendingOperation = {
    version: 4,
    operationId: crypto.randomUUID(),
    scope,
    ...input,
    createdAt: createdAt.toISOString(),
    expiresAt: new Date(createdAt.getTime() + PENDING_OPERATION_TTL_MS).toISOString(),
  };
  storePendingOperation(operation);
  return operation;
}

function stateLabel(state: { status: CustomerMainStatus } | null) {
  if (!state) return "准备提问";
  return {
    idle: "准备提问",
    working: "分析进行中",
    needs_input: "等待你的确认",
    checkpoint: "阶段完成",
    completed: "分析完成",
    completed_with_limits: "分析完成 · 有边界",
    failed: "分析未完成",
  }[state.status];
}

function formatThreadUpdatedAt(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function isNewerSnapshot(
  current: CustomerAnalysisSnapshot | null,
  next: CustomerAnalysisSnapshot,
) {
  if (!current) return true;
  if (current.transport.threadHandle !== next.transport.threadHandle) return true;
  const currentCursor = current.transport.eventCursor;
  const nextCursor = next.transport.eventCursor;
  try {
    if (BigInt(nextCursor) !== BigInt(currentCursor)) {
      return BigInt(nextCursor) > BigInt(currentCursor);
    }
    return BigInt(next.stateVersion) > BigInt(current.stateVersion);
  } catch {
    if (nextCursor !== currentCursor) return nextCursor > currentCursor;
    return next.stateVersion > current.stateVersion;
  }
}

function publicConnectionError(): VisibleError {
  return {
    code: "analysis_unavailable",
    title: "暂时无法确认最新状态",
    message: "页面会保留上次已确认状态。请检查网络后重试。",
    recovery: "retry",
  };
}

function operationWasDefinitelyRejected(caught: unknown) {
  return caught instanceof CustomerRequestError && [
    "request_invalid",
    "action_no_longer_available",
    "analysis_not_found",
    "sign_in_required",
  ].includes(caught.detail.code);
}

function AnswerMessage({ state, threadHandle, reasoning }: {
  state: Extract<CustomerAnalysisState, {
  status: "completed" | "completed_with_limits";
  }>;
  threadHandle: string;
  reasoning: TraceReasoning | null;
}) {
  const mainBlocks = state.answer.blocks.filter((block) =>
    ["summary", "finding", "context"].includes(block.kind)
  );
  const limitations = state.answer.blocks.filter((block) => block.kind === "limitation");
  const recommendations = state.answer.blocks.filter(
    (block) => block.kind === "recommendation",
  );
  return (
    <Message className="business-reference" from="assistant">
      <MessageContent>
        <article aria-label="分析回答">
          <PlannerQuestionReview reasoning={reasoning} />
          <h3 className="answer-synthesis-title">综合结论与发现</h3>
          <div className="answer-body">
            {mainBlocks.map((block) => (
              <section
                aria-labelledby={block.heading ? `${block.key}-heading` : undefined}
                className={`answer-section ${block.kind}`}
                key={block.key}
              >
                {block.heading ? (
                  <h3 className="answer-section-heading" id={`${block.key}-heading`}>
                    {block.heading}
                  </h3>
                ) : null}
                <MessageResponse className="answer-section-copy">
                  {block.text}
                </MessageResponse>
                <AnswerClaimRefs
                  blockId={block.key}
                  blockText={block.text}
                  reasoning={reasoning}
                />
              </section>
            ))}
          </div>
          {recommendations.length ? (
            <section className="answer-next-actions">
              <h3>{recommendations[0].heading ?? "运营建议"}</h3>
              {recommendations.map((block) => (
                <MessageResponse key={block.key}>{block.text}</MessageResponse>
              ))}
            </section>
          ) : null}
          <details className="answer-boundaries" open={state.status === "completed_with_limits"}>
            <summary>证据与限制</summary>
            <p className="answer-boundary-stats">
              结论依据 {state.answer.evidenceCount} 项 · 已声明限制 {state.answer.limitationCount} 项
            </p>
            {limitations.length ? limitations.map((block) => (
              <MessageResponse key={block.key}>{block.text}</MessageResponse>
            )) : <p>当前业务参考未声明额外限制。</p>}
            {state.answer.warnings.map((warning, index) => (
              <p className="answer-warning" key={`warning-${index}`}>{warning}</p>
            ))}
          </details>
          <CustomerObservations threadHandle={threadHandle} />
        </article>
      </MessageContent>
    </Message>
  );
}

function PlannerQuestionReview({
  reasoning,
}: {
  reasoning: TraceReasoning | null;
}) {
  if (!reasoning?.issues.length) return null;
  return (
    <section
      aria-labelledby="planner-question-review-title"
      className="planner-question-review"
    >
      <h3 id="planner-question-review-title">逐题回答</h3>
      <ol>
        {reasoning.issues.map((issue, index) => (
          <li
            className={issue.parentIssueId ? "supporting" : "primary"}
            key={issue.issueId}
          >
            <header>
              <span>{index + 1}</span>
              <strong>{issue.question}</strong>
              <small className={issue.status}>
                {reviewStatusLabel(issue.status)}
              </small>
            </header>
            <p>
              {issue.answerText ?? reviewFallbackText(issue)}
            </p>
            <InlineEvidence
              claimRefs={
                issue.usedClaimRefs.length
                  ? issue.usedClaimRefs
                  : issue.claimRefs
              }
              reasoning={reasoning}
            />
          </li>
        ))}
      </ol>
    </section>
  );
}

function reviewStatusLabel(status: TraceReasoningIssue["status"]) {
  return ({
    answered: "已回答",
    partial: "部分回答",
    omitted: "有事实，未作答",
    unresolved: "本次未解决",
  } as const)[status];
}

function reviewFallbackText(issue: TraceReasoningIssue) {
  if (issue.status === "omitted") {
    return "本次形成了相关事实，但它没有进入最终回答；当前不把这条事实当作这个问题的直接答案。";
  }
  return "本次没有形成足以回答这个问题的已核验事实。";
}

function AnswerClaimRefs({
  blockId,
  blockText,
  reasoning,
}: {
  blockId: string;
  blockText: string;
  reasoning: TraceReasoning | null;
}) {
  const answerBlock = reasoning?.answerBlocks.find(
    (block) => block.blockId === blockId || block.text === blockText,
  );
  const claimRefs = answerBlock?.claimRefs ?? [];
  if (!claimRefs.length || !reasoning) return null;
  return <InlineEvidence claimRefs={claimRefs} reasoning={reasoning} />;
}

function InlineEvidence({
  claimRefs,
  reasoning,
}: {
  claimRefs: string[];
  reasoning: TraceReasoning;
}) {
  const facts = claimRefs.flatMap((claimRef) => {
    const fact = reasoning.claims.find((claim) => claim.claimRef === claimRef);
    return fact ? [fact] : [];
  });
  if (!facts.length) return null;
  return (
    <details className="answer-inline-evidence">
      <summary>查看依据 · {facts.length} 条已核验事实</summary>
      <div>
        {facts.map((fact) => (
          <EvidenceFact fact={fact} key={fact.claimRef} />
        ))}
      </div>
    </details>
  );
}

function EvidenceFact({ fact }: { fact: TraceReasoningClaim }) {
  return (
    <article>
      <strong>{fact.summary}</strong>
      {fact.facts.length ? (
        <dl>
          {fact.facts.map((item) => (
            <div key={`${item.name}:${item.value}`}>
              <dt>{item.name}</dt>
              <dd>{item.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {fact.limitationRefs.length ? (
        <small>这条事实带有 {fact.limitationRefs.length} 项适用边界。</small>
      ) : null}
    </article>
  );
}

function CustomerObservations({ threadHandle }: { threadHandle: string }) {
  const [opened, setOpened] = useState(false);
  const [result, setResult] = useState<CustomerObservationResponse | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!opened || result || failed) return;
    const controller = new AbortController();
    void fetch(`/api/threads/${encodeURIComponent(threadHandle)}/observations`, {
      cache: "no-store",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error("customer_observations_unavailable");
        return parseCustomerObservationResponse(await response.json(), threadHandle);
      })
      .then(setResult)
      .catch((error) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setFailed(true);
      });
    return () => controller.abort();
  }, [failed, opened, result, threadHandle]);

  return (
    <details
      className="answer-observations"
      onToggle={(event) => setOpened(event.currentTarget.open)}
    >
      <summary>完整观察</summary>
      <p className="answer-observation-note">
        展示本次能力任务产生的全部聚合观察；结论核验使用其中的有界摘要。
      </p>
      {!result && !failed ? <p>正在读取完整观察…</p> : null}
      {failed ? <p className="answer-warning">完整观察暂时无法读取，请稍后重试。</p> : null}
      {result?.observationSets.length === 0 ? (
        <p>本次运行没有可展示的聚合观察。</p>
      ) : null}
      {result?.observationSets.map((set) => (
        <details className="answer-observation-set" key={set.taskHandle}>
          <summary>
            <strong>{capabilityObservationLabel(set.capability)}</strong>
            <span>
              {set.status === "succeeded" ? "已完成" : "信息不足"}
              {set.evidenceType ? ` · ${set.evidenceType}` : ""}
            </span>
          </summary>
          <ObservationValue value={set.payload} />
          {set.resultRefs.length || set.limitationRefs.length ? (
            <details className="answer-observation-authority">
              <summary>结果引用与适用边界</summary>
              {set.resultRefs.length ? (
                <p className="answer-observation-refs">
                  结果引用：{set.resultRefs.join("；")}
                </p>
              ) : null}
              {set.limitationRefs.length ? (
                <p className="answer-observation-refs">
                  适用边界：{set.limitationRefs.join("；")}
                </p>
              ) : null}
            </details>
          ) : null}
        </details>
      ))}
    </details>
  );
}

function ObservationValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span>未记录</span>;
  if (typeof value === "boolean") return <span>{value ? "是" : "否"}</span>;
  if (typeof value === "string") {
    return <span>{observationValueLabel(value)}</span>;
  }
  if (typeof value === "number") {
    return <span>{String(value)}</span>;
  }
  if (Array.isArray(value)) {
    if (!value.length) return <span>无</span>;
    return value.length > 8
      ? <ObservationCollection values={value} />
      : (
        <ol className="answer-observation-list">
          {value.map((item, index) => (
            <li key={index}><ObservationValue value={item} /></li>
          ))}
        </ol>
      );
  }
  if (typeof value === "object") {
    const entries = Object.entries(value);
    if (
      entries.length === 1
      && ["$decimal", "$date", "$datetime"].includes(entries[0][0])
    ) {
      return <span>{String(entries[0][1])}</span>;
    }
    return (
      <dl className="answer-observation-fields">
        {entries.map(([key, item]) => (
          <div key={key}>
            <dt title={key}>{observationFieldLabel(key)}</dt>
            <dd><ObservationValue value={item} /></dd>
          </div>
        ))}
      </dl>
    );
  }
  return <span>{String(value)}</span>;
}

function ObservationCollection({ values }: { values: unknown[] }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="answer-observation-collection"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>共 {values.length} 项 · 展开查看全部</summary>
      {open ? (
        <ol className="answer-observation-list">
          {values.map((item, index) => (
            <li key={index}><ObservationValue value={item} /></li>
          ))}
        </ol>
      ) : null}
    </details>
  );
}

function parseCustomerObservationResponse(
  value: unknown,
  threadHandle: string,
): CustomerObservationResponse {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("customer_observations_invalid");
  }
  const record = value as Record<string, unknown>;
  if (
    record.schemaVersion !== "customer-capability-observations.v1"
    || record.threadHandle !== threadHandle
    || (record.runHandle !== null && typeof record.runHandle !== "string")
    || !Array.isArray(record.observationSets)
  ) {
    throw new Error("customer_observations_invalid");
  }
  for (const item of record.observationSets) {
    if (
      !item
      || typeof item !== "object"
      || Array.isArray(item)
      || !["succeeded", "unavailable"].includes(
        String((item as Record<string, unknown>).status),
      )
      || typeof (item as Record<string, unknown>).taskHandle !== "string"
      || typeof (item as Record<string, unknown>).capability !== "string"
      || !(item as Record<string, unknown>).payload
      || typeof (item as Record<string, unknown>).payload !== "object"
      || Array.isArray((item as Record<string, unknown>).payload)
      || !Array.isArray((item as Record<string, unknown>).resultRefs)
      || !Array.isArray((item as Record<string, unknown>).limitationRefs)
    ) {
      throw new Error("customer_observations_invalid");
    }
  }
  return record as CustomerObservationResponse;
}

function capabilityObservationLabel(capability: string) {
  return ({
    compare_periods: "月初、月中、月末比较",
    compare_period_phases: "月内阶段模式",
    candidate_dimension_screen: "候选维度观察",
    joint_attribution: "联合维度验证",
    event_evidence: "活动与事件观察",
    event_window_compare: "活动前后付费对比",
    internal_operation_event_window_compare: "运营活动前后付费对比",
    internal_operation_event_evidence: "内部运营活动观察",
    metric_timeseries: "每日付费金额序列",
    market_channel_context: "市场渠道背景",
    source_reconciliation: "来源对账",
    data_quality_profile: "数据质量检查",
    metric_coverage_profile: "数据覆盖检查",
    high_value_user_contribution: "高价值付费用户贡献",
  } as Record<string, string>)[capability] ?? capability;
}

function observationFieldLabel(field: string) {
  return ({
    numeric_facts: "汇总数值",
    typed_payload: "完整业务观察",
    evidence_type: "证据类型",
    strength: "证据强度",
    wording_limit: "结论措辞边界",
    observation_reconciliations: "全部对账观察",
    events: "全部活动",
    event_summary: "活动汇总",
    comparisons: "全部活动前后对比",
    dimension_profiles: "全部维度观察",
    business_readouts: "业务读数",
    joint_exploration_candidates: "联合分析候选",
    claim_material_observations: "结论核验摘要",
    interpretation_contract: "解释边界",
    business_readout: "业务说明",
    claim_boundary: "结论边界",
    high_value_paid_users: "高价值付费用户",
    high_value_paid_users_measure: "用户数口径",
  } as Record<string, string>)[field] ?? field.replaceAll("_", " ");
}

function observationValueLabel(value: string) {
  return ({
    average_users_per_complete_day: "完整天日均用户数",
    distinct_users_in_window: "窗口内去重用户数",
  } as Record<string, string>)[value] ?? value;
}

export default function Home() {
  const [snapshot, setSnapshot] = useState<CustomerAnalysisSnapshot | null>(null);
  const [reasoning, setReasoning] = useState<TraceReasoning | null>(null);
  const [reasoningLoading, setReasoningLoading] = useState(false);
  const [reasoningError, setReasoningError] = useState("");
  const [threads, setThreads] = useState<CustomerThreadSummary[]>([]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<PendingOperation | null>(null);
  const [error, setError] = useState<VisibleError | null>(null);
  const [loading, setLoading] = useState(true);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [connection, setConnection] = useState<ProgressConnection>("idle");
  const eventSourceRef = useRef<EventSource | null>(null);
  const snapshotRef = useRef<CustomerAnalysisSnapshot | null>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const inputRequestRef = useRef<HTMLDivElement>(null);
  const historyToggleRef = useRef<HTMLButtonElement>(null);
  const currentThreadRef = useRef("");
  const reasoningRunRef = useRef("");
  const activeExecutionIdsRef = useRef(new Set<string>());

  const reconcileOperation = useCallback((next: CustomerAnalysisSnapshot) => {
    setPending((current) => {
      const restored = current ?? [
        loadPendingOperation(INITIAL_MESSAGE_SCOPE),
        loadPendingOperation(`message:${next.transport.threadHandle}`),
        next.transport.runHandle
          ? loadPendingOperation(`clarification:${next.transport.runHandle}`)
          : null,
        next.transport.runHandle
          ? loadPendingOperation(`topic_choice:${next.transport.runHandle}`)
          : null,
        next.transport.actionKind === "agent_pending_action" && next.transport.actionHandle
          ? loadPendingOperation(`agent_action:${next.transport.actionHandle}`)
          : null,
      ].find((operation): operation is PendingOperation => Boolean(operation)) ?? null;
      if (!restored) return null;
      if (next.transport.acceptedOperationIds.includes(restored.operationId)) {
        removePendingOperationCopies(restored, next.transport.threadHandle);
        return null;
      }
      const actionStillCurrent = restored.kind === "message"
        || (next.state.status === "needs_input"
          && next.transport.actionHandle === restored.actionHandle);
      if (!actionStillCurrent) {
        removePendingOperation(restored);
        return null;
      }
      return restored;
    });
  }, []);

  const acceptSnapshot = useCallback((next: CustomerAnalysisSnapshot) => {
    if (!isNewerSnapshot(snapshotRef.current, next)) return;
    snapshotRef.current = next;
    setSnapshot(next);
    currentThreadRef.current = next.transport.threadHandle;
    storeThreadHandle(next.transport.threadHandle);
    reconcileOperation(next);
    setThreads((current) => {
      const replacement: CustomerThreadSummary = {
        title: next.thread.title,
        status: next.state.status,
        updatedAt: next.confirmedAt,
        transport: { threadHandle: next.transport.threadHandle },
      };
      return [replacement, ...current.filter((thread) =>
        thread.transport.threadHandle !== next.transport.threadHandle
      )];
    });
  }, [reconcileOperation]);

  const refreshSnapshot = useCallback(async (threadHandle?: string) => {
    const active = threadHandle ?? currentThreadRef.current;
    if (!active) return null;
    const response = await fetch(`/api/threads/${encodeURIComponent(active)}`, {
      cache: "no-store",
    });
    const next = snapshotFromPayload(await responsePayload(response));
    acceptSnapshot(next);
    return next;
  }, [acceptSnapshot]);

  const showRequestError = useCallback((caught: unknown) => {
    setError(caught instanceof CustomerRequestError
      ? caught.detail
      : publicConnectionError());
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function restore() {
      try {
        const initialPending = loadPendingOperation(INITIAL_MESSAGE_SCOPE);
        if (initialPending) setPending(initialPending);
        const response = await fetch("/api/threads", { cache: "no-store" });
        const payload = await responsePayload(response);
        const summaries = parseCustomerThreadSummaries(payload);
        if (cancelled) return;
        setThreads(summaries);
        const stored = loadStoredThreadHandle();
        const active = summaries.find((thread) =>
          thread.transport.threadHandle === stored
        )?.transport.threadHandle ?? summaries[0]?.transport.threadHandle;
        if (active) await refreshSnapshot(active);
      } catch (caught) {
        if (!cancelled) showRequestError(caught);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void restore();
    return () => { cancelled = true; };
  }, [refreshSnapshot, showRequestError]);

  useEffect(() => {
    const runHandle = snapshot?.transport.runHandle;
    const terminal = snapshot?.state.status === "completed"
      || snapshot?.state.status === "completed_with_limits"
      || snapshot?.state.status === "failed";
    if (!runHandle) {
      reasoningRunRef.current = "";
      setReasoning(null);
      setReasoningError("");
      setReasoningLoading(false);
      return;
    }
    const firstLoadForRun = reasoningRunRef.current !== runHandle;
    if (firstLoadForRun) {
      reasoningRunRef.current = runHandle;
      setReasoning(null);
      setReasoningError("");
      setReasoningLoading(true);
    }
    const controller = new AbortController();
    const loadReasoning = async () => {
      try {
        const response = await fetch(
          `/api/agent-runs/${encodeURIComponent(runHandle)}/reasoning`,
          { cache: "no-store", signal: controller.signal },
        );
        if (response.status === 404 && !terminal) return;
        if (!response.ok) throw new Error(`run_reasoning_api_${response.status}`);
        const payload = await response.json() as {
          reasoning: TraceReasoning | null;
        };
        setReasoning(payload.reasoning);
        setReasoningError("");
      } catch (caught) {
        if (controller.signal.aborted || !terminal) return;
        setReasoningError(
          caught instanceof Error ? caught.message : "run_reasoning_api_failed",
        );
      } finally {
        if (!controller.signal.aborted) setReasoningLoading(false);
      }
    };
    void loadReasoning();
    const interval = terminal
      ? null
      : window.setInterval(() => void loadReasoning(), 5_000);
    return () => {
      controller.abort();
      if (interval !== null) window.clearInterval(interval);
    };
  }, [
    snapshot?.state.status,
    snapshot?.stateVersion,
    snapshot?.transport.runHandle,
  ]);

  useEffect(() => {
    eventSourceRef.current?.close();
    const eventsUrl = snapshot?.transport.eventsUrl;
    if (!eventsUrl || [
      "checkpoint",
      "completed",
      "completed_with_limits",
      "failed",
    ].includes(
      snapshot.state.status,
    )) {
      setConnection("idle");
      return;
    }
    const expectedThreadHandle = snapshot.transport.threadHandle;
    setConnection("connecting");
    const source = new EventSource(eventsUrl);
    eventSourceRef.current = source;
    source.onopen = () => setConnection("live");
    source.onerror = () => setConnection("reconnecting");
    const onState = (message: MessageEvent) => {
      try {
        const payload: unknown = JSON.parse(message.data);
        if (!isRecord(payload)) throw new Error("customer_event_invalid");
        const next = snapshotFromPayload(payload);
        if (next.transport.threadHandle !== expectedThreadHandle) return;
        acceptSnapshot(next);
        setConnection("live");
        setError(null);
      } catch {
        setError({
          code: "analysis_unavailable",
          title: "状态更新无法读取",
          message: "页面已保留上次确认状态。刷新后会重新从服务器恢复。",
          recovery: "refresh",
        });
        setConnection("reconnecting");
        source.close();
      }
    };
    source.addEventListener("customer_state_changed", onState as EventListener);
    return () => {
      source.removeEventListener("customer_state_changed", onState as EventListener);
      source.close();
    };
  }, [acceptSnapshot, snapshot?.state.status, snapshot?.transport.eventsUrl]);

  useEffect(() => {
    const refresh = () => {
      if (currentThreadRef.current) void refreshSnapshot().catch(showRequestError);
    };
    window.addEventListener("focus", refresh);
    window.addEventListener("online", refresh);
    const storage = (event: StorageEvent) => {
      if (event.key?.startsWith(PENDING_OPERATION_PREFIX)) refresh();
    };
    window.addEventListener("storage", storage);
    return () => {
      window.removeEventListener("focus", refresh);
      window.removeEventListener("online", refresh);
      window.removeEventListener("storage", storage);
    };
  }, [refreshSnapshot, showRequestError]);

  useEffect(() => {
    if (snapshot?.state.status === "needs_input") inputRequestRef.current?.focus();
  }, [snapshot?.state.status, snapshot?.stateVersion]);

  useEffect(() => {
    if (!historyOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setHistoryOpen(false);
      requestAnimationFrame(() => historyToggleRef.current?.focus());
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [historyOpen]);

  const executeOperation = useCallback(async (operation: PendingOperation) => {
    if (activeExecutionIdsRef.current.has(operation.operationId)) return;
    activeExecutionIdsRef.current.add(operation.operationId);
    let currentOperation = operation;
    setPending(currentOperation);
    setError(null);
    try {
      if (currentOperation.kind === "message" && !currentOperation.threadHandle) {
        const claimResponse = await fetch("/api/threads", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": currentOperation.operationId,
          },
          body: JSON.stringify({ requestIdentity: currentOperation.operationId }),
        });
        const claimed = snapshotFromPayload(await responsePayload(claimResponse));
        acceptSnapshot(claimed);
        const initialOperation = currentOperation;
        const claimedOperation = {
          ...currentOperation,
          scope: operationScope("message", claimed.transport.threadHandle, null),
          threadHandle: claimed.transport.threadHandle,
        };
        if (storePendingOperation(claimedOperation)) {
          removePendingOperation(initialOperation);
        }
        currentOperation = claimedOperation;
        setPending(currentOperation);
      }

      const body: Record<string, unknown> = {
        message: currentOperation.message,
        requestIdentity: currentOperation.operationId,
      };
      let url = `/api/threads/${encodeURIComponent(
        currentOperation.threadHandle,
      )}/messages`;
      if (currentOperation.kind === "clarification") {
        url = `/api/runs/${encodeURIComponent(
          currentOperation.actionHandle ?? "",
        )}/clarifications`;
        delete body.message;
        body.answer = currentOperation.message;
        body.selectedOptionIds = currentOperation.optionKeys ?? [];
      } else if (currentOperation.kind === "agent_action") {
        const decision = currentOperation.optionKey === "approved"
          || currentOperation.optionKey === "rejected"
          ? currentOperation.optionKey
          : "answered";
        body.pendingActionResolution = {
          actionRef: currentOperation.actionHandle,
          decision,
          selectedOptionId: decision === "answered" ? currentOperation.optionKey : null,
          answerText: currentOperation.message,
        };
      } else if (currentOperation.kind === "topic_choice") {
        if (currentOperation.optionKey) {
          body.topicSelection = {
            sourceRunId: currentOperation.actionHandle,
            topicId: currentOperation.optionKey,
          };
        } else {
          body.topicChoiceAnswer = {
            sourceRunId: currentOperation.actionHandle,
            answer: currentOperation.message,
          };
        }
      }
      const response = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": currentOperation.operationId,
        },
        body: JSON.stringify(body),
      });
      const next = snapshotFromPayload(await responsePayload(response));
      acceptSnapshot(next);
      if (next.transport.acceptedOperationIds.includes(currentOperation.operationId)) {
        removePendingOperationCopies(currentOperation, next.transport.threadHandle);
        setPending(null);
      }
      setDraft("");
    } catch (caught) {
      try {
        const recovered = currentOperation.threadHandle
          ? await refreshSnapshot(currentOperation.threadHandle)
          : null;
        if (recovered?.transport.acceptedOperationIds.includes(
          currentOperation.operationId,
        )) {
          removePendingOperationCopies(
            currentOperation,
            recovered.transport.threadHandle,
          );
          setPending(null);
          setError(null);
          setDraft("");
          return;
        }
      } catch {
        // Preserve the original actionable error and the stable operation identity.
      }
      if (operationWasDefinitelyRejected(caught)) {
        removePendingOperationCopies(currentOperation, currentOperation.threadHandle);
        setPending(null);
        if (currentOperation.kind === "message") setDraft(currentOperation.message);
      }
      showRequestError(caught);
    } finally {
      activeExecutionIdsRef.current.delete(operation.operationId);
    }
  }, [acceptSnapshot, refreshSnapshot, showRequestError]);

  useEffect(() => {
    if (!loading && pending) void executeOperation(pending);
  }, [executeOperation, loading, pending]);

  async function sendQuestion(input: string) {
    const message = input.trim();
    if (!message || loading || pending || snapshot?.state.status === "working"
      || snapshot?.state.status === "needs_input") return;
    const operation = stableOperation({
      kind: "message",
      threadHandle: snapshot?.transport.threadHandle ?? "",
      actionHandle: null,
      message,
      optionKey: null,
      optionKeys: null,
    });
    setDraft("");
    await executeOperation(operation);
  }

  function submitInput(answer: string, optionKeys?: string[]) {
    if (!snapshot || snapshot.state.status !== "needs_input" || pending) return;
    const operation = stableOperation({
      kind: snapshot.transport.actionKind === "agent_pending_action"
        ? "agent_action"
        : snapshot.state.input.kind === "clarification"
          ? "clarification"
          : "topic_choice",
      threadHandle: snapshot.transport.threadHandle,
      actionHandle: snapshot.transport.actionHandle,
      message: answer,
      optionKey: optionKeys?.[0] ?? null,
      optionKeys: optionKeys ?? null,
    });
    setDraft("");
    void executeOperation(operation);
  }

  function newAnalysis() {
    if (pending) return;
    eventSourceRef.current?.close();
    currentThreadRef.current = "";
    snapshotRef.current = null;
    storeThreadHandle("");
    setSnapshot(null);
    setPending(null);
    setError(null);
    setHistoryOpen(false);
    setConnection("idle");
    setDraft("");
    requestAnimationFrame(() => composerRef.current?.focus());
  }

  async function openThread(threadHandle: string) {
    if (pending) return;
    setLoading(true);
    setError(null);
    setHistoryOpen(false);
    try {
      await refreshSnapshot(threadHandle);
    } catch (caught) {
      showRequestError(caught);
    } finally {
      setLoading(false);
    }
  }

  const customerState = snapshot?.state ?? null;
  const answerComplete = customerState?.status === "completed"
    || customerState?.status === "completed_with_limits";
  const composerBlocked = Boolean(
    loading
    || pending
    || customerState?.status === "working"
    || customerState?.status === "needs_input",
  );
  const showComposer = !loading
    && !(error && !snapshot)
    && !error?.code.startsWith("sign_in");
  const pendingMessageVisible = pending?.kind === "message"
    && !snapshot?.transport.acceptedOperationIds.includes(pending.operationId);
  const signInUrl = process.env.NEXT_PUBLIC_WAJE_SIGN_IN_URL?.trim() ?? "";
  const supportUrl = process.env.NEXT_PUBLIC_WAJE_SUPPORT_URL?.trim() ?? "";

  function retryLatest() {
    if (pending) {
      void executeOperation(pending);
    } else if (currentThreadRef.current) {
      void refreshSnapshot().catch(showRequestError);
    } else {
      window.location.reload();
    }
  }

  return (
    <main className="app-shell customer-app dark">
      <aside className="thread-sidebar" aria-label="分析历史">
        <div className="sidebar-heading">
          <div className="brand">WAJE BI</div>
          <button
            aria-controls="analysis-history"
            aria-expanded={historyOpen}
            aria-label={historyOpen ? "关闭分析历史" : "打开分析历史"}
            className="history-toggle"
            onClick={() => setHistoryOpen((current) => !current)}
            ref={historyToggleRef}
            type="button"
          >
            {historyOpen ? <X size={17} /> : <History size={17} />}
          </button>
        </div>
        <button
          className="new-thread"
          disabled={Boolean(pending)}
          onClick={newAnalysis}
          type="button"
        >
          <Plus size={15} /> 新分析
        </button>
        <nav
          aria-label="最近分析"
          className={historyOpen ? "open" : ""}
          id="analysis-history"
        >
          {threads.map((thread) => {
            const handle = thread.transport.threadHandle;
            return (
              <button
                className={handle === snapshot?.transport.threadHandle ? "active" : ""}
                disabled={Boolean(pending)}
                key={handle}
                onClick={() => void openThread(handle)}
                type="button"
              >
                <strong>{thread.title}</strong>
                <span className="thread-meta">
                  <small>{stateLabel(thread)}</small>
                  <time dateTime={thread.updatedAt}>
                    {formatThreadUpdatedAt(thread.updatedAt)}
                  </time>
                </span>
              </button>
            );
          })}
        </nav>
      </aside>

      {historyOpen ? (
        <button
          aria-label="关闭分析历史"
          className="history-backdrop"
          onClick={() => setHistoryOpen(false)}
          type="button"
        />
      ) : null}

      <section className="chat-shell">
        <header className="chat-header">
          <div>
            <h1>{snapshot?.thread.title ?? "业务分析"}</h1>
            <span
              aria-atomic="true"
              aria-live="polite"
              className={`customer-status ${customerState?.status ?? "idle"}`}
            >
              {loading ? "正在恢复状态" : stateLabel(customerState)}
            </span>
          </div>
        </header>

        {snapshot ? (
          <PlannerStatusCard
            pending={Boolean(pending)}
            reasoningIssues={reasoning?.issues}
            snapshot={snapshot}
          />
        ) : null}

        <Conversation
          className="message-list"
          initial={answerComplete ? false : "smooth"}
          key={`${snapshot?.transport.threadHandle ?? "new"}:${answerComplete ? "answer" : "live"}`}
        >
          <ConversationContent className="customer-conversation-content">
            {loading && !snapshot ? (
              <section aria-live="polite" className="customer-restoring">
                <LoaderCircle aria-hidden="true" className="progress-spinner" size={18} />
                <div>
                  <strong>正在恢复最近一次分析</strong>
                  <p>页面会从服务器读取已确认状态。</p>
                </div>
              </section>
            ) : !snapshot?.messages.length && !pending ? (
              <section className="customer-empty">
                <span className="eyebrow">从一个业务问题开始</span>
                <h1>把问题交给 WAJE BI</h1>
                <p>分析会持续保存进展。只有会明显改变结论、范围或成本时，才会请你确认。</p>
              </section>
            ) : null}

            {snapshot?.messages.map((message) => (
              <Message from={message.role} key={message.key}>
                <MessageContent className={message.role === "user"
                  ? "customer-user-content"
                  : "customer-assistant-content"}>
                  {message.role === "assistant"
                    ? <MessageResponse>{message.text}</MessageResponse>
                    : message.text}
                </MessageContent>
              </Message>
            ))}

            {snapshot?.businessUnderstanding ? (
              <Message from="assistant" key={`understanding:${snapshot.transport.runHandle ?? snapshot.stateVersion}`}>
                <MessageContent className="customer-assistant-content customer-business-understanding">
                  <p>
                    <strong>我的理解：</strong>
                    {snapshot.businessUnderstanding}
                  </p>
                </MessageContent>
              </Message>
            ) : null}

            {reasoningLoading ? (
              <Message from="assistant">
                <MessageContent className="customer-assistant-content">
                  正在读取这条历史记录的完整分析过程…
                </MessageContent>
              </Message>
            ) : null}

            {reasoningError ? (
              <Message className="error-message" from="assistant">
                <MessageContent>
                  <p>分析过程读取失败：{reasoningError}</p>
                </MessageContent>
              </Message>
            ) : null}

            {reasoning ? (
              <Message className="customer-reasoning-message" from="assistant">
                <MessageContent className="customer-reasoning-content">
                  <ReasoningTimeline reasoning={reasoning} />
                </MessageContent>
              </Message>
            ) : null}

            {pendingMessageVisible ? (
              <Message className="pending-user-message" from="user">
                <MessageContent className="customer-user-content">
                  {pending.message}
                  <small>正在确认提交</small>
                </MessageContent>
              </Message>
            ) : null}

            {snapshot?.state.status === "completed"
              || snapshot?.state.status === "completed_with_limits" ? (
                <AnswerMessage
                  reasoning={reasoning}
                  state={snapshot.state}
                  threadHandle={snapshot.transport.threadHandle}
                />
              ) : null}

            {error ? (
              <Message className="error-message" from="assistant">
                <MessageContent>
                  <section role="alert">
                    <strong>{error.title}</strong>
                    <p>{error.message}</p>
                    <div>
                      {error.recovery === "refresh" || error.recovery === "retry" ? (
                        <button onClick={retryLatest} type="button">
                          {pending ? "使用同一提交重试" : "重新获取状态"}
                        </button>
                      ) : null}
                      {error.recovery === "new_analysis" ? (
                        <button onClick={newAnalysis} type="button">开始新分析</button>
                      ) : null}
                      {error.recovery === "sign_in" && signInUrl ? (
                        <a href={signInUrl}>重新登录</a>
                      ) : null}
                      {error.recovery === "sign_in" && !signInUrl ? (
                        <button onClick={() => window.location.reload()} type="button">
                          重新检查登录状态
                        </button>
                      ) : null}
                      {error.recovery === "contact_support" && supportUrl ? (
                        <a href={supportUrl}>联系支持</a>
                      ) : null}
                      {error.recovery === "contact_support" && !supportUrl ? (
                        <span>请联系你所在团队的 WAJE 支持联系人。</span>
                      ) : null}
                    </div>
                  </section>
                </MessageContent>
              </Message>
            ) : null}
          </ConversationContent>
          <ConversationScrollButton aria-label="回到最新消息" />
        </Conversation>

        {snapshot || pending || showComposer ? (
          <div className="agent-dock">
            {snapshot && (snapshot.state.status !== "idle" || pending) ? (
              <AnalysisTaskCard
                connection={connection}
                onNewAnalysis={newAnalysis}
                onRefresh={() => void refreshSnapshot().catch(showRequestError)}
                pending={Boolean(pending)}
                reasoning={reasoning}
                snapshot={snapshot}
              />
            ) : null}

            {snapshot?.state.status === "needs_input" ? (
              <div
                className="customer-question-focus"
                ref={inputRequestRef}
                tabIndex={-1}
              >
                <QuestionCard
                  disabled={Boolean(pending)}
                  input={snapshot.state.input}
                  onContinue={submitInput}
                />
              </div>
            ) : null}

            {showComposer ? (
              <div className="composer customer-composer">
                <PromptInput
                  className="customer-prompt-input"
                  onSubmit={(message) => {
                    if (customerState?.status === "needs_input") return;
                    void sendQuestion(message.text);
                  }}
                >
                  <PromptInputTextarea
                    aria-describedby="composer-help"
                    aria-label="业务问题"
                    disabled={composerBlocked}
                    onChange={(event) => setDraft(event.target.value)}
                    placeholder={customerState?.status === "needs_input"
                      ? "请先完成上方确认"
                      : customerState?.status === "working"
                        ? "分析进行中"
                        : customerState?.status === "completed"
                          || customerState?.status === "completed_with_limits"
                          ? "继续追问这项分析"
                          : customerState?.status === "failed"
                            ? "调整问题或继续输入"
                            : "输入业务问题"}
                    ref={composerRef}
                    value={draft}
                  />
                  <PromptInputFooter>
                    <PromptInputTools>
                      <span id="composer-help">
                        {customerState?.status === "needs_input"
                          ? "请先完成上方确认"
                          : "Enter 发送 · Shift + Enter 换行"}
                      </span>
                    </PromptInputTools>
                    <PromptInputSubmit
                      aria-label="发送业务问题"
                      disabled={composerBlocked || !draft.trim()}
                      status={pending ? "submitted" : undefined}
                    />
                  </PromptInputFooter>
                </PromptInput>
              </div>
            ) : null}
          </div>
        ) : null}
      </section>
    </main>
  );
}
