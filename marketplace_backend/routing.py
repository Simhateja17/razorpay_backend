from __future__ import annotations

import re
from enum import Enum

# Deterministic intent routing (ADR 0021). Explicit checkout intent has precedence
# over every other shopping intent: it is tested first and short-circuits, so
# "complete the purchase" can never reach product search or cart addition.

ADD_TO_CART_RE = re.compile(
    r"\b(?:add|put|include|place)\b(?=.*\b(?:cart|basket|bag)\b)"
    r"|^\s*(?:please\s+)?(?:add|put|include|place)\s+(?!details?\b|information\b)"
    r"|\b(?:buy|purchase|order)\b\s+(?!details?\b|information\b)(?=\w)"
    r"|\bget\b\s+(?:me\s+)?(?:one|an?|the)\b",
    re.IGNORECASE,
)
CHECKOUT_RE = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|alright|sure)[,!]?\s+)?(?:please\s+)?(?:check\s*out|checkout)\s*[.!?]*$"
    r"|\b(?:can|could|would)\s+you\s+(?:please\s+)?(?:check\s*out|checkout)\b"
    r"|\b(?:let(?:'|’)?s|let\s+us)\s+(?:check\s*out|checkout)\b"
    r"|\b(?:complete|finish|finalize|confirm|place)\b(?:\s+\w+){0,3}\s+\b(?:purchase|order|payment|checkout)\b"
    r"|\b(?:pay|paying)\s+(?:for\s+)?(?:my|the|this)\s+(?:cart|order|basket)\b"
    r"|\b(?:proceed|continue|go)\b(?:\s+\w+){0,3}\s+\b(?:checkout|payment)\b"
    r"|\b(?:take|send|bring)\s+me\s+to\s+(?:checkout|payment)\b"
    r"|\b(?:ready|want)\s+to\s+(?:check\s*out|checkout|pay)\b",
    re.IGNORECASE,
)
RELATIVE_ADD_RE = re.compile(r"\b(?:one|it|that|this|them|first|second|third|fourth)\b", re.IGNORECASE)


class Intent(str, Enum):
    CHECKOUT = "checkout"
    ADD_TO_CART = "add_to_cart"
    SEARCH = "search"


def classify(message: str) -> Intent:
    """Resolve one shopping message to exactly one intent, checkout first."""
    if CHECKOUT_RE.search(message):
        return Intent.CHECKOUT
    if ADD_TO_CART_RE.search(message):
        return Intent.ADD_TO_CART
    return Intent.SEARCH


def is_checkout_request(message: str) -> bool:
    return classify(message) is Intent.CHECKOUT


def is_add_request(message: str) -> bool:
    return classify(message) is Intent.ADD_TO_CART


def is_relative_add_request(message: str) -> bool:
    return bool(RELATIVE_ADD_RE.search(message))
