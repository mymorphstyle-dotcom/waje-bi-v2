from __future__ import annotations

from collections.abc import Mapping
from typing import Any


PRODUCT_ROLE_PERMISSION_SCOPES = {
    "business_reader": "viewer",
    "analyst": "analyst",
    "data_owner_admin": "admin",
}
LEGACY_PERMISSION_PRODUCT_ROLES = {
    "viewer": "business_reader",
    "admin": "data_owner_admin",
}
SCOPE_READ_RANKS = {
    "business_reader": 1,
    "viewer": 1,
    "analyst": 2,
    "data_owner_admin": 3,
    "admin": 3,
}


def can_read_scope(
    reader_scope: str | None,
    required_scope: str | None,
) -> bool:
    reader_rank = SCOPE_READ_RANKS.get(str(reader_scope or "").strip())
    required_rank = SCOPE_READ_RANKS.get(str(required_scope or "").strip())
    return (
        reader_rank is not None
        and required_rank is not None
        and reader_rank >= required_rank
    )


def _normalized_product_role(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    role = str(value).strip()
    if role in PRODUCT_ROLE_PERMISSION_SCOPES:
        return role
    if role in LEGACY_PERMISSION_PRODUCT_ROLES:
        return LEGACY_PERMISSION_PRODUCT_ROLES[role]
    return "business_reader"


def resolve_product_runtime_roles(
    product_role: str | None,
    runtime_permission_scope: str | None = None,
    *,
    permission_context_role: str | None = None,
    default_product_role: str = "analyst",
) -> tuple[str, str]:
    explicit_role = _normalized_product_role(product_role)
    context_role = _normalized_product_role(permission_context_role)
    if explicit_role and context_role and explicit_role != context_role:
        raise PermissionError("product_role_mismatch")
    display_role = explicit_role or context_role or _normalized_product_role(
        default_product_role
    )
    if display_role is None:
        display_role = "business_reader"
    expected_scope = PRODUCT_ROLE_PERMISSION_SCOPES[display_role]
    requested_scope = str(runtime_permission_scope or "").strip()
    if requested_scope and requested_scope != expected_scope:
        raise PermissionError("runtime_permission_scope_mismatch")
    return display_role, expected_scope


def runtime_permission_scope_from_request(
    request: Mapping[str, Any],
    *,
    default_product_role: str = "analyst",
) -> str:
    permission_context = request.get("permission_context") or {}
    context_role = (
        permission_context.get("role")
        if isinstance(permission_context, Mapping)
        else None
    )
    return resolve_product_runtime_roles(
        str(request.get("role")) if request.get("role") not in (None, "") else None,
        str(request.get("runtime_permission_scope") or "") or None,
        permission_context_role=(
            str(context_role) if context_role not in (None, "") else None
        ),
        default_product_role=default_product_role,
    )[1]
