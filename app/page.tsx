"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { Plus, Send } from "lucide-react";

const DEFAULT_QUESTION =
  "昨天付费金额为什么变化？主要是首充人数、付费频次、单笔付费金额，还是支付成功率等因素导致的？";

type JsonRecord = Record<string, unknown>;

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
};

type RunState = {
  runId: string;
  runStatus: string;
  agentStatus: string;
  eventsUrl: string;
};

type ProcessUpdate = {
  key: string;
  label: string;
  summary: string;
  status: string;
};

type ClarificationOption = {
  id: string;
  selectedOptionId?: string;
  label: string;
  description: string;
  recommended: boolean;
};

type ClarificationState = {
  runId: string;
  question: string;
  options: ClarificationOption[];
  allowFreeform: boolean;
  recommendationReason: string;
};

type GatewayEvent = {
  event?: unknown;
  runId?: unknown;
  payload?: unknown;
  process?: unknown;
};

function isRecord(value: unknown): value is JsonRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function stringValue(value: unknown) {
  return typeof value === "string" ? value.trim() : "";
}

function statusLabel(status: string) {
  const labels: Record<string, string> = {
    idle: "等待提问",
    queued: "已提交",
    started: "Agent Core 已启动",
    dispatch_in_progress: "等待现有执行完成",
    replayed: "已复用同一请求",
    running: "正在运行",
    running_workflow: "正在分析",
    waiting_for_clarification: "等待业务确认",
    completed: "分析完成",
    completed_without_workflow: "已完成，无需启动分析",
    failed: "运行失败",
  };
  return labels[status] ?? status ?? "未知";
}

function firstRecord(value: unknown, keys: string[]) {
  let current: unknown = value;
  for (const key of keys) {
    if (!isRecord(current)) return null;
    current = current[key];
  }
  return isRecord(current) ? current : null;
}

function answerPackageFrom(value: unknown) {
  if (!isRecord(value)) return null;
  if (isRecord(value.answerPackagePreview)) return value.answerPackagePreview;
  const direct = firstRecord(value, ["agentCore", "result", "answer_package"]);
  if (direct) return direct;
  const result = firstRecord(value, ["result", "answer_package"]);
  return result;
}

function answerTextFromPackage(answerPackage: unknown) {
  if (!isRecord(answerPackage) || !Array.isArray(answerPackage.sections)) return "";
  const summarySection = answerPackage.sections.find(
    (section) => isRecord(section) && section.section_id === "summary",
  );
  if (!isRecord(summarySection) || !isRecord(summarySection.payload)) return "";
  const payload = summarySection.payload;
  const narrative =
    stringValue(payload.final_business_summary) || stringValue(payload.answer_text);
  if (narrative) return narrative;
  if (isRecord(payload.final_explanation)) {
    const explanation = stringValue(payload.final_explanation.explanation);
    const repairPath = stringValue(payload.final_explanation.repair_path);
    if (explanation && repairPath) return `${explanation}\n\n下一步：${repairPath}`;
    if (explanation) return explanation;
  }
  if (Array.isArray(payload.claims)) {
    return payload.claims
      .flatMap((claim) => (isRecord(claim) ? [stringValue(claim.text)] : []))
      .filter(Boolean)
      .join("\n\n");
  }
  return "";
}

function clarificationFrom(value: unknown, runId: string): ClarificationState | null {
  if (!isRecord(value)) return null;
  const candidates = [
    value,
    value.clarification,
    firstRecord(value, ["agentCore", "result"])?.clarification,
    firstRecord(value, ["result"])?.clarification,
    firstRecord(value, ["answer_package"])?.clarification,
  ];
  const raw = candidates.find(
    (candidate) => isRecord(candidate) && Array.isArray(candidate.questions),
  );
  if (!isRecord(raw) || !Array.isArray(raw.questions)) return null;
  const question = raw.questions.find(isRecord);
  if (!question) return null;
  const recommendedAssumption = isRecord(raw.recommended_assumption)
    ? stringValue(raw.recommended_assumption.option)
      || stringValue(raw.recommended_assumption.assumption)
    : stringValue(raw.recommended_assumption);
  const options = Array.isArray(question.options)
    ? question.options.flatMap((option, index) => {
        if (typeof option === "string") {
          return [{
            id: `option-${index + 1}-${option}`,
            label: option,
            description: "",
            recommended:
              option.includes("（推荐）")
              || option === recommendedAssumption,
          }];
        }
        if (!isRecord(option)) return [];
        const label = stringValue(option.label) || stringValue(option.description);
        if (!label) return [];
        const selectedOptionId = stringValue(option.id);
        return [{
          id: selectedOptionId || `option-${index + 1}-${label}`,
          ...(selectedOptionId ? { selectedOptionId } : {}),
          label,
          description: stringValue(option.description),
          recommended:
            option.recommended === true
            || label.includes("（推荐）")
            || label === recommendedAssumption,
        }];
      })
    : [];
  return {
    runId,
    question: stringValue(question.question) || "请确认本轮分析口径。",
    options,
    allowFreeform: raw.allow_freeform === true,
    recommendationReason: stringValue(raw.recommendation_reason),
  };
}

function processUpdateFrom(event: GatewayEvent): ProcessUpdate | null {
  if (!isRecord(event.process)) return null;
  const label = stringValue(event.process.label);
  const summary = stringValue(event.process.summary);
  if (!label && !summary) return null;
  return {
    key: JSON.stringify([event.event, event.process.stage, label, summary, event.process.status]),
    label: label || stringValue(event.event),
    summary,
    status: stringValue(event.process.status),
  };
}

async function responseJson(response: Response) {
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const code = isRecord(payload) ? stringValue(payload.error) : "";
    throw new Error(code || `gateway_http_${response.status}`);
  }
  if (!isRecord(payload)) throw new Error("gateway_response_invalid");
  return payload;
}

function ClarificationCard({
  clarification,
  disabled,
  onSubmit,
}: {
  clarification: ClarificationState;
  disabled: boolean;
  onSubmit: (answer: string, selectedOptionId?: string) => void;
}) {
  const [freeform, setFreeform] = useState("");

  return (
    <section className="gateway-clarification" aria-live="polite">
      <span>需要确认后继续</span>
      <h2>{clarification.question}</h2>
      {clarification.recommendationReason ? (
        <p>{clarification.recommendationReason}</p>
      ) : null}
      <div className="gateway-options">
        {clarification.options.map((option) => (
          <button
            key={option.id}
            className={option.recommended ? "recommended" : ""}
            disabled={disabled}
            onClick={() => onSubmit(option.label, option.selectedOptionId)}
            type="button"
          >
            <strong>{option.label}</strong>
            {option.description && option.description !== option.label ? (
              <small>{option.description}</small>
            ) : null}
          </button>
        ))}
      </div>
      {clarification.allowFreeform ? (
        <form
          className="gateway-freeform"
          onSubmit={(event) => {
            event.preventDefault();
            if (freeform.trim()) onSubmit(freeform.trim());
          }}
        >
          <input
            aria-label="补充你的选择"
            disabled={disabled}
            onChange={(event) => setFreeform(event.target.value)}
            placeholder="告诉系统采用其他方式"
            value={freeform}
          />
          <button disabled={disabled || !freeform.trim()} type="submit">提交</button>
        </form>
      ) : null}
    </section>
  );
}

export default function Home() {
  const [threadId, setThreadId] = useState("");
  const [draft, setDraft] = useState(DEFAULT_QUESTION);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [run, setRun] = useState<RunState>({
    runId: "",
    runStatus: "idle",
    agentStatus: "idle",
    eventsUrl: "",
  });
  const [updates, setUpdates] = useState<ProcessUpdate[]>([]);
  const [clarification, setClarification] = useState<ClarificationState | null>(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const eventSourceRef = useRef<EventSource | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => () => eventSourceRef.current?.close(), []);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, updates, clarification, error]);

  function appendAssistant(text: string) {
    if (!text) return;
    setMessages((current) => [
      ...current,
      { id: crypto.randomUUID(), role: "assistant", text },
    ]);
  }

  function acceptAnswerPackage(answerPackage: JsonRecord | null) {
    if (!answerPackage) return false;
    const answer = answerTextFromPackage(answerPackage);
    if (answer) appendAssistant(answer);
    return Boolean(answer);
  }

  function watchRun(eventsUrl: string, runId: string) {
    eventSourceRef.current?.close();
    const source = new EventSource(eventsUrl);
    eventSourceRef.current = source;
    source.onmessage = (message) => {
      let event: GatewayEvent;
      try {
        event = JSON.parse(message.data) as GatewayEvent;
      } catch {
        setError("gateway_event_invalid");
        source.close();
        return;
      }
      const update = processUpdateFrom(event);
      if (update) {
        setUpdates((current) => current.some((item) => item.key === update.key)
          ? current
          : [...current, update]);
      }
      const payload = isRecord(event.payload) ? event.payload : null;
      if (event.event === "run_status" && payload) {
        const status = stringValue(payload.status);
        if (status) setRun((current) => ({ ...current, runStatus: status }));
        if (status === "failed" || status === "completed_without_workflow") {
          source.close();
          setSubmitting(false);
        }
      }
      if (event.event === "clarification_requested" && payload) {
        const next = clarificationFrom(payload, runId);
        if (next) {
          setClarification(next);
          setSubmitting(false);
          source.close();
        }
      }
      if (event.event === "answer_package_ready" && payload) {
        const answerPackage = isRecord(payload.answer_package)
          ? payload.answer_package
          : null;
        acceptAnswerPackage(answerPackage);
        setRun((current) => ({
          ...current,
          runStatus: current.runStatus === "failed" ? "failed" : "completed",
        }));
        setSubmitting(false);
        source.close();
      }
    };
    source.onerror = () => {
      // The Gateway endpoint publishes a finite persisted snapshot. EventSource
      // reconnects until a terminal run, clarification, or Answer Package appears.
    };
  }

  async function ensureThread() {
    if (threadId) return threadId;
    const response = await fetch("/api/threads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const payload = await responseJson(response);
    if (!isRecord(payload.thread) || !stringValue(payload.thread.id)) {
      throw new Error("gateway_thread_invalid");
    }
    const createdThreadId = stringValue(payload.thread.id);
    setThreadId(createdThreadId);
    return createdThreadId;
  }

  function applyGatewayResponse(payload: JsonRecord, fallbackRunId = "") {
    const runRecord = isRecord(payload.run) ? payload.run : null;
    const agentCore = isRecord(payload.agentCore) ? payload.agentCore : null;
    const agentResult = agentCore && isRecord(agentCore.result) ? agentCore.result : null;
    const runId =
      stringValue(runRecord?.id) ||
      stringValue(payload.resumedRunId) ||
      stringValue(agentResult?.run_id) ||
      fallbackRunId;
    const runStatus =
      stringValue(runRecord?.status) ||
      stringValue(agentResult?.status) ||
      stringValue(payload.status) ||
      "queued";
    const agentStatus = stringValue(agentCore?.status) || "queued";
    const eventsUrl = stringValue(payload.eventsUrl);
    setRun({ runId, runStatus, agentStatus, eventsUrl });

    const nextClarification = clarificationFrom(payload, runId);
    if (nextClarification) setClarification(nextClarification);
    const hasAnswer = acceptAnswerPackage(answerPackageFrom(payload));
    if (agentStatus === "failed") {
      setError(stringValue(agentCore?.error) || stringValue(agentResult?.failure_reason) || "agent_core_run_failed");
    }
    const terminal = ["completed", "completed_without_workflow", "failed"].includes(runStatus);
    if (terminal || nextClarification || hasAnswer) setSubmitting(false);
    if (eventsUrl && !nextClarification && !hasAnswer && runStatus !== "failed") {
      watchRun(eventsUrl, runId);
    }
  }

  async function sendQuestion(event: FormEvent) {
    event.preventDefault();
    const question = draft.trim();
    if (!question || submitting || clarification) return;
    setSubmitting(true);
    setError("");
    setUpdates([]);
    eventSourceRef.current?.close();
    try {
      const activeThreadId = await ensureThread();
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "user", text: question },
      ]);
      setDraft("");
      const response = await fetch(
        `/api/threads/${encodeURIComponent(activeThreadId)}/messages`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": crypto.randomUUID(),
          },
          body: JSON.stringify({ message: question }),
        },
      );
      applyGatewayResponse(await responseJson(response));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "gateway_request_failed");
      setSubmitting(false);
    }
  }

  async function submitClarification(answer: string, selectedOptionId?: string) {
    if (!clarification || submitting) return;
    setSubmitting(true);
    setError("");
    eventSourceRef.current?.close();
    setMessages((current) => [
      ...current,
      { id: crypto.randomUUID(), role: "user", text: answer },
    ]);
    const sourceRunId = clarification.runId;
    setClarification(null);
    try {
      const response = await fetch(
        `/api/runs/${encodeURIComponent(sourceRunId)}/clarifications`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": crypto.randomUUID(),
          },
          body: JSON.stringify({ answer, selectedOptionId: selectedOptionId ?? null }),
        },
      );
      applyGatewayResponse(await responseJson(response), sourceRunId);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "gateway_request_failed");
      setSubmitting(false);
    }
  }

  function newAnalysis() {
    eventSourceRef.current?.close();
    setThreadId("");
    setMessages([]);
    setUpdates([]);
    setClarification(null);
    setError("");
    setRun({ runId: "", runStatus: "idle", agentStatus: "idle", eventsUrl: "" });
    setDraft(DEFAULT_QUESTION);
    setSubmitting(false);
  }

  return (
    <main className="app-shell">
      <aside className="thread-sidebar">
        <div className="brand">WAJE BI v2</div>
        <button className="new-thread" onClick={newAnalysis} type="button">
          <Plus size={14} /> 新分析
        </button>
        <nav>
          {threadId ? <a className="active">{threadId}</a> : <a>会话将在首次提问时创建</a>}
        </nav>
      </aside>

      <section className="chat-shell">
        <header className="chat-header">
          <div>
            <strong>真实数据分析</strong>
            <span>Gateway → ConversationAgentCore → Answer Package</span>
          </div>
          <div className="gateway-run-status" aria-live="polite">
            <span>Run：{statusLabel(run.runStatus)}</span>
            <span>Agent：{statusLabel(run.agentStatus)}</span>
          </div>
        </header>

        <div className="message-list" ref={listRef}>
          {messages.length === 0 ? (
            <div className="gateway-empty">
              输入真实业务问题后，页面会创建会话并交给 Gateway 执行。
            </div>
          ) : null}
          {messages.map((message) => (
            message.role === "user" ? (
              <div className="user-message" key={message.id}>
                <div className="user-bubble">{message.text}</div>
              </div>
            ) : (
              <article className="business-answer" key={message.id}>
                {message.text.split(/\n{2,}/).map((paragraph, index) => (
                  <p key={`${message.id}-${index}`}>{paragraph}</p>
                ))}
              </article>
            )
          ))}

          {run.runId ? (
            <section className="gateway-runtime-card">
              <div>
                <strong>{statusLabel(run.runStatus)}</strong>
                <span>{run.runId}</span>
              </div>
              {updates.length ? (
                <ol className="gateway-process-list">
                  {updates.map((update) => (
                    <li key={update.key}>
                      <strong>{update.label}</strong>
                      <p>{update.summary}</p>
                      {update.status ? <small>{statusLabel(update.status)}</small> : null}
                    </li>
                  ))}
                </ol>
              ) : (
                <p>等待 Gateway 返回持久化运行状态。</p>
              )}
            </section>
          ) : null}

          {clarification ? (
            <ClarificationCard
              clarification={clarification}
              disabled={submitting}
              onSubmit={submitClarification}
            />
          ) : null}
          {error ? <div className="gateway-error" role="alert">{error}</div> : null}
        </div>

        <form className="composer gateway-composer" onSubmit={sendQuestion}>
          <textarea
            aria-label="业务问题"
            disabled={submitting || Boolean(clarification)}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={clarification ? "请先回答上方澄清问题" : "输入业务问题"}
            value={draft}
          />
          <div className="composer-actions">
            <div>
              <span>{threadId || "新会话"}</span>
            </div>
            <button
              aria-label="发送"
              className="send-button"
              disabled={submitting || Boolean(clarification) || !draft.trim()}
              type="submit"
            >
              <Send size={14} />
            </button>
          </div>
        </form>
      </section>
    </main>
  );
}
