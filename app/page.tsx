"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type RefObject,
} from "react";
import {
  AlertTriangle,
  Check,
  CircleHelp,
  History,
  LoaderCircle,
  Plus,
  X,
} from "lucide-react";

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
  type CustomerInputRequest,
  type CustomerMainStatus,
  type CustomerThreadSummary,
} from "./api/_customerAnalysisContract";

const ACTIVE_THREAD_KEY = "waje-active-thread:v2";
const PENDING_OPERATION_PREFIX = "waje-pending-operation:v3:";
const INITIAL_MESSAGE_SCOPE = "message:new";
const PENDING_OPERATION_TTL_MS = 24 * 60 * 60 * 1000;

type PendingOperation = {
  version: 3;
  operationId: string;
  scope: string;
  kind: "message" | "clarification" | "topic_choice" | "agent_action";
  threadHandle: string;
  actionHandle: string | null;
  message: string;
  optionKey: string | null;
  createdAt: string;
  expiresAt: string;
};

type ProgressConnection = "idle" | "connecting" | "live" | "reconnecting";

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
      || value.version !== 3
      || typeof value.operationId !== "string"
      || typeof value.scope !== "string"
      || value.scope !== scope
      || !["message", "clarification", "topic_choice", "agent_action"].includes(String(value.kind))
      || typeof value.threadHandle !== "string"
      || typeof value.message !== "string"
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
    version: 3,
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
    completed: "分析完成",
    completed_with_limits: "分析完成 · 有边界",
    failed: "分析未完成",
  }[state.status];
}

function formatConfirmedAt(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
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

function InputRequestInline({
  input,
  disabled,
  requestRef,
  onSubmit,
}: {
  input: CustomerInputRequest;
  disabled: boolean;
  requestRef: RefObject<HTMLElement | null>;
  onSubmit: (answer: string, optionKey?: string) => void;
}) {
  const [freeform, setFreeform] = useState("");
  return (
    <section
      aria-labelledby="customer-input-title"
      className="customer-input-inline"
      ref={requestRef}
      tabIndex={-1}
    >
      <span className="eyebrow">{input.title}</span>
      <h2 id="customer-input-title">{input.question}</h2>
      {input.explanation ? <p>{input.explanation}</p> : null}
      <div className="customer-input-options">
        {input.options.map((option) => (
          <button
            className={option.recommended ? "recommended" : ""}
            disabled={disabled}
            key={option.optionKey}
            onClick={() => onSubmit(option.label, option.optionKey)}
            type="button"
          >
            <span>
              <strong>{option.label}</strong>
              {option.recommended ? <span className="sr-only">推荐选项</span> : null}
            </span>
            <small>{option.description}</small>
          </button>
        ))}
      </div>
      {input.allowFreeform ? (
        <form
          className="customer-input-freeform"
          onSubmit={(event) => {
            event.preventDefault();
            const answer = freeform.trim();
            if (answer) onSubmit(answer);
          }}
        >
          <label htmlFor="customer-freeform">采用其他方式</label>
          <div>
            <input
              disabled={disabled}
              id="customer-freeform"
              onChange={(event) => setFreeform(event.target.value)}
              placeholder="说明你希望采用的口径或范围"
              value={freeform}
            />
            <button disabled={disabled || !freeform.trim()} type="submit">
              提交
            </button>
          </div>
        </form>
      ) : null}
    </section>
  );
}

function ProgressTimeline({
  snapshot,
  pending,
  connection,
  onNewAnalysis,
  onRefresh,
}: {
  snapshot: CustomerAnalysisSnapshot;
  pending: PendingOperation | null;
  connection: ProgressConnection;
  onNewAnalysis: () => void;
  onRefresh: () => void;
}) {
  const { state } = snapshot;
  const completed = state.status === "completed"
    || state.status === "completed_with_limits";
  const connectionCopy = connection === "reconnecting"
    ? "连接暂时中断，后台分析仍可能继续，正在恢复。"
    : connection === "connecting"
      ? "正在连接进度服务。"
      : connection === "live"
        ? `进度已连接 · 最近确认 ${formatConfirmedAt(snapshot.confirmedAt)}`
        : "可以关闭页面，分析会在后台继续并保存进展。";
  return (
    <Message className="current-agent-turn" from="assistant">
      <MessageContent>
        <section
          aria-label="当前分析状态"
          className={`progress-timeline ${state.status}`}
          role={state.status === "failed" ? "alert" : undefined}
        >
          <header>
            {state.status === "working" || pending ? (
              <LoaderCircle aria-hidden="true" className="progress-spinner" size={16} />
            ) : state.status === "failed" ? (
              <AlertTriangle aria-hidden="true" size={16} />
            ) : state.status === "needs_input" ? (
              <CircleHelp aria-hidden="true" size={16} />
            ) : (
              <Check aria-hidden="true" size={16} />
            )}
            <div>
              <strong>{pending
                ? "正在确认提交结果"
                : completed
                  ? "分析过程已完成"
                  : state.title}</strong>
              {completed ? null : <p>{pending
                ? "会使用同一个操作身份确认服务器结果，未确认的内容不会重复出现在对话中。"
                : state.description}</p>}
            </div>
          </header>
          {state.updates.length ? (
            completed ? (
              <details className="completed-progress">
                <summary>查看已确认的分析过程</summary>
                <ProgressUpdates updates={state.updates} />
              </details>
            ) : <ProgressUpdates updates={state.updates} />
          ) : null}
          {state.status === "working" ? (
            <small className={`connection-copy ${connection}`}>{connectionCopy}</small>
          ) : null}
          {state.status === "failed" ? (
            <div className="failure-actions">
              {state.recovery === "retry" ? (
                <button onClick={onRefresh} type="button">重新获取最新状态</button>
              ) : null}
              <button onClick={onNewAnalysis} type="button">开始新分析</button>
              {state.recovery === "contact_support" ? (
                <span>若故障持续，请联系你所在团队的 WAJE 支持联系人。</span>
              ) : null}
            </div>
          ) : null}
        </section>
      </MessageContent>
    </Message>
  );
}

function ProgressUpdates({
  updates,
}: {
  updates: CustomerAnalysisState["updates"];
}) {
  return (
    <ol aria-label="分析进展">
      {updates.map((update) => (
        <li className={update.status} key={update.key}>
          <span aria-hidden="true">
            {update.status === "completed"
              ? <Check size={12} />
              : update.status === "failed"
                ? <AlertTriangle size={12} />
                : <LoaderCircle className="progress-spinner" size={12} />}
          </span>
          <span>{update.text}</span>
          <time dateTime={update.confirmedAt}>
            {formatConfirmedAt(update.confirmedAt)}
          </time>
        </li>
      ))}
    </ol>
  );
}

function AnswerMessage({ state }: { state: Extract<CustomerAnalysisState, {
  status: "completed" | "completed_with_limits";
}> }) {
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
        <article aria-labelledby="answer-title">
          <header>
            <span className="eyebrow">业务参考</span>
            <h2 id="answer-title">{state.title}</h2>
          </header>
          <div className="answer-body">
            {mainBlocks.map((block) => (
              <MessageResponse className={block.kind} key={block.key}>
                {block.text}
              </MessageResponse>
            ))}
          </div>
          {recommendations.length ? (
            <section className="answer-next-actions">
              <h3>建议下一步</h3>
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
        </article>
      </MessageContent>
    </Message>
  );
}

export default function Home() {
  const [snapshot, setSnapshot] = useState<CustomerAnalysisSnapshot | null>(null);
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
  const inputRequestRef = useRef<HTMLElement>(null);
  const historyToggleRef = useRef<HTMLButtonElement>(null);
  const currentThreadRef = useRef("");
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
    eventSourceRef.current?.close();
    const eventsUrl = snapshot?.transport.eventsUrl;
    if (!eventsUrl || ["completed", "completed_with_limits", "failed"].includes(
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
        body.selectedOptionId = currentOperation.optionKey;
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
    });
    setDraft("");
    await executeOperation(operation);
  }

  function submitInput(answer: string, optionKey?: string) {
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
      optionKey: optionKey ?? null,
    });
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
    && !pending
    && !(error && !snapshot)
    && !error?.code.startsWith("sign_in")
    && (!customerState || [
      "idle",
      "completed",
      "completed_with_limits",
      "failed",
    ].includes(customerState.status));
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

            {pendingMessageVisible ? (
              <Message className="pending-user-message" from="user">
                <MessageContent className="customer-user-content">
                  {pending.message}
                  <small>正在确认提交</small>
                </MessageContent>
              </Message>
            ) : null}

            {snapshot && (snapshot.state.status !== "idle" || pending) ? (
              <ProgressTimeline
                connection={connection}
                onNewAnalysis={newAnalysis}
                onRefresh={() => void refreshSnapshot().catch(showRequestError)}
                pending={pending}
                snapshot={snapshot}
              />
            ) : null}

            {!snapshot && pending ? (
              <Message className="current-agent-turn" from="assistant">
                <MessageContent>
                  <section aria-live="polite" className="progress-timeline working">
                    <header>
                      <LoaderCircle className="progress-spinner" size={16} />
                      <div>
                        <strong>正在确认提交结果</strong>
                        <p>页面会使用同一个操作身份恢复服务器已确认的结果。</p>
                      </div>
                    </header>
                  </section>
                </MessageContent>
              </Message>
            ) : null}

            {snapshot?.state.status === "needs_input" ? (
              <Message className="input-request-message" from="assistant">
                <MessageContent>
                  <InputRequestInline
                    disabled={Boolean(pending)}
                    input={snapshot.state.input}
                    onSubmit={submitInput}
                    requestRef={inputRequestRef}
                  />
                </MessageContent>
              </Message>
            ) : null}

            {snapshot?.state.status === "completed"
              || snapshot?.state.status === "completed_with_limits" ? (
                <AnswerMessage state={snapshot.state} />
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

        {showComposer ? <div className="composer customer-composer">
          <PromptInput
            className="customer-prompt-input"
            onSubmit={(message) => sendQuestion(message.text)}
          >
            <PromptInputTextarea
              aria-describedby="composer-help"
              aria-label="业务问题"
              disabled={composerBlocked}
              onChange={(event) => setDraft(event.target.value)}
              placeholder={customerState?.status === "completed"
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
                <span id="composer-help">Enter 发送 · Shift + Enter 换行</span>
              </PromptInputTools>
              <PromptInputSubmit
                aria-label="发送业务问题"
                disabled={composerBlocked || !draft.trim()}
                status={pending ? "submitted" : undefined}
              />
            </PromptInputFooter>
          </PromptInput>
        </div> : null}
      </section>
    </main>
  );
}
