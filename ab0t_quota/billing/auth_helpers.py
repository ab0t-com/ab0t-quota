"""Auth dependency helpers for the billing router.

Consumers use these to build the `auth_reader` and `auth_admin` deps that
get passed to `create_billing_router(...)` (or the equivalent
`paid_auth_reader` / `paid_auth_admin` arguments to `setup_quota`).

The library refuses to mount admin endpoints when `auth_admin` is None
(was previously a silent fallback to `auth_reader`, which collapsed the
permission boundary — see audit ticket 20260428). Use `make_admin_dep`
instead of hand-rolling, so every consumer ends up gating with the same
canonical permission name.

Example:

    from ab0t_quota import setup_quota
    from ab0t_quota.billing import make_reader_dep, make_admin_dep
    from ab0t_auth import AuthGuard

    auth = AuthGuard(...)

    setup_quota(
        app,
        enable_paid=True,
        paid_auth_reader=make_reader_dep(auth),
        paid_auth_admin=make_admin_dep(auth),
        ...
    )

Then grant the `billing.admin` permission only to org owners (or
whichever subset of users you consider authorised to mutate billing
state). Read-only endpoints stay open to any authenticated user.
"""

from __future__ import annotations

from typing import Any, Sequence

# Canonical permission names. Library users should add these to their
# service's permission registry (e.g. .permissions.json) and grant
# `billing.admin` only to org owners. `billing.read` is the default for
# any authenticated user.
DEFAULT_READER_PERMISSIONS: tuple[str, ...] = ("billing.read", "costs.read")
DEFAULT_ADMIN_PERMISSIONS: tuple[str, ...] = ("billing.admin", "costs.admin")


def make_reader_dep(auth_guard: Any, permissions: Sequence[str] = DEFAULT_READER_PERMISSIONS) -> Any:
    """FastAPI dep for read-level billing endpoints.

    Defaults to requiring at least one of `billing.read` / `costs.read`.
    Pass `permissions=()` (empty) to gate on "any authenticated user"
    instead — only do that when you have an external reason to.
    """
    from ab0t_auth import require_auth
    from ab0t_auth.dependencies import require_any_permission

    if not permissions:
        return require_auth(auth_guard)
    return require_any_permission(auth_guard, *permissions)


def make_admin_dep(auth_guard: Any, permissions: Sequence[str] = DEFAULT_ADMIN_PERMISSIONS) -> Any:
    """FastAPI dep for admin-level billing endpoints (subscription cancel,
    payment-method delete, default-method change, top-up).

    Defaults to requiring at least one of `billing.admin` / `costs.admin`.
    Refuses to build a "any-auth" dep — admin endpoints can mutate spend
    and subscription state, so a permission gate is mandatory. Callers
    that genuinely want every authenticated user to be a billing admin
    must pass an explicit dep (e.g. `require_auth(auth_guard)`) directly
    to `setup_quota`, making the choice visible in code review.
    """
    from ab0t_auth.dependencies import require_any_permission

    if not permissions:
        raise ValueError(
            "make_admin_dep requires at least one permission name. "
            "If you really want any authenticated user to act as billing "
            "admin, pass require_auth(auth_guard) explicitly to setup_quota."
        )
    return require_any_permission(auth_guard, *permissions)
