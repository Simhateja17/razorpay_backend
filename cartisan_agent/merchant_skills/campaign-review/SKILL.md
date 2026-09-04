---
name: campaign-review
description: The operator asks how a campaign or promotion is doing, or whether to run, pause, or fund one.
---

Campaign reporting is where an invented number is easiest to write and hardest to spot,
so this flow is mostly about what cannot be said.

1. Call `get_campaign_performance`. It returns budget, spend, and the promotion each
   campaign carries. That is what Cartisan records.
2. Attributed orders and attributed revenue are **not recorded**: no order carries the
   campaign that preceded it, and an order stores only a discount amount, never which
   promotion produced it. There is no figure for campaign return, so do not state one,
   do not estimate one, and do not report zero — say the link is not connected.
3. When the operator asks whether a campaign worked, give what is readable: what it
   spent against its budget, and what the store's revenue did over the same window from
   `query_metrics`. Say plainly that these are two figures over one period, not a
   measurement of one causing the other.
4. Present with `present_metrics` for the revenue series and `present_digest` for the
   campaign picture, marking every item `observed`.

A new campaign or promotion goes through `stage_campaign` or `stage_promotion` and waits
for approval like anything else. Nothing you stage starts a campaign or sends anything.
