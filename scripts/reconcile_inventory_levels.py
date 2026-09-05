"""Bring `inventory_levels.on_hand` back in line with the movements that explain it.

    python scripts/reconcile_inventory_levels.py --dry-run
    python scripts/reconcile_inventory_levels.py

`inventory_movements` is append-only and is the record of every change to stock, so
on_hand is a running total of it rather than an independent fact. When the two
disagree, the movements are what actually happened and on_hand is the row that drifted.

This exists because restoring generated inventory rows can reinstate levels that
predate real sales whose movements survived: the sale is still recorded, but the level
it drew down went back up. Reconciling makes the level equal its movements again. It
never invents a movement, so a discrepancy caused by a *missing* movement is reported,
not silently absorbed.

`python scripts/seed_commerce.py --validate` reports the same mismatch under
`inventory`, and should read ok once this has run.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from marketplace_backend.store import Store  # noqa: E402

DRIFT = """
SELECT l.variant_id, l.location_id, l.on_hand, l.reserved, t.total
FROM inventory_levels l
JOIN (SELECT variant_id, location_id, SUM(delta) AS total
      FROM inventory_movements GROUP BY variant_id, location_id) t
  ON t.variant_id = l.variant_id AND t.location_id = l.location_id
WHERE l.on_hand <> t.total
"""


def open_store(sqlite_path: str | None) -> Store:
    if sqlite_path:
        return Store(sqlite_path)
    url = os.getenv("SUPABASE_DATABASE_URL")
    if not url:
        raise SystemExit("Set SUPABASE_DATABASE_URL, or pass --sqlite PATH for a local run.")
    return Store(database_url=url)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sqlite", help="Run against a local SQLite file instead of Supabase.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the drift and exit without writing.")
    args = parser.parse_args()
    store = open_store(args.sqlite)

    drifted = store.rows(DRIFT)
    if not drifted:
        print("nothing to reconcile: every level already equals its movements")
        return 0
    for row in drifted:
        print(f"  {row['variant_id']} @ {row['location_id']}: "
              f"on_hand {row['on_hand']} -> {row['total']}")

    # Stock cannot go below what is already held for confirmed orders, and it cannot go
    # negative. Either would mean the movements themselves are wrong, which is a
    # different problem from a level that drifted, so stop rather than write it.
    for row in drifted:
        if int(row["total"]) < 0:
            raise SystemExit(
                f"{row['variant_id']}: movements sum to {row['total']}, which is negative. "
                "The movements are wrong, not the level; not writing.")
        if int(row["total"]) < int(row["reserved"]):
            raise SystemExit(
                f"{row['variant_id']}: movements sum to {row['total']} but {row['reserved']} "
                "are reserved. Reconciling would oversell held stock; not writing.")

    if args.dry_run:
        print(f"{len(drifted)} level(s) would be reconciled")
        return 0

    for row in drifted:
        store.execute(
            "UPDATE inventory_levels SET on_hand = ? WHERE variant_id = ? AND location_id = ?",
            (int(row["total"]), row["variant_id"], row["location_id"]))
    print(f"reconciled {len(drifted)} level(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
