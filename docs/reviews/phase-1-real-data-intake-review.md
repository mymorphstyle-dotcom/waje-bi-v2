# Phase 1 Real Data Intake Review

Status: intake profiling completed  
Expected data date: 2026-07-05  
Scope: minimal profiling only; no formal business conclusions.

Data file: `data/raw/2026-01-01_2026-06-30.csv`  
File size: 8,337,257,507 bytes  
Rows profiled: 38,941,583  
Columns profiled: 23

## Checks

- [x] Field alignment with `付费订单明细模板.xlsx`.
- [x] Row count and non-empty record check.
- [x] Time range and current-data cutoff or watermark.
- [x] Payment status distribution.
- [x] `订单ID` uniqueness and duplicate profile.
- [x] `支付成功金额` and `币种` distribution.
- [x] Refund, reversal, cancellation, or status-backfill signals.
- [x] Sensitive fields profiled only as masked or aggregate statistics.

## Profiling Findings

- CSV is parseable as UTF-8, with 0 malformed rows.
- Actual data has 23 columns. Template columns `IP` and `设备ID` are absent.
- Business owner confirmed raw `IP` and `设备ID` are not needed for the current operations-analysis scope.
- Actual data uses `订单id`, `用户id`, and `分包渠道`; template expects `订单ID`, `用户ID`, and `一级渠道`.
- Data owner confirmed this file was exported on 2026-07-04 and represents the complete January through June 2026 dataset.
- `日期` range is 2026-01-01 through 2026-06-30.
- `支付完成时间` is a 13-digit epoch-millisecond value when present; converted to `Africa/Lagos`, its date range is also 2026-01-01 through 2026-06-30.
- `日期` and `支付完成时间` converted to `Africa/Lagos` business day differ on 11,867 `pay_success` rows; business owner confirmed the analysis date should use `支付完成时间` converted to `Africa/Lagos`.
- `币种` is 100% `NGN`.
- `支付状态` has two values: `pay_success` 23,858,861 rows and `order_success` 15,082,722 rows.
- Business owner confirmed `pay_success` is the paid_amount success status; `order_success` is a prior/non-paid status and should be excluded from paid_amount.
- `pay_success` rows carry `支付成功金额`; `order_success` rows have empty `支付成功金额` and empty `支付完成时间`.
- `order_success` rows all have `支付发起时间`; all have empty `支付耗时秒`, empty `支付完成时间`, and empty `支付成功金额`.
- 4,980 unique `订单id` values appear in both `order_success` and `pay_success`; the `order_success` records should be ignored when the same `订单id` has `pay_success`.
- `支付成功金额` has 23,858,861 numeric values, min 100.0, max 3,000,000.0, no zero, no negative, and total 51,172,026,308.0 NGN for intake profiling.
- `订单id` and `用户id` are non-empty on all rows.
- Exact `订单id` duplicate profile: all rows have 5,006 duplicate extra rows; `pay_success` rows have 14 duplicate extra rows across 14 duplicate keys.
- The 14 duplicate `pay_success` groups are not full-row duplicates. They share `支付状态`, `支付发起金额`, `支付成功金额`, `币种`, `用户id`, and `支付方式`, while `支付发起时间`, `支付完成时间`, and `支付耗时秒` differ in all 14 groups. Business owner confirmed dedup cleaning should keep the latest `支付完成时间` record.
- Applying the confirmed paid_amount cleaning boundary gives 23,858,847 paid records and 51,172,015,308.0 NGN for intake profiling.
- The confirmed clean table is unique by `订单id`: 23,858,847 clean rows and 23,858,847 unique orders.
- Clean-table currency is 100% `NGN`; this supports the current report-currency decision.
- Clean-table field coverage after confirmed paid_amount rules: `支付发起时间` missing 5,247 rows, `支付耗时秒` missing 5,247 rows, `分包渠道` missing 2,913 rows, `国家`/`州/地区`/`城市` missing 5,112 rows each, `设备品牌`/`设备型号` missing 1,737,237 rows each, `操作系统` missing 2,453 rows, `网络类型` missing 5,895,702 rows, `注册时间` missing 72,786 rows, and `首充时间` missing 22,782,055 rows.
- `支付发起时间` is empty on 5,254 rows; these are `pay_success` rows.
- `支付耗时秒` is empty on 15,087,976 rows: all `order_success` rows plus 5,254 `pay_success` rows.
- `分包渠道` is empty on 10,837 rows.
- `国家`, `州/地区`, and `城市` are empty on 26,515 rows each.
- `设备品牌` and `设备型号` are empty on 3,824,242 rows each.
- `网络类型` is empty on 12,309,371 rows.
- `注册时间` is empty on 123,820 rows.
- `首充时间` is empty on 37,864,790 rows.

## Data Owner Review Needed

- Source watermark, extraction cutoff, and current source contract acceptance are confirmed.
- Missing `IP` and `设备ID` should not block current operations analysis; region, device brand/model, OS, and network-type aggregate analysis remain in scope.
- Source owner confirmed `分包渠道` is the channel dimension.
- Business owner and data owner confirmed `支付发起时间` and `支付耗时秒` missingness does not affect paid_amount operations analysis; latency-specific analysis must still carry coverage limits.

## Confirmed Policy Updates

- Amount bucket policy is accepted for the current NGN paid_amount source.
- Source snapshot is accepted as the 2026-07-04 export of the complete January through June 2026 dataset.
- Data owner confirmed no late-arriving records or status backfill for this snapshot.
- Later data updates should create a new dataset run or artifact version and should not rewrite prior answer artifacts.
- `分包渠道` maps to the semantic `channel` dimension for current-data analysis.
- Payment latency fields are not required for paid_amount total, trend, channel, time-window, or amount-bucket analysis.
- Payday, month-start, month-end, holiday, hourly, weekly, quarterly, and arbitrary-window analyses are candidate pattern families. No compiler rule should be hard-coded to a single hypothesized pattern.

## Amount And Materiality Recommendations

Status: amount buckets confirmed; materiality accepted as the initial grain-aware policy.

Key amount facts from `paid_order_success_clean`:

- Paid records: 23,858,847.
- Paid amount: 51,172,015,308 NGN.
- Average order amount: 2,144.78 NGN.
- Approximate quantiles: P25 500, P50 1,000, P75 2,000, P90 5,000, P95 5,000, P99 20,000, P99.9 100,000.
- Distinct paid amounts: 5,936.
- Top 5 paid amounts cover 91.95% of orders and 72.90% of paid amount.
- Top 20 paid amounts cover 97.56% of orders and 87.44% of paid amount.

Confirmed amount buckets:

| Bucket | Order share | Amount share | Recommendation |
|---|---:|---:|---|
| `<=500` | 32.44% | 7.53% | Micro/base entry bucket. |
| `501-1000` | 34.59% | 16.01% | Main base bucket. |
| `1001-2000` | 19.62% | 17.75% | Small paid bucket. |
| `2001-5000` | 10.02% | 21.78% | Mid bucket; high amount share. |
| `5001-10000` | 1.17% | 4.72% | Upper-mid bucket. |
| `10001-20000` | 1.81% | 16.56% | High-value bucket; keep separate because 20,000 is a major package. |
| `20001-50000` | 0.14% | 2.45% | VIP bucket. |
| `50001-100000` | 0.17% | 7.91% | Large VIP bucket; keep 100,000 visible. |
| `>100000` | 0.04% | 5.29% | Whale/extreme bucket. |

Grain-aware materiality facts:

- The first draft threshold was based only on 181 daily Lagos business dates and 180 day-over-day movements.
- A single numeric threshold should not be reused across hourly, daily, weekly, monthly, quarterly, or custom-N-day questions.
- Materiality should be computed from the query grain and comparison mode. Strong wording should require both absolute and percentage movement, enough comparable windows, and a business-readable driver.

Accepted grain-aware materiality policy:

| Query grain | Current comparable evidence | Reportable movement | Material driver | Strong anomaly |
|---|---:|---:|---:|---:|
| Hourly, same Lagos hour across days | 4,344 hour-day points; same-hour P75/P90/P95 deviation 1.87M/2.71M/3.25M NGN and 16.34%/23.04%/27.09% | >=2M or >=15% | >=3M or >=23% | >=4M or >=30% |
| Daily | 180 day-over-day movements; P75/P90/P95 12.97M/18.92M/23.34M NGN and 4.53%/6.96%/8.55% | >=10M or >=3% | >=20M or >=7% | >=30M or >=10% |
| 3-day rolling window | 176 prior-window comparisons; P75/P90/P95 45.49M/63.80M/71.67M NGN and 5.08%/7.45%/8.20% | >=45M or >=5% | >=65M or >=7.5% | >=80M or >=9% |
| 7-day rolling window / full week | 168 rolling comparisons; P75/P90/P95 101.12M/152.59M/168.43M NGN and 4.93%/7.68%/8.88% | >=100M or >=5% | >=150M or >=8% | >=180M or >=10% |
| 14-day rolling window | 154 prior-window comparisons; P75/P90/P95 226.00M/339.98M/364.99M NGN and 6.03%/9.41%/10.20% | >=225M or >=6% | >=340M or >=9.5% | >=400M or >=11% |
| 30-day rolling window / monthly proxy | 122 prior-window comparisons; P75/P90/P95 752.99M/1.06B/1.08B NGN and 9.44%/14.34%/15.33% | >=750M or >=9% | >=1.05B or >=14% | >=1.20B or >=16% |
| Calendar month | 6 months and 5 month-over-month movements; low sample count | Use 30-day proxy and mark low confidence | Use 30-day proxy and mark low confidence | Avoid strong anomaly wording until more months exist |
| Quarter | Q1 and Q2 only | Descriptive comparison only | Descriptive comparison only | No anomaly threshold from current data |

For arbitrary `N`-day questions:

- If enough windows exist, compare rolling `N`-day amount to the previous rolling `N`-day amount and use P75/P90/P95 movement as reportable/material/strong candidates.
- If comparable windows are sparse, degrade to descriptive wording and expose the sample-size limit.
- For hourly questions, compare the same Lagos hour across days before using adjacent-hour movement, because intraday traffic has a visible natural pattern.

Pattern example sanity check:

- Month start days 1-10 average daily amount: 285.39M NGN.
- Month mid days 11-20 average daily amount: 279.42M NGN.
- Month end days 21-end average daily amount: 283.33M NGN.
- This is only an example sanity check from the current dataset. It should not define compiler behavior, capability design, or default answer wording.

## Confirmation Order

1. Data owner confirms fields, source watermark, payment status enum, `订单ID` uniqueness, and permission enforcement.
2. Business owner and data owner use the accepted initial materiality thresholds by query grain and version future tuning when needed.
3. Prepare a draft source contract for owner review, or record concrete backlog blockers.

## Review Outcome

- [x] Draft source contract prepared for owner review.
- [x] Dev ClickHouse raw and clean tables loaded.
- [x] Channel field mapping confirmed.
- [x] Amount bucket policy confirmed.
- [x] Latency missingness confirmed as non-blocking for paid_amount operations analysis.
- [x] Source watermark and extraction cutoff confirmed.
- [x] Status backfill and late-arrival risk confirmed as none for this snapshot.
- [x] Source contract accepted.
- [ ] Source contract remains blocked with concrete backlog items.

## Joint Business Review

- [x] Amount bucket policy reviewed by business owner and data owner together.
- [x] Materiality thresholds reviewed by business owner and data owner together.
- [x] Business-meaning ambiguities for channel and latency reviewed by business owner and data owner together.

## Promotion Outcome

The current 2026-01-01 through 2026-06-30 source snapshot is accepted as `contract_backed` for Phase 1 review and Phase 2 compiler prep. Dev Postgres contract mirror is initialized; future snapshots must bind to versioned source contracts.

## Notes

This intake pass should not be used to publish business conclusions.
