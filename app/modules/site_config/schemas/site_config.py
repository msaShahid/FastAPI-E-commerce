"""The storefront configuration document. The limits mirror the storefront's own schema (src/schemas/site-config.ts): what is accepted here is
what the storefront can render. Every unknown field is REJECTED, so a typo is an error now instead of a silently ignored setting later."""

from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

SCHEMA_VERSION = 1
KNOWN_FLAGS = ("cart", "checkout", "cartQuote", "reviews", "wishlist")
SECTION_TYPES = ("hero", "banner", "product_row", "rich_text")


def is_safe_href(value: str) -> bool:
    """Same rule as the storefront: a site path, an anchor, or an https, mailto or tel link. Never javascript:, data:, a protocol-relative
    URL, a backslash or a control character."""
    v = value.strip()
    if not v or len(v) > 2000 or v.startswith("//"):
        return False
    if v.startswith(("/", "#")):
        return not any(ord(c) < 32 or c == "\\" for c in v)
    try:
        return urlparse(v).scheme in ("https", "mailto", "tel")
    except ValueError:
        return False


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


Label = Annotated[str, Field(min_length=1, max_length=80)]


class Link(_Strict):
    label: Label
    href: str
    visible: bool = True

    @field_validator("href")
    @classmethod
    def _safe(cls, v: str) -> str:
        if not is_safe_href(v):
            raise ValueError("unsafe link: use a site path, an anchor, or an https, mailto or tel link")
        return v.strip()


class LinkGroup(_Strict):
    title: Label
    links: list[Link] = Field(max_length=20)


class SiteNs(_Strict):
    name: str = Field(min_length=1, max_length=60)
    tagline: str | None = Field(default=None, max_length=160)


class DisplayNs(_Strict):
    """Presentation only: there is deliberately no currency here (the store currency is fixed in store settings)."""

    format_locale: str = Field(alias="formatLocale", min_length=2, max_length=20)
    symbol_style: Literal["symbol", "narrowSymbol", "code", "name"] = Field(default="symbol", alias="symbolStyle")


class NavigationNs(_Strict):
    header: list[Link] = Field(max_length=12)
    footer: list[LinkGroup] = Field(max_length=6)


class FooterNs(_Strict):
    text: str | None = Field(default=None, max_length=300)


class AnnouncementNs(_Strict):
    visible: bool
    text: str = Field(max_length=200)
    link: Link | None = None


class SeoNs(_Strict):
    title_template: str | None = Field(default=None, alias="titleTemplate", max_length=100)
    description: str | None = Field(default=None, max_length=300)


class SettingsDoc(_Strict):
    site: SiteNs | None = None
    display: DisplayNs | None = None
    navigation: NavigationNs | None = None
    footer: FooterNs | None = None
    announcement: AnnouncementNs | None = None
    seo: SeoNs | None = None
    countries: list[str] | None = Field(default=None, max_length=250)

    @field_validator("countries")
    @classmethod
    def _countries(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out = [c.strip().upper() for c in v]
        if any(len(c) != 2 or not c.isalpha() for c in out) or len(set(out)) != len(out):
            raise ValueError("countries must be unique two-letter codes (ISO 3166-1 alpha-2)")
        return out


class HeroData(_Strict):
    headline: str = Field(max_length=120)
    body: str | None = Field(default=None, max_length=300)
    primary: Link | None = None
    secondary: Link | None = None


class BannerData(_Strict):
    headline: str = Field(max_length=120)
    body: str | None = Field(default=None, max_length=300)
    cta: Link | None = None


class ProductRowData(_Strict):
    title: str = Field(max_length=80)
    category_id: int | None = Field(default=None, alias="categoryId")
    product_ids: list[int] | None = Field(default=None, alias="productIds", max_length=24)
    limit: int = Field(default=4, ge=1, le=24)


class RichTextData(_Strict):
    markdown: str = Field(max_length=5000)


_SECTION_DATA = {"hero": HeroData, "banner": BannerData, "product_row": ProductRowData, "rich_text": RichTextData}


class Section(_Strict):
    id: str = Field(min_length=1, max_length=64)
    type: Literal["hero", "banner", "product_row", "rich_text"]
    visible: bool = True
    order: int
    schema_version: int = Field(default=1, ge=1, le=1)
    data: dict

    @model_validator(mode="after")
    def _data_matches_the_type(self) -> "Section":
        self.data = _SECTION_DATA[self.type].model_validate(self.data).model_dump(by_alias=True, exclude_none=True)
        return self


class SectionsDoc(_Strict):
    home: list[Section] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def _unique_ids(self) -> "SectionsDoc":
        ids = [s.id for s in self.home]
        if len(set(ids)) != len(ids):
            raise ValueError("section ids must be unique")
        return self


class SiteConfigDocument(_Strict):
    settings: SettingsDoc = Field(default_factory=SettingsDoc)
    sections: SectionsDoc = Field(default_factory=SectionsDoc)
    flags: dict[str, StrictBool] = Field(default_factory=dict)  # strict: the word "yes" is not a flag value

    @field_validator("flags")
    @classmethod
    def _known_flags(cls, v: dict[str, bool]) -> dict[str, bool]:
        unknown = sorted(set(v) - set(KNOWN_FLAGS))
        if unknown:
            raise ValueError(f"unknown flags: {', '.join(unknown)} (known: {', '.join(KNOWN_FLAGS)})")
        return v

    def stored(self) -> dict:
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")


class PublicSiteConfig(BaseModel):
    """What the storefront fetches. `settings`, `sections` and `flags` hold only what an administrator set: the storefront fills the rest from its defaults."""

    schema_version: int = SCHEMA_VERSION
    config_version: int | None = None
    settings: dict = Field(default_factory=dict)
    sections: dict = Field(default_factory=dict)
    flags: dict[str, bool] = Field(default_factory=dict)


class PublishRequest(_Strict):
    note: str | None = Field(default=None, max_length=500)
    expected_published_version: int | None = Field(default=None, description="Refuse if someone published since you loaded the page")


class RollbackRequest(_Strict):
    note: str | None = Field(default=None, max_length=500)


class VersionSummary(BaseModel):
    version: int
    note: str | None
    created_by_user_id: str | None
    created_at: datetime
    is_published: bool = False


class VersionRead(VersionSummary):
    document: dict


class AdminSiteConfig(BaseModel):
    published: VersionRead | None
    draft: dict | None
    draft_updated_at: datetime | None
    draft_differs_from_published: bool


class VersionPage(BaseModel):
    items: list[VersionSummary]
    total: int
