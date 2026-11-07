"""Permission checks for staff endpoints (BE-P1).

Each staff endpoint declares ONE permission by taking its staff user through one of the aliases below, e.g.
``admin: OrdersManage``. The check runs after ``require_admin`` (an active administrator), so a customer still gets the
same 403 as before, and an administrator whose role lacks the permission gets 403 ``permission_denied`` with the missing
permission in ``permission``. tests/roles/test_permission_coverage.py fails if an administrator-only route has none.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends

from app.core.database import DbSession
from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.auth.dependencies.auth import AdminUser
from app.modules.auth.models.user import User
from app.modules.roles.permissions import Permission
from app.modules.roles.repositories.role_repository import RoleRepository
from app.modules.roles.services.access_service import AccessService
from app.modules.roles.services.role_service import RoleService


def get_access_service(db: DbSession) -> AccessService:
    return AccessService(RoleRepository(db))


AccessServiceDep = Annotated[AccessService, Depends(get_access_service)]


def get_role_service(db: DbSession, access: AccessServiceDep, audit: AuditServiceDep) -> RoleService:
    return RoleService(RoleRepository(db), access, audit)


RoleServiceDep = Annotated[RoleService, Depends(get_role_service)]


def require_permission(permission: Permission) -> Callable[..., Awaitable[User]]:
    async def dependency(admin: AdminUser, access: AccessServiceDep) -> User:
        await access.require(admin, permission)
        return admin

    dependency.permission = permission  # type: ignore[attr-defined]  # read by the coverage test
    dependency.__name__ = f"require_{permission.value.replace('.', '_')}"
    return dependency


ReportsRead = Annotated[User, Depends(require_permission(Permission.REPORTS_READ))]
AuditRead = Annotated[User, Depends(require_permission(Permission.AUDIT_READ))]
CatalogueRead = Annotated[User, Depends(require_permission(Permission.CATALOGUE_READ))]
CatalogueWrite = Annotated[User, Depends(require_permission(Permission.CATALOGUE_WRITE))]
InventoryAdjust = Annotated[User, Depends(require_permission(Permission.INVENTORY_ADJUST))]
ReviewsModerate = Annotated[User, Depends(require_permission(Permission.REVIEWS_MODERATE))]
CustomersRead = Annotated[User, Depends(require_permission(Permission.CUSTOMERS_READ))]
CustomersManage = Annotated[User, Depends(require_permission(Permission.CUSTOMERS_MANAGE))]
OrdersRead = Annotated[User, Depends(require_permission(Permission.ORDERS_READ))]
OrdersManage = Annotated[User, Depends(require_permission(Permission.ORDERS_MANAGE))]
ReturnsRead = Annotated[User, Depends(require_permission(Permission.RETURNS_READ))]
ReturnsManage = Annotated[User, Depends(require_permission(Permission.RETURNS_MANAGE))]
RefundsIssue = Annotated[User, Depends(require_permission(Permission.REFUNDS_ISSUE))]
PaymentsRead = Annotated[User, Depends(require_permission(Permission.PAYMENTS_READ))]
PaymentsManage = Annotated[User, Depends(require_permission(Permission.PAYMENTS_MANAGE))]
PaymentsCredentials = Annotated[User, Depends(require_permission(Permission.PAYMENTS_CREDENTIALS))]
PaymentsLive = Annotated[User, Depends(require_permission(Permission.PAYMENTS_LIVE))]
TaxRead = Annotated[User, Depends(require_permission(Permission.TAX_READ))]
TaxConfigure = Annotated[User, Depends(require_permission(Permission.TAX_CONFIGURE))]
StoreConfigure = Annotated[User, Depends(require_permission(Permission.STORE_CONFIGURE))]
CouponsManage = Annotated[User, Depends(require_permission(Permission.COUPONS_MANAGE))]
EmailConfigure = Annotated[User, Depends(require_permission(Permission.EMAIL_CONFIGURE))]
ContentEdit = Annotated[User, Depends(require_permission(Permission.CONTENT_EDIT))]
ContentPublish = Annotated[User, Depends(require_permission(Permission.CONTENT_PUBLISH))]
RolesManage = Annotated[User, Depends(require_permission(Permission.ROLES_MANAGE))]
