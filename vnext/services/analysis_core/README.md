# Analysis Core

Python Analysis Core 拥有 Primary Business Analysis Agent、typed action controller、五类
权威对象 repository、capability fabric、semantic/query、Trust Plane 与 customer-safe
projection。

Gate 2 的 `waje_vnext.controller.WAJEController` 是 authoritative action loop 的唯一所有者。
模型 provider 只返回 typed business proposal；controller 分配 identity、执行 admission、
持久化 accepted heads、checkpoint、outbox 与 event journal。LangGraph 不在该权威路径中。

Gate 3 将 estimand、`EstimatorSpec`、comparison groups、exposure adjustment、alternatives、
falsification、reversal 与 stop conditions 收敛到 `AnalysisFrameRevision`。Primary Agent
基于语义合同和证据自主修订测量设计；admission 只校验结构完整性、Frame requirement 与
WorkPlan task closure，以及确定性数据和证据边界。

`waje_vnext.capabilities.period_comparison` 是首个架构证明 capability。它编译 Agent 提交的
typed ordinal groups，返回 total、observed exposure 与 per-exposure-unit sufficient
statistics，不替 Agent 选择业务 estimand。`waje_vnext.projections.workflow` 从 accepted
Frame/Plan、EvidenceRecord 和 event journal 构造只读业务 Workflow。
