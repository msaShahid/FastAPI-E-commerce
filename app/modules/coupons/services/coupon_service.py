"""Coupons: finding them, deciding whether one applies to an order, and the administrator's management of them.

Rules (the defaults of this store; each is a small change):
  * ONE coupon per order. Codes are case-insensitive. The discount applies to the goods (shipping is waived by a free-shipping coupon).
  * A code that does not exist, is switched off, has not started or has ended gets ONE answer (coupon_invalid).
  * Limits count the orders that carry the coupon and are still alive (pending, paid or fulfilled): a cancelled, expired or refunded order gives
    its use back. A customer is a registered account, or a guest's email address (case-insensitive).
  * At checkout the coupon row is locked while the limits are counted and the order is created, so two simultaneous orders cannot both take
    the last use."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.services.audit_service import AuditService
from app.modules.auth.models.user import User
from app.modules.coupons.exceptions.coupon_exceptions import (
    CouponCodeExistsError,
    CouponInUseError,
    CouponInvalidError,
    CouponLimitReachedError,
    CouponMinimumNotMetError,
    CouponNotApplicableError,
    CouponNotFoundError,
    CouponSettingsInvalidError,
)
from app.modules.coupons.models.coupon import Coupon
from app.modules.coupons.schemas.coupons import CouponCreate, CouponUpdate, validate_value_fields
from app.modules.coupons.services.discount import Discount, compute_discount
from app.modules.orders.models.order import Order
from app.shared.enums.order_status import OrderStatus

ALIVE = (OrderStatus.PENDING, OrderStatus.PAID, OrderStatus.FULFILLED)


def normalise_code(code: str) -> str:
    return code.strip().upper()


async def find_by_code(db: AsyncSession, code: str, *, lock: bool = False) -> Coupon | None:
    query = select(Coupon).where(Coupon.code == normalise_code(code))
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(query)).scalar_one_or_none()


async def uses(db: AsyncSession, coupon_id: int, *, user_id=None, guest_email: str | None = None) -> int:
    """How many live orders carry this coupon (optionally: bought by this customer)."""
    conditions = [Order.coupon_id == coupon_id, Order.status.in_(ALIVE)]
    if user_id is not None:
        conditions.append(Order.user_id == user_id)
    elif guest_email is not None:
        conditions.append(func.lower(Order.guest_email) == guest_email.lower())
    return (await db.execute(select(func.count()).select_from(Order).where(*conditions))).scalar_one()


def status_of(coupon: Coupon, used: int, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    if not coupon.active:
        return "disabled"
    if coupon.starts_at and now < coupon.starts_at:
        return "scheduled"
    if coupon.ends_at and now >= coupon.ends_at:
        return "expired"
    if coupon.max_uses_total is not None and used >= coupon.max_uses_total:
        return "exhausted"
    return "active"


async def evaluate(
    db: AsyncSession, coupon: Coupon | None, *, subtotal_cents: int, shipping_cents: int, user_id=None, guest_email: str | None = None,
    now: datetime | None = None,
) -> Discount:
    """Whether `coupon` applies to an order of this size for this customer, and what it takes off. Raises a specific error otherwise."""
    now = now or datetime.now(UTC)
    if coupon is None or not coupon.active or (coupon.starts_at and now < coupon.starts_at) or (coupon.ends_at and now >= coupon.ends_at):
        raise CouponInvalidError()
    if subtotal_cents < coupon.min_subtotal_cents:
        raise CouponMinimumNotMetError(coupon.min_subtotal_cents)
    if coupon.max_uses_total is not None and await uses(db, coupon.id) >= coupon.max_uses_total:
        raise CouponLimitReachedError()
    if (
        coupon.max_uses_per_customer is not None
        and (user_id is not None or guest_email)
        and await uses(db, coupon.id, user_id=user_id, guest_email=guest_email) >= coupon.max_uses_per_customer
    ):
        raise CouponLimitReachedError()
    discount = compute_discount(
        kind=coupon.kind, percent_off=coupon.percent_off, amount_off_cents=coupon.amount_off_cents,
        max_discount_cents=coupon.max_discount_cents, subtotal_cents=subtotal_cents, shipping_cents=shipping_cents,
    )
    if coupon.kind == "free_shipping" and discount.shipping_cents == 0:
        raise CouponNotApplicableError("Shipping is already free on this order")
    if coupon.kind != "free_shipping" and discount.goods_cents == 0:
        raise CouponNotApplicableError("This coupon would not reduce the price of this order")
    return discount


def snapshot(coupon: Coupon, discount: Discount) -> dict:
    """What an order records about its coupon: the terms as they were, and what it did."""
    return {
        "code": coupon.code, "name": coupon.name, "kind": coupon.kind,
        "percent_off": None if coupon.percent_off is None else str(coupon.percent_off), "amount_off_cents": coupon.amount_off_cents,
        "max_discount_cents": coupon.max_discount_cents, "goods_discount_cents": discount.goods_cents, "shipping_discount_cents": discount.shipping_cents,
    }


class CouponAdminService:
    def __init__(self, db: AsyncSession, audit: AuditService) -> None:
        self.db = db
        self.audit = audit

    async def _get(self, coupon_id: int, *, lock: bool = False) -> Coupon:
        query = select(Coupon).where(Coupon.id == coupon_id)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        coupon = (await self.db.execute(query)).scalar_one_or_none()
        if coupon is None:
            raise CouponNotFoundError()
        return coupon

    async def _ever_used(self, coupon_id: int) -> bool:
        return (await self.db.execute(select(func.count()).select_from(Order).where(Order.coupon_id == coupon_id))).scalar_one() > 0

    async def create(self, payload: CouponCreate, admin: User) -> Coupon:
        coupon = Coupon(created_by_user_id=admin.id, **{**payload.model_dump(), "code": normalise_code(payload.code)})
        try:
            async with self.db.begin_nested():
                self.db.add(coupon)
                await self.db.flush()
        except IntegrityError as exc:
            if "uq_coupons_code" in str(exc.orig):
                raise CouponCodeExistsError() from exc
            raise
        await self.db.refresh(coupon)  # the values as the database holds them (a percentage is stored with two decimals)
        await self.audit.log(actor_user_id=admin.id, action="coupon.created", target_type="coupon", target_id=coupon.id, details={"code": coupon.code, "kind": coupon.kind})
        return coupon

    async def update(self, coupon_id: int, payload: CouponUpdate, admin: User) -> Coupon:
        coupon = await self._get(coupon_id, lock=True)
        changes = payload.model_dump(exclude_unset=True)
        value_fields = {"kind", "percent_off", "amount_off_cents", "max_discount_cents"}
        if value_fields & changes.keys() and await self._ever_used(coupon.id):
            raise CouponInUseError("change what it gives")
        merged = {**{f: getattr(coupon, f) for f in ("kind", "percent_off", "amount_off_cents", "max_discount_cents")}, **{k: v for k, v in changes.items() if k in value_fields}}
        try:
            validate_value_fields(merged["kind"], merged["percent_off"], merged["amount_off_cents"], merged["max_discount_cents"])
        except ValueError as exc:
            raise CouponSettingsInvalidError(str(exc)) from exc
        for key, value in changes.items():
            setattr(coupon, key, value)
        try:
            await self.db.flush()
        except IntegrityError as exc:
            raise CouponSettingsInvalidError("These coupon settings are not consistent (check the dates and limits)") from exc
        await self.db.refresh(coupon)
        await self.audit.log(actor_user_id=admin.id, action="coupon.updated", target_type="coupon", target_id=coupon.id, details={"fields": sorted(changes)})
        return coupon

    async def delete(self, coupon_id: int, admin: User) -> None:
        coupon = await self._get(coupon_id, lock=True)
        if await self._ever_used(coupon.id):
            raise CouponInUseError("delete it")
        code = coupon.code
        await self.db.delete(coupon)
        await self.db.flush()
        await self.audit.log(actor_user_id=admin.id, action="coupon.deleted", target_type="coupon", target_id=coupon_id, details={"code": code})

    async def get(self, coupon_id: int) -> tuple[Coupon, int]:
        coupon = await self._get(coupon_id)
        return coupon, await uses(self.db, coupon.id)

    async def list(self, *, search: str | None, state: str | None, offset: int, limit: int) -> tuple[int, list[tuple[Coupon, int]]]:
        conditions = []
        if search:
            like = f"%{search.strip().upper()}%"
            conditions.append(Coupon.code.like(like) | func.upper(Coupon.name).like(like))
        coupons = list((await self.db.execute(select(Coupon).where(*conditions).order_by(Coupon.created_at.desc(), Coupon.id.desc()))).scalars().all())
        counts = dict((await self.db.execute(
            select(Order.coupon_id, func.count()).where(Order.coupon_id.is_not(None), Order.status.in_(ALIVE)).group_by(Order.coupon_id))).all())
        rows = [(c, counts.get(c.id, 0)) for c in coupons]
        if state:
            rows = [(c, n) for c, n in rows if status_of(c, n) == state]
        return len(rows), rows[offset: offset + limit]

    async def redemptions(self, coupon_id: int, *, offset: int, limit: int):
        await self._get(coupon_id)
        total = (await self.db.execute(select(func.count()).select_from(Order).where(Order.coupon_id == coupon_id))).scalar_one()
        rows = (await self.db.execute(
            select(Order).where(Order.coupon_id == coupon_id).order_by(Order.created_at.desc()).offset(offset).limit(limit))).scalars().all()
        return total, list(rows)
