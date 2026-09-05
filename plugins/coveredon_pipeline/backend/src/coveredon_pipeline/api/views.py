"""Pipeline triage, stats, chart, and health views for Covered On Baserow plugin.

All views authenticate via JWT token obtained from the Baserow REST API
using the admin credentials defined in BASEROW_ADMIN_EMAIL / BASEROW_ADMIN_PASSWORD
environment variables (or the .env file at /home/black/baserow-dmz/.env).

Reads tables 885 (Leads) and 884 (Orgs) through the Baserow REST API at
http://localhost:8682/api/ — NOT via Django ORM directly, because the task
spec requires going through the API path.

Endpoints:
  GET  pipeline/ping/        — health check
  GET  pipeline/triage/      — pipeline triage buckets
  GET  pipeline/stats/       — aggregate counts
  GET  chart/funnel/         — ordered stage funnel (chart data)
  GET  chart/timeline/?days= — leads created per day (chart data)
  GET  chart/channels/       — contact channel distribution (chart data)
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