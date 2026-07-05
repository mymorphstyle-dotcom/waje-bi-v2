import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

const monthEvents = [
  {
    id: "intent",
    label: "识别问题意图",
    phase: "intent_router",
    summary:
      "用户在问跨月份重复出现的月内位置差异，归类为周期模式分析。",
    detail:
      "这类问题先判断 pattern 是否存在，再进入候选解释。当前 mock 会避免把问题当成单期变化或全量累计值判断。",
    durationMs: 390,
    evidence: [
      { label: "自然语言意图命中", tone: "strong" },
      { label: "粒度为月内 bucket", tone: "strong" },
    ],
  },
  {
    id: "pattern-scan",
    label: "计算周期模式",
    phase: "pattern_scan",
    summary:
      "按 1-7 号、8-20 号、21-月底分桶，月初高点在 25/29 个月成立。",
    detail:
      "同时做异常月份剔除和方向一致性复核。这里返回的是 mock 结果，真实实现会由 ClickHouse 聚合和本地数值校验生成。",
    durationMs: 940,
    evidence: [
      { label: "25/29 同向", tone: "strong" },
      { label: "异常复核通过", tone: "strong" },
    ],
  },
  {
    id: "formula",
    label: "拆解付费金额公式",
    phase: "formula_decompose",
    summary:
      "成功订单数是主要贡献项，支付成功率和单笔金额是补充放大项。",
    detail:
      "付费金额被拆成发起次数、支付成功率、单笔成功金额，再比较各项对月初抬升的贡献。",
    durationMs: 720,
    evidence: [
      { label: "订单数贡献 54%", tone: "strong" },
      { label: "单笔金额贡献 14%", tone: "medium" },
    ],
  },
  {
    id: "candidate-scan",
    label: "扫描候选解释",
    phase: "candidate_factor_scan",
    summary:
      "发薪窗口、新老用户结构、渠道结构、节假日和活动进入候选解释池。",
    detail:
      "payday_calendar 用 25-30 号窗口表达大盘事实；活动、礼包、投放等需要事件表补齐后才能提升措辞强度。",
    durationMs: 1080,
    evidence: [
      { label: "发薪窗口强相关", tone: "strong" },
      { label: "活动事件表缺口", tone: "gap" },
    ],
  },
  {
    id: "joint",
    label: "组合归因与升维",
    phase: "joint_attribution",
    summary:
      "pay_window × user_type × channel 的组合解释力高于任意单因子。",
    detail:
      "本地能力先批量试算候选组合，LLM 负责判断是否继续升维和解释是否符合业务现实。",
    durationMs: 1390,
    evidence: [
      { label: "三维组合保留", tone: "strong" },
      { label: "残差 4%", tone: "medium" },
    ],
  },
  {
    id: "verify",
    label: "校验答案边界",
    phase: "answer_verifier",
    summary:
      "主结论可写强，外部事件只作为异常月份解释，缺口路径降级表达。",
    detail:
      "verifier 检查答案里显式写出的数字和 claim 类型，避免 LLM 把候选解释写成已证明主因。",
    durationMs: 660,
    evidence: [
      { label: "数字已检查", tone: "strong" },
      { label: "claim 已降级", tone: "medium" },
    ],
  },
];

const monthAnswer = {
  headline: "月初付费金额更高的模式成立，主因更像是订单数被周期性放大。",
  period: "全量样本 · 2024-01 至 2026-05 · 29 个月",
  verdict:
    "按月份内部比较后，月初高点在大多数月份重复出现。公式拆解显示，成功订单数贡献最大，支付成功率和单笔金额只解释部分差异。",
  metrics: [
    { label: "月初金额抬升", value: "+18.9%", note: "相对月中/月末均值" },
    { label: "方向一致月份", value: "25/29", note: "剔除异常后仍成立" },
    { label: "订单数贡献", value: "54%", note: "公式拆解最大项" },
    { label: "证据强度", value: "中高", note: "外部事件表仍有缺口" },
  ],
  pattern: [
    { month: "24-01", start: 112, rest: 96, lift: 16 },
    { month: "24-04", start: 119, rest: 101, lift: 18 },
    { month: "24-07", start: 116, rest: 99, lift: 17 },
    { month: "24-10", start: 124, rest: 105, lift: 19 },
    { month: "25-01", start: 128, rest: 107, lift: 21 },
    { month: "25-04", start: 121, rest: 103, lift: 18 },
    { month: "25-07", start: 126, rest: 108, lift: 18 },
    { month: "25-10", start: 132, rest: 111, lift: 21 },
    { month: "26-01", start: 135, rest: 112, lift: 23 },
    { month: "26-05", start: 129, rest: 110, lift: 19 },
  ],
  decomposition: [
    { name: "成功订单数", value: 54 },
    { name: "支付成功率", value: 19 },
    { name: "单笔金额", value: 14 },
    { name: "渠道结构", value: 9 },
    { name: "未解释残差", value: 4 },
  ],
  factors: [
    {
      name: "发薪窗口",
      finding: "25-30 号收入进入可支配余额，月初消费高峰更稳定。",
      strength: "强",
      limit: "大盘 window 可用；企业级发薪日不作为首版必需条件。",
    },
    {
      name: "新老用户结构",
      finding: "月初首充和回流用户占比同步抬升，放大成功订单数。",
      strength: "中高",
      limit: "注册、登录、活跃事件越完整，结论越稳。",
    },
    {
      name: "支付成功率",
      finding: "月初成功率略高，解释一部分金额差异。",
      strength: "中",
      limit: "支付失败原因明细会影响措辞强度。",
    },
    {
      name: "节假日/活动",
      finding: "春节和长假附近有异常峰谷，适合解释异常月份。",
      strength: "中",
      limit: "活动投放和礼包配置需要事件表补齐。",
    },
  ],
  verifier: {
    claim: "模式成立；主解释是订单数周期性放大，外部事件只解释异常月份。",
    status: "通过，缺口已降级表达",
    checkedNumbers: ["+18.9%", "25/29", "54%", "19%", "14%"],
  },
};

const weekEvents = [
  {
    id: "intent",
    label: "识别问题意图",
    phase: "intent_router",
    summary:
      "用户在问周一到周日的稳定差异，归类为周内周期模式分析。",
    detail:
      "这类问题先把日期映射为 day_of_week，再判断高低点是否跨周稳定存在。候选解释会优先看订单数、活跃结构、活动/push 和支付链路。",
    durationMs: 360,
    evidence: [
      { label: "周内 bucket", tone: "strong" },
      { label: "周期意图命中", tone: "strong" },
    ],
  },
  {
    id: "pattern-scan",
    label: "计算周内周期模式",
    phase: "pattern_scan",
    summary:
      "周五、周六付费金额高于周均，周一到周三偏低，方向在 19/22 个完整周成立。",
    detail:
      "先按自然周聚合，再排除重大节假日和活动周做复核。周末高点仍然存在，但强度低于月初模式。",
    durationMs: 880,
    evidence: [
      { label: "19/22 同向", tone: "strong" },
      { label: "节假日复核通过", tone: "medium" },
    ],
  },
  {
    id: "formula",
    label: "拆解付费金额公式",
    phase: "formula_decompose",
    summary:
      "周五、周六的抬升主要由成功订单数贡献，单笔金额变化不明显。",
    detail:
      "付费金额拆解后，成功订单数解释 49%，活跃用户结构解释 21%，支付成功率解释 12%，其余来自渠道和残差。",
    durationMs: 700,
    evidence: [
      { label: "订单数贡献 49%", tone: "strong" },
      { label: "成功率贡献 12%", tone: "medium" },
    ],
  },
  {
    id: "candidate-scan",
    label: "扫描候选解释",
    phase: "candidate_factor_scan",
    summary:
      "周末活跃、活动/push、渠道曝光、新老用户结构进入候选解释池。",
    detail:
      "周内差异通常更容易被产品动作和活跃时段影响，所以活动表、push 表、推荐位曝光表会决定最终措辞强度。",
    durationMs: 1060,
    evidence: [
      { label: "活跃结构强相关", tone: "strong" },
      { label: "push 事件表缺口", tone: "gap" },
    ],
  },
  {
    id: "joint",
    label: "组合归因与升维",
    phase: "joint_attribution",
    summary:
      "day_of_week × user_type × channel 的组合解释力最高，活动/push 是异常周解释。",
    detail:
      "本地先批量试算候选组合，再让 LLM 判断业务解释是否合理。这里保留三维组合，并把外部动作作为证据缺口路径。",
    durationMs: 1320,
    evidence: [
      { label: "三维组合保留", tone: "strong" },
      { label: "外部动作待补", tone: "gap" },
    ],
  },
  {
    id: "verify",
    label: "校验答案边界",
    phase: "answer_verifier",
    summary:
      "可以写周末高点和订单数主导，不能把活动/push 写成已证明主因。",
    detail:
      "verifier 检查 +12.4%、19/22、49%、21%、12% 等数字，并把缺少事件合同的解释降级为候选。",
    durationMs: 620,
    evidence: [
      { label: "数字已检查", tone: "strong" },
      { label: "候选解释降级", tone: "medium" },
    ],
  },
];

const weekAnswer = {
  headline: "周五和周六的付费金额更高，主因更像是活跃用户带来的订单数抬升。",
  period: "全量样本 · 最近 22 个完整自然周 · 周内 day-of-week bucket",
  primaryChartTitle: "周五/周六 vs 周均",
  verdict:
    "按自然周内部比较后，周五、周六高点更稳定，周一到周三偏低。公式拆解显示，成功订单数是最大贡献项，活跃用户结构和支付成功率提供补充解释。",
  metrics: [
    { label: "高点抬升", value: "+12.4%", note: "周五/周六相对周均" },
    { label: "方向一致周", value: "19/22", note: "剔除节假日周后仍成立" },
    { label: "订单数贡献", value: "49%", note: "公式拆解最大项" },
    { label: "证据强度", value: "中高", note: "push/活动事件仍有缺口" },
  ],
  pattern: [
    { month: "周一", start: 92, rest: 100, lift: -8 },
    { month: "周二", start: 95, rest: 100, lift: -5 },
    { month: "周三", start: 97, rest: 100, lift: -3 },
    { month: "周四", start: 101, rest: 100, lift: 1 },
    { month: "周五", start: 112, rest: 100, lift: 12 },
    { month: "周六", start: 116, rest: 100, lift: 16 },
    { month: "周日", start: 106, rest: 100, lift: 6 },
  ],
  decomposition: [
    { name: "成功订单数", value: 49 },
    { name: "活跃用户结构", value: 21 },
    { name: "支付成功率", value: 12 },
    { name: "渠道曝光", value: 10 },
    { name: "未解释残差", value: 8 },
  ],
  factors: [
    {
      name: "周末活跃",
      finding: "周五晚到周六的活跃用户和回流用户更多，放大成功订单数。",
      strength: "强",
      limit: "需要登录/活跃事件持续完整。",
    },
    {
      name: "活动/push",
      finding: "部分周末高点与活动和 push 节奏同向。",
      strength: "中",
      limit: "缺少完整事件表时只能作为候选解释。",
    },
    {
      name: "渠道曝光",
      finding: "周末部分渠道曝光占比提升，带来订单数增量。",
      strength: "中",
      limit: "需要 icon 曝光、点击、下单链路补齐。",
    },
    {
      name: "支付链路",
      finding: "支付成功率周末略高，但解释力低于订单数。",
      strength: "中",
      limit: "失败码和银行侧原因会影响判断。",
    },
  ],
  verifier: {
    claim: "周五/周六高点成立；主解释是订单数抬升，活动/push 作为候选解释。",
    status: "通过，事件缺口已降级",
    checkedNumbers: ["+12.4%", "19/22", "49%", "21%", "12%"],
  },
};

export async function POST(req: Request) {
  const body = (await req.json().catch(() => ({}))) as { question?: string };
  const question = body.question ?? "";
  const isWeekPattern = /周|星期|week/i.test(question);

  return NextResponse.json({
    question,
    events: isWeekPattern ? weekEvents : monthEvents,
    answer: isWeekPattern ? weekAnswer : monthAnswer,
  });
}
