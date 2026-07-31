# Gate 3 WAJEgame Episode Suite：组合对抗式审查

日期：2026-07-31
范围：36 个 WAJEgame Required Episode、4 个 transfer probe、Episode v3、
Gate3EvalPolicy v3、catalog/review/readiness authority chain。
结论：结构重组通过；Episode promotion 与 G3 formal admission 继续 Blocked。

## 1. 审查方法

主审与三个独立审查分支分别检查：

- WAJEgame 业务真实性与当前 ClickHouse 能力边界；
- measurement/evidence regression 是否覆盖通用失败类型；
- durable async authority stress 是否覆盖 Reviewer、correction、stale effect、
  duplicate delivery、restart 和 contract release；
- policy、schema、generator、validator、view、review package、readiness 和 hash
  authority 是否同步；
- transfer probe 是否能影响 Gate verdict。

审查不使用行业关键词作为业务真实性判定器。代码只验证 typed suite binding、accepted
dataset vocabulary、data realm、core hash、source/review authority 和 Gate 可达性；最终
业务真实性仍由独立 business owner 与 measurement reviewer 审核。review package 额外
要求检查 title、用户问题、business world、outcome、estimand 和 counterfactual 的语义
一致性，防止标题已换域、内部案卷仍沿用旧测量骨架。

## 2. Blocking finding 与处置

| Finding | 处置 |
|---|---|
| 测量组重复跨月/exposure，丢失 residual 与 causal ceiling | 恢复 `G3-GF-009`、`G3-GF-010`；`G3-EXP-002` 改为用户/渠道 mix 反转；退出 `G3-ADV-011/012` |
| Authority 组缺 Reviewer 否决、stale result、恢复场景 | Episode v3 增加 scheduled business-visible events；8 个 authority Episode 分别覆盖 duplicate、Reviewer objection、stale effect、restart 与 contract release |
| `suite_binding` 未进入 Episode core | generator 与 validator 的 core 同时加入；EvaluatorOracleView 和 review package 可见，AgentWorldView 禁止 |
| generator 读取任意 candidate JSON | 改为 policy 固定的四文件 allowlist，union 前执行 schema/semantic 检查，union 后要求精确 36 |
| policy/readiness 仍按 source pool 配额 | policy v3 开启 WAJEgame authority epoch；pool 仅保留 provenance；readiness 要求每个 Required Episode 有 verified source，并使用全局独立来源 floor |
| current/hybrid/missing realm 语义混淆 | data realm 增加 `support_class`；accepted dataset 使用封闭 vocabulary；hybrid/known-missing 必须形成 typed contract boundary |
| `G3-EXP-001` 表面已改成 WAJEgame，案卷内部仍残留餐饮订单、食材、门店和外卖补贴合同 | 整体重写 business world、truth、estimand、claim target、boundary 与 counterfactual；支付成功金额和 GGR 分别测量，充值活动曝光及支付到玩法关联缺失时只允许相关性表述 |
| `G3-EXP-002` 已改成用户价值 mix 反转，内部 outcome 仍要求跨月窗口与 exposure | 按每付费用户金额重写完整 measurement envelope；用渠道 × 新老付费用户标准化和可核对 decomposition 验证 Simpson 型反转 |
| `G3-ROOT-001` 仍保留续约、奖金、协作账户等 SaaS 语义 | 重建为登录活跃、付费活跃、玩法活跃及 canonical player 映射的 WAJEgame 指标族治理问题 |
| JSON schema 无法发现同一对象中的重复 key，手工改写可能静默覆盖字段 | corpus builder 在 schema 前采用 duplicate-key rejecting parser，并增加回归测试 |
| transfer 与 Required 共享派生链风险 | 4 个 probe 移到 `research/`；catalog、review、promotion、run、readiness 均执行 ID 不相交检查；research 文件不进入 admission hash |
| 旧 v2 hash/review/package 可能被复用 | policy/schema/core hash 全部变化，派生 catalog、registry、profile、review package、ledger、readiness 重新生成；无兼容 alias |

## 3. WAJEgame 数据边界

Required Episode 的真实数据语境固定为 Nigeria、NGN、Africa/Lagos 和当前发布范围。

- `current_clickhouse` 只引用 accepted paid-order、payment-final-outcome、
  market-dashboard、gameplay 与 channel dataset。
- refund/reversal/chargeback、payment failure stage/reason/retry、campaign exposure/control、
  ROAS incrementality、product/operation event timeline、gameplay-to-payment attribution、
  paid-active retention 等缺口进入 hybrid 或 known-missing realm。
- `paid_amount`、gameplay bet amount、gameplay profit/GGR 保持独立指标身份。
- gameplay activity 与 paid amount 只能形成并列/关联证据，缺少合同不能升级为贡献归因。
- 所有 raw user/order ID 仍受隐私与聚合边界控制。

## 4. 最终 36 题结构

| Group | Count | 验收义务 |
|---|---:|---|
| launch question | 8 | 用户提供的八类付费金额问题原话 |
| business chain | 10 | 拉新、支付、价值、渠道、玩法、GGR、事件和定义边界 |
| measurement regression | 10 | calendar/exposure/partial period/funnel/residual/mix/metric/time/causal |
| authority stress | 8 | ambiguity/review/correction/scope/stale effect/restart/recovery |

完整 suite 必须覆盖 14 个 factor group、8 个 question family、4 个 data realm 和 canonical
coverage taxonomy。单个 Episode 只承担相关交叉项；Gate 由完整 Required 集合验收。

## 5. 尚未关闭的门禁

本轮不伪造 gold 状态。以下条件继续阻止 formal admission：

- protected external admission 与真实 Sigstore bundle；
- 每个 Required Episode 的 verified source authority；
- 36 个 Episode 的独立 business/measurement 双审；
- 66 个 truth fact 的 identifiability/support review；
- 逐 estimand/claim ceiling；
- 120 个 executable counterfactual；
- grader calibration、sealed held-out、promotion 与 frozen run manifest。

因此本轮完成的是可执行 authoring authority rebase。它允许团队按新的 WAJEgame 目录逐题
审核，尚不授予 production Evidence、Answer 或 settled publication 权限。
