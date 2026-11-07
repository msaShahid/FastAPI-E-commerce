"""BE-P1 guard: every administrator-only route also declares a permission, and the permission list is well formed.

A route that takes ``AdminUser`` directly (an administrator, any role) would let a custom role reach it. This test fails
for such a route: use an alias from app/modules/roles/dependencies/permission_deps.py instead.
"""

import re
import uuid

from fastapi.routing import APIRoute

from app.main import app
from app.modules.auth.dependencies.auth import require_admin
from app.modules.roles.permissions import (
    NO_ACCESS_ROLE_ID,
    OWNER_ROLE_ID,
    Permission,
    effective,
    role_permissions_for_display,
)


def _routes(routes):
    """Every APIRoute, including those of included routers (this FastAPI keeps them behind an included-router wrapper)."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        inner = getattr(route, "original_router", None) or getattr(route, "router", None)
        if inner is not None and getattr(inner, "routes", None) is not routes:
            yield from _routes(inner.routes)


def _calls(dependant):
    for dep in dependant.dependencies:
        yield dep.call
        yield from _calls(dep)


def test_every_admin_only_route_declares_a_permission():
    missing, guarded = [], 0
    for route in _routes(app.routes):
        calls = list(_calls(route.dependant))
        if require_admin not in calls:
            continue
        guarded += 1
        if not any(getattr(c, "permission", None) for c in calls):
            missing.append(f"{sorted(route.methods)} {route.path}")
    assert guarded >= 130, f"only {guarded} admin routes found: the scan is broken"
    assert not missing, "administrator-only routes without a permission:\n" + "\n".join(missing)


def test_permission_keys_are_resource_dot_action_and_unique():
    values = [p.value for p in Permission]
    assert len(values) == len(set(values))
    assert all(re.fullmatch(r"[a-z]+\.[a-z]+", v) for v in values)


def test_the_owner_role_shows_every_permission_and_others_only_known_ones():
    assert role_permissions_for_display(OWNER_ROLE_ID, []) == sorted(p.value for p in Permission)
    assert role_permissions_for_display(NO_ACCESS_ROLE_ID, []) == []
    assert role_permissions_for_display(uuid.uuid4(), ["reports.read", "gone.away"]) == ["reports.read"]


def test_a_change_permission_includes_the_matching_read():
    assert effective({Permission.CATALOGUE_WRITE}) == {Permission.CATALOGUE_WRITE, Permission.CATALOGUE_READ}
    assert Permission.PAYMENTS_READ in effective({Permission.PAYMENTS_LIVE})
    assert effective({Permission.REPORTS_READ}) == {Permission.REPORTS_READ}
