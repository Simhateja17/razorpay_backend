---
name: compatibility-check
description: The customer wants to know whether two items work together, or is buying an accessory for something they already own.
---

Compatibility is decided by `check_compatibility` and by nothing else. Specifications
that look like they line up are not a verdict, and neither is a brand pairing.

1. Resolve both items to catalogue variants first. The item they already own may not be
   in the cart or in this session: search for it, and if Cartisan does not carry it, say
   so and ask which model it is rather than guessing a match.
2. Call `check_compatibility` with the item they have as `base_variant_id` and each
   candidate as `candidate_variant_id`. Send the candidates in one round.
3. Report the verdict as it came back. A blocking finding means the pair does not work;
   say that plainly, in the finding's own words, and do not offer the item anyway with a
   caveat. An advisory finding is worth one clause, not a paragraph.
4. When the check blocks every candidate, search once more for something that would pass
   and check that too before telling them Cartisan has no match.

Present the candidates that passed with `present_products`, the blocked one named in
your text. Never add an item to the cart on the strength of a compatibility question
alone; the customer chooses.
