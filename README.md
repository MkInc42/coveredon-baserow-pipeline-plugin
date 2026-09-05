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
| GET | `/api/coveredon_pipeline/chart/funnel/` | Ordered stage funnel (chart data) |
| GET | `/api/coveredon_pipeline/chart/timeline/` | Leads created per day (chart data) |
| GET | `/api/coveredon_pipeline/chart/channels/` | Contact channel distribution (chart data) |
| POST | `/api/coveredon_pipeline/upload_image/` | Upload image + optionally attach to lead Screenshots |
| POST | `/api/coveredon_pipeline/upload_images/` | Batch upload multiple images to a lead row |

### `GET /api/coveredon_pipeline/triage/`

Returns four buckets of leads needing attention:

- **needs_contact**: `has_usable_contact` is false OR `contact_channel_recommendation` is empty
- **send_ready**: `stage=SEND_APPROVED` AND `requires_operator_approval` is false
- **stale**: `updated_at` older than 7 days
- **hot_unworked**: `score=HOT` AND stage not in `(SEND_APPROVED, REPLIED)`

Each bucket returns `row_id`, `org_name`, `stage`, `score`, `channel`, `updated_at`.

### `GET /api/coveredon_pipeline/stats/`

Returns counts grouped by `stage`, `score`, `contact_channel`, plus totals.

### `POST /api/coveredon_pipeline/upload_image/`

Upload a single image and optionally attach to a lead row's Screenshots gallery (field 8201).

**Request:** `multipart/form-data`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | file | Yes | Image (png/jpg/jpeg/webp, max 10MB) |
| `row_id` | int | Yes | Leads (885) row id |
| `attach` | string | No | `"true"` to attach to Screenshots gallery |
| `screenshot_path` | string | No | Value for screenshot_path field 8175 |

**Response:** `{"uploaded": "<hashed-name>", "attached": bool, "total_screenshots": N}`

**Idempotent:** When `attach=true`, reads the row's current Screenshots, deduplicates by hashed name, appends only if the name is new. A re-POST of the same file returns `attached: false`.

**Errors:** 400 (invalid file/row_id), 404 (row not found), 502 (Baserow upstream failure).

### `POST /api/coveredon_pipeline/upload_images/`

Batch upload multiple images (same `files` field name repeated) to one lead row. Always attaches — no `attach` toggle.

**Request:** `multipart/form-data` with `files[]` + `row_id`.

**Response:** `{"results": [...], "total_uploaded": N, "total_attached": N}`

Each result entry mirrors the single-upload response. Files process sequentially to avoid race conditions.

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
                    └── views.py       # PingView, TriageView, StatsView, UploadImageView, UploadImagesView
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

## Deploy gotchas learned live 2026-09-05 (upload endpoints rollout)

- **uv venv ships WITHOUT pip.** `/baserow/venv/bin/pip` does not exist on a
  fresh 2.3.3 image; `/baserow/venv/bin/python -m pip` fails with
  "No module named pip". Bootstrap FIRST:
  `docker exec -u root baserow /baserow/venv/bin/python -m ensurepip`
- **Files on the volume ≠ wheel in the venv.** install-plugin only lays files.
  If Django's plugin scan finds the dir but the module isn't importable, the
  backend + both celery workers crash-loop with
  `ModuleNotFoundError: No module named 'coveredon_pipeline'` (container stays
  up, frontend serves `connect ECONNREFUSED 127.0.0.1:8000` on every page).
- **Backend-direct vs Caddy routing for uploads.** `localhost:8000/api` (backend
  direct) 404s `POST /api/user-files/upload-file/` — user-file upload is only
  served correctly through in-container Caddy (:80) with the public backend
  Host header. The upload views therefore POST to
  `http://localhost/api/user-files/upload-file/` with
  `Host: $PUBLIC_BACKEND_HOST` (env, default `baserow.dmz.local`). Row/token
  endpoints (`/api/database/...`, `/api/user/token-auth/`) work fine direct on
  `localhost:8000`.
- **Idempotent attach contract (verified live):** upload + attach reads the row
  first, dedupes by hashed file `name`, preserves existing Screenshots entries.
  Re-POSTing the same bytes returns `attached: false` — safe to retry.
- **PATCH format for the file field:** `user_field_names=true` +
  `{"Screenshots": [{"name": "<hashed name from upload response>"}]}`. The
  field REPLACES, never appends — always merge with the row's existing list.
- **Diagnosis speed-run for future crashes:** frontend up + 500 pages saying
  `connect ECONNREFUSED 127.0.0.1:8000` = backend process dead, check compose
  logs for the Python traceback; `ModuleNotFoundError` on the plugin = files
  present but wheel missing from venv (ensurepip + pip install fixes).
- **Dozzle MCP** (`DOZZLE_ENABLE_MCP=true`, endpoint `/api/mcp`) gives
  list_containers/get_container_logs/search_container_logs to agents — but it
  only sees containers on the Docker host it is pointed at. The baserow
  container lives on ext-host-co's own Docker; a Dozzle watching docker-proxy
  will NOT show it.
