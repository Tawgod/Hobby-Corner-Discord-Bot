from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class ProductDecision:
    migration_status: str
    proposed_reorder_point: float
    proposed_restock_level: float
    review_reason: str


def _months_old(dt: Optional[datetime], now: datetime) -> Optional[float]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta_days = (now - dt).total_seconds() / 86400.0
    return delta_days / 30.4375


def is_reliable_sale_date(last_sold: Optional[datetime]) -> bool:
    return bool(last_sold and last_sold.year >= 2000)


def is_games_workshop(supplier_name: Optional[str], sku: Optional[str]) -> bool:
    supplier = (supplier_name or "").strip().lower()
    code = (sku or "").strip().upper()
    return "games workshop" in supplier or code.startswith("GW")


def decide_product(
    *,
    quantity: float,
    inactive: bool,
    do_not_order: bool,
    last_sold: Optional[datetime],
    last_received: Optional[datetime],
    reorder_point: float,
    restock_level: float,
    supplier_name: Optional[str] = None,
    sku: Optional[str] = None,
    now: Optional[datetime] = None,
) -> ProductDecision:
    """
    Initial migration/reorder rules.

    Important:
    - quantity is a selection signal only and is never an opening inventory value.
    - supplier/category mapping is resolved separately from LS: importer.
    """
    now = now or datetime.now(timezone.utc)
    sale_reliable = is_reliable_sale_date(last_sold)
    sale_age = _months_old(last_sold, now) if sale_reliable else None
    receipt_age = _months_old(last_received, now)

    gw = is_games_workshop(supplier_name, sku)

    if quantity > 0:
        migration_status = "READY"
    elif sale_reliable and sale_age is not None and sale_age <= 12:
        migration_status = "READY"
    elif receipt_age is not None and receipt_age <= 12:
        migration_status = "READY"
    elif sale_reliable and sale_age is not None and sale_age <= 24:
        migration_status = "REVIEW"
    else:
        migration_status = "EXCLUDE"

    suppress_reorder = (
        inactive
        or do_not_order
        or not sale_reliable
        or (sale_age is not None and sale_age > 12)
    )

    proposed_reorder_point = 0 if suppress_reorder else (reorder_point or 0)
    proposed_restock_level = 0 if suppress_reorder else (restock_level or 0)

    reason = ""

    if gw:
        migration_status = "REVIEW" if migration_status != "EXCLUDE" else migration_status
        reason = "Games Workshop - verify current product/description and reused part number"
    elif inactive and quantity > 0:
        reason = "Inactive but RMS shows stock - migrate product, no reorder"
    elif do_not_order and quantity > 0:
        reason = "Do Not Order but RMS shows stock - migrate product, no reorder"
    elif receipt_age is not None and receipt_age <= 12 and (
        not sale_reliable or (sale_age is not None and sale_age > 12)
    ):
        reason = "Recent receipt but aging/no reliable sales history"
    elif quantity > 0 and (not sale_reliable or (sale_age is not None and sale_age > 24)):
        reason = "Aging stock - migrate product for physical inventory"
    elif quantity <= 0 and (not sale_reliable or (sale_age is not None and sale_age > 24)):
        reason = "Old zero-stock product"

    return ProductDecision(
        migration_status=migration_status,
        proposed_reorder_point=float(proposed_reorder_point),
        proposed_restock_level=float(proposed_restock_level),
        review_reason=reason,
    )
