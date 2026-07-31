"""WAJE BI Agent vNext analysis core."""

from .bootstrap import (
    CONTRACT_VERSION,
    DATABASE_SCHEMA,
    ENVIRONMENT_PREFIX,
    SERVICE_NAME,
    health_snapshot,
)

__all__ = [
    "CONTRACT_VERSION",
    "DATABASE_SCHEMA",
    "ENVIRONMENT_PREFIX",
    "SERVICE_NAME",
    "health_snapshot",
]
