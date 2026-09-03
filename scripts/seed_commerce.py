"""Generate the seeded merchant. Destructive by design.

    python scripts/seed_commerce.py --dry-run      # counts only, nothing written
    python scripts/seed_commerce.py --reset        # replace every seeded row
    python scripts/seed_commerce.py --validate     # check invariants, change nothing

`--reset` deletes only rows carrying the `sd_` seed prefix, so live demo records
in the same tables survive. It is never implicit: without `--reset`, a database
that already holds seeded rows is left alone.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from marketplace_backend.metrics import MetricsRepository  # noqa: E402
from marketplace_backend.seed import (  # noqa: E402
    GENERATOR_VERSION,
    CommerceGenerator,
    install_scenarios,
    validate_all,
)
from marketplace_backend.store import Store  # noqa: E402


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
    parser.add_argument("--seed", type=int, default=20260904)
    parser.add_argument("--as-of", default="2026-09-04",
                        help="History anchor (YYYY-MM-DD). Fixed so runs are reproducible.")
    parser.add_argument("--sqlite", help="Run against a local SQLite file instead of Supabase.")
    parser.add_argument("--reset", action="store_true",
                        help="Delete every seeded row before generating. Destructive.")
    parser.add_argument("--no-scenarios", action="store_true", help="Skip the named scenario packs.")
    parser.add_argument("--validate", action="store_true",
                        help="Only run the invariant validators against what is already there.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what is present and exit without writing.")
    args = parser.parse_args()

    store = open_store(args.sqlite)
    as_of = datetime.fromisoformat(args.as_of).replace(tzinfo=UTC)
    generator = CommerceGenerator(store, seed=args.seed, as_of=as_of)

    if args.validate or args.dry_run:
        existing = generator.seeded_row_count()
        print(f"backend={store.backend}  seeded rows present={existing}")
        if args.dry_run:
            return 0
        return report_invariants(store)

    existing = generator.seeded_row_count()
    if existing and not args.reset:
        print(f"{existing} seeded rows already exist. Pass --reset to replace them.", file=sys.stderr)
        return 1

    if args.reset and existing:
        print(f"removing {existing} seeded rows…")
        generator.reset()

    print(f"generating with version {GENERATOR_VERSION}, seed {args.seed}, as of {as_of.date()}…")
    world = generator.generate()
    for name, count in world.counts.items():
        print(f"  {name:<24} {count:>7}")

    if not args.no_scenarios:
        print("installing scenario packs…")
        before = generator.snapshot_ids()
        results = install_scenarios(store, world, as_of=as_of)
        captured = generator.capture_scenario_rows(before, world.seed_run_id)
        print(f"  captured {captured} scenario rows for a future reset")
        for key, outcome in results.items():
            print(f"  {key:<32} {json.dumps(outcome, default=str)}")

    status = report_invariants(store)

    metrics = MetricsRepository(store).snapshot(origin="seeded")
    print("\nseeded business snapshot")
    for metric in metrics["metrics"]:
        print(f"  {metric['key']:<32} {metric['value']}")
    return status


def report_invariants(store: Store) -> int:
    print("\ninvariants")
    failed = 0
    for report in validate_all(store):
        print(f"  {report}")
        for problem in report.problems[:5]:
            print(f"      - {problem}")
        if len(report.problems) > 5:
            print(f"      … and {len(report.problems) - 5} more")
        failed += 0 if report.ok else 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
