from fastapi import APIRouter, Depends, Query

from app.modules.auth.dependencies.auth import AdminUser
from app.modules.tax.dependencies.tax_deps import TaxServiceDep
from app.modules.tax.schemas.tax import (
    ApproveRequest,
    ClassificationCreate,
    ClassificationRead,
    ClassificationUpdate,
    RegistrationCreate,
    RegistrationRead,
    RegistrationUpdate,
    RuleClose,
    RuleCreate,
    RuleRead,
    RuleUpdate,
)
from app.shared.enums.tax import ClassificationKind, TaxRecordStatus
from app.shared.pagination.schemas import PageParams, PaginatedResponse

# Administrator-only. Records start as drafts; nothing here is used by checkout yet.
tax_router = APIRouter(prefix="/admin/tax", tags=["tax-master"])


def _page(items, total, params: PageParams, schema):
    return PaginatedResponse(
        items=[schema.model_validate(i) for i in items],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


# ------------------------------------------------------------------ classifications
@tax_router.post("/classifications", response_model=ClassificationRead, status_code=201)
async def create_classification(
    payload: ClassificationCreate, admin: AdminUser, service: TaxServiceDep
):
    return await service.create_classification(admin, payload)


@tax_router.get("/classifications", response_model=PaginatedResponse[ClassificationRead])
async def list_classifications(
    _admin: AdminUser,
    service: TaxServiceDep,
    params: PageParams = Depends(),
    status: TaxRecordStatus | None = None,
    kind: ClassificationKind | None = None,
    q: str | None = Query(default=None, max_length=100, description="Search code or description"),
):
    items, total = await service.list_classifications(
        offset=params.offset, limit=params.page_size, status=status, kind=kind, q=q
    )
    return _page(items, total, params, ClassificationRead)


@tax_router.patch("/classifications/{record_id}", response_model=ClassificationRead)
async def update_classification(
    record_id: int, payload: ClassificationUpdate, admin: AdminUser, service: TaxServiceDep
):
    return await service.update_classification(admin, record_id, payload)


@tax_router.post("/classifications/{record_id}/approve", response_model=ClassificationRead)
async def approve_classification(
    record_id: int, admin: AdminUser, service: TaxServiceDep, payload: ApproveRequest | None = None
):
    return await service.approve_classification(admin, record_id, payload.note if payload else None)


@tax_router.post("/classifications/{record_id}/retire", response_model=ClassificationRead)
async def retire_classification(record_id: int, admin: AdminUser, service: TaxServiceDep):
    return await service.retire_classification(admin, record_id)


# ------------------------------------------------------------------ rules
@tax_router.post("/rules", response_model=RuleRead, status_code=201)
async def create_rule(payload: RuleCreate, admin: AdminUser, service: TaxServiceDep):
    return await service.create_rule(admin, payload)


@tax_router.get("/rules", response_model=PaginatedResponse[RuleRead])
async def list_rules(
    _admin: AdminUser,
    service: TaxServiceDep,
    params: PageParams = Depends(),
    status: TaxRecordStatus | None = None,
    classification_id: int | None = None,
):
    items, total = await service.list_rules(
        offset=params.offset, limit=params.page_size, status=status, classification_id=classification_id
    )
    return _page(items, total, params, RuleRead)


@tax_router.patch("/rules/{record_id}", response_model=RuleRead)
async def update_rule(record_id: int, payload: RuleUpdate, admin: AdminUser, service: TaxServiceDep):
    return await service.update_rule(admin, record_id, payload)


@tax_router.post("/rules/{record_id}/approve", response_model=RuleRead)
async def approve_rule(
    record_id: int, admin: AdminUser, service: TaxServiceDep, payload: ApproveRequest | None = None
):
    return await service.approve_rule(admin, record_id, payload.note if payload else None)


@tax_router.post("/rules/{record_id}/close", response_model=RuleRead)
async def close_rule(record_id: int, payload: RuleClose, admin: AdminUser, service: TaxServiceDep):
    return await service.close_rule(admin, record_id, payload.effective_to)


@tax_router.post("/rules/{record_id}/retire", response_model=RuleRead)
async def retire_rule(record_id: int, admin: AdminUser, service: TaxServiceDep):
    return await service.retire_rule(admin, record_id)


# ------------------------------------------------------------------ business GST registrations
@tax_router.post("/registrations", response_model=RegistrationRead, status_code=201)
async def create_registration(payload: RegistrationCreate, admin: AdminUser, service: TaxServiceDep):
    return await service.create_registration(admin, payload)


@tax_router.get("/registrations", response_model=PaginatedResponse[RegistrationRead])
async def list_registrations(
    _admin: AdminUser,
    service: TaxServiceDep,
    params: PageParams = Depends(),
    status: TaxRecordStatus | None = None,
):
    items, total = await service.list_registrations(
        offset=params.offset, limit=params.page_size, status=status
    )
    return _page(items, total, params, RegistrationRead)


@tax_router.patch("/registrations/{record_id}", response_model=RegistrationRead)
async def update_registration(
    record_id: int, payload: RegistrationUpdate, admin: AdminUser, service: TaxServiceDep
):
    return await service.update_registration(admin, record_id, payload)


@tax_router.post("/registrations/{record_id}/approve", response_model=RegistrationRead)
async def approve_registration(
    record_id: int, admin: AdminUser, service: TaxServiceDep, payload: ApproveRequest | None = None
):
    return await service.approve_registration(admin, record_id, payload.note if payload else None)


@tax_router.post("/registrations/{record_id}/retire", response_model=RegistrationRead)
async def retire_registration(record_id: int, admin: AdminUser, service: TaxServiceDep):
    return await service.retire_registration(admin, record_id)
