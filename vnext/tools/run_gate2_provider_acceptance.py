#!/usr/bin/env python3
"""Run one live typed-action turn through the vNext provider adapter."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from waje_vnext.controller import ScriptedEffectExecutor, WAJEController
from waje_vnext.providers import (
    ChatCompletionsProvider,
    ChatCompletionsProviderSettings,
    ProviderConfigurationError,
)
from waje_vnext.storage import InMemoryAuthorityStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--question",
        default="请先为这个经营问题定义可执行的测量口径。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        settings = ChatCompletionsProviderSettings.from_env()
    except ProviderConfigurationError as error:
        raise SystemExit(
            "live provider configuration is unavailable: {}".format(error)
        ) from error
    store = InMemoryAuthorityStore()
    provider = ChatCompletionsProvider(settings)
    controller = WAJEController(
        store=store,
        provider=provider,
        effect_executor=ScriptedEffectExecutor(()),
        owner_id="gate2-live-provider",
    )
    stamp = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S%f")
    case_id = "gate2-live-{}".format(stamp)
    controller.start(
        case_id=case_id,
        thread_id="gate2-live-thread-{}".format(stamp),
        run_id="gate2-live-run-{}".format(stamp),
        user_message=args.question,
    )
    state = controller.advance(case_id)
    events = store.list_events(case_id)
    action_events = tuple(
        event
        for event in events
        if event.event_type.value in {"action_admitted", "action_rejected"}
    )
    print(
        json.dumps(
            {
                "provider": settings.provider_name,
                "model": settings.model,
                "phase": state.phase.value,
                "head_version": state.head_version,
                "action_outcome": action_events[-1].event_type.value,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
