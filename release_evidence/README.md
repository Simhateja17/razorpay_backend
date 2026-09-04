# Phase 8 release evidence

Each JSON file in this directory is one independently invoked release gate. A gate is
`passed`, `failed`, or `not_run`; missing live credentials and unavailable services are
never reported as a pass. `claim` uses Cartisan's settled `Claim` shape, so ratios carry
their numerator, denominator, basis, and limitations. Live reports may list correlation
IDs that can be opened through `EvidenceView.journey`; secrets and Razorpay URLs are not
written here.

Run gates from `backend/`:

```sh
.venv/bin/python scripts/run_release_gate.py domain
.venv/bin/python scripts/run_release_gate.py contract
.venv/bin/python scripts/run_release_gate.py transcript
.venv/bin/python scripts/run_release_gate.py supabase
.venv/bin/python scripts/run_release_gate.py razorpay
CARTISAN_BROWSER_QA_COMMAND='your-browser-test-command' \
  .venv/bin/python scripts/run_release_gate.py browser
```

Do not combine these statuses. ADR 0031 makes each gate independently necessary.
