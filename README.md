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

Run tests with `pytest`. API docs are available at `http://localhost:8000/docs`.
