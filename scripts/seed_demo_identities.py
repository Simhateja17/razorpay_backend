"""Create the pre-created demo identities in Supabase Auth.

Pre-created identities keep the demo focused without weakening the production
boundary: they are ordinary Supabase accounts signing in with a password, and the
backend derives their principal from a verified token exactly as it would for any
other customer. Run this once per environment.

    python scripts/seed_demo_identities.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# (email, password, display name, role). The role goes into Supabase *app* metadata,
# which the client cannot set — `require_operator` reads it from the verified token
# and never from the request, so this list grants a demo account its surface without
# weakening the boundary (ADR 0010).
IDENTITIES = [
    ("ira@example.com", "cartisan-demo-shopper", "Ira Menon", "customer"),
    ("dev@example.com", "cartisan-demo-shopper", "Dev Rao", "customer"),
    # Without an operator the merchant portal, the approval queue and the operations
    # views cannot be opened at all: every one of them is behind `require_operator`.
    ("maya@example.com", "cartisan-demo-operator", "Maya Iyer", "merchant_operator"),
]


def main() -> int:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    service_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not service_key:
        print("Set SUPABASE_URL and SUPABASE_SERVICE_KEY in backend/.env first.", file=sys.stderr)
        return 1

    # The admin endpoint creates already-confirmed users, so the demo does not
    # depend on an e-mail round-trip. The service key stays server-side: it is
    # only ever used by this script, never by the API or the browser.
    headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}",
               "Content-Type": "application/json"}
    failures = 0
    for email, password, display_name, role in IDENTITIES:
        response = httpx.post(
            f"{url}/auth/v1/admin/users",
            headers=headers,
            json={"email": email, "password": password, "email_confirm": True,
                  "user_metadata": {"display_name": display_name},
                  "app_metadata": {"cartisan_role": role}},
            timeout=20,
        )
        if response.status_code < 300:
            print(f"created: {email}")
            continue
        body = response.text
        if "already" in body.lower() or "exists" in body.lower():
            print(f"already exists: {email}")
            continue
        failures += 1
        print(f"FAILED {email}: {response.status_code} {body}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
