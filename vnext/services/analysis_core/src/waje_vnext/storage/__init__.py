"""Authority persistence contracts and adapters."""

from .in_memory import InMemoryAuthorityStore
from .postgres import (
    PostgresAuthorityStore,
    apply_gate1_migration,
    apply_gate2_migration,
)
from .ports import (
    AuthorityConflict,
    AuthorityNotFound,
    AuthorityStore,
    InvalidAuthorityTransition,
    LeaseConflict,
    LeaseFenceLost,
    StaleHead,
)

__all__ = [
    "AuthorityConflict",
    "AuthorityNotFound",
    "AuthorityStore",
    "InMemoryAuthorityStore",
    "InvalidAuthorityTransition",
    "LeaseConflict",
    "LeaseFenceLost",
    "PostgresAuthorityStore",
    "StaleHead",
    "apply_gate1_migration",
    "apply_gate2_migration",
]
