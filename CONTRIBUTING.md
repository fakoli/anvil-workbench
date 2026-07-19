# Contributing to Anvil Workbench

## Local setup

```powershell
python -m pip install -e ".[dev]"
Set-Location web
npm ci
```

Use `.env.example` as a starting point for an untracked `.env`. For a local browser/API smoke test, keep the Compose bind on `127.0.0.1`, set `WORKBENCH_ALLOW_INSECURE_DEV_ACTOR=true`, and provide non-production database and Neo4j passwords. `ANVIL_ROUTER_TOKEN` remains an environment variable; do not commit it.

## Validation

Run these before opening a pull request:

```powershell
python -m pytest -q
Set-Location web
npm run build
Set-Location ..
docker compose config -q
```

For an end-to-end local check, start the Compose stack, wait for `http://127.0.0.1:8090/healthz`, and exercise the Delivery view in a real browser. The production tailnet identity proxy is intentionally not emulated by the browser; local development uses the explicitly opt-in owner fallback.

## Pull requests

- Keep a PR limited to one operational or product concern.
- Explain how State, Serving, bridge, approval, and graph boundaries are affected.
- State whether UI validation used the production proxy path or the loopback-only development fallback.
- Never commit `.env`, bridge tokens, API keys, model credentials, database dumps, raw transcripts, or Neo4j data.
- Do not merge a PR that changes model policy, State acceptance, or deployment authorization without a human review.
