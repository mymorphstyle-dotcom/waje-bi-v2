from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from waje_vnext import (
    CONTRACT_VERSION,
    DATABASE_SCHEMA,
    ENVIRONMENT_PREFIX,
    SERVICE_NAME,
    health_snapshot,
)


class BootstrapContractTest(unittest.TestCase):
    def test_runtime_identity_uses_vnext_namespaces(self) -> None:
        snapshot = health_snapshot()

        self.assertEqual(snapshot["service"], SERVICE_NAME)
        self.assertEqual(snapshot["contract_version"], CONTRACT_VERSION)
        self.assertEqual(snapshot["database_schema"], DATABASE_SCHEMA)
        self.assertEqual(snapshot["environment_prefix"], ENVIRONMENT_PREFIX)
        self.assertEqual(snapshot["python_namespace"], "waje_vnext")
        self.assertEqual(snapshot["status"], "ok")

    def test_health_snapshot_is_immutable(self) -> None:
        with self.assertRaises(TypeError):
            health_snapshot()["status"] = "mutated"  # type: ignore[index]

    def test_module_entrypoint_emits_health_json(self) -> None:
        source_root = (
            Path(__file__).resolve().parents[1]
            / "services"
            / "analysis_core"
            / "src"
        )
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(source_root),
        }
        completed = subprocess.run(
            [sys.executable, "-m", "waje_vnext", "health"],
            check=True,
            capture_output=True,
            cwd=source_root.parents[2],
            env=environment,
            text=True,
        )

        payload = json.loads(completed.stdout)
        self.assertEqual(payload["service"], SERVICE_NAME)
        self.assertEqual(payload["status"], "ok")


if __name__ == "__main__":
    unittest.main()
