"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
  type NodeTypes,
  Position,
  type ReactFlowInstance,
} from "@xyflow/react";
import { X } from "lucide-react";

import type {
  TraceAcceptedTask,
  TraceCapabilityOutcomeStatus,
  TraceCapabilityRetryability,
  TraceClaim,
  TraceEvidence,
  TraceNode,
  TraceOwner,
  TraceRun,
} from "./contracts";
import styles from "../phase4-replay/replay.module.css";

type CanvasKind = "workflow" | "capability" | "stage";
type TraceOutcome = "completed" | "failed" | "waiting" | "skipped" | "unknown";
type AcceptedTaskStatus = "not_started" | "unsettled" | TraceCapabilityOutcomeStatus;
type BindingCompleteness = "known" | "unknown" | "incomplete";
type StageId =
  | "intent"
  | "question"
  | "plan"
  | "accepted"
  | "evidence"
  | "coverage"
  | "authority"
  | "narrative"
  | "delivery"
  | "unknown";

type CanvasNodeData = {
  kind: CanvasKind;
  title: string;
  summary: string;
  meta: string[];
  revealed: boolean;
  outcome: TraceOutcome;
  selected: boolean;
  collapsed?: boolean;
  stage?: StageId;
  stageItems?: TraceNode[];
  onToggleStage?: (stage: StageId) => void;
  owner?: TraceOwner;
  route?: string;
  traceNode?: TraceNode;
  acceptedTask?: TraceAcceptedTask;
  evidenceItems?: TraceEvidence[];
  claimBinding?: {
    completeness: BindingCompleteness;
    detail: string;
    claims: TraceClaim[];
  };
  sourcePosition?: Position;
  targetPosition?: Position;
};

type CanvasNode = Node<CanvasNodeData, CanvasKind>;
type CanvasEdge = Edge;

const nodeTypes: NodeTypes = {
  workflow: CanvasNodeCard,
  capability: CanvasNodeCard,
  stage: CanvasNodeCard,
};

const STAGES: { id: StageId; label: string; summary: string }[] = [
  { id: "intent", label: "意图和边界", summary: "绑定用户业务意图与可执行边界。" },
  { id: "question", label: "澄清决策", summary: "仅在关键歧义会改变结论时暂停并提问。" },
  { id: "plan", label: "权威计划", summary: "把已接受意图编译为可执行能力 DAG。" },
  { id: "accepted", label: "计划内能力", summary: "本轮权威计划接纳的能力任务。" },
  { id: "evidence", label: "证据执行", summary: "执行能力 DAG，并封存证据与完整性记录。" },
  { id: "coverage", label: "结论覆盖", summary: "检查未解决结论是否仍有可执行、可验收的证据路径。" },
  { id: "authority", label: "结论权威", summary: "结算 claim 强度、限制和 verifier 结果。" },
  { id: "narrative", label: "权威叙事", summary: "在已封存结论边界内生成并校验表达。" },
  { id: "delivery", label: "发布交付", summary: "生成唯一客户安全投影并记录交付。" },
  { id: "unknown", label: "未分类节点", summary: "源 trace 没有提供可识别的阶段映射。" },
];

type StageDefinition = (typeof STAGES)[number];
type StageGroup = {
  id: string;
  definition: StageDefinition;
  items: TraceNode[];
};

export function WorkflowCanvasModal({
  run,
  visibleCount,
  selectedNodeId,
  onClose,
  onSelectNode,
}: {
  run: TraceRun;
  visibleCount: number;
  selectedNodeId?: string;
  onClose: () => void;
  onSelectNode: (nodeId: string) => void;
}) {
  const dialogRef = useRef<HTMLElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const titleId = useId();
  const descriptionId = useId();
  const [flowInstance, setFlowInstance] = useState<ReactFlowInstance<CanvasNode, CanvasEdge> | null>(null);
  const [selectedCanvasId, setSelectedCanvasId] = useState(selectedNodeId || run.processSummary.nodes[0]?.id || "");
  const [collapsedStages, setCollapsedStages] = useState<Set<StageId>>(() => defaultCollapsedStages());

  useEffect(() => {
    if (!selectedNodeId) return;
    setSelectedCanvasId(selectedNodeId);
    const selected = run.processSummary.nodes.find((node) => node.id === selectedNodeId);
    const stage = selected ? stageForNodeName(selected.node) : undefined;
    if (stage) {
      setCollapsedStages((current) => withoutStage(current, stage));
    }
  }, [run.processSummary.nodes, selectedNodeId]);

  useEffect(() => {
    previouslyFocusedRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const focusFrame = window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    function handleDialogKeyboard(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(
        dialog.querySelectorAll<HTMLElement>(
          'button:not([disabled]), select:not([disabled]), textarea:not([disabled]), input:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => element.getClientRects().length > 0 && element.getAttribute("aria-hidden") !== "true");
      if (!focusable.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !dialog.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", handleDialogKeyboard);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener("keydown", handleDialogKeyboard);
      document.body.style.overflow = previousOverflow;
      previouslyFocusedRef.current?.focus();
    };
  }, []);

  const toggleStage = useCallback((stage: StageId) => {
    setCollapsedStages((current) => {
      const next = new Set(current);
      if (next.has(stage)) next.delete(stage);
      else next.add(stage);
      return next;
    });
  }, []);
  const allCollapsed = STAGES.filter((stage) => stage.id !== "unknown").every((stage) => collapsedStages.has(stage.id));
  const model = useMemo(
    () => buildCanvasModel(run, visibleCount, selectedCanvasId, collapsedStages, toggleStage),
    [collapsedStages, run, selectedCanvasId, toggleStage, visibleCount],
  );
  const selected = model.selectedData ?? model.nodes.find((node) => node.id === selectedCanvasId)?.data ?? model.nodes[0]?.data;
  const topologyKey = model.nodes.map((node) => node.id).join("|");
  const acceptedGraphSummary = acceptedGraphLabel(run);

  useEffect(() => {
    if (!flowInstance || !model.nodes.length) return;
    let frame = 0;
    const fitCanvas = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        void flowInstance.fitView({ padding: 0.14, minZoom: 0.32, maxZoom: 0.92, duration: 0 });
      });
    };
    fitCanvas();
    const observer = new ResizeObserver(fitCanvas);
    if (canvasRef.current) observer.observe(canvasRef.current);
    return () => {
      window.cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [flowInstance, model.nodes.length, run.id, topologyKey]);

  return (
    <div className={styles.workflowBackdrop} role="presentation" onMouseDown={onClose}>
      <section
        aria-describedby={descriptionId}
        aria-labelledby={titleId}
        aria-modal="true"
        className={styles.workflowModal}
        onMouseDown={(event) => event.stopPropagation()}
        ref={dialogRef}
        role="dialog"
        tabIndex={-1}
      >
        <header className={styles.workflowModalHeader}>
          <div>
            <strong id={titleId}>完整工作流画布</strong>
            <span id={descriptionId}>
              {run.processSummary.nodes.length} 个节点 · {acceptedGraphSummary} · {playbackLabel(run, visibleCount)} · {executionLabel(run)}
            </span>
          </div>
          <div className={styles.workflowModalHeaderActions}>
            <button
              className={styles.workflowHeaderButton}
              onClick={() =>
                setCollapsedStages(
                  allCollapsed ? new Set() : new Set(STAGES.map((stage) => stage.id).filter((stage) => stage !== "unknown")),
                )
              }
              type="button"
            >
              {allCollapsed ? "展开全部" : "折叠全部"}
            </button>
            <button aria-label="关闭画布" onClick={onClose} ref={closeButtonRef} type="button">
              <X size={17} />
            </button>
          </div>
        </header>

        <div className={styles.workflowModalBody}>
          <div className={styles.workflowCanvas} ref={canvasRef}>
            <ReactFlow
              edges={model.edges}
              fitView
              fitViewOptions={{ padding: 0.14, minZoom: 0.32, maxZoom: 0.92 }}
              maxZoom={1.35}
              minZoom={0.25}
              nodes={model.nodes}
              nodeTypes={nodeTypes}
              nodesDraggable={false}
              onInit={setFlowInstance}
              onNodeClick={(_, node) => {
                setSelectedCanvasId(node.id);
                const data = node.data as CanvasNodeData;
                if (data.traceNode) onSelectNode(data.traceNode.id);
              }}
              proOptions={{ hideAttribution: true }}
            >
              <Background color="var(--border-soft)" gap={18} />
              <MiniMap
                maskColor="rgba(15, 16, 18, 0.72)"
                nodeColor={(node) => {
                  const data = node.data as CanvasNodeData;
                  if (data.selected) return "#74a9d8";
                  const taskColor = data.acceptedTask
                    ? acceptedTaskMiniMapColor(acceptedTaskStatus(data.acceptedTask))
                    : undefined;
                  if (taskColor) return taskColor;
                  if (data.kind === "capability") return "var(--green)";
                  if (data.outcome === "failed") return "var(--red)";
                  if (data.outcome === "waiting") return "var(--amber)";
                  return data.revealed ? "#3a4a57" : "var(--border-soft)";
                }}
                pannable
                zoomable
              />
              <Controls showInteractive={false} />
            </ReactFlow>
          </div>
          <CanvasInspector data={selected} />
        </div>
      </section>
    </div>
  );
}

function CanvasNodeCard({ data }: NodeProps<CanvasNode>) {
  const kindClass = data.kind === "capability" ? styles.capabilityCanvasNode : data.kind === "stage" ? styles.stageCanvasNode : "";
  const outcomeClass =
    data.outcome === "completed"
      ? styles.completedCanvasNode
      : data.outcome === "failed"
        ? styles.failedCanvasNode
        : data.outcome === "waiting"
          ? styles.waitingCanvasNode
          : data.outcome === "skipped"
            ? styles.skippedCanvasNode
            : "";
  return (
    <div
      className={`${styles.canvasNode} ${kindClass} ${outcomeClass} ${
        data.revealed ? styles.revealedCanvasNode : styles.unrevealedCanvasNode
      } ${data.selected ? styles.selectedCanvasNode : ""}`}
      data-task-status={data.acceptedTask ? acceptedTaskStatus(data.acceptedTask) : undefined}
    >
      <Handle className={styles.canvasHandle} position={data.targetPosition ?? Position.Left} type="target" />
      <Handle className={styles.canvasHandle} position={data.sourcePosition ?? Position.Right} type="source" />
      <div className={styles.canvasNodeHeader}>
        <strong>{data.title}</strong>
        <span>{canvasNodeTypeLabel(data)}</span>
      </div>
      <p>{data.summary}</p>
      <div className={styles.canvasNodeMeta}>
        {data.meta.map((item) => (
          <small key={item}>{item}</small>
        ))}
      </div>
      {data.kind === "stage" && data.stage ? (
        <button
          className={styles.canvasFoldButton}
          onClick={(event) => {
            event.stopPropagation();
            data.onToggleStage?.(data.stage!);
          }}
          type="button"
        >
          {data.collapsed ? "展开阶段" : "折叠阶段"}
        </button>
      ) : null}
      {data.route ? <em>{data.route}</em> : null}
    </div>
  );
}

function CanvasInspector({ data }: { data?: CanvasNodeData }) {
  if (!data) {
    return (
      <aside className={styles.workflowInspector}>
        <strong>节点详情</strong>
        <p>点击画布节点查看本轮业务判断。</p>
      </aside>
    );
  }
  const acceptedTask = data.acceptedTask;

  return (
    <aside aria-live="polite" className={styles.workflowInspector}>
      <div className={styles.inspectorHeader}>
        <small>{inspectorKicker(data)}</small>
        <strong>{data.title}</strong>
        <span>{data.meta.join(" · ")}</span>
      </div>
      <section>
        <h3>业务说明</h3>
        <p>{data.summary}</p>
      </section>
      {data.kind === "stage" && data.stageItems?.length ? (
        <section>
          <h3>阶段内节点</h3>
          {data.stageItems.map((node) => (
            <p key={node.id}>
              {node.index}. {node.label}
            </p>
          ))}
        </section>
      ) : null}
      {data.route ? (
        <section>
          <h3>本轮分支</h3>
          <p>{data.route}</p>
        </section>
      ) : null}
      {acceptedTask ? (
        <section>
          <h3>任务结果</h3>
          <p className={styles.taskOutcome} data-task-status={acceptedTaskStatus(acceptedTask)}>
            {acceptedTaskStatusLabel(acceptedTaskStatus(acceptedTask))}
          </p>
          {acceptedTask.execution.state === "settled" ? (
            <>
              <small>{acceptedTaskRetryabilityLabel(acceptedTask.execution.retryability)}</small>
              {acceptedTask.execution.failure?.businessBoundary ? (
                <p>业务边界：{acceptedTask.execution.failure.businessBoundary}</p>
              ) : null}
              {acceptedTask.execution.limitationRefs.length ? (
                <p>限制引用：{acceptedTask.execution.limitationRefs.join("；")}</p>
              ) : null}
            </>
          ) : null}
        </section>
      ) : null}
      {data.traceNode?.evidenceCompleteness ? (
        <section>
          <h3>本轮能力执行记录</h3>
          <p>
            已绑定 {data.traceNode.evidenceRefs?.length ?? 0} 条引用 ·
            完整性{completenessLabel(data.traceNode.evidenceCompleteness)}
          </p>
          {data.traceNode.evidenceRefs?.map((evidenceRef) => (
            <p key={evidenceRef} title={evidenceRef}>{shortRef(evidenceRef)}</p>
          ))}
        </section>
      ) : null}
      {data.evidenceItems?.length ? (
        <section>
          <h3>能力执行与证据边界</h3>
          {data.evidenceItems.map((evidence) => (
            <div
              className={styles.inspectorEvidence}
              data-binding-state={evidence.bindingState}
              data-execution-state={evidence.executionState}
              key={evidence.evidenceRef}
            >
              <p>{evidence.detail}</p>
              <small>
                {evidenceBindingLabel(evidence.bindingState)}
                {` · ${evidenceExecutionLabel(evidence.executionState)}`}
                {evidence.planState === "superseded" ? " · 历史计划" : " · 当前计划"}
              </small>
              <small>证据引用 {evidence.evidenceRef}</small>
              {evidence.limitations.length ? <small>{evidence.limitations.join("；")}</small> : null}
            </div>
          ))}
        </section>
      ) : null}
      {data.kind === "capability" ? (
        <section>
          <h3>关联结论</h3>
          <p className={styles.bindingStatus} data-completeness={data.claimBinding?.completeness ?? "unknown"}>
            {data.claimBinding?.detail ?? "结论绑定状态未记录。"}
          </p>
          {data.claimBinding?.claims.map((claim, index) => <p key={`${claim.text}-${index}`}>{claim.text}</p>)}
        </section>
      ) : null}
    </aside>
  );
}

function buildCanvasModel(
  run: TraceRun,
  visibleCount: number,
  selectedId: string,
  collapsedStages: Set<StageId>,
  onToggleStage: (stage: StageId) => void,
) {
  const nodes: CanvasNode[] = [];
  const edges: CanvasEdge[] = [];
  const mainIds: string[] = [];
  const orderedTraceNodes = chronologicalTraceNodes(run.processSummary.nodes);
  const revealedNodeIds = new Set(orderedTraceNodes.slice(0, Math.max(0, visibleCount)).map((node) => node.id));
  const stageGroups = groupTraceNodes(orderedTraceNodes);
  const stageGroupByNodeId = new Map(stageGroups.flatMap((group) => group.items.map((node) => [node.id, group.id] as const)));
  let selectedData: CanvasNodeData | undefined;

  stageGroups.forEach((group) => {
    if (group.definition.id !== "unknown" && collapsedStages.has(group.definition.id)) {
      const node = stageNode(group, revealedNodeIds, selectedId, onToggleStage);
      nodes.push(node);
      mainIds.push(node.id);
      if (node.data.selected) selectedData = node.data;
      return;
    }

    group.items.forEach((traceNode) => {
      const node = workflowNode(traceNode, revealedNodeIds, selectedId);
      nodes.push(node);
      mainIds.push(node.id);
      if (node.data.selected) selectedData = node.data;
    });
  });

  mainIds.forEach((id, index) => {
    const next = mainIds[index + 1];
    if (next) edges.push(mainEdge(`main-${id}-${next}`, id, next));
  });

  addCapabilityBranch(
    run,
    selectedId,
    collapsedStages,
    onToggleStage,
    revealedNodeIds,
    stageGroupByNodeId,
    orderedTraceNodes,
    nodes,
    edges,
  );

  const positioned = layoutOrderedCanvas(nodes);
  selectedData ??= positioned.find((node) => node.id === selectedId)?.data;
  return { nodes: positioned, edges, selectedData };
}

function workflowNode(traceNode: TraceNode, revealedNodeIds: Set<string>, selectedId: string): CanvasNode {
  const outcome = nodeOutcome(traceNode);
  const revealed = revealedNodeIds.has(traceNode.id);
  const executionEvidenceMeta = traceNode.evidenceCompleteness
    ? `执行记录 ${traceNode.evidenceRefs?.length ?? 0} · ${completenessLabel(traceNode.evidenceCompleteness)}`
    : undefined;
  return {
    id: traceNode.id,
    type: "workflow",
    position: { x: 0, y: 0 },
    sourcePosition: Position.Right,
    targetPosition: Position.Left,
    style: { width: 260 },
    data: {
      kind: "workflow",
      title: traceNode.label,
      summary: traceNode.summary,
      owner: traceNode.owner,
      route: traceNode.route ? routeLabel(traceNode.route) : undefined,
      traceNode,
      outcome,
      revealed,
      selected: traceNode.id === selectedId,
      meta: [
        stageLabel(traceNode.node),
        outcomeLabel(outcome),
        durationLabel(traceNode.durationMs),
        ...(executionEvidenceMeta ? [executionEvidenceMeta] : []),
        revealed ? "已播放" : "尚未播放",
      ],
    },
  };
}

function stageNode(
  group: StageGroup,
  revealedNodeIds: Set<string>,
  selectedId: string,
  onToggleStage: (stage: StageId) => void,
): CanvasNode {
  const { definition: stage, items: stageItems } = group;
  const revealedCount = stageItems.filter((node) => revealedNodeIds.has(node.id)).length;
  const llmCount = stageItems.filter((node) => node.owner === "LLM" || node.owner === "混合").length;
  const selected = stageItems.some((node) => node.id === selectedId) || group.id === selectedId;
  const outcome = aggregateOutcome(stageItems);
  return {
    id: group.id,
    type: "stage",
    position: { x: 0, y: 0 },
    sourcePosition: Position.Right,
    targetPosition: Position.Left,
    style: { width: 260 },
    data: {
      kind: "stage",
      title: stageItems.length === 1 ? stageItems[0].label : stage.label,
      summary: stage.summary,
      stage: stage.id,
      stageItems,
      collapsed: true,
      outcome,
      revealed: revealedCount > 0,
      selected,
      onToggleStage,
      meta: [`${stageItems.length} 个节点`, `${llmCount} 个含模型参与`, `${revealedCount}/${stageItems.length} 已播放`, outcomeLabel(outcome)],
    },
  };
}

function acceptedStageNode(
  acceptedGraph: TraceAcceptedTask[],
  graphCompleteness: BindingCompleteness,
  selectedId: string,
  onToggleStage: (stage: StageId) => void,
  revealed: boolean,
): CanvasNode {
  const stage = STAGES.find((item) => item.id === "accepted")!;
  return {
    id: "stage-accepted",
    type: "stage",
    position: { x: 0, y: 0 },
    sourcePosition: Position.Left,
    targetPosition: Position.Left,
    style: { width: 250 },
    data: {
      kind: "stage",
      title: stage.label,
      summary: stage.summary,
      stage: stage.id,
      collapsed: true,
      outcome: aggregateAcceptedTaskOutcome(acceptedGraph),
      revealed,
      selected: selectedId === "stage-accepted" || selectedId.startsWith("capability-"),
      onToggleStage,
      meta: [
        `${acceptedGraph.length} 项任务`,
        acceptedTaskOutcomeSummary(acceptedGraph),
        completenessLabel(graphCompleteness),
      ],
    },
  };
}

function addCapabilityBranch(
  run: TraceRun,
  selectedId: string,
  collapsedStages: Set<StageId>,
  onToggleStage: (stage: StageId) => void,
  revealedNodeIds: Set<string>,
  stageGroupByNodeId: Map<string, string>,
  orderedTraceNodes: TraceNode[],
  nodes: CanvasNode[],
  edges: CanvasEdge[],
) {
  const acceptedGraph = run.processSummary.acceptedGraph;
  if (!acceptedGraph?.length) return;
  const execute = findLast(orderedTraceNodes, (node) => node.node === "execute_capability_dag");
  const executePosition = execute ? orderedTraceNodes.indexOf(execute) : orderedTraceNodes.length;
  const source = findLast(
    orderedTraceNodes.slice(0, executePosition),
    (node) => node.node === "compile_authoritative_plan" || node.node === "compile_plan_patch",
  );
  const sourceId = source ? visibleTraceId(source, collapsedStages, stageGroupByNodeId) : undefined;
  const executeId = execute ? visibleTraceId(execute, collapsedStages, stageGroupByNodeId) : undefined;
  const branchRevealed = source ? revealedNodeIds.has(source.id) : false;

  if (collapsedStages.has("accepted")) {
    nodes.push(acceptedStageNode(acceptedGraph, run.traceCompleteness.acceptedGraph, selectedId, onToggleStage, branchRevealed));
    if (sourceId) edges.push(canvasEdge("accepted-stage-in", sourceId, "stage-accepted", "已验收"));
    if (executeId) edges.push(canvasEdge("accepted-stage-out", "stage-accepted", executeId, "执行"));
    return;
  }

  acceptedGraph.forEach((task, index) => {
    const evidenceItems = run.traceEvidence.filter(
      (item) => item.taskId === task.taskId && item.planRevisionId === task.planRevisionId,
    );
    const claimBinding = bindClaimsByEvidenceRefs(run, task, evidenceItems);
    const id = `capability-${index}-${acceptedTaskIdentity(task)}`;
    nodes.push({
      id,
      type: "capability",
      position: { x: 0, y: 0 },
      sourcePosition: Position.Left,
      targetPosition: Position.Left,
      style: { width: 250 },
      data: {
        kind: "capability",
        title: capabilityLabel(task.capabilityId),
        summary: acceptedTaskSummary(task, evidenceItems),
        acceptedTask: task,
        evidenceItems,
        claimBinding,
        outcome: acceptedTaskCanvasOutcome(task),
        revealed: branchRevealed,
        selected: id === selectedId,
        meta: [
          acceptedTaskStatusLabel(acceptedTaskStatus(task)),
          `任务 ${shortRef(task.taskId)}`,
          `计划 ${shortRef(task.planRevisionId)}`,
          ...(task.execution.state === "settled"
            ? [acceptedTaskRetryabilityLabel(task.execution.retryability)]
            : []),
          ...(evidenceItems.length
            ? [evidenceBindingSummary(evidenceItems), evidenceExecutionSummary(evidenceItems)]
            : []),
          acceptedTaskClaimBindingLabel(task, claimBinding.completeness),
          ...(evidenceItems[0]?.strength ? [`证据强度 ${strengthLabel(evidenceItems[0].strength)}`] : []),
        ],
      },
    });
    if (sourceId) edges.push(canvasEdge(`accept-${id}`, sourceId, id, "纳入"));
    if (executeId) edges.push(canvasEdge(`execute-${id}`, id, executeId, "执行"));
  });
}

function canvasEdge(id: string, source: string, target: string, label: string): CanvasEdge {
  return {
    id,
    source,
    target,
    type: "smoothstep",
    label,
    markerEnd: { type: MarkerType.ArrowClosed, color: "var(--green)" },
    style: { stroke: "var(--green)", strokeWidth: 1.6 },
    labelBgPadding: [6, 3],
    labelBgBorderRadius: 6,
    labelBgStyle: { fill: "var(--surface-raised)", fillOpacity: 0.92 },
    labelStyle: { fill: "var(--waje-muted)", fontSize: 11, fontWeight: 600 },
  };
}

function mainEdge(id: string, source: string, target: string): CanvasEdge {
  return {
    id,
    source,
    target,
    type: "smoothstep",
    markerEnd: { type: MarkerType.ArrowClosed, color: "#74a9d8" },
    style: { stroke: "#74a9d8", strokeWidth: 2 },
  };
}

function layoutOrderedCanvas(nodes: CanvasNode[]) {
  const main = nodes.filter((node) => node.data.kind === "workflow" || (node.data.kind === "stage" && node.data.stage !== "accepted"));
  const branches = nodes.filter((node) => node.data.kind === "capability" || node.data.stage === "accepted");
  const positions = new Map<string, { x: number; y: number }>();
  const handlePositions = new Map<string, { source: Position; target: Position }>();
  const columns = Math.max(1, Math.min(4, main.length));
  const horizontalGap = 282;
  const verticalGap = 176;

  main.forEach((node, index) => {
    const row = Math.floor(index / columns);
    const offset = index % columns;
    const forward = row % 2 === 0;
    const column = forward ? offset : columns - 1 - offset;
    positions.set(node.id, { x: column * horizontalGap, y: row * verticalGap });
    handlePositions.set(node.id, {
      source: forward ? Position.Right : Position.Left,
      target: forward ? Position.Left : Position.Right,
    });
  });

  const mainRows = Math.max(1, Math.ceil(main.length / columns));
  branches.forEach((node, index) => {
    const row = Math.floor(index / 4);
    const column = index % 4;
    positions.set(node.id, { x: column * horizontalGap, y: mainRows * verticalGap + 64 + row * verticalGap });
    handlePositions.set(node.id, { source: Position.Bottom, target: Position.Top });
  });

  return nodes.map((node) => {
    const handles = handlePositions.get(node.id);
    return {
      ...node,
      position: positions.get(node.id) ?? node.position,
      sourcePosition: handles?.source ?? node.sourcePosition,
      targetPosition: handles?.target ?? node.targetPosition,
      data: {
        ...node.data,
        sourcePosition: handles?.source ?? node.data.sourcePosition,
        targetPosition: handles?.target ?? node.data.targetPosition,
      },
    };
  });
}

function defaultCollapsedStages(openStage?: StageId) {
  const stages = new Set<StageId>(STAGES.map((stage) => stage.id).filter((stage) => stage !== "unknown"));
  if (openStage) stages.delete(openStage);
  return stages;
}

function withoutStage(stages: Set<StageId>, stage: StageId) {
  if (!stages.has(stage)) return stages;
  const next = new Set(stages);
  next.delete(stage);
  return next;
}

function chronologicalTraceNodes(nodes: TraceNode[]) {
  return nodes
    .map((node, position) => ({ node, position }))
    .sort((left, right) => left.node.index - right.node.index || left.position - right.position)
    .map(({ node }) => node);
}

function groupTraceNodes(nodes: TraceNode[]): StageGroup[] {
  const groups: StageGroup[] = [];
  nodes.forEach((node) => {
    const stageId = stageForNodeName(node.node);
    const definition = STAGES.find((stage) => stage.id === stageId)!;
    const current = groups.at(-1);
    if (current?.definition.id === stageId) {
      current.items.push(node);
      return;
    }
    groups.push({ id: `stage-${stageId}-${groups.length}`, definition, items: [node] });
  });
  return groups;
}

function visibleTraceId(node: TraceNode, collapsedStages: Set<StageId>, stageGroupByNodeId: Map<string, string>) {
  const stage = stageForNodeName(node.node);
  return collapsedStages.has(stage) ? stageGroupByNodeId.get(node.id) ?? node.id : node.id;
}

function findLast<T>(values: T[], predicate: (value: T) => boolean) {
  for (let index = values.length - 1; index >= 0; index -= 1) {
    if (predicate(values[index])) return values[index];
  }
  return undefined;
}

function nodeOutcome(node: TraceNode): TraceOutcome {
  return node.outcome;
}

function aggregateOutcome(nodes: TraceNode[]): TraceOutcome {
  const outcomes = nodes.map(nodeOutcome);
  if (outcomes.includes("failed")) return "failed";
  if (outcomes.includes("waiting")) return "waiting";
  if (outcomes.every((outcome) => outcome === "skipped")) return "skipped";
  if (outcomes.every((outcome) => outcome === "completed" || outcome === "skipped")) return "completed";
  return "unknown";
}

function acceptedTaskIdentity(task: TraceAcceptedTask) {
  return `${task.planRevisionId}:${task.taskId}`;
}

function acceptedTaskStatus(task: TraceAcceptedTask): AcceptedTaskStatus {
  return task.execution.state === "settled" ? task.execution.status : task.execution.state;
}

function acceptedTaskCanvasOutcome(task: TraceAcceptedTask): TraceOutcome {
  const status = acceptedTaskStatus(task);
  if (status === "not_started" || status === "unsettled") return "waiting";
  if (status === "succeeded" || status === "unavailable") return "completed";
  if (status === "integrity_failed" || status === "technical_failed") return "failed";
  return "skipped";
}

function acceptedTaskMiniMapColor(status: AcceptedTaskStatus) {
  return (
    {
      not_started: "#6f7580",
      unsettled: "var(--amber)",
      succeeded: "var(--green)",
      unavailable: "var(--amber)",
      integrity_failed: "var(--red)",
      technical_failed: "var(--red)",
      skipped: "var(--subtle)",
      superseded: "#8f86a8",
    } satisfies Record<AcceptedTaskStatus, string>
  )[status];
}

function aggregateAcceptedTaskOutcome(tasks: TraceAcceptedTask[]): TraceOutcome {
  const outcomes = tasks.map(acceptedTaskCanvasOutcome);
  if (outcomes.includes("failed")) return "failed";
  if (outcomes.includes("waiting")) return "waiting";
  if (outcomes.every((outcome) => outcome === "skipped")) return "skipped";
  return "completed";
}

function acceptedTaskStatusLabel(status: AcceptedTaskStatus) {
  return (
    {
      not_started: "计划已接纳 · 尚未执行",
      unsettled: "执行结果尚未结算",
      succeeded: "执行成功",
      unavailable: "能力不可用",
      integrity_failed: "完整性失败",
      technical_failed: "技术失败",
      skipped: "已跳过",
      superseded: "已被后续计划替代",
    } satisfies Record<AcceptedTaskStatus, string>
  )[status];
}

function acceptedTaskRetryabilityLabel(retryability: TraceCapabilityRetryability) {
  return (
    {
      never: "无需重试",
      same_input: "可按相同输入重试",
      replan_required: "需要重新规划后执行",
    } satisfies Record<TraceCapabilityRetryability, string>
  )[retryability];
}

function acceptedTaskOutcomeSummary(tasks: TraceAcceptedTask[]) {
  const orderedStatuses: AcceptedTaskStatus[] = [
    "not_started",
    "unsettled",
    "succeeded",
    "unavailable",
    "integrity_failed",
    "technical_failed",
    "skipped",
    "superseded",
  ];
  return orderedStatuses
    .map((status) => {
      const count = tasks.filter((task) => acceptedTaskStatus(task) === status).length;
      return count ? `${acceptedTaskStatusLabel(status)} ${count}` : "";
    })
    .filter(Boolean)
    .join(" / ");
}

function acceptedTaskSummary(task: TraceAcceptedTask, evidenceItems: TraceEvidence[]) {
  if (task.execution.state === "not_started") {
    return "任务已进入权威计划，尚未开始执行。";
  }
  if (task.execution.state === "unsettled") {
    return "任务已有执行活动，结果尚未结算。";
  }
  if (task.execution.status === "succeeded") {
    return evidenceItems[0]?.detail ?? "能力任务已成功结算。";
  }
  const businessBoundary = task.execution.failure?.businessBoundary;
  const suffix = businessBoundary ? `：${businessBoundary}。` : "。";
  return (
    {
      unavailable: `能力任务结算为不可用边界${suffix}`,
      integrity_failed: `能力任务因完整性失败停止${suffix}`,
      technical_failed: `能力任务因技术失败停止${suffix}`,
      skipped: "能力任务已按权威执行结果跳过。",
      superseded: "能力任务结果已被后续计划替代。",
    } satisfies Record<Exclude<TraceCapabilityOutcomeStatus, "succeeded">, string>
  )[task.execution.status];
}

function acceptedTaskClaimBindingLabel(
  task: TraceAcceptedTask,
  completeness: BindingCompleteness,
) {
  if (task.execution.state === "not_started") return "结论绑定未开始";
  if (task.execution.state === "unsettled") return "结论绑定待结算";
  if (task.execution.status !== "succeeded") return "无发布结论绑定";
  return `结论绑定${completenessLabel(completeness)}`;
}

function outcomeLabel(outcome: TraceOutcome) {
  return (
    {
      completed: "执行完成",
      failed: "执行失败",
      waiting: "等待输入",
      skipped: "已跳过",
      unknown: "执行结果未记录",
    } satisfies Record<TraceOutcome, string>
  )[outcome];
}

function bindClaimsByEvidenceRefs(
  run: TraceRun,
  task: TraceAcceptedTask,
  evidenceItems: TraceEvidence[],
) {
  const evidenceCompleteness = traceCompleteness(run, "evidence");
  const claimCompleteness = traceCompleteness(run, "claims");
  const evidenceRefs = new Set(evidenceItems.map((evidence) => evidence.evidenceRef));
  const claims = run.traceClaims.filter((claim) => claim.evidenceRefs.some((evidenceRef) => evidenceRefs.has(evidenceRef)));

  if (task.execution.state !== "settled") {
    return {
      completeness: "known" as const,
      detail: task.execution.state === "not_started"
        ? "计划已接纳且尚未执行；当前不形成可发布结论绑定。"
        : "执行结果尚未结算；当前不形成可发布结论绑定。",
      claims,
    };
  }

  if (!evidenceItems.length) {
    if (task.execution.status !== "succeeded") {
      return {
        completeness: "known" as const,
        detail: `${acceptedTaskStatusLabel(task.execution.status)}；该任务终态不形成可发布结论绑定。`,
        claims,
      };
    }
    return {
      completeness: evidenceCompleteness === "known" ? ("incomplete" as const) : evidenceCompleteness,
      detail:
        evidenceCompleteness === "known"
          ? "任务已成功结算，但客户投影没有可绑定的结构化证据记录，结论绑定不完整。"
          : evidenceCompleteness === "incomplete"
            ? "证据记录不完整，无法确认该能力的全部结论绑定。"
            : "证据记录状态未知，无法确认结论绑定。",
      claims,
    };
  }
  if (claims.length) {
    const traceIsComplete = evidenceCompleteness === "known" && claimCompleteness === "known";
    const bindingIsComplete = traceIsComplete;
    return {
      completeness: bindingIsComplete ? ("known" as const) : ("incomplete" as const),
      detail: !traceIsComplete
          ? `已通过 evidenceRef 绑定 ${claims.length} 条结论；claim/evidence trace 的完整性仍未闭合。`
          : `已通过 evidenceRef 绑定 ${claims.length} 条结论。`,
      claims,
    };
  }
  if (task.execution.status !== "succeeded") {
    return {
      completeness: "known" as const,
      detail: `${acceptedTaskStatusLabel(task.execution.status)}；相关记录只表达执行边界，未进入已发布结论。`,
      claims,
    };
  }
  return {
    completeness: claimCompleteness === "unknown" ? ("unknown" as const) : ("incomplete" as const),
    detail:
      claimCompleteness === "unknown"
        ? "结论记录状态未知，无法确认该证据是否被结论引用。"
        : "已记录 evidenceRef，但没有结构化结论引用该证据，结论绑定不完整。",
    claims,
  };
}

function traceCompleteness(
  run: TraceRun,
  key: "chronology" | "llmCalls" | "acceptedGraph" | "claims" | "evidence" | "timing",
): BindingCompleteness {
  return run.traceCompleteness[key];
}

function completenessLabel(value: BindingCompleteness) {
  return (
    {
      known: "完整",
      incomplete: "不完整",
      unknown: "未记录",
    } satisfies Record<BindingCompleteness, string>
  )[value];
}

function acceptedGraphLabel(run: TraceRun) {
  const graph = run.processSummary.acceptedGraph;
  const completeness = traceCompleteness(run, "acceptedGraph");
  if (graph === undefined) {
    return completeness === "unknown"
      ? "计划内能力未记录"
      : `计划内能力缺失（完整性标记：${completenessLabel(completeness)}）`;
  }
  if (completeness === "unknown") return `${graph.length} 项计划内能力（完整性未记录）`;
  return completeness === "incomplete" ? `${graph.length} 项计划内能力（记录不完整）` : `${graph.length} 项计划内能力`;
}

function playbackLabel(run: TraceRun, visibleCount: number) {
  const count = Math.min(Math.max(visibleCount, 0), run.processSummary.nodes.length);
  if (run.runMode === "static_snapshot") return `静态快照 ${count}/${run.processSummary.nodes.length}`;
  if (run.traceCompleteness.chronology === "unknown") return `回放进度 ${count}/${run.processSummary.nodes.length}（时序未记录）`;
  if (run.traceCompleteness.chronology === "incomplete") return `回放进度 ${count}/${run.processSummary.nodes.length}（时序不完整）`;
  return `回放进度 ${count}/${run.processSummary.nodes.length}`;
}

function executionLabel(run: TraceRun) {
  const lifecycle = run.lifecycle.execution;
  if (lifecycle.completeness === "unknown" || !lifecycle.status) return "执行状态未记录";
  const status = statusLabel(lifecycle.status);
  return lifecycle.completeness === "incomplete" ? `执行状态 ${status}（记录不完整）` : `执行状态 ${status}`;
}

function statusLabel(value: string) {
  return (
    {
      completed: "已完成",
      complete: "已完成",
      failed: "失败",
      waiting: "等待输入",
      waiting_for_clarification: "等待澄清",
      planned: "计划已生成",
      evidence_ready: "证据已就绪",
      started: "执行中",
      running: "执行中",
    } as Record<string, string>
  )[value] ?? value;
}

function stageForNodeName(node: string): StageId {
  if (["conversation_entry", "bind_intent"].includes(node)) return "intent";
  if (["generate_clarification", "persist_waiting_for_decision", "accept_material_decision"].includes(node))
    return "question";
  if (["compile_authoritative_plan", "compile_plan_patch"].includes(node)) return "plan";
  if (node === "execute_capability_dag") return "evidence";
  if (node === "evaluate_claim_coverage") return "coverage";
  if (["settle_claim_authority", "seal_authority_bundle"].includes(node)) return "authority";
  if (node === "compose_claim_aware_narrative") return "narrative";
  if (["publish_customer_projection", "deliver_publication"].includes(node)) return "delivery";
  return "unknown";
}

function inspectorKicker(data: CanvasNodeData) {
  if (data.kind === "workflow") {
    if (data.owner === "LLM") return "LangGraph 节点 · LLM 介入";
    if (data.owner === "本地系统") return "LangGraph 节点 · 本地系统";
    if (data.owner === "混合") return "LangGraph 节点 · 模型与系统";
    if (data.owner === "用户") return "LangGraph 节点 · 用户决策";
    return "LangGraph 节点 · 参与方未记录";
  }
  if (data.kind === "capability") return "计划内能力记录";
  return "业务阶段";
}

function canvasNodeTypeLabel(data: CanvasNodeData) {
  if (data.kind === "stage") return "阶段";
  if (data.kind === "capability") return "能力记录";
  if (data.owner === "LLM") return "模型判断";
  if (data.owner === "混合") return "模型与系统";
  if (data.owner === "本地系统") return "系统校验";
  if (data.owner === "用户") return "用户决策";
  return "参与方未记录";
}

function routeLabel(value: string) {
  return (
    {
      clear: "无需追问",
      needs_question: "需要用户确认",
    } as Record<string, string>
  )[value] ?? `分支 ${value}`;
}

function strengthLabel(value: string) {
  return (
    {
      high: "强",
      medium: "中",
      low: "弱",
    } as Record<string, string>
  )[value] ?? value;
}

function evidenceExecutionLabel(value: TraceEvidence["executionState"]) {
  return (
    {
      available: "可用证据",
      unavailable: "不可用边界记录",
      integrity_failed: "完整性失败",
      technical_failed: "技术失败",
    } satisfies Record<TraceEvidence["executionState"], string>
  )[value];
}

function evidenceBindingLabel(value: TraceEvidence["bindingState"]) {
  return value === "bound" ? "执行绑定已闭合" : "执行绑定未闭合";
}

function evidenceBindingSummary(items: TraceEvidence[]) {
  if (!items.length) return "执行绑定未记录";
  const boundCount = items.filter((item) => item.bindingState === "bound").length;
  return boundCount === items.length
    ? `执行绑定已闭合 ${boundCount}`
    : `执行绑定 ${boundCount}/${items.length} 已闭合`;
}

function evidenceExecutionSummary(items: TraceEvidence[]) {
  if (!items.length) return "能力执行记录未记录";
  const counts = new Map<TraceEvidence["executionState"], number>();
  items.forEach((item) => counts.set(item.executionState, (counts.get(item.executionState) ?? 0) + 1));
  return [...counts.entries()]
    .map(([state, count]) => `${evidenceExecutionLabel(state)} ${count}`)
    .join(" / ");
}

function shortRef(value: string) {
  return value.length <= 18 ? value : `${value.slice(0, 8)}…${value.slice(-6)}`;
}

function stageLabel(node: string) {
  return STAGES.find((stage) => stage.id === stageForNodeName(node))?.label ?? "业务总结";
}

function durationLabel(value?: number) {
  if (value === undefined) return "耗时未记录";
  return formatMs(value);
}

function formatMs(value: number) {
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${Math.round(value)}ms`;
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
