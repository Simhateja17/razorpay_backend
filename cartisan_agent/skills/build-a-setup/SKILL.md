---
name: build-a-setup
description: The customer is assembling several items that must work together — a desk setup, a home theatre, a room of smart devices — rather than buying one thing.
---

1. Establish the anchor first: the device everything else attaches to, and the budget for
   the whole set. Ask once if neither is stated.
2. Search per role, one search per distinct item, all in the same round.
3. Run `check_compatibility` between the anchor and each candidate before presenting
   anything. A set whose parts have not been checked against each other is not a setup.
4. Present the chosen working set with `present_products`, `purpose=setup`, and the
   customer's exact `budget_minor`. Put the anchor first and make each `reason` name
   the role it fills and the verified fact that makes it fit. The server calculates
   the combined total and remaining budget shown above the cards. When two candidates fill the same role, use
   `present_comparison` for those two only.
5. Add nothing until the customer picks. Then add by the `item_ref` of the card they
   chose, one at a time, and say what the running subtotal is.

Keep the set inside the stated budget. If it cannot be done, say what the cheapest
working set comes to and which role is driving the cost; loosening the budget is the
customer's decision, not yours.
