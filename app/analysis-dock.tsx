"use client";

import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  LoaderCircle,
} from "lucide-react";

import {
  CUSTOMER_PHASES,
  type CustomerAnalysisSnapshot,
  type CustomerAnalysisState,
  type CustomerInputRequest,
} from "./api/_customerAnalysisContract";
import type {
  TraceReasoning,
  TraceReasoningIssue,
  TraceReasoningQuery,
  TraceReasoningTask,
} from "./run-reasoning-contracts";

type ProgressConnection = "idle" | "connecting" | "live" | "reconnecting";
const CUSTOM_QUESTION_OPTION = "__custom_question_answer__";

function formatConfirmedAt(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function connectionLabel(
  connection: ProgressConnection,
  confirmedAt: string,
) {
  if (connection === "reconnecting") return "连接中断，正在恢复最新进度";
  if (connection === "connecting") return "正在连接进度服务";
  if (connection === "live") {
    return `进度已连接 · 最近确认 ${formatConfirmedAt(confirmedAt)}`;
  }
  return "进展会持续保存，可以稍后返回";
}

function plannerStatusCopy(
  state: CustomerAnalysisState,
  pending: boolean,
) {
  if (pending) return { label: "待确认", tone: "pending" };
  if (state.status === "needs_input") {
    return { label: "等待确认", tone: "pending" };
  }
  if (state.status === "checkpoint") {
    return {
      label: state.phase === "planning" ? "已规划" : "阶段完成",
      tone: "completed",
    };
  }
  if (state.status === "completed") {
    return { label: "已完成", tone: "completed" };
  }
  if (state.status === "completed_with_limits") {
    return { label: "有边界", tone: "limited" };
  }
  if (state.status === "failed") {
    return { label: "受限", tone: "limited" };
  }
  if (state.status === "working" && state.phase === "planning") {
    return { label: "规划中", tone: "active" };
  }
  return { label: "分析中", tone: "active" };
}

function questionSelections(input: CustomerInputRequest) {
  return Object.fromEntries(
    input.questions.map((question) => [
      question.questionKey,
      question.options.find((option) => option.recommended)?.optionKey
        ?? question.options[0]?.optionKey
        ?? "",
    ]),
  );
}

export function PlannerStatusCard({
  snapshot,
  pending,
  reasoningIssues,
}: {
  snapshot: CustomerAnalysisSnapshot;
  pending: boolean;
  reasoningIssues?: TraceReasoningIssue[];
}) {
  const [expanded, setExpanded] = useState(true);
  useEffect(() => {
    if (window.matchMedia("(max-width: 1100px)").matches) setExpanded(false);
  }, []);
  const answeredCount = reasoningIssues?.filter(
    (issue) => issue.status === "answered",
  ).length ?? 0;
  const status = reasoningIssues?.length
    ? {
        label: `${answeredCount}/${reasoningIssues.length} 已完整回答`,
        tone: answeredCount === reasoningIssues.length
          ? "completed"
          : "limited",
      }
    : plannerStatusCopy(snapshot.state, pending);
  const issueStates = reasoningIssues?.length
    ? reasoningIssues.map((issue) => ({
        question: issue.question,
        status: issue.status,
        statusLabel: reasoningIssueStatusLabel(issue.status),
      }))
    : snapshot.plannerIssueStates.length
      ? snapshot.plannerIssueStates
      : snapshot.plannerIssues.map((question) => ({
          question,
          status: "pending" as const,
          statusLabel: "待分析",
        }));
  const visibleIssues = issueStates.slice(0, 5);
  if (!visibleIssues.length) return null;

  return (
    <aside
      aria-label="本轮待解决问题"
      className={`planner-status-card ${status.tone} ${expanded ? "expanded" : ""}`}
    >
      <button
        aria-expanded={expanded}
        className="planner-status-header"
        onClick={() => setExpanded((current) => !current)}
        type="button"
      >
        <strong>本轮待解决</strong>
        <span className={`planner-status-label ${status.tone}`}>
          <span aria-hidden="true" />
          {status.label}
        </span>
        <ChevronDown
          aria-hidden="true"
          className="planner-status-chevron"
          size={14}
        />
      </button>
      {expanded ? (
        <div className="planner-status-body">
          <ol>
            {visibleIssues.map((issue, index) => (
              <li key={`${index}:${issue.question}`}>
                <span aria-hidden="true">{index + 1}</span>
                <p>{issue.question}</p>
                <small className={`planner-issue-state ${issue.status}`}>
                  {issue.statusLabel}
                </small>
              </li>
            ))}
          </ol>
          {issueStates.length > visibleIssues.length ? (
            <small>另有 {issueStates.length - visibleIssues.length} 项</small>
          ) : null}
        </div>
      ) : null}
    </aside>
  );
}

function reasoningIssueStatusLabel(status: TraceReasoningIssue["status"]) {
  return ({
    answered: "已完整回答",
    partial: "部分回答",
    omitted: "有证据，未进入回答",
    unresolved: "本次未解决",
  } as const)[status];
}

export function AnalysisTaskCard({
  snapshot,
  pending,
  connection,
  reasoning,
  onNewAnalysis,
  onRefresh,
}: {
  snapshot: CustomerAnalysisSnapshot;
  pending: boolean;
  connection: ProgressConnection;
  reasoning?: TraceReasoning | null;
  onNewAnalysis: () => void;
  onRefresh: () => void;
}) {
  const { state } = snapshot;
  const [expanded, setExpanded] = useState(
    state.status === "working" && !pending,
  );
  const [expandedTaskId, setExpandedTaskId] = useState<string | null>(null);

  useEffect(() => {
    setExpanded(state.status === "working" && !pending);
  }, [pending, state.status]);

  const completedCount = reasoning
    ? reasoning.tasks.filter((task) => taskIsSettled(task)).length
    : state.updates.filter((update) => update.status === "completed").length;
  const totalCount = reasoning?.tasks.length ?? CUSTOMER_PHASES.length;
  const activeTask = reasoning?.tasks.find((task) =>
    task.status === "unsettled" || task.status === "not_started"
  );
  const activeTaskId = activeTask?.taskId ?? null;
  const taskRollup = reasoning
    ? {
        completed: reasoning.tasks.filter((task) => task.status === "succeeded").length,
        insufficient: reasoning.tasks.filter((task) => task.status === "unavailable").length,
        bounded: reasoning.tasks.filter((task) =>
          ["integrity_failed", "technical_failed", "skipped", "superseded"]
            .includes(task.status)
        ).length,
        active: reasoning.tasks.filter((task) => task.status === "unsettled").length,
        waiting: reasoning.tasks.filter((task) => task.status === "not_started").length,
      }
    : null;

  useEffect(() => {
    setExpandedTaskId(activeTaskId);
  }, [activeTaskId, reasoning?.tasks.length]);

  const statusCopy = pending
    ? "正在确认提交"
    : state.status === "needs_input"
      ? state.title
      : state.status === "completed" || state.status === "completed_with_limits"
        ? "分析已完成"
        : activeTask?.businessLabel ?? state.title;

  return (
    <section
      aria-label="分析任务"
      className={`analysis-task-card ${state.status} ${expanded ? "expanded" : ""}`}
      role={state.status === "failed" ? "alert" : undefined}
    >
      <button
        aria-expanded={expanded}
        className="analysis-task-summary"
        onClick={() => setExpanded((current) => !current)}
        type="button"
      >
        <span className="analysis-task-title">
          <strong>分析任务</strong>
          <span>{reasoning ? "已结算" : "进度"} {completedCount}/{totalCount}</span>
        </span>
        <span className="analysis-task-state">
          {state.status === "working" || pending ? (
            <LoaderCircle aria-hidden="true" className="progress-spinner" size={14} />
          ) : state.status === "needs_input" ? (
            <CircleHelp aria-hidden="true" size={14} />
          ) : state.status === "failed" ? (
            <AlertTriangle aria-hidden="true" size={14} />
          ) : (
            <Check aria-hidden="true" size={14} />
          )}
          <span>{statusCopy}</span>
          <ChevronDown aria-hidden="true" className="analysis-task-chevron" size={16} />
        </span>
      </button>

      {expanded ? (
        <div className="analysis-task-detail">
          <p>
            {pending
              ? "正在确认这次提交。"
              : activeTask
                ? `当前正在处理：${activeTask.businessLabel}`
                : state.description}
          </p>
          {reasoning?.tasks.length ? (
            <>
              {taskRollup ? (
                <div aria-label="任务结算汇总" className="analysis-task-rollup">
                  {taskRollup.completed ? (
                    <span className="completed">{taskRollup.completed} 完成</span>
                  ) : null}
                  {taskRollup.insufficient ? (
                    <span className="insufficient">
                      {taskRollup.insufficient} 信息不足
                    </span>
                  ) : null}
                  {taskRollup.bounded ? (
                    <span className="bounded">{taskRollup.bounded} 有边界</span>
                  ) : null}
                  {taskRollup.active ? (
                    <span className="active">{taskRollup.active} 进行中</span>
                  ) : null}
                  {taskRollup.waiting ? (
                    <span className="waiting">{taskRollup.waiting} 待处理</span>
                  ) : null}
                </div>
              ) : null}
              <ol aria-label="任务与查询状态" className="analysis-runtime-tasks">
                {reasoning.tasks.map((task) => {
                  const taskExpanded = expandedTaskId === task.taskId;
                  const hasDetails = Boolean(
                    task.queries.length
                    || task.failure?.businessBoundary,
                  );
                  return (
                    <li className={taskStatusClass(task)} key={task.taskId}>
                      <button
                        aria-expanded={hasDetails ? taskExpanded : undefined}
                        className="analysis-runtime-task-row"
                        disabled={!hasDetails}
                        onClick={() => {
                          if (!hasDetails) return;
                          setExpandedTaskId((current) =>
                            current === task.taskId ? null : task.taskId
                          );
                        }}
                        type="button"
                      >
                        <span aria-hidden="true">
                          {task.status === "succeeded" ? (
                            <Check size={12} />
                          ) : task.status === "not_started" || task.status === "unsettled" ? (
                            <LoaderCircle className="progress-spinner" size={12} />
                          ) : (
                            <AlertTriangle size={12} />
                          )}
                        </span>
                        <span className="analysis-runtime-task-label">
                          {task.businessLabel}
                        </span>
                        <small>{dockTaskStatusLabel(task)}</small>
                        {hasDetails ? (
                          <ChevronRight
                            aria-hidden="true"
                            className={taskExpanded ? "expanded" : ""}
                            size={13}
                          />
                        ) : (
                          <span aria-hidden="true" />
                        )}
                      </button>
                      {taskExpanded && task.queries.length ? (
                        <ul aria-label={`${task.businessLabel}的查询状态`}>
                          {task.queries.map((query) => (
                            <li className={query.status} key={query.resultRef}>
                              <span>{query.label}</span>
                              <small>{dockQueryStatusLabel(query)}</small>
                            </li>
                          ))}
                        </ul>
                      ) : null}
                      {taskExpanded && task.failure?.businessBoundary ? (
                        <p className="analysis-runtime-task-boundary">
                          {task.failure.businessBoundary}
                        </p>
                      ) : null}
                    </li>
                  );
                })}
              </ol>
            </>
          ) : state.updates.length ? (
            <ol aria-label="分析进展">
              {state.updates.map((update) => (
                <li className={update.status} key={update.key}>
                  <span aria-hidden="true">
                    {update.status === "completed" ? (
                      <Check size={12} />
                    ) : update.status === "failed" ? (
                      <AlertTriangle size={12} />
                    ) : (
                      <LoaderCircle className="progress-spinner" size={12} />
                    )}
                  </span>
                  <span>{update.text}</span>
                  <time dateTime={update.confirmedAt}>
                    {formatConfirmedAt(update.confirmedAt)}
                  </time>
                </li>
              ))}
            </ol>
          ) : null}
          {state.status === "working" ? (
            <small className={`analysis-connection ${connection}`}>
              {connectionLabel(connection, snapshot.confirmedAt)}
            </small>
          ) : null}
          {state.status === "failed" ? (
            <div className="analysis-task-actions">
              {state.recovery === "retry" ? (
                <button onClick={onRefresh} type="button">重新获取状态</button>
              ) : null}
              <button onClick={onNewAnalysis} type="button">开始新分析</button>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function taskIsSettled(task: TraceReasoningTask) {
  return !["not_started", "unsettled"].includes(task.status);
}

function taskStatusClass(task: TraceReasoningTask) {
  if (task.status === "succeeded") return "completed";
  if (task.status === "not_started" || task.status === "unsettled") return "active";
  return "failed";
}

function dockTaskStatusLabel(task: TraceReasoningTask) {
  if (task.status === "succeeded") return "完成";
  if (task.status === "not_started") return "等待";
  if (task.status === "unsettled") return "进行中";
  if (task.status === "unavailable") return "信息不足";
  return "有边界";
}

function dockQueryStatusLabel(query: TraceReasoningQuery) {
  const label = ({
    waiting: "等待",
    running: "查询中",
    completed: "完成",
    limited: "有边界",
    failed: "失败",
  } as const)[query.status];
  return query.rowCount === undefined ? label : `${label} · ${query.rowCount} 行`;
}

export function QuestionCard({
  input,
  disabled,
  onContinue,
}: {
  input: CustomerInputRequest;
  disabled: boolean;
  onContinue: (answer: string, optionKeys?: string[]) => void;
}) {
  const questions = input.questions.length
    ? input.questions
    : [{
        questionKey: "primary",
        question: input.question,
        explanation: input.explanation,
        options: input.options,
      }];
  const questionIdentity = questions.map((question) =>
    `${question.questionKey}:${question.options.map((option) => option.optionKey).join(",")}`
  ).join("|");
  const [activeIndex, setActiveIndex] = useState(0);
  const [selections, setSelections] = useState<Record<string, string>>(
    () => questionSelections(input),
  );
  const [customAnswers, setCustomAnswers] = useState<Record<string, string>>({});

  useEffect(() => {
    setActiveIndex(0);
    setSelections(questionSelections(input));
    setCustomAnswers({});
  }, [questionIdentity]);

  const activeQuestion = questions[activeIndex] ?? questions[0];
  const selectedOptionKey = selections[activeQuestion.questionKey] ?? "";
  const isLastQuestion = activeIndex === questions.length - 1;
  const activeAnswered = Boolean(
    selectedOptionKey
    && (
      selectedOptionKey !== CUSTOM_QUESTION_OPTION
      || customAnswers[activeQuestion.questionKey]?.trim()
    ),
  );
  const allAnswered = questions.every(
    (question) => {
      const selection = selections[question.questionKey];
      return Boolean(
        selection
        && (
          selection !== CUSTOM_QUESTION_OPTION
          || customAnswers[question.questionKey]?.trim()
        ),
      );
    },
  );

  return (
    <section
      aria-labelledby="customer-question-title"
      className="customer-question-card"
    >
      <h2 id="customer-question-title">{activeQuestion.question}</h2>
      {activeQuestion.explanation ? (
        <p className="sr-only">{activeQuestion.explanation}</p>
      ) : null}

      <fieldset className="customer-question-options">
        <legend className="sr-only">选择一个回答</legend>
        {activeQuestion.options.map((option) => (
          <label
            className={option.optionKey === selectedOptionKey ? "selected" : ""}
            key={option.optionKey}
          >
            <input
              checked={option.optionKey === selectedOptionKey}
              disabled={disabled}
              name={`customer-question-option-${activeQuestion.questionKey}`}
              onChange={() => setSelections((current) => ({
                ...current,
                [activeQuestion.questionKey]: option.optionKey,
              }))}
              type="radio"
              value={option.optionKey}
            />
            <span className="customer-question-copy">
              <strong>{option.label}</strong>
              {option.recommended ? <span className="sr-only">推荐选项。</span> : null}
              <span className="sr-only">{option.description}</span>
            </span>
          </label>
        ))}
        {input.allowFreeform ? (
          <label
            className={
              selectedOptionKey === CUSTOM_QUESTION_OPTION
                ? "selected custom"
                : "custom"
            }
          >
            <input
              checked={selectedOptionKey === CUSTOM_QUESTION_OPTION}
              disabled={disabled}
              name={`customer-question-option-${activeQuestion.questionKey}`}
              onChange={() => setSelections((current) => ({
                ...current,
                [activeQuestion.questionKey]: CUSTOM_QUESTION_OPTION,
              }))}
              type="radio"
              value={CUSTOM_QUESTION_OPTION}
            />
            <span className="customer-question-copy">
              <strong>补充其他口径</strong>
            </span>
          </label>
        ) : null}
      </fieldset>

      {selectedOptionKey === CUSTOM_QUESTION_OPTION ? (
        <div className="customer-question-freeform">
          <textarea
            aria-label="补充其他口径"
            autoFocus
            disabled={disabled}
            onChange={(event) => setCustomAnswers((current) => ({
              ...current,
              [activeQuestion.questionKey]: event.target.value,
            }))}
            placeholder="直接说明你希望采用的口径"
            rows={2}
            value={customAnswers[activeQuestion.questionKey] ?? ""}
          />
        </div>
      ) : null}

      <footer>
        {questions.length > 1 ? (
          <div className="question-pagination" aria-label="澄清问题导航">
            <button
              aria-label="上一个问题"
              disabled={disabled || activeIndex === 0}
              onClick={() => setActiveIndex((current) => current - 1)}
              type="button"
            >
              <ChevronLeft aria-hidden="true" size={14} />
            </button>
            <span>{activeIndex + 1}/{questions.length}</span>
            <button
              aria-label="下一个问题"
              disabled={disabled || isLastQuestion || !activeAnswered}
              onClick={() => setActiveIndex((current) => current + 1)}
              type="button"
            >
              <ChevronRight aria-hidden="true" size={14} />
            </button>
          </div>
        ) : <span />}
        {isLastQuestion ? (
          <button
            className="question-continue"
            disabled={disabled || !allAnswered}
            onClick={() => {
              const answers = questions.map((question) => {
                const optionKey = selections[question.questionKey];
                if (optionKey === CUSTOM_QUESTION_OPTION) {
                  return customAnswers[question.questionKey]?.trim() ?? "";
                }
                return question.options.find(
                  (option) => option.optionKey === optionKey,
                )?.label ?? "";
              });
              const hasCustomAnswer = questions.some(
                (question) =>
                  selections[question.questionKey] === CUSTOM_QUESTION_OPTION,
              );
              if (answers.every(Boolean)) {
                onContinue(
                  hasCustomAnswer
                    ? questions.map(
                      (question, index) => `${question.question}：${answers[index]}`,
                    ).join("；")
                    : answers.join("；"),
                  hasCustomAnswer
                    ? undefined
                    : questions.map(
                      (question) => selections[question.questionKey],
                    ),
                );
              }
            }}
            type="button"
          >
            继续
          </button>
        ) : (
          <button
            className="question-next"
            disabled={disabled || !activeAnswered}
            onClick={() => setActiveIndex((current) => current + 1)}
            type="button"
          >
            下一项
          </button>
        )}
      </footer>
    </section>
  );
}
