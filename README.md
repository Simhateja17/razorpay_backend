# Cartisan backend

Python 3.11+ backend for Cartisan. It vendors Anthropic's commerce-agent cores and Agent SDK runtimes, while `marketplace_backend/` owns Cartisan's catalog, bounded carts, Razorpay MCP checkout, approval-gated merchant changes, SQLite state, and audit trail.

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn api.main:app --reload --port 8000
```

Copy `.env.example` to `.env` and supply test credentials. Never commit `.env`.

## Supabase persistence

1. Create a Supabase project and run
   `supabase/migrations/20260903000000_create_cartisan_schema.sql` in its SQL Editor.
2. In **Connect**, copy the transaction-pooler URI and add it to `.env` as
   `SUPABASE_DATABASE_URL`. Keep this value server-side; never put it in a
   `NEXT_PUBLIC_*` variable.
3. Install the updated requirements and restart the API.
4. Confirm `GET http://localhost:8000/health/database` returns
   `{"status":"ok","database":"supabase"}`.

Without `SUPABASE_DATABASE_URL`, the backend intentionally falls back to the
local SQLite database for offline development and tests.

Run tests with `pytest`. API docs are available at `http://localhost:8000/docs`.
