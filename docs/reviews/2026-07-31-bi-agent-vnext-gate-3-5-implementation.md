# WAJE BI Agent vNext G3.5 实施审查

> 日期：2026-07-31
> 分支：`codex/gate3-5-evidence-answer-continuity`
> 基线：`origin/main@15f61c51`
> 状态：Local closeout complete；ready for PR

## 1. 结论

G3.5 已关闭 conformance realm 内以下连续性：

```text
accepted Plan + QueryBinding
  -> capability result T1
  -> Evidence disposition T2
  -> validity/use/satisfaction
  -> provisional Answer claim precheck
  -> settlement precondition
  -> journal-driven Workflow projection
```

当前实现继续硬拒绝：

- 缺少 Gate 4 trusted physical query/source registry 的 production Evidence；
- settled Answer、publication 和 delivery；
- generic Workflow `completed`；
- selected sensitivity identity 尚未封存进 dispatch/result 合同时的 `run_sensitivity`；
- caller 自报 Evidence admission、claim identity、settlement trace completeness 或
  Reviewer disposition；
- accepted Question/Frame/Plan、window、exposure、scope、unit、grain、data version、
  realm 或 strength 漂移。

G3.E0 verifier 仍派生 `deny_g3_1`。本地代码完成没有提升 external trust、独立双审、
calibration、held-out、promotion 或 frozen run 状态。

## 2. 实施边界

### 2.1 Evidence T1/T2

`EvidenceRuntime` 将 capability completion 分成两个 durable transaction：

1. T1 按 sealed dispatch authority 验证并持久化 immutable result envelope、Evidence、
   receipt 和 journal lineage，不终结 obligation job；
2. T2 重新读取 current accepted authority，派生 admission、validity root、obligation
   satisfaction 与 terminal job disposition。

T1/T2 重投返回同一 canonical record。T1 后 correction 会让旧 result 保留审计，同时令
T2 产生 rejected/never-admitted/superseded disposition。T2 中途失败由事务整体回滚，
receipt 留作恢复锚点。

lease/fencing 使用 storage trusted clock：InMemory 由测试可控 clock 注入，PostgreSQL
使用事务时钟。worker 提供的 `received_at` 只进入审计，不能延长或恢复过期 lease。
Schedule identity 由共享 canonical builder 绑定 run、Plan adoption 与业务权威哈希，
controller 与 repository 都会重算。

### 2.2 Evidence 与 claim

Capability-native `EvidenceRecord` exact 绑定 Question、Frame、Plan、task、estimand、
requirement、obligation、resolution、QueryBinding、logical execution、actual windows、
scope、exposure、data version、realm、result material 与 strength。

`EvidenceUseBinding` 和 `ClaimPrecheckRecord` 由 repository 调用 shared deterministic
compiler 生成。LLM 继续拥有 claim statement 和 typed proposal，无法提交 claim ID、
Answer ID 或绕过 applicability/strength/limitation closure。

### 2.3 provisional Answer 与 settlement

Answer candidate 必须逐字绑定已持久化的 `propose_answer` action。accepted candidate
只产生 provisional Answer，并进入 `waiting_for_review`。

Settlement precondition 读取并核对：

- current accepted heads；
- provisional Answer、claim prechecks 与 EvidenceUseBinding；
- latest Evidence validity 与 obligation satisfaction；
- persisted RunTraceManifest ID/hash 和 Answer/Evidence/obligation coverage；
- 当前 Answer 的 latest Reviewer objection heads。

Caller 提供的 trace hash、completeness 或 objection refs 与持久化事实不一致时，repository
直接拒绝。Conformance Evidence 始终带
`production_evidence_unavailable`，无法取得 future settlement eligibility。

correction 导致 mailbox epoch、accepted Question/Frame/Plan 或 active Frame candidate
变化时，旧 Answer 被标记 `stale_answer_authority`。Evidence、obligation 与 Reviewer
进展分别由 current closure 和 persisted objection heads 校验，合法 Reviewer 进展不会被
粗粒度 snapshot 比较误判为 stale。

### 2.4 Workflow

Workflow reducer 只接受封闭 journal event policy，并通过 storage resolver 读取 immutable
authority record。read model 分开表示：

- execution；
- obligation；
- publication；
- delivery。

每个 cursor 形成 immutable application receipt，mutable head 使用单调 CAS。全量 replay
与 incremental projection 产生同一 snapshot。Frame correction 会 fence active Plan；
迟到事实只进入审计。Evidence revoke 会追加新的 obligation satisfaction fact，并把
Workflow obligation 轴重开为 blocked。

projector 直接消费 durable journal，不增加第二套 projection outbox authority。双 worker
通过 cursor CAS 竞争，commit 后 ACK 丢失可由独立连接重放。

## 3. 实施期间发现并关闭的问题

| 问题 | 根因 | 通用修复 |
|---|---|---|
| PostgreSQL 无法解析 dispatch Workflow fact | journal 引用 logical dispatch ID，resolver 使用 immutable record ID | event、storage admission 与 reducer 统一绑定 `dispatch_record_id` |
| PostgreSQL checkpoint replay 查询失败 | adapter 按 schema 中不存在的 `completed_at` 排序 | 使用 completion record 的持久化 `created_at` 和 ID 确定性排序 |
| 嵌套模型结果写入后无法读回 | typed decoder 未处理递归 `FrozenJson` alias | 增加通用 JSON scalar/tree decoder，覆盖任意合法嵌套结果 |
| RunTraceManifest 缺 Evidence lineage | T1 只发 result-landed event | 同一 T1 事务追加 immutable `EVIDENCE_RECORDED` fact |
| settlement 可接受 caller 自报 trace/reviewer 状态 | repository 直接转发请求参数 | exact replay persisted manifest、current Reviewer heads 与 latest Evidence state |
| accepted Answer retry 在 authority 推进后仍可能返回旧 bundle | PostgreSQL idempotent branch 缺 current-head parity；内存分支只核对 Answer ID | 两种 adapter 共用 accepted-candidate current-authority 验证 |
| InMemory 接受 forged conformance provenance | adapter 没从 persisted spec/attempt 重建 execution provenance | InMemory/PostgreSQL 共用 sealed provenance exact validation |
| check disposition 可由调用方自证 | storage 暴露 positive check map | repository 移除该入口；trusted persisted check execution 缺失时 fail closed |
| satisfaction 混入 claim-use，settlement 不检查 closure 漂移 | obligation fulfillment 与 claim consumption 共用一套集合 | satisfaction 只表达 fulfillment；EvidenceUseBinding/ClaimPrecheck 独占 claim consumption；settlement exact 比较 closure |
| T2 retry 返回后续 validity/satisfaction head | retry path 查询 latest mutable state | 返回首次 canonical T2 outcome；latest state 使用独立查询 |
| backdated receipt time 可绕过 lease expiry | lease 校验使用 worker 时间 | lease/fence 改用 storage trusted clock |
| 任意 schedule ID 可直接写入 repository | canonical ID 只在 controller 生成 | shared builder + 双 adapter 重算拒绝 |
| correction 只推进 mailbox epoch 时旧 Answer 未 stale | settlement 只比较 accepted revision IDs | 单独校验消息与 measurement authority continuity |

这些修复都针对可复用 failure class，没有添加某个问题、日期窗口、Episode 或模型输出的
专用分支。

## 4. 验收证据

| 验收 | 结果 |
|---|---|
| G3.5 专项 | 113 tests passed；普通运行跳过 13 个显式 PostgreSQL 条件测试，随后由 disposable runner 全部执行 |
| 全量 Python | 438 tests passed；跳过 35 个显式 provider/PostgreSQL 条件测试 |
| disposable PostgreSQL | 9 migration/constraint + 7 storage/replay + 10 fault/race tests passed |
| contract drift | JSON Schema、Python schema、TypeScript bindings、measurement identity passed |
| deletion independence | clean copy、Python 3.12.13 venv、wheel、438 tests、health smoke passed |
| G3.E0 | verifier integrity passed；derived entry decision 继续为 `deny_g3_1` |

一次性 PostgreSQL runner 不读取项目 `.env`，只使用临时 Docker database，并验证 migration
006 ledger failure 的全量事务回滚。

## 5. 剩余边界

- G3.6：真实 provider 的 universal measurement behavior eval 与独立 Reviewer grader；
- G3.6/Gate 4：封存 selected sensitivity identity 的 dispatch/result contract 与正向执行；
- Gate 4：physical QuerySpec、真实 capability/source registry、SQL 与 production
  Evidence；
- Gate 5：数字/单位/分母/文字方向 verifier、Reviewer disposition、settled transition、
  publication 与 delivery；
- G3.E0：外部 admission、来源、独立双审、truth review、calibration、held-out 与 frozen
  run。

## 6. 终轮对抗审查

| 审查线 | 首轮/终轮发现 | 关闭证据 | 最终状态 |
|---|---|---|---|
| authority/security | forged provenance、check 自证、closure 漂移、settlement retry、trusted clock、schedule ID、correction continuity | 双 adapter mutation/replay/race tests | Blocking 0、Major 0 |
| async/PostgreSQL | controller 绕过 T1/T2、lease fence、跨连接并发、T2 canonical replay | typed controller、advisory lock、ACK recovery、fault injection | Blocking 0、Major 0 |
| tests/docs/CI | static worlds、expectation coupling、fault/race 缺口、PG 未进 CI、文档边界不清 | 24 executable worlds、13 条 PG 条件测试、root CI、当前文档 | Blocking 0、Major 0 |

终审没有发现业务题、日期、渠道、Episode 文案或某次模型输出的专用规则。
