# Phase 1 Factor Ledger Review

Review status: draft pending business owner and data/engineering owner review  
Contract version: `0.1`  
Primary SSOT: `contracts/ssot/付费金额影响因子分析.mm`

## Review Purpose

This artifact groups the 142-node SSOT by business factor group. Reviewers should approve business meaning, claim boundary, data contract state, grain support, fixed restricted-output boundary, source-connection access, and upgrade path at the group level, then use source refs in the YAML files for node-level trace.

## Marker Legend

| Marker | Meaning | Review owner |
|---|---|---|
| `pending_owner` | Business/data owner has not approved meaning, state, or feasibility. | business owner + data owner |
| `contract_gap` | A needed metric, dimension, event, source, or policy contract is missing. | data/engineering owner |
| `restricted_output_gap` | A fixed raw-detail or sparse-output rule needs review or enforcement. It applies equally to every customer. | data/engineering owner |
| `unsupported_grain` | Aggregate use may be allowed while requested fine grain is blocked. | business owner + data owner |
| `out_of_scope` | Explicitly outside Phase 1 contract foundation or current launch path. | product + data owner |

## Factor Group Review Table

| Factor group | Current state | Representative supported paths | Markers and review needs |
|---|---|---|---|
| 付费金额指标与订单明细源 | `contract_backed`, P0 | `data_quality_check`, `pattern_scan`, `answer_verify` can use the accepted 2026 H1 paid-order source and initial grain-aware materiality policy. | `pending_owner`, `contract_gap`: refund/reversal source. Current source binding, NGN, status boundary, dedup, Lagos business date, materiality, and dev Postgres contract mirror are accepted for the 2026-01-01 through 2026-06-30 snapshot. |
| 付费订单与支付链路指标 | `evidence_linked`, P0 | `formula_decompose` can use 大盘 daily formula fields and paid-order detail joint fields. | `pending_owner`, `contract_gap`: `付费日活` maps to 大盘 `日活历史付费人数`; paid-order detail handles joint analysis, order status, dedup, payment method, channel, geo/device, and amount bucket paths. |
| 新增、注册、首充与留存因素 | `evidence_linked`, P0 | `formula_decompose` and impact review can use 大盘 day and channel-day fields for DAU, new users, registration, first-pay, same-day new paid, paid users, and paid amount after owner review. | `pending_owner`, `contract_gap`: v2 rates use `新增` as denominator and should be derived from counts; lifetime first-pay needs full historical paid-order imports; paid-active retention still needs cohort contract; 大盘 cannot support joint grains. |
| 支付方式、支付渠道与通道稳定性 | `evidence_linked`, P0 | `segment_bridge` and `joint_attribution` can use current channel and payment-method values for mix/contribution analysis. | `pending_owner`, `contract_gap`: payment incident links, bank/server stability, campaign/exposure joins, and channel-history merges. |
| 充值档位、客单价与用户价值结构 | `missing_contract`, P0 | `formula_decompose` and `segment_bridge` after data-assisted NGN amount buckets and conversion rules exist. | `pending_owner`, `contract_gap`: amount bucket policy, conversion source, recharge strategy events. |
| 投放、渠道、素材、SEO、GEO 与拉新运营 | `missing_contract`, P0 | Aggregate channel and aggregate `投放成本` can be profiled; campaign/action attribution waits for contracts. | `pending_owner`, `contract_gap`: 投放预算、出价、campaign 消耗、素材 CTR/CVR、SEO/GEO 排名、用户推荐活动 are unavailable without new source information. |
| 地理、设备、系统、网络与环境因素 | `contract_backed`, P0 | Aggregate `segment_bridge` or `joint_attribution` can run for every customer at the supported city/device grains. | `pending_owner`, `restricted_output_gap`, `unsupported_grain`: raw IP/device ID outputs stay blocked; aggregate city/device claims require dimension contracts and fixed sparse-cell enforcement. |
| 时间窗口、发薪周期、节假日与日内分布 | `static_assumption`, P0 | `pattern_scan` and `event_evidence` can use the 25..30 payday dimension as candidate mechanism. | `pending_owner`, `contract_gap`: payday window wording, calendar/event contracts, hour-level timing source contract. |
| 产品、活动、服务器与运营事件 | `missing_contract`, P0 | `event_evidence` for business-object impact after event timing, affected scope, exposure/control exist. | `pending_owner`, `contract_gap`: 服务器稳定性、Grafana、支付事故、产品更新、首充礼包、充值活动 are unavailable without new source information. |
| 外部环境、竞品、政策、赛事、天气与黑天鹅候选 | `evidence_linked`, P1 | `event_evidence` and `outlier_scan` can use the imported external-event workbook and competitor ranking CSV for context or candidate mechanisms; confirmed cause wording is blocked. | `contract_gap`, `out_of_scope`: workbook covers sports, weather, power, network, media policy, macro/fx, payday, social stability, holidays; competitor ranking is imported; stronger official policy feeds remain separate gaps. |
| 玩法曝光、点击、付费率、频次与玩法 ARPU | `missing_contract`, P1 | Gameplay files cover activity and betting fields, but payment attribution paths still block. | `pending_owner`, `contract_gap`: icon exposure, icon click, gameplay paid rate, gameplay paid amount, gameplay paid frequency, gameplay single-payment amount, and payment-order-to-gameplay linkage are unavailable without new source information. |
| 玩法流水、人均下注、下注金额与返奖率 | `evidence_linked`, P1 | `segment_bridge` can review gameplay users, penetration, rounds, bet count, bet amount, average bet amount, system rake rate, and GGR. | `pending_owner`, `contract_gap`: `玩法盈利` is confirmed as GGR; payout rate is deferred for now; gameplay payment attribution still needs payment-order-to-gameplay linkage. |
| 支付状态、耗时、失败与数据质量 | `evidence_linked`, P0 | `data_quality_check`, `outlier_scan`, and `formula_decompose` can use accepted status and dedup rules for current paid-order snapshot. | `pending_owner`, `contract_gap`: latency-specific claims carry missingness limits; server/Grafana/payment-incident links remain unavailable without new source information. |
| 用户、IP、设备标识与去重支持字段 | `contract_backed`, P0 | Aggregate analysis, internal dedup, and data-quality checks can reference fields; visible raw identifier and individual-level claims are blocked by fixed output safety. | `restricted_output_gap`, `unsupported_grain`: raw user/IP/device output and individual claims stay blocked; customer-safe projection, audit, and sparse-cell enforcement still need data/engineering review. |

## Question Family Coverage

| Question family | Representative path status |
|---|---|
| `paid_amount_change_explanation` | Covered by metric source quality, formula decomposition, payment/channel segment bridge, growth-op attribution, and answer verification. |
| `pattern_explanation` | Covered by pattern scan, payday-dimension event evidence, payment-quality outlier review, hourly timing gap, and answer verification. |
| `business_object_impact_review` | Covered by product/operation event evidence, metric-driver formula decomposition, and answer verification. |
| `revenue_health_review` | Covered by formula decomposition, payment-quality anomaly review, and answer verification. |
| `segment_or_factor_attribution` | Covered by payment-method segment bridge, contract-backed aggregate geo/device joint attribution, fixed sparse-output safety, and answer verification. |
| `anomaly_or_black_swan_review` | Covered by external-context outlier/event evidence, raw external ingestion scope block, and answer verification. |
| `custom_baseline_comparison` | Covered by pattern scan, formula decomposition, and answer verification. |
| `data_quality_or_evidence_review` | Covered by contract coverage review, sensitive-output/dedup review, accepted materiality policy, and answer verification. |

## 2026-07-05 Source Coverage Update

New local raw copies were added under `data/raw/` for review profiling only:

- `market-dashboard-2024-01-01_2026-06-02`: 884 rows, 2024-01-01 through 2026-06-02. Covers daily 大盘 fields such as `日活`, `新增`, `注册人数`, `首充人数`, `新增付费人数`, `付费人数`, `付费金额`, `投放成本`, `利润`, and withdrawal aggregates.
- `market-dashboard-channel-2024-01-01_2026-06-02`: 13,418 rows across 183 CSV files, 104 non-empty files, 61 filename-derived channels across all files, and 49 non-empty filename-derived channels. Supports channel-day single-dimension dashboard review using the filename-prefix channel rule.
- `gameplay-overall-2024-01-01_2026-06-02`: 195,023 rows. Covers `玩法`, `区服`, `游戏人数`, `玩法渗透率`, `游戏局数`, `玩家下注次数`, `玩家下注总额`, `系统抽水率`, and `玩法盈利`.
- `gameplay-channel-2024-01-01_2026-06-02`: 740,100 rows across 183 CSV files, 61 filename-derived channels across all files, and 45 non-empty filename-derived channels. Supports gameplay channel-day review using the filename-prefix channel rule.
- `external-events/外部影响因素0608.xlsx`: 9 sheets and 324 normalized events in the old WAJE loader, covering 2024-01-01 through 2026-06-08.
- `competitor-ranking-2024-01-01_2026-06-07`: 889 rows with daily ranking fields for 1xBet, Bangbet, Bet9ja, MSport, SportyBet, bet365, iLOTBet, and Ludo Naira; bet365 has no populated values in the current file.

Formula component status after the user clarification:

- `付费日活` maps to 大盘 `日活历史付费人数`.
- Current paid-order detail can support `付费次数`, `付费频次`, `单笔付费金额`, `发起支付次数`, and `支付成功率` for the accepted 2026 H1 snapshot.
- 大盘 can support aggregate `日活`, `新增`, `注册`, `首充`, `新增首日付费`, `付费人数`, and `付费金额` for 2024-01-01 through 2026-06-02 after owner field confirmation.
- v2 `注册率/首次付费率/新增首日付费率` are 大盘 `注册率/首充率/新增付费率`.
- The reason to use paid-order detail is joint ability: order detail can support channel x region x device x amount bucket x payment method paths, status/dedup, and aggregate user flags/counts. 大盘 still remains useful for day and channel-day formula components.
- `付费人数/付费日活` uses 大盘 `付费人数 / 日活历史付费人数`.

Explicit unavailable items unless new source information is provided:

- 投放预算、出价、campaign 消耗、素材 CTR/CVR、SEO/GEO 排名、用户推荐活动.
- 服务器稳定性、Grafana、支付事故、产品更新、首充礼包、充值活动.
- refund/reversal/chargeback/cancellation adjustment source.
- 玩法 icon 曝光、玩法 icon 点击、玩法付费率、玩法付费金额、玩法付费频次、玩法单笔付费金额、支付订单到玩法的明细归因链路. 返奖率暂时不接。

## Open Review Items

- Business owner + data owner together: amount bucket policy is accepted after real NGN payment amount distribution profiling.
- Business owner + data owner together: materiality thresholds are accepted as the initial grain-aware policy; tune later only when needed.
- Data/engineering owner: refund/reversal adjustment source, original-currency validation, dashboard/gameplay source field meanings, event contracts, sensitive-output enforcement, sparse-cell rules, and supported-grain enforcement remain review items. Current-data cutoff/watermark, source binding, NGN reporting, Africa/Lagos source-time mapping, payment status/dedup, amount buckets, channel rules, and payment-method enum are confirmed for the accepted snapshot.
- Real data confirmation order: data owner checks executable source facts first, business owner and data owner review business-impacting thresholds/ambiguities second, then source contracts or backlog blockers are updated.
- Future product/data review: define AnySearch-like external evidence connector contract before any raw external ingestion enters runtime.

## Phase 1 Signoff Package

Phase 1 Contract Foundation has a reviewable source loop for CF-04 through CF-10:

- Factor master: `contracts/ledger/factor-ledger.yaml`
- Capability support matrix: `contracts/ledger/capability-support.yaml`
- Missing-contract backlog: `contracts/backlog/missing-contracts.yaml`
- Dimension sources: `contracts/dimensions/dimensions.yaml`
- Event sources: `contracts/events/events.yaml`
- Static assumption: `contracts/assumptions/payday.assumption.yaml`
- Capability cards: `contracts/capabilities/*.yaml`
- Validation command: `ruby tools/contracts/validate-contracts.rb`

Remaining signoff blockers are expected owner confirmations and missing executable contracts. Phase 2 graph compiler can consume these draft sources for validation, repair, degrade, and block rules, while runtime query execution waits for later semantic contracts.
