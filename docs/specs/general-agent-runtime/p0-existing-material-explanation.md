# General Agent Runtime P0：已有材料解释

## 当前完成范围

本阶段让通用 Agent 可以在同一 thread 中读取并解释已经发布的 BI 材料：

- `PostgresAnalysisArtifactRegistry` 从客户 publication、其
  `NarrativeMaterialProjection` 和被 projection 引用的 EvidenceLedger entry 构造只读
  artifact 图；
- publication、claim、evidence、limitation 和 score explanation 使用同一
  `ArtifactDescriptor` 合同，保留稳定 ref、版本、digest、source refs、visibility policy
  和客户安全摘要；
- `inspect_analysis_artifact` 读取一个已发布 artifact；
- `explain_claim` 返回结论、证据强度、解释合同、得分组成项和限制；
- 工具输入、输出和最终回答均使用 WAJE Pydantic 合同，Agents SDK 类型停留在 adapter
  内；
- 已有材料解释只执行 PostgreSQL `SELECT` 和 Agent 模型—工具循环，不创建、更新或继续
  BI analysis run。

现有 LangGraph、IntentRevision、PlanRevision、query、EvidenceLedger、ClaimGraph、
publication 和 delivery 权威没有改写。

## 发布可达性边界

Registry 先读取 thread 下已生成 customer payload 的 publication，再沿
`publication_projections.material_projection_ref` 读取对应材料投影。只有材料投影明确
引用的 evidence 和 score explanation 会注册为可检索 artifact。

同一个 BI run 中未进入已发布材料的 evidence 不会因为 run 已发布而进入 Agent 上下文。
原始 capability payload、Provider payload、内部错误和未投影字段继续留在服务端审计。

可检索类型：

| artifact type | 客户安全内容 | 主要来源 |
|---|---|---|
| `bi_publication` | 已发布回答 blocks 与引用闭包 | customer publication payload |
| `bi_claim` | claim、关联 evidence、解释合同、限制 | narrative material projection |
| `bi_evidence` | evidence kind、strength、公开 facts、解释合同 | narrative material projection |
| `bi_limitation` | limitation 与 boundary facets | narrative material projection |
| `score_explanation` | 版本化公式、组成项、权重、贡献、比较范围 | 已发布 evidence entry |

## 工具合同

两个工具都返回统一的 `AgentToolResult`：

```json
{
  "status": "succeeded | limited | failed | needs_input",
  "output": {},
  "artifactRefs": [],
  "materialRefs": [],
  "limitationRefs": [],
  "retryability": "never | same_input | replan_required",
  "customerSummary": "客户安全摘要",
  "technicalDetailRef": null
}
```

`limited` 表示材料可解释且带有必须保留的适用边界。材料不存在时返回 typed failure，
不会生成本地业务答案。SDK adapter 将结构化工具结果编码为规范 JSON 后交回大陆模型，
ledger 仍保存结构化 tool payload。

## 可解释计算合同

### 诊断优先级得分

`dimension-diagnostic-priority@2` 保存：

- 评分主体和跨维度排序范围；
- 每个组成项的 raw value、normalized value、effective weight 和 contribution；
- 最终得分与 contribution sum 闭合；
- comparison allowed 与 limitation refs；
- 缺少 primary-factor alignment 时将该项标记为 `not_applicable`，并在已测组成项之间
  重归一化权重，不注入默认加分。

### 公式分解

现有 `formula-accounting-decomposition-interpretation.v1` 继续保存公式路径、合同 ref、
基线/目标值、各组成项的 signed contribution、contribution share、残差、容差和调和状态。
其贡献语义是会计分解，因果解释保持禁止。

### 窗口比较

`window-metric-comparison-interpretation.v1` 保存 target 与 primary baseline 的聚合定义、
`target - baseline` 绝对变化公式、`absolute / baseline` 相对变化公式、零基线不可计算策略、
完整日与 observation key 权威以及非因果边界。event-relative 比较复用同一解释合同，并
保留 event 和 temporal authority identity。

## Runner 纵向闭环

当前测试覆盖以下路径：

```text
持久化 ThreadItemLedger
  -> AgentContextAssembler artifact index
  -> OpenAI Agents SDK Runner
  -> MainlandModelProvider / Chat Completions
  -> explain_claim
  -> 已发布 claim + evidence + score + limitation
  -> 强类型 AgentFinalOutput
  -> assistant + terminal + ThreadHead 原子终局
```

测试在删除 `OPENAI_API_KEY` 后运行，模型请求只到显式大陆 Provider host，并断言材料解释
过程中没有对 `waje_runtime.analysis_runs` 执行 `INSERT` 或 `UPDATE`。

## 本阶段之后

BI 分析工具提交边界已经由
[P1 BI 分析工具提交边界](./p1-bi-analysis-tools.md) 完成。以下工作进入后续阶段：

- durable long-tool checkpoint、lease、heartbeat 和 worker recovery；
- clarification / approval interruption；
- 已在 P1 transport cutover 完成 Conversation 正常入口和 thread SSE cursor 切换；
- context compaction、动态工具发现和受控多 Agent。
