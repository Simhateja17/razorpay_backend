---
name: price-change
description: The operator wants to change a price, discount something, or asks what a listing should cost.
---

A price change is staged against the price the record holds right now, and inside a
stated bound.

1. Call `get_pricing_context` for the exact variant. It returns the current price, the
   price history, the units sold in the window, and the floor and ceiling one change may
   move within. When the operator named a listing, call `get_listing` first and settle
   which variant.
2. Check the price they want against the floor and ceiling before you stage it. A price
   outside them will be refused, and proposing it wastes their turn.
3. Stage with `stage_price_update`. The rationale names the evidence: the current price,
   the units sold and over what window, and what the change is meant to do.
4. In the round after the staging, one sentence: the move, and that the operator decides
   on the approval queue.

What you must not claim: that a lower price will sell more, or by how much. Cartisan has
run no price experiment, so no elasticity is known and no forecast of the effect exists.
`revenue_at_current_rate` holds the observed rate fixed and says so — it is what the
next stretch would take if nothing changed, not a projection of the change.

Cost and margin are not recorded, so the bounds are policy limits on the size of one
change, not margin floors. Never describe a price as protecting margin.

If the operator wants a larger move than the bound allows, say what the bound is and
stage the largest change that fits. Do not stage two changes to add up to one the bound
refuses.
