from uuid import uuid4

from fastapi import UploadFile

from app.core.config import get_settings
from app.core.storage.interface import StorageService
from app.modules.categories.repositories.category_repository import CategoryRepository
from app.modules.products.exceptions.product_exceptions import (
    ClassificationNotApprovedError,
    InvalidCategoryError,
    InvalidTaxClassificationError,
    ProductNotFoundError,
    ProductTaxRequiredError,
    SkuAlreadyExistsError,
)
from app.modules.products.models.product import Product
from app.modules.products.repositories.product_repository import ProductRepository
from app.modules.tax.models.tax import TaxClassification
from app.modules.tax.repositories.tax_repository import TaxRepository
from app.shared.enums.product_status import ProductStatus
from app.shared.utils.image_validation import get_image_extension, validate_image
from app.shared.utils.slugify import slugify

UNSET = object()  # "field not sent" (None means "clear it")


class ProductService:

    def __init__(
        self,
        repository: ProductRepository,
        category_repository: CategoryRepository,
        storage: StorageService,
        tax_repository: TaxRepository | None = None,
    ) -> None:
        self.repository = repository
        self.category_repository = category_repository
        self.storage = storage
        self.tax_repository = tax_repository
        # (old, new) classification ids when the last update changed the assignment, for auditing.
        self.tax_assignment_change: tuple[int | None, int | None] | None = None

    async def _classification_status(self, classification_id: int | None) -> str | None:
        """Status of an existing classification; raises if the id does not exist."""
        if classification_id is None:
            return None
        record = (
            await self.tax_repository.get(TaxClassification, classification_id)
            if self.tax_repository is not None
            else None
        )
        if record is None:
            raise InvalidTaxClassificationError(classification_id)
        return record.status

    async def _validate_assignment(self, classification_id: int) -> None:
        """Only an existing, APPROVED classification can be assigned (the DB enforces it too)."""
        if await self._classification_status(classification_id) != "approved":
            raise ClassificationNotApprovedError(classification_id)

    async def _enforce_tax_required(self, status: ProductStatus, classification_id: int | None) -> None:
        """When TAX_CLASSIFICATION_REQUIRED is on, an active product needs an approved classification."""
        if not get_settings().tax_classification_required or status != ProductStatus.ACTIVE:
            return
        if await self._classification_status(classification_id) != "approved":
            raise ProductTaxRequiredError()

    async def _validate_category(self, category_id: int) -> None:
        category = await self.category_repository.get_by_id(category_id)
        if category is None or not category.is_active:
            raise InvalidCategoryError(category_id)

    async def _unique_slug_for(self, name: str) -> str:
        base_slug = slugify(name)
        candidate = base_slug
        suffix = 2
        while await self.repository.get_by_slug(candidate) is not None:
            candidate = f"{base_slug}-{suffix}"
            suffix += 1
        return candidate

    async def create_product(
        self,
        *,
        name: str,
        description: str | None,
        price_cents: int,
        sku: str,
        stock: int,
        category_id: int,
        status: ProductStatus,
        tax_classification_id: int | None = None,
        unit: str | None = None,
    ) -> Product:
        await self._validate_category(category_id)
        if tax_classification_id is not None:
            await self._validate_assignment(tax_classification_id)
        await self._enforce_tax_required(status, tax_classification_id)

        if await self.repository.get_by_sku(sku) is not None:
            raise SkuAlreadyExistsError(sku)

        slug = await self._unique_slug_for(name)

        return await self.repository.create(
            name=name,
            slug=slug,
            description=description,
            price_cents=price_cents,
            sku=sku,
            stock=stock,
            category_id=category_id,
            status=status,
            tax_classification_id=tax_classification_id,
            unit=unit,
        )

    async def get_product(self, product_id: int, *, is_admin: bool = False) -> Product:
        product = await self.repository.get_by_id(product_id)
        if product is None:
            raise ProductNotFoundError(product_id)

        # A draft/archived product doesn't exist as far as an anonymous or non-admin caller 
        if product.status != ProductStatus.ACTIVE and not is_admin:
            raise ProductNotFoundError(product_id)

        return product

    async def list_products(
        self,
        *,
        offset: int,
        limit: int,
        category_id: int | None = None,
        search: str | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        sort: str = "-created_at",
        is_admin: bool = False,
    ) -> tuple[list[Product], int]:
        return await self.repository.list_paginated(
            offset=offset,
            limit=limit,
            category_id=category_id,
            search=search,
            min_price=min_price,
            max_price=max_price,
            sort=sort,
            include_inactive=is_admin,
        )

    async def update_product(
        self,
        *,
        product_id: int,
        name: str | None,
        description: str | None,
        price_cents: int | None,
        stock: int | None,
        category_id: int | None,
        status: ProductStatus | None,
        tax_classification_id=UNSET,
        unit=UNSET,
    ) -> Product:
        product = await self.repository.get_by_id(product_id)
        if product is None:
            raise ProductNotFoundError(product_id)

        updates: dict = {}
        self.tax_assignment_change = None

        if tax_classification_id is not UNSET:
            if tax_classification_id is not None:
                await self._validate_assignment(tax_classification_id)
            if tax_classification_id != product.tax_classification_id:
                self.tax_assignment_change = (product.tax_classification_id, tax_classification_id)
            updates["tax_classification_id"] = tax_classification_id
        if unit is not UNSET:
            updates["unit"] = unit

        final_status = status if status is not None else product.status
        final_classification = updates.get("tax_classification_id", product.tax_classification_id)
        await self._enforce_tax_required(final_status, final_classification)

        if category_id is not None:
            await self._validate_category(category_id)
            updates["category_id"] = category_id

        if name is not None and name != product.name:
            updates["name"] = name
            updates["slug"] = await self._unique_slug_for(name)

        if description is not None:
            updates["description"] = description
        if price_cents is not None:
            updates["price_cents"] = price_cents
        if stock is not None:
            updates["stock"] = stock
        if status is not None:
            updates["status"] = status

        return await self.repository.update(product, **updates)

    async def archive_product(self, product_id: int) -> Product:
        product = await self.repository.get_by_id(product_id)
        if product is None:
            raise ProductNotFoundError(product_id)
        return await self.repository.update(product, status=ProductStatus.ARCHIVED)

    async def upload_product_image(
        self, *, product_id: int, image: UploadFile
    ) -> Product:

        product = await self.repository.get_by_id(product_id)
        if product is None:
            raise ProductNotFoundError(product_id)

        validate_image(image)
        extension = get_image_extension(image.filename)

        path = f"products/{product.id}/{uuid4()}.{extension}"

        old_image_path = product.image_path

        await self.storage.upload(
            file=image.file,
            path=path,
            content_type=image.content_type or "application/octet-stream",
        )

        try:
            product = await self.repository.update(product, image_path=path)
        except Exception:
            await self.storage.delete(path=path)
            raise

        if old_image_path:
            await self.storage.delete(path=old_image_path)

        return product

    async def delete_product_image(self, *, product_id: int) -> Product:
        product = await self.repository.get_by_id(product_id)
        if product is None:
            raise ProductNotFoundError(product_id)

        old_image_path = product.image_path
        if old_image_path is None:
            return product

        product = await self.repository.update(product, image_path=None)
        await self.storage.delete(path=old_image_path)

        return product