"""Regression coverage for catalogue term boundaries."""

from cartisan_agent.core_port import _matches_search_term


def test_phone_does_not_match_headphones() -> None:
    assert not _matches_search_term("phones", "Over-Ear Headphones Personal Audio")
    assert _matches_search_term("phones", "Phone Case Cases and Protection")
