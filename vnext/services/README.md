# vNext service topology

`analysis_core/` contains the current Gate 0–2 domain and conformance implementation. Deployment
adapters are split by responsibility as later Gates land:

| Unit | Contract |
|---|---|
| Command API | Authenticate, append case mailbox message + journal + controller-wake outbox in one short transaction, return `runId` and cursor |
| Agent worker | Consume controller wakes, process one case authority channel at a time, enqueue durable jobs |
| Job workers | Execute LLM, semantic, probe, capability, sensitivity, reviewer, and projection jobs outside authority transactions |
| Projection stream | Rebuild customer-safe projections from accepted heads and journal, expose cursor-based SSE/WebSocket |

The units may share one deployable during development. They cannot share in-memory authority or rely
on one process lifetime. PostgreSQL mailbox, journal, outbox, checkpoints, leases, receipts, and
accepted-head CAS remain the recovery boundary.

`asyncio` may be used inside a worker for local I/O concurrency. It does not replace the durable
cross-process contracts.
