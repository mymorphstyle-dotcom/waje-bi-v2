"use client";

import { useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { Check, ChevronDown, CircleAlert, Loader2, RefreshCw, RotateCcw, SkipForward, Workflow } from "lucide-react";

import type {
  TraceAcceptedTask,
  TraceAnswer,
  TraceCard,
  TraceCapabilityOutcomeStatus,
  TraceLifecycleOutcome,
  TraceMessage,
  TraceNode,
  TraceRun,
  TraceRunOutcome,
} from "./contracts";
import styles from "../phase4-replay/replay.module.css";

type TodoId = "intent" | "plan" | "evidence" | "coverage" | "authority" | "delivery";
type TodoStatus = "pending" | "in_progress" | "completed" | "failed" | "waiting";
type PlaybackState = "snapshot" | "ready" | "playing" | "completed";
type AcceptedTaskStatus = "not_started" | "unsettled" | TraceCapabilityOutcomeStatus;
type RunsResponse = { runs: TraceRun[] };

const WorkflowCanvasModal = dynamic(() => import("./WorkflowCanvasModal").then((module) => module.WorkflowCanvasModal), {
  ssr: false,
});

export function AgentRunWorkbench() {
  const [runs, setRuns] = useState<TraceRun[]>([]);
  const [activeId, setActiveId] = useState("");
  const [visibleCount, setVisibleCount] = useState(0);
  const [selectedNodeId, setSelectedNodeId] = useState("");
  const [playbackState, setPlaybackState] = useState<PlaybackState>("snapshot");
  const [canvasOpen, setCanvasOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const timers = useRef<number[]>([]);
  const messageListRef = useRef<HTMLDivElement>(null);

  const active = runs.find((run) => run.id === activeId);
  const nodes = active?.processSummary.nodes ?? [];
  const visibleNodes = nodes.slice(0, visibleCount);
  const playing = playbackState === "playing";
  const playbackComplete = playbackState === "completed";
  const showingFinal = Boolean(
    active
    && (playbackComplete || playbackState === "ready" || playbackState === "snapshot")
    && visibleCount >= nodes.length,
  );
  const visibleMessages = active ? messagesForVisibleRun(active, visibleNodes, showingFinal) : [];
  const todos = active ? buildTodoStatuses(visibleNodes, playbackState) : [];
  const canReplay = Boolean(active && playbackScheduleFor(active));

  useEffect(() => {
    void loadRuns();
    return () => clearTimers();
  }, []);

  useEffect(() => {
    messageListRef.current?.scrollTo({ top: 0, behavior: "auto" });
  }, [activeId]);

  async function loadRuns(preferredRunId?: string) {
    preferredRunId ? setRefreshing(true) : setLoading(true);
    setError("");
    try {
      const response = await fetch("/api/agent-runs", { cache: "no-store" });
      if (!response.ok) throw new Error(`agent_runs_api_${response.status}`);
      const data = (await response.json()) as RunsResponse;
      setRuns(data.runs);
      const selected = data.runs.find((run) => run.id === preferredRunId) ?? data.runs[0];
      if (selected) showRun(selected);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "agent_runs_api_failed");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }

  function clearTimers() {
    timers.current.forEach((timer) => window.clearTimeout(timer));
    timers.current = [];
  }

  function showRun(run: TraceRun) {
    if (!run) return;
    clearTimers();
    setActiveId(run.id);
    setSelectedNodeId("");
    setVisibleCount(run.processSummary.nodes.length);
    setPlaybackState(run.runMode === "event_replay" ? "ready" : "snapshot");
  }

  function startPlayback(run = active) {
    if (!run) return;
    const schedule = playbackScheduleFor(run);
    if (!schedule) return;
    clearTimers();
    setActiveId(run.id);
    setSelectedNodeId("");
    setVisibleCount(0);
    setPlaybackState("playing");
    schedule.forEach((time, index) => {
      timers.current.push(
        window.setTimeout(() => {
          setVisibleCount(index + 1);
          setSelectedNodeId(run.processSummary.nodes[index]?.id ?? "");
          if (index === schedule.length - 1) setPlaybackState("completed");
        }, time),
      );
    });
  }

  function showFinal(run = active) {
    if (!run) return;
    clearTimers();
    setActiveId(run.id);
    setVisibleCount(run.processSummary.nodes.length);
    setSelectedNodeId(run.processSummary.nodes.at(-1)?.id ?? "");
    setPlaybackState(run.runMode === "event_replay" ? "ready" : "snapshot");
  }

  return (
    <main className={styles.shell}>
      <section className={styles.chat}>
        <header className={styles.header}>
          <div className={styles.headerTitle}>
            <span className={styles.brandInline}>WAJE BI v2</span>
            <strong>{active ? runHeading(active) : "Agent Run Workbench"}</strong>
            <span>
              {active
                ? `${active.processSummary.checkpointCount} 个节点 · ${llmCallLabel(active)} · ${runModeLabel(active.runMode)}`
                : "加载真实运行记录"}
            </span>
          </div>
          <div className={styles.headerActions}>
            <label className={styles.runSelect}>
              <span>运行</span>
              <select
                disabled={loading || refreshing || !runs.length}
                onChange={(event) => {
                  const run = runs.find((item) => item.id === event.target.value);
                  if (run) showRun(run);
                }}
                value={activeId}
              >
                {runs.map((run) => (
                  <option key={run.id} value={run.id}>
                    {runOptionLabel(run)}
                  </option>
                ))}
              </select>
            </label>
            <button disabled={loading || refreshing} onClick={() => void loadRuns(activeId)} type="button">
              <RefreshCw className={refreshing ? styles.spin : undefined} size={14} />
              刷新
            </button>
            <button
              disabled={!active}
              onClick={() => {
                setSelectedNodeId("");
                setCanvasOpen(true);
              }}
              type="button"
            >
              <Workflow size={14} />
              完整工作流
            </button>
            <button disabled={!canReplay || playing} onClick={() => startPlayback()} type="button">
              <RotateCcw size={14} />
              重放
            </button>
            <button disabled={!active} onClick={() => showFinal()} type="button">
              <SkipForward size={14} />
              最终结果
            </button>
          </div>
        </header>

        <div className={styles.messageList} ref={messageListRef}>
          {loading ? <p className={styles.empty}>正在加载真实 run...</p> : null}
          {error ? <p className={styles.error}>{error}</p> : null}
          {!loading && !error && !active ? (
            <p className={styles.empty}>当前没有可回放的持久化运行记录。</p>
          ) : null}
          {active ? (
            <>
              <RunOverview run={active} />
              <RunBadge active={active} playbackState={playbackState} visible={visibleCount} />
              <div className={styles.workbenchGrid}>
                <section className={styles.chatTranscript}>
                  <div className={styles.sectionHeader}>
                    <h2>线性对话</h2>
                    <p>从用户问题到最终回答，只展示业务可读的判断和系统动作。</p>
                  </div>
                  <ChatTranscript
                    messages={visibleMessages}
                    selectedNodeId={selectedNodeId}
                    onSelectNode={(nodeId) => {
                      setSelectedNodeId(nodeId);
                      setCanvasOpen(true);
                    }}
                  />
                </section>
              </div>
              {showingFinal && active.answer ? (
                <AnswerCard answer={active.answer} cards={active.summaryCards} run={active} />
              ) : null}
            </>
          ) : null}
        </div>

        <div className={styles.bottomPanel}>
          {active ? (
            <div className={styles.bottomStack}>
              <TodoWorkbench playbackState={playbackState} run={active} todos={todos} />
            </div>
          ) : null}
        </div>
      </section>
      {active && canvasOpen ? (
        <WorkflowCanvasModal
          onClose={() => setCanvasOpen(false)}
          onSelectNode={setSelectedNodeId}
          run={active}
          selectedNodeId={selectedNodeId}
          visibleCount={visibleCount}
        />
      ) : null}
    </main>
  );
}

function RunOverview({ run }: { run: TraceRun }) {
  const graph = acceptedGraphLabel(run);
  return (
    <section className={styles.runOverview}>
      <div className={styles.questionBlock}>
        <small>用户问题</small>
        <p>{run.question}</p>
      </div>
      <div className={styles.summaryCards}>
        {run.summaryCards.map((card) => (
          <SummaryTile card={card} key={`${card.label}-${card.value}`} />
        ))}
      </div>
      {run.businessThreads?.length ? (
        <details className={styles.auditDetails}>
          <summary>
            发布与审计信息
            <ChevronDown size={13} />
          </summary>
          <div className={styles.summaryCards}>
            {run.businessThreads.map((card) => (
              <SummaryTile card={card} key={`${card.label}-${card.value}`} />
            ))}
          </div>
        </details>
      ) : null}
      <p className={styles.runNarrative}>
        当前记录以{runModeLabel(run.runMode)}呈现；{lifecycleSummary(run)}。计划内能力任务：{graph}。
      </p>
    </section>
  );
}

function SummaryTile({ card }: { card: TraceCard }) {
  return (
    <div className={styles.summaryTile}>
      <small>{card.label}</small>
      <strong>{card.value}</strong>
      {card.detail ? <span>{card.detail}</span> : null}
    </div>
  );
}

function RunBadge({
  active,
  playbackState,
  visible,
}: {
  active: TraceRun;
  playbackState: PlaybackState;
  visible: number;
}) {
  const total = active.processSummary.nodes.length;
  const playing = playbackState === "playing";
  const mode =
    playbackState === "snapshot"
      ? "静态快照"
      : playbackState === "ready"
        ? "事件记录可重放"
        : playbackState === "completed"
          ? "路径回放完成"
          : `回放中 ${visible}/${total}`;
  return (
    <div className={styles.runBadge}>
      <RunOutcomeIcon outcome={active.runOutcome} playing={playing} />
      <span>{mode} · {runOutcomeLabel(active.runOutcome)} · {lifecycleSummary(active)} · {runTimingLabel(active)}</span>
    </div>
  );
}

function ChatTranscript({
  messages,
  selectedNodeId,
  onSelectNode,
}: {
  messages: TraceMessage[];
  selectedNodeId?: string;
  onSelectNode: (nodeId: string) => void;
}) {
  return (
    <div className={styles.chatTurns}>
      {messages.map((message) => (
        <button
          className={`${styles.chatTurn} ${styles[message.role]} ${message.nodeId && message.nodeId === selectedNodeId ? styles.activeTurn : ""}`}
          disabled={!message.nodeId}
          key={message.id}
          onClick={() => message.nodeId && onSelectNode(message.nodeId)}
          type="button"
        >
          <span>{message.title}</span>
          <p>{message.text}</p>
        </button>
      ))}
    </div>
  );
}

function AnswerCard({ answer, cards, run }: { answer: TraceAnswer; cards: TraceCard[]; run: TraceRun }) {
  const reviewNeedsAttention = run.humanReview.status === "revision_requested"
    || run.lifecycle.verifier.status === "findings";
  return (
    <article className={styles.answer}>
      <div className={styles.answerHeader}>
        <strong>{run.lifecycle.publication.outcome === "complete" ? "已发布业务参考" : "业务参考"}</strong>
        <span className={reviewNeedsAttention ? styles.degradedBadge : styles.passBadge}>
          {answerAuthorityLabel(run)}
        </span>
      </div>
      <div className={styles.answerText}>
        {answer.answerText.split(/\n+/).filter(Boolean).map((paragraph) => (
          <p key={paragraph}>{paragraph}</p>
        ))}
      </div>
      <div className={styles.stats}>
        {cards.map((card) => (
          <span key={`${card.label}-${card.value}`}>
            <strong>{card.value}</strong>
            <small>{card.label}</small>
          </span>
        ))}
      </div>
      <section className={styles.limitations}>
        <h3>人工复核与学习标注</h3>
        <p>{humanReviewLabel(run)}</p>
        {run.humanReview.latest ? (
          <div>
            {Object.entries(run.humanReview.latest.scores).map(([dimension, score]) => (
              <span key={dimension}>{insightDimensionLabel(dimension)} {score}/5</span>
            ))}
          </div>
        ) : null}
        {run.humanReview.latest
          ? Object.entries(run.humanReview.latest.humanReasons).map(([dimension, reason]) => (
              <p key={dimension}><strong>{insightDimensionLabel(dimension)}：</strong>{reason}</p>
            ))
          : null}
      </section>
      {answer.limitations.length ? (
        <section className={styles.limitations}>
          <h3>已声明边界</h3>
          <div>
            {answer.limitations.map((limitation) => (
              <span key={limitation}>{limitation}</span>
            ))}
          </div>
        </section>
      ) : null}
      {answer.evidence.length ? (
        <section className={styles.evidence}>
          <h3>能力执行与证据边界</h3>
          {answer.evidence.map((item) => (
            <div
              data-binding-state={item.bindingState}
              data-execution-state={item.executionState}
              key={item.evidenceRef}
            >
              <strong>{capabilityLabel(item.capability)}</strong>
              <small>
                {evidenceBindingLabel(item.bindingState)}
                {` · ${evidenceExecutionLabel(item.executionState)}`}
                {item.planState === "superseded" ? " · 历史计划" : ""}
                {` · ${evidenceTypeLabel(item.label)} · ${item.detail}`}
              </small>
            </div>
          ))}
        </section>
      ) : null}
    </article>
  );
}

function answerAuthorityLabel(run: TraceRun) {
  if (run.lifecycle.publication.outcome === "failed") return "发布失败";
  if (run.lifecycle.publication.outcome !== "complete") return "尚未发布";
  if (run.humanReview.status === "revision_requested") return "已发布 · 人工要求重写";
  if (run.humanReview.status === "reviewed") return "已发布 · 已人工标注";
  if (run.lifecycle.verifier.status === "findings") return "已发布 · 后台核验有发现";
  return "已发布 · 待人工复核";
}

function humanReviewLabel(run: TraceRun) {
  if (run.humanReview.status === "revision_requested") {
    return `已完成 ${run.humanReview.evaluationCount} 次复核；最近一次要求发起独立叙事尝试。当前发布版本继续保留。`;
  }
  if (run.humanReview.status === "reviewed") {
    return `已完成 ${run.humanReview.evaluationCount} 次复核并保留当前发布版本。`;
  }
  if (run.humanReview.status === "pending") {
    return "当前业务参考已发布，等待人工审计、标注与清洗。后台核验发现只作为复核线索。";
  }
  return "当前运行尚未形成可复核的发布版本。";
}

function insightDimensionLabel(dimension: string) {
  return ({
    explanation_value: "解释价值",
    novelty: "洞察新颖度",
    decision_usefulness: "决策帮助",
    competing_hypotheses: "竞争假设",
    uncertainty_handling: "不确定性处理",
    actionability: "可行动性",
  } as Record<string, string>)[dimension] ?? dimension;
}

function TodoWorkbench({
  playbackState,
  run,
  todos,
}: {
  playbackState: PlaybackState;
  run: TraceRun;
  todos: { id: TodoId; label: string; status: TodoStatus }[];
}) {
  const active = todos.find((todo) => todo.status === "in_progress");
  const completed = todos.filter((todo) => todo.status === "completed").length;
  const summary = playbackState === "playing" && active
    ? `当前阶段 · ${active.label}`
    : runOutcomeLabel(run.runOutcome);
  return (
    <details className={styles.todoTool}>
      <summary className={styles.todoSummary}>
        <span>
          <RunOutcomeIcon outcome={run.runOutcome} playing={playbackState === "playing"} />
          {summary}
        </span>
        <small>{completed}/{todos.length}</small>
        <ChevronDown className={styles.todoChevron} size={14} />
      </summary>
      <div className={styles.todoRows}>
        {todos.map((todo) => (
          <div className={`${styles.todoRow} ${styles[todo.status]}`} key={todo.id}>
            <span>
              {todo.status === "completed" ? (
                <Check size={11} />
              ) : todo.status === "in_progress" ? (
                <Loader2 className={styles.spin} size={11} />
              ) : todo.status === "failed" ? (
                "!"
              ) : todo.status === "waiting" ? (
                "…"
              ) : null}
            </span>
            <p>{todo.label}</p>
          </div>
        ))}
      </div>
    </details>
  );
}

function buildTodoStatuses(nodes: TraceNode[], playbackState: PlaybackState) {
  const todos: { id: TodoId; label: string }[] = [
    { id: "intent", label: "理解业务问题和边界" },
    { id: "plan", label: "编译权威分析计划" },
    { id: "evidence", label: "执行能力证据图" },
    { id: "coverage", label: "检查结论证据覆盖" },
    { id: "authority", label: "结算结论权威" },
    { id: "delivery", label: "生成并交付客户发布" },
  ];
  const current = stageForNode(nodes.at(-1)?.node ?? "");
  return todos.map((todo) => {
    const stageNodes = nodes.filter((node) => stageForNode(node.node) === todo.id);
    let status: TodoStatus = "pending";
    if (stageNodes.some((node) => node.outcome === "failed")) status = "failed";
    else if (stageNodes.some((node) => node.outcome === "waiting")) status = "waiting";
    else if (playbackState === "playing" && current === todo.id) status = "in_progress";
    else if (stageNodes.length && stageNodes.every((node) => node.outcome === "completed" || node.outcome === "skipped")) status = "completed";
    return { ...todo, status };
  });
}

function stageForNode(node: string): TodoId | undefined {
  if (
    [
      "conversation_entry",
      "bind_intent",
      "generate_clarification",
      "persist_waiting_for_decision",
      "accept_material_decision",
    ].includes(node)
  ) return "intent";
  if (node === "compile_authoritative_plan") return "plan";
  if (node === "execute_capability_dag") return "evidence";
  if (node === "evaluate_claim_coverage") return "coverage";
  if (["settle_claim_authority", "seal_authority_bundle"].includes(node)) return "authority";
  if (["compose_claim_aware_narrative", "publish_customer_projection", "deliver_publication"].includes(node)) return "delivery";
  return undefined;
}

function messagesForVisibleRun(run: TraceRun, nodes: TraceNode[], showingFinal: boolean) {
  const visibleNodeIds = new Set(nodes.map((node) => node.id));
  const answerText = normalizeMessageText(run.answer?.answerText ?? "");
  return (run.messages ?? []).filter((message) => {
    if (message.id === "user-question") return true;
    if (answerText && normalizeMessageText(message.text) === answerText) return false;
    return message.nodeId ? visibleNodeIds.has(message.nodeId) : showingFinal;
  });
}

function normalizeMessageText(value: string) {
  return value.replace(/\s+/g, " ").trim();
}

function playbackScheduleFor(run: TraceRun): number[] | undefined {
  if (run.runMode !== "event_replay" || run.traceCompleteness.chronology !== "known") return undefined;
  const nodes = run.processSummary.nodes;
  if (!nodes.length) return undefined;
  const eventTimes = nodes.map((node) => eventTime(node));
  if (eventTimes.some((value) => value === undefined)) return undefined;
  const timestamps = eventTimes as number[];
  if (timestamps.some((value, index) => index > 0 && value < timestamps[index - 1])) return undefined;
  const firstStartedAt = parseEventTime(nodes[0]?.startedAt);
  const origin = firstStartedAt === undefined ? timestamps[0] : Math.min(firstStartedAt, timestamps[0]);
  const elapsed = timestamps.map((value) => value - origin);
  const total = elapsed.at(-1) ?? 0;
  if (total <= 0) return undefined;
  const scale = total > 10000 ? 10000 / total : 1;
  return elapsed.map((value) => Math.max(1, Math.round(value * scale)));
}

function eventTime(node: TraceNode) {
  return parseEventTime(node.finishedAt) ?? parseEventTime(node.startedAt);
}

function parseEventTime(value?: string) {
  if (!value) return undefined;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function formatMs(value: number) {
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${Math.round(value)}ms`;
}

function runTimingLabel(run: TraceRun) {
  if (run.timing.completeness === "unknown" || run.timing.actualDurationMs === undefined) return "耗时未记录";
  const label = `真实耗时 ${formatMs(run.timing.actualDurationMs)}`;
  return run.timing.completeness === "incomplete" ? `${label}（记录不完整）` : label;
}

function llmCallLabel(run: TraceRun) {
  if (run.traceCompleteness.llmCalls === "unknown" || run.processSummary.llmCallCount === undefined) return "模型判断未记录";
  const label = `${run.processSummary.llmCallCount} 次模型判断`;
  return run.traceCompleteness.llmCalls === "incomplete" ? `${label}（记录不完整）` : label;
}

function runModeLabel(mode: TraceRun["runMode"]) {
  return mode === "event_replay" ? "事件回放记录" : "静态快照";
}

function runOutcomeLabel(outcome: TraceRunOutcome) {
  const labels: Record<TraceRunOutcome, string> = {
    completed: "分析已完成",
    running: "分析进行中",
    checkpoint: "阶段检查点已封存",
    interaction_completed: "交互已完成",
    delivery_pending: "发布等待交付",
    delivery_failed: "分析完成，交付失败",
    failed: "分析失败",
    waiting: "等待继续处理",
    withheld: "发布已暂停",
    published: "分析已发布",
    unknown: "运行结果未记录",
  };
  return labels[outcome];
}

function RunOutcomeIcon({ outcome, playing }: { outcome: TraceRunOutcome; playing: boolean }) {
  if (playing || outcome === "waiting" || outcome === "running" || outcome === "delivery_pending") {
    return <Loader2 className={playing || outcome === "running" ? styles.spin : undefined} size={13} />;
  }
  if (["published", "completed", "checkpoint", "interaction_completed"].includes(outcome)) {
    return <Check size={13} />;
  }
  return <CircleAlert size={13} />;
}

function lifecycleSummary(run: TraceRun) {
  return ([
    lifecycleOutcomeLabel("execution", run.lifecycle.execution.outcome),
    lifecycleOutcomeLabel("verifier", run.lifecycle.verifier.outcome),
    lifecycleOutcomeLabel("publication", run.lifecycle.publication.outcome),
    lifecycleOutcomeLabel("delivery", run.lifecycle.delivery.outcome),
  ] as string[]).join(" · ");
}

function lifecycleOutcomeLabel(
  domain: keyof TraceRun["lifecycle"],
  outcome: TraceLifecycleOutcome,
) {
  const labels: Record<keyof TraceRun["lifecycle"], Record<TraceLifecycleOutcome, string>> = {
    execution: {
      complete: "分析完成",
      running: "分析进行中",
      checkpoint: "停在阶段检查点",
      pending: "分析进行中",
      failed: "分析失败",
      blocked: "分析受阻",
      not_applicable: "无需分析",
      unknown: "分析状态未记录",
    },
    verifier: {
      complete: "校验完成",
      running: "校验进行中",
      checkpoint: "校验检查点已封存",
      pending: "等待校验",
      failed: "校验失败",
      blocked: "校验阻断",
      not_applicable: "无需校验",
      unknown: "校验状态未记录",
    },
    publication: {
      complete: "发布完成",
      running: "发布进行中",
      checkpoint: "发布检查点已封存",
      pending: "等待发布",
      failed: "发布失败",
      blocked: "发布暂停",
      not_applicable: "无需发布",
      unknown: "发布状态未记录",
    },
    delivery: {
      complete: "交付完成",
      running: "交付进行中",
      checkpoint: "交付检查点已封存",
      pending: "等待交付",
      failed: "交付失败",
      blocked: "交付暂停",
      not_applicable: "无需交付",
      unknown: "交付状态未记录",
    },
  };
  return labels[domain][outcome];
}

function acceptedGraphLabel(run: TraceRun) {
  const tasks = run.processSummary.acceptedGraph ?? [];
  const capabilities = [...new Set(tasks.map((task) => capabilityLabel(task.capabilityId)))];
  const graph = capabilities.join("、");
  if (run.traceCompleteness.acceptedGraph === "unknown") return "未记录";
  if (!graph) return run.traceCompleteness.acceptedGraph === "incomplete" ? "记录不完整" : "无计划内能力任务";
  const label = `${tasks.length} 项（${acceptedTaskOutcomeSummary(tasks)}）：${graph}`;
  return run.traceCompleteness.acceptedGraph === "incomplete" ? `${label}（记录不完整）` : label;
}

function acceptedTaskOutcomeSummary(tasks: TraceAcceptedTask[]) {
  const labels = {
    not_started: "尚未执行",
    unsettled: "尚未结算",
    succeeded: "成功",
    unavailable: "不可用",
    integrity_failed: "完整性失败",
    technical_failed: "技术失败",
    skipped: "已跳过",
    superseded: "已替代",
  } satisfies Record<AcceptedTaskStatus, string>;
  return (Object.keys(labels) as AcceptedTaskStatus[])
    .map((status) => {
      const count = tasks.filter((task) => acceptedTaskStatus(task) === status).length;
      return count ? `${labels[status]} ${count}` : "";
    })
    .filter(Boolean)
    .join(" / ");
}

function acceptedTaskStatus(task: TraceAcceptedTask): AcceptedTaskStatus {
  return task.execution.state === "settled" ? task.execution.status : task.execution.state;
}

function runOptionLabel(run: TraceRun) {
  const time = formatRunTime(run.generatedAt);
  return `${time} · ${runOutcomeLabel(run.runOutcome)} · ${shortRunId(run)} · ${run.question || "业务问题未记录"}`;
}

function runHeading(run: TraceRun) {
  return `${run.question || "业务问题未记录"} · ${formatRunTime(run.generatedAt)} · ${shortRunId(run)} · ${runOutcomeLabel(run.runOutcome)}`;
}

function shortRunId(run: TraceRun) {
  return run.runId.replace(/^run-/, "").slice(-8) || run.runId.slice(-8);
}

function formatRunTime(value?: number) {
  if (value === undefined || !Number.isFinite(value)) return "时间未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未记录";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function capabilityLabel(value: string) {
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
      candidate_dimension_screen: "候选维度筛选",
      candidate_crosswalk: "候选因素对照",
      metric_timeseries: "指标时间序列",
      cross_source_association: "跨来源关联",
      cross_source_panel_association: "跨来源面板关联",
      market_health_compare: "市场健康度对比",
      market_channel_context: "市场渠道背景",
      source_reconciliation: "来源一致性核对",
      segment_contribution: "分群贡献拆解",
      outlier_contribution: "异常贡献拆解",
      event_evidence: "事件解释线索",
      segment_bridge: "分群一致性检查",
      outlier_scan: "异常周期检查",
      joint_attribution: "组合归因检查",
    } as Record<string, string>
  )[value] ?? value;
}

function evidenceTypeLabel(value: string) {
  return (
    {
      observed: "观测证据",
      derived: "派生证据",
      statistical_association: "统计关联证据",
      boundary: "边界证据",
    } as Record<string, string>
  )[value] ?? value;
}

function evidenceExecutionLabel(value: TraceAnswer["evidence"][number]["executionState"]) {
  return (
    {
      available: "可用证据",
      unavailable: "不可用边界记录",
      integrity_failed: "完整性失败",
      technical_failed: "技术失败",
    } satisfies Record<TraceAnswer["evidence"][number]["executionState"], string>
  )[value];
}

function evidenceBindingLabel(value: TraceAnswer["evidence"][number]["bindingState"]) {
  return value === "bound" ? "执行绑定已闭合" : "执行绑定未闭合";
}
