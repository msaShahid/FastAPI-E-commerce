from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.modules.audit.services.audit_service import AuditService
from app.modules.auth.models.user import User
from app.modules.tax.engine import (
    GST_CALCULATOR_IMPLEMENTED,
    evaluate_config,
    india_today,
)
from app.modules.tax.exceptions.tax_exceptions import (
    ApprovalPrerequisiteError,
    ApprovalRequiresDistinctApproverError,
    ClassificationInUseError,
    DuplicateClassificationError,
    DuplicateRegistrationError,
    ImmutableTaxRecordError,
    InvalidTaxInputError,
    InvalidTaxTransitionError,
    RegistrationInUseError,
    RuleOverlapError,
    SelfApprovalNoteRequiredError,
    SettingsOverlapError,
    TaxRecordNotFoundError,
)
from app.modules.tax.models.tax import (
    BusinessTaxSettings,
    GstRegistration,
    TaxClassification,
    TaxRule,
)
from app.modules.tax.repositories.tax_repository import TaxRepository
from app.modules.tax.schemas.tax import (
    ClassificationCreate,
    ClassificationUpdate,
    PaymentReadiness,
    ProductTaxRead,
    ReadinessBlocker,
    ReadinessRead,
    RegistrationCreate,
    RegistrationUpdate,
    RuleCreate,
    RuleUpdate,
    SettingsCreate,
    SettingsUpdate,
    check_rule_shape,
)
from app.shared.enums.tax import product_tax_state


def _blank(value: str | None) -> bool:
    return value is None or not value.strip()


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so a search term is matched literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class TaxService:
    """Administration of the GST master. Every mutation is audited, none touches checkout."""

    def __init__(self, repository: TaxRepository, audit: AuditService) -> None:
        self.repo = repository
        self.audit = audit

    # ------------------------------------------------------------ shared helpers
    async def _get(self, model, record_id: int, what: str, *, lock: bool = False):
        record = await self.repo.get(model, record_id, for_update=lock)
        if record is None:
            raise TaxRecordNotFoundError(what)
        return record

    async def _log(self, actor: User, action: str, target_type: str, record, details=None) -> None:
        await self.audit.log(
            actor_user_id=actor.id,
            action=action,
            target_type=target_type,
            target_id=record.id,
            details=details,
        )

    @staticmethod
    def _apply_changes(record, changes: dict, *, nullable: set[str]) -> None:
        """Applies a PATCH. An explicit null is only allowed for genuinely optional fields."""
        for key, value in changes.items():
            if value is None and key not in nullable:
                raise InvalidTaxInputError(f"{key} cannot be null")
            setattr(record, key, value.value if hasattr(value, "value") else value)

    @staticmethod
    def _require_draft(record) -> None:
        if record.status != "draft":
            raise ImmutableTaxRecordError()

    @staticmethod
    def _approve(record, actor: User, note: str | None) -> dict:
        """Draft -> approved, enforcing creator != approver unless self-approval is enabled."""
        if record.status != "draft":
            raise InvalidTaxTransitionError("approve", record.status)
        self_approved = record.created_by_user_id is not None and record.created_by_user_id == actor.id
        if self_approved:
            if not get_settings().tax_master_allow_self_approval:
                raise ApprovalRequiresDistinctApproverError()
            if _blank(note):
                raise SelfApprovalNoteRequiredError()
        record.status = "approved"
        record.approved_by_user_id = actor.id
        record.approved_at = datetime.now(UTC)
        return {"self_approved": self_approved, "note": note}

    @staticmethod
    def _retire(record) -> None:
        if record.status == "retired":
            raise InvalidTaxTransitionError("retire", record.status)
        record.status = "retired"

    # ------------------------------------------------------------ classifications
    async def create_classification(self, actor: User, data: ClassificationCreate):
        if await self.repo.classification_by_kind_code(data.kind, data.code) is not None:
            raise DuplicateClassificationError()
        record = await self.repo.add(
            TaxClassification(
                kind=data.kind.value,
                code=data.code,
                description=data.description,
                source_reference=data.source_reference,
                created_by_user_id=actor.id,
            )
        )
        await self._log(actor, "tax.classification.created", "tax_classification", record,
                        {"kind": record.kind, "code": record.code})
        return record

    async def list_classifications(self, *, offset, limit, status=None, kind=None, q=None):
        conditions = []
        if status:
            conditions.append(TaxClassification.status == status.value)
        if kind:
            conditions.append(TaxClassification.kind == kind.value)
        if q:
            like = f"%{_escape_like(q)}%"
            conditions.append(
                TaxClassification.code.ilike(like, escape="\\")
                | TaxClassification.description.ilike(like, escape="\\")
            )
        return await self.repo.list(TaxClassification, conditions, offset=offset, limit=limit)

    async def update_classification(self, actor: User, record_id: int, data: ClassificationUpdate):
        record = await self._get(TaxClassification, record_id, "Classification", lock=True)
        self._require_draft(record)
        changes = data.model_dump(exclude_unset=True)
        self._apply_changes(record, changes, nullable={"source_reference"})
        await self.repo.save(record)
        await self._log(actor, "tax.classification.updated", "tax_classification", record,
                        {"fields": sorted(changes)})
        return record

    async def approve_classification(self, actor: User, record_id: int, note: str | None):
        record = await self._get(TaxClassification, record_id, "Classification", lock=True)
        if record.status == "draft" and _blank(record.source_reference):
            raise ApprovalPrerequisiteError("A source reference is required to approve a classification")
        details = self._approve(record, actor, note)
        await self.repo.save(record)
        await self._log(actor, "tax.classification.approved", "tax_classification", record, details)
        return record

    async def retire_classification(self, actor: User, record_id: int):
        record = await self._get(TaxClassification, record_id, "Classification", lock=True)
        if record.status != "retired":
            if await self.repo.has_open_rules(record.id):
                raise ClassificationInUseError()
            count = await self.repo.count_products_using(record.id)
            if count:
                raise ClassificationInUseError(f"it is assigned to {count} product(s)")
        self._retire(record)
        await self.repo.save(record)
        await self._log(actor, "tax.classification.retired", "tax_classification", record)
        return record

    async def list_products(self, *, offset: int, limit: int, state=None):
        rows, total = await self.repo.list_products_by_tax_state(
            state.value if state else None, offset=offset, limit=limit
        )
        items = [
            ProductTaxRead(
                id=p.id,
                sku=p.sku,
                name=p.name,
                status=p.status.value if hasattr(p.status, "value") else str(p.status),
                unit=p.unit,
                tax_classification_id=p.tax_classification_id,
                tax_state=product_tax_state(cls_status),
            )
            for p, cls_status in rows
        ]
        return items, total

    # ------------------------------------------------------------ rules
    async def create_rule(self, actor: User, data: RuleCreate):
        classification = await self._get(TaxClassification, data.classification_id, "Classification")
        if classification.status == "retired":
            raise InvalidTaxInputError("Cannot add a rule to a retired classification")
        record = await self.repo.add(
            TaxRule(
                classification_id=data.classification_id,
                treatment=data.treatment.value,
                cgst_rate=data.cgst_rate,
                sgst_utgst_rate=data.sgst_utgst_rate,
                igst_rate=data.igst_rate,
                effective_from=data.effective_from,
                effective_to=data.effective_to,
                source_reference=data.source_reference,
                created_by_user_id=actor.id,
            )
        )
        await self._log(actor, "tax.rule.created", "tax_rule", record,
                        {"classification_id": record.classification_id, "treatment": record.treatment})
        return record

    async def list_rules(self, *, offset, limit, status=None, classification_id=None):
        conditions = []
        if status:
            conditions.append(TaxRule.status == status.value)
        if classification_id is not None:
            conditions.append(TaxRule.classification_id == classification_id)
        return await self.repo.list(TaxRule, conditions, offset=offset, limit=limit)

    async def update_rule(self, actor: User, record_id: int, data: RuleUpdate):
        record = await self._get(TaxRule, record_id, "Rule", lock=True)
        self._require_draft(record)
        changes = data.model_dump(exclude_unset=True)
        self._apply_changes(record, changes, nullable={"effective_to", "source_reference"})
        try:
            check_rule_shape(record.treatment, Decimal(record.cgst_rate), Decimal(record.sgst_utgst_rate),
                             Decimal(record.igst_rate))
        except ValueError as exc:
            raise InvalidTaxInputError(str(exc)) from exc
        if record.effective_to is not None and record.effective_to <= record.effective_from:
            raise InvalidTaxInputError("effective_to must be after effective_from")
        await self.repo.save(record)
        await self._log(actor, "tax.rule.updated", "tax_rule", record, {"fields": sorted(changes)})
        return record

    async def approve_rule(self, actor: User, record_id: int, note: str | None):
        record = await self._get(TaxRule, record_id, "Rule", lock=True)
        if record.status == "draft":
            if _blank(record.source_reference):
                raise ApprovalPrerequisiteError(
                    "A source reference (official notification or approval) is required to approve a rule"
                )
            classification = await self._get(TaxClassification, record.classification_id, "Classification")
            if classification.status != "approved":
                raise ApprovalPrerequisiteError("The classification must be approved first")
            if await self.repo.overlapping_approved_rule(
                record.classification_id, record.effective_from, record.effective_to, record.id
            ):
                raise RuleOverlapError()
        details = self._approve(record, actor, note)
        try:
            await self.repo.save(record)
        except IntegrityError as exc:
            # Two concurrent approvals can both pass the check above; the exclusion
            # constraint is the real guard, so report it with the stable code.
            if "ex_tax_rules_no_approved_overlap" in str(exc):
                raise RuleOverlapError() from exc
            raise
        await self._log(actor, "tax.rule.approved", "tax_rule", record, details)
        return record

    async def close_rule(self, actor: User, record_id: int, effective_to: date):
        """Ends an approved rule (exclusive end date) so a successor can start. Never extends it."""
        record = await self._get(TaxRule, record_id, "Rule", lock=True)
        if record.status != "approved":
            raise InvalidTaxTransitionError("close", record.status)
        if effective_to <= record.effective_from:
            raise InvalidTaxInputError("effective_to must be after effective_from")
        if record.effective_to is not None and effective_to > record.effective_to:
            raise InvalidTaxInputError("A rule's end date can be brought forward, never extended")
        previous = record.effective_to
        record.effective_to = effective_to
        await self.repo.save(record)
        await self._log(actor, "tax.rule.closed", "tax_rule", record,
                        {"from": str(previous) if previous else None, "to": str(effective_to)})
        return record

    async def retire_rule(self, actor: User, record_id: int):
        record = await self._get(TaxRule, record_id, "Rule", lock=True)
        self._retire(record)
        await self.repo.save(record)
        await self._log(actor, "tax.rule.retired", "tax_rule", record)
        return record

    # ------------------------------------------------------------ registrations
    async def create_registration(self, actor: User, data: RegistrationCreate):
        if await self.repo.registration_by_gstin(data.gstin) is not None:
            raise DuplicateRegistrationError()
        record = await self.repo.add(
            GstRegistration(
                gstin=data.gstin,
                legal_name=data.legal_name,
                state_code=data.state_code,
                valid_from=data.valid_from,
                valid_to=data.valid_to,
                created_by_user_id=actor.id,
            )
        )
        await self._log(actor, "tax.registration.created", "gst_registration", record,
                        {"state_code": record.state_code})  # the GSTIN itself is not copied into the log
        return record

    async def list_registrations(self, *, offset, limit, status=None):
        conditions = [GstRegistration.status == status.value] if status else []
        return await self.repo.list(GstRegistration, conditions, offset=offset, limit=limit)

    async def update_registration(self, actor: User, record_id: int, data: RegistrationUpdate):
        record = await self._get(GstRegistration, record_id, "Registration", lock=True)
        self._require_draft(record)
        changes = data.model_dump(exclude_unset=True)
        self._apply_changes(record, changes, nullable={"valid_to"})
        if record.valid_to is not None and record.valid_to <= record.valid_from:
            raise InvalidTaxInputError("valid_to must be after valid_from")
        await self.repo.save(record)
        await self._log(actor, "tax.registration.updated", "gst_registration", record,
                        {"fields": sorted(changes)})
        return record

    async def approve_registration(self, actor: User, record_id: int, note: str | None):
        record = await self._get(GstRegistration, record_id, "Registration", lock=True)
        details = self._approve(record, actor, note)
        await self.repo.save(record)
        await self._log(actor, "tax.registration.approved", "gst_registration", record, details)
        return record

    async def retire_registration(self, actor: User, record_id: int):
        record = await self._get(GstRegistration, record_id, "Registration", lock=True)
        if record.status != "retired" and await self.repo.registration_has_open_settings(record.id):
            raise RegistrationInUseError()
        self._retire(record)
        await self.repo.save(record)
        await self._log(actor, "tax.registration.retired", "gst_registration", record)
        return record

    # ------------------------------------------------------------ business tax settings
    async def create_settings(self, actor: User, data: SettingsCreate):
        registration = await self._get(GstRegistration, data.registration_id, "Registration")
        if registration.status == "retired":
            raise InvalidTaxInputError("Cannot use a retired registration")
        record = await self.repo.add(
            BusinessTaxSettings(
                pricing_mode=data.pricing_mode.value,
                place_of_supply_basis=data.place_of_supply_basis.value,
                shipping_tax_treatment=data.shipping_tax_treatment.value,
                rounding_policy=data.rounding_policy.value,
                registration_id=data.registration_id,
                effective_from=data.effective_from,
                effective_to=data.effective_to,
                source_reference=data.source_reference,
                created_by_user_id=actor.id,
            )
        )
        await self._log(actor, "tax.settings.created", "business_tax_settings", record,
                        {"pricing_mode": record.pricing_mode, "registration_id": record.registration_id})
        return record

    async def list_settings(self, *, offset, limit, status=None):
        conditions = [BusinessTaxSettings.status == status.value] if status else []
        return await self.repo.list(BusinessTaxSettings, conditions, offset=offset, limit=limit)

    async def update_settings(self, actor: User, record_id: int, data: SettingsUpdate):
        record = await self._get(BusinessTaxSettings, record_id, "Tax configuration", lock=True)
        self._require_draft(record)
        changes = data.model_dump(exclude_unset=True)
        self._apply_changes(record, changes, nullable={"effective_to", "source_reference"})
        if record.effective_to is not None and record.effective_to <= record.effective_from:
            raise InvalidTaxInputError("effective_to must be after effective_from")
        if "registration_id" in changes:
            await self._get(GstRegistration, record.registration_id, "Registration")
        await self.repo.save(record)
        await self._log(actor, "tax.settings.updated", "business_tax_settings", record, {"fields": sorted(changes)})
        return record

    async def approve_settings(self, actor: User, record_id: int, note: str | None):
        record = await self._get(BusinessTaxSettings, record_id, "Tax configuration", lock=True)
        if record.status == "draft":
            if _blank(record.source_reference):
                raise ApprovalPrerequisiteError(
                    "The GST professional's written approval (source reference) is required to approve a tax configuration"
                )
            registration = await self._get(GstRegistration, record.registration_id, "Registration")
            if registration.status != "approved":
                raise ApprovalPrerequisiteError("The registration must be approved first")
            if await self.repo.overlapping_approved_settings(record.effective_from, record.effective_to, record.id):
                raise SettingsOverlapError()
        details = self._approve(record, actor, note)
        try:
            await self.repo.save(record)
        except IntegrityError as exc:
            if "ex_business_tax_settings_no_approved_overlap" in str(exc):
                raise SettingsOverlapError() from exc
            raise
        await self._log(actor, "tax.settings.approved", "business_tax_settings", record, details)
        return record

    async def retire_settings(self, actor: User, record_id: int):
        record = await self._get(BusinessTaxSettings, record_id, "Tax configuration", lock=True)
        self._retire(record)
        await self.repo.save(record)
        await self._log(actor, "tax.settings.retired", "business_tax_settings", record)
        return record

    # ------------------------------------------------------------ readiness report
    async def readiness(self, as_of=None) -> ReadinessRead:
        """Everything still missing before GST could be activated. Read-only; changes nothing."""
        from app.modules.orders.models.order import Order
        from app.modules.payments.services.payment_service import DEFAULT_CURRENCY

        settings = get_settings()
        as_of = as_of or india_today()
        config, config_blockers = await evaluate_config(self.repo, as_of)
        counts = await self.repo.readiness_counts(as_of)
        blockers = [ReadinessBlocker(code=b.reason, detail=_CONFIG_DETAIL[b.reason]) for b in config_blockers]
        for key, code, text in _COUNT_BLOCKERS:
            if counts[key]:
                blockers.append(ReadinessBlocker(code=code, detail=f"{counts[key]} {text}"))
        if not GST_CALCULATOR_IMPLEMENTED:
            blockers.append(ReadinessBlocker(
                code="gst_calculator_not_implemented", detail="The GST calculator has not been implemented yet"))

        payment_blockers = []
        if DEFAULT_CURRENCY.lower() != "inr":
            payment_blockers.append(ReadinessBlocker(
                code="payment_currency_not_inr", detail=f"Payments are created in {DEFAULT_CURRENCY.upper()}, not INR"))
        if not hasattr(Order, "currency"):
            payment_blockers.append(ReadinessBlocker(
                code="orders_have_no_currency", detail="Orders do not record a currency"))
        return ReadinessRead(
            as_of=as_of,
            ready=not blockers,
            blockers=blockers,
            settings_id=config.id if config is not None and not config_blockers else None,
            counts=counts,
            flags={
                "tax_calculation_enabled": settings.tax_calculation_enabled,
                "tax_classification_required": settings.tax_classification_required,
            },
            payments=PaymentReadiness(
                live_payments_enabled=settings.live_payments_enabled,
                payment_currency=DEFAULT_CURRENCY.lower(),
                blockers=payment_blockers,
            ),
        )


_CONFIG_DETAIL = {
    "no_approved_tax_settings": "No approved business tax configuration is effective today",
    "ambiguous_tax_settings": "More than one approved business tax configuration is effective today",
    "registration_not_approved": "The registration the tax configuration refers to is not approved",
    "registration_not_valid_on_date": "The registration the tax configuration refers to is not valid today",
}

_COUNT_BLOCKERS = (
    ("active_unclassified", "active_products_unclassified", "active product(s) have no tax classification"),
    ("active_classification_not_approved", "active_products_classification_not_approved",
     "active product(s) point at a classification that is not approved"),
    ("active_without_effective_rule", "active_products_without_effective_rule",
     "active product(s) have an approved classification but no approved rule effective today"),
)
