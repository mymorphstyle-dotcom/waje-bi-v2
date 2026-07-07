"use client";

import { type FormEvent, useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { Check, ChevronDown, Loader2, Play, RotateCcw, Send, Workflow } from "lucide-react";

import type { TraceAnswer, TraceCard, TraceMessage, TraceNode, TraceRun } from "./contracts";
import styles from "../phase4-replay/replay.module.css";

type TodoStatus = "pending" | "in_progress" | "completed";
type RunsResponse = { runs: TraceRun[] };

const WorkflowCanvasModal = dynamic(() => import("./WorkflowCanvasModal").then((module) => module.WorkflowCanvasModal), {
  ssr: false,
});

export function AgentRunWorkbench({ deprecated = false }: { deprecated?: boolean }) {
  const [runs, setRuns] = useState<TraceRun[]>([]);
  const [activeId, setActiveId] = useState("");
  const [visibleCount, setVisibleCount] = useState(0);
  const [selectedNodeId, setSelectedNodeId] = useState("");
  const [playing, setPlaying] = useState(false);
  const [canvasOpen, setCanvasOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const timers = useRef<number[]>([]);
  const messageListRef = useRef<HTMLDivElement>(null);

  const active = runs.find((run) => run.id === activeId);
  const nodes = active?.processSummary.nodes ?? [];
  const visibleNodes = nodes.slice(0, visibleCount);
  const done = Boolean(active && visibleCount >= nodes.length);
  const visibleMessages = active ? messagesForVisibleRun(active, visibleNodes, done) : [];
  const todos = active ? buildTodoStatuses(visibleNodes, done) : [];

  useEffect(() => {
    void loadRuns();
    return () => clearTimers();
  }, []);

  useEffect(() => {
    messageListRef.current?.scrollTo({ top: messageListRef.current.scrollHeight, behavior: "smooth" });
  }, [activeId, done, visibleCount]);

  async function loadRuns() {
    setLoading(true);
    try {
      const response = await fetch("/api/agent-runs", { cache: "no-store" });
      if (!response.ok) throw new Error(`agent_runs_api_${response.status}`);
      const data = (await response.json()) as RunsResponse;
      setRuns(data.runs);
      if (data.runs[0]) startRun(data.runs[0]);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "agent_runs_api_failed");
    } finally {
      setLoading(false);
    }
  }

  function clearTimers() {
    timers.current.forEach((timer) => window.clearTimeout(timer));
    timers.current = [];
  }

  function startRun(run = active) {
    if (!run) return;
    clearTimers();
    setActiveId(run.id);
    setSelectedNodeId("");
    setVisibleCount(0);
    setPlaying(true);
    const schedule = buildPlaybackSchedule(run.processSummary.nodes);
    schedule.forEach((time, index) => {
      timers.current.push(
        window.setTimeout(() => {
          setVisibleCount(index + 1);
          setSelectedNodeId(run.processSummary.nodes[index]?.id ?? "");
        }, time),
      );
    });
    timers.current.push(window.setTimeout(() => setPlaying(false), schedule.at(-1) ?? 0));
  }

  function showFinal(run = active) {
    if (!run) return;
    clearTimers();
    setActiveId(run.id);
    setVisibleCount(run.processSummary.nodes.length);
    setSelectedNodeId(run.processSummary.nodes.at(-1)?.id ?? "");
    setPlaying(false);
  }

  return (
    <main className={styles.shell}>
      <section className={styles.chat}>
        <header className={styles.header}>
          <div className={styles.headerTitle}>
            <span className={styles.brandInline}>WAJE BI v2</span>
            <strong>{active?.label ?? "Agent Run Workbench"}</strong>
            <span>
              {active
                ? `${active.processSummary.checkpointCount} 个节点 · ${active.processSummary.llmCallCount} 次模型判断 · 真实运行记录`
                : "加载真实运行记录"}
            </span>
          </div>
          <div className={styles.headerActions}>
            <label className={styles.runSelect}>
              <span>运行</span>
              <select
                disabled={!runs.length}
                onChange={(event) => {
                  const run = runs.find((item) => item.id === event.target.value);
                  if (run) startRun(run);
                }}
                value={activeId}
              >
                {runs.map((run) => (
                  <option key={run.id} value={run.id}>
                    {run.label.replace(" · 完整运行", "")}
                  </option>
                ))}
              </select>
            </label>
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
            <button disabled={!active || playing} onClick={() => startRun()} type="button">
              <RotateCcw size={14} />
              重放
            </button>
            <button disabled={!active} onClick={() => showFinal()} type="button">
              <Play size={14} />
              最终结果
            </button>
          </div>
        </header>

        <div className={styles.messageList} ref={messageListRef}>
          {deprecated ? (
            <div className={styles.deprecatedNotice}>这个入口保留兼容；新的审计工作台是 /agent-run-workbench。</div>
          ) : null}
          {loading ? <p className={styles.empty}>正在加载真实 run...</p> : null}
          {error ? <p className={styles.error}>{error}</p> : null}
          {active ? (
            <>
              <RunOverview run={active} />
              <RunBadge active={active} done={done} playing={playing} visible={visibleCount} />
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
              {done && active.answer ? <AnswerCard answer={active.answer} cards={active.summaryCards} /> : null}
            </>
          ) : null}
        </div>

        <div className={styles.bottomPanel}>
          {active ? (
            <div className={styles.bottomStack}>
              <TodoWorkbench todos={todos} />
              <Composer disabled={!active} />
            </div>
          ) : null}
        </div>
      </section>
      {active && canvasOpen ? (
        <WorkflowCanvasModal
          debugAudit={Boolean(active.processSummary.debugAudit)}
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
  const graph = run.processSummary.acceptedGraph.map(capabilityLabel).join("、") || "无";
  return (
    <section className={styles.runOverview}>
      <div className={styles.questionBlock}>
        <small>用户问题</small>
        <p>{run.question}</p>
      </div>
      <div className={styles.summaryCards}>
        {(run.businessThreads ?? []).map((card) => (
          <SummaryTile card={card} key={`${card.label}-${card.value}`} />
        ))}
        {run.summaryCards.map((card) => (
          <SummaryTile card={card} key={`${card.label}-${card.value}`} />
        ))}
      </div>
      <p className={styles.runNarrative}>
        本轮分析先把问题绑定成可执行业务目标，再让模型设计分析路径，本地系统验收为 {graph}。
        后续节点依次完成数据边界检查、证据生成、业务解释、审计修复和最终总结。
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

function RunBadge({ active, done, playing, visible }: { active: TraceRun; done: boolean; playing: boolean; visible: number }) {
  const total = active.processSummary.nodes.length;
  return (
    <div className={styles.runBadge}>
      {playing ? <Loader2 className={styles.spin} size={13} /> : <Check size={13} />}
      <span>
        {done
          ? `路径回放完成 · ${active.processSummary.verifierStatus === "passed" ? "答案已校验" : "答案待复核"} · 真实耗时 ${formatMs(active.timing.actualDurationMs)}`
          : `回放中 ${visible}/${total} · 压缩 ${formatMs(active.timing.playbackDurationMs)} / 真实 ${formatMs(active.timing.actualDurationMs)}`}
      </span>
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

function PathTimeline({
  nodes,
  selectedNodeId,
  onSelectNode,
}: {
  nodes: TraceNode[];
  selectedNodeId?: string;
  onSelectNode: (nodeId: string) => void;
}) {
  return (
    <section className={styles.pathMini}>
      <div className={styles.sectionHeader}>
        <h2>节点路径</h2>
        <p>业务判断、证据动作和耗时按真实顺序排列。</p>
      </div>
      <div className={styles.nodeList}>
        {nodes.map((node) => (
          <TraceNodeRow
            key={node.id}
            node={node}
            selected={node.id === selectedNodeId}
            onSelect={() => onSelectNode(node.id)}
          />
        ))}
      </div>
    </section>
  );
}

function TraceNodeRow({ node, selected, onSelect }: { node: TraceNode; selected: boolean; onSelect: () => void }) {
  return (
    <button className={`${styles.nodeCard} ${selected ? styles.selectedNode : ""}`} onClick={onSelect} type="button">
      <span className={styles.nodeRail}>{node.index}</span>
      <span className={styles.nodeBody}>
        <span className={styles.nodeHeader}>
          <span>
            <strong>{node.label}</strong>
            <small>{node.summary}</small>
          </span>
          <span className={styles.nodeBadges}>
            <span className={node.owner === "LLM" ? styles.llmBadge : styles.localBadge}>{node.owner === "LLM" ? "模型" : "系统"}</span>
            {node.route ? <span>{node.route}</span> : null}
            <span>{formatMs(node.durationMs ?? 0)}</span>
          </span>
        </span>
      </span>
    </button>
  );
}

function NodeInspector({ node, answer, debugAudit }: { node?: TraceNode; answer?: TraceAnswer; debugAudit: boolean }) {
  if (!node) {
    return (
      <aside className={styles.inspector}>
        <strong>节点详情</strong>
        <p>选择一个节点查看业务判断和审计输出。</p>
      </aside>
    );
  }
  return (
    <aside className={styles.inspector}>
      <div className={styles.inspectorHeader}>
        <small>节点 {node.index}</small>
        <strong>{node.label}</strong>
        <span>{node.owner === "LLM" ? "模型参与" : "本地系统执行"} · {formatMs(node.durationMs ?? 0)}</span>
      </div>
      <section>
        <h3>这个节点做了什么</h3>
        <p>{node.summary}</p>
      </section>
      {node.route ? (
        <section>
          <h3>分支</h3>
          <p>{node.route}</p>
        </section>
      ) : null}
      {answer ? (
        <section>
          <h3>最终回答</h3>
          <p>{answer.answerText.split(/\n+/).find(Boolean) ?? answer.answerText}</p>
        </section>
      ) : null}
      {debugAudit && node.audit ? <AuditDetails audit={node.audit} label="结构化审计" /> : null}
    </aside>
  );
}

function AuditDetails({ audit, label }: { audit: unknown; label: string }) {
  return (
    <details className={styles.auditDetails}>
      <summary>
        {label}
        <ChevronDown size={13} />
      </summary>
      <pre>{jsonPretty(audit)}</pre>
    </details>
  );
}

function AnswerCard({ answer, cards }: { answer: TraceAnswer; cards: TraceCard[] }) {
  return (
    <article className={styles.answer}>
      <div className={styles.answerHeader}>
        <strong>{answer.status === "passed" ? "最终业务回答" : "有边界的业务回答"}</strong>
        <span className={answer.status === "passed" ? styles.passBadge : styles.degradedBadge}>{statusLabel(answer.status)}</span>
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
      <section className={styles.evidence}>
        <h3>证据路径</h3>
        {answer.evidence.map((item) => (
          <div key={item.capability}>
            <strong>{item.label || item.capability}</strong>
            <small>{item.detail}</small>
          </div>
        ))}
      </section>
      {answer.visualBlocks.length ? (
        <section className={styles.evidence}>
          <h3>可视化计划</h3>
          {answer.visualBlocks.map((block) => (
            <div key={block.id}>
              <strong>{block.title}</strong>
              <small>
                {block.claimText}
                {block.evidenceRefs.length ? ` · 证据 ${block.evidenceRefs.join("、")}` : ""}
                {block.limitations.length ? ` · 边界 ${block.limitations.join("、")}` : ""}
              </small>
            </div>
          ))}
        </section>
      ) : null}
    </article>
  );
}

function TodoWorkbench({ todos }: { todos: { id: string; label: string; status: TodoStatus }[] }) {
  const active = todos.find((todo) => todo.status === "in_progress");
  const completed = todos.filter((todo) => todo.status === "completed").length;
  return (
    <details className={styles.todoTool}>
      <summary className={styles.todoSummary}>
        <span>
          {active ? <Loader2 className={styles.spin} size={13} /> : <Check size={13} />}
          {active ? `当前阶段 · ${active.label}` : "全链路已完成"}
        </span>
        <small>{completed}/{todos.length}</small>
        <ChevronDown className={styles.todoChevron} size={14} />
      </summary>
      <div className={styles.todoRows}>
        {todos.map((todo) => (
          <div className={`${styles.todoRow} ${styles[todo.status]}`} key={todo.id}>
            <span>{todo.status === "completed" ? <Check size={11} /> : todo.status === "in_progress" ? <Loader2 className={styles.spin} size={11} /> : null}</span>
            <p>{todo.label}</p>
          </div>
        ))}
      </div>
    </details>
  );
}

function Composer({ disabled }: { disabled: boolean }) {
  const [text, setText] = useState("");
  const [notice, setNotice] = useState("");
  const canSend = Boolean(text.trim()) && !disabled;

  function submitComposer(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSend) return;
    setNotice("已记录；当前工作台只回放已生成的真实运行。");
  }

  return (
    <form className={styles.composer} onSubmit={submitComposer}>
      <textarea
        aria-label="输入业务问题"
        disabled={disabled}
        onChange={(event) => {
          setText(event.target.value);
          if (notice) setNotice("");
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            event.currentTarget.form?.requestSubmit();
          }
        }}
        placeholder="继续追问这次分析..."
        rows={1}
        value={text}
      />
      <button aria-label="发送" disabled={!canSend} type="submit">
        <Send size={15} />
      </button>
      {notice ? <small className={styles.composerNotice}>{notice}</small> : null}
    </form>
  );
}

function buildTodoStatuses(nodes: TraceNode[], done: boolean) {
  const todos: { id: string; label: string }[] = [
    { id: "intent", label: "理解业务问题和边界" },
    { id: "route", label: "设计并验收分析路径" },
    { id: "data", label: "确认数据口径和安全" },
    { id: "capability", label: "执行证据路径" },
    { id: "answer", label: "生成并审计答案" },
  ];
  const seen = new Set<string>(nodes.map((node) => stageForNode(node.node)));
  const current = stageForNode(nodes.at(-1)?.node ?? "");
  return todos.map((todo) => ({
    ...todo,
    status: done || (seen.has(todo.id) && todo.id !== current) ? "completed" : todo.id === current ? "in_progress" : "pending",
  })) as { id: string; label: string; status: TodoStatus }[];
}

function stageForNode(node: string) {
  if (["run_status", "question_tool", "understand_business_intent", "decide_question_boundary", "confirm_business_understanding", "clarification_policy_gate"].includes(node)) return "intent";
  if (["design_analysis_route", "accept_analysis_route"].includes(node)) return "route";
  if (["inspect_schema", "validate_runtime_binding", "interpret_data_coverage"].includes(node)) return "data";
  if (["execute_capabilities", "reduce_evidence"].includes(node)) return "capability";
  return "answer";
}

function caseLine(run: TraceRun) {
  const first = run.summaryCards[0];
  const second = run.summaryCards[1];
  return [statusLabel(run.status), first ? `${first.label} ${first.value}` : "", second ? `${second.label} ${second.value}` : ""]
    .filter(Boolean)
    .join(" · ");
}

function messagesForVisibleRun(run: TraceRun, nodes: TraceNode[], done: boolean) {
  const visibleNodeIds = new Set(nodes.map((node) => node.id));
  return (run.messages ?? []).filter((message) => {
    if (message.id === "user-question") return true;
    if (message.id === "final-answer") return done;
    return message.nodeId ? visibleNodeIds.has(message.nodeId) : false;
  });
}

function buildPlaybackSchedule(nodes: TraceNode[]) {
  if (!nodes.length) return [];
  const actualTotal = nodes.reduce((total, node) => total + durationMs(node), 0);
  if (actualTotal <= 0) return nodes.map((_, index) => index + 1);
  const scale = actualTotal > 10000 ? 10000 / actualTotal : 1;
  let elapsed = 0;
  return nodes.map((node) => {
    elapsed += durationMs(node) * scale;
    return Math.round(elapsed);
  });
}

function durationMs(node: TraceNode) {
  const value = Number(node.durationMs ?? 0);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

function jsonPretty(value: unknown) {
  return JSON.stringify(value, null, 2);
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
      driver_decomposition: "贡献拆解",
      segment_contribution: "分群贡献拆解",
      outlier_contribution: "异常贡献拆解",
      event_evidence: "事件解释线索",
      segment_bridge: "分群一致性检查",
      outlier_scan: "异常周期检查",
      joint_attribution: "组合归因检查",
      answer_verify: "答案边界校验",
    } as Record<string, string>
  )[value] ?? value;
}
