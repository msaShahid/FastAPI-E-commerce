from datetime import UTC, datetime

from app.modules.audit.services.audit_service import AuditService
from app.modules.auth.models.user import User
from app.modules.site_config.exceptions.site_config_exceptions import (
    ConfigVersionNotFoundError,
    NoDraftError,
    NothingToChangeError,
    PublishedElsewhereError,
)
from app.modules.site_config.models.site_config import SiteConfigVersion
from app.modules.site_config.repositories.site_config_repository import SiteConfigRepository
from app.modules.site_config.schemas.site_config import (
    SCHEMA_VERSION,
    AdminSiteConfig,
    PublicSiteConfig,
    SiteConfigDocument,
    VersionPage,
    VersionRead,
    VersionSummary,
)


def _summary(row: SiteConfigVersion, published: int | None) -> VersionSummary:
    return VersionSummary(
        version=row.version, note=row.note, created_by_user_id=str(row.created_by_user_id) if row.created_by_user_id else None,
        created_at=row.created_at, is_published=row.version == published,
    )


class SiteConfigService:
    def __init__(self, repository: SiteConfigRepository, audit: AuditService) -> None:
        self.repo = repository
        self.audit = audit

    async def _audit(self, admin: User, action: str, **details) -> None:
        await self.audit.log(actor_user_id=admin.id, action=f"site_config.{action}", target_type="site_config", target_id="1", details=details or None)

    # ------------------------------------------------------------------ public
    async def public(self) -> PublicSiteConfig:
        """The published configuration, in the envelope the storefront parses. Nothing published yet: an empty one (the storefront uses its defaults)."""
        state = await self.repo.state()
        row = await self.repo.get_version(state.published_version) if state.published_version else None
        if row is None:
            return PublicSiteConfig()
        doc = row.document
        return PublicSiteConfig(
            schema_version=SCHEMA_VERSION, config_version=row.version, settings=doc.get("settings", {}),
            sections=doc.get("sections", {}), flags=doc.get("flags", {}),
        )

    # ------------------------------------------------------------------ administrators
    async def overview(self) -> AdminSiteConfig:
        state = await self.repo.state()
        row = await self.repo.get_version(state.published_version) if state.published_version else None
        published = None if row is None else VersionRead(**_summary(row, state.published_version).model_dump(), document=row.document)
        return AdminSiteConfig(
            published=published, draft=state.draft, draft_updated_at=state.draft_updated_at,
            draft_differs_from_published=state.draft is not None and (row is None or state.draft != row.document),
        )

    async def save_draft(self, admin: User, document: SiteConfigDocument) -> AdminSiteConfig:
        state = await self.repo.state(lock=True)
        await self.repo.save_draft(state, draft=document.stored(), user_id=admin.id, now=datetime.now(UTC))
        await self._audit(admin, "draft_saved")
        return await self.overview()

    async def discard_draft(self, admin: User) -> AdminSiteConfig:
        state = await self.repo.state(lock=True)
        await self.repo.save_draft(state, draft=None, user_id=admin.id, now=datetime.now(UTC))
        await self._audit(admin, "draft_discarded")
        return await self.overview()

    async def _publish(self, admin: User, document: dict, note: str | None, *, action: str, **details) -> VersionRead:
        row = await self.repo.add_version(document=document, note=note, user_id=admin.id)
        state = await self.repo.state(lock=True)
        state.published_version = row.version
        await self.repo.db.flush()
        await self._audit(admin, action, version=row.version, **details)
        return VersionRead(**_summary(row, row.version).model_dump(), document=row.document)

    async def publish(self, admin: User, *, note: str | None, expected_published_version: int | None) -> VersionRead:
        state = await self.repo.state(lock=True)
        if expected_published_version is not None and expected_published_version != (state.published_version or 0):
            raise PublishedElsewhereError()
        if state.draft is None:
            raise NoDraftError()
        document = state.draft
        published = await self.repo.get_version(state.published_version) if state.published_version else None
        if published is not None and published.document == document:
            raise NothingToChangeError()
        result = await self._publish(admin, document, note, action="published")
        await self.repo.save_draft(state, draft=None, user_id=admin.id, now=datetime.now(UTC))  # the draft became the published version
        return result

    async def rollback(self, admin: User, version: int, *, note: str | None) -> VersionRead:
        """Publishes a COPY of an earlier version as a new one: history is never rewritten or deleted."""
        state = await self.repo.state(lock=True)
        target = await self.repo.get_version(version)
        if target is None:
            raise ConfigVersionNotFoundError(version)
        if state.published_version == version:
            raise NothingToChangeError()
        return await self._publish(admin, target.document, note or f"Rollback to version {version}", action="rolled_back", rolled_back_to=version)

    async def versions(self, *, offset: int, limit: int) -> VersionPage:
        state = await self.repo.state()
        rows, total = await self.repo.list_versions(offset=offset, limit=limit)
        return VersionPage(items=[_summary(r, state.published_version) for r in rows], total=total)

    async def version(self, version: int) -> VersionRead:
        state = await self.repo.state()
        row = await self.repo.get_version(version)
        if row is None:
            raise ConfigVersionNotFoundError(version)
        return VersionRead(**_summary(row, state.published_version).model_dump(), document=row.document)
