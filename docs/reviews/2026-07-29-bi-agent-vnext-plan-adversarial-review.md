# WAJE BI Agent vNext 开发计划对抗式自审

## 审查对象

- `docs/plans/2026-07-29-bi-agent-vnext-development-plan.md`
- 初审日期：2026-07-29
- 结论：累计发现 5 个 blocking finding、7 个 major finding。全部在 Gate 0 验收前回写计划。

## 攻击方法

本次自审尝试让计划在以下场景中失效：

1. 用隐式 import、路径注入、旧 schema、旧 build cache 或文档扫描盲区绕过 Day 0 隔离。
2. 让 mutable case head、event journal、UI 或 Reviewer 获得业务内容权威。
3. 用一个证明问题和少量 capability 通过 Gate 3 后缩减 launch 范围。
4. 用“测试通过”文本替代可复验的 Gate evidence。
5. 让用户身份改变数据集、分析路线、证据强度或发布强度。
6. 在 LLM timeout 时用本地模板产出高价值业务回答。
7. 把低风险推断、重大澄清和 Gate 访谈混成同一种交互。

## Findings

| ID | 等级 | Finding | 处置 |
|---|---|---|---|
| AR-01 | Blocking | deletion verifier 只写了 clean-copy 目标，缺少扫描范围、禁止标识和对 dynamic import / shell / SQL / artifact 的检查合同 | 新增 machine-readable forbidden manifest、executable-surface scan、import graph、package artifact scan 和 clean environment 约束 |
| AR-02 | Blocking | `InvestigationCase` 保存 current heads，计划未明确 case 内容与 head 更新的边界，可能演化成可变业务真相 | 明确 case 只保存稳定 identity 与 CAS head pointers；业务内容只存在于 immutable revision/version |
| AR-03 | Blocking | Gate completion 可以通过手工勾选，缺少 evidence manifest 的必需字段和 fail-closed 规则 | 新增 Gate evidence contract；无 command/result/hash/source revision 的 Gate 保持 In progress |
| AR-04 | Blocking | 计划未固化统一分析访问原则，未来可能按用户角色切换 dataset、capability、证据或结论强度 | 增加 single analysis access invariant，身份只用于归属、安全、审计和限流 |
| AR-05 | Major | settled AnswerVersion 未绑定 exact frame/plan/evidence set，也未说明新 head 接受后的历史状态 | 增加 settlement binding 与 current/historical 规则 |
| AR-06 | Major | event journal 的“全序”范围含糊，容易引入无价值全局序列化 | 收窄为 case 内单调 cursor；跨 case 只要求独立持久化与可审计时间 |
| AR-07 | Major | Gate 3 只要求检验一个替代解释，可能忽略 Frame 中其他 material alternatives | 要求所有 material alternatives 均被检验、证伪、降级或写出数据/合同 boundary |
| AR-08 | Major | 核心问题家族覆盖没有显式连接业务 factor SSOT | 把 `contracts/ssot/付费金额影响因子分析.mm` 定义为重建输入；产物复制进 vNext 并带 provenance，runtime 不读取旧路径 |
| AR-09 | Major | Gate 0 只要求最小 Python runtime，未证明 Node workspace 和 dependency manifest 与旧根级入口隔离 | Gate 0 加入独立 root package manifest 与 no-legacy dependency scan；完整 UI build 留在 Gate 6 |
| AR-10 | Major | LLM provider 边界缺少“无高价值模板 fallback”和 timeout 正数显式配置约束 | 增加 provider-only timeout/retry、默认等待真实回答和禁止本地高价值 fallback |
| AR-11 | Major | 澄清策略没有区分 Gate 决策、运行时高影响歧义和低风险缺口 | 增加三层决策协议；运行时 ask_user 给 2–3 个业务选项、推荐项和自由纠正出口 |
| AR-12 | Blocking | Gate 0 初版把 macOS 自带 Python 3.9 当成最低兼容基线，toolchain 反向约束了产品实现 | 撤回 3.9 兼容处理；固定 Python 3.12.13 virtualenv、`requires-python >=3.12` 与 `uv.lock`，clean copy 重建 venv |

## 范围收窄攻击结果

- Gate 3 保持架构证明切片定位。
- Gate 7 继续以问题家族 × factor/capability × claim type matrix 为 launch 门禁。
- capability 需要跨多个问题家族的合同与测试，单题 API 无法进入 registry。
- 历史失败案例只转换为当前 expectation package，不继承旧断言。

## 退出结论

上述 finding 回写后，计划可以进入 Gate 0。Gate 1 仍保留一个显式复核点：
`InvestigationCase` 是否继续作为第五类权威对象。该复核不影响 Day 0 隔离和 Gate 0 骨架。
