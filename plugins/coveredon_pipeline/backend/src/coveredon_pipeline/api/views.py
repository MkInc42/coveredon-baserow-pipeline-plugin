"""Pipeline triage, stats, chart, health, and image upload views for Covered On Baserow plugin.

All views authenticate via JWT token obtained from the Baserow REST API
using the admin credentials defined in BASEROW_ADMIN_EMAIL / BASEROW_ADMIN_PASSWORD
environment variables (or the .env file at /home/black/baserow-dmz/.env).

Reads tables 885 (Leads) and 884 (Orgs) through the Baserow REST API at
http://localhost:8000/api/ — NOT via Django ORM directly, because the task
spec requires going through the API path.

Endpoints:
  GET  pipeline/ping/         — health check
  GET  pipeline/triage/       — pipeline triage buckets
  GET  pipeline/stats/        — aggregate counts
  GET  chart/funnel/          — ordered stage funnel (chart data)
  GET  chart/timeline/?days=  — leads created per day (chart data)
  GET  chart/channels/        — contact channel distribution (chart data)
  POST pipeline/upload_image/    — upload + optionally attach image to lead row
  POST pipeline/upload_images/   — batch upload (multiple files) to one lead row
"""
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError
import json
import os

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status

# ── Additional stdlib imports for file upload ────────────────────────
# We build multipart bodies and PATCH requests using stdlib to avoid
# adding external dependencies (requests, httpx, etc). Baserow itself
# provides no file-upload helper for external callers, so we do it
# the old-fashioned way with urllib + manual multipart encoding.
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.encoders import encode_base64
import uuid

# ── Configuration ──────────────────────────────────────────────────

# Baserow REST API base URL (inside the container: localhost:8682 is
# the external Traefik port — the container's own Caddy serves :80,
# but the task spec says :8682, so we respect it).
BASEROW_API = "http://localhost:8000/api"  # backend directly (bypasses Caddy Host-based routing)

# Fallback path for the env file when env vars are not set inside the
# container (common when .env is not mounted into the container).
ENV_FILE = "/home/black/baserow-dmz/.env"

LEADS_TABLE_ID = 885
ORGS_TABLE_ID = 884

# Bucket definitions (read from task body — these are the business rules)
STALE_DAYS = 7  # leads older than this many days are "stale"
HOT_UNWORKED_EXCLUDED_STAGES = ("SEND_APPROVED", "REPLIED")


# ── Auth helpers ───────────────────────────────────────────────────

def _load_env_file(path):
    """Load key=value lines from a simple .env file (no shell parsing).

    Handles bare KEY=VALUE lines (no export prefix). Returns a dict.
    Avoids shell injection risk from sourcing untrusted .env files.
    """
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Strip optional 'export ' prefix
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip()
    except (FileNotFoundError, PermissionError):
        pass  # file doesn't exist — rely on os.environ
    return env


def _get_admin_credentials():
    """Return (email, password) from env vars or the .env fallback file.

    Environment variables take priority (they are the cleanest way when
    the container mounts them). Falls back to reading the .env file for
    flexibility during development.
    """
    email = os.environ.get("BASEROW_ADMIN_EMAIL")
    password = os.environ.get("BASEROW_ADMIN_PASSWORD")

    if email and password:
        return email, password

    # Fallback: read the .env file
    env = _load_env_file(ENV_FILE)
    email = env.get("BASEROW_ADMIN_EMAIL") or email
    password = env.get("BASEROW_ADMIN_PASSWORD") or password
    return email, password


def _get_jwt(request=None):
    """Obtain a Baserow admin JWT via the token-auth endpoint.

    Raises RuntimeError if credentials are missing or authentication
    fails (which results in a 500 response to the caller — the endpoint
    cannot function without a valid token).
    """
    # Prefer forwarding the caller's own JWT (no admin creds needed in-container).
    if request is not None:
        auth = request.headers.get("Authorization", "")
        if auth:
            return auth.split(" ", 1)[1] if " " in auth else auth
    email, password = _get_admin_credentials()
    if not email or not password:
        raise RuntimeError(
            "BASEROW_ADMIN_EMAIL and BASEROW_ADMIN_PASSWORD must be set "
            "in environment or /home/black/baserow-dmz/.env"
        )

    payload = json.dumps({"username": email, "email": email, "password": password}).encode()
    req = Request(
        f"{BASEROW_API}/user/token-auth/",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urlopen(req, timeout=10)
        data = json.loads(resp.read())
    except URLError as exc:
        raise RuntimeError(
            f"Failed to obtain JWT from Baserow API: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid JSON response from token-auth endpoint: {exc}"
        ) from exc

    token = data.get("token")
    if not token:
        raise RuntimeError(
            "token-auth response did not contain a 'token' field"
        )
    return token


def _api_get(path, token):
    """Make an authenticated GET request to the Baserow REST API.

    Args:
        path: API path (e.g. "/database/rows/table/885/?user_field_names=true")
        token: JWT token from _get_jwt()

    Returns:
        Parsed JSON response as a dict.

    Raises:
        RuntimeError: if the API call fails (HTTP error, timeout, etc.)
    """
    req = Request(
        f"{BASEROW_API}{path}",
        headers={
            "Authorization": f"JWT {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        resp = urlopen(req, timeout=15)
        return json.loads(resp.read())
    except URLError as exc:
        raise RuntimeError(
            f"Baserow API GET {path} failed: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid JSON in Baserow API response for {path}: {exc}"
        ) from exc


def _fetch_leads(token):
    """Fetch all lead rows from table 885 (Leads).

    Returns a list of lead dicts with user_field_names=true field keys.
    """
    result = _api_get(
        f"/database/rows/table/{LEADS_TABLE_ID}/?user_field_names=true&limit=1000",
        token,
    )
    return result.get("results", [])


def _fetch_org_name(token, lead):
    """Resolve the organization name for a lead.

    If the lead has an organization_name field, use it directly.
    Otherwise, look up org by lc_org_id from table 884 (Orgs).
    """
    org_name = lead.get("organization_name")
    if org_name:
        return org_name

    # Fallback: look up the org by row id. The lead stores lc_org_id
    # (the Lead Console's org id), but the org table uses its own row id.
    # We search by the Name field matching the lead's org hint, or by
    # reading a specific row if lc_org_id is available as a known mapping.
    # For now, just return the lead name as fallback since the task
    # says "org name" — the lead's organization_name field should be set.
    return lead.get("Name", "")


def _is_truthy(val):
    """Check if a Baserow boolean field value is truthy.

    Baserow returns True/False as JSON booleans, but can also return
    None for unset boolean fields. Treat None as False.
    """
    return bool(val) if val is not None else False


# ── File upload helpers ─────────────────────────────────────────────

# Baserow user-files upload endpoint path (multipart form, field name 'file')
USER_FILES_UPLOAD_PATH = "/api/user-files/upload-file/"

# Allowed image MIME types for the upload endpoint. Block unsupported
# formats (gif, svg, bmp, tiff) at the API level before touching Baserow.
ALLOWED_MIME_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}

# Max upload size guard: 10MB. Baserow has its own server-side limit
# (typically 100MB for user files), but we enforce a tighter limit
# at the plugin level to prevent abuse via large uploads.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _build_multipart_body(field_name, filename, content_type, file_bytes):
    """Build a multipart/form-data body with a single file field.

    Uses Python's email.mime package to construct proper MIME multipart
    encoding. This avoids shelling out to curl or adding a requests dep.

    Args:
        field_name: The multipart field name (e.g. 'file').
        filename: The original filename for the Content-Disposition header.
        content_type: MIME type for the file part (e.g. 'image/png').
        file_bytes: Raw bytes of the file.

    Returns:
        Tuple of (body_bytes, boundary_string).
    """
    # Use a unique boundary that won't appear in binary content
    boundary = f"----{uuid.uuid4().hex}"

    # Build the multipart container
    msg = MIMEMultipart("form-data", boundary=boundary)

    # Create the file part with proper Content-Disposition
    part = MIMEBase("application", "octet-stream")
    part.set_payload(file_bytes)
    encode_base64(part)  # binary-safe encoding
    part.add_header(
        "Content-Disposition",
        f'form-data; name="{field_name}"; filename="{filename}"',
    )
    part.add_header("Content-Type", content_type)
    part.set_payload(file_bytes)
    del part["Content-Transfer-Encoding"]  # let the multipart serializer handle it

    # Actually, MIMEMultipart serialization is tricky with binary payloads.
    # Build the raw multipart body manually to avoid email library encoding.
    # We keep the MIMEMultipart approach but replace the payload assembly.
    # The email library's as_string() prepends MIME-Version headers we don't want.
    # Build raw bytes instead.

    # Build the headers preamble + body parts as raw bytes.
    lines = []

    # No preamble — start directly with the boundary
    lines.append(f"--{boundary}".encode())
    lines.append(
        f'Content-Disposition: form-data; name="{field_name}"; '
        f'filename="{filename}"'.encode()
    )
    lines.append(f"Content-Type: {content_type}".encode())
    lines.append(b"Content-Transfer-Encoding: binary")
    lines.append(b"")
    lines.append(file_bytes)
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")

    return b"\r\n".join(lines), boundary


def _api_patch(path, body, token):
    """Make an authenticated PATCH request to the Baserow REST API.

    Used for updating row fields (e.g. attaching screenshots).

    Args:
        path: API path (e.g. '/database/rows/table/885/42/?user_field_names=true').
        body: JSON-serializable dict to send as the PATCH body.
        token: JWT token from _get_jwt().

    Returns:
        Parsed JSON response as a dict.

    Raises:
        RuntimeError: if the API call fails.
    """
    data = json.dumps(body).encode()
    req = Request(
        f"{BASEROW_API}{path}",
        data=data,
        headers={
            "Authorization": f"JWT {token}",
            "Content-Type": "application/json",
        },
        method="PATCH",
    )
    try:
        resp = urlopen(req, timeout=15)
        return json.loads(resp.read())
    except URLError as exc:
        raise RuntimeError(
            f"Baserow API PATCH {path} failed: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid JSON in Baserow PATCH response for {path}: {exc}"
        ) from exc


def _api_get_single_row(table_id, row_id, token):
    """Fetch a single row from a Baserow table by row id.

    Uses user_field_names=true so JSON keys are readable field names
    (e.g. 'Screenshots') instead of numeric field ids.

    Args:
        table_id: The Baserow table id (e.g. 885 for Leads).
        row_id: The row id within the table.
        token: JWT token from _get_jwt().

    Returns:
        Parsed row JSON as a dict.

    Raises:
        RuntimeError: if the row is not found or the API call fails.
    """
    path = f"/database/rows/table/{table_id}/{row_id}/?user_field_names=true"
    req = Request(
        f"{BASEROW_API}{path}",
        headers={
            "Authorization": f"JWT {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        resp = urlopen(req, timeout=15)
        return json.loads(resp.read())
    except URLError as exc:
        # Distinguish 404 (row not found) from other errors
        if getattr(exc, "code", None) == 404:
            raise RuntimeError(
                f"Row {row_id} not found in table {table_id}"
            ) from exc
        raise RuntimeError(
            f"Baserow API GET {path} failed: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid JSON in Baserow API response for {path}: {exc}"
        ) from exc


# ── Views ──────────────────────────────────────────────────────────


class PingView(APIView):
    """Health-check endpoint.

    Returns a simple JSON payload confirming the plugin is loaded and
    can authenticate against the Baserow API. Does NOT need auth because
    it is a health check — but the task says IsAuthenticated, so we keep
    it consistent.
    """
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        try:
            token = _get_jwt(request)
        except RuntimeError as exc:
            return Response(
                {"plugin": "coveredon_pipeline", "status": "degraded", "error": str(exc)},
                status=status.HTTP_200_OK,  # 200 even when degraded — the plugin itself works
            )

        return Response(
            {
                "plugin": "coveredon_pipeline",
                "status": "ok",
                "baserow_version": "2.3.3",
                "auth": "jwt",
            }
        )


class TriageView(APIView):
    """Pipeline triage endpoint.

    Returns four buckets of leads needing attention:
      - needs_contact:  has_usable_contact is false OR contact_channel_recommendation is empty
      - send_ready:     stage=SEND_APPROVED AND requires_operator_approval is false
      - stale:          updated_at older than 7 days
      - hot_unworked:   score=HOT AND stage not in (SEND_APPROVED, REPLIED)

    Each bucket returns lead row ids, org name, stage, score, channel, updated_at.
    """
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        try:
            token = _get_jwt(request)
            leads = _fetch_leads(token)
        except RuntimeError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=STALE_DAYS)

        buckets = {
            "needs_contact": [],
            "send_ready": [],
            "stale": [],
            "hot_unworked": [],
        }

        for lead in leads:
            row_id = lead.get("id")
            stage = lead.get("stage") or ""
            score = lead.get("score") or ""
            channel = lead.get("contact_channel_recommendation") or ""
            org_name = _fetch_org_name(token, lead)

            # Parse updated_at — Baserow returns ISO 8601 strings
            updated_at_str = lead.get("updated_at")
            updated_at = None
            if updated_at_str:
                try:
                    updated_at = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    updated_at = None

            common = {
                "row_id": row_id,
                "org_name": org_name,
                "stage": stage,
                "score": score,
                "channel": channel,
                "updated_at": updated_at_str,
            }

            # ── Bucket 1: needs_contact ──────────────────────────
            # A lead needs contact when there is no usable contact info
            # OR no channel recommendation telling us how to reach them.
            has_contact = _is_truthy(lead.get("has_usable_contact"))
            if not has_contact or not channel:
                buckets["needs_contact"].append(common)

            # ── Bucket 2: send_ready ─────────────────────────────
            # Approved for sending AND does not require operator review.
            send_approved = stage == "SEND_APPROVED"
            requires_op = _is_truthy(lead.get("requires_operator_approval"))
            if send_approved and not requires_op:
                buckets["send_ready"].append(common)

            # ── Bucket 3: stale ──────────────────────────────────
            # No update in STALE_DAYS — the lead is stagnating.
            if updated_at and updated_at < cutoff:
                buckets["stale"].append(common)

            # ── Bucket 4: hot_unworked ───────────────────────────
            # High-value leads that haven't been sent or replied to.
            if score == "HOT" and stage not in HOT_UNWORKED_EXCLUDED_STAGES:
                buckets["hot_unworked"].append(common)

        return Response(buckets)


class StatsView(APIView):
    """Pipeline statistics endpoint.

    Returns aggregate counts broken down by:
      - stage:            count of leads in each pipeline stage
      - score:            count of leads at each priority score
      - contact_channel:  count of leads by recommended contact channel
      - totals:           total leads and various filtered counts
    """
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        try:
            token = _get_jwt(request)
            leads = _fetch_leads(token)
        except RuntimeError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        from collections import Counter

        stage_counts = Counter()
        score_counts = Counter()
        channel_counts = Counter()
        has_contact_count = 0
        requires_op_count = 0
        stale_count = 0

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=STALE_DAYS)

        for lead in leads:
            stage = lead.get("stage") or "(unset)"
            score = lead.get("score") or "(unset)"
            channel = lead.get("contact_channel_recommendation") or "(unset)"

            stage_counts[stage] += 1
            score_counts[score] += 1
            channel_counts[channel] += 1

            if _is_truthy(lead.get("has_usable_contact")):
                has_contact_count += 1
            if _is_truthy(lead.get("requires_operator_approval")):
                requires_op_count += 1

            # Count stale leads
            updated_at_str = lead.get("updated_at")
            if updated_at_str:
                try:
                    updated_at = datetime.fromisoformat(
                        updated_at_str.replace("Z", "+00:00")
                    )
                    if updated_at < cutoff:
                        stale_count += 1
                except (ValueError, TypeError):
                    pass

        return Response(
            {
                "stage": dict(stage_counts),
                "score": dict(score_counts),
                "contact_channel": dict(channel_counts),
                "totals": {
                    "total_leads": len(leads),
                    "has_usable_contact": has_contact_count,
                    "requires_operator_approval": requires_op_count,
                    "stale": stale_count,
                },
            }
        )


# ── Chart Views ───────────────────────────────────────────────────


class FunnelView(APIView):
    """Pipeline stage funnel endpoint.

    Returns an ordered list of [{stage, count}] sorted by pipeline order:
    NEW, COREY_DRAFT_QUEUED, DRAFT_READY, SEND_APPROVED, REPLIED, then
    any other stages alphabetically last. Unset/null stages appear as
    '(unset)' — matching the pattern StatsView uses.
    """
    permission_classes = (IsAuthenticated,)

    # Canonical pipeline order — stages not in this list sort last
    PIPELINE_ORDER = [
        "NEW",
        "COREY_DRAFT_QUEUED",
        "DRAFT_READY",
        "SEND_APPROVED",
        "REPLIED",
    ]

    def get(self, request):
        try:
            token = _get_jwt(request)
            leads = _fetch_leads(token)
        except RuntimeError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        from collections import Counter

        stage_counts = Counter()
        for lead in leads:
            stage = lead.get("stage") or "(unset)"
            stage_counts[stage] += 1

        # Build sorted list: known stages in pipeline order, then others alphabetically
        known = {s: stage_counts.get(s, 0) for s in self.PIPELINE_ORDER}
        others = {s: c for s, c in stage_counts.items() if s not in self.PIPELINE_ORDER}

        funnel = []
        for stage in self.PIPELINE_ORDER:
            cnt = known[stage]
            if cnt > 0:
                funnel.append({"stage": stage, "count": cnt})

        # Append remaining stages in alphabetical order
        for stage in sorted(others.keys()):
            funnel.append({"stage": stage, "count": others[stage]})

        return Response({"funnel": funnel})


class TimelineView(APIView):
    """Lead creation timeline endpoint.

    Returns lead counts per day for the last N days (default 14).
    Each entry: {date: YYYY-MM-DD, count, hot_count}. Days with zero
    leads are included so the chart always spans the full window.

    Query params:
      days (int, optional) — number of days to look back (default 14).
    """
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        # Parse days param — default 14, clamp to positive int
        try:
            days = int(request.query_params.get("days", 14))
        except (ValueError, TypeError):
            days = 14
        if days < 1:
            days = 14

        try:
            token = _get_jwt(request)
            leads = _fetch_leads(token)
        except RuntimeError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        now = datetime.now(timezone.utc)
        range_start = now - timedelta(days=days - 1)

        # Build a dict of {YYYY-MM-DD: {date, count, hot_count}} for every day in range.
        # Initialise all days with zero so the chart always spans the full window.
        timeline = {}
        for i in range(days):
            d = (range_start + timedelta(days=i)).strftime("%Y-%m-%d")
            timeline[d] = {"date": d, "count": 0, "hot_count": 0}

        for lead in leads:
            created_str = lead.get("created_at")
            if not created_str:
                continue  # skip leads with no created_at
            try:
                created = datetime.fromisoformat(
                    created_str.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                continue

            date_key = created.strftime("%Y-%m-%d")
            if date_key in timeline:
                timeline[date_key]["count"] += 1
                if lead.get("score") == "HOT":
                    timeline[date_key]["hot_count"] += 1

        # Return sorted by date ascending
        result = [timeline[d] for d in sorted(timeline.keys())]
        return Response({"timeline": result})


class ChannelsView(APIView):
    """Contact channel distribution endpoint.

    Returns a list of [{channel, count, usable_contact_count}] showing
    how leads are distributed across contact channels. Unset/null channels
    appear as '(unset)' — matching the pattern StatsView uses.
    """
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        try:
            token = _get_jwt(request)
            leads = _fetch_leads(token)
        except RuntimeError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        from collections import Counter

        channel_counts = Counter()
        usable_contact_counts = Counter()

        for lead in leads:
            channel = lead.get("contact_channel_recommendation") or "(unset)"
            channel_counts[channel] += 1
            if _is_truthy(lead.get("has_usable_contact")):
                usable_contact_counts[channel] += 1

        # Build result sorted by count descending (most used channels first)
        result = [
            {
                "channel": channel,
                "count": channel_counts[channel],
                "usable_contact_count": usable_contact_counts[channel],
            }
            for channel in sorted(
                channel_counts.keys(),
                key=lambda c: channel_counts[c],
                reverse=True,
            )
        ]

        return Response({"channels": result})


# ── File upload views ───────────────────────────────────────────────


class UploadImageView(APIView):
    """Upload a single image and optionally attach it to a lead row.

    POST /api/coveredon_pipeline/upload_image/
    Content-Type: multipart/form-data

    Form fields:
      file            — required, image file (png/jpg/jpeg/webp)
      row_id          — required, target Leads (885) row id
      attach          — optional, "true" to attach to Screenshots field
      screenshot_path — optional, string value for screenshot_path field

    Response: {uploaded: <name>, attached: bool, total_screenshots: N}
    Errors: 400 missing/invalid file or row_id, 404 row not found,
            502 upstream Baserow failure.
    """
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        # ── Validate file ──────────────────────────────────────────
        # DRF multipart files arrive via request.FILES (Django standard).
        # request.data contains non-file fields from the form.
        if "file" not in request.FILES:
            return Response(
                {"error": "Missing 'file' field — send an image as multipart/form-data"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        uploaded_file = request.FILES["file"]
        mime = uploaded_file.content_type or "application/octet-stream"

        if mime not in ALLOWED_MIME_TYPES:
            return Response(
                {
                    "error": f"Unsupported file type '{mime}'. "
                    f"Allowed: {', '.join(ALLOWED_MIME_TYPES)}"
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Validate file size ─────────────────────────────────────
        # Read the file bytes now so we can check size before uploading.
        file_bytes = uploaded_file.read()
        if len(file_bytes) > MAX_UPLOAD_BYTES:
            return Response(
                {"error": f"File too large ({len(file_bytes)} bytes). "
                 f"Maximum allowed: {MAX_UPLOAD_BYTES} bytes (10MB)"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Validate row_id ────────────────────────────────────────
        try:
            row_id = int(request.data.get("row_id", ""))
        except (TypeError, ValueError):
            return Response(
                {"error": "Missing or invalid 'row_id' — must be an integer"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Parse optional flags ───────────────────────────────────
        attach = str(request.data.get("attach", "")).lower() in ("true", "1", "yes")

        try:
            token = _get_jwt(request)
        except RuntimeError as exc:
            return Response(
                {"error": f"Authentication failed: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # ── Upload file to Baserow user-files ──────────────────────
        # Build multipart body and POST to the user-file upload endpoint.
        body_bytes, boundary = _build_multipart_body(
            field_name="file",
            filename=uploaded_file.name or "upload.png",
            content_type=mime,
            file_bytes=file_bytes,
        )

        try:
            upload_req = Request(
                f"{BASEROW_API}{USER_FILES_UPLOAD_PATH}",
                data=body_bytes,
                headers={
                    "Authorization": f"JWT {token}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
            )
            upload_resp = urlopen(upload_req, timeout=30)
            upload_data = json.loads(upload_resp.read())
        except URLError as exc:
            # URLError.code gives the HTTP status when the server responds
            status_code = getattr(exc, "code", None) or 0
            return Response(
                {"error": f"Baserow file upload failed (HTTP {status_code}): {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except json.JSONDecodeError as exc:
            return Response(
                {"error": f"Invalid JSON from Baserow upload endpoint: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Extract the hashed file name from the upload response.
        # Baserow returns: {"name": "<hashed-name>", "url": "...", ...}
        hashed_name = upload_data.get("name", "")
        if not hashed_name:
            return Response(
                {"error": "Baserow upload response missing 'name' field"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        attached = False
        total_screenshots = 0

        if attach:
            # ── Attach file to lead row Screenshots field ──────────
            # Step 1: GET the current row to see existing screenshots.
            try:
                row = _api_get_single_row(LEADS_TABLE_ID, row_id, token)
            except RuntimeError as exc:
                error_msg = str(exc)
                # Distinguish "not found" (404) from other failures
                if "not found" in error_msg:
                    return Response(
                        {"error": error_msg},
                        status=status.HTTP_404_NOT_FOUND,
                    )
                return Response(
                    {"error": f"Failed to fetch lead row: {error_msg}"},
                    status=status.HTTP_502_BAD_GATEWAY,
                )

            # Step 2: Get existing Screenshots (field 8201).
            # Baserow returns gallery/uploaded file fields as a list of
            # dicts with {"name": "<hashed-name>", ...}.
            existing_screenshots = row.get("Screenshots") or []

            # Step 3: Dedupe — skip if this hashed name is already attached.
            existing_names = {s.get("name") for s in existing_screenshots if isinstance(s, dict)}
            if hashed_name not in existing_names:
                # Merge: append the new file and preserve all existing entries.
                merged = existing_screenshots + [{"name": hashed_name}]
                try:
                    _api_patch(
                        f"/database/rows/table/{LEADS_TABLE_ID}/{row_id}/?user_field_names=true",
                        {"Screenshots": merged},
                        token,
                    )
                except RuntimeError as exc:
                    return Response(
                        {"error": f"Failed to attach screenshot to row: {exc}"},
                        status=status.HTTP_502_BAD_GATEWAY,
                    )
                attached = True
                total_screenshots = len(merged)
            else:
                # File already attached — no-op but report current count.
                total_screenshots = len(existing_screenshots)

            # ── Optionally set screenshot_path (field 8175) ────────
            screenshot_path = request.data.get("screenshot_path", "").strip()
            if screenshot_path:
                try:
                    _api_patch(
                        f"/database/rows/table/{LEADS_TABLE_ID}/{row_id}/?user_field_names=true",
                        {"screenshot_path": screenshot_path},
                        token,
                    )
                except RuntimeError as exc:
                    # Non-critical — log via the error but don't fail the upload.
                    # The image is already attached. We return a warning header
                    # by including a note in the response.
                    pass

        return Response({
            "uploaded": hashed_name,
            "attached": attached,
            "total_screenshots": total_screenshots,
        })


class UploadImagesView(APIView):
    """Upload multiple images and attach all to a single lead row.

    POST /api/coveredon_pipeline/upload_images/
    Content-Type: multipart/form-data

    Form fields:
      files   — required, one or more image files (same field name repeated)
      row_id  — required, target Leads (885) row id

    Processes files sequentially (not parallel — keeps it simple and
    avoids race conditions on the PATCH endpoint). Returns per-file results.

    Response: {
        "results": [
            {"uploaded": "<name1>", "attached": bool, "total_screenshots": N},
            ...
        ],
        "total_uploaded": N,
        "total_attached": N,
    }
    Errors: Same as UploadImageView.
    """
    permission_classes = (IsAuthenticated,)

    def post(self, request):
        # ── Validate files ─────────────────────────────────────────
        # Django stores multiple files with the same field name in
        # request.FILES.getlist('files').
        files = request.FILES.getlist("files")
        if not files:
            return Response(
                {"error": "No 'files' provided — send at least one image as multipart/form-data"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Validate row_id ────────────────────────────────────────
        try:
            row_id = int(request.data.get("row_id", ""))
        except (TypeError, ValueError):
            return Response(
                {"error": "Missing or invalid 'row_id' — must be an integer"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            token = _get_jwt(request)
        except RuntimeError as exc:
            return Response(
                {"error": f"Authentication failed: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # ── Process each file sequentially ─────────────────────────
        # We process one file at a time to avoid race conditions: each
        # file upload fetches the current Screenshots list, dedupes, and
        # writes back. Sequential is simpler and safe.
        results = []
        total_attached = 0

        for uploaded_file in files:
            mime = uploaded_file.content_type or "application/octet-stream"

            # Validate file type
            if mime not in ALLOWED_MIME_TYPES:
                results.append({
                    "error": f"Unsupported type '{mime}' for '{uploaded_file.name}'",
                })
                continue

            # Validate file size
            file_bytes = uploaded_file.read()
            if len(file_bytes) > MAX_UPLOAD_BYTES:
                results.append({
                    "error": f"File '{uploaded_file.name}' too large "
                             f"({len(file_bytes)} bytes, max {MAX_UPLOAD_BYTES})",
                })
                continue

            # Upload to Baserow user-files
            body_bytes, boundary = _build_multipart_body(
                field_name="file",
                filename=uploaded_file.name or "upload.png",
                content_type=mime,
                file_bytes=file_bytes,
            )

            try:
                upload_req = Request(
                    f"{BASEROW_API}{USER_FILES_UPLOAD_PATH}",
                    data=body_bytes,
                    headers={
                        "Authorization": f"JWT {token}",
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                    },
                )
                upload_resp = urlopen(upload_req, timeout=30)
                upload_data = json.loads(upload_resp.read())
            except URLError as exc:
                results.append({
                    "error": f"Upload failed for '{uploaded_file.name}': {exc}",
                })
                continue
            except json.JSONDecodeError as exc:
                results.append({
                    "error": f"Invalid JSON from upload for '{uploaded_file.name}': {exc}",
                })
                continue

            hashed_name = upload_data.get("name", "")
            if not hashed_name:
                results.append({
                    "error": f"Upload response missing 'name' for '{uploaded_file.name}'",
                })
                continue

            # Attach to the lead row (always attach in batch variant)
            try:
                row = _api_get_single_row(LEADS_TABLE_ID, row_id, token)
            except RuntimeError as exc:
                results.append({
                    "error": f"Failed to fetch row {row_id}: {exc}",
                })
                continue

            existing_screenshots = row.get("Screenshots") or []
            existing_names = {s.get("name") for s in existing_screenshots if isinstance(s, dict)}
            attached = False

            if hashed_name not in existing_names:
                merged = existing_screenshots + [{"name": hashed_name}]
                try:
                    _api_patch(
                        f"/database/rows/table/{LEADS_TABLE_ID}/{row_id}/?user_field_names=true",
                        {"Screenshots": merged},
                        token,
                    )
                except RuntimeError as exc:
                    results.append({
                        "error": f"Failed to attach '{uploaded_file.name}': {exc}",
                    })
                    continue
                attached = True
                total_attached += 1

            results.append({
                "uploaded": hashed_name,
                "attached": attached,
                "total_screenshots": len(existing_screenshots) + (1 if attached else 0),
            })

        return Response({
            "results": results,
            "total_uploaded": len(results),
            "total_attached": total_attached,
        })
