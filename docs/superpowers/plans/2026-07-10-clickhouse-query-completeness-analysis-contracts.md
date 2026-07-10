# ClickHouse Query Completeness and Analysis Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立完整的多源分析合同、确定日期窗口、ClickHouse 查询完整性、精确 capability 输入绑定和可审计证据链，让真实收入问题在数据存在时得到可验证洞察，在数据缺失时返回准确的缺口与 owner。

**Architecture:** LLM 产出完整 `AnalysisProposal`，WAJE compiler 将其绑定为版本化 `AnalysisContract`、`QueryContract` 和 `CapabilityExecutionPlan`。ClickHouse source adapters 编译并执行查询，completeness validator 独立判断结果覆盖，PostgreSQL 保存 snapshot、合同、查询、完整性和证据关系；ConversationAgentCore 只把通过输入合同的结果交给 capability。

**Tech Stack:** Python 3.11+, dataclasses, zoneinfo, PyYAML, clickhouse-connect 0.15.1, PostgreSQL/psycopg 3, LangGraph 0.6.11, OpenAI-compatible real LLM client, Ruby contract/schema loaders, unittest/pytest, Next.js Gateway.

## Global Constraints

- 实施必须遵循 [设计规格](../specs/2026-07-10-clickhouse-query-completeness-analysis-contracts-design.md)。
- 所有修复先覆盖可复用失败类型，禁止为八问中的单句文本增加 runtime 特例。
- LLM 负责业务规划、候选路径、修复选择、证据解读和最终表达；compiler 负责合同、日期、权限、SQL、完整性和 claim 上限。
- `analysis_route`、`data_coverage_interpretation`、`next_action`、`evidence_interpretation`、`answer_synthesis`、`semantic_audit`、`final_business_summary`、`degraded_explanation`、`blocked_explanation` 保持真实 LLM 调用，不增加本地叙事 fallback。
- LLM retry/timeout 继续集中在 provider 子进程层；业务节点不增加重复 retry。统一 retry 为 3 次，只有显式正数 `WAJE_LLM_TIMEOUT_SECONDS` 才允许超时 kill，当前约定值为 300 秒且不设置 `max_tokens`。
- 最终 LLM audit 只标记风险，不因措辞、结构或洞察风格阻止用户获得答案。
- 权限、SQL safety、合同合法性、结果完整性和 evidence verifier 保持硬边界；硬边界 retry 必须携带失败原因。
- 所有 production/debug 端到端验证通过 `ConversationAgentCore` 或 Gateway API；node runner 只能定位单节点问题。
- `waiting_for_clarification` 是合法状态；resume 必须回到原 topic。
- 真实质量 eval 使用固定 `as_of=2026-06-03T12:00:00+01:00` 和固定 target `2026-06-02`，不随系统日期移动。
- ClickHouse 保存分析事实；PostgreSQL 保存合同、snapshot、运行、证据和资产；不增加 PostgreSQL 产品页面。
- 原始用户、订单、IP、设备标识不得离开 ClickHouse，能力输入只使用聚合结果。
- `artifacts/` 仅保存本地运行产物，不提交。
- 每个 task 先写测试、确认失败、实现、验证、再单独提交。

---

## File Map

### 新建的核心模块

- `bi_agent/runtime/analysis_contracts.py`
  - 只定义版本化分析、查询、结果、完整性和 capability 输入合同数据类型与稳定签名。
- `bi_agent/runtime/window_resolver.py`
  - 只负责业务时区、`as_of`、watermark 和重叠窗口解析。
- `bi_agent/runtime/dataset_catalog.py`
  - 管理逻辑数据集到版本化 ClickHouse snapshot 的绑定。
- `bi_agent/runtime/runtime_contract_registry.py`
  - 加载 metric/dimension/join/capability runtime bindings，不解释自然语言。
- `bi_agent/runtime/analysis_contract_compiler.py`
  - 把 LLM proposal、accepted graph、dataset catalog 和合同注册表编译为执行合同。
- `bi_agent/runtime/clickhouse_query_compiler.py`
  - 把 `QueryContract` 编译成 ClickHouse SQL，处理重叠窗口和 source adapter。
- `bi_agent/runtime/query_executor.py`
  - 统一 SQL safety、ClickHouse 执行、provider stats 和 `QueryResultEnvelope`。
- `bi_agent/runtime/query_completeness.py`
  - 验证窗口、字段、粒度、唯一性、截断、reconciliation 和 join amplification。
- `bi_agent/runtime/query_repair.py`
  - 根据 typed failure 选择 retry、recompile、clarify 或 degrade，并防止重复签名。
- `bi_agent/runtime/capability_execution.py`
  - 精确绑定 query result 到 capability input slot，生成带完整 provenance 的 evidence。
- `bi_agent/runtime/analysis_runtime.py`
  - 组合 compiler、query execution、completeness、repair 和 capability binding，供 workflow 注入。
- `tools/data/source_loader_common.py`
  - 多源 loader 的 CSV/XLSX 读取、checksum、manifest 和 ClickHouse 写入公共代码。
- `tools/data/load_market_dashboard_clickhouse.py`
  - 经营大盘整体与渠道日数据幂等导入。
- `tools/data/load_gameplay_events_clickhouse.py`
  - 玩法、外部事件和内部运营事件幂等导入。
- `tools/data/clickhouse-analysis-sources.sql`
  - 版本化多源分析表 DDL。
- `tools/phase7/review_analysis_contract_eval.py`
  - 检查真实 eval 的查询完整性、claim provenance 和 LLM 质量评分。

### 修改的现有模块

- `bi_agent/runtime/models.py`
- `bi_agent/runtime/compiler.py`
- `bi_agent/runtime/revenue_runtime_plan.py`
- `bi_agent/runtime/clickhouse_runtime.py`
- `bi_agent/runtime/clickhouse_query_planner.py`
- `bi_agent/runtime/clickhouse_revenue_rows.py`
- `bi_agent/runtime/capability_models.py`
- `bi_agent/runtime/analysis_assets.py`
- `bi_agent/runtime/langgraph_workflow.py`
- `bi_agent/runtime/answer_package.py`
- `bi_agent/runtime/llm_prompts.py`
- `bi_agent/conversation/models.py`
- `bi_agent/conversation/store.py`
- `bi_agent/conversation/postgres_store.py`
- `bi_agent/conversation/agent_core.py`
- `tools/runtime/conversation-runtime.sql`
- `tools/phase7/run_live_conversation_system_test.py`
- `evals/phase7/conversation_scenarios.yaml`
- `requirements.txt`

### 新建和扩展的测试

- `tests/phase4/test_analysis_contracts.py`
- `tests/phase4/test_dataset_catalog.py`
- `tests/phase4/test_analysis_contract_compiler.py`
- `tests/phase4/test_clickhouse_query_compiler.py`
- `tests/phase4/test_query_completeness.py`
- `tests/phase4/test_capability_execution.py`
- `tests/phase4/test_market_dashboard_ingestion.py`
- `tests/phase4/test_gameplay_event_ingestion.py`
- `tests/phase7/test_analysis_runtime_persistence.py`
- Extend: `tests/phase4/test_llm_workflow.py`
- Extend: `tests/phase4/test_clickhouse_revenue_rows.py`
- Extend: `tests/phase7/test_agent_core_bridge.py`
- Extend: `tests/phase7/test_analysis_assets.py`
- Extend: `tests/phase7/test_conversation_persistence.py`

---

### Task 1: Typed Analysis Contracts and Deterministic Windows

**Files:**
- Create: `bi_agent/runtime/analysis_contracts.py`
- Create: `bi_agent/runtime/window_resolver.py`
- Create: `tests/phase4/test_analysis_contracts.py`

**Interfaces:**
- Produces: `ResolvedWindow`, `ContractGap`, `AnalysisContract`, `QueryContract`, `QueryResultEnvelope`, `CompletenessReport`, `CapabilityInputSlot`, `CapabilityExecutionPlan`.
- Produces: `resolve_revenue_windows(target_semantic, baselines, as_of, timezone_name, dataset_watermarks, affected_capabilities, affected_claim_types) -> WindowResolution`.
- Consumed by: Tasks 2-11.

- [ ] **Step 1: 写失败测试，锁定日期、watermark 和重叠窗口行为**

Create `tests/phase4/test_analysis_contracts.py`:

```python
from datetime import date, datetime
import unittest

from bi_agent.runtime.analysis_contracts import stable_contract_signature
from bi_agent.runtime.window_resolver import resolve_revenue_windows


class AnalysisContractsTest(unittest.TestCase):
    def test_resolves_fixed_yesterday_and_three_baselines(self):
        result = resolve_revenue_windows(
            target_semantic="yesterday",
            baselines=("previous_day", "rolling_7_day_baseline", "same_weekday_last_week"),
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            timezone_name="Africa/Lagos",
            dataset_watermarks={"paid_order_success": date(2026, 7, 4)},
            affected_capabilities=("compare_periods",),
            affected_claim_types=("comparative_change",),
        )

        windows = {window.window_id: window for window in result.windows}
        self.assertEqual(windows["target_day"].start_inclusive, "2026-06-02")
        self.assertEqual(windows["previous_day"].start_inclusive, "2026-06-01")
        self.assertEqual(windows["rolling_7_day_baseline"].start_inclusive, "2026-05-26")
        self.assertEqual(windows["rolling_7_day_baseline"].end_exclusive, "2026-06-02")
        self.assertEqual(windows["same_weekday_last_week"].start_inclusive, "2026-05-26")
        self.assertTrue(all(window.membership_policy == "allow_overlap" for window in result.windows))
        self.assertEqual(result.gaps, ())

    def test_reports_requested_target_missing_without_shifting_it(self):
        result = resolve_revenue_windows(
            target_semantic="yesterday",
            baselines=("previous_day",),
            as_of=datetime.fromisoformat("2026-07-10T12:00:00+01:00"),
            timezone_name="Africa/Lagos",
            dataset_watermarks={"paid_order_success": date(2026, 7, 4)},
            affected_capabilities=("compare_periods",),
            affected_claim_types=("comparative_change",),
        )

        self.assertEqual(result.windows[0].start_inclusive, "2026-07-09")
        self.assertEqual(result.gaps[0].gap_type, "window_data_unavailable")
        self.assertEqual(result.gaps[0].dataset_id, "paid_order_success")
        self.assertEqual(result.gaps[0].owner, "data_owner")
        self.assertEqual(result.gaps[0].affected_capabilities, ("compare_periods",))
        self.assertEqual(result.gaps[0].affected_claim_types, ("comparative_change",))

    def test_rejects_duplicate_baselines(self):
        with self.assertRaisesRegex(ValueError, "duplicate_baseline:previous_day"):
            resolve_revenue_windows(
                target_semantic="yesterday",
                baselines=("previous_day", "previous_day"),
                as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
                timezone_name="Africa/Lagos",
                dataset_watermarks={"paid_order_success": date(2026, 7, 4)},
                affected_capabilities=("compare_periods",),
                affected_claim_types=("comparative_change",),
            )

    def test_contract_signature_is_order_stable(self):
        left = stable_contract_signature({"b": [2, 1], "a": {"x": 1}})
        right = stable_contract_signature({"a": {"x": 1}, "b": [2, 1]})
        self.assertEqual(left, right)


if __name__ == "__main__":
    unittest.main()
```

The same module also constructs `AnalysisContract`, `QueryContract`,
`CapabilityExecutionPlan`, and `QueryResultEnvelope` directly. It asserts nested
`to_dict()` output, external result references, internal-only aggregate rows,
structured readiness/degradation mappings, non-empty gap impact fields, and
distinct gap ids for two requested target dates sharing one dataset watermark.

- [ ] **Step 2: 运行测试并确认失败**

Run:

```bash
python3 -m unittest tests.phase4.test_analysis_contracts -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'bi_agent.runtime.analysis_contracts'`.

- [ ] **Step 3: 实现合同类型和稳定序列化**

Create `bi_agent/runtime/analysis_contracts.py` with these concrete definitions:

```python
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from typing import Any, Mapping


def stable_contract_signature(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ContractGap:
    gap_type: str
    gap_id: str
    dataset_id: str = ""
    affected_capabilities: tuple[str, ...] = ()
    affected_claim_types: tuple[str, ...] = ()
    owner: str = "runtime_owner"
    repair_options: tuple[str, ...] = ()
    requires_clarification: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResolvedWindow:
    window_id: str
    role: str
    label: str
    start_inclusive: str
    end_exclusive: str
    timezone: str
    aggregation: str
    required_complete_days: int
    source_watermark_requirement: str
    membership_policy: str = "allow_overlap"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MetricBinding:
    metric_id: str
    contract_ref: str
    dataset_id: str
    expression: str
    aggregation: str
    required_fields: tuple[str, ...]
    grain: tuple[str, ...]
    numerator_metric: str = ""
    denominator_metric: str = ""
    zero_denominator_policy: str = "null"
    claim_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class DimensionBinding:
    dimension_id: str
    contract_ref: str
    dataset_id: str
    source_field: str
    allowed_grains: tuple[str, ...]
    null_bucket: str = "Unknown"
    permission_scope: str = "analyst"


@dataclass(frozen=True)
class ResultShape:
    required_fields: tuple[str, ...]
    unique_key: tuple[str, ...]
    grain: tuple[str, ...]
    required_window_ids: tuple[str, ...]
    result_semantics: str = "complete_aggregate"


@dataclass(frozen=True)
class QueryContract:
    query_contract_id: str
    analysis_contract_ref: str
    query_intent: str
    dataset_snapshot_refs: tuple[str, ...]
    metric_bindings: tuple[MetricBinding, ...]
    dimension_bindings: tuple[DimensionBinding, ...]
    window_refs: tuple[str, ...]
    resolved_windows: tuple[ResolvedWindow, ...]
    filters: tuple[Mapping[str, Any], ...]
    result_shape: ResultShape
    completeness_assertions: tuple[str, ...]
    permission_scope: str
    workload_class: str
    contract_signature: str
    query_parameters: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CapabilityInputSlot:
    slot_id: str
    query_contract_refs: tuple[str, ...]
    required: bool
    accepted_completeness: tuple[str, ...]
    required_fields: tuple[str, ...]
    required_window_ids: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityExecutionPlan:
    capability_id: str
    capability_contract_ref: str
    required_input_slots: tuple[CapabilityInputSlot, ...]
    optional_input_slots: tuple[CapabilityInputSlot, ...]
    merge_strategy: str
    minimum_readiness: Mapping[str, Any]
    degradation_policy: Mapping[str, Any]
    supported_evidence_types: tuple[str, ...]
    maximum_claim_strength: str


@dataclass(frozen=True)
class AnalysisContract:
    analysis_contract_id: str
    contract_version: str
    question_families: tuple[str, ...]
    target_metric_refs: tuple[str, ...]
    claim_intents: tuple[str, ...]
    scope: Mapping[str, Any]
    business_timezone: str
    as_of: str
    resolved_windows: tuple[ResolvedWindow, ...]
    metric_bindings: tuple[MetricBinding, ...]
    dimension_bindings: tuple[DimensionBinding, ...]
    dataset_requirements: tuple[str, ...]
    capability_requirements: tuple[str, ...]
    permission_scope: str
    contract_gaps: tuple[ContractGap, ...] = ()
    clarification_outcome_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QueryResultEnvelope:
    query_contract_ref: str
    query_id: str
    query_hash: str
    result_ref: str
    execution_status: str
    rows_ref: str
    row_count: int
    completeness_report_ref: str
    # Aggregate-only in-process payload. External consumers use rows_ref.
    rows: tuple[Mapping[str, Any], ...] = ()
    observed_schema: Mapping[str, str] = field(default_factory=dict)
    observed_windows: tuple[str, ...] = ()
    observed_grain: tuple[str, ...] = ()
    source_snapshot_refs: tuple[str, ...] = ()
    provider_stats: Mapping[str, Any] = field(default_factory=dict)
    failure_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("rows")
        return payload


@dataclass(frozen=True)
class CompletenessReport:
    report_ref: str
    query_contract_ref: str
    completeness_status: str
    analysis_readiness: str
    assertion_results: tuple[Mapping[str, Any], ...]
    failure_reasons: tuple[str, ...]
    coverage_summary: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

- [ ] **Step 4: 实现 deterministic window resolver**

Create `bi_agent/runtime/window_resolver.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Mapping
from zoneinfo import ZoneInfo

from bi_agent.runtime.analysis_contracts import ContractGap, ResolvedWindow


@dataclass(frozen=True)
class WindowResolution:
    windows: tuple[ResolvedWindow, ...]
    gaps: tuple[ContractGap, ...]


def resolve_revenue_windows(
    *,
    target_semantic: str,
    baselines: tuple[str, ...],
    as_of: datetime,
    timezone_name: str,
    dataset_watermarks: Mapping[str, date],
    affected_capabilities: tuple[str, ...],
    affected_claim_types: tuple[str, ...],
) -> WindowResolution:
    seen_baselines = set()
    for baseline in baselines:
        if baseline in seen_baselines:
            raise ValueError(f"duplicate_baseline:{baseline}")
        seen_baselines.add(baseline)
    local_day = as_of.astimezone(ZoneInfo(timezone_name)).date()
    target_day = _resolve_target_day(target_semantic, local_day)
    windows = [_day_window("target_day", "target", target_day, timezone_name)]
    for baseline in baselines:
        if baseline == "previous_day":
            windows.append(_day_window("previous_day", "baseline", target_day - timedelta(days=1), timezone_name))
        elif baseline == "same_weekday_last_week":
            windows.append(_day_window("same_weekday_last_week", "baseline", target_day - timedelta(days=7), timezone_name))
        elif baseline == "rolling_7_day_baseline":
            start = target_day - timedelta(days=7)
            end = target_day - timedelta(days=1)
            windows.append(
                ResolvedWindow(
                    window_id="rolling_7_day_baseline",
                    role="baseline",
                    label=f"{start.isoformat()}..{end.isoformat()}",
                    start_inclusive=start.isoformat(),
                    end_exclusive=target_day.isoformat(),
                    timezone=timezone_name,
                    aggregation="mean_of_complete_days",
                    required_complete_days=7,
                    source_watermark_requirement=end.isoformat(),
                )
            )
        else:
            raise ValueError(f"unsupported_baseline:{baseline}")
    gaps = []
    required_end = target_day
    has_window_gap = any(watermark < required_end for watermark in dataset_watermarks.values())
    if has_window_gap and not affected_capabilities:
        raise ValueError("window_gap_requires_affected_capabilities")
    if has_window_gap and not affected_claim_types:
        raise ValueError("window_gap_requires_affected_claim_types")
    for dataset_id, watermark in sorted(dataset_watermarks.items()):
        if watermark < required_end:
            gaps.append(
                ContractGap(
                    gap_type="window_data_unavailable",
                    gap_id=(
                        f"{dataset_id}:target_day:{target_day.isoformat()}:"
                        f"watermark:{watermark.isoformat()}"
                    ),
                    dataset_id=dataset_id,
                    affected_capabilities=affected_capabilities,
                    affected_claim_types=affected_claim_types,
                    owner="data_owner",
                    repair_options=("wait_for_refresh", "use_latest_complete_business_day"),
                    requires_clarification=True,
                )
            )
    return WindowResolution(windows=tuple(windows), gaps=tuple(gaps))


def _resolve_target_day(value: str, local_day: date) -> date:
    if value in {"yesterday", "昨天"}:
        return local_day - timedelta(days=1)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"unsupported_target_semantic:{value}") from exc


def _day_window(window_id: str, role: str, day: date, timezone_name: str) -> ResolvedWindow:
    return ResolvedWindow(
        window_id=window_id,
        role=role,
        label=day.isoformat(),
        start_inclusive=day.isoformat(),
        end_exclusive=(day + timedelta(days=1)).isoformat(),
        timezone=timezone_name,
        aggregation="daily_total",
        required_complete_days=1,
        source_watermark_requirement=day.isoformat(),
    )
```

- [ ] **Step 5: 运行 Task 1 验证**

Run:

```bash
python3 -m unittest tests.phase4.test_analysis_contracts -v
python3 -m pytest tests/phase4/test_revenue_runtime_plan.py -q
```

Expected: new tests PASS; existing revenue plan tests remain PASS.

- [ ] **Step 6: 提交 Task 1**

```bash
git add bi_agent/runtime/analysis_contracts.py bi_agent/runtime/window_resolver.py tests/phase4/test_analysis_contracts.py
git commit -m "feat: add deterministic analysis contract windows"
```

---

### Task 2: Dataset Catalog and Snapshot Persistence

**Files:**
- Create: `bi_agent/runtime/dataset_catalog.py`
- Create: `tests/phase4/test_dataset_catalog.py`
- Modify: `tools/runtime/conversation-runtime.sql:43-174`
- Modify: `bi_agent/conversation/store.py:23-220`
- Modify: `bi_agent/conversation/postgres_store.py:25-520`
- Modify: `tests/phase7/test_conversation_persistence.py:88-164`

**Interfaces:**
- Consumes: source contracts and registered snapshot records.
- Produces: `DatasetSnapshot`, `DatasetCatalog.resolve(dataset_id, as_of)`, `DatasetCatalog.common_watermark(dataset_ids)`.
- Produces store methods: `save_dataset_snapshot(payload)` and `list_dataset_snapshots(dataset_id="")`.

**Accepted invariants:**
- `resolve()` requires timezone-aware `as_of` and timezone-aware snapshot `loaded_at`; both values are normalized to UTC before comparison.
- A repeated `snapshot_ref` is an idempotent full replacement. Every mirrored SQL column and the JSON payload must describe the same snapshot version.
- PostgreSQL snapshot upsert plus audit insertion is one explicit transaction. An upsert, audit, or commit exception triggers rollback and is re-raised unchanged.
- InMemory save, list, and audit-read boundaries use canonical deep copies so callers cannot mutate stored snapshots or audit history through shared nested values.

- [ ] **Step 1: 写 catalog 和 persistence 失败测试**

Create `tests/phase4/test_dataset_catalog.py`:

```python
from datetime import date, datetime
import unittest

from bi_agent.runtime.dataset_catalog import DatasetCatalog, DatasetSnapshot


class DatasetCatalogTest(unittest.TestCase):
    def test_resolves_latest_eligible_snapshot_without_hardcoded_table(self):
        catalog = DatasetCatalog(
            (
                DatasetSnapshot(
                    snapshot_ref="snapshot:paid_order:1",
                    dataset_id="paid_order_success",
                    physical_table="paid_order_success_clean_20240101_20260704",
                    watermark="2026-07-04",
                    schema_fingerprint="schema-1",
                    schema_fields=("business_date_lagos", "paid_amount_ngn"),
                    contract_ref="contracts/sources/paid-order-detail.source.yaml@0.2",
                    permission_scopes=("analyst",),
                    loaded_at="2026-07-05T00:00:00+00:00",
                    status="active",
                ),
            )
        )

        snapshot = catalog.resolve(
            "paid_order_success",
            as_of=datetime.fromisoformat("2026-07-10T00:00:00+00:00"),
            permission_scope="analyst",
        )
        self.assertEqual(snapshot.physical_table, "paid_order_success_clean_20240101_20260704")

    def test_common_watermark_uses_oldest_required_source(self):
        catalog = DatasetCatalog(
            (
                DatasetSnapshot("s1", "paid_order_success", "paid", "2026-07-04", "a", (), "c1", ("analyst",), "2026-07-05T00:00:00Z", "active"),
                DatasetSnapshot("s2", "market_dashboard", "dashboard", "2026-06-02", "b", (), "c2", ("analyst",), "2026-06-03T00:00:00Z", "active"),
            )
        )
        self.assertEqual(
            catalog.common_watermark(("paid_order_success", "market_dashboard")),
            date(2026, 6, 2),
        )


if __name__ == "__main__":
    unittest.main()
```

Add catalog regression tests for:

- naive `as_of` rejected with `timezone_aware_required:as_of`;
- naive snapshot `loaded_at` rejected with `timezone_aware_required:loaded_at`;
- equivalent UTC offsets compared as the same instant;
- latest eligible version selected while future, inactive, and permission-blocked versions are excluded.

Extend `tests/phase7/test_conversation_persistence.py`:

```python
def test_schema_and_store_persist_dataset_snapshots(self):
    self.assertIn("waje_runtime.dataset_snapshots", CONVERSATION_SCHEMA_SQL)
    connection = FakeConnection()
    store = PostgresConversationStore(connection)
    store.save_dataset_snapshot(
        {
            "snapshot_ref": "snapshot:paid_order:1",
            "dataset_id": "paid_order_success",
            "physical_table": "paid_order_success_clean_20240101_20260704",
            "watermark": "2026-07-04",
            "schema_fingerprint": "schema-1",
            "schema_fields": ["business_date_lagos", "paid_amount_ngn"],
            "contract_ref": "contracts/sources/paid-order-detail.source.yaml@0.2",
            "permission_scopes": ["analyst"],
            "loaded_at": "2026-07-05T00:00:00+00:00",
            "status": "active",
        }
    )
    sql = "\n".join(statement for statement, _ in connection.statements)
    self.assertIn("waje_runtime.dataset_snapshots", sql)
    self.assertIn("waje_runtime.audit_events", sql)
    self.assertEqual(connection.commits, 1)
```

Add persistence regression tests for:

- failure on the second execute (audit insert) rolls back once and commits zero times;
- commit failure rolls back once and commits zero times;
- `ON CONFLICT` updates every mirrored snapshot column plus `payload`, and bound mirror params match the serialized payload;
- saving a reused `snapshot_ref` replaces the complete in-memory snapshot and removes it from the previous dataset filter;
- nested `schema_fields` and `permission_scopes` cannot be mutated through the original input, list return values, or audit reads.

- [ ] **Step 2: 运行测试并确认失败**

```bash
python3 -m unittest tests.phase4.test_dataset_catalog -v
python3 -m unittest tests.phase7.test_conversation_persistence.ConversationPersistenceTest.test_schema_and_store_persist_dataset_snapshots -v
```

Expected: first command fails on missing module; second fails because schema/store method is absent.

- [ ] **Step 3: 实现 dataset catalog**

Create `bi_agent/runtime/dataset_catalog.py`:

```python
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Iterable


@dataclass(frozen=True)
class DatasetSnapshot:
    snapshot_ref: str
    dataset_id: str
    physical_table: str
    watermark: str
    schema_fingerprint: str
    schema_fields: tuple[str, ...]
    contract_ref: str
    permission_scopes: tuple[str, ...]
    loaded_at: str
    status: str

    def to_dict(self) -> dict:
        return asdict(self)


class DatasetCatalog:
    def __init__(self, snapshots: Iterable[DatasetSnapshot] = ()) -> None:
        self._snapshots = tuple(snapshots)

    def resolve(self, dataset_id: str, *, as_of: datetime, permission_scope: str) -> DatasetSnapshot:
        as_of_utc = _aware_utc(as_of, field="as_of")
        eligible = []
        for item in self._snapshots:
            if (
                item.dataset_id != dataset_id
                or item.status != "active"
                or permission_scope not in item.permission_scopes
            ):
                continue
            loaded_at_utc = _parse_datetime(item.loaded_at)
            if loaded_at_utc <= as_of_utc:
                eligible.append((loaded_at_utc, item))
        if not eligible:
            raise KeyError(f"dataset_snapshot_unavailable:{dataset_id}")
        return max(eligible, key=lambda candidate: (candidate[0], candidate[1].snapshot_ref))[1]

    def common_watermark(self, dataset_ids: tuple[str, ...]) -> date:
        watermarks = []
        for dataset_id in dataset_ids:
            candidates = [date.fromisoformat(item.watermark) for item in self._snapshots if item.dataset_id == dataset_id and item.status == "active"]
            if not candidates:
                raise KeyError(f"dataset_snapshot_unavailable:{dataset_id}")
            watermarks.append(max(candidates))
        return min(watermarks)

    def snapshots(self) -> tuple[DatasetSnapshot, ...]:
        return self._snapshots


def _parse_datetime(value: str) -> datetime:
    return _aware_utc(
        datetime.fromisoformat(value.replace("Z", "+00:00")),
        field="loaded_at",
    )


def _aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"timezone_aware_required:{field}")
    return value.astimezone(timezone.utc)
```

- [ ] **Step 4: 增加 PostgreSQL snapshot 表和两个 store 实现**

Append to `tools/runtime/conversation-runtime.sql` before indexes:

```sql
CREATE TABLE IF NOT EXISTS waje_runtime.dataset_snapshots (
  snapshot_ref text PRIMARY KEY,
  dataset_id text NOT NULL,
  physical_table text NOT NULL,
  watermark date NOT NULL,
  schema_fingerprint text NOT NULL,
  schema_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
  contract_ref text NOT NULL,
  permission_scopes jsonb NOT NULL DEFAULT '[]'::jsonb,
  loaded_at timestamptz NOT NULL,
  status text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dataset_snapshots_lookup
  ON waje_runtime.dataset_snapshots(dataset_id, status, loaded_at DESC);
```

Add matching methods to both stores. `PostgresConversationStore.save_dataset_snapshot()` performs one upsert and one audit event in one explicit transaction; `InMemoryConversationStore` stores deep-copied payloads keyed by `snapshot_ref` and returns deep copies from snapshot and audit-read boundaries.

```python
def save_dataset_snapshot(self, payload: dict[str, Any]) -> None:
    try:
        self._execute(
            """
            INSERT INTO waje_runtime.dataset_snapshots(
              snapshot_ref, dataset_id, physical_table, watermark, schema_fingerprint,
              schema_fields, contract_ref, permission_scopes, loaded_at, status, payload
            ) VALUES (
              %(snapshot_ref)s, %(dataset_id)s, %(physical_table)s, %(watermark)s,
              %(schema_fingerprint)s, %(schema_fields)s::jsonb, %(contract_ref)s,
              %(permission_scopes)s::jsonb, %(loaded_at)s, %(status)s, %(payload)s::jsonb
            )
            ON CONFLICT (snapshot_ref) DO UPDATE SET
              dataset_id = EXCLUDED.dataset_id,
              physical_table = EXCLUDED.physical_table,
              watermark = EXCLUDED.watermark,
              schema_fingerprint = EXCLUDED.schema_fingerprint,
              schema_fields = EXCLUDED.schema_fields,
              contract_ref = EXCLUDED.contract_ref,
              permission_scopes = EXCLUDED.permission_scopes,
              loaded_at = EXCLUDED.loaded_at,
              status = EXCLUDED.status,
              payload = EXCLUDED.payload
            """,
            {
                **payload,
                "schema_fields": _json(payload.get("schema_fields", [])),
                "permission_scopes": _json(payload.get("permission_scopes", [])),
                "payload": _json(payload),
            },
            commit=False,
        )
        self._audit(
            "dataset_snapshot_saved",
            ref=payload["snapshot_ref"],
            payload=payload,
            commit=False,
        )
        self.connection.commit()
    except Exception:
        self.connection.rollback()
        raise
```

The in-memory implementation uses `copy.deepcopy` when saving snapshot payloads,
when returning listed snapshots, when recording audit payloads, and when exposing
audit events to readers.

- [ ] **Step 5: 运行 Task 2 验证**

```bash
python3 -m unittest tests.phase4.test_dataset_catalog -v
python3 -m unittest tests.phase7.test_conversation_persistence -v
ruby tools/runtime/load-conversation-runtime-schema.rb
```

Expected: tests PASS; schema loader prints `Loaded conversation runtime schema`.

- [ ] **Step 6: 提交 Task 2**

```bash
git add bi_agent/runtime/dataset_catalog.py tests/phase4/test_dataset_catalog.py tools/runtime/conversation-runtime.sql bi_agent/conversation/store.py bi_agent/conversation/postgres_store.py tests/phase7/test_conversation_persistence.py
git commit -m "feat: register versioned analytical dataset snapshots"
```

---

### Task 3: Runtime Contract Registry and Analysis Compiler

**Files:**
- Create: `contracts/runtime/clickhouse-analysis-bindings.yaml`
- Create: `bi_agent/runtime/runtime_contract_registry.py`
- Create: `bi_agent/runtime/analysis_contract_compiler.py`
- Create: `tests/phase4/test_analysis_contract_compiler.py`
- Modify: `bi_agent/runtime/models.py:37-42`
- Modify: `bi_agent/runtime/revenue_runtime_plan.py:56-149`
- Modify: `bi_agent/runtime/compiler.py:240-407`

**Interfaces:**
- Consumes: LLM `AnalysisProposal`, accepted capability graph, `DatasetCatalog`, runtime binding YAML, fixed `as_of`.
- Produces: `AnalysisCompileOutcome(analysis_contract, query_contracts, capability_plans)`; compiler gaps live only in `analysis_contract.contract_gaps`.
- Keeps: `CompiledGraph.runtime_plan` as a JSON compatibility projection until Task 10 removes legacy row selection.

- [ ] **Step 1: 写失败测试，证明 compiler 只依赖 proposal 与合同**

Create `tests/phase4/test_analysis_contract_compiler.py`:

```python
from datetime import datetime
import unittest

from bi_agent.runtime.analysis_contract_compiler import compile_analysis_contract
from bi_agent.runtime.dataset_catalog import DatasetCatalog, DatasetSnapshot
from bi_agent.runtime.runtime_contract_registry import RuntimeContractRegistry


def snapshot(dataset_id, table, watermark):
    return DatasetSnapshot(
        f"snapshot:{dataset_id}:1", dataset_id, table, watermark, f"schema:{dataset_id}",
        (
            "business_date_lagos", "business_date", "event_start_date",
            "paid_amount_ngn", "user_id", "order_id", "channel", "payment_method",
            "region", "device_brand", "gameplay", "is_first_payment",
            "订单id", "支付状态", "支付发起时间",
        ),
        f"contract:{dataset_id}@1", ("analyst",), "2026-06-03T00:00:00+00:00", "active",
    )


class AnalysisContractCompilerTest(unittest.TestCase):
    def test_compiles_explicit_llm_proposal_without_question_keywords(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        catalog = DatasetCatalog((
            snapshot("paid_order_success", "paid_success", "2026-07-04"),
            snapshot("payment_attempt", "payment_raw", "2026-07-04"),
        ))
        outcome = compile_analysis_contract(
            run_id="run-1",
            proposal={
                "question_families": ["paid_amount_change_explanation"],
                "target_metrics": ["paid_amount"],
                "requested_components": ["paid_users", "first_paid_users", "paid_frequency", "avg_order_amount", "payment_success_rate"],
                "requested_dimensions": [],
                "baselines": ["previous_day", "rolling_7_day_baseline", "same_weekday_last_week"],
                "claim_intents": ["comparative_change", "formula_component_contribution"],
            },
            accepted_capabilities=("compare_periods", "driver_decomposition", "answer_verify"),
            catalog=catalog,
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        self.assertEqual(outcome.analysis_contract.resolved_windows[0].label, "2026-06-02")
        intents = {contract.query_intent for contract in outcome.query_contracts}
        self.assertIn("daily_metric_baselines", intents)
        self.assertIn("component_driver_scan", intents)
        self.assertIn("payment_success_scan", intents)
        self.assertFalse(outcome.analysis_contract.contract_gaps)

    def test_distinguishes_source_absent_from_contract_absent(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        outcome = compile_analysis_contract(
            run_id="run-2",
            proposal={
                "question_families": ["business_object_impact_review"],
                "target_metrics": ["paid_amount"],
                "requested_dimensions": [],
                "baselines": ["previous_day"],
                "requested_context_sources": ["internal_operation_event"],
                "claim_intents": ["candidate_mechanism"],
            },
            accepted_capabilities=("event_evidence", "answer_verify"),
            catalog=DatasetCatalog(()),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )
        self.assertIn("source_unbound", {gap.gap_type for gap in outcome.analysis_contract.contract_gaps})

    def test_omitted_claim_intents_with_stale_snapshot_returns_typed_window_gap(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        outcome = compile_analysis_contract(
            run_id="run-stale",
            proposal={
                "question_families": ["paid_amount_change_explanation"],
                "target_metrics": ["paid_amount"],
                "requested_dimensions": [],
                "baselines": ["previous_day"],
            },
            accepted_capabilities=("compare_periods",),
            catalog=DatasetCatalog((snapshot("paid_order_success", "paid_success", "2026-07-04"),)),
            registry=registry,
            as_of=datetime.fromisoformat("2026-07-10T12:00:00+01:00"),
            permission_scope="analyst",
        )

        window_gap = next(
            gap for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_type == "window_data_unavailable"
        )
        self.assertEqual(outcome.analysis_contract.claim_intents, ("comparative_change",))
        self.assertEqual(window_gap.affected_claim_types, ("comparative_change",))

    def test_unbound_claim_intent_returns_contract_partial_gap(self):
        registry = RuntimeContractRegistry.from_path("contracts/runtime/clickhouse-analysis-bindings.yaml")
        outcome = compile_analysis_contract(
            run_id="run-unbound-claim",
            proposal={
                "question_families": ["evidence_quality_review"],
                "target_metrics": [],
                "requested_dimensions": [],
                "baselines": [],
            },
            accepted_capabilities=("answer_verify",),
            catalog=DatasetCatalog(()),
            registry=registry,
            as_of=datetime.fromisoformat("2026-06-03T12:00:00+01:00"),
            permission_scope="analyst",
        )

        claim_gap = next(
            gap for gap in outcome.analysis_contract.contract_gaps
            if gap.gap_type == "contract_partial"
        )
        self.assertEqual(outcome.analysis_contract.claim_intents, ("unbound_claim_intent",))
        self.assertEqual(claim_gap.affected_claim_types, ("unbound_claim_intent",))


if __name__ == "__main__":
    unittest.main()
```

The Task 3 suite also locks: explicit claim ceiling rejection; dataset execution-contract validation for missing/empty/dual date sources and non-empty required fields; valid `date_field` and `date_expression` paths; dataset date/metric/dimension snapshot schema closure; run-independent canonical signatures and filter/snapshot/binding/workload sensitivity; dynamic public + canonical capability binding coverage; typed unsupported/duplicate window advisory gaps; precise payment/event/target-only dependency owners; and future-vs-eligible permission classification.

- [ ] **Step 2: 运行测试并确认失败**

```bash
python3 -m unittest tests.phase4.test_analysis_contract_compiler -v
```

Expected: FAIL on missing compiler/registry modules.

- [ ] **Step 3: 增加 reviewed runtime binding YAML**

Create `contracts/runtime/clickhouse-analysis-bindings.yaml` with these required sections and all existing revenue capability IDs:

```yaml
contract_version: "1"
artifact: clickhouse_analysis_runtime_bindings
business_timezone: Africa/Lagos

datasets:
  paid_order_success:
    date_field: business_date_lagos
    required_fields: [business_date_lagos]
  payment_attempt:
    date_expression: "toDate(toTimeZone(fromUnixTimestamp64Milli(toInt64OrZero(`支付发起时间`)), 'Africa/Lagos'))"
    required_fields: [支付发起时间]
  market_dashboard:
    date_field: business_date
    required_fields: [business_date]
  gameplay:
    date_field: business_date
    required_fields: [business_date]
  external_event:
    date_field: event_start_date
    required_fields: [event_start_date]
  internal_operation_event:
    date_field: event_start_date
    required_fields: [event_start_date]

metrics:
  paid_amount:
    contract_ref: contracts/metrics/paid-amount.metric.yaml@0.1
    dataset_id: paid_order_success
    expression: sum(paid_amount_ngn)
    aggregation: sum
    required_fields: [paid_amount_ngn]
    grain: [window_id]
    claim_types: [comparative_change, formula_component_contribution, segment_contribution_or_mix_shift]
  paid_users:
    contract_ref: contracts/backlog/missing-contracts.yaml#component_contracts
    dataset_id: paid_order_success
    expression: uniqExact(user_id)
    aggregation: distinct_count
    required_fields: [user_id]
    grain: [window_id]
  paid_orders:
    contract_ref: contracts/sources/paid-order-detail.source.yaml@0.2
    dataset_id: paid_order_success
    expression: uniqExact(order_id)
    aggregation: distinct_count
    required_fields: [order_id]
    grain: [window_id]
  first_paid_users:
    contract_ref: contracts/backlog/missing-contracts.yaml#component_contracts
    dataset_id: paid_order_success
    expression: "uniqExactIf(user_id, is_first_payment = '1')"
    aggregation: distinct_count_if
    required_fields: [user_id, is_first_payment]
    grain: [window_id]
  paid_frequency:
    contract_ref: contracts/backlog/missing-contracts.yaml#component_contracts
    dataset_id: paid_order_success
    expression: "uniqExact(order_id) / nullIf(uniqExact(user_id), 0)"
    aggregation: ratio
    required_fields: [order_id, user_id]
    grain: [window_id]
  avg_order_amount:
    contract_ref: contracts/backlog/missing-contracts.yaml#component_contracts
    dataset_id: paid_order_success
    expression: "sum(paid_amount_ngn) / nullIf(uniqExact(order_id), 0)"
    aggregation: ratio
    required_fields: [paid_amount_ngn, order_id]
    grain: [window_id]
  payment_success_rate:
    contract_ref: contracts/backlog/missing-contracts.yaml#payment_status_and_dedup_contract
    dataset_id: payment_attempt
    expression: "uniqExactIf(`订单id`, `支付状态` = 'pay_success') / nullIf(uniqExact(`订单id`), 0)"
    aggregation: ratio
    required_fields: [订单id, 支付状态, 支付发起时间]
    grain: [window_id]

dimensions:
  channel: {contract_ref: contracts/dimensions/dimensions.yaml#channel, dataset_id: paid_order_success, source_field: channel, allowed_grains: [day, window_id]}
  payment_method: {contract_ref: contracts/dimensions/dimensions.yaml#payment_method, dataset_id: paid_order_success, source_field: payment_method, allowed_grains: [day, window_id]}
  region: {contract_ref: contracts/dimensions/dimensions.yaml#region, dataset_id: paid_order_success, source_field: region, allowed_grains: [day, window_id]}
  device_brand: {contract_ref: contracts/dimensions/dimensions.yaml#device_brand, dataset_id: paid_order_success, source_field: device_brand, allowed_grains: [day, window_id]}
  gameplay: {contract_ref: contracts/dimensions/dimensions.yaml#gameplay, dataset_id: gameplay, source_field: gameplay, allowed_grains: [day, channel]}

capability_inputs:
  compare_periods:
    query_families: [daily_metric_baselines]
    required_metrics: [paid_amount]
    required_windows: [target_day]
    supported_claim_types: [comparative_change]
  rolling_window_compare: {query_families: [daily_metric_baselines], required_metrics: [paid_amount], required_windows: [target_day, rolling_7_day_baseline]}
  driver_decomposition: {query_families: [component_driver_scan, payment_success_scan], required_metrics: [paid_amount, paid_users, paid_orders, first_paid_users, paid_frequency, avg_order_amount], optional_metrics: [payment_success_rate]}
  segment_contribution: {query_families: [dimension_contribution_scan], required_metrics: [paid_amount]}
  joint_attribution: {query_families: [joint_candidate_scan], required_metrics: [paid_amount]}
  pattern_scan: {query_families: [time_bucket_scan], required_metrics: [paid_amount]}
  data_quality_profile: {query_families: [data_quality_probe], required_metrics: [paid_amount]}
  event_evidence: {query_families: [event_context_probe], required_metrics: []}
  high_value_user_contribution: {query_families: [high_value_scan], required_metrics: [paid_amount]}
  answer_verify:
    query_families: []
    required_metrics: []
    supported_claim_types: []
```

The implementation must load this YAML with `load_contract()` and reject missing sections or duplicate ids. The reviewed artifact also carries `query_shapes` keyed by query family so `ResultShape` and query parameters are contract-driven; daily observation shapes include `window_id`, `window_role`, and `observation_key`, with `observation_key` in the unique key and grain. `high_value_scan` carries reviewed `threshold_quantile=0.95`, `threshold_reference=within_window_user_paid_amount`, and `aggregation_grain=[window_id, observation_key, user_id]`. Runtime bindings cover every id from `public_capability_ids()` plus canonical legacy graph aliases. Coverage tests derive this set dynamically from the canonical registries.

- [ ] **Step 4: 实现 registry 和 compiler**

Create `RuntimeContractRegistry` with `metric()`, `dimension()`, `capability_inputs()`, and `dataset()` accessors that return copied mappings. Query semantic identity is defined once by shared `query_contract_semantic_body()` / `query_contract_signature()` helpers in `analysis_contracts.py`; Task 3 dedupe and final signatures use those helpers. The body covers query intent, snapshot refs, complete metric and dimension bindings, windows, filters, result shape, assertions, permission, workload, and reviewed `query_parameters`, while excluding identity and signature. Query-family parameters are copied from the reviewed `query_shapes` entry. Explicit proposal claim intents remain advisory: the compiler intersects them with the union of accepted capability `supported_claim_types`; no-query verifier/reducer contracts with empty claim support do not expand that ceiling. Unsupported explicit intents produce typed gaps and never enter `AnalysisContract.claim_intents`; when none remain, `unbound_claim_intent` keeps resolver attribution non-empty. Implicit binding then uses accepted capability claim types, metric claim types only when capability contracts provide none, then a typed unbound gap. `maximum_claim_strength` stays a plan/verifier boundary and is never interpreted as a claim type.

```python
@dataclass(frozen=True)
class AnalysisCompileOutcome:
    analysis_contract: AnalysisContract
    query_contracts: tuple[QueryContract, ...]
    capability_plans: tuple[CapabilityExecutionPlan, ...]


def _bind_claim_intents(
    proposal: Mapping[str, Any],
    accepted_capabilities: tuple[str, ...],
    metric_bindings: tuple[MetricBinding, ...],
    registry: RuntimeContractRegistry,
) -> tuple[tuple[str, ...], tuple[ContractGap, ...]]:
    explicit = tuple(dict.fromkeys(
        str(value).strip()
        for value in proposal.get("claim_intents") or ()
        if str(value).strip()
    ))
    capability_inferred = []
    for capability_id in accepted_capabilities:
        capability_contract = registry.capability_inputs(capability_id)
        capability_inferred.extend(capability_contract.get("supported_claim_types") or ())
    capability_ceiling = tuple(dict.fromkeys(
        str(value).strip() for value in capability_inferred if str(value).strip()
    ))
    if explicit:
        accepted = tuple(value for value in explicit if value in capability_ceiling)
        unsupported = tuple(value for value in explicit if value not in capability_ceiling)
        gaps = tuple(
            ContractGap(
                gap_type="contract_partial",
                gap_id=f"claim_intent:{value}:unsupported",
                affected_capabilities=accepted_capabilities or ("analysis_contract",),
                affected_claim_types=(value,),
                owner="contract_owner",
                repair_options=("choose_supported_claim_intent", "clarify_claim_intent"),
                requires_clarification=True,
            )
            for value in unsupported
        )
        return accepted or ("unbound_claim_intent",), gaps
    if capability_ceiling:
        return capability_ceiling, ()

    metric_inferred = []
    for binding in metric_bindings:
        metric_inferred.extend(binding.claim_types)
    accepted = tuple(dict.fromkeys(
        str(value).strip() for value in metric_inferred if str(value).strip()
    ))
    if accepted:
        return accepted, ()

    diagnosed = ("unbound_claim_intent",)
    return diagnosed, (
        ContractGap(
            gap_type="contract_partial",
            gap_id="claim_intents:unbound",
            affected_capabilities=tuple(dict.fromkeys(accepted_capabilities)),
            affected_claim_types=diagnosed,
            owner="contract_owner",
            repair_options=(
                "bind_capability_claim_types",
                "bind_metric_claim_types",
                "clarify_claim_intent",
            ),
            requires_clarification=True,
        ),
    )


def compile_analysis_contract(...):
    dependencies = _build_dependency_index(proposal, accepted_capabilities, registry)
    required_dataset_ids = dependencies.dataset_ids
    executable_dataset_ids, dataset_contract_gaps = _validate_dataset_contracts(
        required_dataset_ids, registry, dependencies.dataset_owners,
    )
    snapshots, source_gaps = _resolve_snapshots(
        executable_dataset_ids, catalog, registry, as_of, permission_scope,
        dependencies.dataset_owners,
    )
    snapshots, dataset_schema_gaps = _validate_snapshot_schemas(
        snapshots, registry, dependencies.dataset_owners,
    )
    metric_bindings, metric_gaps = _bind_metrics(
        dependencies.metric_ids, registry, snapshots, dependencies.metric_owners,
    )
    dimension_bindings, dimension_gaps = _bind_dimensions(
        dependencies.dimension_ids, proposal, registry, permission_scope,
        snapshots, dependencies.dimension_owners,
    )
    accepted_claim_intents, claim_intent_gaps = _bind_claim_intents(
        proposal,
        accepted_capabilities,
        metric_bindings,
        registry,
    )
    resolution = _resolve_advisory_windows(
        target_semantic=str(proposal.get("target_semantic") or "yesterday"),
        baselines=tuple(proposal.get("baselines") or ()),
        as_of=as_of,
        timezone_name="Africa/Lagos",
        dataset_watermarks={item.dataset_id: date.fromisoformat(item.watermark) for item in snapshots},
        affected_capabilities=tuple(accepted_capabilities),
        affected_claim_types=accepted_claim_intents,
    )
    analysis_contract_id = f"analysis:{run_id}:1"
    query_contracts, query_refs_by_capability = _build_query_contracts(
        run_id, analysis_contract_id, accepted_capabilities, proposal, snapshots, resolution.windows,
        metric_bindings, dimension_bindings, registry, permission_scope,
    )
    capability_plans = _build_capability_plans(
        accepted_capabilities, query_contracts, query_refs_by_capability, registry
    )
    capability_input_gaps = _reconcile_capability_inputs(
        accepted_capabilities, proposal, resolution.windows, dimension_bindings,
        capability_plans, registry,
    )
    gaps = tuple((
        *source_gaps,
        *dataset_contract_gaps,
        *dataset_schema_gaps,
        *metric_gaps,
        *dimension_gaps,
        *capability_input_gaps,
        *claim_intent_gaps,
        *resolution.gaps,
    ))
    analysis = AnalysisContract(
        analysis_contract_id=analysis_contract_id,
        contract_version="1",
        question_families=tuple(proposal.get("question_families") or ()),
        target_metric_refs=tuple(binding.contract_ref for binding in metric_bindings if binding.metric_id in proposal.get("target_metrics", ())),
        claim_intents=accepted_claim_intents,
        scope=dict(proposal.get("scope") or {"type": "full_sample"}),
        business_timezone="Africa/Lagos",
        as_of=as_of.isoformat(),
        resolved_windows=resolution.windows,
        metric_bindings=metric_bindings,
        dimension_bindings=dimension_bindings,
        dataset_requirements=required_dataset_ids,
        capability_requirements=tuple(accepted_capabilities),
        permission_scope=permission_scope,
        contract_gaps=gaps,
    )
    return AnalysisCompileOutcome(analysis, query_contracts, capability_plans)
```

`_build_dependency_index()` is the single source for required metric ids and reverse metric/dimension/dataset capability owners. Source, metric, dimension, and schema gaps use only those owners; target-only dependencies use `analysis_contract`. Before catalog resolution, `_validate_dataset_contracts()` requires a non-empty field collection and exactly one non-empty string `date_field` or `date_expression`; missing, empty, invalid, or dual date sources produce a typed `contract_partial` gap owned by `contract_owner` and prevent that dataset from entering executable snapshot/query contracts. Expression dependencies remain explicitly declared in `required_fields`; the compiler never parses SQL to guess them. `DatasetCatalog.as_of_candidates()` applies active + `loaded_at <= as_of` before permission classification, so future snapshots never create a false `permission_blocked` gap. Dataset `required_fields`, metric `required_fields`, and dimension `source_field` are checked against the resolved snapshot schema; invalid snapshots/bindings cannot produce query refs.

`_resolve_advisory_windows()` converts only `unsupported_target_semantic`, `unsupported_baseline`, and `duplicate_baseline` into typed clarification gaps with empty windows/queries; unrelated resolver failures still raise.

`_build_query_contracts()` sets `analysis_contract_ref`, `window_refs`, and an
immutable `resolved_windows` snapshot on every query. One canonical semantic body covers query intent, snapshot refs, full metric/dimension bindings, windows, filters, result shape, completeness assertions, permission scope, and workload class while excluding `query_contract_id` and `analysis_contract_ref`. Its hash is both the dedupe key and `contract_signature`, so equivalent contracts keep the same signature across run ids. The default workload class is `interactive_aggregate`; reviewed YAML may override it. Dedupe retains capability ownership for every query ref. `_build_capability_plans()` binds query refs by both capability ownership and query family and returns the
structured `minimum_readiness` and `degradation_policy` mappings from reviewed
capability contracts; plans remain only on `AnalysisCompileOutcome`. `_reconcile_capability_inputs()` compares required windows, context sources, dimensions, and required query slots with the compiled result and emits typed gaps instead of leaving an accepted capability with silently empty or semantically invalid inputs.

- [ ] **Step 5: 将新 outcome 挂到 CompiledGraph，保留兼容 projection**

Add defaulted fields to `CompiledGraph`:

```python
analysis_contract: dict[str, Any] = field(default_factory=dict)
query_contracts: tuple[dict[str, Any], ...] = ()
capability_execution_plans: tuple[dict[str, Any], ...] = ()
```

Update `build_revenue_runtime_plan()` to return a compatibility dict containing `analysis_contract`, `query_contracts`, and `capability_execution_plans`. Existing legacy fields remain until Task 10. No new question-text branch is added.

- [ ] **Step 6: 运行 Task 3 验证**

```bash
python3 -m unittest tests.phase4.test_analysis_contract_compiler -v
python3 -m pytest tests/phase4/test_recipe_registry_and_compiler.py tests/phase4/test_revenue_runtime_plan.py -q
ruby tools/contracts/validate-contracts.rb
```

Expected: all commands PASS.

- [ ] **Step 7: 提交 Task 3**

```bash
git add contracts/runtime/clickhouse-analysis-bindings.yaml bi_agent/runtime/runtime_contract_registry.py bi_agent/runtime/analysis_contract_compiler.py bi_agent/runtime/models.py bi_agent/runtime/revenue_runtime_plan.py bi_agent/runtime/compiler.py tests/phase4/test_analysis_contract_compiler.py
git commit -m "feat: compile llm proposals into analysis contracts"
```

---

### Task 4: ClickHouse Query Compilation and Execution Envelopes

**Files:**
- Create: `bi_agent/runtime/clickhouse_query_compiler.py`
- Create: `bi_agent/runtime/query_executor.py`
- Create: `tests/phase4/test_clickhouse_query_compiler.py`
- Modify: `bi_agent/runtime/clickhouse_runtime.py:29-165`
- Modify: `bi_agent/runtime/clickhouse_query_planner.py:42-626`
- Modify: `bi_agent/runtime/clickhouse_revenue_rows.py:19-543`
- Modify: `bi_agent/runtime/sql_safety.py:49-58`
- Modify: `bi_agent/runtime/analysis_contracts.py`
- Modify: `bi_agent/runtime/analysis_contract_compiler.py`
- Modify: `contracts/runtime/clickhouse-analysis-bindings.yaml`
- Modify: `tests/phase4/test_analysis_contract_compiler.py`
- Modify: `tests/phase4/test_clickhouse_revenue_rows.py`
- Modify: `tests/phase4/test_sql_safety_and_binding.py`

**Interfaces:**
- Consumes: `QueryContract`, resolved `DatasetSnapshot` records.
- Produces: `CompiledQuery(sql_text, parameters, settings, query_contract_ref)`.
- Produces: `ClickHouseQueryExecutor.execute(contract, snapshots) -> QueryResultEnvelope`.

- [ ] **Step 1: 写失败测试，锁定重叠窗口和禁止 `now()`**

Create `tests/phase4/test_clickhouse_query_compiler.py`:

```python
import unittest

from bi_agent.runtime.analysis_contracts import DimensionBinding, MetricBinding, QueryContract, ResolvedWindow, ResultShape
from bi_agent.runtime.clickhouse_query_compiler import compile_clickhouse_query
from bi_agent.runtime.dataset_catalog import DatasetSnapshot


class ClickHouseQueryCompilerTest(unittest.TestCase):
    def test_compiles_overlapping_windows_as_independent_memberships(self):
        windows = (
            ResolvedWindow("target_day", "target", "2026-06-02", "2026-06-02", "2026-06-03", "Africa/Lagos", "daily_total", 1, "2026-06-02"),
            ResolvedWindow("rolling_7_day_baseline", "baseline", "2026-05-26..2026-06-01", "2026-05-26", "2026-06-02", "Africa/Lagos", "mean_of_complete_days", 7, "2026-06-01"),
            ResolvedWindow("same_weekday_last_week", "baseline", "2026-05-26", "2026-05-26", "2026-05-27", "Africa/Lagos", "daily_total", 1, "2026-05-26"),
        )
        metric = MetricBinding("paid_amount", "metric:paid_amount@1", "paid_order_success", "sum(paid_amount_ngn)", "sum", ("paid_amount_ngn",), ("window_id",))
        contract = QueryContract(
            query_contract_id="query:run:baseline:1",
            analysis_contract_ref="analysis:run:1",
            query_intent="daily_metric_baselines",
            dataset_snapshot_refs=("snapshot:paid:1",),
            metric_bindings=(metric,),
            dimension_bindings=(),
            window_refs=tuple(item.window_id for item in windows),
            resolved_windows=windows,
            filters=(),
            result_shape=ResultShape(("window_id", "window_role", "observation_key", "paid_amount"), ("window_id", "observation_key"), ("window_id", "observation_key"), tuple(item.window_id for item in windows)),
            completeness_assertions=("required_windows", "unique_key"),
            permission_scope="analyst",
            workload_class="interactive_aggregate",
            contract_signature="signature",
        )
        snapshot = DatasetSnapshot("snapshot:paid:1", "paid_order_success", "paid_success", "2026-07-04", "schema", ("business_date_lagos", "paid_amount_ngn"), "contract", ("analyst",), "2026-07-05T00:00:00Z", "active")

        compiled = compile_clickhouse_query(contract, {"snapshot:paid:1": snapshot})

        self.assertNotIn("now(", compiled.sql_text)
        self.assertRegex(compiled.sql_text.casefold(), r"\barray\s+join\b")
        self.assertEqual(compiled.parameters["start_1"], compiled.parameters["start_2"])
        self.assertNotEqual(compiled.parameters["window_id_1"], compiled.parameters["window_id_2"])
        self.assertIn("%(window_id_1)s", compiled.sql_text)
        self.assertIn("%(window_id_2)s", compiled.sql_text)
        self.assertNotIn("LIMIT 5000", compiled.sql_text)


if __name__ == "__main__":
    unittest.main()
```

Extend `tests/phase4/test_clickhouse_revenue_rows.py` with a fake client test asserting query parameters/settings reach the client and `QueryResultEnvelope` preserves provider stats plus `rows_ref`, `row_count`, and `completeness_report_ref`.

- [ ] **Step 2: 运行测试并确认失败**

```bash
python3 -m unittest tests.phase4.test_clickhouse_query_compiler -v
```

Expected: FAIL on missing query compiler.

- [ ] **Step 3: 扩展 ClickHouseRuntime 为参数化、安全、可审计执行**

Change `ClickHouseRuntime.aggregate()` to:

```python
def aggregate(
    self,
    sql: str,
    query_id: str,
    *,
    parameters: Mapping[str, Any] | None = None,
    settings: Mapping[str, Any] | None = None,
) -> ClickHouseQueryResult:
    return self._execute_select(
        sql,
        query_id=query_id,
        aggregate=True,
        parameters=parameters,
        settings=settings,
    )
```

Pass `parameters` and `settings` to `client.query()`. Compatibility retry may remove only an explicitly unsupported `query_id`. Typed `parameters` and `settings` are required safety inputs: an explicit rejection returns a precise failed result after one provider call, and broad internal `TypeError` values propagate. Add `provider_stats` to `ClickHouseQueryResult`, sourced from `result.summary` and `result.query_id` where available. Configure aggregate queries with `result_overflow_mode='throw'`; runtime never accepts server-side break/truncation as success.

- [ ] **Step 4: 实现 source adapters and window relation**

Create `clickhouse_query_compiler.py` with:

```python
@dataclass(frozen=True)
class CompiledQuery:
    sql_text: str
    parameters: Mapping[str, Any]
    settings: Mapping[str, Any]
    query_contract_ref: str


def compile_clickhouse_query(contract: QueryContract, snapshots: Mapping[str, DatasetSnapshot]) -> CompiledQuery:
    snapshot = _single_snapshot(contract, snapshots)
    date_expression = _date_expression(snapshot.dataset_id)
    parameters = _window_parameters(contract.resolved_windows)
    window_tuples = ", ".join(
        f"(%(window_id_{index})s, %(window_role_{index})s, toDate(%(start_{index})s), toDate(%(end_{index})s))"
        for index, _ in enumerate(contract.resolved_windows)
    )
    dimensions = [binding.source_field for binding in contract.dimension_bindings]
    select_dimensions = [f"{_quote_identifier(field)} AS {_quote_identifier(field)}" for field in dimensions]
    select_metrics = [f"{binding.expression} AS {_quote_identifier(binding.metric_id)}" for binding in contract.metric_bindings]
    sql = "\n".join((
        f"WITH [{window_tuples}] AS analysis_windows",
        "SELECT",
        "  tupleElement(analysis_window, 1) AS window_id,",
        "  tupleElement(analysis_window, 2) AS window_role,",
        f"  toString({date_expression}) AS observation_key,",
        "  " + ",\n  ".join((*select_dimensions, *select_metrics)),
        f"FROM {snapshot.physical_table}",
        "ARRAY JOIN analysis_windows AS analysis_window",
        f"WHERE {date_expression} >= tupleElement(analysis_window, 3)",
        f"  AND {date_expression} < tupleElement(analysis_window, 4)",
        "GROUP BY window_id, window_role, observation_key" + (", " + ", ".join(_quote_identifier(item) for item in dimensions) if dimensions else ""),
    ))
    return CompiledQuery(
        sql_text=sql,
        parameters=parameters,
        settings={"result_overflow_mode": "throw", "readonly": 2},
        query_contract_ref=contract.query_contract_id,
    )
```

At entry, the compiler validates the concrete `QueryContract` nested runtime types and every snapshot value before reading attributes, so malformed direct typed objects produce explicit `TypeError` or `ValueError` boundaries that the executor maps to blocked envelopes. Snapshot scalar metadata must be non-empty, watermark must be an ISO date, and loaded-at must be a timezone-aware ISO datetime. After this structural gate it recomputes the shared semantic signature and fails closed on mismatch. The non-empty, ordered, unique resolved window ids must exactly equal `window_refs`; `ResultShape.required_window_ids` must match the same tuple; each resolved window must have non-empty typed fields plus a valid timezone, date interval, watermark requirement, and complete-day bound. Metric and dimension bindings, query shape parameters, and dataset date adapters are compared exactly with reviewed registry entries. Lexical defense applies function allowlists and structural-keyword rejection after masking quoted literals and identifiers. Implement dedicated branches for `payment_attempt`, `time_bucket_scan`, `data_quality_probe`, `high_value_scan`, and event datasets. Each branch uses only active contract expressions and logical snapshot bindings. `high_value_scan` reads its threshold quantile, reference, and aggregation grain from reviewed `query_parameters`, passes the quantile as a ClickHouse parameter, and uses separate user totals, threshold, classification, and final aggregate layers so the output contains aggregate buckets without user ids or aggregate alias shadowing. Its reviewed grain is `[window_id, observation_key, user_id]`; dimension bindings are rejected until a versioned registry grain/template explicitly defines per-dimension thresholds.

- [ ] **Step 5: 实现 executor envelope and compatibility wrapper**

`ClickHouseQueryExecutor.execute()` validates SQL with
`validate_select_only(..., aggregate=True)`, calls `ClickHouseRuntime.aggregate()`,
derives observed schema/windows/grain, persists aggregate rows behind `rows_ref`,
and returns one `QueryResultEnvelope` with explicit `row_count` and
`completeness_report_ref`. The in-process `rows` payload remains aggregate-only
and is excluded from `to_dict()`. The executor generates
`result:<query_hash>:<query_contract_signature_prefix>` only after successful
execution.

Observed grain is derived from expected key fields present in every returned row;
it is empty when the result does not demonstrate that grain. Compile, signature,
permission, reviewed-contract, and SQL-safety failures return blocked envelopes;
provider failures return failed envelopes. Typed revenue execution collects every
contract envelope even when one query is blocked or failed. Typed projection is
strict for nested bindings, windows, filters, result shape, and query parameters;
malformed projections fail in typed mode and never fall through to legacy. Rows
refs include query hash, semantic signature, and snapshot identity, and row reads
return isolated copies.

The aggregate-shape validator must recognize the reviewed ClickHouse aggregate
function families used by Task 3 bindings, including `uniqExact`,
`uniqExactIf`, and `quantileExact`; it must not reject a typed aggregate merely
because the reviewed function is a stricter ClickHouse variant.

Update `ClickHouseRevenueRows` to delegate typed contracts to the executor. Keep the old `build_clickhouse_query_specs()` path only when `compiler_runtime_plan.query_contracts` is absent. Mark legacy query results with `contract_mode="legacy"`; they cannot satisfy the new completeness acceptance in Task 11.

- [ ] **Step 6: 运行 Task 4 验证**

```bash
python3 -m unittest tests.phase4.test_clickhouse_query_compiler -v
python3 -m pytest tests/phase4/test_clickhouse_query_planner.py tests/phase4/test_clickhouse_revenue_rows.py tests/phase4/test_sql_safety_and_binding.py -q
```

Expected: all tests PASS; no generated typed query contains `now(` or `LIMIT 5000`.

- [ ] **Step 7: 提交 Task 4**

```bash
git add bi_agent/runtime/clickhouse_query_compiler.py bi_agent/runtime/query_executor.py bi_agent/runtime/clickhouse_runtime.py bi_agent/runtime/clickhouse_query_planner.py bi_agent/runtime/clickhouse_revenue_rows.py tests/phase4/test_clickhouse_query_compiler.py tests/phase4/test_clickhouse_revenue_rows.py
git commit -m "feat: execute typed clickhouse query contracts"
```

---

### Task 5: Query Completeness, Reconciliation, and Repair

**Files:**
- Create: `bi_agent/runtime/query_completeness.py`
- Create: `bi_agent/runtime/query_repair.py`
- Create: `tests/phase4/test_query_completeness.py`
- Modify: `bi_agent/runtime/query_executor.py`

**Interfaces:**
- Produces: `validate_query_result(contract, result, snapshot) -> CompletenessReport`.
- Produces: `validate_query_set(contracts, results, reports) -> tuple[CompletenessReport, ...]`.
- Produces: `plan_query_repair(contract, report, attempted_signatures) -> QueryRepairDecision`.

- [ ] **Step 1: 写覆盖根因的失败测试**

Create `tests/phase4/test_query_completeness.py` with these tests:

```python
class QueryCompletenessTest(unittest.TestCase):
    def test_sql_success_with_history_but_missing_target_is_partial(self):
        report = validate_query_result(
            baseline_contract(required_windows=("target_day", "previous_day")),
            successful_result(rows=(
                {"window_id": "previous_day", "window_role": "baseline", "observation_key": "2026-06-01", "paid_amount": 100.0},
            )),
            paid_snapshot("2026-07-04"),
        )
        self.assertEqual(report.completeness_status, "partial")
        self.assertEqual(report.analysis_readiness, "blocked")
        self.assertIn("missing_required_window:target_day", report.failure_reasons)

    def test_rolling_window_requires_seven_complete_days(self):
        rows = tuple(
            {"window_id": "rolling_7_day_baseline", "window_role": "baseline", "observation_key": f"2026-05-{day:02d}", "paid_amount": 100.0}
            for day in range(26, 32)
        )
        report = validate_query_result(rolling_contract(), successful_result(rows=rows), paid_snapshot("2026-07-04"))
        self.assertIn("incomplete_window:rolling_7_day_baseline:6/7", report.failure_reasons)

    def test_same_observation_can_satisfy_two_window_memberships(self):
        rows = (
            {"window_id": "rolling_7_day_baseline", "window_role": "baseline", "observation_key": "2026-05-26", "paid_amount": 100.0},
            {"window_id": "same_weekday_last_week", "window_role": "baseline", "observation_key": "2026-05-26", "paid_amount": 100.0},
        )
        report = validate_query_result(overlap_contract(), successful_result(rows=rows), paid_snapshot("2026-07-04"))
        self.assertEqual(report.completeness_status, "complete")

    def test_result_bound_or_reconciliation_mismatch_cannot_be_complete(self):
        truncated = successful_result(rows=(), provider_stats={"result_overflow_mode": "break"})
        report = validate_query_result(baseline_contract(), truncated, paid_snapshot("2026-07-04"))
        self.assertEqual(report.completeness_status, "truncated")
```

The test module includes concrete builders returning the Task 1 dataclasses; do not mock the validator itself.

- [ ] **Step 2: 运行测试并确认失败**

```bash
python3 -m unittest tests.phase4.test_query_completeness -v
```

Expected: FAIL on missing modules.

- [ ] **Step 3: 实现 assertion-based completeness validator**

`validate_query_result()` runs these assertions in order and records each result:

```python
ASSERTIONS = (
    "execution_succeeded",
    "snapshot_watermark",
    "required_fields",
    "required_windows",
    "complete_window_days",
    "unique_key",
    "valid_denominators",
    "provider_not_truncated",
    "aggregate_only",
)
```

Use this status reducer:

```python
def _statuses(assertions):
    failures = {item["assertion"] for item in assertions if not item["passed"]}
    if "execution_succeeded" in failures:
        return "invalid", "blocked"
    if "provider_not_truncated" in failures:
        return "truncated", "blocked"
    if "snapshot_watermark" in failures:
        return "stale", "blocked"
    if failures & {"required_fields", "required_windows", "complete_window_days", "unique_key"}:
        return "partial", "blocked"
    if failures:
        return "partial", "degraded"
    return "complete", "ready"
```

`validate_query_set()` adds `dimension_total_reconciliation`, `join_cardinality`, and `paired_target_baseline` reports. Tolerance comes from the metric contract, defaulting to absolute `0.01` for currency totals and exact equality for counts.

- [ ] **Step 4: 实现 repair planner and loop prevention**

Create `query_repair.py`:

```python
@dataclass(frozen=True)
class QueryRepairDecision:
    action: str
    reason: str
    failed_query_contract_ref: str
    failed_signature: str
    requires_llm: bool
    requires_clarification: bool


def plan_query_repair(contract, report, attempted_signatures):
    if contract.contract_signature in attempted_signatures:
        return QueryRepairDecision("degrade", "repeated_query_contract_signature", contract.query_contract_id, contract.contract_signature, False, False)
    reasons = set(report.failure_reasons)
    if any(reason.startswith("transient_clickhouse:") for reason in reasons):
        return QueryRepairDecision("retry_same", "transient_clickhouse", contract.query_contract_id, contract.contract_signature, False, False)
    if any(reason.startswith(("missing_field:", "invalid_type:", "duplicate_key:")) for reason in reasons):
        return QueryRepairDecision("recompile", "query_shape_mismatch", contract.query_contract_id, contract.contract_signature, True, False)
    if any(reason.startswith(("snapshot_stale", "missing_required_window:")) for reason in reasons):
        return QueryRepairDecision("clarify", "window_coverage_failure", contract.query_contract_id, contract.contract_signature, True, True)
    return QueryRepairDecision("degrade", "insufficient_complete_evidence", contract.query_contract_id, contract.contract_signature, True, False)
```

`retry_same` is available only for transient ClickHouse transport/service errors and uses the centralized ClickHouse adapter retry policy. All other retries require a changed contract signature.

- [ ] **Step 5: 运行 Task 5 验证**

```bash
python3 -m unittest tests.phase4.test_query_completeness -v
python3 -m pytest tests/phase4/test_clickhouse_query_compiler.py tests/phase4/test_clickhouse_revenue_rows.py -q
```

Expected: all tests PASS.

- [ ] **Step 6: 提交 Task 5**

```bash
git add bi_agent/runtime/query_completeness.py bi_agent/runtime/query_repair.py bi_agent/runtime/query_executor.py tests/phase4/test_query_completeness.py
git commit -m "feat: validate clickhouse result completeness"
```

---

### Task 6: Exact Capability Inputs, Evidence Provenance, and Asset Reuse

**Files:**
- Create: `bi_agent/runtime/capability_execution.py`
- Create: `tests/phase4/test_capability_execution.py`
- Modify: `bi_agent/runtime/capability_models.py:82-108`
- Modify: `bi_agent/runtime/analysis_assets.py:1-721`
- Modify: `tests/phase7/test_analysis_assets.py`

**Interfaces:**
- Consumes: `CapabilityExecutionPlan`, `QueryResultEnvelope`, `CompletenessReport`.
- Produces: `BoundCapabilityInput` or typed blocked/degraded input.
- Extends: `CapabilityEvidenceEnvelope` with analysis/query/completeness/snapshot refs and supported claim types.

- [ ] **Step 1: 写失败测试，禁止 intent fallback 和不完整输入**

Create `tests/phase4/test_capability_execution.py`:

```python
import unittest

from bi_agent.runtime.capability_execution import bind_capability_inputs


class CapabilityExecutionTest(unittest.TestCase):
    def test_joint_attribution_rejects_unbound_daily_rows(self):
        bound = bind_capability_inputs(
            joint_plan(required_query_ref="query:joint:1"),
            results={"query:daily:1": complete_result("query:daily:1")},
            reports={"query:daily:1": ready_report("query:daily:1")},
        )
        self.assertEqual(bound.status, "blocked")
        self.assertEqual(bound.reasons, ("missing_required_slot:joint_candidates",))

    def test_complete_exact_slot_preserves_all_provenance(self):
        bound = bind_capability_inputs(
            joint_plan(required_query_ref="query:joint:1"),
            results={"query:joint:1": complete_result("query:joint:1", result_ref="result:joint:1")},
            reports={"query:joint:1": ready_report("query:joint:1", report_ref="complete:joint:1")},
        )
        self.assertEqual(bound.status, "ready")
        self.assertEqual(bound.query_contract_refs, ("query:joint:1",))
        self.assertEqual(bound.result_refs, ("result:joint:1",))
        self.assertEqual(bound.completeness_report_refs, ("complete:joint:1",))

    def test_optional_slot_can_degrade_without_replacing_required_rows(self):
        bound = bind_capability_inputs(
            driver_plan(required_query_ref="query:components:1", optional_query_ref="query:success:1"),
            results={"query:components:1": complete_result("query:components:1")},
            reports={"query:components:1": ready_report("query:components:1")},
        )
        self.assertEqual(bound.status, "degraded")
        self.assertIn("missing_optional_slot:payment_success", bound.reasons)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试并确认失败**

```bash
python3 -m unittest tests.phase4.test_capability_execution -v
```

Expected: FAIL on missing module.

- [ ] **Step 3: 实现 exact slot binder**

Create `bi_agent/runtime/capability_execution.py`:

```python
@dataclass(frozen=True)
class BoundCapabilityInput:
    capability_id: str
    status: str
    rows_by_slot: Mapping[str, tuple[Mapping[str, Any], ...]]
    reasons: tuple[str, ...]
    query_contract_refs: tuple[str, ...]
    result_refs: tuple[str, ...]
    completeness_report_refs: tuple[str, ...]
    source_snapshot_refs: tuple[str, ...]


def bind_capability_inputs(plan, *, results, reports):
    rows_by_slot = {}
    reasons = []
    query_refs = []
    result_refs = []
    report_refs = []
    snapshot_refs = []
    for slot in (*plan.required_input_slots, *plan.optional_input_slots):
        matched = _match_exact_slot(slot, results, reports)
        if matched is None:
            prefix = "missing_required_slot" if slot.required else "missing_optional_slot"
            reasons.append(f"{prefix}:{slot.slot_id}")
            continue
        result, report = matched
        rows_by_slot[slot.slot_id] = tuple(result.rows)
        query_refs.append(result.query_contract_ref)
        result_refs.append(result.result_ref)
        report_refs.append(report.report_ref)
        snapshot_refs.extend(result.source_snapshot_refs)
    missing_required = any(reason.startswith("missing_required_slot:") for reason in reasons)
    status = "blocked" if missing_required else "degraded" if reasons else "ready"
    return BoundCapabilityInput(
        capability_id=plan.capability_id,
        status=status,
        rows_by_slot=rows_by_slot,
        reasons=tuple(reasons),
        query_contract_refs=tuple(dict.fromkeys(query_refs)),
        result_refs=tuple(dict.fromkeys(result_refs)),
        completeness_report_refs=tuple(dict.fromkeys(report_refs)),
        source_snapshot_refs=tuple(dict.fromkeys(snapshot_refs)),
    )
```

`_match_exact_slot()` only accepts query refs explicitly listed in the slot, report completeness listed in `accepted_completeness`, required fields present in every row, and required windows present. It never scans unrelated results.

- [ ] **Step 4: 扩展 evidence envelope and asset signature**

Append these defaulted fields to `CapabilityEvidenceEnvelope`:

```python
analysis_contract_ref: str = ""
capability_contract_ref: str = ""
query_contract_refs: tuple[str, ...] = ()
completeness_report_refs: tuple[str, ...] = ()
source_snapshot_refs: tuple[str, ...] = ()
supported_claim_types: tuple[str, ...] = ()
```

Update every envelope builder to populate them from `BoundCapabilityInput`. Extend asset reuse signatures with concrete windows, query contract signatures, completeness digest, capability contract version, and snapshot refs. Reuse returns `context_only` whenever completeness is not `complete` or a required signature differs. Partial window overlap produces a delta-query descriptor listing exact missing window ids.

- [ ] **Step 5: 运行 Task 6 验证**

```bash
python3 -m unittest tests.phase4.test_capability_execution -v
python3 -m pytest tests/phase4/test_capability_harness.py tests/phase7/test_analysis_assets.py -q
```

Expected: all tests PASS.

- [ ] **Step 6: 提交 Task 6**

```bash
git add bi_agent/runtime/capability_execution.py bi_agent/runtime/capability_models.py bi_agent/runtime/analysis_assets.py tests/phase4/test_capability_execution.py tests/phase7/test_analysis_assets.py
git commit -m "feat: bind capabilities to complete query inputs"
```

---

### Task 7: Market Dashboard Ingestion and Runtime Binding

**Files:**
- Create: `tools/data/source_loader_common.py`
- Create: `tools/data/load_market_dashboard_clickhouse.py`
- Create: `tools/data/clickhouse-analysis-sources.sql`
- Create: `tests/phase4/test_market_dashboard_ingestion.py`
- Modify: `contracts/sources/market-dashboard.source.yaml`
- Modify: `contracts/runtime/clickhouse-analysis-bindings.yaml`

**Interfaces:**
- Produces ClickHouse tables: `market_dashboard_daily`, `market_dashboard_channel_daily`.
- Produces `SourceLoadManifest` JSON and PostgreSQL `DatasetSnapshot` payloads.
- Consumed by: market health, user growth, channel, cost-context, and reconciliation queries.

- [ ] **Step 1: 写 parser、empty-file 和 manifest 失败测试**

Create `tests/phase4/test_market_dashboard_ingestion.py` using `TemporaryDirectory` and UTF-8-SIG CSV fixtures:

```python
def test_parses_overall_and_filename_channel_rows(self):
    overall = self.write_csv("大盘_2024-01-01_2026-06-02.csv", [
        ["日期", "游戏", "日活", "首充人数", "付费人数", "付费金额", "投放成本"],
        ["2026-06-02", "Waje Special", "100", "4", "20", "3000", "120"],
    ])
    channel = self.write_csv("WajeSpecial_2024-01-01_2026-06-02.csv", [
        ["日期", "游戏", "日活", "首充人数", "付费人数", "付费金额", "投放成本"],
        ["2026-06-02", "Waje Special", "80", "3", "15", "2500", "100"],
    ])
    rows, manifest = load_market_dashboard_rows(overall, (channel,), snapshot_id="dashboard-20260602")
    self.assertEqual(rows.channel_rows[0]["channel"], "WajeSpecial")
    self.assertEqual(rows.overall_rows[0]["paid_amount"], 3000.0)
    self.assertEqual(manifest.watermark, "2026-06-02")

def test_empty_channel_file_is_no_data_not_zero_observation(self):
    empty = self.write_csv("Empty_2024-01-01_2026-06-02.csv", [["日期", "付费金额"]])
    rows, manifest = load_market_dashboard_rows(self.overall_fixture(), (empty,), snapshot_id="s1")
    self.assertEqual(rows.channel_rows, ())
    self.assertIn("Empty", manifest.no_data_partitions)
```

- [ ] **Step 2: 运行测试并确认失败**

```bash
python3 -m unittest tests.phase4.test_market_dashboard_ingestion -v
```

Expected: FAIL on missing loader module.

- [ ] **Step 3: 实现公共 loader 和 ClickHouse DDL**

`source_loader_common.py` defines:

```python
@dataclass(frozen=True)
class SourceLoadManifest:
    snapshot_ref: str
    dataset_id: str
    physical_table: str
    watermark: str
    row_count: int
    schema_fields: tuple[str, ...]
    schema_fingerprint: str
    source_checksums: Mapping[str, str]
    no_data_partitions: tuple[str, ...]
    contract_ref: str

def file_sha256(path: Path) -> str: ...
def schema_fingerprint(fields: Sequence[str]) -> str: ...
def insert_json_each_row(client, table: str, rows: Sequence[Mapping[str, Any]]) -> None: ...
```

`clickhouse-analysis-sources.sql` creates snapshot-aware MergeTree tables. The dashboard tables include `snapshot_id`, `business_date`, `game`, optional `channel`, and nullable numeric fields from the reviewed source contract. Primary ordering is `(snapshot_id, business_date, game[, channel])`.

- [ ] **Step 4: 实现幂等 dashboard loader**

The loader:

1. validates headers against the source contract;
2. derives channel only with the reviewed trailing-date filename regex;
3. deletes rows for the same `snapshot_id` before insert inside one loader run;
4. writes aggregate rows to ClickHouse;
5. validates row count, date range, and paid-amount reconciliation;
6. prints one snapshot payload accepted by `save_dataset_snapshot()`;
7. never treats empty channel files as zero observations.

Add `runtime_binding` to the source contract with logical dataset ids and table names. Keep review status unchanged until the real load and owner review complete.

- [ ] **Step 5: 运行 tests and local real load**

```bash
python3 -m unittest tests.phase4.test_market_dashboard_ingestion -v
python3 tools/data/load_market_dashboard_clickhouse.py \
  --overall /Users/luka/work/waje_bi/经营大盘-整体数据\ （2024-01-01_2026-06-02）/大盘_2024-01-01_2026-06-02.csv \
  --channels /Users/luka/work/waje_bi/经营大盘-分包渠道数据\ （2024-01-01_2026-06-02） \
  --snapshot-id dashboard-20240601-20260602 \
  --manifest-out artifacts/data-ingestion/market-dashboard-20260602.json
```

Expected: test PASS; loader reports overall/channel row counts, watermark `2026-06-02`, reconciliation status, and manifest path. If source paths are absent, record them as missing inputs owned by the data owner and do not mark the source loaded.

- [ ] **Step 6: 提交 Task 7**

```bash
git add tools/data/source_loader_common.py tools/data/load_market_dashboard_clickhouse.py tools/data/clickhouse-analysis-sources.sql tests/phase4/test_market_dashboard_ingestion.py contracts/sources/market-dashboard.source.yaml contracts/runtime/clickhouse-analysis-bindings.yaml
git commit -m "feat: ingest market dashboard analysis snapshots"
```

---

### Task 8: Gameplay and Event Ingestion Boundaries

**Files:**
- Create: `tools/data/load_gameplay_events_clickhouse.py`
- Create: `tests/phase4/test_gameplay_event_ingestion.py`
- Create: `contracts/sources/internal-operation-events.source.yaml`
- Modify: `tools/data/clickhouse-analysis-sources.sql`
- Modify: `contracts/sources/gameplay.source.yaml`
- Modify: `contracts/sources/external-events.source.yaml`
- Modify: `contracts/runtime/clickhouse-analysis-bindings.yaml`
- Modify: `requirements.txt`

**Interfaces:**
- Produces tables: `gameplay_daily`, `gameplay_channel_daily`, `business_events`.
- Supports external event context and a validated internal event import interface.
- Keeps gameplay paid-amount attribution blocked without an order-to-gameplay join contract.

- [ ] **Step 1: 写 gameplay, XLSX event, and internal event 失败测试**

Use temporary CSV files and an `openpyxl.Workbook` fixture. Assert:

```python
def test_gameplay_loader_preserves_activity_metrics_without_paid_amount_alias(self):
    rows, manifest = load_gameplay_rows(self.gameplay_files(), snapshot_id="gameplay-20260602")
    self.assertEqual(rows[0]["gameplay"], "Rummy")
    self.assertEqual(rows[0]["player_bet_amount"], 5000.0)
    self.assertNotIn("paid_amount", rows[0])
    self.assertEqual(manifest.watermark, "2026-06-02")

def test_external_event_loader_sets_context_wording_limit(self):
    rows, manifest = load_external_event_workbook(self.external_event_workbook(), snapshot_id="events-20260608")
    self.assertEqual(rows[0]["wording_limit"], "context")
    self.assertEqual(rows[0]["source_family"], "external_event")

def test_internal_event_schema_rejects_missing_authority_and_scope(self):
    with self.assertRaisesRegex(ValueError, "missing_internal_event_fields"):
        load_internal_event_rows(self.write_internal_csv([["event_id", "event_start_date"], ["e1", "2026-06-02"]]), snapshot_id="ops-1")
```

- [ ] **Step 2: 运行测试并确认失败**

```bash
python3 -m unittest tests.phase4.test_gameplay_event_ingestion -v
```

Expected: FAIL on missing loader module or `openpyxl`.

- [ ] **Step 3: 增加 XLSX dependency and tables**

Append `openpyxl==3.1.5` to `requirements.txt`. Extend DDL with:

- `gameplay_daily(snapshot_id, business_date, service_scope, gameplay, metrics...)`
- `gameplay_channel_daily(snapshot_id, business_date, channel, service_scope, gameplay, metrics...)`
- `business_events(snapshot_id, source_family, event_id, event_type, event_start_date, event_end_date, affected_scope, authority, evidence_level, wording_limit, payload)`

All tables retain `snapshot_id` in the ordering key.

- [ ] **Step 4: 实现 three ingestion paths**

`load_gameplay_events_clickhouse.py` exposes pure parsing functions used by tests and CLI subcommands:

```bash
python3 tools/data/load_gameplay_events_clickhouse.py gameplay ...
python3 tools/data/load_gameplay_events_clickhouse.py external-events ...
python3 tools/data/load_gameplay_events_clickhouse.py internal-events ...
```

The gameplay parser selects the first reviewed duplicate `服务费抽水` column, maps `机器人输的chah` to `robot_cash_lost_raw`, and never creates payment metrics. The external-event parser is WAJE-owned and reads the nine reviewed sheets directly. The internal-event parser requires event id, type, start/end, affected scope, source authority, evidence level, and wording limit.

Create `internal-operation-events.source.yaml` with `data_contract_state: source_unbound`, owner `data_operations_owner`, input schema, allowed candidate-mechanism wording, and blocked causal/ROI claims. The loader changes runtime availability only after a snapshot is successfully registered.

- [ ] **Step 5: 运行 tests and available real loads**

```bash
python3 -m unittest tests.phase4.test_gameplay_event_ingestion -v
python3 tools/data/load_gameplay_events_clickhouse.py gameplay \
  --overall "/Users/luka/Downloads/玩法-整体数据 （2024-01-01_2026-06-02）" \
  --channels "/Users/luka/Downloads/玩法-分包渠道数据 （2024-01-01_2026-06-02）" \
  --snapshot-id gameplay-20240101-20260602 \
  --manifest-out artifacts/data-ingestion/gameplay-20260602.json
python3 tools/data/load_gameplay_events_clickhouse.py external-events \
  --workbook /Users/luka/work/waje_bi/外部影响因素0608.xlsx \
  --snapshot-id external-events-20240101-20260608 \
  --manifest-out artifacts/data-ingestion/external-events-20260608.json
ruby tools/contracts/validate-contracts.rb
```

Expected: tests and contract validation PASS. Available loaders report row counts and watermarks. Internal-operation events remain `source_unbound` unless a maintained input file is supplied.

- [ ] **Step 6: 提交 Task 8**

```bash
git add tools/data/load_gameplay_events_clickhouse.py tests/phase4/test_gameplay_event_ingestion.py tools/data/clickhouse-analysis-sources.sql contracts/sources/internal-operation-events.source.yaml contracts/sources/gameplay.source.yaml contracts/sources/external-events.source.yaml contracts/runtime/clickhouse-analysis-bindings.yaml requirements.txt
git commit -m "feat: ingest gameplay and event evidence snapshots"
```

---

### Task 9: Runtime Persistence and Answer Package Provenance

**Files:**
- Create: `tests/phase7/test_analysis_runtime_persistence.py`
- Modify: `tools/runtime/conversation-runtime.sql`
- Modify: `bi_agent/conversation/store.py`
- Modify: `bi_agent/conversation/postgres_store.py`
- Modify: `bi_agent/runtime/answer_package.py`
- Modify: `bi_agent/conversation/models.py`
- Modify: `tests/phase7/test_conversation_persistence.py`

**Interfaces:**
- Produces transactional persistence for analysis contracts, query contracts/runs, completeness reports, evidence manifests, claim links, and repair attempts.
- Adds these records to Answer Package admin audit and `context_manifest` refs.

- [ ] **Step 1: 写 transactional persistence 失败测试**

Create `tests/phase7/test_analysis_runtime_persistence.py`:

```python
import unittest

from bi_agent.conversation.postgres_store import PostgresConversationStore
from tests.phase7.test_conversation_persistence import FakeConnection


class AnalysisRuntimePersistenceTest(unittest.TestCase):
    def test_persists_contract_result_completeness_and_evidence_in_one_commit(self):
        connection = FakeConnection()
        store = PostgresConversationStore(connection)
        store.save_analysis_runtime_records(
            run_id="run-1",
            analysis_contract={"analysis_contract_id": "analysis:run-1:1"},
            query_contracts=({"query_contract_id": "query:run-1:1"},),
            query_results=({"result_ref": "result:run-1:1", "query_contract_ref": "query:run-1:1"},),
            completeness_reports=({"report_ref": "complete:run-1:1", "query_contract_ref": "query:run-1:1"},),
            evidence_manifests=({"evidence_ref": "evidence:run-1:1", "result_refs": ["result:run-1:1"]},),
            claim_links=({"claim_ref": "claim:run-1:1", "evidence_ref": "evidence:run-1:1"},),
            repair_attempts=(),
        )
        sql = "\n".join(statement for statement, _ in connection.statements)
        for table in ("analysis_contracts", "query_contracts", "query_runs", "query_completeness_reports", "evidence_manifests", "claim_evidence_links"):
            self.assertIn(f"waje_runtime.{table}", sql)
        self.assertEqual(connection.commits, 1)
```

- [ ] **Step 2: 运行测试并确认失败**

```bash
python3 -m unittest tests.phase7.test_analysis_runtime_persistence -v
```

Expected: FAIL because store method and tables are absent.

- [ ] **Step 3: 增加 normalized runtime audit tables**

Add tables with primary refs and JSON payloads:

```sql
CREATE TABLE IF NOT EXISTS waje_runtime.analysis_contracts (
  analysis_contract_id text PRIMARY KEY,
  run_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  contract_signature text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.query_contracts (
  query_contract_id text PRIMARY KEY,
  run_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  analysis_contract_id text NOT NULL REFERENCES waje_runtime.analysis_contracts(analysis_contract_id) ON DELETE CASCADE,
  contract_signature text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.query_runs (
  result_ref text PRIMARY KEY,
  run_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  query_contract_id text NOT NULL REFERENCES waje_runtime.query_contracts(query_contract_id) ON DELETE CASCADE,
  execution_status text NOT NULL,
  query_hash text NOT NULL DEFAULT '',
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.query_completeness_reports (
  report_ref text PRIMARY KEY,
  result_ref text NOT NULL REFERENCES waje_runtime.query_runs(result_ref) ON DELETE CASCADE,
  completeness_status text NOT NULL,
  analysis_readiness text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.evidence_manifests (
  evidence_ref text PRIMARY KEY,
  run_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS waje_runtime.claim_evidence_links (
  claim_ref text NOT NULL,
  evidence_ref text NOT NULL REFERENCES waje_runtime.evidence_manifests(evidence_ref) ON DELETE CASCADE,
  PRIMARY KEY (claim_ref, evidence_ref)
);

CREATE TABLE IF NOT EXISTS waje_runtime.query_repair_attempts (
  attempt_ref text PRIMARY KEY,
  run_id text NOT NULL REFERENCES waje_runtime.analysis_runs(run_id) ON DELETE CASCADE,
  failed_signature text NOT NULL,
  action text NOT NULL,
  reason text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 4: 实现 one-transaction store methods**

`save_analysis_runtime_records()` performs all inserts with `commit=False`, writes one audit event with `commit=False`, then commits once. On exception it calls `connection.rollback()` and re-raises. Add the same logical API to `InMemoryConversationStore` for unit tests.

Do not persist aggregate row payloads in PostgreSQL. Persist result ref, query hash, schema/window/grain summaries, provider stats, completeness, and artifact refs. ClickHouse remains the result source.

- [ ] **Step 5: 扩展 Answer Package and ContextManifest**

Add these admin-audit fields:

```python
"analysis_contract": to_jsonable(analysis_contract),
"query_contracts": to_jsonable(query_contracts),
"query_results": to_jsonable(query_results),
"completeness_reports": to_jsonable(completeness_reports),
"capability_execution_plans": to_jsonable(capability_execution_plans),
"repair_attempts": to_jsonable(repair_attempts),
```

Each claim includes `claim_ref`, `context_manifest_ref`, `reuse_decisions`, `evidence_refs`, `result_refs`, `artifact_refs`, and `memory_refs`. `ContextManifest.sources` receives evidence and completeness refs only after verifier acceptance.

- [ ] **Step 6: 运行 Task 9 验证**

```bash
python3 -m unittest tests.phase7.test_analysis_runtime_persistence -v
python3 -m pytest tests/phase7/test_conversation_persistence.py tests/phase4/test_workflow_artifacts_answer.py -q
ruby tools/runtime/load-conversation-runtime-schema.rb
```

Expected: tests PASS; schema applies successfully.

- [ ] **Step 7: 提交 Task 9**

```bash
git add tests/phase7/test_analysis_runtime_persistence.py tools/runtime/conversation-runtime.sql bi_agent/conversation/store.py bi_agent/conversation/postgres_store.py bi_agent/runtime/answer_package.py bi_agent/conversation/models.py tests/phase7/test_conversation_persistence.py
git commit -m "feat: persist analysis contract evidence chains"
```

---

### Task 10: LangGraph and ConversationAgentCore Integration

**Files:**
- Create: `bi_agent/runtime/analysis_runtime.py`
- Modify: `bi_agent/runtime/langgraph_workflow.py:90-350, 981-1305, 1305-2015, 3497-3640`
- Modify: `bi_agent/runtime/compiler.py`
- Modify: `bi_agent/runtime/revenue_runtime_plan.py`
- Modify: `bi_agent/runtime/llm_prompts.py:28-594`
- Modify: `bi_agent/conversation/models.py:289-302`
- Modify: `bi_agent/conversation/agent_core.py:21-260, 588-618`
- Modify: `tests/phase4/fake_llm.py`
- Modify: `tests/phase4/test_llm_workflow.py`
- Modify: `tests/phase7/test_agent_core_bridge.py`

**Interfaces:**
- Produces: `AnalysisRuntime.from_environment(store)` and `AnalysisRuntime.execute(request, proposal, accepted_graph)`.
- ConversationAgentCore accepts optional internal `analysis_context` for fixed eval clocks; Gateway production calls omit it.
- Workflow consumes exact `CapabilityExecutionPlan` inputs and removes arbitrary live row fallback.

- [ ] **Step 1: 写 ConversationAgentCore fixed-clock and no-fallback tests**

Extend `tests/phase7/test_agent_core_bridge.py`:

```python
def test_agent_core_passes_fixed_analysis_clock_to_workflow(self):
    captured = {}
    def workflow(request):
        captured.update(request)
        return completed_workflow_result()
    core = ConversationAgentCore(InMemoryConversationStore(), workflow_runner=workflow)
    core.run_message(
        thread_id="thread-fixed-clock",
        user_message="昨天付费金额为什么变化？",
        analysis_context={"as_of": "2026-06-03T12:00:00+01:00"},
    )
    self.assertEqual(captured["analysis_context"]["as_of"], "2026-06-03T12:00:00+01:00")

def test_live_workflow_does_not_fall_back_to_default_pattern_rows(self):
    result = run_pattern_workflow({
        "run_id": "run-no-fallback",
        "question": "昨天付费金额为什么变化？",
        "llm_client": FakeLLMClient(),
        "analysis_runtime": EmptyAnalysisRuntime(),
        "run_mode": "live",
    })
    evidence = result.answer_package["sections"][1]["payload"]["evidence"]
    self.assertFalse(any(item.get("result_refs") == ["phase4-draft-query"] for item in evidence))
```

Extend `tests/phase4/test_llm_workflow.py` with:

- complete query results execute the exact bound capability;
- missing target routes to query-gap clarification;
- clarification answer resumes the same topic and recompiles with a changed target window;
- repeated contract signature degrades without a loop;
- final answer remains visible when final LLM audit returns warnings.

- [ ] **Step 2: 运行 targeted tests and confirm failure**

```bash
python3 -m unittest tests.phase7.test_agent_core_bridge.ConversationAgentCoreBridgeTest.test_agent_core_passes_fixed_analysis_clock_to_workflow -v
python3 -m pytest tests/phase4/test_llm_workflow.py -k "query_contract or completeness or fixed_clock" -q
```

Expected: FAIL because `analysis_context` and analysis runtime graph nodes are absent.

- [ ] **Step 3: 实现 AnalysisRuntime facade**

Create `analysis_runtime.py` with a narrow orchestration API:

```python
@dataclass(frozen=True)
class AnalysisRuntimeResult:
    analysis_contract: AnalysisContract
    query_contracts: tuple[QueryContract, ...]
    query_results: tuple[QueryResultEnvelope, ...]
    completeness_reports: tuple[CompletenessReport, ...]
    capability_plans: tuple[CapabilityExecutionPlan, ...]
    repair_decisions: tuple[QueryRepairDecision, ...]


class AnalysisRuntime:
    def __init__(self, *, catalog, registry, executor):
        self.catalog = catalog
        self.registry = registry
        self.executor = executor

    def compile(self, *, run_id, proposal, accepted_graph, as_of, permission_scope): ...
    def execute_queries(self, compile_outcome): ...
    def validate_results(self, compile_outcome, results): ...
    def bind_capabilities(self, compile_outcome, results, reports): ...
```

Each method returns immutable typed values. It does not call an LLM and does not create business prose.

- [ ] **Step 4: 扩展 LLM analysis proposal and query-gap clarification prompts**

Add `analysis_requirements` to `analysis_route` output, containing target metrics, requested components, dimensions, baselines, context sources, claim intents, and scope. Add task `query_gap_clarification` with required keys `questions`, `recommended_assumption`, `decision_summary`.

The prompt receives typed gaps and business labels. It must return 2-3 business options when changing target date, baseline, grain, permission exposure, claim strength, or material execution cost. It cannot claim that data exists or that a repair is executable.

Update `FakeLLMClient` with structured outputs. Fake output is test-only and never used in live runtime.

- [ ] **Step 5: Integrate graph nodes and remove live fallback**

Change the graph segment to:

```text
accept_analysis_route
  -> inspect_schema
  -> validate_runtime_binding
  -> fetch_runtime_rows
  -> validate_query_completeness
  -> decide_query_repair
       ready/degraded -> interpret_data_coverage
       recompile      -> repair_analysis_contract -> fetch_runtime_rows
       clarify        -> generate_query_gap_clarification -> END(waiting)
  -> execute_capabilities
```

`_accept_analysis_route()` passes `analysis_route.analysis_requirements` to the compiler. `_fetch_runtime_rows()` delegates to `AnalysisRuntime` and stores typed records. `_execute_capabilities()` calls `bind_capability_inputs()` and passes rows only from exact slots.

Replace `_capability_rows_for()` and `_capability_result_refs_for()` live behavior. Explicit fixture rows remain available only when `run_mode == "fixture"`; dry-run stays in `_dry_run_workflow`. No production path calls `_default_pattern_rows()`.

Local completeness code determines legal execution state. LLM `data_coverage_interpretation` receives completeness summaries and provides business interpretation; local code does not overwrite its narrative with a template.

- [ ] **Step 6: Wire ConversationAgentCore and persistence**

Add `analysis_context: dict | None = None` to `run_message()` and `ConversationRunRequest`. Persist it and inject `AnalysisRuntime` from `from_environment(real_clickhouse=True)`. CLI accepts optional `--as-of` for eval/debug; production Gateway omits it.

After workflow completion call `save_analysis_runtime_records()` before recording the Answer Package. A persistence failure marks the run failed with `analysis_runtime_persistence_failed`; it never publishes unpersisted verified claims.

- [ ] **Step 7: 运行 Task 10 targeted and regression tests**

```bash
python3 -m pytest tests/phase4/test_llm_workflow.py tests/phase7/test_agent_core_bridge.py tests/phase7/test_analysis_assets.py -q
python3 -m pytest tests/phase4 tests/phase7 tests/phase8 -q
npm run build
```

Expected: all Python tests PASS; Next.js build PASS. No test may use a node runner as end-to-end evidence.

- [ ] **Step 8: 提交 Task 10**

```bash
git add bi_agent/runtime/analysis_runtime.py bi_agent/runtime/langgraph_workflow.py bi_agent/runtime/compiler.py bi_agent/runtime/revenue_runtime_plan.py bi_agent/runtime/llm_prompts.py bi_agent/conversation/models.py bi_agent/conversation/agent_core.py tests/phase4/fake_llm.py tests/phase4/test_llm_workflow.py tests/phase7/test_agent_core_bridge.py
git commit -m "feat: run analysis contracts through conversation core"
```

---

### Task 11: Fixed Real Evaluation, Shadow Comparison, and Delivery Audit

**Files:**
- Create: `tools/phase7/review_analysis_contract_eval.py`
- Modify: `evals/phase7/conversation_scenarios.yaml:695-786`
- Modify: `tools/phase7/run_live_conversation_system_test.py:20-360, 585-742`
- Modify: `tests/phase7/test_agent_core_bridge.py:522-548, 803-920`
- Create: `docs/phase-7-live-conversation-eval.md`

**Interfaces:**
- Eval case provides fixed `analysis_context` and required datasets.
- Harness verifies query contracts, snapshots, completeness, capability readiness, claim provenance, and user-visible answer independently.
- Review tool emits runtime correctness and LLM quality scorecards without turning quality warnings into display gates.

- [ ] **Step 1: 写 fixed eval contract and review failures**

Extend the case:

```yaml
  - id: paid_amount_revenue_diagnostics_8_question_set
    group: production_revenue_diagnostics
    analysis_context:
      as_of: "2026-06-03T12:00:00+01:00"
      target_date: "2026-06-02"
      previous_day: "2026-06-01"
      rolling_7_day_start: "2026-05-26"
      rolling_7_day_end: "2026-06-01"
      same_weekday_last_week: "2026-05-26"
      pattern_history_start: "2026-01-01"
      anomaly_history_start: "2026-05-03"
    required_datasets:
      - paid_order_success
      - payment_attempt
      - market_dashboard
      - gameplay
      - external_event
```

Add tests asserting the dates and required datasets remain fixed. Add harness tests where a query hash exists but completeness is partial; `_real_clickhouse_review()` must fail with `incomplete_clickhouse_query:<query_contract_ref>`.

- [ ] **Step 2: 运行 tests and confirm failure**

```bash
python3 -m pytest tests/phase7/test_agent_core_bridge.py -k "revenue_diagnostic_question_set or clickhouse_query" -q
```

Expected: FAIL because fixed context and completeness review are absent.

- [ ] **Step 3: Upgrade the live harness**

`run_case()` passes `case.analysis_context` to every `ConversationAgentCore.run_message()` call, including clarification resume. `_real_clickhouse_review()` verifies:

- every executable query contract has one result and one completeness report;
- required query results have `execution_status=succeeded`;
- required capability inputs only reference `complete/ready` or contract-allowed `partial/degraded` reports;
- required snapshots match the fixed window and permission scope;
- every verified claim resolves through evidence -> result -> query contract -> snapshot;
- legacy result refs cannot satisfy this case.

Do not require fixed answer words except hard boundary text already present in the scenario. Final answer quality is evaluated by LLM and human scorecard fields.

- [ ] **Step 4: Implement eval review tool**

`review_analysis_contract_eval.py` reads the artifact and returns nonblocking quality dimensions for each turn:

```json
{
  "runtime_correctness": {
    "all_required_queries_complete": true,
    "all_capabilities_bound": true,
    "all_claims_traceable": true
  },
  "answer_quality": {
    "directness": 1,
    "insight": 1,
    "actionability": 1,
    "evidence_discipline": 1,
    "risk_markers": []
  }
}
```

Scores use a documented 1-5 rubric and the real LLM final-audit output. The tool reports quality regressions and never changes the displayed answer or process exit based solely on style scores.

- [ ] **Step 5: Apply schemas, contracts, and source manifests**

```bash
ruby tools/runtime/load-conversation-runtime-schema.rb
ruby tools/runtime/load-contracts-to-postgres.rb
python3 -m pytest tests/phase4 tests/phase7 tests/phase8 -q
ruby tools/contracts/validate-contracts.rb
npm run build
```

Expected: all commands PASS. Register successful loader manifests from Tasks 7-8 in PostgreSQL. Record absent source files and internal-operation events with missing item and owner; do not synthesize success.

- [ ] **Step 6: Run one-turn fixed-window smoke through ConversationAgentCore**

```bash
python3 -m bi_agent.conversation.agent_core \
  --thread-id fixed-window-smoke \
  --run-id fixed-window-smoke-1 \
  --message "相比前一天、近 7 日均值、上周同日，昨天付费金额为什么变化？" \
  --role analyst \
  --as-of 2026-06-03T12:00:00+01:00
```

Expected: status `completed` or a real `waiting_for_clarification`; Answer Package includes target `2026-06-02`, all three baselines, query contracts, result refs, completeness reports, and no missing-target misclassification.

- [ ] **Step 7: Run full real LLM + ClickHouse + PostgreSQL eval twice**

```bash
python3 tools/phase7/run_live_conversation_system_test.py \
  --case paid_amount_revenue_diagnostics_8_question_set \
  --real-llm \
  --real-clickhouse \
  --strict-quality \
  --artifact-dir artifacts/phase7/live-conversation-fixed-analysis-contracts-run-1

python3 tools/phase7/run_live_conversation_system_test.py \
  --case paid_amount_revenue_diagnostics_8_question_set \
  --real-llm \
  --real-clickhouse \
  --strict-quality \
  --artifact-dir artifacts/phase7/live-conversation-fixed-analysis-contracts-run-2
```

Do not set an external short timeout. Wait for every high-value LLM answer. If provider transport fails after the centralized three attempts, retain the exact failure and owner.

- [ ] **Step 8: Review both artifacts and compare with baseline**

```bash
python3 tools/phase7/review_analysis_contract_eval.py \
  artifacts/phase7/live-conversation-fixed-analysis-contracts-run-1/paid_amount_revenue_diagnostics_8_question_set.json \
  --baseline artifacts/phase7/live-conversation-real-clickhouse-post-p0-final/paid_amount_revenue_diagnostics_8_question_set.json \
  --out artifacts/phase7/live-conversation-fixed-analysis-contracts-run-1/review.json

python3 tools/phase7/review_analysis_contract_eval.py \
  artifacts/phase7/live-conversation-fixed-analysis-contracts-run-2/paid_amount_revenue_diagnostics_8_question_set.json \
  --baseline artifacts/phase7/live-conversation-real-clickhouse-post-p0-final/paid_amount_revenue_diagnostics_8_question_set.json \
  --out artifacts/phase7/live-conversation-fixed-analysis-contracts-run-2/review.json
```

Acceptance requires:

- two consecutive runs have all eight user-visible answers;
- every executed required query has contract, snapshot, result, and completeness refs;
- no missing target, hidden truncation, invalid grain, failed reconciliation, or join amplification is marked complete;
- no capability consumes unbound rows;
- supportable claims have context manifest, ReuseDecision, evidence/result/artifact/memory refs;
- Q7 has target, previous day, rolling seven-day mean, and same-weekday baseline concurrently;
- Q1 uses all available component metrics and accurately names unavailable components;
- Q2/Q5/Q6 use single-dimension screening before joint attribution;
- Q3 distinguishes available event context from missing internal-operation source;
- Q8 separates freshness, attribution, status, dedup, and abnormal-user evidence;
- final LLM audit warnings remain visible and nonblocking;
- answer-quality scorecard documents directness, insight, actionability, and evidence discipline relative to baseline.

- [ ] **Step 9: 提交 Task 11**

```bash
git add tools/phase7/review_analysis_contract_eval.py evals/phase7/conversation_scenarios.yaml tools/phase7/run_live_conversation_system_test.py tests/phase7/test_agent_core_bridge.py docs/phase-7-live-conversation-eval.md
git commit -m "test: verify fixed-window analysis contract quality"
```

---

## Final Verification and Delivery Audit

After Task 11, run:

```bash
git status --short
git log --oneline 4ecbb585..HEAD
python3 -m pytest tests/phase4 tests/phase7 tests/phase8 -q
ruby tools/contracts/validate-contracts.rb
npm run build
```

Expected:

- worktree contains no tracked `artifacts/` changes;
- exactly one implementation commit exists per task;
- all tests and build pass;
- real run artifacts and review JSON exist under the two declared local artifact directories;
- missing real source inputs list exact path/env name and owner;
- delivery report lists each task commit, every command/result, real artifact paths, query completeness delta, answer-quality delta, remaining capability gaps, reasons, and next action.

Before claiming completion, invoke `superpowers:verification-before-completion`, inspect both live artifacts directly, and request an independent code review with `superpowers:requesting-code-review`.
