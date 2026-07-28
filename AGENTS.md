# AGENTS.md

## Communication

- 不用“不是...而是...”这种表达。
- 回答避免 AI 腔。
- 所有实现避免 AI flop。
- 除非用户明确 override，不做过早收窄。

## Implementation Principle

- 所有修复必须有通用适配性，先归纳成业务模式、能力契约、证据边界或运行时策略，再落代码。
- 不为单个测试用例、单句 eval 文案或某次 LLM 偶发输出写定制特例。
- 当测试暴露问题时，先说明它代表的可复用失败类型；如果只能靠单例规则通过，应暂停并重新设计修复点。
- 显式控制指令、固定枚举和硬安全边界可以使用确定性映射；开放业务意图、纠正、挑战和澄清自由文本由 typed LLM binding 处理，本地代码只校验结构、合同、数据访问安全、证据和 verifier 边界，不用关键词字典猜测开放语义。

## Development Compatibility Principle

- 当前处于开发期且没有线上用户，默认只实现当前需求和当前合同，不做向后兼容。
- 需求或合同发生变化时，直接删除或重写约束旧行为的测试、fixture、旧语法解析和兼容分支；禁止为了让旧测试继续通过而保留双轨逻辑。
- SQL 安全、固定敏感输出与数据访问安全、数据合同、证据来源、claim provenance 和 verifier 等硬边界继续严格执行，一律依据当前合同验证。
- 只有用户明确要求兼容既有生产数据、外部调用方或已发布 artifact 时，才设计迁移或兼容方案，并单独说明范围与退出条件。

## Single Analysis Access Principle

- 当前版本所有正常用户使用同一套 BI 分析能力，数据查询、分析路线、证据强度和结论发布强度不得因用户角色或线程 owner 不同而变化。
- 用户身份只用于个人对话历史归属、性能安全、审计和限流，不进入数据集选择、snapshot/release 解析、查询合同、result/artifact 复用或 verifier 判定。
- 用户可见内容采用固定安全投影：业务总结、聚合证据、非敏感诊断、合同与 query/result 引用、完整性和 verifier 结果可以展示；内部审计、密钥、原始行和内部 owner/debug 字段留在服务端。
- 原始标识、聚合粒度、稀疏样本、SQL 安全、数据合同、snapshot/release、证据来源、claim provenance 和 verifier 继续作为与用户角色无关的硬边界。

## LLM Runtime Principle

- 高价值 LLM 节点默认等待真实回答完成，不用短超时截断业务判断、洞察生成或最终总结。
- LLM 子进程隔离用于保护主进程稳定性；只有显式配置正数 `WAJE_LLM_TIMEOUT_SECONDS` 时，才允许 kill 子进程并按统一 LLM client 重试策略重试。
- timeout、retry、provider 熔断必须集中在 LLM provider 层，业务节点不写分散重试循环，也不使用本地模板补高价值回答。

## Local Runtime Bootstrap

- 项目根目录的 `.env` 是本地 PostgreSQL、LLM provider 和分析运行时配置入口。Next.js 开发服务器会自动读取 `.env`，独立 Python 命令不会自动读取。
- 运行真实 Conversation Core、Phase 02/03 acceptance、runtime worker 或直接访问 `PostgresConversationStore.from_env()` 前，统一从项目根目录执行：

  ```bash
  set -a
  source .env
  set +a
  PYTHONPATH=. .venv/bin/python <script> <args>
  ```

- Phase 02 单问题真实计划测试的标准入口：

  ```bash
  set -a
  source .env
  set +a
  PYTHONPATH=. .venv/bin/python \
    tools/phase7/run_single_authority_phase02_acceptance.py \
    plan-once \
    --question '<业务问题>' \
    --artifact-root artifacts/phase7/single-authority-phase02
  ```

- `npm run dev` 正常运行只代表前端进程已加载环境，不能据此判断当前 shell 中的 Python 进程已有 `WAJE_RUNTIME_DATABASE_URL`。
- 不打印、复制或提交 `.env` 内容；排查时只检查必需变量是否存在以及依赖服务是否可达。

## Clarification Principle

- 把 ask question 当成可选的澄清分支，用来降低业务误判、证据误用和无效执行成本。
- 遇到会改变业务结论、baseline、时间语义、固定敏感输出或数据访问安全边界、claim 强度或执行成本的歧义时，优先让 LLM 生成 2-3 个业务选项和推荐解释。用户角色和数据能力等级不得成为澄清项。
- 低风险缺口不打开 ask question，系统采用推荐推断继续，把决定写入 `DecisionLedger`，由 accepted `PlanRevision` 引用，并进入 `AuthorityBundle` 与 verifier 闭环检查。
- 一旦打开 ask question，它可以阻塞当前 run；选项里必须允许用户接受推荐推断继续，也必须保留 `tell the agent to do differently` 出口。
- LangGraph 流程里，clarify 节点应作为 intent binding / graph compile / graph repair / final verification 的可插入节点。节点输出是业务化选项、推荐假设、用户选择或系统推断，不暴露隐藏推理。
- 本地 decision/plan compiler 和 policy 负责决定澄清结果能否进入 accepted `PlanRevision`；LLM 可以建议问题、选项和推荐，但不能绕过合同、固定敏感输出与数据访问安全、证据和 verifier。
