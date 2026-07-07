"use client";

import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, Loader2, Play, RotateCcw } from "lucide-react";

import styles from "./replay.module.css";

type TodoStatus = "pending" | "in_progress" | "completed";

type ReplayTodo = { id: string; label: string };
type ReplayTool = { label: string; status: string; detail: string; audit?: unknown };
type ReplayAudit = {
  task?: string;
  node?: string;
  node_label?: string;
  model?: string;
  prompt_version?: string;
  started_at?: string;
  finished_at?: string;
  duration_ms?: number;
  node_duration_ms?: number | null;
  usage?: unknown;
  structured_output?: unknown;
  messages?: unknown;
  raw_response_content?: string;
};
type ReplayAnswer = {
  status: string;
  answerText: string;
  claims: unknown[];
  limitations: string[];
  repairPath: string;
  stats: { label: string; value: string }[];
  evidence: { capability: string; label: string; detail: string; strength: string; limitations: string[] }[];
};
type ReplayEvent =
  | {
      id: string;
      kind: "assistant";
      todoId: string;
      label: string;
      node?: string;
      text: string;
      durationMs?: number;
      startedAt?: string;
      finishedAt?: string;
      audit?: ReplayAudit;
    }
  | {
      id: string;
      kind: "tool_group";
      todoId: string;
      title: string;
      completedTitle: string;
      summary: string;
      tools: ReplayTool[];
      durationMs?: number;
      startedAt?: string;
      finishedAt?: string;
      audit?: unknown;
    }
  | {
      id: string;
      kind: "answer";
      todoId: string;
      durationMs?: number;
      startedAt?: string;
      finishedAt?: string;
      answer: ReplayAnswer;
    };

type Replay = {
  id: string;
  label: string;
  question: string;
  expectedStatus: string;
  status: string;
  runId: string;
  generatedAt?: number;
  todos: ReplayTodo[];
  events: ReplayEvent[];
  timing: {
    actualDurationMs: number;
    playbackDurationMs: number;
  };
  processSummary: {
    checkpointCount: number;
    llmCallCount: number;
    acceptedGraph: string[];
    verifierStatus: string;
    sourceArtifact?: string;
    debugStage?: string;
  };
};

type ReplayResponse = {
  replays: Replay[];
};

export default function Phase4ReplayPage() {
  const [replays, setReplays] = useState<Replay[]>([]);
  const [activeId, setActiveId] = useState("");
  const [visibleCount, setVisibleCount] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const timers = useRef<number[]>([]);
  const listRef = useRef<HTMLDivElement>(null);

  const active = replays.find((replay) => replay.id === activeId);
  const visibleEvents = active?.events.slice(0, visibleCount) ?? [];
  const done = Boolean(active && visibleCount >= active.events.length);
  const todoStatuses = active ? buildTodoStatuses(active.todos, visibleEvents, done) : [];

  useEffect(() => {
    void loadReplays();
    return () => clearTimers();
  }, []);

  useEffect(() => {
    scrollToBottom(playing ? "smooth" : "auto");
  }, [activeId, visibleCount, playing]);

  async function loadReplays() {
    setLoading(true);
    try {
      const response = await fetch("/api/replays", { cache: "no-store" });
      if (!response.ok) throw new Error(`replay_api_${response.status}`);
      const data = (await response.json()) as ReplayResponse;
      setReplays(data.replays);
      if (data.replays[0]) startReplay(data.replays[0]);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "replay_api_failed");
    } finally {
      setLoading(false);
    }
  }

  function clearTimers() {
    timers.current.forEach((timer) => window.clearTimeout(timer));
    timers.current = [];
  }

  function startReplay(replay = active) {
    if (!replay) return;
    clearTimers();
    setActiveId(replay.id);
    setVisibleCount(0);
    setPlaying(true);
    const schedule = buildPlaybackSchedule(replay.events);
    schedule.forEach((time, index) => {
      timers.current.push(window.setTimeout(() => setVisibleCount(index + 1), time));
    });
    timers.current.push(window.setTimeout(() => setPlaying(false), schedule.at(-1) ?? 0));
  }

  function showFinal(replay = active) {
    if (!replay) return;
    clearTimers();
    setActiveId(replay.id);
    setVisibleCount(replay.events.length);
    setPlaying(false);
    scrollToBottom("auto");
  }

  function scrollToBottom(behavior: ScrollBehavior) {
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        const list = listRef.current;
        if (!list) return;
        list.scrollTo({ top: list.scrollHeight, behavior });
      });
    });
  }

  return (
    <main className={styles.shell}>
      <aside className={styles.sidebar}>
        <div className={styles.brand}>WAJE BI v2</div>
        <p className={styles.sidebarTitle}>Phase 4 真实运行回放</p>
        <nav className={styles.caseList}>
          {replays.map((replay) => (
            <button
              className={replay.id === activeId ? styles.activeCase : ""}
              key={replay.id}
              onClick={() => startReplay(replay)}
              type="button"
            >
              <span>{replay.label}</span>
              <small>{statusLabel(replay.status)} · {replay.processSummary.debugStage ?? "回放"}</small>
            </button>
          ))}
        </nav>
      </aside>

      <section className={styles.chat}>
        <header className={styles.header}>
          <div>
            <strong>{active?.label ?? "Phase 4 replay"}</strong>
            <span>{active ? `${statusLabel(active.status)} · ${active.processSummary.debugStage ?? "调试记录"} · ${active.processSummary.sourceArtifact ?? "artifact"}` : "加载真实 artifact"}</span>
          </div>
          <div className={styles.headerActions}>
            <button disabled={!active || playing} onClick={() => startReplay()} type="button">
              <RotateCcw size={14} />
              重放
            </button>
            <button disabled={!active} onClick={() => showFinal()} type="button">
              <Play size={14} />
              最终结果
            </button>
          </div>
        </header>

        <div className={styles.messageList} ref={listRef}>
          {loading ? <p className={styles.empty}>正在加载回放 artifact...</p> : null}
          {error ? <p className={styles.error}>{error}</p> : null}
          {active ? (
            <>
              <UserQuestion text={active.question} />
              <RunBadge active={active} done={done} playing={playing} visible={visibleCount} />
              {visibleEvents.map((event) => {
                if (event.kind === "assistant") return <AssistantMessage event={event} key={event.id} />;
                if (event.kind === "tool_group") return <ToolGroup event={event} key={event.id} />;
                return <AnswerCard answer={event.answer} key={event.id} />;
              })}
            </>
          ) : null}
        </div>

        <div className={styles.bottomPanel}>
          {active ? <TodoWorkbench todos={todoStatuses} /> : null}
        </div>
      </section>
    </main>
  );
}

function UserQuestion({ text }: { text: string }) {
  return (
    <div className={styles.userMessage}>
      <div className={styles.userBubble}>{text}</div>
    </div>
  );
}

function RunBadge({
  active,
  done,
  playing,
  visible,
}: {
  active: Replay;
  done: boolean;
  playing: boolean;
  visible: number;
}) {
  return (
    <div className={styles.runBadge}>
      {playing ? <Loader2 className={styles.spin} size={13} /> : <Check size={13} />}
      <span>
          {done
          ? `回放完成 · ${active.processSummary.verifierStatus === "passed" ? "已校验" : "待校验"} · 真实耗时 ${formatMs(active.timing.actualDurationMs)}`
          : `回放中 ${visible}/${active.events.length} · 压缩 ${formatMs(active.timing.playbackDurationMs)} / 真实 ${formatMs(active.timing.actualDurationMs)}`}
      </span>
    </div>
  );
}

function AssistantMessage({ event }: { event: Extract<ReplayEvent, { kind: "assistant" }> }) {
  return (
    <section className={styles.assistantMessage}>
      <div className={styles.messageHeader}>
        <strong>{event.label}</strong>
        <small>{event.durationMs ? `模型判断 · ${formatMs(event.durationMs)}` : "模型判断"}</small>
      </div>
      <p>{event.text}</p>
      {event.audit ? <AuditDetails audit={event.audit} label="详情" /> : null}
    </section>
  );
}

function ToolGroup({ event }: { event: Extract<ReplayEvent, { kind: "tool_group" }> }) {
  return (
    <details className={styles.toolGroup}>
      <summary>
        <span className={styles.stepIcon}>
          <Check size={12} />
        </span>
        <div>
          <strong>{event.completedTitle}</strong>
          <small>{event.summary}{event.durationMs ? ` · ${formatMs(event.durationMs)}` : ""}</small>
        </div>
        <ChevronDown className={styles.chevron} size={14} />
      </summary>
      <div className={styles.nestedTools}>
        {event.tools.map((tool) => (
          <details className={styles.nestedTool} key={`${tool.label}-${tool.detail}`}>
            <summary>
              <span>{tool.status === "completed" ? <Check size={11} /> : null}</span>
              <strong>{tool.label}</strong>
              <small>{tool.detail}</small>
            </summary>
            {tool.audit ? <pre>{jsonPretty(tool.audit)}</pre> : null}
          </details>
        ))}
      </div>
      {event.audit ? <AuditDetails audit={event.audit} label="工具审计" /> : null}
    </details>
  );
}

function AuditDetails({ audit, label = "LLM 结构化输出" }: { audit: unknown; label?: string }) {
  return (
    <details className={styles.auditDetails}>
      <summary>{label}</summary>
      <pre>{jsonPretty(audit)}</pre>
    </details>
  );
}

function AnswerCard({ answer }: { answer: ReplayAnswer }) {
  return (
    <article className={styles.answer}>
      <div className={styles.answerHeader}>
        <strong>{answer.status === "passed" ? "业务结论" : "降级结果"}</strong>
        <span className={answer.status === "passed" ? styles.passBadge : styles.degradedBadge}>{statusLabel(answer.status)}</span>
      </div>
      <div className={styles.answerText}>
        {answer.answerText.split(/\n+/).filter(Boolean).map((paragraph) => (
          <p key={paragraph}>{paragraph}</p>
        ))}
      </div>
      <div className={styles.stats}>
        {answer.stats.map((stat) => (
          <span key={stat.label}>
            <strong>{stat.value}</strong>
            <small>{stat.label}</small>
          </span>
        ))}
      </div>
      {answer.limitations.length ? (
        <section className={styles.limitations}>
          <h3>限制</h3>
          <div>
            {answer.limitations.map((item) => (
              <span key={item}>{item}</span>
            ))}
          </div>
        </section>
      ) : null}
      {answer.repairPath ? (
        <section className={styles.repair}>
          <h3>修复路径</h3>
          <p>{answer.repairPath}</p>
        </section>
      ) : null}
      <section className={styles.evidence}>
        <h3>证据路径</h3>
        {answer.evidence.map((item) => (
          <div key={item.capability}>
            <strong>{item.label || item.capability}</strong>
            <small>{item.detail || [strengthLabel(item.strength), ...item.limitations].filter(Boolean).join(" · ")}</small>
          </div>
        ))}
      </section>
    </article>
  );
}

function TodoWorkbench({ todos }: { todos: { id: string; label: string; status: TodoStatus }[] }) {
  const active = todos.find((todo) => todo.status === "in_progress");
  const completed = todos.filter((todo) => todo.status === "completed").length;
  return (
    <section className={styles.todoTool}>
      <div className={styles.todoSummary}>
        <span>
          {active ? <Loader2 className={styles.spin} size={13} /> : <Check size={13} />}
          {active ? `执行中 · ${active.label}` : "执行完成"}
        </span>
        <small>
          {completed}/{todos.length}
        </small>
      </div>
      <div className={styles.todoRows}>
        {todos.map((todo) => (
          <div className={`${styles.todoRow} ${styles[todo.status]}`} key={todo.id}>
            <span>{todo.status === "completed" ? <Check size={11} /> : todo.status === "in_progress" ? <Loader2 className={styles.spin} size={11} /> : null}</span>
            <p>{todo.label}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

function buildTodoStatuses(todos: ReplayTodo[], events: ReplayEvent[], done: boolean) {
  const seen = new Set(events.map((event) => event.todoId));
  const current = events.at(-1)?.todoId;
  return todos.map((todo) => ({
    ...todo,
    status: done || (seen.has(todo.id) && todo.id !== current) ? "completed" : todo.id === current ? "in_progress" : "pending",
  })) as { id: string; label: string; status: TodoStatus }[];
}

function jsonPretty(value: unknown) {
  return JSON.stringify(value, null, 2);
}

function buildPlaybackSchedule(events: ReplayEvent[]) {
  if (!events.length) return [];
  const actualTotal = events.reduce((total, event) => total + durationMs(event), 0);
  if (actualTotal <= 0) {
    const fallbackStep = 10000 / events.length;
    return events.map((_, index) => Math.round(fallbackStep * (index + 1)));
  }
  const scale = actualTotal > 10000 ? 10000 / actualTotal : 1;
  let elapsed = 0;
  return events.map((event) => {
    elapsed += durationMs(event) * scale;
    return Math.round(elapsed);
  });
}

function durationMs(event: ReplayEvent) {
  const value = Number(event.durationMs ?? 0);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

function formatMs(value: number) {
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${Math.round(value)}ms`;
}

function statusLabel(value: string) {
  return (
    {
      passed: "已通过",
      degraded: "已降级",
      blocked: "已阻断",
      failed: "失败",
    } as Record<string, string>
  )[value] ?? value;
}

function strengthLabel(value: string) {
  return (
    {
      high: "证据强度 强",
      medium: "证据强度 中",
      low: "证据强度 弱",
      insufficient: "证据不足",
      unknown: "证据强度未知",
    } as Record<string, string>
  )[value] ?? value;
}
