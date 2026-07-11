# Task 2 Report: Register Existing Paid-Success Facts

## Status

Completed. The existing paid-success ClickHouse fact table is registered as one
immutable canonical release. The release contains only `paid_order_success` and
does not create or imply `payment_attempt` coverage.

## Plan correction

The original Task 2 plan called `publish_dataset_snapshot_release()` for
`paid_order_success` without declaring canonical release membership. The shared
validator therefore had no legal dataset set for this release. The plan was
corrected before implementation to require:

- `requires_release: true`
- `release_membership: {policy: exact_dataset_set, dataset_ids: [paid_order_success]}`

This follows the existing canonical release schema and keeps payment attempts
outside this authority record.

## RED

Initial command:

```bash
/tmp/waje-bi-v2-py312/bin/python3 -m pytest \
  tests/phase4/test_paid_success_snapshot_registration.py -q
```

Observed expected RED: collection failed with
`ModuleNotFoundError: tools.data.register_existing_paid_success_snapshot`.

Additional bug REDs found during real validation:

- Nullable fingerprint test failed because the generated aggregate SQL hashed
  `Nullable(String)` directly. ClickHouse 24.8 returned code 48,
  `Method getDataAt is not supported for Nullable(String) ... NULL`.
- Complete payload test failed with `KeyError: reconciliation_ref`. The missing
  JSON key became NULL in the Postgres write-after-read validation comparison,
  while the mirrored column contained the empty-string default.

## GREEN

Implemented:

- read-only schema inspection through `system.columns`;
- one aggregate-only fact query for row count, date range, critical nulls,
  positive finite amounts, duplicate order keys, and two deterministic XOR
  fingerprints;
- type-driven nullable canonicalization before aggregate hashing;
- archive SHA-256 and exact reviewed physical schema validation;
- source business semantics validation for success status, Lagos business date,
  dedup key, and latest-completion rule;
- complete immutable snapshot payload and canonical release authority;
- lock, validate-before-write, one `publish_dataset_snapshot_release()` call,
  same-release idempotence, and immutable drift fail closed;
- dry-run and publish CLI modes with non-secret output and explicit owner/impact
  on validation failure.

Focused GREEN:

```text
8 passed, 5 subtests passed
```

The source contract was versioned from 0.2 to 0.3 only after the first real
inspection proved that reviewed archive SHA-256 and clean physical schema were
missing. The added fields record observed facts and do not change business
semantics.

## Regression

Command:

```bash
/tmp/waje-bi-v2-py312/bin/python3 -m pytest \
  tests/phase4/test_paid_success_snapshot_registration.py \
  tests/phase4/test_dataset_release_authority.py \
  tests/phase7/test_conversation_persistence.py -q
```

Result before final verification: `48 passed, 47 subtests passed`.

Six generic persistence tests initially used `paid_order_success` as a direct
save fixture. Once the dataset became canonical required-release authority,
those fixtures correctly failed with `dataset_snapshot_release_required`.
They now use non-canonical legacy dataset IDs; the dedicated release-boundary
tests continue to verify direct-save rejection.

## Real dry-run

Command:

```bash
set -a
source /Users/luka/work/waje-bi-v2/.env
set +a
/tmp/waje-bi-v2-py312/bin/python3 \
  tools/data/register_existing_paid_success_snapshot.py \
  --archive /Users/luka/Downloads/dapan_pay_data.zip \
  --physical-table paid_order_success_clean_20240101_20260704 \
  --snapshot-id paid-order-detail-20240101-20260704 \
  --load-revision accepted-20260705 \
  --dry-run
```

First contract-aware result was withheld with exact mismatches:

- `archive_checksum:reviewed_contract_value_missing`
- `schema:reviewed_contract_value_missing`
- owner: `payment_contract_owner`
- impact: `paid_order_success authority release withheld`

After the reviewed metadata was added, dry-run exited 0 with:

- `ready_to_publish=true`
- row count: `41,234,677`
- date range: `2024-01-01` through `2026-07-04`
- watermark: `2026-07-04`
- schema fields: 22
- archive SHA-256:
  `d27df6f2a79029e360d82da74c30413c2c842c820e0bfbc85e5afcf129787ae6`
- schema fingerprint:
  `ef102782a08790495d1f77e03bd9a0067fb5323b848336da5634c0933c20871f`
- rows content hash:
  `dde17fdfb35f358034c2935f7c64561c56bbc23b4210f61fa7a744cea1bb756d`
- validation errors: none

No fact rows were loaded into Python.

## Real publish

The publish command used the same arguments with `--publish` after the passing
dry-run.

The first attempt rolled back during Postgres write-after-read validation with
`dataset_snapshot_release_validation_failed`. Investigation confirmed no
existing conflicting snapshot. The payload lacked `reconciliation_ref`; the
mirrored column defaulted to `""` while JSON extraction produced NULL. After a
RED test and contract-complete payload fix, publish exited 0.

Published refs:

- snapshot:
  `snapshot:paid-order-detail-20240101-20260704:accepted-20260705:paid_order_success`
- release:
  `dataset-release:sha256:c7223754dc4a00e969a923c92b01a19660c9fdab4e94ad8ec5a7b49498ab81ff`
- authority record:
  `dataset-release-authority:sha256:e8fa7ce41675a09587e03549f1df8d318f25936ec07c4507f4aa12d1216016b2`
- dataset IDs: `paid_order_success`

The identical publish command was run a second time and returned the same three
refs with exit code 0, proving real idempotence.

## Self-review

- Aggregate inspection returns only schema metadata and one aggregate row.
- Table identifiers are validated before interpolation.
- Nullable fields are canonicalized by physical type, covering the stable
  ClickHouse nullable-hash failure class.
- Validation errors fail closed before lock or write.
- Payloads are validated and authority integrity is checked before publication.
- Publication is under `dataset_snapshot_release_lock()` and calls the atomic
  store publication method once.
- The canonical dataset set has one exact member; extra `payment_attempt`
  membership fails validation.
- Contract version 0.3 records only live-inspected checksum and schema facts.
- CLI output contains no database credentials.

## Concerns

The aggregate content fingerprint uses ClickHouse `cityHash64` plus two
`groupBitXor` accumulators and is intentionally version-labelled in its hash
payload. It is deterministic and scalable, but future physical type or
canonicalization changes require a new load revision or source contract version.
