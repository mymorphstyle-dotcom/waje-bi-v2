# WAJE BI v2 Architecture Decisions

This directory records durable architecture decisions for the current WAJE BI v2
development baseline.

- [2026-07-17: Single-authority agent workflow convergence](./2026-07-17-single-authority-agent-workflow.md) — Accepted architecture and no-compatibility cutover contract for intent, planning, execution, evidence, claims, narrative, persistence, and delivery.
- [2026-07-20：业务参考持续交付与人工审计闭环](./2026-07-20-advisory-publication-human-review.md) — 覆盖 narrative 核验、发布状态、客户交互和人工学习闭环的当前合同。

The accepted ADRs together define the current workflow authority. The 2026-07-20
decision supersedes conflicting publication-verifier sections in the 2026-07-17
workflow ADR. Earlier phase workflow documents are retired references and cannot
define runtime behavior, schemas, or acceptance criteria.
