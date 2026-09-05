---
name: campaign-review
description: The operator asks how a campaign or promotion is doing, or whether to run, pause, or fund one.
---

Campaign reporting is where an invented number is easiest to write and hardest to spot,
so this flow is mostly about what cannot be said.

1. Call `get_campaign_performance`. It returns budget, spend, the promotion each campaign
   carries, and — for a campaign that carries one — the orders that redeemed that
   promotion inside the campaign's own window.
2. Attribution here is **redemption, not exposure**. An attributed order is one that
   used the code while the campaign ran. Cartisan records no impression and no click, so
   a customer who saw the campaign and bought without the code is not counted, and one
   who got the code from a friend is. Report it as what it is, and always name the
   window.
3. That figure is descriptive and never causal. Do not call it lift, incremental revenue,
   ROI, or return, and do not say the campaign drove, caused, or produced it. Quote
   attributed revenue and campaign spend as two separate recorded numbers; dividing one
   by the other implies a causal claim the data cannot carry.
4. A campaign with **no promotion code** has no attribution at all: nothing joins an order
   to it. Say the link is not connected — do not state a figure, estimate one, or report
   zero for it.
5. When the operator asks whether a campaign worked, give what is readable: spend against
   budget, what the promotion was redeemed on, and what the store's revenue did over the
   same window from `query_metrics`. Say plainly that these are figures over one period,
   not a measurement of one causing the other.
6. Present with `present_metrics` for the revenue series and `present_digest` for the
   campaign picture, marking every item `observed`.

A new campaign or promotion goes through `stage_campaign` or `stage_promotion` and waits
for approval like anything else. Nothing you stage starts a campaign or sends anything.
