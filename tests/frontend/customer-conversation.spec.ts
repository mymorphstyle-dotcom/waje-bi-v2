import { expect, test, type Page } from "@playwright/test";

const baseTransport = {
  threadHandle: "thread-handle",
  runHandle: "run-handle",
  actionHandle: "run-handle",
  actionKind: "bi_clarification",
  eventsUrl: null,
  eventCursor: "2000",
  latestItemSequence: 1,
  acceptedOperationIds: [] as string[],
  technicalDetailRef: null,
};

const waitingSnapshot = {
  schemaVersion: "customer-conversation.v2",
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
      allowFreeform: true,
    },
  },
  transport: baseTransport,
};

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
            text: "主要业务结论。这个结论包含足够长的业务解释，用来确认页面刷新后仍会完整恢复最终答案。",
          },
          { key: "answer-1", kind: "limitation", text: "结论仅适用于当前数据范围。" },
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
            text: "4 月 20 日付费金额较前一天下降 7.23%，主要由客单价下降驱动，付费用户数增长抵消了部分降幅。",
          },
          {
            key: "answer-1",
            kind: "finding",
            text: "8 组可比滚动窗口中有 4 组方向一致，方向一致率为 50%。",
          },
          {
            key: "answer-2",
            kind: "limitation",
            text: "渠道数据已查询，可用于背景判断和候选定位；渠道合计与市场总量仍有未调和差额，因此不用于直接归因或因果结论。",
          },
        ],
      },
    },
  };
}

function idleSnapshot(threadHandle = "thread-created") {
  return {
    schemaVersion: "customer-conversation.v2",
    stateVersion: "1000",
    confirmedAt: "2026-07-20T00:00:01.000Z",
    thread: { title: "新分析", createdAt: "2026-07-20T00:00:01.000Z" },
    messages: [],
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
  let clarificationPosts = 0;
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
  await page.route("**/api/threads/thread-handle", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ snapshot: current }),
    });
  });
  await page.route("**/api/runs/run-handle/clarifications", async (route) => {
    clarificationPosts += 1;
    const body = route.request().postDataJSON() as { requestIdentity: string };
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
  };
}

test("澄清只可操作一次，刷新后恢复完整业务参考", async ({ page }) => {
  const observed = await installConversationRoutes(page);
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "请选择比较基线" })).toHaveCount(1);
  await page.getByRole("button", { name: /前一天/ }).click();

  await expect(page.getByText(/主要业务结论。这个结论包含足够长/)).toHaveCount(1);
  await expect(page.getByRole("heading", { name: "请选择比较基线" })).toHaveCount(0);
  await expect(page.locator(".answer-facts")).toHaveCount(0);
  expect(observed.clarificationPosts).toBe(1);

  await page.reload();
  await expect(page.getByText(/主要业务结论。这个结论包含足够长/)).toHaveCount(1);
  await expect(page.getByRole("heading", { name: "请选择比较基线" })).toHaveCount(0);
  expect(observed.clarificationPosts).toBe(1);
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
  await page.getByRole("button", { name: /前一天/ }).click();

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
  await expect(page.locator(".progress-timeline.failed")).toHaveCount(1);
  await expect(page.locator(".progress-timeline.failed .progress-spinner")).toHaveCount(0);
  await expect(page.locator("main").getByRole("alert")).toHaveCount(1);
  await expect(page.getByText("运行审计")).toHaveCount(0);
  await expect(page.getByText("已生成业务参考")).toHaveCount(0);
});

test("移动端历史使用抽屉且等待确认时不显示普通输入框", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installConversationRoutes(page);
  await page.goto("/");

  await expect(page.getByRole("textbox", { name: "业务问题", exact: true })).toHaveCount(0);
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

test("移动端恢复已完成任务时从业务参考开头阅读", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installConversationRoutes(page, completedSnapshot());
  await page.goto("/");

  const answerTitle = page.getByRole("heading", {
    name: "业务参考已生成，结论有适用边界",
  });
  await expect(answerTitle).toBeVisible();
  const box = await answerTitle.boundingBox();
  expect(box?.y ?? 9999).toBeLessThan(844);
  await expect(page.locator(".answer-facts")).toHaveCount(0);
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
