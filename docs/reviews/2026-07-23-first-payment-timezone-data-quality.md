# First-payment and timezone data-quality closeout

Status: accepted and published on 2026-07-23

## Authority decisions

- Successful payment orders remain unique by `order_id`, keeping the latest successful completion.
- First-payment candidates come only from successful rows whose source first-payment flag is `1`.
- Each user receives one canonical first-payment order: the earliest candidate ordered by `(payment_completed_ms, order_id)`.
- The source flag is preserved as `is_first_payment_source`; runtime analysis uses canonical `is_first_payment`.
- `registered_at` source wall-clock text is interpreted in `Asia/Shanghai` and projected to `Africa/Lagos`.
- Payment and first-payment epoch milliseconds are interpreted as UTC instants and projected to `Africa/Lagos`.
- Residual negative registration-to-first-payment lags are retained as `negative_source_anomaly` and excluded from lag conclusions.

The dedup rule deliberately does not infer lifetime first payment from the earliest successful order in the archive. The archive starts on 2024-01-01, so such an inference would misclassify users whose true first payment predates the intake window.

## Verified invariants

| Check | Result |
|---|---:|
| Successful payment rows | 41,234,677 |
| Successful payment amount | 88,881,490,051 NGN |
| Source first-payment rows | 2,432,324 |
| Canonical first-payment users/rows | 2,430,586 |
| Source users with repeated first-payment flags | 1,719 |
| Duplicate source flags removed | 1,738 |
| Canonical users with repeated first-payment flags | 0 |
| Valid registration-to-first-payment lags | 2,425,013 |
| Missing registered time | 5,506 |
| Residual negative source anomalies | 67 |
| Median valid lag | 2,139 seconds |

Row count, paid amount, date range, order uniqueness, archive checksum, schema fingerprint, content fingerprint, first-payment uniqueness, and lag-quality distribution all passed the release inspector.

## Published dataset authority

- Physical table: `waje_bi.paid_order_success_clean_20240101_20260704_v2`
- Runtime binding: `contracts/runtime/clickhouse-analysis-bindings.yaml@19`
- Source contract: `contracts/sources/paid-order-detail.source.yaml@0.4`
- Dataset release: `dataset-release:sha256:22c8cb2b770a0b03d32db806ae9900cb1b922c8545c7719b14fa1ea17a74705a`
- Previous physical release remains persisted for historical run replay and is marked superseded.

## Current-stage coverage

Market Dashboard is accepted as complete through 2026-06-02 for the current analysis stage. Dashboard funnel and market-health evidence may run for explicit windows ending on or before that date. External-event data covers through 2026-06-08 and supports candidate event context for the same current-stage window. Later requested dates continue to produce an explicit availability gap.

## Deferred boundary

User-level registration funnel analysis remains deferred. The normalized canonical timestamps and quality flags make a future implementation possible without reopening these data-quality decisions.
