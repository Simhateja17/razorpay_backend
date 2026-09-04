"""One lineage per request (ADR 0032).

Every meaningful thing Cartisan does already accepts a `Correlation`; until this
module existed nothing at the edge supplied one, so a browser action, the turn it
started, the checkout that turn staged, the outbox message, the Razorpay attempt
and the webhook that settled it each began a separate story. `EvidenceLedger.
for_correlation` returned a fragment, and a judge following a purchase had to join
it by eye.

The lineage comes from the request when the client sends one and is minted here
when it does not. It is deliberately client-supplied and deliberately powerless: a
correlation id groups evidence and authorizes nothing, which is why it can be
accepted from a header while `customer_id` never can (ADR 0010). Both fields are
bounded and character-restricted before they reach a query, so a hostile header is
a rejected request rather than a row.

`demo_run_id` is the browser's demo session. It is what lets an audit view answer
"show me this run and nothing else" — the acceptance criterion's "without
unrelated-session noise".
"""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi import Header, Response

from marketplace_backend.evidence import Correlation

CORRELATION_HEADER = "X-Cartisan-Correlation-Id"
DEMO_RUN_HEADER = "X-Cartisan-Demo-Run"

# Ids are opaque handles, so the safe set is small on purpose: what our own minting
# produces, plus what a demo run label needs to stay readable.
_SAFE = re.compile(r"^[A-Za-z0-9_:.-]{1,80}$")


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value if value and _SAFE.match(value) else None


def new_correlation_id() -> str:
    return f"corr_{uuid4().hex[:12]}"


def request_correlation(
    response: Response,
    x_cartisan_correlation_id: str | None = Header(default=None),
    x_cartisan_demo_run: str | None = Header(default=None),
) -> Correlation:
    """The lineage for this request, echoed back so the client can continue it.

    A client that keeps sending the id it was handed turns a sequence of separate
    HTTP calls — add to cart, stage, confirm, return from Razorpay — into one
    journey. A client that sends nothing still gets a lineage; it is just a shorter
    one, which is a worse demo but never a wrong record.
    """
    correlation = Correlation(
        correlation_id=_clean(x_cartisan_correlation_id) or new_correlation_id(),
        demo_run_id=_clean(x_cartisan_demo_run),
    )
    response.headers[CORRELATION_HEADER] = correlation.correlation_id
    if correlation.demo_run_id:
        response.headers[DEMO_RUN_HEADER] = correlation.demo_run_id
    return correlation


__all__ = ["CORRELATION_HEADER", "DEMO_RUN_HEADER", "new_correlation_id", "request_correlation"]
