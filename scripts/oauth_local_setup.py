#!/usr/bin/env python3
"""ONE-TIME local helper: mint the YouTube OAuth refresh token (Phase 2/3).

Run this ONCE on your own machine (not the runner):

    pip install google-auth-oauthlib
    python scripts/oauth_local_setup.py --client-id X --client-secret Y

Prerequisites (free, no billing/card):
  1. https://console.cloud.google.com → create a project
  2. Enable "YouTube Data API v3" + "YouTube Analytics API" for it
  3. OAuth consent screen → External → set yourself as a test user
  4. Create OAuth Client ID — type: **Desktop app** — download id/secret
  NOTE: keep the app in Testing mode but be aware test-mode refresh tokens
  expire after 7 days; publishing the app (no verification needed, unverified
  warning is fine for personal automation) avoids that. See README.

This script opens a browser for consent (read-only scopes) and prints the
three secrets to store in GitHub:
  GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / YT_REFRESH_TOKEN
"""
import argparse
import json
import sys

SCOPES = [
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
    # Phase 3 (auto-upload). Tokens minted BEFORE this scope was added must be
    # re-minted by rerunning this script — a refresh token only carries the
    # scopes granted at its own consent screen.
    "https://www.googleapis.com/auth/youtube.upload",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", required=True)
    args = ap.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Install the helper dependency first:\n    pip install google-auth-oauthlib",
              file=sys.stderr)
        return 1

    flow = InstalledAppFlow.from_client_config(
        {"installed": {
            "client_id": args.client_id,
            "client_secret": args.client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }},
        scopes=SCOPES,
    )
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    refresh = creds.refresh_token
    if not refresh:
        print("\n⚠ No refresh_token returned — revoke the app's access at "
              "https://myaccount.google.com/permissions and rerun.", file=sys.stderr)
        return 1

    print("\n✅ OAuth complete. Add these as GitHub Actions secrets:")
    print(f"    gh secret set GOOGLE_CLIENT_ID --body {json.dumps(args.client_id)}")
    print(f"    gh secret set GOOGLE_CLIENT_SECRET --body {json.dumps(args.client_secret)}")
    print(f"    gh secret set YT_REFRESH_TOKEN --body {json.dumps(refresh)}")
    print("\n(.env for local runs: GOOGLE_CLIENT_ID=…  GOOGLE_CLIENT_SECRET=…  YT_REFRESH_TOKEN=…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
