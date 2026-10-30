"""Administering options and variants, and converting a simple product into one with options.

Rules this service adds on top of the database's own (variant_guard.py): nothing is generated for the administrator (every variant is
created on purpose); the option structure can only be redefined while there are no variants; stock changes always carry a reason and
go through the ledger; a product with stock, carts or orders can only become a product with options through the conversion operation,
which moves its stock exactly and refuses while an order could still be restocked.

Concurrency: every operation first takes the PRODUCT row (`SELECT ... FOR UPDATE`), the same lock checkout, restock and the database's
own variant triggers use, so an operation on a product is serialised with sales of it. Deferred database rules are checked at the end of
each operation (SET CONSTRAINTS ALL IMMEDIATE) so a violation is reported as a clear error and rolls the whole request back."""


from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.stock_context import stock_change
from app.modules.audit.services.audit_service import AuditService
from app.modules.auth.models.user import User
from app.modules.cart.models.cart_item import CartItem
from app.modules.orders.models.order import Order
from app.modules.orders.models.order_item import OrderItem
from app.modules.products.exceptions.product_exceptions import ProductNotFoundError
from app.modules.products.models.product import Product
from app.modules.products.models.stock_movement import StockMovement
from app.modules.products.models.variants import (
    ProductOption,
    ProductOptionValue,
    ProductVariant,
    SkuRegistry,
)
from app.modules.products.repositories.product_repository import ProductRepository
from app.modules.variants.exceptions.variant_exceptions import (
    AlreadyHasVariantsError,
    InvalidVariantSpecError,
    OptionNotFoundError,
    OptionsLockedError,
    OptionValueInUseError,
    ProductHasOpenOrdersError,
    ProductHasStockError,
    ProductInUseError,
    ProductNeedsActiveVariantError,
    ProductNotConvertibleError,
    SkuInUseError,
    StockAllocationMismatchError,
    StockChangedError,
    StockWouldBeNegativeError,
    UseVariantStockError,
    VariantCombinationExistsError,
    VariantInUseError,
    VariantNotFoundError,
)
from app.modules.variants.schemas.variants import (
    ConvertRequest,
    MissingCombinations,
    OptionSpec,
    OptionsSet,
    StockAdjust,
    StockAdjustmentResult,
    VariantPatch,
    VariantSpec,
)
from app.shared.enums.order_status import OrderStatus
from app.shared.enums.product_status import ProductStatus

MAX_MISSING_LISTED = 500


def translate_integrity_error(exc: IntegrityError) -> Exception:
    """A clear error for the database rules an administrator can run into; anything else is returned unchanged."""
    message = str(getattr(exc.orig, "args", [""])[0] or exc.orig)
    constraint = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None) or ""
    if "needs at least one active variant" in message:
        return ProductNeedsActiveVariantError()
    if "uq_variants_combination" in constraint or "uq_variants_combination" in message:
        return VariantCombinationExistsError()
    if "sku_registry" in constraint or "sku_registry" in message:
        return SkuInUseError(["(see request)"])
    if "ux_option_values_option_lvalue" in constraint or "ux_option_values_option_lvalue" in message:
        return InvalidVariantSpecError("that option already has a value with that name (ignoring letter case)")
    if "ux_product_options_product_lname" in constraint or "ux_product_options_product_lname" in message:
        return InvalidVariantSpecError("the product already has an option with that name (ignoring letter case)")
    if "declares" in message and "option(s)" in message:
        return InvalidVariantSpecError("the product's options are inconsistent: " + message)
    if "violates foreign key constraint" in message and ("stock_movements" in message or "cart_items" in message or "order_items" in message):
        return VariantInUseError()
    return exc


class VariantAdminService:
    def __init__(self, db: AsyncSession, products: ProductRepository, audit: AuditService) -> None:
        self.db = db
        self.products = products
        self.audit = audit

    # ------------------------------------------------------------------------------------------------ plumbing
    async def _locked_product(self, product_id: int) -> Product:
        product = await self.products.get_by_id_for_update(product_id)
        if product is None:
            raise ProductNotFoundError(product_id)
        return product

    async def _flush(self) -> None:
        try:
            await self.db.flush()
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc

    async def _check_now(self) -> None:
        """Runs the database's deferred rules now, so a violation becomes a clear error and the request rolls back as a whole."""
        try:
            await self.db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc

    async def _count(self, model, *conditions) -> int:
        return (await self.db.execute(select(func.count()).select_from(model).where(*conditions))).scalar_one()

    async def _references(self, product_id: int) -> bool:
        return bool(await self._count(CartItem, CartItem.product_id == product_id) or await self._count(OrderItem, OrderItem.product_id == product_id))

    async def _log(self, admin: User, action: str, product_id: int, **details) -> None:
        await self.audit.log(actor_user_id=admin.id, action=action, target_type="product", target_id=product_id, details=details)

    # ------------------------------------------------------------------------------------------------ option structure
    async def _install_options(self, product: Product, options: list[OptionSpec]) -> None:
        await self.db.execute(delete(ProductOption).where(ProductOption.product_id == product.id))   # its values go with it
        rows = []
        for position, spec in enumerate(options, start=1):
            option = ProductOption(product_id=product.id, position=position, name=spec.name)
            self.db.add(option)
            rows.append((option, spec))
        await self._flush()
        for option, spec in rows:
            for value in spec.values:
                self.db.add(ProductOptionValue(option_id=option.id, product_id=product.id, option_position=option.position, value=value))
        product.option_count = len(options)
        await self._flush()

    async def set_options(self, product_id: int, spec: OptionsSet, admin: User) -> Product:
        product = await self._locked_product(product_id)
        if await self.products.variants_for([product.id]):
            raise OptionsLockedError()
        if spec.options:
            if product.stock > 0:
                raise ProductHasStockError()
            if await self._references(product.id):
                raise ProductInUseError()
        await self._install_options(product, spec.options)
        await self._check_now()
        await self._log(admin, "product.options_set", product.id, options=[{"name": o.name, "values": o.values} for o in spec.options])
        return product

    async def _option_for(self, product_id: int, option_id: int) -> ProductOption:
        option = (await self.db.execute(select(ProductOption).where(ProductOption.id == option_id, ProductOption.product_id == product_id))).scalar_one_or_none()
        if option is None:
            raise OptionNotFoundError()
        return option

    async def _value_for(self, product_id: int, value_id: int) -> ProductOptionValue:
        value = (await self.db.execute(select(ProductOptionValue).where(ProductOptionValue.id == value_id, ProductOptionValue.product_id == product_id))).scalar_one_or_none()
        if value is None:
            raise OptionNotFoundError()
        return value

    async def add_value(self, product_id: int, option_id: int, value: str, admin: User) -> Product:
        product = await self._locked_product(product_id)
        option = await self._option_for(product_id, option_id)
        if await self._count(ProductOptionValue, ProductOptionValue.option_id == option.id) >= 50:
            raise InvalidVariantSpecError("an option has at most 50 values")
        self.db.add(ProductOptionValue(option_id=option.id, product_id=product_id, option_position=option.position, value=value))
        await self._flush()
        await self._log(admin, "product.option_value_added", product_id, option=option.name, value=value)
        return product

    async def rename_value(self, product_id: int, value_id: int, new_value: str, admin: User) -> Product:
        product = await self._locked_product(product_id)
        value = await self._value_for(product_id, value_id)
        old = value.value
        value.value = new_value
        await self._flush()
        await self._log(admin, "product.option_value_renamed", product_id, old=old, new=new_value)   # past orders keep their own snapshot
        return product

    async def delete_value(self, product_id: int, value_id: int, admin: User) -> Product:
        product = await self._locked_product(product_id)
        value = await self._value_for(product_id, value_id)
        if await self._count(ProductVariant, (ProductVariant.value1_id == value_id) | (ProductVariant.value2_id == value_id) | (ProductVariant.value3_id == value_id)):
            raise OptionValueInUseError()
        if await self._count(ProductOptionValue, ProductOptionValue.option_id == value.option_id) <= 1:
            raise InvalidVariantSpecError("an option needs at least one value")
        await self.db.delete(value)
        await self._flush()
        await self._log(admin, "product.option_value_deleted", product_id, value=value.value)
        return product

    # ------------------------------------------------------------------------------------------------ variants
    async def _create_variants(self, product: Product, specs: list[VariantSpec]) -> list[ProductVariant]:
        """Validates every spec against the product's options, then inserts them all (or, on any problem, none)."""
        options = await self.products.options_for([product.id])
        if not options or product.option_count != len(options):
            raise InvalidVariantSpecError("This product has no options yet: define them first (PUT /products/{id}/options)")
        by_name = {o.name.lower(): o for o in options}
        values = {o.name.lower(): {v.value.lower(): v.id for v in o.values} for o in options}
        existing_combos = {tuple(r) for r in (await self.db.execute(
            select(ProductVariant.value1_id, ProductVariant.value2_id, ProductVariant.value3_id).where(ProductVariant.product_id == product.id))).all()}
        taken_skus = set((await self.db.execute(select(SkuRegistry.sku_norm).where(SkuRegistry.sku_norm.in_([s.sku.upper() for s in specs])))).scalars().all())

        invalid: list[str] = []
        combo_conflicts: list[str] = []
        sku_conflicts: list[str] = []
        seen_combos: set[tuple] = set()
        seen_skus: set[str] = set()
        built: list[tuple[VariantSpec, tuple]] = []
        for index, spec in enumerate(specs, start=1):
            label = f"variant {index} ({spec.sku})"
            given = {name.lower(): value for name, value in spec.option_values.items()}
            if set(given) != set(by_name):
                invalid.append(f"{label}: needs exactly one value for each of {', '.join(o.name for o in options)}")
                continue
            ids, problem = [], False
            for option in options:
                value_id = values[option.name.lower()].get(given[option.name.lower()].lower())
                if value_id is None:
                    invalid.append(f"{label}: '{given[option.name.lower()]}' is not a value of {option.name}")
                    problem = True
                    break
                ids.append(value_id)
            if problem:
                continue
            combo = tuple(ids + [None] * (3 - len(ids)))
            if spec.sku.upper() in seen_skus:
                invalid.append(f"{label}: the SKU appears twice in this request")
            elif combo in seen_combos:
                invalid.append(f"{label}: the same combination appears twice in this request")
            elif combo in existing_combos:
                combo_conflicts.append(label)
            elif spec.sku.upper() in taken_skus:
                sku_conflicts.append(spec.sku)
            seen_skus.add(spec.sku.upper())
            seen_combos.add(combo)
            built.append((spec, combo))
        if invalid:
            raise InvalidVariantSpecError("; ".join(invalid[:10]) + (f" (and {len(invalid) - 10} more)" if len(invalid) > 10 else ""))
        if combo_conflicts:
            raise VariantCombinationExistsError("These combinations already exist: " + ", ".join(combo_conflicts))
        if sku_conflicts:
            raise SkuInUseError(sku_conflicts)

        created = []
        for spec, combo in built:
            variant = ProductVariant(
                product_id=product.id, option_count=product.option_count, value1_id=combo[0], value2_id=combo[1], value3_id=combo[2],
                sku=spec.sku, price_override_cents=spec.price_override_cents, stock=spec.stock, status=spec.status,
            )
            self.db.add(variant)
            created.append(variant)
        await self._flush()
        return created

    async def create_variants(self, product_id: int, specs: list[VariantSpec], admin: User) -> Product:
        product = await self._locked_product(product_id)
        async with stock_change(self.db, "initial", actor_user_id=admin.id, reason="variant created"):
            created = await self._create_variants(product, specs)
        await self._check_now()
        await self._log(admin, "variant.created", product.id, count=len(created), skus=[v.sku for v in created])
        return product

    async def _variant_for(self, product_id: int, variant_id: int, *, lock: bool = False) -> ProductVariant:
        query = select(ProductVariant).where(ProductVariant.id == variant_id, ProductVariant.product_id == product_id)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        variant = (await self.db.execute(query)).scalar_one_or_none()
        if variant is None:
            raise VariantNotFoundError()
        return variant

    async def update_variant(self, product_id: int, variant_id: int, patch: VariantPatch, admin: User) -> Product:
        product = await self._locked_product(product_id)
        variant = await self._variant_for(product_id, variant_id, lock=True)
        sent = patch.model_fields_set
        if "price_override_cents" in sent:
            variant.price_override_cents = patch.price_override_cents
        if "status" in sent and patch.status is not None:
            variant.status = patch.status
        await self._flush()
        await self._check_now()
        await self._log(admin, "variant.updated", product.id, variant_id=variant.id, fields=sorted(sent))
        return product

    async def delete_variant(self, product_id: int, variant_id: int, admin: User) -> Product:
        product = await self._locked_product(product_id)
        variant = await self._variant_for(product_id, variant_id, lock=True)
        if (await self._count(CartItem, CartItem.variant_id == variant.id) or await self._count(OrderItem, OrderItem.variant_id == variant.id)
                or await self._count(StockMovement, StockMovement.variant_id == variant.id)):
            raise VariantInUseError()
        sku = variant.sku
        await self.db.delete(variant)
        await self._flush()
        await self._check_now()
        await self._log(admin, "variant.deleted", product.id, variant_id=variant_id, sku=sku)
        return product

    async def missing_combinations(self, product_id: int) -> MissingCombinations:
        product = await self.products.get_by_id(product_id)
        if product is None:
            raise ProductNotFoundError(product_id)
        options = await self.products.options_for([product_id])
        if not options:
            raise InvalidVariantSpecError("This product has no options")
        existing = {tuple(r) for r in (await self.db.execute(
            select(ProductVariant.value1_id, ProductVariant.value2_id, ProductVariant.value3_id).where(ProductVariant.product_id == product_id))).all()}
        items, total = [], 0

        def walk(level: int, chosen: list):
            nonlocal total
            if level == len(options):
                if tuple(c[0] for c in chosen) + (None,) * (3 - len(chosen)) not in existing:
                    total += 1
                    if len(items) < MAX_MISSING_LISTED:
                        items.append({options[i].name: chosen[i][1] for i in range(len(options))})
                return
            for value in options[level].values:
                walk(level + 1, [*chosen, (value.id, value.value)])

        walk(0, [])
        return MissingCombinations(total=total, truncated=total > len(items), items=items)

    # ------------------------------------------------------------------------------------------------ stock
    async def adjust_stock(self, product_id: int, variant_id: int | None, spec: StockAdjust, admin: User) -> StockAdjustmentResult:
        product = await self._locked_product(product_id)
        if variant_id is None:
            if (product.option_count or 0) > 0:
                raise UseVariantStockError()
            unit = product
        else:
            unit = await self._variant_for(product_id, variant_id, lock=True)
        before = unit.stock
        after = spec.set_to if spec.set_to is not None else before + spec.delta
        if after < 0:
            raise StockWouldBeNegativeError(before)
        if after == before:
            return StockAdjustmentResult(product_id=product_id, variant_id=variant_id, before_qty=before, after_qty=after, delta=0, changed=False)
        kind = "admin_set" if spec.set_to is not None else "admin_delta"
        async with stock_change(self.db, kind, actor_user_id=admin.id, reason=spec.reason):
            unit.stock = after
            await self._flush()
        await self._log(admin, "stock.adjusted", product_id, variant_id=variant_id, before=before, after=after, reason=spec.reason, kind=kind)
        return StockAdjustmentResult(product_id=product_id, variant_id=variant_id, before_qty=before, after_qty=after, delta=after - before, changed=True)

    async def stock_history(self, product_id: int, variant_id: int | None, offset: int, limit: int):
        if await self.products.get_by_id(product_id) is None:
            raise ProductNotFoundError(product_id)
        conditions = [StockMovement.product_id == product_id]
        if variant_id is not None:
            conditions.append(StockMovement.variant_id == variant_id)
        total = await self._count(StockMovement, *conditions)
        rows = (await self.db.execute(select(StockMovement).where(*conditions).order_by(StockMovement.id.desc()).offset(offset).limit(limit))).scalars().all()
        return total, list(rows)

    # ------------------------------------------------------------------------------------------------ conversion
    async def convert_to_variants(self, product_id: int, request: ConvertRequest, admin: User) -> tuple[Product, int]:
        """One transaction: lock the product (blocking checkouts and cart adds for it), verify, move the stock, create options and variants.
        Any failure rolls back everything, including the cart lines removed."""
        product = await self._locked_product(product_id)
        if (product.option_count or 0) > 0:
            raise AlreadyHasVariantsError()
        if product.status == ProductStatus.ARCHIVED:
            raise ProductNotConvertibleError("An archived product cannot be converted")
        if request.expected_stock != product.stock:
            raise StockChangedError(product.stock)
        allocated = sum(v.stock for v in request.variants)
        if allocated != product.stock:
            raise StockAllocationMismatchError(allocated, product.stock)
        open_orders = (await self.db.execute(
            select(func.count(func.distinct(Order.id))).select_from(OrderItem).join(Order, Order.id == OrderItem.order_id)
            .where(OrderItem.product_id == product.id, Order.status.in_([OrderStatus.PENDING, OrderStatus.PAID]))
        )).scalar_one()
        if open_orders:
            raise ProductHasOpenOrdersError(open_orders)

        removed = (await self.db.execute(delete(CartItem).where(CartItem.product_id == product.id))).rowcount or 0   # carts are not records
        old_stock = product.stock
        async with stock_change(self.db, "conversion", actor_user_id=admin.id, reason=request.reason):
            if old_stock:
                product.stock = 0
                await self._flush()
            await self._install_options(product, request.options)
            created = await self._create_variants(product, request.variants)
        await self._check_now()
        await self._log(admin, "product.converted_to_variants", product.id, stock_moved=old_stock, variants=len(created),
                        cart_lines_removed=removed, reason=request.reason)
        return product, removed
