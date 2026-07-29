import { expect, test, type Page } from "@playwright/test";

const baseTransport = {
  threadHandle: "thread-handle",
  runHandle: "run-handle",
  actionHandle: "run-handle",
  actionKind: "bi_clarification",
  eventsUrl: null,
  eventCursor: "2000",
  latestItemSequence: 1,
  messageHistory: {
    hasMore: false,
    beforeCursor: null,
  },
  acceptedOperationIds: [] as string[],
  technicalDetailRef: null,
};

const waitingSnapshot = {
  schemaVersion: "customer-conversation.v6",
  stateVersion: "2000",
  confirmedAt: "2026-07-20T00:00:02.000Z",
  thread: {
    title: "为什么变化？",
    createdAt: "2026-07-20T00:00:00.000Z",
  },
  messages: [{
    key: "message-key",
    role: "user",
    text: "为什么变化？",
    createdAt: "2026-07-20T00:00:01.000Z",
  }],
  businessUnderstanding: "你想先确定用于解释业务变化的比较基线，再按同一口径继续分析。",
  plannerIssues: [],
  plannerIssueStates: [],
  repairNotices: [],
  state: {
    status: "needs_input",
    phase: "understanding",
    title: "需要你的确认",
    description: "这个选择会显著影响分析结论。提交后，分析会从当前运行继续。",
    updates: [{
      key: "understanding",
      text: "理解业务问题",
      status: "active",
      confirmedAt: "2026-07-20T00:00:02.000Z",
    }],
    input: {
      kind: "clarification",
      title: "需要确认后继续",
      question: "请选择比较基线",
      explanation: "",
      options: [
        {
          optionKey: "previous",
          label: "前一天",
          description: "前一个完整自然日",
          recommended: true,
        },
        {
          optionKey: "rolling",
          label: "近 7 日均值",
          description: "此前七个完整自然日",
          recommended: false,
        },
      ],
      questions: [
        {
          questionKey: "comparison_baseline",
          question: "请选择比较基线",
          explanation: "",
          options: [
            {
              optionKey: "previous",
              label: "前一天",
              description: "前一个完整自然日",
              recommended: true,
            },
            {
              optionKey: "rolling",
              label: "近 7 日均值",
              description: "此前七个完整自然日",
              recommended: false,
            },
          ],
        },
      ],
      allowFreeform: true,
    },
  },
  transport: baseTransport,
};

function singleAdmittedOptionWaitingSnapshot() {
  const onlyOption = waitingSnapshot.state.input.options[0];
  return {
    ...waitingSnapshot,
    state: {
      ...waitingSnapshot.state,
      input: {
        ...waitingSnapshot.state.input,
        options: [onlyOption],
        questions: [{
          ...waitingSnapshot.state.input.questions[0],
          options: [onlyOption],
        }],
        allowFreeform: true,
      },
    },
  };
}

function completedSnapshot(operationId = "operation-accepted") {
  return {
    ...waitingSnapshot,
    stateVersion: "3000",
    confirmedAt: "2026-07-20T00:00:03.000Z",
    state: {
      status: "completed_with_limits",
      phase: "delivering",
      title: "业务参考已生成，结论有适用边界",
      description: "请结合证据边界和限制进行业务判断，重要决策建议由人复核。",
      updates: [
        "理解业务问题",
        "整理分析路径",
        "查询并分析数据",
        "汇总结论与边界",
        "生成业务参考",
      ].map((text, index) => ({
        key: ["understanding", "planning", "querying", "synthesizing", "delivering"][index],
        text,
        status: "completed",
        confirmedAt: "2026-07-20T00:00:03.000Z",
      })),
      answer: {
        blocks: [
          {
            key: "answer-0",
            kind: "summary",
            heading: "核心结论",
            text: "主要业务结论。这个结论包含足够长的业务解释，用来确认页面刷新后仍会完整恢复最终答案。",
          },
          {
            key: "answer-1",
            kind: "limitation",
            heading: "证据边界",
            text: "结论仅适用于当前数据范围。",
          },
        ],
        warnings: ["当前结论需要结合业务背景复核。"],
        evidenceCount: 1,
        limitationCount: 1,
      },
    },
    transport: {
      ...baseTransport,
      actionHandle: null,
      actionKind: null,
      acceptedOperationIds: [operationId],
    },
  };
}

function multiQuestionWaitingSnapshot() {
  const questions = [
    {
      questionKey: "month_phase_definition",
      question: "月初、月中和月末分别包含哪些日期？",
      explanation: "",
      options: [
        {
          optionKey: "month-phase.ten-day",
          label: "1—10日、11—20日、21日至月末",
          description: "三个连续阶段覆盖整个月",
          recommended: true,
        },
        {
          optionKey: "month-phase.week-shaped",
          label: "1—7日、8—21日、22日至月末",
          description: "月初和月末采用七天长度",
          recommended: false,
        },
      ],
    },
    {
      questionKey: "phase_aggregation",
      question: "比较每个阶段的总额还是日均金额？",
      explanation: "",
      options: [
        {
          optionKey: "phase-aggregation.daily-mean",
          label: "比较日均金额",
          description: "控制不同阶段的天数差异",
          recommended: true,
        },
        {
          optionKey: "phase-aggregation.total",
          label: "比较阶段总额",
          description: "比较每个阶段贡献的总金额",
          recommended: false,
        },
      ],
    },
  ];
  return {
    ...waitingSnapshot,
    state: {
      ...waitingSnapshot.state,
      input: {
        ...waitingSnapshot.state.input,
        question: questions[0].question,
        options: questions[0].options,
        questions,
      },
    },
  };
}

function completedBoundarySnapshot() {
  const snapshot = completedSnapshot();
  return {
    ...snapshot,
    thread: {
      ...snapshot.thread,
      title: "4 月 20 日付费金额为什么下降？",
    },
    state: {
      ...snapshot.state,
      answer: {
        ...snapshot.state.answer,
        blocks: [
          {
            key: "answer-0",
            kind: "summary",
            heading: "核心结论",
            text: "4 月 20 日付费金额较前一天下降 7.23%，主要由客单价下降驱动，付费用户数增长抵消了部分降幅。",
          },
          {
            key: "answer-1",
            kind: "finding",
            heading: "驱动机制",
            text: "8 组可比滚动窗口中有 4 组方向一致，方向一致率为 50%。",
          },
          {
            key: "answer-2",
            kind: "limitation",
            heading: "证据边界",
            text: "渠道数据已查询，可用于背景判断和候选定位；渠道合计与市场总量仍有未调和差额，因此不用于直接归因或因果结论。",
          },
        ],
      },
    },
  };
}

function readableCompletedSnapshot() {
  const snapshot = completedSnapshot();
  return {
    ...snapshot,
    thread: {
      ...snapshot.thread,
      title: "2026 年 Q2 付费金额增长由什么驱动？",
    },
    messages: [{
      key: "message-key",
      role: "user",
      text: "2026年Q2相比Q1付费金额提升，主要是付费用户数增加还是单付费用户金额提升带来的？",
      createdAt: "2026-07-20T00:00:01.000Z",
    }],
    state: {
      ...snapshot.state,
      answer: {
        ...snapshot.state.answer,
        blocks: [
          {
            key: "answer-0",
            kind: "summary",
            heading: "核心结论",
            text: "2026 年 Q2 付费金额较 Q1 增长 38.62 亿元，增幅为 16.3%。从同层因素贡献看，单付费用户金额贡献约 25.25 亿元，占增量的 65.4%，付费人数贡献约 13.37 亿元，占 34.6%，所以当前增长首先表现为已有付费用户价值提升。\n\n这个判断影响运营优先级：短期应先解释单个付费用户为什么贡献更多，再判断新增付费用户能否继续扩大增长。付费人数仍然提供了重要增量，但它目前承担的是第二增长来源。",
          },
          {
            key: "answer-1",
            kind: "finding",
            heading: "驱动机制",
            text: "继续拆开单付费用户金额，可以看到增长由付费频次推动，单笔付费金额形成抵消。付费频次由 16.0 次提高到 17.96 次，对总增量贡献 29.53 亿元；单笔付费金额由 2164.3 元下降到 2128.3 元，带来 4.29 亿元的负向贡献。\n\n两项合并后，单付费用户金额净贡献约 25.24 亿元。这说明增长依赖用户更频繁地付费，单次交易价值暂未同步改善。如果后续频次回落，当前增长会缺少单笔金额这一层缓冲。\n\n- 付费频次：主要正向来源\n- 单笔金额：持续负向抵消\n- 付费人数：第二增长来源",
          },
          {
            key: "answer-2",
            kind: "finding",
            heading: "重点定位",
            text: "维度诊断把排查重点指向地区和部分产品属性。地区维度中，拉各斯州的诊断优先级最高，并出现最大的超额变动；设备型号、渠道、网络类型、支付方式和设备品牌也提供了候选定位线索。\n\n这些分数用于安排调查顺序，不能直接理解为各维度对总增长的可加总贡献。运营上更有价值的动作，是先在高优先级地区核对付费频次提升来自哪些用户群和场景，再检查相同模式能否在其他地区复现。",
          },
          {
            key: "answer-3",
            kind: "context",
            heading: "补充观察",
            text: "当前结果来自统一数据快照，覆盖约 181 个窗口聚合组和 2386 万条窗口级记录。核心金额、人数与频次分解通过了现有数据合同和证据校验，可以支撑增长来源判断。\n\n市场健康度和渠道材料适合提供背景与候选方向。它们能帮助解释外部环境和渠道差异，但没有获得与核心付费数据相同的归因强度。",
          },
          {
            key: "answer-4",
            kind: "recommendation",
            heading: "运营建议",
            text: "第一，优先复核高贡献地区中付费频次增长的用户构成、触发场景和留存表现，判断它是可复制的行为变化还是阶段性集中。\n\n第二，单独调查单笔付费金额下降的原因，区分价格、商品结构、折扣和支付场景变化。这样可以在保留频次增长的同时，减少交易价值继续下滑带来的风险。",
          },
          {
            key: "answer-5",
            kind: "limitation",
            heading: "证据边界",
            text: "当前材料支持会计分解、同口径比较和候选维度定位，尚不足以把地区、渠道或设备差异表述为已经证实的因果关系。重要运营决策仍需结合用户行为日志、活动记录或受控实验复核。",
          },
        ],
      },
    },
  };
}

function idleSnapshot(threadHandle = "thread-created") {
  return {
    schemaVersion: "customer-conversation.v6",
    stateVersion: "1000",
    confirmedAt: "2026-07-20T00:00:01.000Z",
    thread: { title: "新分析", createdAt: "2026-07-20T00:00:01.000Z" },
    messages: [],
    businessUnderstanding: null,
    plannerIssues: [],
    plannerIssueStates: [],
    repairNotices: [],
    state: {
      status: "idle",
      title: "准备开始分析",
      description: "输入业务问题后，分析会在后台持续运行并保存当前状态。",
      updates: [],
    },
    transport: {
      threadHandle,
      runHandle: null,
      actionHandle: null,
      actionKind: null,
      eventsUrl: null,
      eventCursor: "1000",
      latestItemSequence: 0,
      messageHistory: {
        hasMore: false,
        beforeCursor: null,
      },
      acceptedOperationIds: [] as string[],
      technicalDetailRef: null,
    },
  };
}

function workingSnapshot(operationId: string, question: string) {
  return {
    ...idleSnapshot(),
    stateVersion: "2500",
    confirmedAt: "2026-07-20T00:00:02.500Z",
    thread: { ...idleSnapshot().thread, title: question },
    messages: [{
      key: "message-created",
      role: "user",
      text: question,
      createdAt: "2026-07-20T00:00:02.000Z",
    }],
    businessUnderstanding: "你想检查当前指标的变化方向，并找出能够解释变化的业务因素。",
    plannerIssues: [
      "确认指标变化是否成立并量化幅度",
      "检查能够解释变化的业务因素",
    ],
    plannerIssueStates: [
      {
        question: "确认指标变化是否成立并量化幅度",
        status: "evidenced",
        statusLabel: "已有证据",
      },
      {
        question: "检查能够解释变化的业务因素",
        status: "querying",
        statusLabel: "查询中",
      },
    ],
    state: {
      status: "working",
      phase: "querying",
      title: "正在查询和分析数据",
      description: "正在查询数据并寻找能够解释业务变化的模式。",
      updates: [
        {
          key: "understanding",
          text: "理解业务问题",
          status: "completed",
          confirmedAt: "2026-07-20T00:00:02.000Z",
        },
        {
          key: "querying",
          text: "查询并分析数据",
          status: "active",
          confirmedAt: "2026-07-20T00:00:02.500Z",
        },
      ],
      safeToClose: true,
    },
    transport: {
      ...idleSnapshot().transport,
      runHandle: "run-created",
      acceptedOperationIds: [operationId],
    },
  };
}

function failedSnapshot() {
  return {
    ...waitingSnapshot,
    stateVersion: "4000",
    state: {
      status: "failed",
      phase: "delivering",
      title: "本次分析未完成",
      description: "本次运行遇到故障，暂时无法生成业务参考。故障已完整记录，可重新发起分析或联系支持。",
      updates: [{
        key: "delivering",
        text: "生成业务参考",
        status: "failed",
        confirmedAt: "2026-07-20T00:00:04.000Z",
      }],
      recovery: "new_analysis",
    },
    transport: { ...baseTransport, actionHandle: null, actionKind: null },
  };
}

function agentWaitingSnapshot() {
  return {
    ...waitingSnapshot,
    transport: {
      ...baseTransport,
      runHandle: null,
      actionHandle: "pending-action:1",
      actionKind: "agent_pending_action",
    },
  };
}

async function installConversationRoutes(
  page: Page,
  initial: Record<string, unknown> = waitingSnapshot,
) {
  let current = initial;
  const initialSnapshot = initial as typeof waitingSnapshot;
  let clarificationPosts = 0;
  const clarificationBodies: Record<string, unknown>[] = [];
  await page.route("**/api/threads", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const snapshot = current as typeof waitingSnapshot;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        threads: [{
          title: snapshot.thread.title,
          status: snapshot.state.status,
          updatedAt: snapshot.confirmedAt,
          transport: { threadHandle: snapshot.transport.threadHandle },
        }],
      }),
    });
  });
  await page.route(
    `**/api/threads/${initialSnapshot.transport.threadHandle}`,
    async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ snapshot: current }),
    });
    },
  );
  await page.route("**/api/runs/run-handle/clarifications", async (route) => {
    clarificationPosts += 1;
    const body = route.request().postDataJSON() as Record<string, unknown> & {
      requestIdentity: string;
    };
    clarificationBodies.push(body);
    current = completedSnapshot(body.requestIdentity);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ snapshot: current }),
    });
  });
  return {
    get clarificationPosts() {
      return clarificationPosts;
    },
    get clarificationBodies() {
      return clarificationBodies;
    },
  };
}

test("Planner 待解决问题显示在右上角独立状态卡", async ({ page }) => {
  const initial = {
    ...workingSnapshot("operation-planner", "检查本周转化变化"),
    transport: {
      ...baseTransport,
      actionHandle: null,
      actionKind: null,
    },
  };
  await installConversationRoutes(page, initial);
  await page.goto("/");

  const card = page.getByLabel("本轮待解决问题");
  await expect(card).toBeVisible();
  await expect(card.getByText("分析中")).toBeVisible();
  await expect(card.getByText("确认指标变化是否成立并量化幅度")).toBeVisible();
  await expect(card.getByText("已有证据")).toBeVisible();
  await expect(card.getByText("检查能够解释变化的业务因素")).toBeVisible();
  await expect(card.getByText("查询中")).toBeVisible();
  await card.getByRole("button").click();
  await expect(card.getByText("确认指标变化是否成立并量化幅度")).toHaveCount(0);
});

test("运行中修复以业务语言出现在主对话且不暴露内部错误", async ({ page }) => {
  const initial = {
    ...workingSnapshot("operation-repair", "检查本周转化变化"),
    repairNotices: [
      "检测到日期比较关系没有完整覆盖原问题，已重新分析并核验修正后的口径。",
    ],
  };
  await installConversationRoutes(page, initial);
  await page.goto("/");

  const repair = page.getByLabel("分析过程修正");
  await expect(repair).toBeVisible();
  await expect(repair).toContainText(
    "检测到日期比较关系没有完整覆盖原问题，已重新分析并核验修正后的口径。",
  );
  await expect(page.getByText("temporal_calendar_partition_baseline_class_invalid"))
    .toHaveCount(0);
});

test("主对话只保留 Planner，任务和查询集中在 composer 上方卡片", async ({ page }) => {
  const initial = workingSnapshot("operation-reasoning", "检查本周转化变化");
  await installConversationRoutes(page, initial);
  await page.route("**/api/agent-runs/run-created/reasoning", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        reasoning: {
          runId: "run-created",
          businessUnderstanding: initial.businessUnderstanding,
          planRevisionId: "plan-revision",
          issues: [
            {
              issueId: "I1",
              parentIssueId: null,
              question: "本周转化变化是否成立？",
              targetClaimKind: "direction",
              status: "unresolved",
              taskIds: ["task-1", "task-2"],
              claimRefs: [],
              usedClaimRefs: [],
              limitationRefs: [],
            },
            {
              issueId: "I2",
              parentIssueId: "I1",
              question: "哪些渠道贡献了变化？",
              targetClaimKind: "driver",
              status: "unresolved",
              taskIds: ["task-2"],
              claimRefs: [],
              usedClaimRefs: [],
              limitationRefs: [],
            },
          ],
          tasks: [
            {
              taskId: "task-1",
              rank: 1,
              taskKey: "trend",
              capabilityId: "metric_timeseries",
              businessLabel: "核验转化趋势",
              status: "succeeded",
              queryStatus: "completed",
              queryCount: 1,
              queries: [{
                resultRef: "result-1",
                queryContractRef: "query-1",
                label: "查询 1 · 每日转化趋势",
                status: "completed",
                rowCount: 14,
              }],
              resultRefs: ["result-1"],
              evidenceRefs: ["evidence-1"],
              claimRefs: [],
              issueIds: ["I1"],
              dependencyTaskIds: [],
              limitationRefs: [],
            },
            {
              taskId: "task-2",
              rank: 2,
              taskKey: "channel",
              capabilityId: "candidate_dimension_screen",
              businessLabel: "检查渠道贡献",
              status: "not_started",
              queryStatus: "not_run",
              queryCount: 0,
              queries: [],
              resultRefs: [],
              evidenceRefs: [],
              claimRefs: [],
              issueIds: ["I1", "I2"],
              dependencyTaskIds: ["task-1"],
              limitationRefs: [],
            },
          ],
          claims: [],
          answerBlocks: [],
          counts: {
            taskTotal: 2,
            taskCompleted: 1,
            queryTotal: 1,
            evidenceTotal: 1,
            claimTotal: 0,
            claimUsedInAnswer: 0,
          },
        },
      }),
    });
  });
  await page.goto("/");

  const timeline = page.getByLabel("分析问题");
  await expect(timeline.getByText("本周转化变化是否成立？")).toBeVisible();
  await expect(timeline.getByText("哪些渠道贡献了变化？")).toBeVisible();
  await expect(page.getByLabel("分析执行过程")).toHaveCount(0);

  const taskCard = page.getByLabel("分析任务");
  await expect(taskCard.getByText("已结算 1/2")).toBeVisible();
  await expect(taskCard.getByText("1 完成")).toBeVisible();
  await expect(taskCard.getByText("1 待处理")).toBeVisible();
  await expect(
    taskCard.getByRole("button", { name: /^核验转化趋势 完成$/ }),
  ).toBeVisible();
  await expect(
    taskCard.getByRole("button", { name: /^检查渠道贡献 等待$/ }),
  ).toBeVisible();
  await expect(taskCard.getByText("查询 1 · 每日转化趋势")).toHaveCount(0);

  await taskCard.getByRole("button", { name: /核验转化趋势/ }).click();
  await expect(taskCard.getByText("查询 1 · 每日转化趋势")).toBeVisible();
  await expect(taskCard.getByText("完成 · 14 行")).toBeVisible();
  await taskCard.getByRole("button", { name: /核验转化趋势/ }).click();
  await expect(taskCard.getByText("查询 1 · 每日转化趋势")).toHaveCount(0);
});

test("澄清只可操作一次，刷新后恢复完整业务参考", async ({ page }) => {
  const observed = await installConversationRoutes(page);
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "请选择比较基线" })).toHaveCount(1);
  await expect(page.getByText(/^我的理解：/)).toHaveCount(1);
  await page.getByRole("radio", { name: /前一天/ }).check();
  await page.getByRole("button", { name: "继续" }).click();

  await expect(page.getByText(/主要业务结论。这个结论包含足够长/)).toHaveCount(1);
  await expect(page.getByRole("heading", { name: "请选择比较基线" })).toHaveCount(0);
  await expect(page.locator(".answer-facts")).toHaveCount(0);
  expect(observed.clarificationPosts).toBe(1);

  await page.reload();
  await expect(page.getByText(/主要业务结论。这个结论包含足够长/)).toHaveCount(1);
  await expect(page.getByRole("heading", { name: "请选择比较基线" })).toHaveCount(0);
  expect(observed.clarificationPosts).toBe(1);
});

test("多个 SQL 口径逐题确认并一次提交", async ({ page }) => {
  const observed = await installConversationRoutes(
    page,
    multiQuestionWaitingSnapshot(),
  );
  await page.goto("/");

  await page.getByRole("radio", { name: /1—7日/ }).check();
  await page.getByRole("button", { name: "下一项" }).click();
  await expect(
    page.getByRole("heading", { name: "比较每个阶段的总额还是日均金额？" }),
  ).toBeVisible();
  await page.getByRole("radio", { name: /比较阶段总额/ }).check();
  await page.getByRole("button", { name: "上一个问题" }).click();
  await expect(page.getByRole("radio", { name: /1—7日/ })).toBeChecked();
  await page.getByRole("button", { name: "下一个问题" }).click();
  await expect(page.getByRole("radio", { name: /比较阶段总额/ })).toBeChecked();
  await page.getByRole("button", { name: "继续" }).click();

  expect(observed.clarificationPosts).toBe(1);
  expect(observed.clarificationBodies[0].selectedOptionIds).toEqual([
    "month-phase.week-shaped",
    "phase-aggregation.total",
  ]);
});

test("合同投影只保留一个有效解释时仍可接受推荐或填写其他口径", async ({ page }) => {
  const observed = await installConversationRoutes(
    page,
    singleAdmittedOptionWaitingSnapshot(),
  );
  await page.goto("/");

  await expect(page.getByRole("radio", { name: /前一天/ })).toBeVisible();
  await expect(page.getByRole("radio", { name: "补充其他口径" })).toBeVisible();
  await page.getByRole("radio", { name: /前一天/ }).check();
  await page.getByRole("button", { name: "继续" }).click();

  expect(observed.clarificationPosts).toBe(1);
  expect(observed.clarificationBodies[0].selectedOptionIds).toEqual([
    "previous",
  ]);
});

test("多题卡混合自定义与固定选项时一次提交各自槽位", async ({ page }) => {
  const observed = await installConversationRoutes(
    page,
    multiQuestionWaitingSnapshot(),
  );
  await page.goto("/");

  await page.getByRole("radio", { name: "补充其他口径" }).check();
  await page.getByRole("textbox", { name: "补充其他口径" }).fill(
    "月初1-5日，月中6-24日，月末25-31日",
  );
  await page.getByRole("button", { name: "下一项" }).click();
  await page.getByRole("radio", { name: /比较阶段总额/ }).check();
  await page.getByRole("button", { name: "继续" }).click();

  expect(observed.clarificationPosts).toBe(1);
  expect(observed.clarificationBodies[0].selectedOptionIds).toEqual([
    "phase-aggregation.total",
  ]);
  expect(observed.clarificationBodies[0].answer).toContain(
    "月初1-5日，月中6-24日，月末25-31日",
  );
  expect(observed.clarificationBodies[0].answer).toContain("比较阶段总额");
});

test("其他口径在澄清卡内填写且 composer 不接管确认", async ({ page }) => {
  const observed = await installConversationRoutes(page);
  await page.goto("/");

  const composer = page.getByRole("textbox", { name: "业务问题", exact: true });
  await expect(composer).toBeDisabled();
  await expect(composer).toHaveAttribute("placeholder", "请先完成上方确认");
  await page.getByRole("radio", { name: "补充其他口径" }).check();

  const customAnswer = page.getByRole("textbox", { name: "补充其他口径" });
  await expect(customAnswer).toBeVisible();
  await customAnswer.fill("使用发薪日前三个完整自然日作为比较基线");
  await page.getByRole("button", { name: "继续" }).click();

  expect(observed.clarificationPosts).toBe(1);
  expect(observed.clarificationBodies[0].selectedOptionIds).toEqual([]);
  expect(observed.clarificationBodies[0].answer).toContain(
    "使用发薪日前三个完整自然日作为比较基线",
  );
});

test("Agent pending action 通过统一消息入口提交 typed resolution", async ({ page }) => {
  let current: Record<string, unknown> = agentWaitingSnapshot();
  const submitted: Array<Record<string, unknown>> = [];
  await page.route("**/api/threads", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        threads: [{
          title: waitingSnapshot.thread.title,
          status: "needs_input",
          updatedAt: waitingSnapshot.confirmedAt,
          transport: { threadHandle: "thread-handle" },
        }],
      }),
    });
  });
  await page.route("**/api/threads/thread-handle", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ snapshot: current }),
    });
  });
  await page.route("**/api/threads/thread-handle/messages", async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    submitted.push(body);
    current = {
      ...completedSnapshot(String(body.requestIdentity)),
      transport: {
        ...completedSnapshot(String(body.requestIdentity)).transport,
        runHandle: null,
        actionHandle: null,
        actionKind: null,
      },
    };
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ snapshot: current }),
    });
  });

  await page.goto("/");
  await page.getByRole("radio", { name: /前一天/ }).check();
  await page.getByRole("button", { name: "继续" }).click();

  await expect(page.getByText(/主要业务结论。这个结论包含足够长/)).toHaveCount(1);
  expect(submitted).toHaveLength(1);
  expect(submitted[0].pendingActionResolution).toEqual({
    actionRef: "pending-action:1",
    decision: "answered",
    selectedOptionId: "previous",
    answerText: "前一天",
  });
  expect(submitted[0]).not.toHaveProperty("topicSelection");
  expect(submitted[0]).not.toHaveProperty("topicChoiceAnswer");
});

test("多标签页复用同一持久化消息操作身份", async ({ browser }) => {
  const context = await browser.newContext();
  let current: Record<string, unknown> = idleSnapshot("thread-created");
  const operationIds: string[] = [];
  await context.route("**/api/threads", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          threads: [{
            title: "新分析",
            status: "idle",
            updatedAt: "2026-07-20T00:00:01.000Z",
            transport: { threadHandle: "thread-created" },
          }],
        }),
      });
      return;
    }
    await route.fallback();
  });
  await context.route("**/api/threads/thread-created", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ snapshot: current }),
    });
  });
  await context.route("**/api/threads/thread-created/messages", async (route) => {
    const body = route.request().postDataJSON() as {
      message: string;
      requestIdentity: string;
    };
    operationIds.push(body.requestIdentity);
    await new Promise((resolve) => setTimeout(resolve, 250));
    current = workingSnapshot(body.requestIdentity, body.message);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ snapshot: current }),
    });
  });

  const first = await context.newPage();
  const second = await context.newPage();
  await Promise.all([first.goto("/"), second.goto("/")]);
  const question = "检查本周转化变化";
  await first.getByRole("textbox", { name: "业务问题", exact: true }).fill(question);
  await second.getByRole("textbox", { name: "业务问题", exact: true }).fill(question);
  await first.getByRole("textbox", { name: "业务问题", exact: true }).press("Enter");
  await second.getByRole("textbox", { name: "业务问题", exact: true }).press("Enter");
  await expect(first.getByText("正在查询和分析数据")).toHaveCount(1);
  await expect(second.getByText("正在查询和分析数据")).toHaveCount(1);

  expect(operationIds).toHaveLength(2);
  expect(operationIds[1]).toBe(operationIds[0]);
  await context.close();
});

test("较旧 event cursor 不会覆盖标签页中的较新 snapshot", async ({ page }) => {
  let current: Record<string, unknown> = {
    ...workingSnapshot("operation-current", "检查本周转化变化"),
    stateVersion: "5",
    transport: {
      ...workingSnapshot("operation-current", "检查本周转化变化").transport,
      eventCursor: "5000",
    },
  };
  await page.route("**/api/threads", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        threads: [{
          title: "检查本周转化变化",
          status: "working",
          updatedAt: "2026-07-20T00:00:05.000Z",
          transport: { threadHandle: "thread-created" },
        }],
      }),
    });
  });
  await page.route("**/api/threads/thread-created", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ snapshot: current }),
    });
  });

  await page.goto("/");
  await expect(page.getByText("正在查询和分析数据")).toHaveCount(1);
  current = {
    ...completedSnapshot("operation-old"),
    stateVersion: "9999",
    transport: {
      ...completedSnapshot("operation-old").transport,
      threadHandle: "thread-created",
      eventCursor: "4000",
    },
  };
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await page.waitForTimeout(250);

  await expect(page.getByText("正在查询和分析数据")).toHaveCount(1);
  await expect(page.getByText(/主要业务结论。这个结论包含足够长/)).toHaveCount(0);
});

test("关闭页面后从 thread snapshot 恢复长任务", async ({ browser }) => {
  const context = await browser.newContext();
  const current = workingSnapshot("operation-long-task", "检查长期留存变化");
  await context.route("**/api/threads", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        threads: [{
          title: "检查长期留存变化",
          status: "working",
          updatedAt: current.confirmedAt,
          transport: { threadHandle: "thread-created" },
        }],
      }),
    });
  });
  await context.route("**/api/threads/thread-created", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ snapshot: current }),
    });
  });

  const first = await context.newPage();
  await first.goto("/");
  await expect(first.getByText("正在查询和分析数据")).toHaveCount(1);
  await first.close();

  const restored = await context.newPage();
  await restored.goto("/");
  await expect(restored.getByText("正在查询和分析数据")).toHaveCount(1);
  await expect(restored.getByText("检查长期留存变化").first()).toBeVisible();
  await context.close();
});

test("首次提问快速重复发送只创建一个会话和一个运行", async ({ page }) => {
  const claimedIds: string[] = [];
  const messageIds: string[] = [];
  let current: Record<string, unknown> = idleSnapshot();
  await page.route("**/api/threads", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ contentType: "application/json", body: '{"threads":[]}' });
      return;
    }
    const body = route.request().postDataJSON() as { requestIdentity: string };
    claimedIds.push(body.requestIdentity);
    expect(route.request().headers()["idempotency-key"]).toBe(body.requestIdentity);
    await new Promise((resolve) => setTimeout(resolve, 120));
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ snapshot: current }),
    });
  });
  await page.route("**/api/threads/thread-created/messages", async (route) => {
    const body = route.request().postDataJSON() as {
      message: string;
      requestIdentity: string;
    };
    messageIds.push(body.requestIdentity);
    expect(route.request().headers()["idempotency-key"]).toBe(body.requestIdentity);
    current = workingSnapshot(body.requestIdentity, body.message);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ snapshot: current }),
    });
  });

  await page.goto("/");
  const composer = page.getByRole("textbox", { name: "业务问题", exact: true });
  await expect(composer).toHaveValue("");
  await composer.fill("请分析本周转化变化");
  await page.getByRole("button", { name: "发送业务问题" }).evaluate((button) => {
    if (!(button instanceof HTMLButtonElement)) throw new Error("send_button_missing");
    button.click();
    button.click();
  });

  await expect(page.getByText("正在查询和分析数据")).toHaveCount(1);
  expect(claimedIds).toHaveLength(1);
  expect(messageIds).toHaveLength(1);
  expect(messageIds[0]).toBe(claimedIds[0]);
});

test("网络结果不确定时沿用同一操作身份恢复", async ({ page }) => {
  let current: Record<string, unknown> = idleSnapshot();
  const messageIds: string[] = [];
  await page.route("**/api/threads", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ contentType: "application/json", body: '{"threads":[]}' });
      return;
    }
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ snapshot: current }),
    });
  });
  await page.route("**/api/threads/thread-created", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ snapshot: current }),
    });
  });
  await page.route("**/api/threads/thread-created/messages", async (route) => {
    const body = route.request().postDataJSON() as {
      message: string;
      requestIdentity: string;
    };
    messageIds.push(body.requestIdentity);
    if (messageIds.length === 1) {
      await route.abort("failed");
      return;
    }
    current = workingSnapshot(body.requestIdentity, body.message);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ snapshot: current }),
    });
  });

  await page.goto("/");
  const composer = page.getByRole("textbox", { name: "业务问题", exact: true });
  await composer.fill("检查留存下降原因");
  await composer.press("Enter");
  await expect(page.getByRole("button", { name: "使用同一提交重试" })).toBeVisible();
  await page.getByRole("button", { name: "使用同一提交重试" }).click();
  await expect(page.getByText("正在查询和分析数据")).toHaveCount(1);
  expect(messageIds).toHaveLength(2);
  expect(messageIds[1]).toBe(messageIds[0]);
});

test("首次创建结果不确定时刷新页面会恢复同一提交", async ({ page }) => {
  const claimIds: string[] = [];
  let current: Record<string, unknown> = idleSnapshot();
  await page.route("**/api/threads", async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ contentType: "application/json", body: '{"threads":[]}' });
      return;
    }
    const body = route.request().postDataJSON() as { requestIdentity: string };
    claimIds.push(body.requestIdentity);
    if (claimIds.length === 1) {
      await route.abort("failed");
      return;
    }
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ snapshot: current }),
    });
  });
  await page.route("**/api/threads/thread-created/messages", async (route) => {
    const body = route.request().postDataJSON() as {
      message: string;
      requestIdentity: string;
    };
    current = workingSnapshot(body.requestIdentity, body.message);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ snapshot: current }),
    });
  });

  await page.goto("/");
  const composer = page.getByRole("textbox", { name: "业务问题", exact: true });
  await composer.fill("核对首次提交恢复");
  await composer.press("Enter");
  await expect(page.getByRole("button", { name: "使用同一提交重试" })).toBeVisible();

  await page.reload();
  await expect(page.getByText("正在查询和分析数据")).toHaveCount(1);
  expect(claimIds).toHaveLength(2);
  expect(claimIds[1]).toBe(claimIds[0]);
});

test("失败状态不会显示活跃进度或工程入口", async ({ page }) => {
  await installConversationRoutes(page, failedSnapshot());
  await page.goto("/");

  await expect(page.locator(".customer-status")).toHaveCount(1);
  await expect(page.locator(".analysis-task-card.failed")).toHaveCount(1);
  await expect(page.locator(".analysis-task-card.failed .progress-spinner")).toHaveCount(0);
  await expect(page.locator("main").getByRole("alert")).toHaveCount(1);
  await expect(page.getByText("运行审计")).toHaveCount(0);
  await expect(page.getByText("已生成业务参考")).toHaveCount(0);
});

test("移动端历史使用抽屉且等待确认时保留其他口径输入出口", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installConversationRoutes(page);
  await page.goto("/");

  await expect(page.getByRole("textbox", { name: "业务问题", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "请选择比较基线" })).toBeVisible();
  const toggle = page.getByRole("button", { name: "打开分析历史" });
  await toggle.focus();
  await toggle.press("Enter");
  await expect(page.locator("#analysis-history")).toHaveClass(/open/);
  await expect.poll(async () => (
    await page.locator("#analysis-history").boundingBox()
  )?.x ?? -999).toBeGreaterThanOrEqual(0);
  await page.keyboard.press("Escape");
  await expect(page.locator("#analysis-history")).not.toHaveClass(/open/);
  await expect(page.getByRole("button", { name: "打开分析历史" })).toBeFocused();
});

test("移动端恢复已完成任务时直接从业务回答阅读", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installConversationRoutes(page, completedSnapshot());
  await page.goto("/");

  const answerStart = page.getByRole("heading", { name: "综合结论与发现" });
  await expect(answerStart).toBeVisible();
  const box = await answerStart.boundingBox();
  expect(box?.y ?? 9999).toBeLessThan(844);
  await expect(page.getByText("业务参考已生成，结论有适用边界")).toHaveCount(0);
  await expect(page.locator(".answer-facts")).toHaveCount(0);
});

test("逐题回答主位只展示业务文本且已核验事实折叠为依据明细", async ({ page }) => {
  await installConversationRoutes(page, completedSnapshot());
  await page.route("**/api/agent-runs/run-handle/reasoning", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        reasoning: {
          runId: "run-handle",
          businessUnderstanding: "核验金额变化并解释主要驱动。",
          planRevisionId: "plan-revision",
          issues: [
            {
              issueId: "issue-direction",
              parentIssueId: null,
              question: "金额变化方向是什么？",
              targetClaimKind: "direction",
              status: "answered",
              answerText: "月初金额低于月末，29 个可比月份中有 22 个月方向一致。",
              taskIds: [],
              claimRefs: ["claim-direction"],
              usedClaimRefs: ["claim-direction"],
              limitationRefs: [],
            },
            {
              issueId: "issue-driver",
              parentIssueId: "issue-direction",
              question: "主要驱动因素是什么？",
              targetClaimKind: "driver",
              status: "unbound",
              taskIds: [],
              claimRefs: ["claim-driver"],
              usedClaimRefs: ["claim-driver"],
              limitationRefs: [],
            },
          ],
          tasks: [],
          claims: [
            {
              proposedClaimRef: "proposed-direction",
              claimRef: "claim-direction",
              claimKind: "direction",
              claimClass: "observed_fact",
              source: "runtime_derived",
              verificationStatus: "accepted",
              summary: "29 个可比月份中有 22 个月月初金额较低。",
              taskIds: [],
              evidenceRefs: ["evidence-direction"],
              issueIds: ["issue-direction"],
              facts: [{ name: "方向一致月份", value: "22/29" }],
              usedInAnswer: true,
              answerBlockIds: ["answer-0"],
              limitationRefs: [],
            },
            {
              proposedClaimRef: "proposed-driver",
              claimRef: "claim-driver",
              claimKind: "driver",
              claimClass: "observed_fact",
              source: "runtime_derived",
              verificationStatus: "accepted",
              summary: "付费人数下降是主要拖累因素。",
              taskIds: [],
              evidenceRefs: ["evidence-driver"],
              issueIds: ["issue-driver"],
              facts: [{ name: "付费人数贡献", value: "64.5%" }],
              usedInAnswer: true,
              answerBlockIds: ["answer-0"],
              limitationRefs: [],
            },
          ],
          answerBlocks: [],
          counts: {
            taskTotal: 0,
            taskCompleted: 0,
            queryTotal: 0,
            evidenceTotal: 2,
            claimTotal: 2,
            claimUsedInAnswer: 2,
          },
        },
      }),
    });
  });
  await page.goto("/");

  const review = page.getByRole("heading", { name: "逐题回答" })
    .locator("..");
  const direction = review.locator("li").nth(0);
  await expect(
    direction.locator(".planner-question-answer"),
  ).toHaveText("月初金额低于月末，29 个可比月份中有 22 个月方向一致。");
  await expect(
    direction.getByText("29 个可比月份中有 22 个月月初金额较低。"),
  ).toBeHidden();
  await direction.getByText("查看依据 · 1 条已核验事实").click();
  await expect(
    direction.getByText("29 个可比月份中有 22 个月月初金额较低。"),
  ).toBeVisible();

  const driver = review.locator("li").nth(1);
  await expect(driver.locator(".planner-question-answer")).toHaveCount(0);
  await expect(driver.getByText("仅综合覆盖", { exact: true })).toBeVisible();
  await expect(
    page.getByText("1/2 已逐题回答 · 1 项仅综合覆盖", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(
    "本次形成了相关事实，但它没有进入最终回答；当前不把这条事实当作这个问题的直接答案。",
  )).toHaveCount(0);
  await expect(driver.getByText(/没有为这个问题单独写出答案/)).toBeHidden();
  await driver.getByText("查看依据 · 1 条已核验事实").click();
  await expect(driver.getByText(/没有为这个问题单独写出答案/)).toBeVisible();
  await expect(driver.getByText("付费人数下降是主要拖累因素。")).toBeVisible();
});

test("完成态只显示一份答案并优先于折叠的分析过程", async ({ page }) => {
  await installConversationRoutes(page, readableCompletedSnapshot());
  await page.goto("/");

  await expect(page.getByText(/付费金额较 Q1 增长 38\.62 亿元/)).toHaveCount(1);
  const answer = page.locator(".business-reference");
  const progress = page.locator(".analysis-task-card.completed_with_limits");
  await expect(answer).toBeVisible();
  await expect(progress).toBeVisible();
  expect(await answer.evaluate((element, other) => (
    Boolean(element.compareDocumentPosition(other as Node) & Node.DOCUMENT_POSITION_FOLLOWING)
  ), await progress.elementHandle())).toBe(true);
  await expect(progress.locator(".analysis-task-detail")).toHaveCount(0);
});

test("结构化回答具有清晰的段落层级且输入区不遮挡滚动区域", async ({ page }) => {
  const browserErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  await installConversationRoutes(page, readableCompletedSnapshot());
  await page.route("**/api/agent-runs/run-handle/reasoning", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        reasoning: {
          runId: "run-handle",
          businessUnderstanding: "核验付费金额变化并解释主要驱动。",
          planRevisionId: "plan-revision",
          repairNotices: [
            "生成的日期比较口径没有完整覆盖业务问题，已重新理解时间关系并核验修正后的分析口径。",
          ],
          issues: [],
          tasks: [],
          claims: [{
            proposedClaimRef: "proposed-context",
            claimRef: "claim-context",
            claimKind: "context",
            claimClass: "observed_fact",
            source: "runtime_derived",
            verificationStatus: "accepted",
            summary: "统一数据快照覆盖当前分析窗口。",
            taskIds: [],
            evidenceRefs: ["evidence-context"],
            issueIds: [],
            facts: [{ name: "窗口聚合组", value: "181" }],
            usedInAnswer: true,
            answerBlockIds: ["answer-3"],
            limitationRefs: [],
          }],
          answerBlocks: [{
            blockId: "answer-3",
            role: "context",
            text: "当前结果来自统一数据快照。",
            claimRefs: ["claim-context"],
            limitationRefs: [],
          }],
          counts: {
            taskTotal: 0,
            taskCompleted: 0,
            queryTotal: 0,
            evidenceTotal: 1,
            claimTotal: 1,
            claimUsedInAnswer: 1,
          },
        },
      }),
    });
  });
  for (const viewport of [
    { width: 1440, height: 1000 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await page.goto("/");

    const summary = page.locator(".answer-section.summary");
    const summaryCopy = summary.locator(".answer-section-copy");
    const finding = page.locator(".answer-section.finding").first();
    const repair = page.getByRole("region", { name: "分析过程修正" });
    await expect(summary).toBeVisible();
    await expect(repair).toContainText("已重新理解时间关系并核验修正后的分析口径");
    await expect(summary.getByRole("heading", { name: "核心结论" })).toBeVisible();
    await expect(finding.getByRole("heading", { name: "驱动机制" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "重点定位" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "运营建议" })).toBeVisible();
    await expect(summaryCopy.locator("p")).toHaveCount(2);
    const typography = await summaryCopy.evaluate((element) => {
      const style = getComputedStyle(element);
      return {
        fontSize: Number.parseFloat(style.fontSize),
        lineHeight: Number.parseFloat(style.lineHeight),
      };
    });
    expect(typography.fontSize).toBe(14);
    expect(typography.lineHeight).toBeGreaterThanOrEqual(24);
    const paragraphSpacing = await summaryCopy.locator("p").first().evaluate(
      (element) => Number.parseFloat(getComputedStyle(element).marginBottom),
    );
    expect(paragraphSpacing).toBeGreaterThanOrEqual(16);
    expect((await page.locator(".business-reference").innerText()).length).toBeGreaterThan(800);
    const inlineEvidence = page.locator(
      ".answer-section.context .answer-inline-evidence",
    );
    const nextActions = page.locator(".answer-next-actions");
    await expect(inlineEvidence).toBeVisible();
    await expect(nextActions).toBeVisible();
    const evidenceBox = await inlineEvidence.boundingBox();
    const nextActionsBox = await nextActions.boundingBox();
    expect(
      (evidenceBox?.y ?? 0) + (evidenceBox?.height ?? 0),
    ).toBeLessThanOrEqual(nextActionsBox?.y ?? Number.POSITIVE_INFINITY);

    const listItems = finding.locator("li");
    await expect(listItems).toHaveCount(3);
    const composerBox = await page.locator(".composer").boundingBox();
    const listBox = await page.locator(".message-list").boundingBox();
    expect((listBox?.y ?? 0) + (listBox?.height ?? 0)).toBeLessThanOrEqual(
      (composerBox?.y ?? Number.POSITIVE_INFINITY) + 1,
    );
    expect(await page.locator("main").evaluate((element) => (
      element.scrollWidth <= element.clientWidth
    ))).toBe(true);
    await expect(page.locator("body")).not.toContainText(/Build Error|Runtime Error|Unhandled Error/);
    if (viewport.width === 1440) {
      const boundaries = page.locator(".answer-boundaries");
      await boundaries.locator("summary").click();
      await expect(boundaries).not.toHaveAttribute("open", "");
      await boundaries.locator("summary").click();
      await expect(boundaries).toHaveAttribute("open", "");
    }
  }
  expect(browserErrors).toEqual([]);

  const evidenceDirectory = process.env.WAJE_VISUAL_EVIDENCE_DIR;
  if (evidenceDirectory) {
    for (const viewport of [
      { name: "desktop", width: 1728, height: 1600 },
      { name: "mobile", width: 390, height: 1200 },
    ]) {
      await page.setViewportSize(viewport);
      await page.goto("/");
      await page.screenshot({
        path: `${evidenceDirectory}/answer-readability-${viewport.name}.png`,
      });
    }
  }
});

test("完成答案在桌面和移动端保持业务术语、精确比例和证据角色", async ({ page }) => {
  await installConversationRoutes(page, completedBoundarySnapshot());
  for (const viewport of [
    { width: 1440, height: 1000 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await page.goto("/");

    await expect(page.getByText(/主要由客单价下降驱动/)).toBeVisible();
    await expect(page.getByText(/方向一致率为 50%/)).toBeVisible();
    await expect(page.getByText(/渠道数据已查询/)).toBeVisible();
    const bodyText = await page.locator("body").innerText();
    expect(bodyText).not.toMatch(/paid_amount|paid_users|略超 50%|略超50%/);
    expect(bodyText).not.toMatch(/地区总体|合约缺口而不完整/);
    expect(await page.locator("main").evaluate((element) => (
      element.scrollWidth <= element.clientWidth
    ))).toBe(true);
  }
});

test("桌面和移动端 DOM 保持客户安全且没有横向溢出", async ({ page }) => {
  await installConversationRoutes(page);
  for (const viewport of [
    { width: 1440, height: 1000 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await page.goto("/");

    const bodyText = await page.locator("body").innerText();
    expect(bodyText).not.toMatch(/[0-9a-f]{8}-[0-9a-f-]{27,}/i);
    expect(bodyText).not.toMatch(/ConversationAgentCore|Authority Publication|Gateway|thread-handle|run-handle/);
    expect(bodyText).not.toMatch(/clarification_source_not_waiting|publication_withheld|运行审计/);
    expect(await page.locator("main").evaluate((element) => (
      element.scrollWidth <= element.clientWidth
    ))).toBe(true);
    const html = await page.locator("main").evaluate((element) => element.outerHTML);
    expect(html).not.toMatch(/threadHandle|runHandle|actionHandle|operationId|dispatchId/);
  }
});
