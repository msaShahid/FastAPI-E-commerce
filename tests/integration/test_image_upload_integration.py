"""
HTTP-level tests for image upload (categories and products): real
multipart requests through the ASGI app, real auth, real database.

Storage is swapped for an in-memory recorder so nothing is written to
disk, which also lets the tests assert exactly what did and did not reach
storage. The validator itself previously had no tests at all, so every
rejection rule below is pinned here.
"""

import pytest
from sqlalchemy import select

from app.core.storage.interface import StorageService
from app.main import app
from app.modules.categories.dependencies.category_deps import get_storage_service
from app.modules.categories.models.category import Category
from app.modules.products.models.product import Product

# Smallest byte strings that carry each format's real file signature.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32
WEBP_BYTES = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
EXE_BYTES = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64  # Windows executable header
SCRIPT_BYTES = b"<script>alert(1)</script>"


class RecordingStorage(StorageService):
    """In-memory storage that remembers every upload and delete."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def upload(self, *, file, path: str, content_type: str) -> str:
        self.files[path] = file.read()
        return path

    async def delete(self, *, path: str) -> None:
        self.files.pop(path, None)
        self.deleted.append(path)

    def get_url(self, *, path: str) -> str:
        return f"/fake-media/{path}"


@pytest.fixture
def storage(client):
    # Depends on `client` so it runs after it; the client fixture clears
    # all dependency overrides on teardown.
    recorder = RecordingStorage()
    app.dependency_overrides[get_storage_service] = lambda: recorder
    return recorder


@pytest.fixture
async def category_id(client, admin_headers):
    response = await client.post(
        "/api/v1/categories", json={"name": "Photos", "description": None}, headers=admin_headers
    )
    return response.json()["id"]


@pytest.fixture
async def product_id(client, admin_headers, category_id):
    response = await client.post(
        "/api/v1/products",
        json={
            "name": "Camera",
            "description": None,
            "price_cents": 9900,
            "sku": "CAM-001",
            "stock": 5,
            "category_id": category_id,
            "status": "active",
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def upload(client, url, headers, *, filename, content, content_type):
    return client.post(url, files={"image": (filename, content, content_type)}, headers=headers)


# --------------------------------------------------------------------------
# Accepted uploads
# --------------------------------------------------------------------------


async def test_create_product_returns_201_with_its_category(client, admin_headers, category_id):
    # Regression: POST /products used to 500 because the new product's
    # `category` relationship was never loaded before serialisation.
    response = await client.post(
        "/api/v1/products",
        json={
            "name": "Tripod",
            "description": None,
            "price_cents": 4500,
            "sku": "TRI-001",
            "stock": 3,
            "category_id": category_id,
            "status": "active",
        },
        headers=admin_headers,
    )

    assert response.status_code == 201, response.text
    assert response.json()["category"]["id"] == category_id


@pytest.mark.parametrize(
    "filename,content,content_type,expected_ext",
    [
        ("photo.png", PNG_BYTES, "image/png", "png"),
        ("photo.jpg", JPEG_BYTES, "image/jpeg", "jpg"),
        ("photo.jpeg", JPEG_BYTES, "image/jpeg", "jpg"),  # .jpeg is normalised to .jpg
        ("photo.webp", WEBP_BYTES, "image/webp", "webp"),
        ("PHOTO.PNG", PNG_BYTES, "image/png", "png"),  # extension case is irrelevant
    ],
    ids=["png", "jpg", "jpeg", "webp", "uppercase-extension"],
)
async def test_valid_image_is_accepted_for_a_category(
    client, admin_headers, storage, category_id, filename, content, content_type, expected_ext
):
    response = await upload(
        client,
        f"/api/v1/categories/{category_id}/image",
        admin_headers,
        filename=filename,
        content=content,
        content_type=content_type,
    )

    assert response.status_code == 200, response.text
    assert response.json()["image_url"].endswith(f".{expected_ext}")
    assert len(storage.files) == 1
    assert next(iter(storage.files.values())) == content


async def test_valid_image_is_accepted_for_a_product(client, admin_headers, storage, product_id):
    response = await upload(
        client,
        f"/api/v1/products/{product_id}/image",
        admin_headers,
        filename="camera.webp",
        content=WEBP_BYTES,
        content_type="image/webp",
    )

    assert response.status_code == 200, response.text
    assert response.json()["image_url"].endswith(".webp")


# --------------------------------------------------------------------------
# Rejected uploads: every case must be refused AND leave storage/DB untouched
# --------------------------------------------------------------------------

REJECTED_UPLOADS = [
    pytest.param("shell.jpg", EXE_BYTES, "image/jpeg", id="exe-renamed-to-jpg"),
    pytest.param("shell.png", EXE_BYTES, "image/png", id="exe-renamed-to-png"),
    pytest.param("page.jpg", SCRIPT_BYTES, "image/jpeg", id="html-script-renamed-to-jpg"),
    pytest.param("empty.png", b"", "image/png", id="empty-file"),
    pytest.param("notes.png", PNG_BYTES, "text/plain", id="disallowed-content-type"),
    pytest.param("anim.gif", PNG_BYTES, "image/png", id="disallowed-extension-gif"),
    pytest.param("anim.gif", b"GIF89a" + b"\x00" * 20, "image/gif", id="real-gif"),
    pytest.param("photo", PNG_BYTES, "image/png", id="no-extension"),
    pytest.param("photo.jpg", PNG_BYTES, "image/jpeg", id="png-content-labelled-jpg"),
    pytest.param("photo.png", JPEG_BYTES, "image/png", id="jpeg-content-labelled-png"),
    pytest.param("photo.png", PNG_BYTES, "image/jpeg", id="content-type-contradicts-content"),
    pytest.param("photo.jpg", PNG_BYTES, "image/png", id="extension-contradicts-content"),
]


@pytest.mark.parametrize("filename,content,content_type", REJECTED_UPLOADS)
async def test_invalid_category_image_is_rejected_and_nothing_is_stored(
    client, admin_headers, storage, db_session, category_id, filename, content, content_type
):
    response = await upload(
        client,
        f"/api/v1/categories/{category_id}/image",
        admin_headers,
        filename=filename,
        content=content,
        content_type=content_type,
    )

    assert response.status_code in (400, 409, 422), response.text
    assert storage.files == {}
    db_session.expire_all()
    category = (
        await db_session.execute(select(Category).where(Category.id == category_id))
    ).scalar_one()
    assert category.image_path is None


@pytest.mark.parametrize("filename,content,content_type", REJECTED_UPLOADS)
async def test_invalid_product_image_is_rejected_and_nothing_is_stored(
    client, admin_headers, storage, db_session, product_id, filename, content, content_type
):
    response = await upload(
        client,
        f"/api/v1/products/{product_id}/image",
        admin_headers,
        filename=filename,
        content=content,
        content_type=content_type,
    )

    assert response.status_code in (400, 409, 422), response.text
    assert storage.files == {}
    db_session.expire_all()
    product = (await db_session.execute(select(Product).where(Product.id == product_id))).scalar_one()
    assert product.image_path is None


async def test_request_without_a_file_part_is_rejected(client, admin_headers, storage, category_id):
    response = await client.post(f"/api/v1/categories/{category_id}/image", headers=admin_headers)

    assert response.status_code == 422
    assert storage.files == {}


# --------------------------------------------------------------------------
# Authorization and missing targets
# --------------------------------------------------------------------------


async def test_non_admin_cannot_upload_an_image(client, storage, category_id):
    await client.post(
        "/api/v1/auth/register",
        json={"username": "plainuser", "email": "plain@example.com", "password": "supersecret123"},
    )
    login = await client.post(
        "/api/v1/auth/login", json={"email": "plain@example.com", "password": "supersecret123"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await upload(
        client,
        f"/api/v1/categories/{category_id}/image",
        headers,
        filename="photo.png",
        content=PNG_BYTES,
        content_type="image/png",
    )

    assert response.status_code == 403
    assert storage.files == {}


async def test_unauthenticated_request_cannot_upload_an_image(client, storage, product_id):
    response = await upload(
        client,
        f"/api/v1/products/{product_id}/image",
        {},
        filename="photo.png",
        content=PNG_BYTES,
        content_type="image/png",
    )

    assert response.status_code in (401, 403)
    assert storage.files == {}


async def test_upload_to_a_nonexistent_category_is_404(client, admin_headers, storage):
    response = await upload(
        client,
        "/api/v1/categories/999999/image",
        admin_headers,
        filename="photo.png",
        content=PNG_BYTES,
        content_type="image/png",
    )

    assert response.status_code == 404
    assert storage.files == {}


# --------------------------------------------------------------------------
# Replace and delete
# --------------------------------------------------------------------------


async def test_replacing_an_image_removes_the_old_file(client, admin_headers, storage, category_id):
    url = f"/api/v1/categories/{category_id}/image"
    await upload(
        client, url, admin_headers, filename="a.png", content=PNG_BYTES, content_type="image/png"
    )
    first_path = next(iter(storage.files))

    response = await upload(
        client, url, admin_headers, filename="b.jpg", content=JPEG_BYTES, content_type="image/jpeg"
    )

    assert response.status_code == 200
    assert first_path in storage.deleted
    assert len(storage.files) == 1
    assert first_path not in storage.files


async def test_a_rejected_replacement_keeps_the_existing_image(
    client, admin_headers, storage, category_id
):
    url = f"/api/v1/categories/{category_id}/image"
    await upload(
        client, url, admin_headers, filename="a.png", content=PNG_BYTES, content_type="image/png"
    )
    original = dict(storage.files)

    response = await upload(
        client, url, admin_headers, filename="b.jpg", content=EXE_BYTES, content_type="image/jpeg"
    )

    assert response.status_code in (400, 409, 422)
    assert storage.files == original
    assert storage.deleted == []


async def test_deleting_an_image_removes_it_from_storage(client, admin_headers, storage, product_id):
    url = f"/api/v1/products/{product_id}/image"
    await upload(
        client, url, admin_headers, filename="a.png", content=PNG_BYTES, content_type="image/png"
    )

    response = await client.delete(url, headers=admin_headers)

    assert response.status_code == 200
    assert response.json()["image_url"] is None
    assert storage.files == {}
