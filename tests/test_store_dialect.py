"""Regressions for the SQLite/Postgres boundary.

Every case here is a bug that passed the SQLite suite and failed only against the
live Supabase project. They are pinned as unit tests because the dialect
translation is the one place where a green test suite can be actively misleading.
"""

import pytest

from marketplace_backend.core_schema import to_postgres, to_sqlite
from marketplace_backend.store import Store
from marketplace_backend.timeutil import as_datetime, is_past


@pytest.fixture
def pg():
    """A Store wired for Postgres SQL generation, without a connection."""
    store = Store.__new__(Store)
    store.backend = "supabase"
    return store


def test_table_names_are_schema_qualified(pg):
    assert pg._postgres_sql("SELECT id FROM commerce_orders") == \
        "SELECT id FROM cartisan.commerce_orders"


def test_a_column_alias_is_not_mistaken_for_a_table(pg):
    """`COUNT(*) AS orders` names a result column, not the orders table."""
    sql = pg._postgres_sql("SELECT COUNT(*) AS orders FROM commerce_events")

    assert "AS orders" in sql
    assert "AS cartisan.orders" not in sql


def test_an_already_qualified_name_is_not_qualified_twice(pg):
    assert "cartisan.cartisan" not in pg._postgres_sql("SELECT id FROM cartisan.orders")


def test_a_literal_percent_is_escaped_for_the_driver(pg):
    """`LIKE 'sd_%'` would otherwise be read as a malformed placeholder."""
    sql = pg._postgres_sql("SELECT id FROM products WHERE id LIKE 'sd_%'")

    assert "'sd_%%'" in sql


def test_percent_escaping_does_not_corrupt_placeholders(pg):
    sql = pg._postgres_sql("SELECT id FROM products WHERE id LIKE 'sd_%' AND price > ?")

    assert sql.endswith("AND price > %s")
    assert "%%s" not in sql


def test_a_word_inside_an_identifier_is_left_alone(pg):
    sql = pg._postgres_sql("SELECT customer_id FROM customer_carts")

    assert sql == "SELECT customer_id FROM cartisan.customer_carts"


# ------------------------------------------------------------ schema text


def test_sqlite_translation_keeps_quoted_domain_values():
    """A CHECK list must keep its values, not have them renamed to SQLite types."""
    sqlite = to_sqlite()

    assert "value_kind in ('text', 'numeric', 'bool')" in sqlite
    assert "value_kind in ('text', 'real', 'bool')" not in sqlite


def test_sqlite_translation_rewrites_actual_types():
    code = "\n".join(line.partition("--")[0] for line in to_sqlite().split("\n"))

    for postgres_only in ("timestamptz", "boolean", " numeric"):
        assert postgres_only not in code, f"{postgres_only} survived translation"


def test_postgres_and_sqlite_declare_the_same_tables():
    """The one definition really does produce both schemas."""
    from marketplace_backend.core_schema import table_names

    postgres = to_postgres()
    for table in table_names():
        assert f"cartisan.{table}" in postgres
        assert f"create table if not exists {table} (" in to_sqlite()


# --------------------------------------------------------------- timestamps


def test_timestamps_compare_across_both_representations():
    """Postgres returns datetimes, SQLite returns ISO strings; deadlines must work
    for both without raising."""
    from datetime import UTC, datetime

    text = "2020-01-01T00:00:00+00:00"
    native = datetime(2020, 1, 1, tzinfo=UTC)

    assert as_datetime(text) == as_datetime(native)
    assert is_past(text) and is_past(native)
    assert not is_past("2999-01-01T00:00:00+00:00")


def test_a_naive_timestamp_is_treated_as_utc():
    assert is_past("2020-01-01T00:00:00")
    assert as_datetime("2020-01-01T00:00:00").tzinfo is not None


def test_a_missing_deadline_is_not_past():
    assert is_past(None) is False
    assert as_datetime(None) is None


# ------------------------------------------------------------- constraints


def test_sqlite_enforces_foreign_keys(tmp_path):
    """Off by default, which would let a test pass against a schema Postgres rejects."""
    store = Store(tmp_path / "fk.db")

    assert store.rows("PRAGMA foreign_keys")[0]["foreign_keys"] == 1
    with pytest.raises(Exception):
        store.execute(
            "INSERT INTO variant_specs (variant_id,spec_key,value_text) VALUES ('nope','k','v')")


def test_booleans_are_written_as_real_booleans():
    """Postgres has a real boolean column and rejects a smallint."""
    from marketplace_backend.seed.generator import CommerceGenerator

    row = CommerceGenerator._typed_spec("v1", "anc", True)

    assert row[5] is True
    assert not isinstance(row[5], int) or isinstance(row[5], bool)
