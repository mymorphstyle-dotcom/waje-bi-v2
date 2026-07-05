# 付费订单明细模板 Extract

Source file: `contracts/source-templates/付费订单明细模板.xlsx`  
Original source path: `/Users/luka/Downloads/付费订单明细模板.xlsx`  
Source SHA-256: `efc5ccb8a79d8650b924b095f46728aa5a56ff61a6baa11401628c8556e444dd`  
Intake date: `2026-07-03`  
Source owner: `pending business/data owner confirmation`  
Extraction method: local workbook metadata and header scan
Real data expected: `2026-07-05`  
Runtime source status: candidate template only until source contract review

## Workbook Shape

- Sheet: `付费订单明细`
- Worksheet reported size: 198 rows x 25 columns
- Non-empty template content found: header row plus one sample data row

## Headers

1. 日期
2. 支付发起时间
3. 支付完成时间
4. 订单ID
5. 用户ID
6. 是否新用户
7. 是否首充
8. 支付状态
9. 支付发起金额
10. 支付成功金额
11. 币种
12. 一级渠道
13. 支付方式
14. 国家
15. 州/地区
16. 城市
17. IP
18. 设备ID
19. 设备品牌
20. 设备型号
21. 操作系统
22. 网络类型
23. 注册时间
24. 首充时间
25. 支付耗时秒

## Intended Use

### Phase 1: Contract Review Input

Use this workbook as a source template candidate for the `paid_amount` metric contract and payment-related factor review. It can help reviewers define metric meaning, time semantics, amount basis, status taxonomy, currency policy, dedup key, user flags, channel/payment dimensions, geo/device dimensions, and payment latency evidence.

It should not become a final table contract until owners confirm definitions, refresh source or watermark, permissions, timezone, refund/reversal policy, and data quality rules.

### Real Data Review

When the real dataset arrives, review actual fields, sample values, source watermark, permissions, status enum, order ID uniqueness, currency basis, refund/reversal behavior, and sensitive identifier treatment before accepting any runtime source contract.

First pass should run minimal profiling only: field alignment, row count, time range, current-data watermark, payment status distribution, order ID uniqueness, amount and currency distribution, refund or status-backfill signals, and masked sensitive-field statistics. Do not publish formal business conclusions from this intake pass.

### Phase 3: Semantic Query Candidate

After semantic contracts exist, this template can inform a paid order detail source contract for:

- daily paid amount from final successful orders using `支付完成时间`, `订单ID`, and `支付成功金额`
- initiated payment amount from `支付发起时间` and `支付发起金额`
- successful order count after one final success record per `订单ID`, plus initiated order count
- payment success rate from `支付状态`
- average paid amount per successful order
- payment latency from `支付耗时秒`
- channel, payment method, geo, device, OS, network segment bridges
- new-user and first-payment flags

### Phase 4: First Pattern Vertical Slice

For intra-month payment pattern analysis, this source becomes useful when the system needs order-level proof that month-phase comparisons use non-cumulative successful paid amount. It can also support exception scans by payment method, channel, geo, device, new-user flag, first-payment flag, and payment latency.

## Review Gaps

- Confirm whether `日期` is business date, partition date, or derived from payment completion.
- Confirm timezone and day-boundary rule for `支付发起时间`, `支付完成时间`, `注册时间`, and `首充时间`.
- Enforce accepted paid_amount status rule: only final successful orders count toward paid amount.
- Confirm raw `支付状态` allowed values that mean final success, failure, pending/processing, and retry-only.
- Confirm whether `支付成功金额` includes refunds, chargebacks, reversals, or post-payment adjustments.
- Confirm currency conversion and display currency policy.
- Confirm whether `订单ID` is globally unique and sufficient for one-final-success deduplication.
- Confirm current-data cutoff or source watermark for each run.
- Confirm whether status backfills are visible in the current source snapshot at run time.
- Keep this template as review input until real data is checked and a source contract is accepted.
- Later data updates should create a new run or artifact version instead of rewriting historical answers.
- Enforce accepted permission treatment for `用户ID`, `IP`, and `设备ID`: aggregate analysis, internal quality checks, and dedup may use these fields; raw identifiers must not appear in answers or visualizations.
