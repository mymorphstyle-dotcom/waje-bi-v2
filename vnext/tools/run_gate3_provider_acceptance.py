#!/usr/bin/env python3
"""Ask the live Primary Agent to create an exposure-aware AnalysisFrame."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from waje_vnext.controller import (
    EffectExecutionResult,
    EffectPermanentError,
    WAJEController,
)
from waje_vnext.domain.canonical import content_sha256, freeze_json, to_jsonable
from waje_vnext.domain.controller import ControllerPhase
from waje_vnext.domain.runtime_state import OutboxMessage
from waje_vnext.providers import (
    ChatCompletionsProvider,
    ChatCompletionsProviderSettings,
    ProviderConfigurationError,
)
from waje_vnext.storage import InMemoryAuthorityStore


QUESTION = (
    "全量样本看，为什么从2024年1月开始到2026年5月结束，"
    "每个月月初的付费金额都比月中月末高一些？"
)


class SemanticContractExecutor:
    def __init__(self, contracts: tuple[dict[str, object], ...]) -> None:
        self._contracts = contracts

    def execute(self, message: OutboxMessage) -> EffectExecutionResult:
        if message.destination != "semantic_inspection":
            raise EffectPermanentError(
                "provider acceptance only permits semantic inspection"
            )
        return EffectExecutionResult(
            payload={"semantic_contracts": self._contracts},
            business_summary=(
                "Paid amount metric, business-time, source grain, snapshot, "
                "and availability contracts are available"
            ),
        )


def main() -> int:
    try:
        settings = ChatCompletionsProviderSettings.from_env()
    except ProviderConfigurationError as error:
        raise SystemExit(
            "live provider configuration is unavailable: {}".format(error)
        ) from error
    root = Path(__file__).resolve().parents[1]
    contract_paths = (
        root / "contracts" / "semantics" / "metric-paid-amount.v1.json",
        root / "contracts" / "semantics" / "source-paid-order-daily.v1.json",
    )
    contracts = tuple(
        json.loads(path.read_text(encoding="utf-8"))
        for path in contract_paths
    )
    store = InMemoryAuthorityStore()
    provider = ChatCompletionsProvider(settings)
    controller = WAJEController(
        store=store,
        provider=provider,
        effect_executor=SemanticContractExecutor(contracts),
        owner_id="gate3-live-provider",
    )
    stamp = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S%f")
    case_id = "gate3-live-{}".format(stamp)
    state = controller.start(
        case_id=case_id,
        thread_id="gate3-live-thread-{}".format(stamp),
        run_id="gate3-live-run-{}".format(stamp),
        user_message=QUESTION,
    )
    for _ in range(6):
        state = controller.advance(case_id)
        if state.phase is ControllerPhase.WAITING_FOR_EFFECT:
            state = controller.deliver_pending_effect(case_id)
        case = store.get_case(case_id)
        if case.accepted_frame_revision_id is not None:
            frame = store.get_frame(case.accepted_frame_revision_id)
            artifact = {
                "acceptance": "gate3-live-provider-frame",
                "recorded_at": datetime.now(tz=UTC).isoformat(),
                "provider": settings.provider_name,
                "model": settings.model,
                "question": QUESTION,
                "frame": to_jsonable(frame),
                "action_kinds": [
                    record.action.kind.value
                    for record in (
                        store.get_action(event.action_id)
                        for event in store.list_events(case_id)
                        if event.event_type.value == "action_admitted"
                        and event.action_id is not None
                    )
                ],
            }
            artifact["content_sha256"] = content_sha256(
                freeze_json(artifact)
            )
            artifact_root = root / "artifacts" / "gate3-live-provider"
            artifact_root.mkdir(parents=True, exist_ok=True)
            path = artifact_root / "analysis-frame.json"
            path.write_text(
                json.dumps(
                    artifact,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            print(
                json.dumps(
                    {
                        "artifact": str(path),
                        "content_sha256": artifact["content_sha256"],
                        "provider": settings.provider_name,
                        "model": settings.model,
                        "frame_revision_id": frame.frame_revision_id,
                        "comparison_group_count": len(
                            frame.comparison.groups
                        ),
                        "exposure_balance_assumption": (
                            frame.exposure.balance_assumption.value
                        ),
                        "requirement_count": len(frame.requirements),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if state.phase is ControllerPhase.WAITING_FOR_USER:
            raise SystemExit(
                "live provider requested a user decision before using "
                "available semantic facts: {}".format(
                    json.dumps(_diagnostics(store, case_id), sort_keys=True)
                )
            )
        if state.phase in {
            ControllerPhase.COMPLETED,
            ControllerPhase.STOPPED,
        }:
            raise SystemExit(
                "live provider terminated before creating AnalysisFrame: "
                "{}".format(
                    json.dumps(_diagnostics(store, case_id), sort_keys=True)
                )
            )
    raise SystemExit(
        "live provider did not create AnalysisFrame in six turns: {}".format(
            json.dumps(_diagnostics(store, case_id), sort_keys=True)
        )
    )


def _diagnostics(
    store: InMemoryAuthorityStore,
    case_id: str,
) -> dict[str, object]:
    outcomes = []
    decisions = []
    for event in store.list_events(case_id):
        if event.event_type.value == "user_decision_requested":
            decisions.append(event.customer_projection)
        if event.event_type.value not in {
            "action_admitted",
            "action_rejected",
        }:
            continue
        action = store.get_action(event.action_id or "").action
        outcomes.append(
            {
                "kind": action.kind.value,
                "outcome": event.event_type.value,
                "reason_code": event.payload["reason_code"],
            }
        )
    return {
        "action_outcomes": outcomes,
        "decision_requests": decisions,
    }


if __name__ == "__main__":
    raise SystemExit(main())
