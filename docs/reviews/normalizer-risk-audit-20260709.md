# Normalizer Risk Audit - 2026-07-09

## Fixed

- `bi_agent/runtime/langgraph_workflow.py::_normalize_route_requested_nodes`
  - Risk: destructive route rewrite changed LLM-requested capabilities, especially `joint_attribution -> segment_contribution` or `driver_decomposition`.
  - Fix: production route design and repair no longer call it; compatibility helper now delegates to `compile_graph`.

- `bi_agent/conversation/runtime.py::_requested_nodes`
  - Risk: Gateway duplicated analysis-capability keyword routing before LangGraph, drifting from compiler behavior.
  - Fix: analysis capability suggestions now call `suggest_revenue_diagnostic_nodes` from compiler.

- `bi_agent/runtime/compiler.py::compile_graph`
  - Risk: compiler only filtered nodes and had shallow family enablement.
  - Fix: compiler now expands revenue-diagnostic axes into capability bundles, orders nodes by execution dependency, records auto-added mutations, and emits `compiler_runtime_plan`.

- `bi_agent/runtime/clickhouse_revenue_rows.py::ClickHouseRevenueRows.plan`
  - Risk: ClickHouse row dimensions were inferred only from accepted graph, not compiler row-shape contract.
  - Fix: provider consumes `compiler_runtime_plan.row_shapes[*].dimension_keys` and `required_fields` first.

- `bi_agent/capabilities/segment_bridge.py::_sample_size`
  - Risk: aggregate rows with `orders` or `paid_users` were treated as sample-size-unverified.
  - Fix: sample-size aliases now include `orders` and `paid_users`.

## Remaining Bounded Keyword Gates

- `bi_agent/conversation/runtime.py` local intent, topic relation, clarification, unsupported request, and memory-update guards still use business tokens.
  - Boundary: these do not compile analysis capabilities. They gate conversation control, safety, or clarification.

- `bi_agent/runtime/langgraph_workflow.py` local fallback intent binding, pattern params, boundary decisions, and answer wording repair still use bounded text checks.
  - Boundary: these are fallback or presentation policies; they should not replace accepted graph capabilities.

- `bi_agent/conversation/agent_core.py` dry-run text helpers still contain case-specific phrasing.
  - Boundary: dry-run only. Real debugging and eval must enter through `ConversationAgentCore` or Gateway API.

## Follow-up

- Move remaining pattern-param text binding into compiler plan params when revenue row-shape work expands to hour, weekday, package, gameplay, and payment-funnel contracts.
- Add a static lint that flags future destructive capability rewrites outside compiler.
