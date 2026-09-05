"""Regression coverage for catalogue term boundaries."""

from cartisan_agent.core_port import _matches_search_term


def test_phone_does_not_match_headphones() -> None:
    assert not _matches_search_term("phones", "Over-Ear Headphones Personal Audio")
    assert _matches_search_term("phones", "Phone Case Cases and Protection")


def test_exact_plural_outranks_stemmed_singular_only_match() -> None:
    """A search for "earbuds" must rank the actual earbuds above an "Earbud Case" —
    otherwise a tie on relevance lets the accessory crowd the real product out of a
    small result page (the bug behind the live "we don't sell earbuds" incident)."""
    assert _matches_search_term(
        "earbuds", "aster slate wireless earbuds personal audio"
    ) > _matches_search_term("earbuds", "nimbus slate earbud case cases & protection")
