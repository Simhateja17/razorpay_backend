"""Deterministic estimates, with their formulas and inputs on the outside.

ADR 0017 draws the line these functions sit on. A measured figure is `observed`
and comes from `MetricsRepository`. An `estimated` figure is arithmetic over
observed inputs, and it is only allowed to exist here, where the formula is one
expression, the operands are the values it was given, and both travel with the
answer. Nothing in this module models behaviour: there is no elasticity, no
uplift, no seasonality curve, because Cartisan has measured none of those and a
plausible-looking coefficient would be an invented fact wearing a number's
clothes.

Every function returns a `Claim`, so a figure that reaches the operator can be
recomputed from what the claim carries. The transcript tests do exactly that.
"""

from __future__ import annotations

from .merchant_types import ESTIMATED, Claim

# What "healthy cover" means for this store, in days. It is a stated convention,
# not a measurement, so it appears in the inputs rather than hiding in the formula.
TARGET_COVER_DAYS = 21


def daily_sales_rate(units_sold: int, window_days: int) -> float:
    return units_sold / window_days if window_days > 0 else 0.0


def days_of_cover(*, variant_id: str, sellable: int, units_sold: int, window_days: int) -> Claim:
    """How long current sellable stock lasts at the rate the window actually sold."""
    rate = daily_sales_rate(units_sold, window_days)
    value = round(sellable / rate, 1) if rate > 0 else None
    return Claim(
        key=f"days_of_cover:{variant_id}",
        value=value,
        unit="days",
        claim_kind=ESTIMATED,
        basis="sellable / (units_sold / window_days)",
        inputs={"variant_id": variant_id, "sellable": sellable, "units_sold": units_sold,
                "window_days": window_days, "daily_rate": round(rate, 4)},
        limitations=[
            "Assumes the last window's sales rate continues unchanged; it is not a forecast "
            "of demand and carries no seasonality.",
            "Nothing was sold in this window, so a rate cannot be computed." if rate == 0
            else "A single large order inside the window moves this figure as much as a "
                 "sustained trend does.",
        ],
    )


def restock_quantity(*, variant_id: str, sellable: int, units_sold: int, window_days: int,
                     target_days: int = TARGET_COVER_DAYS) -> Claim:
    """Units that would bring cover up to `target_days` at the observed rate."""
    rate = daily_sales_rate(units_sold, window_days)
    needed = max(0, round(rate * target_days) - sellable) if rate > 0 else 0
    return Claim(
        key=f"restock_quantity:{variant_id}",
        value=needed,
        unit="units",
        claim_kind=ESTIMATED,
        basis="max(0, round(daily_rate * target_cover_days) - sellable)",
        inputs={"variant_id": variant_id, "sellable": sellable, "units_sold": units_sold,
                "window_days": window_days, "daily_rate": round(rate, 4),
                "target_cover_days": target_days},
        limitations=[
            "Sizes an order against the observed sales rate only; it accounts for no lead "
            "time, no supplier minimum, and no holding cost, because Cartisan records none.",
            "A rate of zero produces zero: an item that sold nothing in the window cannot "
            "be sized this way." if rate == 0 else
            f"Target cover of {target_days} days is a stated convention, not a measured "
            "optimum.",
        ],
    )


def revenue_at_current_rate(*, variant_id: str, price_minor: int, units_sold: int,
                            window_days: int, days: int) -> Claim:
    """What `days` more days would take at the price and rate just measured.

    Deliberately not a price-change projection. Cartisan has never run a price
    experiment, so how a new price moves the sales rate is unmeasured; this holds
    the rate fixed and says so, which is the only honest arithmetic available.
    """
    rate = daily_sales_rate(units_sold, window_days)
    value = round(rate * days * price_minor)
    return Claim(
        key=f"revenue_at_current_rate:{variant_id}",
        value=value,
        unit="INR paise",
        claim_kind=ESTIMATED,
        basis="round((units_sold / window_days) * days * price_minor)",
        inputs={"variant_id": variant_id, "price_minor": price_minor, "units_sold": units_sold,
                "window_days": window_days, "daily_rate": round(rate, 4), "days": days},
        limitations=[
            "Holds the observed sales rate fixed. It is not a forecast, and it says nothing "
            "about what a price change would do to that rate — no price experiment has run, "
            "so no elasticity is known.",
            "Ignores stock: it does not check that enough units exist to sell.",
        ],
    )


def stockout_exposure(*, variant_id: str, price_minor: int, units_sold: int, window_days: int,
                      sellable: int, horizon_days: int = TARGET_COVER_DAYS) -> Claim:
    """Revenue the horizon would take that current stock cannot cover."""
    rate = daily_sales_rate(units_sold, window_days)
    demand = rate * horizon_days
    short = max(0.0, demand - sellable)
    return Claim(
        key=f"stockout_exposure:{variant_id}",
        value=round(short * price_minor),
        unit="INR paise",
        claim_kind=ESTIMATED,
        basis="max(0, (daily_rate * horizon_days) - sellable) * price_minor",
        inputs={"variant_id": variant_id, "price_minor": price_minor, "units_sold": units_sold,
                "window_days": window_days, "daily_rate": round(rate, 4),
                "horizon_days": horizon_days, "sellable": sellable,
                "units_short": round(short, 2)},
        limitations=[
            "Counts demand that current stock cannot serve at the observed rate. It assumes "
            "every unserved unit is a lost sale rather than a delayed or substituted one.",
            "Restocking inside the horizon removes this exposure entirely; it is not a loss "
            "that has happened.",
        ],
    )


def price_change_ratio(*, current_minor: int, proposed_minor: int) -> Claim:
    """How far a proposed price moves from the current one. The staging bound is
    checked against this same arithmetic, so the model can see the refusal coming."""
    ratio = abs(proposed_minor - current_minor) / current_minor if current_minor else 0.0
    return Claim(
        key="price_change_ratio",
        value=round(ratio, 4),
        unit="ratio",
        claim_kind=ESTIMATED,
        basis="abs(proposed_minor - current_minor) / current_minor",
        inputs={"current_minor": current_minor, "proposed_minor": proposed_minor},
        limitations=["A size, not an effect: it says nothing about what the change would do."],
    )
