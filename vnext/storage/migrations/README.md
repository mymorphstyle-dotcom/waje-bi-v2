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
