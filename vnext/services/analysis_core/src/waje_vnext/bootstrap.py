"""Gate 0 runtime identity and health contract."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


SERVICE_NAME = "waje-bi-agent-vnext-analysis-core"
CONTRACT_VERSION = "gate0.bootstrap.v1"
PYTHON_NAMESPACE = "waje_vnext"
DATABASE_SCHEMA = "waje_vnext"
ENVIRONMENT_PREFIX = "WAJE_VNEXT_"

_HEALTH_SNAPSHOT = MappingProxyType(
    {
        "service": SERVICE_NAME,
        "status": "ok",
        "contract_version": CONTRACT_VERSION,
        "python_namespace": PYTHON_NAMESPACE,
        "database_schema": DATABASE_SCHEMA,
        "environment_prefix": ENVIRONMENT_PREFIX,
    }
)


def health_snapshot() -> Mapping[str, str]:
    """Return the immutable Gate 0 health projection."""

    return _HEALTH_SNAPSHOT
