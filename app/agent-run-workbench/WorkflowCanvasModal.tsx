"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
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
} from "@xyflow/react";
import { ChevronDown, X } from "lucide-react";

import type { TraceEvidence, TraceNode, TraceOwner, TraceRun } from "./contracts";
import styles from "../phase4-replay/replay.module.css";

type CanvasKind = "workflow" | "capability" | "bypass" | "stage";
type StageId = "intent" | "route" | "accepted" | "data" | "evidence" | "business" | "audit" | "final";

type CanvasNodeData = {
  kind: CanvasKind;
  title: string;
  summary: string;
  meta: string[];
  completed: boolean;
  selected: boolean;
  collapsed?: boolean;
  stage?: StageId;
  stageItems?: TraceNode[];
  onToggleStage?: (stage: StageId) => void;
  owner?: TraceOwner;
  route?: string;
  traceNode?: TraceNode;
  evidence?: TraceEvidence;
};

type CanvasNode = Node<CanvasNodeData, CanvasKind>;
type CanvasEdge = Edge;

const nodeTypes: NodeTypes = {
  workflow: CanvasNodeCard,
  capability: CanvasNodeCard,
  bypass: CanvasNodeCard,
  stage: CanvasNodeCard,
};

const STAGES: { id: StageId; label: string; summary: string }[] = [
  { id: "intent", label: "意图和边界", summary: "理解用户问题，确认是否需要追问。" },
  { id: "route", label: "分析路径", summary: "设计候选路径，并验收为可执行证据路径。" },
  { id: "accepted", label: "已验收证据路径", summary: "本轮被接受、会进入证据生成的能力组合。" },
  { id: "data", label: "数据边界", summary: "确认口径、权限、安全和覆盖范围。" },
  { id: "evidence", label: "证据生成", summary: "执行能力并整理证据简报。" },
  { id: "business", label: "业务解释", summary: "解释证据、判断下一步，并形成业务含义。" },
  { id: "audit", label: "答案审计", summary: "检查语义边界，必要时修正或收敛答案。" },
  { id: "final", label: "最终交付", summary: "整理最终业务回答并保存审计记录。" },
];

export function WorkflowCanvasModal({
  run,
  visibleCount,
  selectedNodeId,
  debugAudit,
  onClose,
  onSelectNode,
}: {
  run: TraceRun;
  visibleCount: number;
  selectedNodeId?: string;
  debugAudit?: boolean;
  onClose: () => void;
  onSelectNode: (nodeId: string) => void;
}) {
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
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  const toggleStage = useCallback((stage: StageId) => {
    setCollapsedStages((current) => {
      const next = new Set(current);
      if (next.has(stage)) next.delete(stage);
      else next.add(stage);
      return next;
    });
  }, []);
  const allCollapsed = STAGES.every((stage) => collapsedStages.has(stage.id));
  const model = useMemo(
    () => buildCanvasModel(run, visibleCount, selectedCanvasId, collapsedStages, toggleStage),
    [collapsedStages, run, selectedCanvasId, toggleStage, visibleCount],
  );
  const selected = model.selectedData ?? model.nodes.find((node) => node.id === selectedCanvasId)?.data ?? model.nodes[0]?.data;

  return (
    <div className={styles.workflowBackdrop} role="presentation" onMouseDown={onClose}>
      <section
        aria-label="完整工作流画布"
        aria-modal="true"
        className={styles.workflowModal}
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
      >
        <header className={styles.workflowModalHeader}>
          <div>
            <strong>完整工作流画布</strong>
            <span>
              {run.processSummary.nodes.length} 个节点 · {run.processSummary.acceptedGraph.length} 条已验收证据路径 ·{" "}
              {visibleCount >= run.processSummary.nodes.length ? "本轮已完成" : `回放中 ${visibleCount}/${run.processSummary.nodes.length}`}
            </span>
          </div>
          <div className={styles.workflowModalHeaderActions}>
            <button
              className={styles.workflowHeaderButton}
              onClick={() => setCollapsedStages(allCollapsed ? new Set() : new Set(STAGES.map((stage) => stage.id)))}
              type="button"
            >
              {allCollapsed ? "展开全部" : "折叠全部"}
            </button>
            <button aria-label="关闭画布" onClick={onClose} type="button">
              <X size={17} />
            </button>
          </div>
        </header>

        <div className={styles.workflowModalBody}>
          <div className={styles.workflowCanvas}>
            <ReactFlow
              defaultViewport={{ x: 32, y: 86, zoom: 0.56 }}
              edges={model.edges}
              maxZoom={1.35}
              minZoom={0.25}
              nodes={model.nodes}
              nodeTypes={nodeTypes}
              nodesDraggable={false}
              onNodeClick={(_, node) => {
                setSelectedCanvasId(node.id);
                const data = node.data as CanvasNodeData;
                if (data.traceNode) onSelectNode(data.traceNode.id);
              }}
              proOptions={{ hideAttribution: true }}
            >
              <Background color="#2a2c32" gap={18} />
              <MiniMap
                maskColor="rgba(15, 16, 18, 0.72)"
                nodeColor={(node) => {
                  const data = node.data as CanvasNodeData;
                  if (data.selected) return "#74a9d8";
                  if (data.kind === "capability") return "#8fc69c";
                  if (data.kind === "bypass") return "#777980";
                  return data.completed ? "#3a4a57" : "#27292e";
                }}
                pannable
                zoomable
              />
              <Controls showInteractive={false} />
            </ReactFlow>
          </div>
          <CanvasInspector data={selected} debugAudit={Boolean(debugAudit ?? run.processSummary.debugAudit)} run={run} />
        </div>
      </section>
    </div>
  );
}

function CanvasNodeCard({ data }: NodeProps<CanvasNode>) {
  return (
    <div
      className={`${styles.canvasNode} ${styles[`${data.kind}CanvasNode`]} ${data.completed ? styles.completedCanvasNode : ""} ${
        data.selected ? styles.selectedCanvasNode : ""
      }`}
    >
      <Handle className={styles.canvasHandle} position={Position.Left} type="target" />
      <Handle className={styles.canvasHandle} position={Position.Right} type="source" />
      <div className={styles.canvasNodeHeader}>
        <strong>{data.title}</strong>
        <span>
          {data.kind === "stage"
            ? "阶段"
            : data.owner === "LLM"
              ? "模型判断"
              : data.owner === "本地系统"
                ? "系统校验"
                : data.kind === "bypass"
                  ? "旁路"
                  : "证据路径"}
        </span>
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

function CanvasInspector({ data, debugAudit, run }: { data?: CanvasNodeData; debugAudit: boolean; run: TraceRun }) {
  if (!data) {
    return (
      <aside className={styles.workflowInspector}>
        <strong>节点详情</strong>
        <p>点击画布节点查看本轮业务判断。</p>
      </aside>
    );
  }

  return (
    <aside className={styles.workflowInspector}>
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
      {data.evidence ? (
        <section>
          <h3>证据说明</h3>
          <p>{data.evidence.detail}</p>
          {data.evidence.limitations.length ? <small>{data.evidence.limitations.join("；")}</small> : null}
        </section>
      ) : null}
      {data.kind === "capability" ? (
        <section>
          <h3>关联结论</h3>
          {run.traceClaims.length ? (
            run.traceClaims.slice(0, 3).map((claim) => <p key={claim.text}>{claim.text}</p>)
          ) : (
            <p>本轮没有单独拆出的结构化结论。</p>
          )}
        </section>
      ) : null}
      {debugAudit && data.traceNode?.audit ? (
        <details className={styles.auditDetails}>
          <summary>
            结构化审计
            <ChevronDown size={13} />
          </summary>
          <pre>{JSON.stringify(data.traceNode.audit, null, 2)}</pre>
        </details>
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
  const traceNodesByStage = groupTraceNodes(run.processSummary.nodes);
  let selectedData: CanvasNodeData | undefined;

  STAGES.filter((stage) => stage.id !== "accepted").forEach((stage) => {
    const stageItems = traceNodesByStage.get(stage.id) ?? [];
    if (!stageItems.length) return;

    if (collapsedStages.has(stage.id)) {
      const node = stageNode(stage, stageItems, visibleCount, selectedId, onToggleStage);
      nodes.push(node);
      mainIds.push(node.id);
      if (node.data.selected) selectedData = node.data;
      return;
    }

    stageItems.forEach((traceNode) => {
      const node = workflowNode(traceNode, visibleCount, selectedId);
      nodes.push(node);
      mainIds.push(node.id);
      if (node.data.selected) selectedData = node.data;
    });
  });

  mainIds.forEach((id, index) => {
    const next = mainIds[index + 1];
    if (next) edges.push(mainEdge(`main-${id}-${next}`, id, next));
  });

  addCapabilityBranch(run, visibleCount, selectedId, collapsedStages, onToggleStage, nodes, edges);
  addBypassBranch("clarification_policy_gate", "需要用户确认时", "暂停当前执行，让用户确认分析范围、对比口径或结论目标，再继续生成可验收路径。", run, collapsedStages, selectedId, nodes, edges);
  addBypassBranch("accept_analysis_route", "路径未通过时", "返回模型重新设计证据路径，直到证据路径能被本地合同验收。", run, collapsedStages, selectedId, nodes, edges);
  addBypassBranch("hard_verify_answer", "答案未通过时", "回到答案修正和语义审计，收敛到有证据边界的业务表达。", run, collapsedStages, selectedId, nodes, edges);

  const positioned = layoutOrderedCanvas(nodes, run, collapsedStages);
  selectedData ??= positioned.find((node) => node.id === selectedId)?.data;
  return { nodes: positioned, edges, selectedData };
}

function workflowNode(traceNode: TraceNode, visibleCount: number, selectedId: string): CanvasNode {
  const completed = traceNode.index <= visibleCount;
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
      completed,
      selected: traceNode.id === selectedId,
      meta: [stageLabel(traceNode.node), formatMs(traceNode.durationMs ?? 0)],
    },
  };
}

function stageNode(
  stage: { id: StageId; label: string; summary: string },
  stageItems: TraceNode[],
  visibleCount: number,
  selectedId: string,
  onToggleStage: (stage: StageId) => void,
): CanvasNode {
  const completedCount = stageItems.filter((node) => node.index <= visibleCount).length;
  const llmCount = stageItems.filter((node) => node.owner === "LLM").length;
  const selected = stageItems.some((node) => node.id === selectedId);
  return {
    id: `stage-${stage.id}`,
    type: "stage",
    position: { x: 0, y: 0 },
    sourcePosition: Position.Right,
    targetPosition: Position.Left,
    style: { width: 260 },
    data: {
      kind: "stage",
      title: stage.label,
      summary: stage.summary,
      stage: stage.id,
      stageItems,
      collapsed: true,
      completed: completedCount === stageItems.length,
      selected,
      onToggleStage,
      meta: [`${stageItems.length} 个节点`, `${llmCount} 次模型判断`, `${completedCount}/${stageItems.length} 已完成`],
    },
  };
}

function acceptedStageNode(
  run: TraceRun,
  selectedId: string,
  onToggleStage: (stage: StageId) => void,
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
      completed: true,
      selected: selectedId.startsWith("capability-"),
      onToggleStage,
      meta: [`${run.processSummary.acceptedGraph.length} 条路径`, "侧支展示"],
    },
  };
}

function addCapabilityBranch(
  run: TraceRun,
  visibleCount: number,
  selectedId: string,
  collapsedStages: Set<StageId>,
  onToggleStage: (stage: StageId) => void,
  nodes: CanvasNode[],
  edges: CanvasEdge[],
) {
  if (!run.processSummary.acceptedGraph.length) return;
  const source = run.processSummary.nodes.find((node) => node.node === "accept_analysis_route");
  const execute = run.processSummary.nodes.find((node) => node.node === "execute_capabilities");
  if (!source) return;
  const sourceId = visibleTraceId(source, collapsedStages);
  const executeId = execute ? visibleTraceId(execute, collapsedStages) : undefined;

  if (collapsedStages.has("accepted")) {
    nodes.push(acceptedStageNode(run, selectedId, onToggleStage));
    edges.push(canvasEdge("accepted-stage-in", sourceId, "stage-accepted", "已验收"));
    if (executeId) edges.push(canvasEdge("accepted-stage-out", "stage-accepted", executeId, "执行"));
    return;
  }

  const completed = Boolean(execute && execute.index <= visibleCount);
  run.processSummary.acceptedGraph.forEach((capability) => {
    const evidence = run.traceEvidence.find((item) => item.capability === capability);
    const id = `capability-${capability}`;
    nodes.push({
      id,
      type: "capability",
      position: { x: 0, y: 0 },
      sourcePosition: Position.Left,
      targetPosition: Position.Left,
      style: { width: 250 },
      data: {
        kind: "capability",
        title: capabilityLabel(capability),
        summary: evidence?.detail || "已进入本轮已验收证据路径，作为后续证据生成路径。",
        evidence,
        completed,
        selected: id === selectedId,
        meta: ["已验收路径", evidence?.strength ? `证据强度 ${strengthLabel(evidence.strength)}` : "待生成证据"],
      },
    });
    edges.push(canvasEdge(`accept-${id}`, sourceId, id, "纳入"));
    if (executeId) edges.push(canvasEdge(`execute-${id}`, id, executeId, "执行"));
  });
}

function addBypassBranch(
  sourceName: string,
  title: string,
  summary: string,
  run: TraceRun,
  collapsedStages: Set<StageId>,
  selectedId: string,
  nodes: CanvasNode[],
  edges: CanvasEdge[],
) {
  const source = run.processSummary.nodes.find((node) => node.node === sourceName);
  if (!source || collapsedStages.has(stageForNodeName(source.node))) return;
  const id = `bypass-${sourceName}`;
  nodes.push({
    id,
    type: "bypass",
    position: { x: 0, y: 0 },
    sourcePosition: Position.Left,
    targetPosition: Position.Left,
    style: { width: 250 },
    data: {
      kind: "bypass",
      title,
      summary,
      completed: false,
      selected: id === selectedId,
      meta: ["本轮未触发"],
    },
  });
  edges.push({
    ...canvasEdge(`bypass-edge-${sourceName}`, source.id, id, "旁路"),
    animated: false,
    style: { stroke: "#777980", strokeDasharray: "5 5", strokeWidth: 1.3 },
    markerEnd: { type: MarkerType.ArrowClosed, color: "#777980" },
  });
}

function canvasEdge(id: string, source: string, target: string, label: string): CanvasEdge {
  return {
    id,
    source,
    target,
    type: "smoothstep",
    label,
    markerEnd: { type: MarkerType.ArrowClosed, color: "#8fc69c" },
    style: { stroke: "#8fc69c", strokeWidth: 1.6 },
    labelBgPadding: [6, 3],
    labelBgBorderRadius: 6,
    labelBgStyle: { fill: "#16171a", fillOpacity: 0.92 },
    labelStyle: { fill: "#c8c9cf", fontSize: 11, fontWeight: 600 },
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

function layoutOrderedCanvas(nodes: CanvasNode[], run: TraceRun, collapsedStages: Set<StageId>) {
  const main = nodes.filter((node) => node.data.kind === "workflow" || node.data.kind === "stage").filter((node) => node.data.stage !== "accepted");
  const positions = new Map<string, { x: number; y: number }>();
  main.forEach((node, index) => positions.set(node.id, { x: index * 292, y: 0 }));

  const accept = run.processSummary.nodes.find((node) => node.node === "accept_analysis_route");
  const acceptedAnchor = accept ? positions.get(visibleTraceId(accept, collapsedStages)) : undefined;
  nodes.filter((node) => node.data.kind === "capability" || node.data.stage === "accepted").forEach((node, index) => {
    positions.set(node.id, { x: (acceptedAnchor?.x ?? 0) + index * 270, y: 170 });
  });

  const bypassRows = new Map<string, number>();
  nodes.filter((node) => node.data.kind === "bypass").forEach((node) => {
    const source = sourceNameForBypass(node.id);
    const sourceTrace = run.processSummary.nodes.find((item) => item.node === source);
    const sourceId = sourceTrace ? visibleTraceId(sourceTrace, collapsedStages) : "";
    const sourcePosition = positions.get(sourceId) ?? { x: 0, y: 0 };
    const row = bypassRows.get(sourceId) ?? 0;
    positions.set(node.id, { x: sourcePosition.x, y: 340 + row * 132 });
    bypassRows.set(sourceId, row + 1);
  });

  return nodes.map((node) => ({ ...node, position: positions.get(node.id) ?? node.position }));
}

function defaultCollapsedStages(openStage?: StageId) {
  const stages = new Set<StageId>(STAGES.map((stage) => stage.id));
  if (openStage) stages.delete(openStage);
  return stages;
}

function withoutStage(stages: Set<StageId>, stage: StageId) {
  if (!stages.has(stage)) return stages;
  const next = new Set(stages);
  next.delete(stage);
  return next;
}

function groupTraceNodes(nodes: TraceNode[]) {
  const grouped = new Map<StageId, TraceNode[]>();
  nodes.forEach((node) => {
    const stage = stageForNodeName(node.node);
    grouped.set(stage, [...(grouped.get(stage) ?? []), node]);
  });
  return grouped;
}

function visibleTraceId(node: TraceNode, collapsedStages: Set<StageId>) {
  const stage = stageForNodeName(node.node);
  return collapsedStages.has(stage) ? `stage-${stage}` : node.id;
}

function sourceNameForBypass(id: string) {
  return id.replace("bypass-", "");
}

function stageForNodeName(node: string): StageId {
  if (["understand_business_intent", "decide_question_boundary", "confirm_business_understanding", "clarification_policy_gate"].includes(node)) return "intent";
  if (["design_analysis_route", "accept_analysis_route"].includes(node)) return "route";
  if (["inspect_schema", "validate_runtime_binding", "interpret_data_coverage"].includes(node)) return "data";
  if (["execute_capabilities", "reduce_evidence"].includes(node)) return "evidence";
  if (["decide_next_action", "interpret_evidence", "audit_causal_implications", "synthesize_answer"].includes(node)) return "business";
  if (["semantic_audit", "hard_verify_answer", "repair_answer", "sanitize_answer"].includes(node)) return "audit";
  return "final";
}

function inspectorKicker(data: CanvasNodeData) {
  if (data.kind === "workflow") return data.owner === "LLM" ? "LangGraph 节点 · LLM 介入" : "LangGraph 节点 · 本地系统";
  if (data.kind === "capability") return "已验收证据路径";
  if (data.kind === "stage") return "业务阶段";
  return "旁路";
}

function routeLabel(value: string) {
  return (
    {
      clear: "无需追问",
      ask: "需要用户确认",
      repair: "需要修复后继续",
      semantic_sanitized_to_bounded_answer: "收敛到有边界答案",
      answer_verified: "答案已通过校验",
      answer_repaired: "进入答案修正",
    } as Record<string, string>
  )[value] ?? "进入对应处理分支";
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

function stageLabel(node: string) {
  return STAGES.find((stage) => stage.id === stageForNodeName(node))?.label ?? "业务总结";
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
