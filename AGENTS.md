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
- 关键词、路由和澄清策略可以使用业务表达触发，但必须覆盖一类稳定业务意图，并保留合同、权限、证据和 verifier 的硬边界。

## LLM Runtime Principle

- 高价值 LLM 节点默认等待真实回答完成，不用短超时截断业务判断、洞察生成或最终总结。
- LLM 子进程隔离用于保护主进程稳定性；只有显式配置正数 `WAJE_LLM_TIMEOUT_SECONDS` 时，才允许 kill 子进程并按统一 LLM client 重试策略重试。
- timeout、retry、provider 熔断必须集中在 LLM provider 层，业务节点不写分散重试循环，也不使用本地模板补高价值回答。

## Clarification Principle

- 把 ask question 当成可选的澄清分支，用来降低业务误判、证据误用和无效执行成本。
- 遇到会改变业务结论、baseline、时间语义、权限边界、claim 强度或执行成本的歧义时，优先让 LLM 生成 2-3 个业务选项和推荐解释。
- 低风险缺口不打开 ask question，系统采用推荐推断继续，并把假设写入 accepted graph、Answer Package 和 verifier 检查。
- 一旦打开 ask question，它可以阻塞当前 run；选项里必须允许用户接受推荐推断继续，也必须保留 `tell the agent to do differently` 出口。
- LangGraph 流程里，clarify 节点应作为 intent binding / graph compile / graph repair / final verification 的可插入节点。节点输出是业务化选项、推荐假设、用户选择或系统推断，不暴露隐藏推理。
- 本地 compiler 和 policy 负责决定澄清结果能否进入 accepted graph；LLM 可以建议问题、选项和推荐，但不能绕过合同、权限、证据和 verifier。
