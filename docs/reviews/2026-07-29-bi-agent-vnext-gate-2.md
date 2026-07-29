# WAJE BI Agent vNext Gate 2：单主 Agent runtime

## 状态

- Gate：2
- 日期：2026-07-29
- 状态：Complete
- Entry interview：用户确认 WAJE-owned controller 为唯一 runtime 权威
- LangGraph boundary：不进入 authoritative action loop
- 其余用户决策：本 Gate 无需第二项用户决策
- Source branch：`codex/bi-agent-vnext`
- Gate 1 commit：`4525189c`

## 入口结论

Primary Business Analysis Agent 持续拥有开放业务语义，通过 typed business proposal 与
controller 交互。controller 分配系统 identity、执行 action admission、CAS、checkpoint、
resume、outbox、effect retry 和 journal 写入。provider adapter 没有 authority write
权限。

高价值模型调用默认等待真实回答。只有显式正数 `WAJE_VNEXT_LLM_TIMEOUT_SECONDS` 会设置
provider timeout；网络重试集中在 provider adapter。`ask_user` 阻塞当前 run，保留两到三个
业务选项、推荐项和自由纠正入口。

## 实现结果

### Typed Primary Agent binding

- `AgentActionProposal` 只包含 `kind + business payload`。Frame、Plan、Answer 和 interruption
  identity 均由 controller 分配。
- `revise_frame` 返回完整测量草案，覆盖 estimand、观察单位、分子、分母、exposure、
  comparison、assumptions、alternatives、falsification、reversal、success 和 stop
  conditions。
- `revise_plan` 返回完整业务任务图。技术 effect retry 仅追加 attempt/event，不创建
  FrameRevision 或 WorkPlanRevision。
- `propose_answer` 生成 `provisional` AnswerVersion；claim 初始 verifier status 为
  `pending`，settled 仍受 verifier 和 reviewer hard boundary 约束。

### WAJE-owned controller

- `WAJEController` 是唯一 authoritative action loop，覆盖 start、advance、
  run-until-boundary、ask-user resume、effect delivery 和 crash resume。
- 每次 proposal 先绑定当前 ContextPacket/head，再进入 deterministic admission。
- action、authority mutation、journal、receipt、context 和 checkpoint 在同一 store
  transaction 中提交；失败会回滚到上一个 durable checkpoint。
- provider 调用和 effect 执行位于短事务外。提交前通过 controller lease、fencing token
  和 checkpoint hash 复核 in-flight state；并发结果只能有一个进入 authority chain。
- Frame 变化会清空 accepted Plan/Answer heads。后续 Plan/Answer 仍延续 case 级 immutable
  revision lineage。

### Context 与 interruption

- ContextPacket 携带 accepted Frame/Plan/Answer 完整 payload、bounded business event
  projection、Evidence、Decision 和 Reviewer objection index。
- customer projection 为空的 action admission、checkpoint 和 retry attempt 不进入 Primary
  Agent 业务上下文。
- 成功 effect 的 business summary 与 typed result 一同 hash-bound 并进入后续
  ContextPacket；失败重试只进入技术 journal。
- `DecisionRecord` 支持 option selection 和 freeform correction 二选一。checkpoint 保存
  最近用户输入，restart 后无需依赖进程内对话缓存。

### 持久化与 provider

- migration v2 增加 append-only action records、user decision requests、effect attempts
  和可 fencing 的 controller leases。
- In-memory 与 PostgreSQL adapters 实现同一 authority/storage port。
- migration runner 使用 transaction-scoped advisory lock 和 checksum ledger。
- HTTPS Chat Completions adapter 只读取 `WAJE_VNEXT_LLM_` 前缀，密钥不进入 repr、日志或
  acceptance output。
- `tools/run_gate2_provider_acceptance.py` 提供真实 provider 单 turn smoke 入口。本地没有
  vNext provider 配置，因此本 Gate 未发起外部模型请求；Gate 7 的真实 provider eval
  仍按计划执行。

## 对抗式自审

| 发现 | 风险 | 修正 | 结果 |
|---|---|---|---|
| 初始 revise action 只携带 authority ID | 模型可伪造系统 identity，controller 无法构造业务内容 | proposal 改为完整业务草案，identity/revision 由 controller 分配 | Closed |
| 初始 ContextPacket 只携带 accepted ID | Primary Agent 看不到当前测量与调查任务 | 加入 accepted authority payload 与 bounded indexes | Closed |
| freeform correction 无 DecisionRecord 表达 | 用户纠正无法持久恢复 | option/freeform exactly-one contract | Closed |
| checkpoint 未保存最近用户输入 | restart 依赖进程内消息 | ControllerState 增加 `latest_user_message` | Closed |
| Frame 变化后 Plan/Answer lineage 可能重置 | revision number 与 prior chain 分叉 | 通过 event journal 查找 case 级 latest revision | Closed |
| effect business summary 未与结果 hash 绑定 | 后续解释无法证明上下文来源 | result + business summary 共同持久化和 hash | Closed |
| malformed proposal 的顶层错误可能逃逸 provider taxonomy | controller 收到未分类异常 | strict decoder 统一转换为 provider permanent error | Closed |
| repository 可接受与 action 不一致的 outbox/decision request | 可绕过 typed action 边界 | store 验证 action kind、payload binding 与 effect terminal chain | Closed |
| runtime immutable record 的相同重放被视为冲突 | crash retry 无法幂等恢复 | authority event record 与 runtime idempotent record 分开处理 | Closed |
| migration apply 缺少并发锁 | 两个启动者可能竞争 ledger | 加入 advisory transaction lock | Closed |
| provider setting repr 可能暴露密钥 | 调试输出泄密 | `api_key` 标记 `repr=False` | Closed |

Blocking findings：0。

## 验收证据

### Python 与语言中立合同

- Python：3.12.13，项目内 `vnext/.venv`。
- `npm run test:bootstrap`：52 tests，48 pass，4 PostgreSQL environment skips。
- `npm run check:contracts`：通过。
- JSON Schema 新增 `ControllerState`，更新 typed proposal、ContextPacket 和 journal
  events；TypeScript bindings 由 schema 重新生成。

### PostgreSQL

- `npm run test:postgres`：Gate 1 authority/CAS 2 tests passed。
- `npm run test:postgres:gate2`：controller checkpoint/retry/resume、lease fencing 和
  append-only 2 tests passed。
- 数据库：唯一临时 `postgres:17-alpine` container，tmpfs data directory；测试结束后自动
  删除。

### Crash、并发与 retry

- 模拟 checkpoint 前 crash：完整 transaction 回滚，旧 checkpoint/hash 保持不变；相同
  proposal 可再次提交。
- 两个 controller 从同一 checkpoint 并发恢复：只有一个 Frame proposal 被接受。
- transient effect retry：accepted Frame/Plan IDs 和 head version 不变。
- duplicate delivery：成功后 controller 拒绝再次交付；effect attempt terminal chain
  不能扩展。
- 业务口径变化：生成新 FrameRevision，清空 accepted Plan/Answer，并以 PlanRevision 2
  延续 prior chain。

### 删除独立性

- `npm run check`：全部 isolation checks passed。
- clean copy 重新创建 Python 3.12.13 `.venv`、执行 `npm ci`、检查 generated contracts、
  build wheel、compile、运行 52 tests 和 health smoke。
- clean-copy wheel SHA-256：
  `3e88cea26d04f053ed1b8de66c69bbed2d4131ccb2e10b37c14cc00f5f0eb93d`
- clean-copy tree SHA-256：
  `3233941645f43adeea44bae8c655f511c77367dc343ff0f3ef74a5400f8e722c`
- 旧实现、旧测试、旧 runtime、旧 schema、旧 provider config 均未进入依赖图。

## Gate 2 Exit criteria

- [x] 单一 Primary Agent 持有开放业务语义。
- [x] crash/restart 后从 accepted heads 与 event cursor 恢复。
- [x] stale action 不能覆盖新 head。
- [x] 技术失败局部恢复；影响业务口径的变化生成 FrameRevision。
- [x] timeout/retry 只在 provider 或 tool supervision 层发生。

Gate 2 accepted。Gate 3 进入前仍按 `$grill-me` 纪律执行入口判断。
