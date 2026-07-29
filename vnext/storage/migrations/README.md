# vNext migrations

Gate 1 从 migration version 1 创建独立 PostgreSQL schema `waje_vnext`。migration 不读取、
修改或升级历史 schema。

Gate 2 migration version 2 增加 action record、用户决策请求、effect attempt 与 controller
lease。迁移按版本顺序应用；当前开发期只支持 clean vNext database。
