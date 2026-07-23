# Full Payment Data Cleaning Review

Status: cleaning completed
Scope: 2024-01-01 through 2026-07-04 payment details

## Outputs

- Raw ClickHouse table: `paid_order_detail_raw_20240101_20260704`
- Clean ClickHouse table: `paid_order_success_clean_20240101_20260704_v2`
- Daily summary table: `paid_order_success_daily_20240101_20260704_v2`
- Latest-key helper table: `paid_order_success_latest_key_20240101_20260704_v2`
- First-payment authority table: `paid_order_success_first_payment_20240101_20260704_v2`
- Profile JSON: `artifacts/data-cleaning/payment_details_20240101_20260704_profile.json`

## Cleaning Rules

- Include `pay_success` rows for paid amount.
- Exclude `order_success` from paid amount.
- Deduplicate successful payment rows by `订单id`, keeping the latest `支付完成时间`.
- Within source rows marked as first payment, keep one canonical first-payment order per user using the earliest `(payment_completed_ms, order_id)` tuple.
- Use `支付完成时间` converted to `Africa/Lagos` as `business_date_lagos`.
- Interpret `注册时间` wall-clock text as `Asia/Shanghai`, then store the instant in `Africa/Lagos`.
- Interpret `首充时间` epoch milliseconds as UTC, then store the canonical first-payment instant in `Africa/Lagos`.
- Mark residual negative registration-to-first-payment lags as source anomalies; exclude them from lag conclusions.
- Preserve source dimension values; only empty string and `NULL` are converted to null.

## Summary

- Raw rows loaded: 75,984,922
- Latest successful order keys: 41,234,677
- Clean paid rows: 41,234,677
- Clean paid amount: 88,881,490,051.00 NGN
- Clean date range: 2024-01-01 through 2026-07-04
- Source first-payment rows: 2,432,324
- Canonical first-payment rows/users: 2,430,586 / 2,430,586
- Canonical first-payment duplicate users: 0
- Residual negative registration-to-first-payment rows: 67
- Duplicate success rows removed: 1
- Clean rows above latest-key count: 0
- Invalid success rows excluded: 0
- Raw `日期` mismatch on valid success rows: 25,719
- 2026 H1 row-count match with existing clean table: yes
- 2026 H1 amount match with existing clean table: yes

## Status Distribution

| Status | Rows |
|---|---:|
| pay_success | 41,234,678 |
| order_success | 34,750,244 |

## Currency Distribution

| Currency | Rows | Paid Amount NGN |
|---|---:|---:|
| NGN | 41,234,677 | 88,881,490,051.00 |

## Missing Clean Fields

| Field | Missing Rows |
|---|---:|
| registered_to_first_paid_lag_seconds | 38,809,597 |
| first_paid_at | 38,804,091 |
| network_type | 9,626,012 |
| device_brand | 2,347,968 |
| device_model | 2,347,968 |
| country | 227,952 |
| region | 227,952 |
| city | 227,952 |
| registered_at | 118,464 |
| payment_started_ms | 4,463 |
| payment_latency_seconds | 4,463 |
| channel | 651 |
| os | 147 |

## Top Payment Methods

| Payment Method | Rows | Paid Amount NGN |
|---|---:|---:|
| OPAY | 31,037,091 | 58,225,795,304.00 |
| PALMPAY_BANK | 3,578,026 | 13,663,002,127.00 |
| APP_PALMPAY | 3,479,139 | 7,787,079,081.00 |
| MERCHANT_OPAY | 2,734,400 | 7,763,774,818.00 |
| PALM | 377,970 | 1,357,029,039.00 |
| MONNIFY | 16,625 | 50,729,416.00 |
| PAYSTACK | 11,426 | 34,080,266.00 |

## Top Channels

| Channel | Rows | Paid Amount NGN |
|---|---:|---:|
| WajeSpecial | 29,115,675 | 60,224,724,824.00 |
| PAWAJEIOS | 4,403,610 | 12,361,073,487.00 |
| PAWAJEPALM2 | 2,132,816 | 3,430,780,162.00 |
| PAWAJEBETH5 | 1,798,790 | 4,012,718,380.00 |
| PAWAJEH5 | 1,168,026 | 2,865,862,561.00 |
| PAWAJEH5OP | 847,860 | 1,842,080,465.00 |
| PAPAWAJEH5GA | 312,673 | 1,103,325,994.00 |
| PAWAJEH5PWW | 283,663 | 524,505,314.00 |
| PACHAMPIONS | 259,643 | 651,476,834.00 |
| PAWAJEH5OP3 | 123,385 | 231,249,286.00 |
| PAWAJESPOPAY | 115,949 | 280,811,174.00 |
| PAWAJEH5OP2 | 109,869 | 198,982,471.00 |
| PAWAJEH5OP6 | 85,417 | 206,032,252.00 |
| PAWAJEH5PW | 84,227 | 228,097,238.00 |
| PAPAWJBETCY2 | 48,867 | 70,589,292.00 |
| PAWAJEH5FUN | 46,086 | 90,624,597.00 |
| PAWAJEH5PWA | 44,130 | 69,596,411.00 |
| PAWAJEPALMS | 39,850 | 97,332,146.00 |
| PAWAJEH5OP4 | 38,007 | 77,686,139.00 |
| PAWAJEXENDER | 33,379 | 45,146,758.00 |

## Notes

- This is a new dataset run and does not rewrite the accepted 2026 H1 snapshot.
- `IP` and `设备ID` are still absent from the real payment-detail files.
- This report is data-quality evidence for intake and runtime binding, not a business conclusion.
