from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_thread_owner_is_immutable_in_python_store() -> None:
    source = (
        ROOT / "bi_agent" / "conversation" / "postgres_store.py"
    ).read_text(encoding="utf-8")
    assert "ON CONFLICT (thread_id) DO NOTHING" in source
    assert "thread_owner_immutable" in source
    assert "SET owner_id = EXCLUDED.owner_id" not in source
    assert "set_config('waje.actor_id'" in source


def test_runtime_schema_forces_actor_scoped_row_level_security() -> None:
    schema = (ROOT / "tools/runtime/conversation-runtime.sql").read_text(
        encoding="utf-8"
    )
    assert "ENABLE ROW LEVEL SECURITY" in schema
    assert "FORCE ROW LEVEL SECURITY" in schema
    assert "CREATE POLICY waje_tenant_isolation" in schema
    assert "current_setting('waje.actor_id', true)" in schema
    assert "tenant_thread.owner_id" in schema
    assert "tenant_run.run_attempt_id" in schema
    assert "tenant_run.run_id" in schema


def test_every_authenticated_customer_route_enters_database_actor_scope() -> None:
    route_paths = sorted((ROOT / "app/api").glob("**/route.ts"))
    customer_routes = [
        path
        for path in route_paths
        if "resolveCustomerActor" in path.read_text(encoding="utf-8")
    ]
    assert customer_routes
    for path in customer_routes:
        source = path.read_text(encoding="utf-8")
        assert "withCustomerActorScope" in source, path


def test_gateway_actor_scope_is_transaction_local_and_pool_defaults_internal() -> None:
    source = (ROOT / "app/api/_conversationStore.ts").read_text(
        encoding="utf-8"
    )
    assert "AsyncLocalStorage" in source
    assert "SELECT set_config('waje.actor_id', $1, true)" in source
    assert 'options: "-c waje.actor_id=system"' in source
    assert "customer_database_nested_transaction_forbidden" in source


def test_deployment_backup_and_cutover_connections_use_internal_actor_scope() -> None:
    paths = (
        ROOT / "bi_agent/runtime/general_agent_deployment.py",
        ROOT / "tools/runtime/backup_waje_runtime.py",
        ROOT / "tools/runtime/cutover_single_authority_schema.py",
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert 'options="-c waje.actor_id=system"' in source, path
