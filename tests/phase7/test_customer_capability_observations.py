from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_customer_observations_use_accepted_capability_outputs_without_truncation():
    route = (
        ROOT / "app/api/threads/[threadId]/observations/route.ts"
    ).read_text(encoding="utf-8")
    store = (ROOT / "app/api/_conversationStore.ts").read_text(encoding="utf-8")
    page = (ROOT / "app/page.tsx").read_text(encoding="utf-8")

    assert "resolveCustomerActor(request)" in route
    assert "loadCustomerThreadCapabilityObservations" in route
    assert "assertInternalRouteAvailable" not in route
    assert "waje_runtime.durable_call_acceptances" in store
    assert "waje_runtime.capability_execution_snapshots" in store
    assert "outcome.status IN ('succeeded', 'unavailable')" in store
    observation_query = store.split(
        "export async function loadCustomerCapabilityObservations",
        1,
    )[1].split(
        "export async function loadCustomerThreadCapabilityObservations",
        1,
    )[0]
    assert "acceptance.output_digest = outcome.output_digest" not in observation_query
    assert "payload: structuredClone(payload)" in store
    assert "CAPABILITY_EVIDENCE_OBSERVATION_BYTE_LIMIT" not in route
    assert 'summary>完整观察</summary>' in page
    assert "/api/threads/" in page
    assert "/observations" in page
    assert "ObservationValue" in page


def test_customer_observation_projection_excludes_adapter_failure_and_call_metadata():
    store = (ROOT / "app/api/_conversationStore.ts").read_text(encoding="utf-8")
    projection = store.split(
        "function projectCustomerCapabilityObservationSet",
        1,
    )[1].split(
        "export function recordCustomerRunStateFromAgentResult",
        1,
    )[0]

    assert "adapterOutput.output_payload" in projection
    assert "adapterOutput.failure" not in projection
    assert "technical_detail_ref" not in projection
    assert "input_payload" not in projection
    assert "observation_facts" not in projection
