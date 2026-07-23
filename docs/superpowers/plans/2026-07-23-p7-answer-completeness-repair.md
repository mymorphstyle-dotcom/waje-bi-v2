# P7：回答完整性补全闭环

状态：`implemented; accepted`

## 目标

P7 把“流程跑通后，用户仍拿不到完整回答”收敛为三个可观测的结构化缺口，并复用现有单权威
链完成有界修复：

1. accepted `PlanRevision` 的必答 obligation 尚无可用证据时，继续使用现有 claim coverage 与
   `PlanPatch` 补能力、补查询和补证据；
2. 已有证据尚未形成满足 obligation 的 claim 时，继续由 claim settlement 生成 verified claim
   或显式边界；
3. verified claim、显式请求因子的公开基线/目标 facts 和 limitation 已进入
   `NarrativeMaterialProjection`，初稿遗漏必答 requirement 时，在首次 publication 前只增补
   缺失块，再做一次本地安全校验和 advisory verifier 审计。

补全路径有界。数据合同、来源或 Provider 令补全无法闭合时，系统继续发布已经形成的可靠内容，
并通过客户安全 warning 与 limitation 表达适用边界，终局保持 `completed_with_limits`。完整性
检查不产生空白终局。

## 继承的决议

- `IntentRevision -> DecisionLedger -> PlanRevision -> capability -> evidence -> claim ->
  AuthorityBundle -> publication -> delivery` 继续是唯一业务权威链。
- 2026-07-20 ADR 中的人工质量审阅保持 advisory。逐块 verifier 的质量发现、深度评分、可读性、
  行动性和人工评价不触发自动改写，也不获得 publication 否决权。
- P7 的自动补全只读取 accepted obligation 与不透明 requirement/claim/limitation handles。
  它不读取中文关键词，不评分文风，不针对单个题目生成模板。
- SQL 安全、固定敏感输出、数据合同、证据来源、claim provenance、持久化完整性和客户投影安全
  继续 fail closed。
- narrative 补全不重开已封存事实，不改变 claim 强度，不删除初稿，不重排已保留段落。成功补全
  形成同一 `AuthorityBundle` 下的可追溯 narrative revision；补全失败保留初稿。
- 原始 Provider payload、失败详情和模型信息只进入 Workbench。正常客户页只收到客户安全文本、
  聚合事实、limitation 和 warning。

## 失败类型与修复动作

| 缺口层 | 判定依据 | 修复动作 | 无法修复时 |
|---|---|---|---|
| execution | obligation 未覆盖，且 registry 存在 admissible route | 现有 `PlanPatch` 增量补跑 | 显式边界后继续 |
| claim | 证据存在，claim settlement 尚未闭合 | 现有 proposal/verifier/settlement | 降级 claim strength 或显式边界 |
| narrative | 必答 requirement 的 claim/fact/limitation handles 未出现在 required blocks | focused completion revision | 发布初稿并附完整性 warning |
| follow-up material | 已发布 AuthorityBundle 有材料，当前正文未使用 | 从 publication inventory 定位 claim/evidence，再读取客户安全聚合事实 | 明确材料范围，必要时开新 BI revision |
| follow-up delivery | 客户安全只读工具已成功持久化，最终模型组织文本失败 | 交付工具声明的 `customer_summary` 与材料 refs | `completed_with_limits`，技术错误只进 Workbench |
| follow-up selection | 动态工具选择的模型步骤失败，线程已有完整 `bi_publication` | 保全最近一次已发布全文与原 authority refs，明确本轮新增解释未完成 | `completed_with_limits`，不伪装成对新问题的回答 |

## 实施任务

### Task 1：类型化 narrative completeness assessment

- 新增内容寻址的 assessment，逐项记录 requirement 状态、缺失 claim handle 组选项、显式请求
  因子的缺失 public fact handles、缺失 limitation handles 和 source narrative identity。
- 判定只依赖 `NarrativeMaterialProjection.publication_requirements` 与 required blocks。
- assessment 进入 `NarrativeWorkflowResult`，回放时重新计算并验证 digest。

### Task 2：有界 structural completion revision

- 初稿完整时不增加 Provider 调用。
- 初稿缺少必答 handles 时，保留全部原有 blocks，只为未覆盖 requirement 创建增补 target。
- 显式请求 factor 使用 contract metric ref 解析全样本基线/目标 facts；contract-declared dimension
  使用同一成员跨窗口的代表性 summary facts，不从自然语言猜测维度或指标。
- 每个缺失 claim requirement 使用 typed claim kind 映射到稳定 block role；缺失 limitation 使用
  已有 boundary target 规则。
- 增补 writer 只可使用 scoped public claim/fact/limitation handles；合并后必须达到结构化完整。
- 成功后形成第二个 narrative revision，并对新增块运行 advisory verifier。
- 增补 writer 或增补 verifier 调用失败时，保留首版 narrative，记录 exhausted 状态并继续发布。
- 逐块 verifier 的 veto 不触发该补全路径。

### Task 3：追问材料读取完整性

- publication artifact 向通用 Agent 暴露客户安全的 claim inventory，便于从正文遗漏处继续定位。
- claim/evidence artifact 返回有字节和条数上限的聚合 public fact 摘要，包含截断标记。
- 不返回 raw rows、SQL、Provider payload、内部 owner、技术错误或 digest。
- 工具仍要求精确 artifact ref；自然语言关联继续由 typed tool binding 处理。
- 客户安全只读工具通过 typed `failure_recovery=customer_summary` 声明恢复能力。只有 required
  tool 的成功结果已经进入 `ThreadItemLedger`，且失败发生在后续模型组织阶段时，runtime 才可
  直接交付该摘要；普通工具、挂起工具、失败结果和未持久化结果不能进入此路径。
- 该恢复终局固定为 `completed_with_limits`。Provider error code 留在服务端 terminal/Workbench，
  客户侧只接收工具已审阅的摘要、material refs、limitation refs 与安全状态。
- 动态工具选择发生 Provider 或可重试 SDK runtime 故障时，只有当前上下文中存在客户安全
  `bi_publication` 才能保全最近一次完整发布。恢复不生成新的业务判断，不选择工具，也不把旧
  publication 描述成新问题的答案；客户文本明确本轮追加解释尚未完成。
- 上下文装配、工具目录、selection binding、来源闭包或客户投影安全失败继续 fail closed。

### Task 4：标准测试集

自动化案例至少覆盖：

1. 初稿完整，无补全调用；
2. verified claim 已存在但初稿遗漏，插入缺失块并完成 publication；
3. required flag 全部缺失，补全后结构闭合；
4. 补全 Provider 失败，首稿继续发布并携带 warning；
5. verifier veto 仍只进入审计，不触发补全；
6. claim coverage 有 admissible route 时生成 `PlanPatch`；
7. 无 admissible route 时形成 explicit boundary 并继续；
8. publication 追问可以发现正文未使用的 claim；
9. claim/evidence 工具能提供客户安全聚合事实，并执行条数/字节上限；
10. 客户 DOM 不出现 SDK、Provider、trace、内部 ref 或技术错误；
11. 显式请求的 payment outcome factors 绑定全样本基线/目标 facts，并覆盖 accepted axis 中
    payment method/channel 的代表性终态变化；
12. observed final outcome 不被改写成支付效率、延迟、重试、故障环节或因果机制。
13. 客户安全只读工具已完成后，最终模型失败仍交付已持久化摘要；无恢复声明的工具继续失败关闭。
14. 动态工具选择 Provider 失败时，有 publication 的线程保全完整旧发布并标记边界；无
    publication、selection 合同错误或不安全摘要继续失败关闭。

真实验收使用 P6 支付终态问题及两轮追问，检查首答包含支付终态主要事实，追问不会再把已发布
材料描述为缺失。主观洞察质量、可读性和行动性继续进入人工审阅字段。

## 完成标准

- accepted obligation 的数据缺口可进入现有 PlanPatch 补查路径。
- 已封存材料的 narrative 遗漏最多触发一次结构化增补，不由质量 verifier 触发。
- 显式请求 factor 的回答完整性同时检查 claim、必要 public facts 与 limitation，不以“claim 已出现”
  替代数字和维度事实覆盖。
- 增补失败仍能交付可靠首稿，状态与 warning 可观测。
- 追问可以读取已发布但未写入正文的聚合材料。
- 追问的客户安全工具结果已持久化后，最终模型失败不会把可用回答改成空白失败页。
- 追问在工具选择前遇到模型服务故障时，最近一次完整发布仍可交付；客户能够区分旧发布与本轮
  尚未完成的新增解释。
- P7 聚焦测试、Phase 7 相关回归、Provider 出站合同、TypeScript/前端静态合同和 release manifest
  全部通过。
- 真实环境没有 `OPENAI_API_KEY`，唯一模型出站保持 DeepSeek Chat Completions。
