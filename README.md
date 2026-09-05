# Covered On — Baserow Pipeline Plugin (coveredon_pipeline)

Backend-only Baserow 2.3.3 plugin providing pipeline triage and stats
endpoints for the Covered On lead generation system. Reads Leads (table 885)
and Orgs (table 884) via the Baserow REST API with JWT admin auth.

## Install

```bash
# 1. Install plugin (lays down files)
docker exec baserow ./baserow.sh install-plugin \
  --url https://api.github.com/repos/MkInc42/coveredon-baserow-pipeline-plugin/tarball

# 2. Bootstrap pip if first time (one-time per container)
docker exec -u root baserow /baserow/venv/bin/python -m ensurepip

# 3. Install wheel into the real venv
docker exec baserow /baserow/venv/bin/python -m pip install \
  /baserow/data/plugins/coveredon_pipeline/backend

# 4. Restart
docker restart baserow

# 5. Verify
curl http://baserow.dmz.local:8682/api/coveredon_pipeline/ping/
```

## API Endpoints

All endpoints require JWT authentication (DRF `IsAuthenticated`).

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/coveredon_pipeline/ping/` | Health check |
| GET | `/api/coveredon_pipeline/triage/` | Pipeline triage buckets |
| GET | `/api/coveredon_pipeline/stats/` | Aggregate statistics |

### `GET /api/coveredon_pipeline/triage/`

Returns four buckets of leads needing attention:

- **needs_contact**: `has_usable_contact` is false OR `contact_channel_recommendation` is empty
- **send_ready**: `stage=SEND_APPROVED` AND `requires_operator_approval` is false
- **stale**: `updated_at` older than 7 days
- **hot_unworked**: `score=HOT` AND stage not in `(SEND_APPROVED, REPLIED)`

Each bucket returns `row_id`, `org_name`, `stage`, `score`, `channel`, `updated_at`.

### `GET /api/coveredon_pipeline/stats/`

Returns counts grouped by `stage`, `score`, `contact_channel`, plus totals.

## Auth

The plugin reads `BASEROW_ADMIN_EMAIL` and `BASEROW_ADMIN_PASSWORD` from
environment variables, falling back to `/home/black/baserow-dmz/.env`.
It exchanges these for a JWT via the `/api/user/token-auth/` endpoint.

## Repo Layout

```
plugins/
└── coveredon_pipeline/    # folder name = Django app name
    └── backend/
        ├── setup.py       # pip-installable wheel
        └── src/
            └── coveredon_pipeline/
                ├── __init__.py
                ├── apps.py           # AppConfig registers Plugin
                ├── plugins.py         # Plugin subclass w/ get_api_urls()
                └── api/
                    ├── __init__.py
                    ├── urls.py
                    └── views.py       # PingView, TriageView, StatsView
```

## Verification

```bash
cd plugins/coveredon_pipeline
python3 verify.py          # static structure + syntax checks
python3 verify.py --live   # also hits live Baserow API
```

Requires Baserow 2.3.3 running with Leads table (885) and Orgs table (884).
## Updating an already-installed plugin (CRITICAL gotchas)

`install-plugin` WITHOUT `--overwrite` will NOT refresh files ("Found an existing
plugin installed... not overwriting") — the wheel then rebuilds from STALE code.
Also, `pip install <repo-tarball-url>` fails ("neither setup.py nor pyproject.toml
found") because setup.py is nested under plugins/<module>/backend in the archive.

Correct update procedure:

```bash
# 1. refresh files with --overwrite
docker exec baserow ./baserow.sh install-plugin   --overwrite   --url https://api.github.com/repos/MkInc42/coveredon-baserow-pipeline-plugin/tarball/master

# 2. rebuild + reinstall the wheel from the refreshed local path
docker exec baserow /baserow/venv/bin/python -m pip install --force-reinstall --no-deps   /baserow/data/plugins/coveredon_pipeline/backend

# 3. restart
docker restart baserow
```

Gotchas log (learned live):
- tarball/master URLs: pip needs setup.py at the archive root — it is not; use the
  local-path install (step 2) instead.
- install-plugin skips existing dirs without --overwrite (silently keeps OLD code).
- Plugin auth design: endpoints forward the CALLER's JWT to Baserow REST for row
  reads — no admin credentials needed inside the container. JWT is obtained via
  /api/user/token-auth/ with {"username", "email", "password"} (send both keys).
- Container-internal API base is http://localhost/api (Caddy :80), NOT :8682.
