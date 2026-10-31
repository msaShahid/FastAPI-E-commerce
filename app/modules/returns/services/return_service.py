"""Customer returns. See models/return_request.py for the lifecycle and for why money is handled the way it is.

Rules (this store's defaults):
  * Registered customers only; the order must be shipped (fulfilled) and within RETURN_WINDOW_DAYS of delivery (or of dispatch when no
    delivery date is recorded). 0 days switches returns off.
  * A request names order lines and quantities; across all non-rejected, non-cancelled requests a line can never be returned more than it
    was bought. Requests for one order are serialised on the order row, so two simultaneous requests cannot both claim the last unit.
  * Goods that come back can be put back in stock (per line, by the administrator, who has seen their condition), recorded in the stock ledger
    and once only per line and return.
  * Money: NovaCart refunds a WHOLE order through the provider. A return that brings back every unit of the order can start that refund; any
    other return is closed with a recorded resolution (for example refunded outside NovaCart, at the payment provider)."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.stock_context import stock_change
from app.modules.audit.services.audit_service import AuditService
from app.modules.auth.models.user import User
from app.modules.orders.exceptions.order_exceptions import OrderNotFoundError
from app.modules.orders.models.order import Order
from app.modules.orders.models.order_item import OrderItem
from app.modules.orders.models.order_status_history import OrderStatusHistory
from app.modules.orders.repositories.order_repository import OrderRepository
from app.modules.payments.models.refund import Refund
from app.modules.products.repositories.product_repository import ProductRepository
from app.modules.returns.exceptions.return_exceptions import (
    NothingReturnableError,
    OrderNotShippedError,
    ReturnItemInvalidError,
    ReturnNotFoundError,
    ReturnNotFullError,
    ReturnQuantityExceededError,
    ReturnReceiptInvalidError,
    ReturnsDisabledError,
    ReturnStateError,
    ReturnWindowClosedError,
)
from app.modules.returns.models.return_request import ReturnRequest, ReturnRequestItem
from app.modules.returns.schemas.returns import (
    EligibilityItem,
    EligibilityRead,
    RefundSummary,
    ReturnClose,
    ReturnCreate,
    ReturnItemRead,
    ReturnRead,
    ReturnReceive,
    VariantOption,
)
from app.modules.shipments.models.order_shipment import OrderShipment
from app.shared.enums.order_status import OrderStatus

OPEN = ("requested", "approved")
COUNTED = ("requested", "approved", "received", "closed")          # everything that uses up part of a line (not rejected or cancelled)


def _options(item: OrderItem) -> list[VariantOption]:
    return [VariantOption(name=o["name"], value=o["value"]) for o in ((item.variant_snapshot or {}).get("options") or [])]


class ReturnService:
    def __init__(self, db: AsyncSession, audit: AuditService) -> None:
        self.db = db
        self.audit = audit

    # ------------------------------------------------------------------------------------------------ helpers
    async def _locked_order(self, order_id) -> Order:
        order = await OrderRepository(self.db).get_by_id_for_update(order_id)
        if order is None:
            raise OrderNotFoundError(order_id)
        return order

    async def _window_start(self, order: Order) -> datetime:
        delivered = (await self.db.execute(select(func.max(OrderShipment.delivered_at)).where(OrderShipment.order_id == order.id))).scalar_one()
        if delivered is not None:
            return delivered
        shipped = (await self.db.execute(
            select(func.max(OrderStatusHistory.created_at)).where(OrderStatusHistory.order_id == order.id, OrderStatusHistory.to_status == OrderStatus.FULFILLED)
        )).scalar_one()
        start = shipped or order.created_at
        return start if start.tzinfo else start.replace(tzinfo=UTC)

    async def _used(self, order_id) -> dict[int, int]:
        rows = (await self.db.execute(
            select(ReturnRequestItem.order_item_id, func.sum(ReturnRequestItem.quantity))
            .join(ReturnRequest, ReturnRequest.id == ReturnRequestItem.return_id)
            .where(ReturnRequest.order_id == order_id, ReturnRequest.status.in_(COUNTED)).group_by(ReturnRequestItem.order_item_id)
        )).all()
        return {item_id: int(total) for item_id, total in rows}

    async def eligibility(self, order: Order) -> EligibilityRead:
        window = get_settings().return_window_days
        used = await self._used(order.id)
        items = [EligibilityItem(order_item_id=i.id, product_name=i.product_name_snapshot, sku=i.sku_snapshot, options=_options(i),
                                 purchased=i.quantity, returnable=max(i.quantity - used.get(i.id, 0), 0)) for i in order.items]
        ends = None
        reason: Exception | None = None
        if window == 0:
            reason = ReturnsDisabledError()
        elif order.status != OrderStatus.FULFILLED:
            reason = OrderNotShippedError()
        else:
            ends = (await self._window_start(order)) + timedelta(days=window)
            if datetime.now(UTC) > ends:
                reason = ReturnWindowClosedError(window)
            elif not any(i.returnable for i in items):
                reason = NothingReturnableError()
        return EligibilityRead(eligible=reason is None, reason_code=getattr(reason, "code", None), reason=str(reason) if reason else None,
                               window_days=window, window_ends_at=ends, items=items)

    async def _read(self, returns: list[ReturnRequest], *, admin: bool) -> list[ReturnRead]:
        if not returns:
            return []
        line_ids = {i.order_item_id for r in returns for i in r.items}
        lines = {x.id: x for x in (await self.db.execute(select(OrderItem).where(OrderItem.id.in_(line_ids)))).scalars().all()}
        refund_ids = {r.refund_id for r in returns if r.refund_id}
        refunds = {x.id: x for x in (await self.db.execute(select(Refund).where(Refund.id.in_(refund_ids)))).scalars().all()} if refund_ids else {}
        out = []
        for r in returns:
            refund = refunds.get(r.refund_id)
            out.append(ReturnRead(
                id=r.id, order_id=r.order_id, status=r.status, customer_comment=r.customer_comment, rejection_reason=r.rejection_reason,
                resolution=r.resolution, refund=RefundSummary(id=refund.id, status=str(getattr(refund.status, "value", refund.status))) if refund else None,
                requested_at=r.requested_at, decided_at=r.decided_at, received_at=r.received_at, closed_at=r.closed_at,
                items=[ReturnItemRead(
                    id=i.id, order_item_id=i.order_item_id, product_name=lines[i.order_item_id].product_name_snapshot, sku=lines[i.order_item_id].sku_snapshot,
                    options=_options(lines[i.order_item_id]), quantity=i.quantity, reason=i.reason, received_quantity=i.received_quantity,
                    restock=i.restock, restocked_quantity=i.restocked_quantity, condition_note=i.condition_note if admin else None) for i in r.items],
                admin_note=r.admin_note if admin else None, resolution_note=r.resolution_note if admin else None, user_id=r.user_id if admin else None,
            ))
        return out

    async def _get(self, return_id, *, lock: bool = False) -> ReturnRequest:
        query = select(ReturnRequest).where(ReturnRequest.id == return_id)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        ret = (await self.db.execute(query)).scalar_one_or_none()
        if ret is None:
            raise ReturnNotFoundError()
        return ret

    async def _log(self, actor: User, action: str, ret: ReturnRequest, **details) -> None:
        await self.audit.log(actor_user_id=actor.id, action=f"return.{action}", target_type="return", target_id=ret.id, details={"order_id": str(ret.order_id), **details})

    # ------------------------------------------------------------------------------------------------ customers
    async def order_eligibility(self, user: User, order_id) -> EligibilityRead:
        order = await OrderRepository(self.db).get_by_id(order_id)
        if order is None or order.user_id != user.id:
            raise OrderNotFoundError(order_id)
        return await self.eligibility(order)

    async def create(self, user: User, order_id, payload: ReturnCreate) -> ReturnRead:
        order = await self._locked_order(order_id)      # serialises concurrent requests for one order
        if order.user_id != user.id:
            raise OrderNotFoundError(order_id)
        state = await self.eligibility(order)
        if not state.eligible:
            if state.reason_code == "returns_disabled":
                raise ReturnsDisabledError()
            if state.reason_code == "order_not_shipped":
                raise OrderNotShippedError()
            if state.reason_code == "nothing_returnable":
                raise NothingReturnableError()
            raise ReturnWindowClosedError(state.window_days)
        returnable = {i.order_item_id: i.returnable for i in state.items}
        for line in payload.items:
            if line.order_item_id not in returnable:
                raise ReturnItemInvalidError("One of the items is not part of this order")
            if line.quantity > returnable[line.order_item_id]:
                raise ReturnQuantityExceededError(returnable[line.order_item_id])
        ret = ReturnRequest(order_id=order.id, user_id=user.id, customer_comment=payload.comment,
                            items=[ReturnRequestItem(order_item_id=i.order_item_id, quantity=i.quantity, reason=i.reason) for i in payload.items])
        self.db.add(ret)
        await self.db.flush()
        await self._log(user, "requested", ret, items=len(ret.items))
        return (await self._read([ret], admin=False))[0]

    async def mine(self, user: User, offset: int, limit: int, order_id=None):
        conditions = [ReturnRequest.user_id == user.id] + ([ReturnRequest.order_id == order_id] if order_id else [])
        total = (await self.db.execute(select(func.count()).select_from(ReturnRequest).where(*conditions))).scalar_one()
        rows = (await self.db.execute(select(ReturnRequest).where(*conditions).order_by(ReturnRequest.requested_at.desc()).offset(offset).limit(limit))).scalars().all()
        return total, await self._read(list(rows), admin=False)

    async def get_mine(self, user: User, return_id) -> ReturnRead:
        ret = await self._get(return_id)
        if ret.user_id != user.id:
            raise ReturnNotFoundError()
        return (await self._read([ret], admin=False))[0]

    async def cancel(self, user: User, return_id) -> ReturnRead:
        ret = await self._get(return_id, lock=True)
        if ret.user_id != user.id:
            raise ReturnNotFoundError()
        if ret.status not in OPEN:
            raise ReturnStateError(ret.status, "cancelled")
        ret.status, ret.closed_at = "cancelled", datetime.now(UTC)
        await self.db.flush()
        await self._log(user, "cancelled", ret)
        return (await self._read([ret], admin=False))[0]

    # ------------------------------------------------------------------------------------------------ administrators
    async def admin_list(self, *, status: str | None, order_id, from_date, to_date, offset: int, limit: int):
        conditions = []
        if status:
            conditions.append(ReturnRequest.status == status)
        if order_id:
            conditions.append(ReturnRequest.order_id == order_id)
        if from_date:
            conditions.append(func.date(ReturnRequest.requested_at) >= from_date)
        if to_date:
            conditions.append(func.date(ReturnRequest.requested_at) <= to_date)
        total = (await self.db.execute(select(func.count()).select_from(ReturnRequest).where(*conditions))).scalar_one()
        rows = (await self.db.execute(select(ReturnRequest).where(*conditions).order_by(ReturnRequest.requested_at.desc()).offset(offset).limit(limit))).scalars().all()
        return total, await self._read(list(rows), admin=True)

    async def admin_get(self, return_id) -> ReturnRead:
        return (await self._read([await self._get(return_id)], admin=True))[0]

    async def approve(self, return_id, admin: User, note: str | None) -> ReturnRead:
        ret = await self._get(return_id, lock=True)
        if ret.status != "requested":
            raise ReturnStateError(ret.status, "approved")
        ret.status, ret.decided_at, ret.admin_note, ret.handled_by_user_id = "approved", datetime.now(UTC), note, admin.id
        await self.db.flush()
        await self._log(admin, "approved", ret)
        return (await self._read([ret], admin=True))[0]

    async def reject(self, return_id, admin: User, reason: str) -> ReturnRead:
        ret = await self._get(return_id, lock=True)
        if ret.status != "requested":
            raise ReturnStateError(ret.status, "rejected")
        ret.status, ret.decided_at, ret.rejection_reason, ret.handled_by_user_id = "rejected", datetime.now(UTC), reason, admin.id
        await self.db.flush()
        await self._log(admin, "rejected", ret, reason=reason)
        return (await self._read([ret], admin=True))[0]

    async def receive(self, return_id, admin: User, payload: ReturnReceive) -> ReturnRead:
        ret = await self._get(return_id, lock=True)
        if ret.status != "approved":
            raise ReturnStateError(ret.status, "marked as received")
        by_line = {i.order_item_id: i for i in ret.items}
        given = {i.order_item_id: i for i in payload.items}
        if len(given) != len(payload.items) or set(given) != set(by_line):
            raise ReturnReceiptInvalidError("Say what arrived for every line of the return, once each")
        for line_id, entry in given.items():
            if entry.received_quantity > by_line[line_id].quantity:
                raise ReturnReceiptInvalidError("More units arrived than were requested: ask for a new return instead")
            if entry.restock and entry.received_quantity == 0:
                raise ReturnReceiptInvalidError("There is nothing to put back in stock for a line where nothing arrived")
        order = await self._locked_order(ret.order_id)
        lines = {i.id: i for i in order.items}
        for line_id, entry in given.items():
            item = by_line[line_id]
            item.received_quantity, item.restock, item.condition_note = entry.received_quantity, entry.restock, entry.condition_note
        await self._restock(order, ret, lines, admin)
        ret.status, ret.received_at, ret.handled_by_user_id = "received", datetime.now(UTC), admin.id
        await self.db.flush()
        await self._log(admin, "received", ret, restocked=sum(i.restocked_quantity for i in ret.items))
        return (await self._read([ret], admin=True))[0]

    async def _restock(self, order: Order, ret: ReturnRequest, lines: dict[int, OrderItem], admin: User) -> None:
        """Puts the units the administrator approved back in stock: products first, then variants (ascending ids), the same lock order as
        checkout. Recorded once per line and return (event key), in the stock ledger, and capped by the database at what was bought."""
        todo = [i for i in ret.items if i.restock and (i.received_quantity or 0) > 0]
        if not todo:
            return
        products = ProductRepository(self.db)
        locked = await products.get_many_for_update([lines[i.order_item_id].product_id for i in todo])
        variant_ids = [lines[i.order_item_id].variant_id for i in todo if lines[i.order_item_id].variant_id is not None]
        locked_variants = await products.get_variants_for_update(variant_ids) if variant_ids else {}
        try:
            recorded = await OrderRepository(self.db).record_restock_events([(i.order_item_id, f"return:{ret.id}", i.received_quantity) for i in todo])
        except IntegrityError as exc:
            raise ReturnReceiptInvalidError("That would put back more units than the order contained") from exc
        stock_back: dict[int, int] = defaultdict(int)
        variant_back: dict[int, int] = defaultdict(int)
        for item in todo:
            if item.order_item_id not in recorded:
                continue
            line = lines[item.order_item_id]
            item.restocked_quantity = item.received_quantity
            if line.variant_id is not None and line.variant_id in locked_variants:
                variant_back[line.variant_id] += item.received_quantity
            elif line.variant_id is None and line.product_id in locked:
                stock_back[line.product_id] += item.received_quantity
        async with stock_change(products, "restock", order_id=order.id, actor_user_id=admin.id, reason=f"customer return {ret.id}"):
            await products.adjust_stock_many(locked, dict(stock_back))
            if variant_back:
                await products.adjust_variant_stock_many(locked_variants, dict(variant_back))

    async def refund(self, return_id, admin: User, lifecycle) -> ReturnRead:
        """Starts the order's (whole) refund, when this return and the others received for the order bring back every unit of it."""
        ret = await self._get(return_id, lock=True)
        if ret.status != "received":
            raise ReturnStateError(ret.status, "refunded")
        order = await self._locked_order(ret.order_id)
        received = (await self.db.execute(
            select(ReturnRequestItem.order_item_id, func.sum(ReturnRequestItem.received_quantity))
            .join(ReturnRequest, ReturnRequest.id == ReturnRequestItem.return_id)
            .where(ReturnRequest.order_id == order.id, ReturnRequest.status.in_(("received", "closed"))).group_by(ReturnRequestItem.order_item_id)
        )).all()
        back = {line_id: int(total or 0) for line_id, total in received}
        if any(back.get(item.id, 0) < item.quantity for item in order.items):
            raise ReturnNotFullError()
        refund, _created = await lifecycle.request_refund(order.id, admin.id)
        ret.status, ret.resolution, ret.refund_id, ret.closed_at, ret.handled_by_user_id = "closed", "refunded", refund.id, datetime.now(UTC), admin.id
        await self.db.flush()
        await self._log(admin, "refund_requested", ret, refund_id=str(refund.id))
        return (await self._read([ret], admin=True))[0]

    async def close(self, return_id, admin: User, payload: ReturnClose) -> ReturnRead:
        ret = await self._get(return_id, lock=True)
        if ret.status not in ("approved", "received"):
            raise ReturnStateError(ret.status, "closed")
        ret.status, ret.resolution, ret.resolution_note = "closed", payload.resolution, payload.note
        ret.closed_at, ret.handled_by_user_id = datetime.now(UTC), admin.id
        await self.db.flush()
        await self._log(admin, "closed", ret, resolution=payload.resolution)
        return (await self._read([ret], admin=True))[0]
