# WAJE BI v2 production release

The production archive is content-addressed by the active
`bi_agent/runtime/release_manifest.json` and the exact Git commit. It contains:

- the audited Next.js standalone Gateway build;
- Python runtime, capability, contract, worker, migration, and audit sources;
- the exact Node and Python dependency declarations;
- the passing `general-agent-deployment.v1` build attestation;
- `RELEASE.json` and per-file SHA-256 checksums.

The archive intentionally excludes `.env`, credentials, local databases, raw
data, virtual environments, `node_modules` outside the standalone build,
development caches, test output, and historical analysis artifacts.

## Build

Use one clean commit and the same prebuilt Python 3.12 environment used by the
Gateway and worker:

```bash
set -a
source .env
set +a

env -u OPENAI_API_KEY \
  "$WAJE_PYTHON_EXECUTABLE" \
  -m tools.runtime.validate_general_agent_deployment \
  --all \
  --json-output artifacts/deployment-reports/production-release.json

npm run build

npm run release:package -- \
  --deployment-report artifacts/deployment-reports/production-release.json
```

The final archive, archive checksum, and release metadata are written under
`dist/releases/<manifest-version>/`.

## Runtime requirements

The target must provide:

- Node.js compatible with the locked Next.js version;
- one prebuilt and audited Python 3.12 environment matching
  `requirements.txt`;
- PostgreSQL and ClickHouse endpoints accepted by the current runtime
  contracts;
- an authenticating ingress in front of the Gateway;
- the required Provider, database, identity, readiness, and process-capacity
  environment variables documented in
  `docs/specs/general-agent-runtime/p3-deployment-acceptance.md`.

Run the archive from its extracted root:

```bash
HOSTNAME=127.0.0.1 PORT=3000 node server.js
```

Run at least one independent recovery worker with the same runtime
configuration:

```bash
"$WAJE_PYTHON_EXECUTABLE" -m tools.runtime.recover_run_dispatches
```

Schedule trace pruning separately:

```bash
"$WAJE_PYTHON_EXECUTABLE" -m tools.runtime.prune_agent_traces
```

Before accepting traffic on each production target:

1. verify the archive SHA-256 and `RELEASE-FILES.sha256`;
2. back up the target PostgreSQL authority database;
3. perform any required in-place schema upgrade;
4. run the full deployment gate against that target and preserve its new
   report;
5. verify ingress authentication, readiness token handling, Provider egress,
   Gateway health, worker health, and rollback ownership.
