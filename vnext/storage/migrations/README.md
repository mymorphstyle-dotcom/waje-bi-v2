# vNext migrations

Gate 1 从 migration version 1 创建独立 PostgreSQL schema `waje_vnext`。migration 不读取、
修改或升级历史 schema。

Gate 2 migration version 2 增加 action record、用户决策请求、effect attempt 与 controller
lease。迁移按版本顺序应用；当前开发期只支持 clean vNext database。

Gate 3.1 migration version 3 将 authority schema 提升到 epoch 3，引入
`QuestionRevision`、typed measurement identity、resolution/obligation/validity/precondition
append-only record，并在数据库层拒绝 Gate 3 `settled` Answer。它只接受空的 epoch-1/2
authority schema；若已有权威数据会明确拒绝，开发环境需重置 `waje_vnext` schema 后从
version 1 重新应用。这里没有旧 Frame payload 的兼容读取或迁移路径。

Gate 3.2 migration version 4 增加 durable model/result saga、obligation
schedule/dispatch/checkpoint、job disposition、message impact binding 与 run trace。

Gate 3.4 migration version 5 增加 accepted Plan adoption、QueryBinding、
conformance execution spec 与 logical execution attempt，使执行能够精确绑定 accepted
measurement authority。

Gate 3.5 migration version 6 直接替换开发期的 Evidence、Answer、validity、
satisfaction、settlement 与 Reviewer 占位合同，并新增：

- immutable capability result envelope、receipt、Evidence admission、Evidence use；
- append-only validity 与 obligation satisfaction 链；
- provisional Answer candidate、逐 claim precheck、Answer claim authority links；
- system-derived settlement precondition report；
- immutable Workflow snapshot/application receipt，以及受单调 CAS 保护的 mutable head。

version 6 保留 Question、Frame、Plan adoption、QueryBinding、resolution、obligation、
execution 与 journal 的外键连续性。旧占位表只要存在一行，迁移就会以 SQLSTATE `55000`
拒绝，并要求重置 disposable development database；没有旧合同读取或兼容分支。

Gate 3.6 migration version 7 直接替换开发期的模型调用持久化合同。若 logical job、provider
attempt/receipt 或 durable result 已有记录，迁移会以 SQLSTATE `55000` 拒绝，并要求重置
disposable development database。version 7 新增：

- logical job 的 configuration、model request artifact、实际 provider request 与 output contract
  identity；
- attempt 的稳定 provider idempotency key、request/config/artifact identity 和同 job prior chain；
- 每个 logical job 最多一个成功 receipt；
- success receipt 与 typed result 的 deferred exact-pair constraint，要求二者在同一事务内共同
  出现并保持 job、attempt 和 output hash 一致；
- attempt number 与 prior attempt 必须形成同 job 连续序列；
- succeeded receipt 必须携带 response ID 和 output hash；
- typed result 通过复合外键绑定 logical job 的 configuration/artifact/output contract，以及
  success receipt 的 job/attempt/output identity。

version 7 没有旧模型调用记录的兼容读取或转换路径。provider 端是否遵守幂等键仍由真实
provider acceptance 验证，数据库约束只证明 WAJE 本地 authority mutation 的原子性。

Gate 3 数据库硬边界：

- production Evidence admission 在 Gate 4 trusted registry 开放前只能写
  `rejected`；
- Answer status 只能写 `provisional`；
- Workflow publication 不接受 `settled`，delivery 不接受 `delivered`，execution
  projection 不接受 generic `completed`；
- 除 `workflow_projection_heads` 外，G3.5 表都由 immutable trigger 禁止更新和删除；
- Workflow head 每次只允许 `version/cursor + 1`，重复、乱序和 stale writer 会失败。

迁移必须通过外层事务同时提交 schema 变更和 `schema_migrations` ledger row。G3.5 的
验收入口 `tools/run_gate3_5_migration_acceptance.py` 只启动临时 Docker PostgreSQL，
不读取项目 `.env`，并注入 ledger failure 验证 version 6 全量回滚。
