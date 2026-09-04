"""Release-gate ownership for the Phase 8 suite.

The phase-oriented suites deliberately remain intact for lineage. Phase 5, 6, and 7
exercise both deterministic domain behaviour and public/runtime contracts, so they are
double-marked rather than being made to look like one kind of evidence.
"""

from pathlib import Path

import pytest


DOMAIN_FILES = {
    "test_authority.py", "test_commerce_core.py", "test_seed.py",
    "test_store_dialect.py", "test_phase5_checkout.py",
    "test_phase6_merchant.py", "test_phase7_observability.py",
}
CONTRACT_FILES = {
    "test_runtime_contracts.py", "test_phase5_checkout.py",
    "test_phase6_merchant.py", "test_phase7_observability.py",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        filename = Path(str(item.fspath)).name
        if filename in DOMAIN_FILES:
            item.add_marker(pytest.mark.domain)
        if filename in CONTRACT_FILES:
            item.add_marker(pytest.mark.contract)
        if filename == "test_runtime_transcripts.py":
            item.add_marker(pytest.mark.transcript)
