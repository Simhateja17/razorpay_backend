---
name: restock-decision
description: The operator asks about stock running out, what to reorder, or how much of something to order.
---

A restock is sized by arithmetic over what actually sold, and the arithmetic is shown.

1. Call `get_inventory_alerts`. Each alert carries the units sold in its window, the
   sellable stock, the estimated days of cover, and an estimated restock quantity — each
   with the formula in its `basis` and the operands in its `inputs`.
2. When the operator named a listing rather than a variant, call `get_listing` and work
   per variant: a listing has no stock of its own.
3. Stage with `stage_inventory_action`, one variant at a time, `action` `restock`, and a
   quantity the estimate supports. The rationale names the figures: units sold, the
   window, the current sellable stock, and the cover the order buys.
4. The staging shows its own preview. In the round after it, one sentence: what it
   would order, and that it is waiting on the approval queue.

Say which figures are observed and which are estimated. The restock quantity is an
estimate: it assumes the last window's rate holds, and it knows nothing about lead
times, supplier minimums, or holding cost, because Cartisan records none of them. Say
that when it bears on the decision.

An item that sold nothing in the window has no rate, so it has no cover and no restock
size. That is slow stock, not a stockout risk; say so rather than sizing an order from
a rate of zero.
