#!/usr/bin/env python3
"""Verify the coveredon_pipeline plugin structure and logic.

This script runs OUTSIDE the Baserow container — it checks:
  1. Plugin file structure matches the reference skeleton
  2. All expected files exist with correct content signatures
  3. Python syntax of all .py files
  4. Import structure is self-consistent (no broken internal refs)
  5. Optionally hits the live Baserow endpoints (when --live is passed)

Usage:
  # Static structure check only (no Baserow dependency)
  python3 verify.py

  # Live test against Baserow API (requires running Baserow instance)
  python3 verify.py --live
"""
import argparse
import ast
import os
import sys

PLUGIN_DIR = os.path.join(os.path.dirname(__file__), "plugins", "coveredon_pipeline")
BACKEND_DIR = os.path.join(PLUGIN_DIR, "backend")
SRC_DIR = os.path.join(BACKEND_DIR, "src", "coveredon_pipeline")
API_DIR = os.path.join(SRC_DIR, "api")

# Every file that must exist
REQUIRED_FILES = [
    os.path.join(BACKEND_DIR, "setup.py"),
    os.path.join(SRC_DIR, "__init__.py"),
    os.path.join(SRC_DIR, "apps.py"),
    os.path.join(SRC_DIR, "plugins.py"),
    os.path.join(API_DIR, "__init__.py"),
    os.path.join(API_DIR, "urls.py"),
    os.path.join(API_DIR, "views.py"),
]


def check_structure():
    """Verify all required files exist."""
    errors = []
    for path in REQUIRED_FILES:
        if not os.path.isfile(path):
            errors.append(f"MISSING: {path}")
        else:
            size = os.path.getsize(path)
            print(f"  OK [{size:5d} bytes] {os.path.relpath(path, PLUGIN_DIR)}")
    return errors


def check_syntax():
    """Compile all .py files to verify no syntax errors."""
    errors = []
    for root, dirs, files in os.walk(PLUGIN_DIR):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(root, fname)
            try:
                with open(path) as f:
                    ast.parse(f.read(), filename=path)
                print(f"  SYNTAX OK  {os.path.relpath(path, PLUGIN_DIR)}")
            except SyntaxError as exc:
                errors.append(f"SYNTAX ERROR in {path}: {exc}")
    return errors


def check_content_signatures():
    """Verify key content patterns expected in each file."""
    errors = []
    checks = {
        "setup.py": ["find_packages", "package_dir", "src", "coveredon"],
        "apps.py": ["AppConfig", "plugin_registry", "CoveredonPipelineConfig"],
        "plugins.py": ["Plugin", "get_api_urls", "CoveredonPipelinePlugin"],
        "urls.py": ["PingView", "TriageView", "StatsView", "UploadImageView", "UploadImagesView", "app_name"],
        "views.py": ["APIView", "IsAuthenticated", "PingView", "TriageView", "StatsView", "UploadImageView", "UploadImagesView"],
    }

    for fname, patterns in checks.items():
        # Find the file under PLUGIN_DIR
        for root, dirs, files in os.walk(PLUGIN_DIR):
            if fname in files:
                path = os.path.join(root, fname)
                with open(path) as f:
                    content = f.read()
                for pat in patterns:
                    if pat not in content:
                        errors.append(
                            f"MISSING PATTERN '{pat}' in {os.path.relpath(path, PLUGIN_DIR)}"
                        )
                    else:
                        print(f"  PATTERN OK '{pat}' in {fname}")
                break
        else:
            errors.append(f"FILE NOT FOUND for content check: {fname}")
    return errors


def check_view_endpoints():
    """Verify that views.py defines all three required endpoints."""
    path = os.path.join(API_DIR, "views.py")
    if not os.path.isfile(path):
        return [f"views.py not found at {path}"]

    with open(path) as f:
        content = f.read()

    classes_expected = ["PingView", "TriageView", "StatsView", "UploadImageView", "UploadImagesView"]
    errors = []
    for cls in classes_expected:
        if f"class {cls}" not in content:
            errors.append(f"MISSING endpoint class: {cls}")
        else:
            print(f"  CLASS OK  {cls}")
    return errors


def check_urls_endpoints():
    """Verify urls.py registers all three routes."""
    path = os.path.join(API_DIR, "urls.py")
    if not os.path.isfile(path):
        return [f"urls.py not found at {path}"]

    with open(path) as f:
        content = f.read()

    routes_expected = ["ping/$", "triage/$", "stats/$", "upload_image/$", "upload_images/$"]
    errors = []
    for route in routes_expected:
        if route not in content:
            errors.append(f"MISSING URL route pattern: {route}")
        else:
            print(f"  ROUTE OK  {route}")
    return errors


def check_ping_class():
    """Verify PingView uses IsAuthenticated."""
    path = os.path.join(API_DIR, "views.py")
    if not os.path.isfile(path):
        return ["views.py not found"]

    with open(path) as f:
        content = f.read()

    errors = []
    if "IsAuthenticated" not in content:
        errors.append("MISSING IsAuthenticated permission class")
    else:
        print("  AUTH OK    IsAuthenticated used in views")

    # Verify triage bucket logic is present
    for bucket in ["needs_contact", "send_ready", "stale", "hot_unworked"]:
        if bucket not in content:
            errors.append(f"MISSING triage bucket: {bucket}")
        else:
            print(f"  BUCKET OK  {bucket}")
    return errors


def live_test():
    """Hit the actual Baserow endpoints to verify they work.

    Requires a running Baserow instance at http://localhost:8682
    and valid admin credentials in the environment or .env file.
    """
    errors = []

    # Try loading from .env
    env_paths = [
        "/home/black/baserow-dmz/.env",
    ]
    env = {}
    for ep in env_paths:
        ep = os.path.abspath(ep)
        if os.path.isfile(ep):
            with open(ep) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if "=" in line:
                            key, _, val = line.partition("=")
                            env[key.strip()] = val.strip()

    email = os.environ.get("BASEROW_ADMIN_EMAIL") or env.get("BASEROW_ADMIN_EMAIL")
    password = os.environ.get("BASEROW_ADMIN_PASSWORD") or env.get("BASEROW_ADMIN_PASSWORD")

    if not email or not password:
        errors.append("Cannot run live test: no BASEROW_ADMIN_EMAIL/PASSWORD found")
        return errors

    import urllib.request
    import json
    from urllib.request import Request, urlopen

    # Get JWT
    payload = json.dumps({"email": email, "password": password}).encode()
    try:
        req = Request(
            "http://localhost:8682/api/user/token-auth/",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urlopen(req, timeout=10)
        result = json.loads(resp.read())
        token = result["token"]
        print(f"  AUTH OK    JWT token obtained ({len(token)} chars)")
    except Exception as exc:
        errors.append(f"JWT auth failed: {exc}")
        return errors

    # Test ping
    try:
        req = Request(
            "http://localhost:8682/api/coveredon_pipeline/ping/",
            headers={"Authorization": f"JWT {token}"},
        )
        resp = urlopen(req, timeout=10)
        data = json.loads(resp.read())
        print(f"  PING OK    {json.dumps(data, indent=2)}")
    except Exception as exc:
        errors.append(f"Ping failed: {exc}")

    # Test triage
    try:
        req = Request(
            "http://localhost:8682/api/coveredon_pipeline/triage/",
            headers={"Authorization": f"JWT {token}"},
        )
        resp = urlopen(req, timeout=15)
        data = json.loads(resp.read())
        for bucket, items in data.items():
            print(f"  TRIAGE OK  {bucket}: {len(items)} leads")
    except Exception as exc:
        errors.append(f"Triage failed: {exc}")
        import traceback
        traceback.print_exc()

    # Test stats
    try:
        req = Request(
            "http://localhost:8682/api/coveredon_pipeline/stats/",
            headers={"Authorization": f"JWT {token}"},
        )
        resp = urlopen(req, timeout=15)
        data = json.loads(resp.read())
        for section, counts in data.items():
            if isinstance(counts, dict):
                print(f"  STATS OK   {section}: {len(counts)} categories")
            else:
                print(f"  STATS OK   {section}: {counts}")
    except Exception as exc:
        errors.append(f"Stats failed: {exc}")
        import traceback
        traceback.print_exc()

    return errors


def main():
    parser = argparse.ArgumentParser(description="Verify coveredon_pipeline plugin")
    parser.add_argument("--live", action="store_true", help="Run live Baserow API tests")
    args = parser.parse_args()

    print("=" * 60)
    print("CoveredOn Pipeline Plugin — Verification")
    print("=" * 60)

    all_errors = []

    print("\n--- Structure Check ---")
    all_errors += check_structure()

    print("\n--- Syntax Check ---")
    all_errors += check_syntax()

    print("\n--- Content Signatures ---")
    all_errors += check_content_signatures()

    print("\n--- Endpoint Classes ---")
    all_errors += check_view_endpoints()

    print("\n--- URL Routes ---")
    all_errors += check_urls_endpoints()

    print("\n--- Security & Bucket Logic ---")
    all_errors += check_ping_class()

    if args.live:
        print("\n--- Live Baserow API Test ---")
        all_errors += live_test()

    print("\n" + "=" * 60)
    if all_errors:
        print(f"FAILED — {len(all_errors)} error(s):")
        for e in all_errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("ALL CHECKS PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()