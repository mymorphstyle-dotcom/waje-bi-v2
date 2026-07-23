# 下一轮优化计划：回答完整性与客户对话可读性

状态：`implemented-and-verified`
范围：本轮实现与验收记录；本文件不改变当前运行时、发布或人工审核权威。

## 实施与验收结果

- Gateway 已按 operation identity、item type 与 terminal admission 建立唯一显示面，完成态答案只在
  客户 DOM 出现一次；BI terminal 严格绑定 exact durable task publication，并恢复 typed block。
- General Agent 对已发布 evidence/claim 的解释获得客户安全公式上下文、分解方法和已发布数字；
  原始 fact、Provider payload、技术错误和内部 ref 继续留在 Workbench。
- 当前 operation 的 tool result 直接参与最终 source closure，回答完成后不再依赖一次新的上下文
  压缩；摘要输出经过确定性的 source-admission，未知引用与失去权威来源的 statement 不进入摘要。
- Dynamic Tool Discovery 获得 typed `publishedAnalysisTasks` 修订目标；已发布任务的 material revision
  选择 `continue_bi_analysis`，新调查继续使用 `run_bi_analysis`，不使用开放语义关键词判断。
- 评测集现含 10 个真实措辞 case，覆盖 direct、能力说明、分析、两类澄清、异常值、证据追问、
  结论挑战、边界追问和 material revision。人工完整性观察保持 `pending_human_review`，不参与
  hard pass、重试或发布状态。
- 真实 DeepSeek 累积验收中，其余 case 已由完整运行覆盖；本轮修复后的证据追问为 1/1 通过，
  material revision 为 1/1 通过。两次报告均记录 `openAiApiKeyPresent=false`、
  `openAiHostedRequestCount=0` 与 `outboundOrigin=https://api.deepseek.com`。
- 自动化验收：Phase 7 `1549 passed, 40 skipped`；Phase 8 `16 passed`；权威查询链 `7 passed`；
  TypeScript 与 Next.js production build 通过；Playwright `15 passed`。
- 浏览器代表性验收覆盖桌面与 390px 移动端：无横向溢出，唯一答案，移动端滚动到底后答案与
  composer 保留 51.75px clearance。截图用于视觉复核，DOM 与 computed-style 断言承担回归门禁。

## 目标

下一轮同时收敛两个用户可感知问题：

1. 通用 Agent 跑完整条权威链后，回答应完整覆盖已接受问题中的主要比较、因子层级、关键证据、
   局部限制和可执行下一步；连续追问应复用正确的已发布材料。
2. 客户对话页面应让主结论、分析依据、限制和下一步形成清晰阅读层级；段落、列表、字体、颜色、
   留白和滚动位置在桌面与移动端都可读。

计划以 2026-07-22 用户提供的 1728×1117 桌面截图为当前视觉基线。截图暴露的主要问题包括：
终局回答重复出现、运行状态与业务回答混在同一阅读流、长段落缺少垂直节奏、正文与次要信息
对比不足、输入框遮挡底部内容、侧栏信息密度偏高。

## 已锁定决议

本轮实施必须继承以下现行合同，不修改其含义：

- `IntentRevision`、`DecisionLedger`、accepted `PlanRevision`、能力执行、证据、claim、
  `AuthorityBundle`、publication 和 delivery 继续构成唯一 BI 权威链。
- OpenAI Agents SDK 只负责进程内模型—工具循环；`AgentTurnRuntime`、PostgreSQL 和既有 BI
  LangGraph 继续承担状态、恢复和业务权威。
- 大陆模型 Provider 继续作为唯一模型出口；运行链不新增 OpenAI 托管能力。
- 回答质量、完整性评分、解释深度、潜在幻觉和视觉表达问题进入 Workbench 与人工审核，
  不改变 `publication_ready`，不触发自动 writer retry、自动改写或首次交付撤回。
- 权限、固定敏感输出、SQL 安全、数据合同、证据来源、claim provenance、持久化和客户安全
  投影完整性继续作为硬边界。
- 不使用关键词字典解释开放业务语义，不增加本地高价值回答模板，不为某一句 eval 或某个
  DeepSeek 偶发输出写特例。
- 不把 SDK、Provider、trace、内部 UUID、digest 或技术错误投影到客户 DOM。
- 当前处于无线上用户的开发期；新合同直接替换冲突的旧测试或旧显示逻辑，不保留双轨 UI。

## 问题分层

### 结构性问题

1. **一个终局回答占用两个显示面。** 当前 General Agent 的最后一条 assistant item 已进入
   `messages`，`stateFromAgentHead` 又把同一文本投影成 `state.answer`，页面随后同时渲染
   `MessageResponse` 与 `AnswerMessage`。
2. **持久化进度 item 被当成普通助手正文。** `conversation_messages.item_type=progress` 在
   Gateway 读取后被压成只有 `role/text` 的 `CustomerMessage`，页面无法把“已进入持久化执行队列”
   放回进度区域。
3. **General Agent 包装后的 BI publication 丢失块级角色。** durable BI task 已有客户安全
   publication blocks，Agent terminal 仍只把拼接后的 markdown 交给页面，导致 summary、finding、
   context、limitation 和 recommendation 的视觉层级被压平。
4. **Markdown 内部没有明确排版节奏。** 当前 CSS 主要设置 `[data-streamdown]` 根节点间距，
   Tailwind reset 后的 `p`、`ul`、`ol`、`li`、`h2/h3`、table 和 blockquote 缺少完整规则。
5. **底部阅读区域未为 composer 预留足够空间。** 长回答末尾会进入固定输入框后方，用户难以
   连续阅读或确认内容是否结束。

### 视觉打磨问题

- 14px 长正文在当前暗色背景上阅读负担偏高。
- 主正文、次要说明、时间、状态和限制之间的色阶过近。
- 业务参考标题、主结论、一般发现和限制之间的段前段后距离不足。
- 侧栏连续条目的标题、状态和时间挤在较窄区域，活跃条目与历史条目的区分仍可加强。
- 已完成进度虽然折叠，仍与最终回答争夺首屏注意力。

## 实施任务

### Task 0：冻结当前基线与回归 fixture

**涉及文件**

- 修改：`tests/frontend/customer-conversation.spec.ts`
- 新增：`tests/frontend/fixtures/` 下的稳定客户安全长回答 fixture（如现有目录约定不同，沿用现有
  测试数据位置）
- 运行时生成：`output/playwright/answer-readability-<timestamp>/`

**工作内容**

- 保存当前 1728×1117 桌面状态的等价本地 fixture，并补充 1440×900、390×844 两个视口。
- fixture 至少包含 user message、持久化进度、完成状态、summary、两条 finding、limitation、
  recommendation、长列表和两段连续正文。
- 测试只使用客户安全投影，不把 Provider payload 或内部 ref 注入浏览器。
- 在任何实现改动前写出会失败的断言：终局正文出现一次、进度文案不进入普通 assistant prose、
  composer 不遮挡答案末尾、主要文本达到约定字号与间距。

**完成边界**

- 基线截图、DOM 断言和计算样式断言可以稳定重现当前问题。
- 默认 `npm run test:ui` 在冷启动环境可重复运行。

### Task 1：建立“一条持久化 item，一个客户显示面”投影规则

**涉及文件**

- 修改：`app/api/_conversationStore.ts`
- 修改：`app/api/_customerAnalysisContract.ts`
- 修改：`app/page.tsx`
- 测试：`tests/phase7/test_gateway_typescript_contract.py`
- 测试：`tests/frontend/customer-conversation.spec.ts`

**合同**

Gateway 内部保留已持久化 `item_type`，并在构造 snapshot 时按状态决定显示面：

1. 当前 `progress`、`clarification`、`approval_request` 由 `state` 区域显示；
2. 当前 terminal assistant item 由 `state.answer` 显示；
3. 已结束的历史问答继续作为普通 conversation messages 回放；
4. 每个 item 仍完整保存在 `ThreadItemLedger`，只调整客户 snapshot 的呈现归属；
5. 归属判断只使用 item type、operation identity、terminal admission 和 state version，不比较文本，
   不使用关键词。

**工作内容**

- 让 PostgreSQL 与 in-memory store 的内部 `MessageRecord` 都保留 `itemType` 和必要的 operation
  identity，保持两种 store 行为一致。
- 新增一个确定性的 presentation projection helper，过滤当前 state 已经拥有的 item。
- 页面删除与新投影冲突的双轨渲染分支。
- 完成态进度保留为紧凑可展开区域；用户仍可查看已经确认的执行过程。

**完成边界**

- 当前终局回答在 DOM 和复制内容中只出现一次。
- “任务已进入持久化队列”只显示为真实进度，不形成独立业务回答段落。
- 刷新、多标签页、断线恢复和历史分页后仍保持同一显示归属。
- SDK 类型、item digest 和 operation key 不进入客户合同或 DOM。

### Task 2：恢复 BI publication 的客户安全块级结构

**涉及文件**

- 修改：`app/api/_conversationStore.ts`
- 修改：`app/api/_customerAnalysisContract.ts`
- 可能修改：`app/api/_customerPublicationContract.ts`（仅当现有 role 映射不足）
- 测试：`tests/phase7/test_gateway_route_runtime.py`
- 测试：`tests/phase7/test_gateway_typescript_contract.py`
- 测试：`tests/frontend/customer-conversation.spec.ts`

**合同**

- `agent-terminal-admission.v1` 已持有 `completionKind=analysis_publication` 与 `durableTaskRef`。
  Gateway 应严格读取这两个 WAJE 内部字段，绑定该 task 的 exact published customer payload。
- `analysis_publication` 使用 publication 自带的 customer-safe blocks 构造 `CustomerAnswer`；direct、
  context 和普通 tool response 保持 markdown 回答。
- publication block 的 role 只映射到现有 `summary | finding | context | limitation |
  recommendation`。未知或冲突 role 产生可观测的 projection contract violation，不猜测语义。
- 页面不接收 claim/evidence 原始结构，不接收 raw Provider payload。

**工作内容**

- 扩展 `agentTerminalFromPayload` 的内部解析，验证 terminal admission schema、completion kind 和
  durable task binding。
- 对 analysis publication 加载 exact task publication，并复用既有 `answerFromPublication`。
- 如果合法 direct response 没有 publication，继续渲染一个 markdown summary block。
- 删除把完整 BI publication 永久压成单一 summary block 的现行路径。

**完成边界**

- 主结论、一般发现、背景、限制和建议在客户投影中保留原有 typed block 边界。
- 恢复、分页和 SSE 更新不能改变 block 顺序或 block identity。
- 该绑定只决定展示结构，不改变 narrative、publication 或 delivery 权威。

### Task 3：重建对话阅读层级和暗色排版节奏

**涉及文件**

- 修改：`app/globals.css`
- 修改：`app/page.tsx`
- 必要时修改：`components/ai-elements/message.tsx`（只增加稳定 class/data hook，不改 markdown
  内容）
- 测试：`tests/frontend/customer-conversation.spec.ts`

**视觉规则**

1. **正文尺度**
   - 桌面业务正文目标字号 15–16px，行高 1.75–1.85；移动端不低于 15px。
   - 业务参考正文最大宽度以约 720–780px 为起点，在真实中文内容下复核每行长度。
   - summary 比普通 finding 高一个字号层级，避免使用大面积粗体。
2. **垂直节奏**
   - 相邻段落间距 12–16px；typed block 之间 20–28px；section 标题与正文之间 8–12px。
   - 列表项间距 6–10px，列表与前后段落至少 14px。
   - `h2/h3`、paragraph、list、table、blockquote、code block 分别定义 Streamdown 内部规则，
     不依赖浏览器默认 margin。
3. **颜色层级**
   - 主正文、次要说明、metadata、限制、成功、警告使用现有 token 家族重新分层。
   - 正常正文与背景目标对比度至少 4.5:1；大文本、图标和非文字控件至少 3:1。
   - 限制使用颜色加标题或边界样式表达，不能只靠黄色区分。
4. **页面结构**
   - final answer 成为完成态第一阅读焦点；已完成进度收为一行 summary。
   - composer 上方保留足够 scroll clearance，并设置 `scroll-padding-bottom`，答案最后一行可以
     完整滚到输入框上方。
   - 用户气泡、业务回答、限制和建议保持现有暗色设计语言，不新增无意义卡片或装饰。
   - 侧栏适度增加条目纵向间距、活跃项对比和 metadata 可读性；保持高密度历史浏览能力。

**响应式与无障碍检查**

- 390px、768px、1440px、1728px 宽度无横向溢出。
- 200% zoom 下主回答、限制和 composer 仍可完整访问。
- 键盘可聚焦 history、details、composer 和提交按钮，focus ring 清楚。
- `prefers-reduced-motion` 下自动滚动和 spinner 不造成持续运动。
- 用自动 contrast/axe 检查加人工截图复核；截图本身不能证明完整 WCAG 合规。

**完成边界**

- 长回答中每个段落、列表、限制和建议在截图中可快速区分。
- 页面首次完成后滚动到唯一业务参考开头；答案末尾不被 composer 遮挡。
- 客户复制内容不包含重复回答、进度文案或内部 ref。

### Task 4：扩展回答完整性与连续追问真实评测

**涉及文件**

- 修改：`evals/general_agent_runtime/cases.jsonl`
- 修改：`evals/general_agent_runtime/run_local.py`
- 修改：`evals/general_agent_runtime/README.md`
- 测试：`tests/phase7/test_general_agent_runtime_eval.py`
- 按失败模式修改：`bi_agent/runtime/narrative_workflow.py`、
  `bi_agent/runtime/agent_tool_discovery.py`、`bi_agent/runtime/analysis_artifacts.py` 或对应 typed
  binding；只有出现可复用失败类型时才进入代码修改。

**评测组**

1. direct response 与能力说明；
2. 明确 BI 分析，主结论先行；
3. 同层主因子比较与复合因子内部解释；
4. 已发布 evidence/claim/limitation 追问；
5. 用户纠正和挑战原结论；
6. 会改变结论的 baseline、时间或范围澄清；
7. 已发布任务的 material revision；
8. Provider、数据合同或执行失败的真实终局。

每个 case 使用“真实用户措辞 + typed expectation package”，至少检查：

- action/tool route；
- 是否创建了不必要的新 BI task；
- accepted intent/plan obligation coverage；
- publication、artifact 和 material ref closure；
- 数字、日期、单位和结论强度 fidelity；
- 主要问题是否在回答前部得到回应；
- 客户 DOM 是否只出现一个终局回答；
- 人工完整性、清晰度、篇幅和视觉评分。

最后一组人工评分只进入评测报告和 Workbench review queue。运行时不根据评分阻断、自动重写、
重试或撤回交付。只有重复出现、人工确认、可归纳且经业务 owner 与系统 owner 批准的模式，
才进入后续合同或能力改进提案。

### Task 5：真实 DeepSeek、浏览器和发布边界验收

**执行顺序**

1. 运行 Task 1–3 的 TypeScript 与 Playwright focused tests；
2. 运行 Task 4 的 Python contract/eval tests；
3. 显式删除 `OPENAI_API_KEY`，通过当前 DeepSeek 配置运行 10-case 真实会话矩阵与失败项定向回归；
4. 每个不同客户视觉状态保存代表性 desktop/mobile 截图；逐 case 保存客户 snapshot、Workbench
   trace 与待人工点评包。视觉相同的 case 共用 UI 证据，case 行为仍逐项验收；
5. 运行完整 Phase 7、相关 Phase 8、TypeScript、Next production build 和默认 Playwright suite；
6. 复验唯一出站 origin、Chat Completions path 和 WAJE-only trace；
7. 更新当前文档与 release manifest，仅记录已经通过的合同。

**最终门禁**

- 一个 turn 只有一个客户可见终局回答。
- progress、input request、answer 和 historical message 的显示归属稳定且可恢复。
- BI publication 保留 typed block 层级，direct response 仍支持自由 markdown。
- 同层因子、证据追问、纠正、澄清和 material revision 的真实评测全部跑通。
- 人工质量低分不会改变交付状态，也不会增加自动 writer 调用。
- 页面在四个目标视口和 200% zoom 下可读，正文、段落、颜色和 composer clearance 达标。
- 客户 API 和 DOM 中没有 SDK、Provider、trace、内部 ref 或技术错误。
- `api.openai.com` 请求数为 0，OpenAI 默认 trace exporter 使用数为 0。

## 明确不做

- 不新建或重跑 Case B。
- 不实现 Hosted Multi-Agent 或新的多 Agent 产品逻辑。
- 不增加本地高价值答案模板、关键词语义路由或测试句专用规则。
- 不把人工评分、视觉评分或措辞偏好升级为发布门禁。
- 不改写既有 publication、delivery、evidence 或 claim authority。
- 不做与当前对话阅读问题无关的品牌重塑、插画、动效或新页面。

## 交付物

- 客户 snapshot 单显示面合同及回归测试；
- 保留 publication block role 的 General Agent 客户投影；
- 桌面与移动端暗色排版 token、Streamdown 节奏和 composer clearance；
- 10-case 真实 DeepSeek 连续会话评测与失败项定向回归报告；
- 完成、澄清、执行中和失败等不同客户视觉状态的代表性前端截图，以及逐 case 人工点评包；
- 现行合同文档、测试结果和下一阶段遗留清单。
