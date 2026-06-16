"""
One-off helper to mint a Google OAuth refresh token with Sheets + Drive
scopes for the gsheets MCP service.

Bypasses Google OAuth Playground (which is finicky with Workspace org
policies and fiddly redirect URIs). This script:

  1. Spawns a tiny HTTP server on http://localhost:8080/
  2. Opens your browser to Google's OAuth consent screen
  3. Captures the authorization code when Google redirects back
  4. Exchanges the code for a refresh token
  5. Prints the refresh token to stdout

Zero external dependencies — pure stdlib.

== ONE-TIME SETUP ==

1. In Google Cloud Console -> APIs & Services -> Credentials, click your
   OAuth 2.0 Client ID (the one your GA service uses).

2. Under "Authorized redirect URIs", add:
       http://localhost:8080/
   ...and Save.

3. Edit the CLIENT_ID and CLIENT_SECRET constants below. The Client ID
   is visible on the same Credentials page. The Client secret is on the
   same edit screen — click "Download JSON" if you don't see it, the
   secret is in the file.

== HOW TO RUN ==

   python get_refresh_token.py

Your default browser will open to Google's consent screen. Log in with
the Google account that owns / has access to the Sheets you want the
MCP to read and write. Click "Allow" through the scope prompts.

The script will print the refresh token. Copy it (starts with "1//")
and paste it back to Claude — I'll push it to DO as the
GSHEETS_REFRESH_TOKEN secret.

== SECURITY ==

The refresh token grants Sheets + Drive.file access to whichever
Google account you log in with, for as long as the token is valid
(typically until you revoke it at myaccount.google.com/permissions).
Treat it like a password. After we verify the MCP works, you can
rotate by revoking + re-running this script.
"""

import http.server
import json
import secrets
import socket
import sys
import urllib.parse
import urllib.request
import webbrowser
from threading import Event

# === FILL THESE IN ===========================================================
CLIENT_ID = "60891177814-rs0onvo5jo44lln8ijrb7m6rpc22kf7q.apps.googleusercontent.com"
CLIENT_SECRET = "PASTE_YOUR_OAUTH_CLIENT_SECRET_HERE"
# =============================================================================

PORT = 8765
REDIRECT_URI = f"http://localhost:{PORT}/"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
STATE = secrets.token_urlsafe(16)


def _build_auth_url() -> str:
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",  # ask for a refresh token
        "prompt": "consent",       # force the consent screen so refresh_token comes back
        "state": STATE,
    })


# --- Callback capture --------------------------------------------------------

_captured: dict = {"code": None, "error": None}
_done = Event()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if params.get("state", [""])[0] != STATE:
            _captured["error"] = f"state mismatch — possible CSRF (got {params.get('state')})"
        elif "error" in params:
            _captured["error"] = params["error"][0]
        elif "code" in params:
            _captured["code"] = params["code"][0]
        else:
            _captured["error"] = "no code or error in callback"

        self.send_response(200 if _captured["code"] else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if _captured["code"]:
            self.wfile.write(
                b"<h1>OAuth complete</h1><p>You can close this tab. "
                b"Check your terminal for the refresh token.</p>"
            )
        else:
            self.wfile.write(
                f"<h1>OAuth error</h1><p>{_captured['error']}</p>".encode("utf-8")
            )
        _done.set()

    def log_message(self, fmt, *args):
        pass  # silence stdlib's noisy default access log


def _exchange_code_for_tokens(code: str) -> dict:
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "body": e.read().decode("utf-8", "replace")}


def main() -> int:
    if CLIENT_SECRET == "PASTE_YOUR_OAUTH_CLIENT_SECRET_HERE":
        print("ERROR: fill in CLIENT_SECRET at the top of this script first.")
        return 1

    # Make sure localhost:8080 is free
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("localhost", PORT))
        probe.close()
    except OSError:
        print(f"ERROR: localhost:{PORT} is in use. Close whatever's on that port and retry.")
        return 1

    server = http.server.HTTPServer(("localhost", PORT), _Handler)
    auth_url = _build_auth_url()
    print("\n" + "=" * 70)
    print("  PASTE THIS URL INTO YOUR PREFERRED BROWSER (Chrome / Firefox / etc.)")
    print("=" * 70)
    print()
    print(f"  {auth_url}")
    print()
    print("=" * 70)
    print(f"\nListening on {REDIRECT_URI} for Google's redirect...")
    print("(Press Ctrl+C to cancel)")
    while not _done.is_set():
        server.handle_request()
    server.server_close()

    if _captured["error"] or not _captured["code"]:
        print(f"\nFAILED: {_captured['error'] or 'no code'}")
        return 1

    print("\nGot authorization code. Exchanging for tokens...")
    tokens = _exchange_code_for_tokens(_captured["code"])
    if "error" in tokens:
        print(f"\nToken exchange failed: {tokens['error']}\n{tokens.get('body', '')}")
        return 1

    refresh = tokens.get("refresh_token")
    if not refresh:
        print("\nNo refresh_token returned. This usually means Google has already issued one")
        print("to this app for this user. Revoke it at https://myaccount.google.com/permissions")
        print("(remove the OAuth app), then re-run this script.")
        print(f"\nFull response: {json.dumps(tokens, indent=2)}")
        return 1

    print("\n" + "=" * 70)
    print("  REFRESH TOKEN (paste this back to Claude):")
    print("=" * 70)
    print()
    print(f"  {refresh}")
    print()
    print("=" * 70)
    print(f"\nScopes granted: {tokens.get('scope', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
