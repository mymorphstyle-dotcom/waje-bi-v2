# Phase 4 Agent Workflow Reference

Version: 2026-07-06.v1

This document is the reference for the Phase 4 Agent runtime workflow. Update this file when the main workflow changes.

## Scope

The main workflow is the fixed LangGraph runtime lifecycle for a WAJE BI Agent run. It is not the accepted graph.

- Main workflow: how the Agent runs, loops, asks, repairs, verifies, and persists.
- Accepted graph: the concrete business analysis graph accepted for one user question, including recipe variants, subgraphs, capability nodes, metric, scope, baseline, time window, dependencies, evidence needs, and degraded or skipped paths.
- Run trace: the actual node path, branches, loops, retries, and stop reasons for one run.
- Answer Package: the draft business answer, evidence refs, limitations, verifier result, role visibility, and audit material.

## Main Workflow

```mermaid
flowchart TD
  A["开始一次经营分析<br/>LLM: 否"] --> B["理解用户业务意图<br/>输出: question_family / metric / scope候选 / baseline候选 / claim目标<br/>LLM: 是"]

  B --> C["生成边界判定包<br/>判断 scope / baseline / 时间语义 / claim强度 / 权限 / 成本是否明确<br/>输出: clear | low_risk_assumption | needs_question | cannot_answer<br/>LLM: 是"]

  C --> D{"澄清策略门禁<br/>只判断是否要打断用户<br/>LLM: 否"}

  D -->|clear| E["确认本次业务理解<br/>记录明确边界<br/>LLM: 是"]
  D -->|low_risk_assumption| F["采用推荐推断继续<br/>写入 accepted graph / Answer Package / verifier checks<br/>LLM: 是"]
  D -->|needs_question| G["生成澄清问题<br/>2-3个业务选项 + 推荐推断 + tell agent differently<br/>LLM: 是"]
  D -->|cannot_answer| X["阻断或降级说明<br/>权限/合同/数据不可满足<br/>LLM: 条件"]

  G --> H["等待用户选择<br/>LangGraph interrupt/resume<br/>LLM: 否"]
  H --> I{"用户选择类型<br/>LLM: 否"}
  I -->|接受推荐| F
  I -->|选择某个边界| J["按用户选择重绑意图<br/>LLM: 是"]
  I -->|tell agent differently| K["重新理解用户新方向<br/>LLM: 是"]

  J --> C
  K --> C
  F --> E

  E --> L["设计分析路线<br/>选择recipe、能力节点、依赖、证据需求、旁路、降级规则<br/>LLM: 是"]

  L --> M{"分析路线验收<br/>合同/权限/能力/预算/证据输出<br/>LLM: 否"}
  M -->|accepted| N["确认真实数据可覆盖范围<br/>LLM读取业务摘要，本地不暴露raw schema<br/>LLM: 是"]
  M -->|repair_requested| O["修正分析路线<br/>补节点/删节点/改依赖/解释跳过<br/>LLM: 是，最多2次"]
  M -->|needs_question| G
  M -->|degraded| Y["降级为可支持路线<br/>记录弱证据或缺口<br/>LLM: 是"]
  M -->|blocked| X
  O --> M

  N --> P{"数据口径与覆盖验收<br/>表/字段/粒度/时间/金额/覆盖<br/>LLM: 否"}
  P -->|sufficient| Q["执行已接受分析路径<br/>pattern/公式/事件/分群/异常/数据质量<br/>LLM: 否"]
  P -->|coverage_gap_but_answerable| Y
  P -->|needs_question| G
  P -->|blocked| X

  Q --> R["整理证据简报<br/>pattern强度、例外、残差、候选机制、限制<br/>LLM: 否"]

  R --> S{"判断下一步分析动作<br/>继续补证据 / 扫sibling / 升维 / 提问 / 停止<br/>LLM: 是"}
  S -->|补证据或扫sibling| L
  S -->|需要用户确认| G
  S -->|升维| T["提出组合归因方向<br/>候选维度、业务理由、预期证据<br/>LLM: 是"]
  S -->|证据足够| U["解释证据和业务含义<br/>主pattern、例外、候选机制、边界<br/>LLM: 是"]
  S -->|证据不足| Y

  T --> V{"组合归因门禁<br/>样本/稀疏/权限/合同/预算<br/>LLM: 否"}
  V -->|accepted| W["执行组合归因<br/>LLM: 否"]
  V -->|skip_with_supported_pattern| U
  V -->|degraded| Y
  V -->|needs_question| G
  V -->|blocked| X
  W --> R

  U --> Z["生成业务答案草稿<br/>自然语言答案 + claim清单 + 数字 + evidence refs<br/>LLM: 是"]

  Z --> AA["语义审计答案<br/>抽取claim、未列明断言、措辞强度、scope映射<br/>LLM: 是"]

  AA -->|passed| AB{"答案硬验收<br/>数字/证据/scope/baseline/claim强度/禁用措辞<br/>LLM: 否"}
  AA -->|needs_revision, first time| AD["按verifier反馈修答案<br/>LLM: 是，最多1次"]
  AA -->|still needs_revision, pattern supported| AG["收敛为有边界答案<br/>只保留本地evidence支持的pattern claim<br/>LLM: 否"]
  AA -->|unsupported after repair| Y
  AG --> AB
  AB -->|passed_or_warning| AC["保存审计结果并返回draft<br/>workflow trace / LLM audit / evidence / SQL / verifier<br/>LLM: 否"]
  AB -->|repairable| AD["按verifier反馈修答案<br/>LLM: 是，最多1次"]
  AB -->|needs_question| G
  AB -->|unsupported_main_claim| Y
  AD --> AA

  Y --> AE["生成降级说明<br/>说明可支持结论、缺口、owner、修复路径<br/>LLM: 是"]
  AE --> AC

  X --> AF["生成阻断说明<br/>说明硬边界、owner、修复路径；不发布业务结论<br/>LLM: 是"]
  AF --> AC
```

## Workflow Rules

- LLM decides business intent, boundary clarity, clarification options, route design, route repair, next analytical action, promotion need, evidence interpretation, answer drafting, semantic audit, answer repair, and business-facing degraded or blocked explanation.
- Local policy and validators decide hard gates only: contracts, permissions, SQL safety, data coverage, metric/time/grain validity, sample red lines, sparse-cell red lines, budget, evidence completeness, and final hard verifier checks.
- Clarification is a loop. User answers, accepted recommendation, and "tell agent differently" all return to boundary rebinding before analysis continues.
- Route repair is a loop with max 2 repair attempts.
- Answer repair is a loop with max 1 repair attempt.
- Evidence expansion and promotion loops return to the evidence brief before final interpretation, with a hard loop cap before answer synthesis.
- If pattern evidence already supports a bounded answer, missing mechanism, event, outlier, or attribution evidence limits the explanation instead of downgrading the main pattern answer.
- If semantic audit still rejects an LLM answer after one repair but the pattern is evidence-supported, local policy can sanitize to one bounded pattern claim before hard verification.
- No local business conclusion fallback is allowed after LangGraph execution failure.
- User-facing process events use business labels. Technical ids, raw SQL, full provider payloads, token metadata, and hidden reasoning stay out of business-reader output.

## Audit Requirements

Admin artifact must include:

- workflow trace with node label, internal node id, status, attempts, branch, failure type, and timing when available
- LLM call audit with task, provider, model, prompt version, response id, input hash, output hash, usage when available, structured output, and prompt consistency status
- proposed graph, accepted graph, rejected/degraded mutations, repair attempts, and clarification outcomes
- runtime binding status, SQL hash, SQL text, and validator results
- evidence refs, result refs, capability outputs, limitations, and evidence strength
- semantic audit output and hard verifier result
- role visibility decision

Business-reader artifact may include only:

- draft business answer
- aggregate evidence
- visible limitations
- SQL hash
