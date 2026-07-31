# Analysis Core

Python Analysis Core 拥有 Primary Business Analysis Agent、typed action controller、五类
权威对象 repository、capability fabric、semantic/query、Trust Plane 与 customer-safe
projection。

Gate 2 的 `waje_vnext.controller.WAJEController` 是 authoritative action loop 的唯一所有者。
模型 provider 只返回 typed business proposal；controller 分配 identity、执行 admission、
持久化 accepted heads、checkpoint、outbox 与 event journal。LangGraph 不在该权威路径中。

Gate 3 G3.5 增加：

- `EvidenceRuntime` 的 T1 result landing 与 T2 Evidence disposition；
- capability-native immutable Evidence、append-only validity/satisfaction 和 claim-scoped
  Evidence use；
- provisional Answer candidate/precheck 与 `waiting_for_review`；
- 从 persisted trace、Reviewer heads 和最新 Evidence 状态派生的 settlement precondition；
- 从 accepted Plan 与 durable journal 重建的 execution/obligation/publication/delivery
  四轴 Workflow read model。

capability result 使用两个短事务：T1 在 storage-owned lease clock/fence 下保存 sealed
result、Evidence 与 immutable receipt，T2 根据当前 authority 派生 admission、validity、
satisfaction 与 terminal disposition。Schedule ID 由 controller 与 repository 共同重算。
Workflow projector 直接消费 journal，并以 cursor application receipt 和 CAS head 支持
双 worker 与 ACK 丢失恢复。

Analysis Core 在当前 Gate 拒绝 production Evidence admission、settled Answer 和 delivered
Workflow。物理 QuerySpec/真实数据执行由 Gate 4 提供，内容 verifier、Reviewer disposition
与 publication 由 Gate 5 提供。selected sensitivity identity 尚未进入 sealed
dispatch/result contract，因此 `run_sensitivity` 当前明确拒绝。
