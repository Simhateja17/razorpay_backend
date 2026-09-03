"""Derived metrics, computed from the append-only event log (ADR 0018).

Nothing here keeps a running total. Every figure is recomputed from
`commerce_events` and the transactional tables, which is what lets the merchant
surface show its working — and what keeps a metric from drifting away from the
records it claims to summarise.

Every method takes an `origin` filter, because a seeded ninety-day history and a
live demo purchase must never be silently added together (ADR 0032).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .store import Store

ORIGINS = ("seeded", "live_app", "razorpay_test")


@dataclass
class Metric:
    """A number with its provenance attached, so a claim can be checked."""

    key: str
    value: float | int | None
    unit: str
    basis: str                                  # what it was computed from
    inputs: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"key": self.key, "value": self.value, "unit": self.unit, "basis": self.basis,
                "inputs": self.inputs, "limitations": self.limitations}


class MetricsRepository:
    def __init__(self, store: Store) -> None:
        self.store = store

    def _origins(self, origin: str | tuple[str, ...] | None) -> tuple[str, ...]:
        if origin is None:
            return ORIGINS
        values = (origin,) if isinstance(origin, str) else tuple(origin)
        unknown = set(values) - set(ORIGINS)
        if unknown:
            raise ValueError(f"unknown origin(s) {sorted(unknown)}; expected {ORIGINS}")
        return values

    def _day_expr(self, column: str) -> str:
        """Truncate a timestamp to a calendar day in whichever dialect is live.

        SQLite keeps ISO text, so a substring is the day; Postgres has a real
        timestamptz, so it needs a cast.
        """
        return f"SUBSTR({column},1,10)" if self.store.backend == "sqlite" else f"CAST({column} AS date)"

    def _window(self, days: int | None) -> tuple[str, tuple]:
        if not days:
            return "", ()
        # Compared in SQL so it works on both backends: SQLite stores ISO text,
        # which sorts chronologically, and Postgres casts the bound parameter.
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        return " AND occurred_at >= ?", (cutoff,)

    # ------------------------------------------------------------- headline

    def revenue(self, *, origin: str | tuple[str, ...] | None = "seeded",
                days: int | None = None) -> Metric:
        """Paid revenue, net of refunds, from the event log."""
        origins = self._origins(origin)
        placeholders = ",".join("?" for _ in origins)
        window, window_params = self._window(days)
        paid = self.store.rows(
            f"SELECT COALESCE(SUM(amount_minor),0) AS total, COUNT(*) AS n FROM commerce_events "
            f"WHERE event_type='order_paid' AND origin IN ({placeholders}){window}",
            origins + window_params)[0]
        refunded = self.store.rows(
            f"SELECT COALESCE(SUM(amount_minor),0) AS total, COUNT(*) AS n FROM commerce_events "
            f"WHERE event_type='order_refunded' AND origin IN ({placeholders}){window}",
            origins + window_params)[0]
        net = int(paid["total"]) - int(refunded["total"])
        return Metric(
            key="net_revenue_minor", value=net, unit="INR paise",
            basis="SUM(order_paid) - SUM(order_refunded) over commerce_events",
            inputs={"gross_minor": int(paid["total"]), "paid_orders": int(paid["n"]),
                    "refunded_minor": int(refunded["total"]), "refunds": int(refunded["n"]),
                    "origins": list(origins), "window_days": days},
            limitations=["Refunds are netted on the day they completed, not the day of the sale."])

    def orders(self, *, origin: str | tuple[str, ...] | None = "seeded",
               days: int | None = None) -> Metric:
        origins = self._origins(origin)
        placeholders = ",".join("?" for _ in origins)
        window, window_params = self._window(days)
        row = self.store.rows(
            f"SELECT COUNT(*) AS n FROM commerce_events WHERE event_type='order_paid' "
            f"AND origin IN ({placeholders}){window}", origins + window_params)[0]
        return Metric(key="paid_orders", value=int(row["n"]), unit="orders",
                      basis="COUNT(order_paid) over commerce_events",
                      inputs={"origins": list(origins), "window_days": days})

    def average_order_value(self, *, origin: str | tuple[str, ...] | None = "seeded",
                            days: int | None = None) -> Metric:
        revenue = self.revenue(origin=origin, days=days)
        orders = self.orders(origin=origin, days=days)
        value = round(revenue.inputs["gross_minor"] / orders.value) if orders.value else None
        return Metric(
            key="average_order_value_minor", value=value, unit="INR paise",
            basis="gross paid revenue / paid order count",
            inputs={"gross_minor": revenue.inputs["gross_minor"], "paid_orders": orders.value,
                    "origins": revenue.inputs["origins"], "window_days": days},
            limitations=["Computed on gross revenue, so refunded orders still count."]
            if orders.value else ["No paid orders in this window."])

    def conversion(self, *, origin: str | tuple[str, ...] | None = "seeded",
                   days: int | None = None) -> Metric:
        """Paid orders as a share of orders created. Not sessions — we only count
        what the event log actually recorded."""
        origins = self._origins(origin)
        placeholders = ",".join("?" for _ in origins)
        window, window_params = self._window(days)
        created = int(self.store.rows(
            f"SELECT COUNT(*) AS n FROM commerce_events WHERE event_type='order_created' "
            f"AND origin IN ({placeholders}){window}", origins + window_params)[0]["n"])
        paid = int(self.store.rows(
            f"SELECT COUNT(*) AS n FROM commerce_events WHERE event_type='order_paid' "
            f"AND origin IN ({placeholders}){window}", origins + window_params)[0]["n"])
        return Metric(
            key="checkout_conversion_rate", value=round(paid / created, 4) if created else None,
            unit="ratio", basis="COUNT(order_paid) / COUNT(order_created) over commerce_events",
            inputs={"orders_created": created, "orders_paid": paid, "origins": list(origins),
                    "window_days": days},
            limitations=[
                "This is checkout conversion, not visit-to-purchase conversion: "
                "browsing sessions that never reached an order are not in the denominator."])

    # -------------------------------------------------------- attribution

    def agent_assisted_revenue(self, *, origin: str | tuple[str, ...] | None = "seeded") -> Metric:
        """Revenue on order lines with unbroken recommendation lineage.

        Descriptive, never causal: this is what customers bought after accepting a
        recommendation, not what the recommendation caused them to buy (ADR 0019).
        """
        origins = self._origins(origin)
        placeholders = ",".join("?" for _ in origins)
        row = self.store.rows(
            f"SELECT COALESCE(SUM(l.amount_minor),0) AS total, COUNT(*) AS n "
            f"FROM commerce_order_lines l "
            f"JOIN commerce_orders o ON o.id = l.order_id "
            f"JOIN recommendations r ON r.id = l.recommendation_id "
            f"WHERE o.status='paid' AND o.origin IN ({placeholders}) "
            f"AND r.accepted_at IS NOT NULL AND r.variant_id = l.variant_id", origins)[0]
        presented = int(self.store.rows(
            "SELECT COUNT(*) AS n FROM recommendations")[0]["n"])
        accepted = int(self.store.rows(
            "SELECT COUNT(*) AS n FROM recommendations WHERE accepted_at IS NOT NULL")[0]["n"])
        return Metric(
            key="agent_assisted_revenue_minor", value=int(row["total"]), unit="INR paise",
            basis="paid order lines whose recommendation was presented, accepted, and for the "
                  "same variant",
            inputs={"attributed_lines": int(row["n"]), "recommendations_presented": presented,
                    "recommendations_accepted": accepted, "origins": list(origins)},
            limitations=[
                "Descriptive, not causal: it does not claim these purchases would not have "
                "happened without the recommendation.",
                "An accepted recommendation on an unpaid order contributes nothing."])

    # ---------------------------------------------------------- operations

    def inventory_alerts(self, *, threshold: int = 5, limit: int = 25) -> list[dict]:
        """Variants whose sellable stock has fallen to or below the threshold."""
        return self.store.rows(
            "SELECT l.variant_id, v.title, SUM(l.on_hand - l.reserved) AS sellable "
            "FROM inventory_levels l JOIN catalog_variants v ON v.id = l.variant_id "
            "GROUP BY l.variant_id, v.title HAVING SUM(l.on_hand - l.reserved) <= ? "
            "ORDER BY sellable, l.variant_id LIMIT ?", (threshold, limit))

    def checkout_health(self, *, origin: str | tuple[str, ...] | None = "seeded") -> dict:
        """Where confirmed checkouts end up. The failure column is the point."""
        origins = self._origins(origin)
        placeholders = ",".join("?" for _ in origins)
        rows = self.store.rows(
            f"SELECT status, COUNT(*) AS n FROM commerce_orders "
            f"WHERE origin IN ({placeholders}) GROUP BY status ORDER BY status", origins)
        attempts = self.store.rows(
            "SELECT status, COUNT(*) AS n FROM payment_attempts GROUP BY status ORDER BY status")
        return {"orders_by_status": {row["status"]: int(row["n"]) for row in rows},
                "attempts_by_status": {row["status"]: int(row["n"]) for row in attempts}}

    def daily_revenue(self, *, origin: str | tuple[str, ...] | None = "seeded",
                      limit: int = 90) -> list[dict]:
        """A day-by-day series, for the merchant surface to draw."""
        origins = self._origins(origin)
        placeholders = ",".join("?" for _ in origins)
        return self.store.rows(
            f"SELECT {self._day_expr('occurred_at')} AS day, COUNT(*) AS orders, "
            f"COALESCE(SUM(amount_minor),0) AS revenue_minor FROM commerce_events "
            f"WHERE event_type='order_paid' AND origin IN ({placeholders}) "
            f"GROUP BY {self._day_expr('occurred_at')} ORDER BY day DESC LIMIT ?",
            origins + (limit,))

    def snapshot(self, *, origin: str | tuple[str, ...] | None = "seeded",
                 days: int | None = None) -> dict:
        """Everything the merchant digest needs, each figure carrying its basis."""
        return {
            "currency": "INR",
            "metrics": [m.as_dict() for m in (
                self.revenue(origin=origin, days=days),
                self.orders(origin=origin, days=days),
                self.average_order_value(origin=origin, days=days),
                self.conversion(origin=origin, days=days),
                self.agent_assisted_revenue(origin=origin),
            )],
            "checkout_health": self.checkout_health(origin=origin),
            "inventory_alerts": self.inventory_alerts(),
        }
