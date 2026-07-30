# Analysis Core

Python Analysis Core 拥有 Primary Business Analysis Agent、typed action controller、五类
权威对象 repository、capability fabric、semantic/query、Trust Plane 与 customer-safe
projection。

Gate 2 的 `waje_vnext.controller.WAJEController` 是 authoritative action loop 的唯一所有者。
模型 provider 只返回 typed business proposal；controller 分配 identity、执行 admission、
持久化 accepted heads、checkpoint、outbox 与 event journal。LangGraph 不在该权威路径中。
