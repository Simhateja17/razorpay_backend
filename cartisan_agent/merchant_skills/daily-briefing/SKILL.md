---
name: daily-briefing
description: The operator wants an overview of how the store is doing, or asks what needs their attention today.
---

A briefing is a reading of the store's position, not a report of everything readable.

1. In one round, call `get_business_snapshot` and `get_inventory_alerts`. Add
   `get_pending_changes` when anything might be waiting on the operator. These do not
   depend on each other, so they go together.
2. Read the results before you write. The movement figures say what changed; the
   alerts say what is close to running out; the pending list says what is waiting on
   them.
3. Present with `present_digest`, at most five items, each one thing the operator could
   act on. Mark every item's `claim_kind`: a figure a read measured is `observed`, a
   figure a formula produced from measured inputs — days of cover, a restock size — is
   `estimated`. Nothing here is `causal`, so do not say a change in sales was caused by
   anything.
4. Call `present_suggestions` in the same round, with chips that take the next step:
   opening the listing behind an alert, pulling a metric over a longer window.

Lead with what moved against the comparison window and what is closest to stocking out.
An item with no action behind it is not worth a line. If a figure the operator would
expect is not connected — traffic, campaign attribution — say so once rather than
leaving a gap they will read as zero.

Do not stage anything in a briefing unless the operator asked for a change. A briefing
that quietly queues work is a briefing they have to audit.
