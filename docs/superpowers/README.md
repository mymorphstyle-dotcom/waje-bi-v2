# Archived implementation plans

Files under `docs/superpowers/` are historical implementation plans and design snapshots. They preserve the terminology and constraints that applied when each plan was written, and they do not define the current product, runtime, contract, test, or acceptance semantics.

Current authority lives in `AGENTS.md`, `docs/prd.md`, `docs/product-decisions.md`, `contracts/`, current runtime code, and current eval/test contracts. In particular, earlier product-role hierarchies, role-derived data scopes, `permission_limited` states, permission-filtered artifacts, and `aggregate_permission_allowed` preconditions are superseded by:

- one BI analysis capability for every normal user;
- identity used only for personal history/artifact ownership, performance safety, audit, and rate limits;
- fixed customer-safe restricted-output rules for raw identifiers, individual-level detail, and unsafe sparse aggregates;
- service-level source-connection access that blocks only dependent query paths.
