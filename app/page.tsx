"use client";

import { useEffect, useRef, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  Check,
  ChevronDown,
  FileSpreadsheet,
  Plus,
  Send,
  X,
} from "lucide-react";

const DEFAULT_QUESTION =
  "全量样本看帮我分析一下，为什么从 2024 年 1 月开始到 2026 年 5 月结束，每个月月初的付费金额都比月中月末高一些";

const attachment = {
  id: "paid-order-template",
  filename: "付费订单明细模板.xlsx",
  size: 184_320,
};

const chartColors = {
  start: "#74A9D8",
  rest: "#6F747D",
  contribution: "#75AD8E",
  grid: "#262A30",
};

const axisTick = { fill: "var(--muted)", fontSize: 11 };
const categoryTick = { fill: "var(--muted)", fontSize: 12 };
const tooltipProps = {
  contentStyle: {
    background: "#202125",
    border: "1px solid #37383d",
    borderRadius: 10,
    color: "#eeeeef",
    fontSize: 12,
    lineHeight: 1.45,
    padding: "8px 10px",
  },
  itemStyle: { color: "#eeeeef", fontSize: 12 },
  labelStyle: { color: "#eeeeef", fontSize: 12, marginBottom: 4 },
};

type TodoStatus = "pending" | "in_progress" | "completed";
type ToolStatus = "pending" | "running" | "completed";
type ToolGroupState = "running" | "completed";
type RunClockState = "idle" | "queued" | "running" | "completed";

type Todo = { id: string; label: string; status: TodoStatus };
type NestedTool = { id: string; label: string; status: ToolStatus };
type RunClock = {
  state: RunClockState;
  elapsedMs: number;
};
type ToolGroupRun = {
  id: string;
  state: ToolGroupState;
  title: string;
  completedTitle: string;
  summary: string;
  elapsedMs: number;
  nestedTools: NestedTool[];
};
type QuestionRun = {
  id: string;
  title: string;
  body: string;
  options: string[];
  selected?: string;
};
type WorkbenchState = {
  todos: Todo[];
  activeQuestion?: QuestionRun;
};
type Message =
  | { id: string; kind: "assistant"; text: string }
  | { id: string; kind: "tool_group"; group: ToolGroupRun }
  | { id: string; kind: "answer" };
type UserMessage = {
  text: string;
  attachment?: typeof attachment;
};
type ToolGroupTemplate = {
  id: string;
  todoId: string;
  title: string;
  completedTitle: string;
  summary: string;
  tools: string[];
};

const emptyWorkbench: WorkbenchState = {
  todos: [],
};

const idleRunClock: RunClock = {
  state: "idle",
  elapsedMs: 0,
};

const todoSeed: Todo[] = [
  { id: "intent", label: "识别分析意图", status: "pending" },
  { id: "pattern", label: "证明月内模式是否存在", status: "pending" },
  { id: "formula", label: "拆解付费金额公式", status: "pending" },
  { id: "candidate", label: "扫描候选业务解释", status: "pending" },
  { id: "joint", label: "组合归因与升维", status: "pending" },
  { id: "verify", label: "校验答案边界", status: "pending" },
];

const toolGroups: Record<string, ToolGroupTemplate> = {
  pattern: {
    id: "pattern",
    todoId: "pattern",
    title: "正在计算月内周期模式",
    completedTitle: "月内周期模式计算完成",
    summary: "25/29 个月成立，剔除异常月份后仍成立",
    tools: [
      "bucket_payment_by_month_position",
      "compare_month_position_lift",
      "remove_calendar_outliers",
    ],
  },
  formula: {
    id: "formula",
    todoId: "formula",
    title: "正在拆解付费金额公式",
    completedTitle: "付费金额公式拆解完成",
    summary: "成功订单数解释最大，支付成功率和单笔金额是放大项",
    tools: ["compile_formula_tree", "run_contribution_decompose"],
  },
  candidate: {
    id: "candidate",
    todoId: "candidate",
    title: "正在并行扫描候选解释",
    completedTitle: "候选解释扫描完成",
    summary: "发薪窗口、新老用户结构、渠道结构进入高相关候选",
    tools: [
      "payday_window_fit",
      "user_type_mix_scan",
      "channel_mix_scan",
      "holiday_activity_gap_check",
      "outlier_month_review",
    ],
  },
  joint: {
    id: "joint",
    todoId: "joint",
    title: "正在计算组合归因",
    completedTitle: "组合归因计算完成",
    summary: "pay_window × user_type × channel 的解释力高于任意单因子",
    tools: ["rank_single_factor_fit", "rank_joint_factor_fit", "residual_check"],
  },
  verify: {
    id: "verify",
    todoId: "verify",
    title: "正在校验答案边界",
    completedTitle: "答案边界校验完成",
    summary: "+18.9%、25/29、54% 已校验，活动因素降级表达",
    tools: ["claim_number_check", "evidence_strength_check", "wording_boundary_check"],
  },
};

const patternData = [
  { month: "24-01", start: 112, rest: 96 },
  { month: "24-04", start: 119, rest: 101 },
  { month: "24-07", start: 116, rest: 99 },
  { month: "24-10", start: 124, rest: 105 },
  { month: "25-01", start: 128, rest: 107 },
  { month: "25-04", start: 121, rest: 103 },
  { month: "25-07", start: 126, rest: 108 },
  { month: "25-10", start: 132, rest: 111 },
  { month: "26-01", start: 135, rest: 112 },
  { month: "26-05", start: 129, rest: 110 },
];

const contributionData = [
  { name: "成功订单数", value: 54 },
  { name: "支付成功率", value: 19 },
  { name: "单笔金额", value: 14 },
  { name: "渠道结构", value: 9 },
  { name: "未解释残差", value: 4 },
];

function wait(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function formatFileSize(bytes: number) {
  return `${(bytes / 1024).toFixed(1)} KB`;
}

function formatDuration(ms: number) {
  const totalSeconds = Math.floor(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

function SpiralLoader() {
  return <span className="spiral-loader" aria-hidden="true" />;
}

function RunClockBadge({ clock }: { clock: RunClock }) {
  const label =
    clock.state === "queued"
      ? "已提交"
      : clock.state === "running"
        ? `已处理 ${formatDuration(clock.elapsedMs)}`
        : clock.state === "completed"
          ? `已处理 ${formatDuration(clock.elapsedMs)}`
          : "verifier ready";

  return <span className={`run-clock ${clock.state}`}>{label} ›</span>;
}

function AttachmentButton({ onClick }: { onClick?: () => void }) {
  return (
    <button className="attachment-button" type="button" aria-label="Attach" onClick={onClick}>
      <Plus size={16} />
    </button>
  );
}

function FileAttachment({ removable = false, onRemove }: { removable?: boolean; onRemove?: () => void }) {
  return (
    <div className="file-attachment">
      <span className="file-icon">
        <FileSpreadsheet size={15} />
      </span>
      <span className="file-copy">
        <strong>{attachment.filename}</strong>
        <small>{formatFileSize(attachment.size)}</small>
      </span>
      {removable ? (
        <button type="button" aria-label="Remove attachment" onClick={onRemove}>
          <X size={12} />
        </button>
      ) : null}
    </div>
  );
}

function UserMessageBubble({ message }: { message: UserMessage }) {
  return (
    <div className="user-message">
      {message.attachment ? <FileAttachment /> : null}
      <div className="user-bubble">{message.text}</div>
    </div>
  );
}

function AssistantText({ children }: { children: string }) {
  return <p className="assistant-text">{children}</p>;
}

function TodoToolBlock({ todos, blocked }: { todos: Todo[]; blocked: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const activeIndex = todos.findIndex((todo) => todo.status === "in_progress");
  const activeTodo = activeIndex >= 0 ? todos[activeIndex] : undefined;
  const completedCount = todos.filter((todo) => todo.status === "completed").length;
  const hasTodos = todos.length > 0;
  const nextSummary = !hasTodos
    ? ""
    : blocked
      ? "需要确认 · 表达边界"
      : activeTodo
        ? `执行中 · ${activeTodo.label}`
      : completedCount === todos.length
        ? "执行完成"
        : "";
  const summaryText = expanded ? "执行清单" : nextSummary;
  const [ticker, setTicker] = useState({ current: summaryText, previous: "" });
  const isRunningSummary = Boolean(activeTodo && !blocked && !expanded);

  useEffect(() => {
    if (!summaryText) return;

    setTicker((current) => (current.current === summaryText ? current : { current: summaryText, previous: current.current }));
    const timeout = window.setTimeout(() => {
      setTicker((current) => (current.current === summaryText ? { ...current, previous: "" } : current));
    }, 240);

    return () => window.clearTimeout(timeout);
  }, [summaryText]);

  if (!todos.length) return null;

  return (
    <section className={`todo-tool ${expanded ? "expanded" : "collapsed"}`}>
      <button className="todo-summary" type="button" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
        <span className="todo-current">
          {!expanded && activeTodo && !blocked ? <SpiralLoader /> : null}
          <span className={`todo-current-viewport ${ticker.previous ? "rolling" : ""}`}>
            {ticker.previous ? (
              <span className="todo-current-line previous">
                <span className="todo-current-copy">{ticker.previous}</span>
              </span>
            ) : null}
            <span className="todo-current-line current">
              <span className={isRunningSummary ? "todo-current-copy text-shimmer" : "todo-current-copy"}>{ticker.current}</span>
            </span>
          </span>
        </span>
        <small className="todo-count">{completedCount}/{todos.length}</small>
        <ChevronDown className={expanded ? "expanded" : ""} size={14} />
      </button>
      {expanded
        ? todos.map((todo) => (
            <div className={`todo-row ${todo.status}`} key={todo.id}>
              <span>{todo.status === "in_progress" ? <SpiralLoader /> : null}</span>
              <p>{todo.label}</p>
            </div>
          ))
        : null}
    </section>
  );
}

function ToolGroupBlock({ group }: { group: ToolGroupRun }) {
  return (
    <details className={`tool-group ${group.state}`} {...(group.state === "running" ? { open: true } : {})}>
      <summary>
        <span>{group.state === "running" ? <SpiralLoader /> : <Check size={12} />}</span>
        <div>
          <strong>{group.state === "running" ? group.title : group.completedTitle}</strong>
          <small>{group.summary} · {(group.elapsedMs / 1000).toFixed(1)}s</small>
        </div>
        <ChevronDown className="chevron" size={14} />
      </summary>
      <div className="nested-tools">
        {group.nestedTools.map((tool) => (
          <div className={`nested-tool ${tool.status}`} key={tool.id}>
            <span>
              {tool.status === "running" ? <SpiralLoader /> : null}
              {tool.status === "completed" ? <Check size={11} /> : null}
            </span>
            <code>{tool.label}</code>
          </div>
        ))}
      </div>
    </details>
  );
}

function QuestionWorkbench({ question, onAnswer }: { question: QuestionRun; onAnswer: (value: string) => void }) {
  return (
    <section className={`workbench-question ${question.selected ? "answered" : "blocking"}`} role="alert">
      <div className="question-header">
        <strong>{question.selected ? "已确认表达边界" : question.title}</strong>
        {!question.selected ? <span>需要确认后继续</span> : null}
      </div>
      <div>
        <p>{question.body}</p>
      </div>
      <div className="question-actions">
        {question.selected ? (
          <span>{question.selected}</span>
        ) : (
          question.options.map((option) => (
            <button key={option} onClick={() => onAnswer(option)} type="button">
              {option}
            </button>
          ))
        )}
      </div>
    </section>
  );
}

function RunWorkbench({ workbench, onAnswer }: { workbench: WorkbenchState; onAnswer: (value: string) => void }) {
  const hasContent =
    workbench.todos.length > 0 ||
    Boolean(workbench.activeQuestion);

  if (!hasContent) return null;

  return (
    <div className={`run-workbench ${workbench.activeQuestion ? "question-blocked" : ""}`}>
      {workbench.activeQuestion ? <QuestionWorkbench question={workbench.activeQuestion} onAnswer={onAnswer} /> : null}
      <TodoToolBlock todos={workbench.todos} blocked={Boolean(workbench.activeQuestion)} />
    </div>
  );
}

function Answer() {
  return (
    <article className="business-answer">
      <p>
        这个模式是成立的：从 2024 年 1 月到 2026 年 5 月，月初付费金额在大多数月份都稳定高于月中和月末。更像主因的是订单数被月内周期放大，单笔金额只解释了小部分差异。
      </p>
      <p>
        拆开公式后，成功订单数贡献最大，大约解释 54% 的差异；支付成功率和单笔金额也有帮助，但更像放大项。发薪窗口、新老用户结构和渠道结构组合后解释力最高，活动和节假日更适合解释少数异常月份。
      </p>

      <div className="answer-stats">
        <span><strong>+18.9%</strong><small>月初金额抬升</small></span>
        <span><strong>25/29</strong><small>方向一致月份</small></span>
        <span><strong>54%</strong><small>订单数贡献</small></span>
        <span><strong>中高</strong><small>证据强度</small></span>
      </div>

      <div className="answer-charts">
        <section>
          <h3>月初 vs 月中/月末</h3>
          <div className="chart-box">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={patternData}>
                <defs>
                  <linearGradient id="startFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={chartColors.start} stopOpacity={0.2} />
                    <stop offset="100%" stopColor={chartColors.start} stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke={chartColors.grid} vertical={false} />
                <XAxis dataKey="month" tickLine={false} axisLine={false} minTickGap={18} interval="preserveStartEnd" tick={axisTick} />
                <YAxis tickLine={false} axisLine={false} width={34} tick={axisTick} />
                <Tooltip {...tooltipProps} />
                <Area type="monotone" dataKey="start" name="月初" stroke={chartColors.start} fill="url(#startFill)" strokeWidth={2} />
                <Area type="monotone" dataKey="rest" name="月中/月末" stroke={chartColors.rest} fill="transparent" strokeWidth={2} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </section>
        <section>
          <h3>公式拆解贡献</h3>
          <div className="chart-box">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={contributionData} layout="vertical">
                <CartesianGrid stroke={chartColors.grid} horizontal={false} />
                <XAxis type="number" hide domain={[0, 60]} />
                <YAxis dataKey="name" type="category" tickLine={false} axisLine={false} width={92} tickMargin={8} tick={categoryTick} />
                <Tooltip {...tooltipProps} />
                <Bar dataKey="value" name="贡献占比" barSize={12} radius={[0, 4, 4, 0]} fill={chartColors.contribution} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </section>
      </div>
    </article>
  );
}

function Composer({
  value,
  hasAttachment,
  running,
  blocked,
  onChange,
  onAttach,
  onRemoveAttachment,
  onSend,
}: {
  value: string;
  hasAttachment: boolean;
  running: boolean;
  blocked: boolean;
  onChange: (value: string) => void;
  onAttach: () => void;
  onRemoveAttachment: () => void;
  onSend: () => void;
}) {
  return (
    <div className={`composer ${blocked ? "blocked" : ""}`}>
      {hasAttachment ? <FileAttachment removable onRemove={onRemoveAttachment} /> : null}
      <textarea value={value} onChange={(event) => onChange(event.target.value)} disabled={running || blocked} />
      <div className="composer-actions">
        <div>
          <AttachmentButton onClick={onAttach} />
          <button className="mode-pill" type="button">分析 <ChevronDown size={13} /></button>
          <span>{blocked ? "先回答上方问题" : "WAJE LangGraph"}</span>
        </div>
        <button className="send-button" type="button" onClick={onSend} disabled={running || blocked || !value.trim()} aria-label="Send">
          <Send size={14} />
        </button>
      </div>
    </div>
  );
}

export default function Home() {
  const [draft, setDraft] = useState(DEFAULT_QUESTION);
  const [hasDraftAttachment, setHasDraftAttachment] = useState(true);
  const [userMessage, setUserMessage] = useState<UserMessage | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [workbench, setWorkbench] = useState<WorkbenchState>(emptyWorkbench);
  const [runClock, setRunClock] = useState<RunClock>(idleRunClock);
  const [running, setRunning] = useState(false);
  const cancelled = useRef(false);
  const runClockRef = useRef<RunClock>(idleRunClock);
  const runStartedAtRef = useRef<number | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, userMessage]);

  useEffect(() => {
    if (runClock.state !== "running") return;

    const interval = window.setInterval(() => {
      setServerClock((current) => ({
        ...current,
        elapsedMs: runStartedAtRef.current ? Date.now() - runStartedAtRef.current : current.elapsedMs,
      }));
    }, 1000);

    return () => window.clearInterval(interval);
  }, [runClock.state]);

  function setServerClock(next: RunClock | ((current: RunClock) => RunClock)) {
    const resolved = typeof next === "function" ? next(runClockRef.current) : next;
    runClockRef.current = resolved;
    setRunClock(resolved);
  }

  async function serverWait(ms: number) {
    let remaining = ms;

    while (remaining > 0) {
      const slice = Math.min(remaining, 1000);
      await wait(slice);
      if (cancelled.current) return false;
      remaining -= slice;
    }

    return true;
  }

  function appendAssistant(text: string) {
    setMessages((current) => [...current, { id: crypto.randomUUID(), kind: "assistant", text }]);
  }

  function setTodoStatus(id: string, status: TodoStatus) {
    setWorkbench((current) => ({
      ...current,
      todos: current.todos.map((todo) => (todo.id === id ? { ...todo, status } : todo)),
    }));
  }

  function updateActiveGroup(group: ToolGroupRun) {
    setMessages((current) => {
      const next = { id: group.id, kind: "tool_group" as const, group };
      return current.some((message) => message.kind === "tool_group" && message.id === group.id)
        ? current.map((message) => (message.kind === "tool_group" && message.id === group.id ? next : message))
        : [...current, next];
    });
  }

  function completeActiveGroup(group: ToolGroupRun) {
    const completed = { ...group, state: "completed" as const };
    updateActiveGroup(completed);
  }

  async function runToolGroup(template: ToolGroupTemplate) {
    setTodoStatus(template.todoId, "in_progress");

    const baseTools = template.tools.map((label) => ({ id: label, label, status: "pending" as const }));
    const baseGroup = {
      id: template.id,
      state: "running" as const,
      title: template.title,
      completedTitle: template.completedTitle,
      summary: template.summary,
      elapsedMs: 0,
      nestedTools: baseTools,
    };

    updateActiveGroup(baseGroup);

    for (let index = 0; index < baseTools.length; index += 1) {
      if (cancelled.current) return;
      updateActiveGroup({
        ...baseGroup,
        elapsedMs: (index + 1) * 700,
        nestedTools: baseTools.map((tool, toolIndex) => ({
          ...tool,
          status: toolIndex < index ? "completed" : toolIndex === index ? "running" : "pending",
        })),
      });
      if (!(await serverWait(650))) return;
    }

    const completedGroup = {
      ...baseGroup,
      elapsedMs: Math.max(1200, baseTools.length * 700),
      nestedTools: baseTools.map((tool) => ({ ...tool, status: "completed" as const })),
    };
    completeActiveGroup(completedGroup);
    setTodoStatus(template.todoId, "completed");
    await serverWait(450);
  }

  async function playFlow(question: string, includeAttachment: boolean) {
    cancelled.current = false;
    setRunning(true);
    setUserMessage({ text: question, attachment: includeAttachment ? attachment : undefined });
    setMessages([]);
    setWorkbench(emptyWorkbench);
    runStartedAtRef.current = null;
    setServerClock({ state: "queued", elapsedMs: 0 });

    await wait(500);
    if (cancelled.current) return;
    runStartedAtRef.current = Date.now();
    setServerClock({ state: "running", elapsedMs: 0 });
    appendAssistant("我会按月内周期模式处理。先确认模式是否稳定存在，再做公式拆解、候选解释、组合归因，最后校验哪些结论能写强。");

    if (!(await serverWait(600))) return;
    setWorkbench({
      todos: todoSeed.map((todo) => (todo.id === "intent" ? { ...todo, status: "in_progress" } : todo)),
    });

    if (!(await serverWait(1000))) return;
    setTodoStatus("intent", "completed");
    appendAssistant("用户已经给出 2024-01 到 2026-05 的完整区间，并明确比较月初和月中/月末，因此不需要打断用户确认。");

    await runToolGroup(toolGroups.pattern);
    if (cancelled.current) return;
    appendAssistant("月初高点在 25/29 个月成立，剔除春节、长假和活动峰值月份后，方向仍然稳定。");

    await runToolGroup(toolGroups.formula);
    if (cancelled.current) return;
    appendAssistant("拆开公式后，成功订单数贡献最大；支付成功率和单笔金额有帮助，但更像放大项。");

    await runToolGroup(toolGroups.candidate);
    if (cancelled.current) return;
    appendAssistant("发薪窗口、新老用户结构和渠道结构进入高相关候选；活动事件表存在缺口，只能按候选解释处理。");

    setTodoStatus("joint", "in_progress");
    if (!(await serverWait(900))) return;
    appendAssistant("单因子能解释方向，组合因子更能解释月份间强弱差异，因此进入 pay_window × user_type × channel。");

    await runToolGroup(toolGroups.joint);
    if (cancelled.current) return;
    appendAssistant("pay_window × user_type × channel 的解释力高于任意单因子，残差主要集中在活动和异常月份。");

    setWorkbench((current) => ({
      ...current,
      activeQuestion: {
        id: "event-gap",
        title: "需要确认表达边界",
        body: "活动事件表不完整，是否允许把活动因素写成候选解释？",
        options: ["允许候选表达", "只写已证明因素"],
      },
    }));
    setRunning(false);
  }

  async function continueAfterQuestion(value: string) {
    setRunning(true);
    setServerClock((current) => ({ ...current, state: "running" }));
    setWorkbench((current) => ({
      ...current,
      activeQuestion: current.activeQuestion ? { ...current.activeQuestion, selected: value } : undefined,
    }));
    appendAssistant(value === "允许候选表达" ? "我会把活动因素保留为候选解释，并在答案里降低表达强度。" : "我会只写已证明因素，活动相关路径不进入主结论。");

    if (!(await serverWait(700))) return;
    setWorkbench((current) => ({ ...current, activeQuestion: undefined }));
    await runToolGroup(toolGroups.verify);
    if (cancelled.current) return;
    setMessages((current) => [...current, { id: "answer", kind: "answer" }]);
    setServerClock((current) => ({
      ...current,
      state: "completed",
      elapsedMs: runStartedAtRef.current ? Date.now() - runStartedAtRef.current : current.elapsedMs,
    }));
    setRunning(false);
  }

  function send() {
    if (running || workbench.activeQuestion) return;
    const question = draft.trim();
    if (!question) return;

    const includeAttachment = hasDraftAttachment;
    setDraft("");
    setHasDraftAttachment(false);
    void playFlow(question, includeAttachment);
  }

  return (
    <main className="app-shell">
      <aside className="thread-sidebar">
        <div className="brand">WAJE BI v2</div>
        <button className="new-thread" type="button">New analysis</button>
        <nav>
          <a className="active">月初付费金额归因</a>
          <a>发薪窗口回溯</a>
          <a>支付成功率异常</a>
        </nav>
      </aside>

      <section className="chat-shell">
        <header className="chat-header">
          <div>
            <strong>月内周期模式分析</strong>
            <span>LangGraph mock · SQL-first evidence flow</span>
          </div>
        </header>

        <div className="message-list" ref={listRef}>
          {userMessage ? <UserMessageBubble message={userMessage} /> : null}
          {userMessage && runClock.state !== "idle" ? <RunClockBadge clock={runClock} /> : null}
          {messages.map((message) => {
            if (message.kind === "assistant") return <AssistantText key={message.id}>{message.text}</AssistantText>;
            if (message.kind === "tool_group") return <ToolGroupBlock group={message.group} key={message.id} />;
            return <Answer key={message.id} />;
          })}
        </div>

        <div className="bottom-panel">
          <RunWorkbench workbench={workbench} onAnswer={continueAfterQuestion} />
          <Composer
            value={draft}
            hasAttachment={hasDraftAttachment}
            running={running}
            blocked={Boolean(workbench.activeQuestion)}
            onChange={setDraft}
            onAttach={() => setHasDraftAttachment(true)}
            onRemoveAttachment={() => setHasDraftAttachment(false)}
            onSend={send}
          />
        </div>
      </section>
    </main>
  );
}
