"""The fixed list of staff permissions (BE-P1). Code defines the permissions; administrators combine them into roles.

Rules:
- Only an active administrator (``users.role = admin``) can hold permissions. Customers hold none, whatever is stored.
- An administrator with no role assigned is a legacy administrator and keeps every permission, exactly as before roles
  existed. The migration assigns every existing administrator the Owner role explicitly.
- The Owner role is a system role: it always holds every permission (including ones added later) and cannot be edited
  or deleted. The "No access" system role holds nothing: someone promoted to administrator through the API gets it, so
  access is always granted deliberately by assigning a real role.
- A permission that changes something includes the matching read (IMPLIES), so a role can never edit what it cannot see.
"""

import enum
import uuid


class Permission(enum.StrEnum):
    REPORTS_READ = "reports.read"
    AUDIT_READ = "audit.read"
    CATALOGUE_READ = "catalogue.read"
    CATALOGUE_WRITE = "catalogue.write"
    INVENTORY_ADJUST = "inventory.adjust"
    REVIEWS_MODERATE = "reviews.moderate"
    CUSTOMERS_READ = "customers.read"
    CUSTOMERS_MANAGE = "customers.manage"
    ORDERS_READ = "orders.read"
    ORDERS_MANAGE = "orders.manage"
    RETURNS_READ = "returns.read"
    RETURNS_MANAGE = "returns.manage"
    REFUNDS_ISSUE = "refunds.issue"
    PAYMENTS_READ = "payments.read"
    PAYMENTS_MANAGE = "payments.manage"
    PAYMENTS_CREDENTIALS = "payments.credentials"
    PAYMENTS_LIVE = "payments.live"
    TAX_READ = "tax.read"
    TAX_CONFIGURE = "tax.configure"
    STORE_CONFIGURE = "store.configure"
    COUPONS_MANAGE = "coupons.manage"
    EMAIL_CONFIGURE = "email.configure"
    CONTENT_EDIT = "content.edit"
    CONTENT_PUBLISH = "content.publish"
    ROLES_MANAGE = "roles.manage"

    @property
    def group(self) -> str:
        return self.value.split(".", 1)[0]


ALL_PERMISSIONS: frozenset[Permission] = frozenset(Permission)

# Fixed ids so the migration, the service and tests agree on which rows are the system roles.
OWNER_ROLE_ID = uuid.UUID("6f0c1e2a-0000-4000-8000-000000000001")
OWNER_ROLE_NAME = "Owner"
NO_ACCESS_ROLE_ID = uuid.UUID("6f0c1e2a-0000-4000-8000-000000000002")
NO_ACCESS_ROLE_NAME = "No access"

IMPLIES: dict[Permission, frozenset[Permission]] = {
    Permission.CATALOGUE_WRITE: frozenset({Permission.CATALOGUE_READ}),
    Permission.INVENTORY_ADJUST: frozenset({Permission.CATALOGUE_READ}),
    Permission.CUSTOMERS_MANAGE: frozenset({Permission.CUSTOMERS_READ}),
    Permission.ORDERS_MANAGE: frozenset({Permission.ORDERS_READ}),
    Permission.RETURNS_MANAGE: frozenset({Permission.RETURNS_READ, Permission.ORDERS_READ}),
    Permission.REFUNDS_ISSUE: frozenset({Permission.ORDERS_READ, Permission.RETURNS_READ, Permission.PAYMENTS_READ}),
    Permission.PAYMENTS_MANAGE: frozenset({Permission.PAYMENTS_READ}),
    Permission.PAYMENTS_CREDENTIALS: frozenset({Permission.PAYMENTS_READ}),
    Permission.PAYMENTS_LIVE: frozenset({Permission.PAYMENTS_READ}),
    Permission.TAX_CONFIGURE: frozenset({Permission.TAX_READ}),
    Permission.CONTENT_PUBLISH: frozenset({Permission.CONTENT_EDIT}),
}


def effective(granted: set[Permission] | frozenset[Permission]) -> frozenset[Permission]:
    """The granted permissions plus everything they include."""
    out = set(granted)
    for p in granted:
        out |= IMPLIES.get(p, frozenset())
    return frozenset(out)


def role_permissions_for_display(role_id: uuid.UUID, stored: list[str]) -> list[str]:
    """The permissions a role was given, as shown to administrators: every permission for Owner, else the stored known ones."""
    if role_id == OWNER_ROLE_ID:
        return sorted(p.value for p in Permission)
    known = {p.value for p in Permission}
    return sorted(p for p in stored if p in known)
