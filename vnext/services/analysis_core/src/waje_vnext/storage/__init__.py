"""Authority persistence contracts and adapters."""

from .in_memory import InMemoryAuthorityStore
from .postgres import PostgresAuthorityStore, apply_gate1_migration
from .ports import (
    AuthorityConflict,
    AuthorityNotFound,
    AuthorityStore,
    InvalidAuthorityTransition,
    StaleHead,
)

__all__ = [
    "AuthorityConflict",
    "AuthorityNotFound",
    "AuthorityStore",
    "InMemoryAuthorityStore",
    "InvalidAuthorityTransition",
    "PostgresAuthorityStore",
    "StaleHead",
    "apply_gate1_migration",
]
