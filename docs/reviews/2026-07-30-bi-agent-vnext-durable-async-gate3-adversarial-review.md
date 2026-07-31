# WAJE vNext durable async / Gate 3 对抗式审查

## 1. Verdict

| 对象 | 结论 |
|---|---|
| 顶层原则 | 接受：case-scoped durable async + serial authority admission |
| Gate 0–2 amendment | authority safety substrate 已落地，跨进程 worker 的生产完备性仍有 Gate 3 工作 |
| Gate 3 文档 | 已按异步拓扑、局部同步提交和 schedule-perturbation tests 修订 |
| Gate 3 entry | 继续 Blocked；G3.E0 `policy_ready=false` |
| 用户决策 | 本轮无需用户决策 |

审查采用故障注入视角：消息、LLM、review、effect、result、authority CAS、journal、outbox、
projection 任意两步可乱序，任一步可重复或 crash。

## 2. 已满足的不变量

- command ingress 的 mailbox、journal、controller wake outbox 在一个短事务内提交；
- user correction 推进 authority epoch，无需等待开放语义 binding；
- Primary Agent provider 已从 `advance()` 拆为 durable LLM outbox job；
- ControllerState 显式保存 mailbox cursor、authority epoch 和 `pending_job_ids[]`；
- LLM/effect 最终提交同时检查 accepted head 和 authority epoch；
- effect 在 correction 竞态下保留 attempt/result，随后进入 `JOB_SUPERSEDED`；
- outbox job 带 operation/causation/correlation、payload hash、expected head/epoch；
- effect job 必须绑定 admitted action，job kind mismatch fail closed；
- outbox 可跨进程枚举和幂等 dispatch，重复 wake/completion 不产生第二次 authority mutation；
- job delivery lease 提供 fencing token、heartbeat API、expiry 和 takeover state；
- journal、checkpoint、outbox、mailbox、action 与 job contracts 有 Python/JSON Schema/TS
  binding；
- 真实用户八问已进入独立 candidate Episode，拟合 expectation 没有冒充用户确认。

## 3. Blocking implementation findings

这些 finding 不否定 Gate 0–2 amendment 的 authority safety结论；它们阻止 Gate 3 runtime exit。

### DA-B1. 周期 heartbeat supervisor 尚未实现

storage port 和 PostgreSQL table 已支持 heartbeat，当前本地 worker handler 只 acquire/release。
超过 lease duration 的 LLM/capability 调用需要独立 supervisor 周期续租，同时处理续租失败和
旧 fencing token 返回。

关闭条件：

- job worker 在 effect 运行期间按 policy heartbeat；
- heartbeat failure 使 worker 放弃 authority commit；
- expired lease takeover 后旧 worker result 只能保存为 stale/superseded receipt；
- fake clock + PostgreSQL integration 覆盖续租、过期、接管和旧 token。

### DA-B2. 缺少通用 terminal JobDispositionRecord

outbox 可以枚举，当前 terminal 状态分散在 LLM event、effect attempt 和 controller state。
dispatcher 重启后需要一个通用、不可变、按 outbox ID 唯一的 disposition，区分
`succeeded | superseded | terminal_failed`，并允许 retryable attempt 保持 pending。

关闭条件：

- `JobDispositionRecord` 绑定 outbox/operation/result hash/authority fence；
- terminal disposition 与 completion/supersede journal 在同一事务；
- dispatcher 只 claim 无 terminal disposition 的 job；
- duplicate terminal completion 内容相同幂等，内容不同拒绝；
- projection 不从 queue visibility 推断业务 task success。

### DA-B3. 多 pending job state 尚无 obligation-aware scheduler

`pending_job_ids[]` 可以持久化多个 job，当前 Primary Agent action loop 一次只创建一个 effect。
Gate 3 需要依据 accepted WorkPlan dependency 和 obligation state 做 fan-out/fan-in。

关闭条件：

- scheduler 只从 accepted Plan + obligation projection 推导 runnable set；
- 独立 obligation 可并行，依赖 obligation 等待 admission；
- 每个 completion 独立重检 Frame/Plan/head/epoch/obligation；
- 所有关键完成排列、局部失败、retry 和 correction 通过；
- 完成顺序不改变业务 measurement 或计划含义。

### DA-B4. Reviewer async phase 只有 substrate

`WAITING_FOR_REVIEW` 和 reviewer job kind 已进入合同，Frame candidate review request/result、
objection closure 和 stale review rejection 尚未实现。

关闭条件：

- review request 绑定 candidate hash、question/frame head 与 epoch；
- Reviewer worker 使用独立 provider invocation；
- correction 和新 candidate fence 旧 review；
- blocking objection closure proof 在 Frame CAS 内重算。

### DA-B5. Operation lineage 尚未覆盖 Gate 3 新对象

Gate 0–2 command/action/job 与显式 controller event 已携带 operation identity。当前
storage authority mutation API 仍允许 `_derived_event_operation()` 静默派生 lineage；
缺省路径可能用 case head 或 `0` 作为 causation，并退化到 case 级 correlation，无法证明
它对应触发本次 mutation 的 command/action/job。QuestionRevision、MessageImpactBinding、
FrameCandidateBundle、review result、Evidence admission 和 projection event 也仍需统一
causation/correlation 规则。

关闭条件：

- checked-in lineage matrix 定义每种 command/event/job/authority record 的 producer、
  causation source、correlation scope 和 authority revision；
- authority mutation API 显式接收 causal operation，production path 删除静默缺省派生；
- deterministic validator 检查断链、payload hash mismatch 和跨 case correlation；
- trace verifier 从 ingress 到 provisional Answer 可沿 operation lineage 闭合。

## 4. Evaluation findings

### DA-E1. 八个真实问题共享同一 source session

`real_user_language=8` 满足当前 raw count floor，但不代表八个独立用户、行业或会话分布。
它们有真实措辞价值，无法单独证明真实用户分布覆盖。

Disposition：保持 G3.E0 blocked。canonical taxonomy/source registry 应分别记录 unique
source sessions、unique users/roles、industries 和 question worlds；policy 需要独立性 floor。

### DA-E2. 八个 business world 与 expectation 由同一测试作者拟合

source trace 只证明原问题。hidden truth、合同状态、valid design family、forbidden outcome 和
grader 尚无 business/measurement 双审。

Disposition：保持 `candidate/authoring`，不得进入 development/held-out denominator。

### DA-E3. 异步故障不能只做单元测试

单元测试证明局部 fence，仍需 model-based schedule generator 或 property-based state-machine
test 枚举消息、job、review、admission、crash 和 retry interleavings。固定几个 race case
无法证明未来新 job kind。

关闭条件：

- 建立实现无关的 runtime state model；
- 生成 valid/invalid schedules；
- 验证 safety、liveness、idempotency、locality 和 replay；
- 每个新 async job kind 自动继承相同 conformance profile。

## 5. 文档修订要求

Gate 3 计划已加入：

- runtime topology；
- local synchronous commit matrix；
- obligation fan-out/fan-in 与乱序 admission；
- durable async schedule-perturbation suite；
- `JobDispositionRecord`、periodic heartbeat supervisor 和 lineage matrix 工作项；
- 八个真实用户 question slice 与其覆盖边界。

Gate 3 禁止以单进程 `asyncio`、长 HTTP、queue 完成状态、固定 completion order 或单问题
router 代替这些合同。

## 6. 最终状态

当前 amendment 足以让 Gate 0–2 不再把同步 provider 调用误当成目标 runtime，也能机械阻止
correction 后的旧 LLM/effect 进入 authority。Gate 3 仍不能开工生产 measurement kernel：
G3.E0 的 registry/review/calibration/held-out blockers 保持，DA-B1 至 DA-B5 也必须进入
G3.2 exit。
