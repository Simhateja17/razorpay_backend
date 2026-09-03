"""Regenerate the commerce-core migration from the shared schema definition.

Run after editing marketplace_backend/core_schema.py so the Postgres migration
and the SQLite test schema stay identical.
"""

from __future__ import annotations

from pathlib import Path

from marketplace_backend.core_schema import to_postgres

TARGET = Path(__file__).resolve().parents[1] / "supabase/migrations/20260904010000_commerce_core.sql"
HEADER = """-- Phase 2: the normalized commerce core (CARTISAN_COMMERCE_ARCHITECTURE.md).
--
-- Generated from marketplace_backend/core_schema.py, which is the single source
-- for this schema; the SQLite schema the tests run against comes from the same
-- text. Regenerate with: python scripts/generate_core_migration.py
"""

if __name__ == "__main__":
    TARGET.write_text(HEADER + to_postgres())
    print(f"wrote {TARGET}")
