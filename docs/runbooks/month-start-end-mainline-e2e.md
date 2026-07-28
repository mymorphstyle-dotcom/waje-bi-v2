# 月初/月末主线 Case 端到端续跑

本文档用于在新窗口从当前代码基线重新执行月初/月末分析，检查真实
`/` 页面、Planner、动态 Query IR、任务执行、Claim 结算、Narrative 和
最终发布的完整链路。

## 当前基线

- 当前 PostgreSQL schema authority：`single-authority-workflow.v23`
- 当前 release manifest：`single-authority.final.release.2026-07-29-v59`
- 最近一次已完成的同类运行：
  - thread：`thread-4fe3a8afb742eb1691df29917c8e70d8`
  - run：`run-e7119703407293e445671d37`
  - 查询调用：51/51 成功
  - 持久化查询合同和报告：46/46 complete + ready
- 该历史运行只用于回归对照。新窗口应创建新分析，获取新的 thread/run
  身份，不复用历史结论。

## 启动顺序

在项目根目录 `/Users/luka/work/waje-bi-v2` 操作。

1. 确认 ClickHouse：

   ```bash
   docker compose -f compose.clickhouse.yaml up -d
   docker compose -f compose.clickhouse.yaml ps
   ```

2. 在当前 shell 加载 Python 运行时配置并检查 PostgreSQL：

   ```bash
   set -a
   source .env
   set +a
   PYTHONPATH=. .venv/bin/python \
     -m tools.runtime.validate_general_agent_deployment --database
   ```

   期望 `status` 为 `passed`，并看到 migration
   `single-authority-workflow.v23`。

3. 启动前端 Gateway：

   ```bash
   npm run dev
   ```

4. 在另一个终端启动恢复 worker：

   ```bash
   set -a
   source .env
   set +a
   npm run worker
   ```

5. 在浏览器打开 [http://localhost:3000/](http://localhost:3000/)。
   主线验收使用首页，不使用 `/agent-run-workbench`。

## 测试输入

首次输入：

> 分析2024年1月至2026年5月全量样本中，每月月初是否绝大部分比月末金额高啊？有哪些驱动因子？有哪些例外情况？

如果系统询问日期分段和金额口径，在问题卡中填写：

> 月初1-5日，月中6-24日，月末25-31日；比较各阶段总金额。

澄清内容应作为 Bot 侧确认后的业务表达进入分析过程，不能新增成一条用户
聊天消息。多个会改变 SQL 的问题应在同一紧凑卡片中逐题切换和一次提交。

## 逐阶段验收

1. 业务理解
   - 主对话先出现一句面向业务的理解。
   - 用户原问题只出现一次。
2. Planner
   - Planner 完成后，主对话展示有层级的问题列表。
   - 右上角状态卡持续显示问题状态，用户不需要向上滚动找进度。
3. 任务和查询
   - composer 上方的“分析任务”卡展示全部 Task。
   - 每个 Task 可展开查看独立查询及其 waiting、running、completed、
     limited 或 failed 状态。
   - 主对话不重复渲染大体积 Task 卡。
4. 已核验事实
   - Claim 在用户侧表达为“已核验事实”。
   - 每条事实能追溯 Task、Evidence 和最终回答引用。
   - 软性的 Claim 分类或覆盖漂移只进入审计和结论降级，整次运行继续。
5. 最终回答
   - 先逐题 review Planner 生成的问题，每题明确回答、部分回答或标记本次
     未解决。
   - 随后给出综合结论、驱动因素、例外月份和数据质量边界。
   - 最终回答中的依据入口紧跟对应段落，可以展开回看原事实。
   - 运行终态为 `completed` 或 `completed_with_limits`；只有证据来源、
     数字一致性、SQL/权限/隐私、数据版本或关键数字追溯等硬边界可以阻断。

## 当前门禁命令

```bash
set -a
source .env
set +a
PYTHONPATH=. .venv/bin/pytest -q
npm run build
npm run test:ui
git diff --check
```

如果新运行失败，先保存 thread/run 身份和失败阶段，再检查 Task、Query、
Evidence、Claim、Narrative 的首个断点。不要为这句问题新增专用 SQL、
关键词规则或本地结论模板。
