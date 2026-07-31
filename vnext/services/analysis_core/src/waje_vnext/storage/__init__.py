"""Authority persistence contracts and adapters."""

from .in_memory import InMemoryAuthorityStore
from .postgres import (
    PostgresAuthorityStore,
    apply_gate1_migration,
    apply_gate2_migration,
    apply_gate3_1_migration,
    apply_gate3_2_migration,
    apply_gate3_4_migration,
    apply_gate3_5_migration,
    apply_gate3_6_migration,
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
    "apply_gate3_1_migration",
    "apply_gate3_2_migration",
    "apply_gate3_4_migration",
    "apply_gate3_5_migration",
    "apply_gate3_6_migration",
]
