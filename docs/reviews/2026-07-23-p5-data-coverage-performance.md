# P5 数据覆盖与性能验收

日期：2026-07-23

状态：实现、真实数据验收、组合回归和 release manifest 终检均已完成。

## 1. 当前数据权威

当前阶段接受的完整数据边界为 Lagos 业务日 2026-06-02。

| 业务事实 | 控制来源 | 当前用途 |
| --- | --- | --- |
| 付费金额、付费人数、付费订单 | `paid_order_success` | 业务结论与公式拆解 |
| 付费后 24h/7d 下注、24h 是否游戏 | `payment_order_bet_link` | 已逐单对账的观察性行为证据 |
| 新增、注册、首充、营销成本、利润 | `market_dashboard` | 日级漏斗和市场健康背景 |
| Dashboard 付费金额、付费人数 | `market_dashboard` | 来源对账；发生不一致时只作背景 |

## 2. 支付订单—下注关联源验收

源文件：`/Users/luka/Downloads/支付订单关联下注金额.csv`

- 299,530 行、299,530 个唯一订单、58,494 个唯一用户；
- 北京时间源窗口转换为 Lagos 业务日 2026-06-01 至 2026-06-02；
- 充值金额 646,563,590 NGN；
- 24h 下注金额 4,165,547,141,440 NGN；7d 下注金额 15,015,128,192,321 NGN；
- 重复订单、负金额、24h 大于 7d、是否游戏标记冲突均为 0；
- 源比率列只作舍入审计，正式比率由金额重算；玩法占比只作结构背景；
- 与 `paid_order_success` 按订单、用户、Lagos 业务日和充值金额逐条匹配，299,530/299,530 通过。

已发布权威：

- ClickHouse 表：`payment_order_bet_link__96dda1b77c8ea55d`；
- snapshot：`dataset-snapshot:sha256:6079ac0d36bb79a7dc178841eb3c86e58d61f2c030a5047b7862d05553bcf0a0`；
- release：`dataset-release:sha256:82bfae9efb981f4735ebdaf793c14d8d67ce52e27cb4c78bc3d0db10dab31c12`；
- reconciliation：`matched`，`evidence_state=claim_ready`。

正式查询 smoke：

| Lagos 日 | 订单 | 用户 | 充值金额 | 24h 游戏率 | 24h 下注 | 7d 下注 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-06-01 | 145,900 | 37,764 | 308,240,309 | 96.7443% | 1,997,267,177,151 | 7,730,785,681,387 |
| 2026-06-02 | 153,630 | 39,156 | 338,323,281 | 96.6686% | 2,168,279,964,289 | 7,284,342,510,934 |

合同编译器完成两条真实链：窗口级 24h 下注比较返回 2 行且无合同缺口；充值档位交叉查询返回
18 个窗口—档位聚合行。结果只包含聚合维度和指标，没有订单 ID、用户 ID 或玩法明细。

## 3. Market Dashboard 对账结论

| Lagos 日 | Dashboard 付费金额 | 付费订单权威金额 | Dashboard 付费人数 | 权威付费人数 | 结论 |
| --- | ---: | ---: | ---: | ---: | --- |
| 2026-06-01 | 308,240,309 | 308,240,309 | 37,763 | 37,764 | 金额匹配；人数差 1 |
| 2026-06-02 | 185,469,962 | 338,323,281 | 23,976 | 39,156 | 金额差 -152,853,319；人数差 -15,180 |

6 月 2 日 Dashboard 金额最接近付费订单在 Lagos 约 14:26 的日内累计值。缺少源提取元数据，
该截点只能记录为诊断证据。运行时已把 Dashboard `paid_amount` 限制为来源对账用途，市场健康比较
继续使用日活、新增、注册、成本和利润；付费结论由付费订单控制。

## 4. 性能基线和实现

真实 P4 完整调查总时延为 741–935 秒。主要节点基线：

- claim authority：208–324 秒；
- narrative：258–383 秒；
- capability materialization：97–122 秒；
- plan：约 70 秒；intent：约 20–26 秒。

P5 增加 `analysis-performance-profile.v1`：按 intent、plan、evidence、coverage、claim authority、
narrative、delivery 记录耗时和预算状态，并记录合同编译、查询合同校验、SQL、结果校验、query-set
校验和 capability binding 子阶段的耗时及输入字节数。profile 只写 WAJE audit/Workbench。

预算采用 audit-only：完整因素调查 p50 300 秒、p95 480 秒；超限动作固定为
`record_and_continue`。预算不能终止节点、减少因素覆盖、跳过 verifier 或改变客户 run status。

block verifier 输入已改为实际段落引用的 claim、evidence、recommendation、limitation、publication
requirement 和 boundary facet 闭包。writer 与 Workbench 仍保存完整材料，claim verifier、推荐验证、
本地硬校验和发布权威保持不变。

## 5. 尚缺数据

- 支付尝试/失败分析仍需要支付发起事件、终态、通道、失败码、发起与完成时间、订单关联键和 Lagos
  业务时间；
- 内部运营和投放归因仍需要 owner、时间窗、作用范围、活动/版本/渠道标识和可复核来源；
- 用户级注册—首充漏斗按现有决议暂缓，当前继续使用 Dashboard 日级漏斗背景。
