from fastapi import UploadFile

from app.core.config import get_settings
from app.core.exceptions import InvalidImageError

ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

# Canonical extension for each real (sniffed) image type. This matches what
# get_image_extension() returns, which normalises "jpeg" to "jpg".
_EXTENSION_FOR_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
# Enough to recognise every signature above (WebP needs bytes 0-3 and 8-11).
_SNIFF_LENGTH = 12


def sniff_image_type(header: bytes) -> str | None:
    """Return the real image MIME type from the file's leading bytes, or None.

    The client-supplied Content-Type header and filename are just text the
    caller chose, so they say nothing about what the bytes actually are.
    """
    if header.startswith(_PNG_SIGNATURE):
        return "image/png"
    if header.startswith(_JPEG_SIGNATURE):
        return "image/jpeg"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def _read_header(image: UploadFile) -> bytes:
    stream = image.file
    stream.seek(0)
    header = stream.read(_SNIFF_LENGTH)
    stream.seek(0)  # leave the stream ready for the storage upload
    return header


def validate_image(image: UploadFile) -> None:
    limit = get_settings().max_image_bytes
    if image.size is not None and image.size > limit:
        raise InvalidImageError(f"The image is too large (the limit is {limit // (1024 * 1024)} MB).")
    if image.content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
        raise InvalidImageError("Only JPEG, PNG and WebP images are allowed.")

    actual_type = sniff_image_type(_read_header(image))
    if actual_type is None:
        raise InvalidImageError("The uploaded file is not a valid JPEG, PNG or WebP image.")

    if actual_type != image.content_type:
        raise InvalidImageError("The file content does not match its declared image type.")

    if get_image_extension(image.filename) != _EXTENSION_FOR_TYPE[actual_type]:
        raise InvalidImageError("The file extension does not match the image content.")


def get_image_extension(filename: str | None) -> str:
    if not filename or "." not in filename:
        raise InvalidImageError("Image must have a valid file extension.")

    extension = filename.rsplit(".", 1)[-1].lower()
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        raise InvalidImageError("Unsupported image extension.")

    return "jpg" if extension == "jpeg" else extension
