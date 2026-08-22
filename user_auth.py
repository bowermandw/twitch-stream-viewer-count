#!/usr/bin/env python3
"""User access token via Twitch's authorization code flow.

Some endpoints (Get Chatters, for one) need a token that represents a *person*
rather than the application, so the app token from twitch_viewers.py won't do.
Getting one means a real browser login, once — after that the refresh token
keeps it alive without further prompting.

    python3 user_auth.py            # authorize (or show status if already done)
    python3 user_auth.py --status
    python3 user_auth.py --force    # re-authorize, e.g. as a different user
"""

import argparse
import http.server
import json
import os
import secrets
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

import twitch_viewers as tv

AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
REVOKE_URL = "https://id.twitch.tv/oauth2/revoke"

# Must match a redirect URL registered on the app at dev.twitch.tv, exactly.
REDIRECT_URI = "http://localhost:3000"
CALLBACK_PORT = 3000

TOKEN_PATH = os.path.join(tv.BASE_DIR, ".user_token.json")
REFRESH_MARGIN = 300  # refresh when under 5 minutes remain

DONE_PAGE = """<!doctype html><meta charset="utf-8">
<title>Authorized</title>
<body style="background:#0f0f0f;color:#f1f1f1;font:16px -apple-system,Helvetica,Arial;
             display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center">
  <div style="font-size:42px">{icon}</div>
  <h1 style="font-size:20px;font-weight:600">{heading}</h1>
  <p style="color:#aaa">{detail}</p>
</div></body>"""


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


def load_token():
    try:
        with open(TOKEN_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def save_token(payload):
    with open(TOKEN_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.chmod(TOKEN_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600


def store(raw, scopes):
    """Normalise a token response and record who it belongs to."""
    payload = {
        "access_token": raw["access_token"],
        "refresh_token": raw.get("refresh_token", ""),
        "expires_at": time.time() + raw.get("expires_in", 14400),
        "scopes": scopes,
    }
    who = validate(payload["access_token"])
    if who:
        payload["login"] = who.get("login", "")
        payload["user_id"] = who.get("user_id", "")
        payload["scopes"] = who.get("scopes", scopes)
    save_token(payload)
    return payload


# --------------------------------------------------------------------------
# twitch calls
# --------------------------------------------------------------------------


def post_form(url, fields):
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(request, timeout=tv.HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def validate(access_token):
    """Ask Twitch who a token belongs to and which scopes it carries."""
    request = urllib.request.Request(VALIDATE_URL)
    request.add_header("Authorization", "OAuth {}".format(access_token))
    try:
        with urllib.request.urlopen(request, timeout=tv.HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return None


def refresh(payload, client_id, client_secret, scopes):
    if not payload.get("refresh_token"):
        return None
    try:
        raw = post_form(TOKEN_URL_FOR(), {
            "grant_type": "refresh_token",
            "refresh_token": payload["refresh_token"],
            "client_id": client_id,
            "client_secret": client_secret,
        })
    except urllib.error.HTTPError:
        return None  # refresh token revoked or expired; needs a fresh login
    return store(raw, scopes)


def TOKEN_URL_FOR():
    return tv.TOKEN_URL


# --------------------------------------------------------------------------
# browser flow
# --------------------------------------------------------------------------


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):  # noqa: N802 - required name
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        CallbackHandler.result = {k: v[0] for k, v in query.items()}

        if "code" in CallbackHandler.result:
            page = DONE_PAGE.format(
                icon="&#10003;", heading="Authorized",
                detail="You can close this tab and return to the terminal.")
        else:
            page = DONE_PAGE.format(
                icon="&#10007;",
                heading="Authorization failed",
                detail=CallbackHandler.result.get("error_description",
                                                  "No authorization code was returned."))
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep the local server quiet


def authorize(client_id, client_secret, scopes, open_browser=True):
    """Run the authorization code flow and return the stored token payload."""
    state = secrets.token_urlsafe(24)
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(scopes),
        "state": state,
        "force_verify": "true",  # always show the account picker
    })
    url = "{}?{}".format(AUTHORIZE_URL, params)

    try:
        server = http.server.HTTPServer(("localhost", CALLBACK_PORT), CallbackHandler)
    except OSError as exc:
        sys.exit(
            "Can't listen on {} ({}).\n"
            "Something else is using port {}. Stop it and try again — the port has to\n"
            "match the redirect URL registered on your Twitch app.".format(
                REDIRECT_URI, exc, CALLBACK_PORT))

    CallbackHandler.result = {}
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("Opening Twitch to authorize.\n")
    print("  Sign in as the account that should be the moderator.")
    print("  Requesting scope(s): {}\n".format(", ".join(scopes)))
    print("If the browser doesn't open, paste this in yourself:\n\n  {}\n".format(url))
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    print("Waiting for the redirect back to {} ...".format(REDIRECT_URI))
    thread.join(timeout=300)
    server.server_close()

    result = CallbackHandler.result
    if not result:
        sys.exit("Timed out after 5 minutes with no response from Twitch.")
    if "error" in result:
        sys.exit("Twitch returned an error: {} — {}".format(
            result["error"], result.get("error_description", "")))
    if result.get("state") != state:
        sys.exit("State mismatch — discarding this response as a safety measure.")

    try:
        raw = post_form(tv.TOKEN_URL, {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": result["code"],
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        })
    except urllib.error.HTTPError as exc:
        sys.exit("Could not exchange the code for a token: HTTP {}\n{}".format(
            exc.code, exc.read().decode("utf-8", "replace")[:300]))

    payload = store(raw, scopes)
    print("\nAuthorized as {} (user id {}).".format(
        payload.get("login", "?"), payload.get("user_id", "?")))
    return payload


# --------------------------------------------------------------------------
# the one function other scripts call
# --------------------------------------------------------------------------


def get_user_token(scopes, interactive=True):
    """A valid user access token carrying every scope in `scopes`.

    Reuses the stored token, refreshes it when stale, and only falls back to a
    browser login when there's no other way.
    """
    client_id, client_secret = tv.load_credentials()
    payload = load_token()

    if payload:
        missing = [s for s in scopes if s not in (payload.get("scopes") or [])]
        if missing:
            if not interactive:
                raise SystemExit(
                    "The stored token lacks the {} scope. Run:  python3 user_auth.py "
                    "--scope {}".format(", ".join(missing),
                                        " --scope ".join(sorted(set(scopes) | set(payload.get("scopes") or [])))))
            # Carry the scopes already granted into the new login, so adding one
            # doesn't silently drop another script's access.
            scopes = sorted(set(scopes) | set(payload.get("scopes") or []))
            print("Stored token is missing the {} scope.".format(", ".join(missing)))
            print("Re-authorizing for: {}\n".format(", ".join(scopes)))
            payload = None
        elif payload["expires_at"] - time.time() < REFRESH_MARGIN:
            payload = refresh(payload, client_id, client_secret, scopes)

    if payload and not validate(payload["access_token"]):
        payload = refresh(payload, client_id, client_secret, scopes)

    if not payload:
        if not interactive:
            raise SystemExit("No usable user token. Run:  python3 user_auth.py")
        payload = authorize(client_id, client_secret, scopes)

    return payload


# --------------------------------------------------------------------------


def describe(payload):
    if not payload:
        print("No user token stored. Run:  python3 user_auth.py")
        return
    left = payload["expires_at"] - time.time()
    live = validate(payload["access_token"])
    print("  user        {} (id {})".format(payload.get("login", "?"),
                                            payload.get("user_id", "?")))
    print("  scopes      {}".format(", ".join(payload.get("scopes") or []) or "—"))
    print("  expires in  {}".format(
        "{:.0f} min".format(left / 60) if left > 0 else "expired"))
    print("  refreshable {}".format("yes" if payload.get("refresh_token") else "no"))
    print("  accepted    {}".format("yes" if live else "no — will refresh on next use"))
    print("  stored at   {}".format(TOKEN_PATH))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scope", action="append", default=None,
                        help="scope to request (repeatable); default moderator:read:chatters")
    parser.add_argument("--status", action="store_true", help="show the stored token and exit")
    parser.add_argument("--force", action="store_true", help="re-run the browser login")
    parser.add_argument("--revoke", action="store_true",
                        help="revoke the stored token and delete it")
    parser.add_argument("--no-browser", action="store_true",
                        help="print the URL instead of opening a browser")
    args = parser.parse_args()

    scopes = args.scope or ["moderator:read:chatters"]

    if args.status:
        describe(load_token())
        return

    if args.revoke:
        payload = load_token()
        if not payload:
            print("Nothing stored.")
            return
        client_id, _ = tv.load_credentials()
        try:
            post_form(REVOKE_URL, {"client_id": client_id,
                                   "token": payload["access_token"]})
        except urllib.error.HTTPError:
            pass  # already invalid; deleting locally is what matters
        os.remove(TOKEN_PATH)
        print("Revoked and deleted {}".format(TOKEN_PATH))
        return

    if args.force:
        client_id, client_secret = tv.load_credentials()
        authorize(client_id, client_secret, scopes, open_browser=not args.no_browser)
    else:
        get_user_token(scopes)

    print()
    describe(load_token())


if __name__ == "__main__":
    main()
