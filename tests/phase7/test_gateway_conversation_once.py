from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.phase7 import run_gateway_conversation_once as gateway_once


def _status_event(status: str) -> dict:
    return {
        "event": "run_status",
        "runId": "run-checkpoint",
        "payload": {"status": status},
    }


class GatewayConversationOnceTest(unittest.TestCase):
    def test_poll_collects_persisted_events_until_user_checkpoint(self):
        node_event = {
            "event": "node_process",
            "runId": "run-checkpoint",
            "payload": {"node_name": "understand_business_intent"},
        }
        with patch.object(
            gateway_once,
            "_events",
            side_effect=[
                [_status_event("running"), node_event],
                [_status_event("waiting_for_clarification"), node_event],
            ],
        ), patch.object(gateway_once, "sleep"):
            result = gateway_once._poll_run_events(
                base_url="http://gateway.test",
                run_id="run-checkpoint",
                events_url="/api/runs/run-checkpoint/events",
                timeout_seconds=10,
                poll_interval_seconds=0.01,
            )

        self.assertTrue(result["checkpoint_reached"])
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["terminal_status"], "waiting_for_clarification")
        self.assertEqual(result["poll_attempts"], 2)
        self.assertEqual(result["events"].count(node_event), 1)

    def test_poll_timeout_keeps_observed_events_and_returns_nonterminal_result(self):
        running = _status_event("running")
        with patch.object(gateway_once, "_events", return_value=[running]), patch.object(
            gateway_once,
            "monotonic",
            side_effect=[0.0, 2.0],
        ), patch.object(gateway_once, "sleep") as sleeper:
            result = gateway_once._poll_run_events(
                base_url="http://gateway.test",
                run_id="run-checkpoint",
                events_url="/api/runs/run-checkpoint/events",
                timeout_seconds=1,
                poll_interval_seconds=0.01,
            )

        self.assertFalse(result["checkpoint_reached"])
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["terminal_status"], "running")
        self.assertEqual(result["events"], [running])
        sleeper.assert_not_called()

    def test_gateway_response_requires_one_consistent_run_identity(self):
        self.assertEqual(
            gateway_once._gateway_run_id(
                {
                    "run": {"id": "run-one"},
                    "agentCore": {"result": {"run_id": "run-one"}},
                }
            ),
            "run-one",
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "gateway_run_identity_invalid",
        ):
            gateway_once._gateway_run_id(
                {
                    "run": {"id": "run-one"},
                    "resumedRunId": "run-two",
                }
            )

    def test_events_only_writes_checkpoint_artifact_and_uses_no_request_identity(self):
        checkpoint = {
            "operation": "events_only",
            "run_id": "run-checkpoint",
            "events_url": "/api/runs/run-checkpoint/events",
            "checkpoint_reached": True,
            "terminal_status": "completed",
            "timed_out": False,
            "poll_attempts": 1,
            "events": [_status_event("completed")],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "checkpoint.json"
            with patch.object(
                gateway_once,
                "_poll_existing_run",
                return_value=checkpoint,
            ) as poll, redirect_stdout(StringIO()):
                exit_code = gateway_once.main(
                    [
                        "--base-url",
                        "http://gateway.test",
                        "--run-id",
                        "run-checkpoint",
                        "--events-only",
                        "--output",
                        str(output_path),
                    ]
                )

            saved = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(saved, checkpoint)
        self.assertNotIn("request_identity", saved)
        poll.assert_called_once_with(
            base_url="http://gateway.test",
            run_id="run-checkpoint",
            timeout_seconds=gateway_once.DEFAULT_TIMEOUT_SECONDS,
            poll_interval_seconds=gateway_once.DEFAULT_POLL_INTERVAL_SECONDS,
        )


if __name__ == "__main__":
    unittest.main()
