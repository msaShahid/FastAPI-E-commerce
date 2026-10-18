from sqlalchemy import DDL, CheckConstraint, String, event
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.modules.orders.models.order import Order
from app.modules.payments.models.payment import Payment
from app.modules.store.models.guard import CURRENCY_FUNCTIONS, CURRENCY_TRIGGERS
from app.shared.mixins import TimestampMixin


class StoreSettings(Base, TimestampMixin):
    """The store's (deployment's) currency. One row; never changes once an order exists.

    Product prices, shipping and tax amounts are integers in this currency's minor units.
    Each order copies the currency when it is created, so history never depends on this row.
    """

    __tablename__ = "store_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_store_settings_singleton"),
        CheckConstraint("currency ~ '^[a-z]{3}$'", name="ck_store_settings_currency_format"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    currency: Mapped[str] = mapped_column(String(3))


for _function_sql in CURRENCY_FUNCTIONS:
    event.listen(
        Base.metadata,
        "before_create",
        DDL(_function_sql.replace("%", "%%")).execute_if(dialect="postgresql"),
    )

_TABLES = {"store_settings": StoreSettings.__table__, "orders": Order.__table__, "payments": Payment.__table__}
for _name, (_table, _when, _function) in CURRENCY_TRIGGERS.items():
    event.listen(
        _TABLES[_table],
        "after_create",
        DDL(
            f"CREATE TRIGGER {_name} {_when} ON {_table} FOR EACH ROW EXECUTE FUNCTION {_function}()"
        ).execute_if(dialect="postgresql"),
    )
