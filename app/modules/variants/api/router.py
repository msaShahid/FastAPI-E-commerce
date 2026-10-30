"""Administrator endpoints for options, variants, stock adjustments and converting a product to variants. All admin-only; every one that
changes a product answers with the updated product detail (options and variants included), so a screen can simply redraw."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.database import DbSession
from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.auth.dependencies.auth import AdminUser
from app.modules.products.api.router import _to_product_read
from app.modules.products.dependencies.product_deps import ProductServiceDep
from app.modules.products.repositories.product_repository import ProductRepository
from app.modules.products.schemas.product import ProductRead
from app.modules.variants.schemas.variants import (
    ConversionResult,
    ConvertRequest,
    MissingCombinations,
    OptionsSet,
    StockAdjust,
    StockAdjustmentResult,
    StockMovementRead,
    ValueAdd,
    VariantPatch,
    VariantsBulk,
    VariantSpec,
)
from app.modules.variants.services.variant_admin_service import VariantAdminService
from app.shared.pagination.schemas import PageParams, PaginatedResponse

variants_router = APIRouter(prefix="/products", tags=["variants"])


def get_variant_admin_service(db: DbSession, audit: AuditServiceDep) -> VariantAdminService:
    return VariantAdminService(db, ProductRepository(db), audit)


AdminServiceDep = Annotated[VariantAdminService, Depends(get_variant_admin_service)]


async def _detail(product_id: int, products: ProductServiceDep) -> ProductRead:
    product = await products.get_product(product_id, is_admin=True)
    await products.attach_variant_views([product], with_options=True)
    return _to_product_read(product, products, include_tax=True, detail=True)


@variants_router.put("/{product_id}/options", response_model=ProductRead)
async def set_options(product_id: int, payload: OptionsSet, admin: AdminUser, service: AdminServiceDep, products: ProductServiceDep):
    """Define a product's option sets (up to 3, e.g. Size and Colour, each with its values). Only for a product with no variants, no stock
    and no carts or orders; otherwise use convert-to-variants. An empty list makes it a simple product again."""
    await service.set_options(product_id, payload, admin)
    return await _detail(product_id, products)


@variants_router.post("/{product_id}/options/{option_id}/values", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
async def add_option_value(product_id: int, option_id: int, payload: ValueAdd, admin: AdminUser, service: AdminServiceDep, products: ProductServiceDep):
    await service.add_value(product_id, option_id, payload.value, admin)
    return await _detail(product_id, products)


@variants_router.patch("/{product_id}/option-values/{value_id}", response_model=ProductRead)
async def rename_option_value(product_id: int, value_id: int, payload: ValueAdd, admin: AdminUser, service: AdminServiceDep, products: ProductServiceDep):
    """Rename a value. Orders already placed keep the name they were bought under."""
    await service.rename_value(product_id, value_id, payload.value, admin)
    return await _detail(product_id, products)


@variants_router.delete("/{product_id}/option-values/{value_id}", response_model=ProductRead)
async def delete_option_value(product_id: int, value_id: int, admin: AdminUser, service: AdminServiceDep, products: ProductServiceDep):
    await service.delete_value(product_id, value_id, admin)
    return await _detail(product_id, products)


@variants_router.get("/{product_id}/variants/missing-combinations", response_model=MissingCombinations)
async def missing_combinations(product_id: int, _admin: AdminUser, service: AdminServiceDep):
    """The combinations of the option values that have no variant yet. A convenience for choosing what to create: nothing is created."""
    return await service.missing_combinations(product_id)


@variants_router.post("/{product_id}/variants/bulk", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
async def create_variants(product_id: int, payload: VariantsBulk, admin: AdminUser, service: AdminServiceDep, products: ProductServiceDep):
    """Up to 100 variants, all or none. Each names a value for every option, its own SKU and stock, and optionally its own price."""
    await service.create_variants(product_id, payload.variants, admin)
    return await _detail(product_id, products)


@variants_router.post("/{product_id}/variants", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
async def create_variant(product_id: int, payload: VariantSpec, admin: AdminUser, service: AdminServiceDep, products: ProductServiceDep):
    await service.create_variants(product_id, [payload], admin)
    return await _detail(product_id, products)


@variants_router.patch("/{product_id}/variants/{variant_id}", response_model=ProductRead)
async def update_variant(product_id: int, variant_id: int, payload: VariantPatch, admin: AdminUser, service: AdminServiceDep, products: ProductServiceDep):
    """Change the price override (null removes it) or the status. SKU, option values and stock cannot be changed here."""
    await service.update_variant(product_id, variant_id, payload, admin)
    return await _detail(product_id, products)


@variants_router.delete("/{product_id}/variants/{variant_id}", response_model=ProductRead)
async def delete_variant(product_id: int, variant_id: int, admin: AdminUser, service: AdminServiceDep, products: ProductServiceDep):
    """Only a variant that never had stock and is in no cart or order. Otherwise set it to inactive."""
    await service.delete_variant(product_id, variant_id, admin)
    return await _detail(product_id, products)


@variants_router.post("/{product_id}/variants/{variant_id}/stock", response_model=StockAdjustmentResult)
async def adjust_variant_stock(product_id: int, variant_id: int, payload: StockAdjust, admin: AdminUser, service: AdminServiceDep):
    """Set a variant's stock (`set_to`) or add/remove units (`delta`). A reason is required; the change is recorded with who made it."""
    return await service.adjust_stock(product_id, variant_id, payload, admin)


@variants_router.post("/{product_id}/stock", response_model=StockAdjustmentResult)
async def adjust_product_stock(product_id: int, payload: StockAdjust, admin: AdminUser, service: AdminServiceDep):
    """The same for a simple product. A product with options keeps its stock on its variants."""
    return await service.adjust_stock(product_id, None, payload, admin)


@variants_router.get("/{product_id}/stock-history", response_model=PaginatedResponse[StockMovementRead])
async def stock_history(
    product_id: int, _admin: AdminUser, service: AdminServiceDep, params: PageParams = Depends(),
    variant_id: Annotated[int | None, Query(description="Only this variant's movements")] = None,
):
    """Every stock change of the product (and its variants), newest first: sales, restocks, administrators' changes, with before and after."""
    total, rows = await service.stock_history(product_id, variant_id, params.offset, params.page_size)
    return PaginatedResponse[StockMovementRead](items=rows, total=total, page=params.page, page_size=params.page_size)


@variants_router.post("/{product_id}/convert-to-variants", response_model=ConversionResult)
async def convert_to_variants(product_id: int, payload: ConvertRequest, admin: AdminUser, service: AdminServiceDep, products: ProductServiceDep):
    """Turn a simple product into a product with options, moving its stock onto the variants EXACTLY (allocated stock must equal the
    product's current stock, and `expected_stock` must still match). Refused while an unpaid or paid-but-unshipped order contains it.
    Its cart lines are removed. All or nothing."""
    _product, removed = await service.convert_to_variants(product_id, payload, admin)
    return ConversionResult(product=await _detail(product_id, products), cart_lines_removed=removed)
