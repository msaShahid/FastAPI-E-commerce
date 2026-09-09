"""
Development database seeding.

Run with:
    docker compose exec api python -m scripts.seed

Creates (idempotently -- safe to run repeatedly):
    - One admin user
    - One regular user
    - A handful of categories
    - A handful of products distributed across those categories

Deliberately reuses the SAME services the API itself uses
(AuthService, CategoryService, ProductService) rather than inserting
rows directly. This means seeded data goes through the exact same
validation, slug generation, and uniqueness rules as data created
through the real API -- there's no second, drifting code path for
"how a user/category/product gets created."
"""

import asyncio

from app.core.config import get_settings
from app.core.database import async_session_factory
from app.modules.auth.repositories.auth_repository import AuthRepository
from app.modules.auth.services.auth_service import AuthService
from app.modules.categories.exceptions.category_exceptions import (
    CategoryNameAlreadyExistsError,
)
from app.modules.categories.repositories.category_repository import CategoryRepository
from app.modules.categories.services.category_service import CategoryService
from app.modules.products.exceptions.product_exceptions import SkuAlreadyExistsError
from app.modules.products.repositories.product_repository import ProductRepository
from app.modules.products.services.product_service import ProductService
from app.modules.users.repositories.user_repository import UserRepository
from app.shared.enums.product_status import ProductStatus
from app.shared.enums.roles import UserRole

settings = get_settings()

# run for seed : docker compose exec api python -m scripts.seed

CATEGORIES = [
    ("Electronics", "Phones, laptops, tablets, and electronic accessories."),
    ("Books", "Fiction, non-fiction, technical, and educational books."),
    ("Home & Kitchen", "Appliances, cookware, furniture, and kitchen essentials."),
    ("Office Supplies", "Stationery, desk accessories, and office equipment."),
    ("Sports & Fitness", "Fitness equipment, sports accessories, and outdoor gear."),
    ("Clothing", "Everyday clothing, casual wear, and accessories."),
    (
        "Beauty & Personal Care",
        "Skincare, grooming products, and personal care essentials.",
    ),
    ("Toys & Games", "Toys, board games, puzzles, and entertainment products."),
]

# (name, sku, price_cents, stock, category_name)
PRODUCTS = [
    # Electronics
    ("Wireless Mouse", "ELEC-MOUSE-001", 1999, 50, "Electronics"),
    ("Mechanical Keyboard", "ELEC-KB-001", 8999, 20, "Electronics"),
    ("USB-C Hub", "ELEC-HUB-001", 3499, 35, "Electronics"),
    ("Bluetooth Headphones", "ELEC-HEADPHONE-001", 5999, 25, "Electronics"),
    ("1080p Webcam", "ELEC-WEBCAM-001", 4499, 18, "Electronics"),
    ("Portable Power Bank", "ELEC-POWERBANK-001", 2999, 40, "Electronics"),
    ("Laptop Stand", "ELEC-STAND-001", 2799, 30, "Electronics"),
    ("USB-C Charging Cable", "ELEC-CABLE-001", 899, 100, "Electronics"),
    # Books
    ("Clean Code", "BOOK-CC-001", 3499, 100, "Books"),
    ("Designing Data-Intensive Applications", "BOOK-DDIA-001", 4999, 40, "Books"),
    ("The Pragmatic Programmer", "BOOK-PP-001", 4299, 60, "Books"),
    ("Python Crash Course", "BOOK-PCC-001", 2999, 75, "Books"),
    ("Atomic Habits", "BOOK-AH-001", 1599, 90, "Books"),
    ("Deep Work", "BOOK-DW-001", 1399, 65, "Books"),
    ("Refactoring", "BOOK-REFACTOR-001", 3899, 35, "Books"),
    # Home & Kitchen
    ("Stainless Steel Kettle", "HOME-KETTLE-001", 2499, 30, "Home & Kitchen"),
    ("Non-Stick Frying Pan", "HOME-PAN-001", 1899, 45, "Home & Kitchen"),
    ("Electric Rice Cooker", "HOME-RICECOOKER-001", 3999, 22, "Home & Kitchen"),
    ("Ceramic Dinner Set", "HOME-DINNERSET-001", 5499, 15, "Home & Kitchen"),
    ("Vacuum Storage Containers", "HOME-CONTAINER-001", 1299, 55, "Home & Kitchen"),
    ("Kitchen Knife Set", "HOME-KNIFE-001", 2999, 25, "Home & Kitchen"),
    # Office Supplies
    ("A5 Hardcover Notebook", "OFFICE-NOTEBOOK-001", 599, 150, "Office Supplies"),
    ("Gel Pen Set", "OFFICE-PENS-001", 399, 200, "Office Supplies"),
    ("Desk Organizer", "OFFICE-ORGANIZER-001", 899, 70, "Office Supplies"),
    ("Ergonomic Desk Mat", "OFFICE-MAT-001", 1499, 35, "Office Supplies"),
    ("Sticky Notes Pack", "OFFICE-STICKY-001", 299, 180, "Office Supplies"),
    # Sports & Fitness
    ("Yoga Mat", "SPORT-YOGA-001", 1299, 50, "Sports & Fitness"),
    ("Adjustable Dumbbell", "SPORT-DUMBBELL-001", 4999, 20, "Sports & Fitness"),
    ("Resistance Band Set", "SPORT-BANDS-001", 999, 65, "Sports & Fitness"),
    ("Insulated Water Bottle", "SPORT-BOTTLE-001", 1799, 80, "Sports & Fitness"),
    ("Jump Rope", "SPORT-JUMPROPE-001", 699, 75, "Sports & Fitness"),
    # Clothing
    ("Classic Cotton T-Shirt", "CLOTH-TSHIRT-001", 999, 100, "Clothing"),
    ("Slim Fit Jeans", "CLOTH-JEANS-001", 2499, 45, "Clothing"),
    ("Hooded Sweatshirt", "CLOTH-HOODIE-001", 2199, 35, "Clothing"),
    ("Canvas Backpack", "CLOTH-BACKPACK-001", 1899, 50, "Clothing"),
    ("Casual Polo Shirt", "CLOTH-POLO-001", 1499, 60, "Clothing"),
    # Beauty & Personal Care
    ("Daily Face Cleanser", "BEAUTY-CLEANSER-001", 799, 60, "Beauty & Personal Care"),
    ("Moisturizing Hand Cream", "BEAUTY-CREAM-001", 499, 90, "Beauty & Personal Care"),
    ("Bamboo Hair Brush", "BEAUTY-BRUSH-001", 699, 45, "Beauty & Personal Care"),
    ("Body Lotion", "BEAUTY-LOTION-001", 899, 70, "Beauty & Personal Care"),
    ("Lip Balm", "BEAUTY-LIPBALM-001", 299, 120, "Beauty & Personal Care"),
    # Toys & Games
    ("Wooden Puzzle Set", "TOY-PUZZLE-001", 899, 40, "Toys & Games"),
    ("Strategy Board Game", "TOY-BOARDGAME-001", 2499, 25, "Toys & Games"),
    ("Building Blocks Set", "TOY-BLOCKS-001", 1599, 55, "Toys & Games"),
    ("Jigsaw Puzzle 1000 Pieces", "TOY-JIGSAW-001", 1199, 35, "Toys & Games"),
    ("Playing Cards", "TOY-CARDS-001", 299, 100, "Toys & Games"),
]


async def seed_users(
    auth_service: AuthService, user_repository: UserRepository
) -> None:
    # Admin: register normally (always creates a USER), then promote --
    # reusing UserRepository.update, the exact mechanism a real admin
    # would use via PATCH /users/{id}. No special-case "create as admin"
    # path exists anywhere in the app, on purpose.
    admin = await auth_service.repository.get_user_by_email(settings.seed_admin_email)
    if admin is None:
        admin = await auth_service.register(
            username="admin",
            email=settings.seed_admin_email,
            password=settings.seed_admin_password,
        )
        await user_repository.update(admin, role=UserRole.ADMIN)
        print(
            f"  Created admin: {settings.seed_admin_email} / {settings.seed_admin_password}"
        )
    else:
        print(f"  Admin already exists: {settings.seed_admin_email}")

    regular = await auth_service.repository.get_user_by_email(settings.seed_user_email)
    if regular is None:
        await auth_service.register(
            username="testuser",
            email=settings.seed_user_email,
            password=settings.seed_user_password,
        )
        print(
            f"  Created user: {settings.seed_user_email} / {settings.seed_user_password}"
        )
    else:
        print(f"  User already exists: {settings.seed_user_email}")


async def seed_categories(category_service: CategoryService) -> dict[str, int]:
    name_to_id: dict[str, int] = {}
    for name, description in CATEGORIES:
        try:
            category = await category_service.create_category(
                name=name, description=description
            )
            print(f"  Created category: {name}")
        except CategoryNameAlreadyExistsError:
            category = await category_service.repository.get_by_name(name)
            print(f"  Category already exists: {name}")
        name_to_id[name] = category.id
    return name_to_id


async def seed_products(
    product_service: ProductService, category_ids: dict[str, int]
) -> None:
    for name, sku, price_cents, stock, category_name in PRODUCTS:
        try:
            await product_service.create_product(
                name=name,
                description=None,
                price_cents=price_cents,
                sku=sku,
                stock=stock,
                category_id=category_ids[category_name],
                status=ProductStatus.ACTIVE,
            )
            print(f"  Created product: {name} ({sku})")
        except SkuAlreadyExistsError:
            print(f"  Product already exists: {name} ({sku})")


async def main() -> None:
    # if settings.environment == "production":
    #     raise RuntimeError(
    #         "Refusing to run the seed script with ENVIRONMENT=production. "
    #         "This script creates known, fixed test credentials -- never run it "
    #         "against a real production database."
    #     )

    async with async_session_factory() as db:
        auth_repo = AuthRepository(db)
        user_repo = UserRepository(db)
        category_repo = CategoryRepository(db)
        product_repo = ProductRepository(db)

        auth_service = AuthService(auth_repo)
        category_service = CategoryService(category_repo)
        product_service = ProductService(product_repo, category_repo)

        print("Seeding users...")
        await seed_users(auth_service, user_repo)

        print("Seeding categories...")
        category_ids = await seed_categories(category_service)

        print("Seeding products...")
        await seed_products(product_service, category_ids)

        await db.commit()

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
