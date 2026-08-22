"""User access token via the authorization code flow.

Some endpoints (chat size, follower *names*) need a token representing a person
rather than the application. That means a real browser login, once — after which
the refresh token keeps it alive unattended, which is what makes this workable
on a server.
"""

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

from . import config
from .logging import log

# Must exactly match a redirect URL registered on the app at dev.twitch.tv.
REDIRECT_URI = os.environ.get("TWITCH_REDIRECT_URI") or "http://localhost:3000"
AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
REVOKE_URL = "https://id.twitch.tv/oauth2/revoke"

REFRESH_MARGIN = 300

DONE_PAGE = """<!doctype html><meta charset="utf-8"><title>{heading}</title>
<body style="background:#0f0f0f;color:#f1f1f1;font:16px -apple-system,Helvetica,Arial;
             display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center"><div style="font-size:42px">{icon}</div>
<h1 style="font-size:20px;font-weight:600">{heading}</h1>
<p style="color:#aaa">{detail}</p></div></body>"""


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


def load_token():
    try:
        with open(config.USER_TOKEN_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def save_token(payload):
    config.ensure_dirs()
    with open(config.USER_TOKEN_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.chmod(config.USER_TOKEN_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600


def _store(raw, scopes):
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


def _post_form(url, fields):
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(request, timeout=config.HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def validate(access_token):
    """Who a token belongs to and which scopes it carries, or None if invalid."""
    request = urllib.request.Request(VALIDATE_URL)
    request.add_header("Authorization", "OAuth {}".format(access_token))
    try:
        with urllib.request.urlopen(request, timeout=config.HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return None


def refresh(payload, client_id, client_secret, scopes):
    if not payload.get("refresh_token"):
        return None
    try:
        raw = _post_form(config.TOKEN_URL, {
            "grant_type": "refresh_token",
            "refresh_token": payload["refresh_token"],
            "client_id": client_id,
            "client_secret": client_secret,
        })
    except urllib.error.HTTPError:
        return None  # revoked or expired; needs a fresh login
    return _store(raw, scopes)


def revoke():
    payload = load_token()
    if not payload:
        return False
    client_id, _ = config.load_credentials()
    try:
        _post_form(REVOKE_URL, {"client_id": client_id, "token": payload["access_token"]})
    except urllib.error.HTTPError:
        pass  # already invalid; removing it locally is what matters
    os.remove(config.USER_TOKEN_PATH)
    return True


# --------------------------------------------------------------------------
# browser flow
# --------------------------------------------------------------------------


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):  # noqa: N802 - name required by BaseHTTPRequestHandler
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.result = {k: v[0] for k, v in query.items()}
        if "code" in _CallbackHandler.result:
            page = DONE_PAGE.format(icon="&#10003;", heading="Authorized",
                                    detail="You can close this tab and return to the terminal.")
        else:
            page = DONE_PAGE.format(
                icon="&#10007;", heading="Authorization failed",
                detail=_CallbackHandler.result.get("error_description",
                                                   "No authorization code was returned."))
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep the local callback server quiet


def authorize(client_id, client_secret, scopes, open_browser=True):
    """Run the authorization code flow and return the stored token payload."""
    state = secrets.token_urlsafe(24)
    url = "{}?{}".format(AUTHORIZE_URL, urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(scopes),
        "state": state,
        "force_verify": "true",  # always show the account picker
    }))

    parsed = urllib.parse.urlparse(REDIRECT_URI)
    port = parsed.port or 80
    try:
        server = http.server.HTTPServer((parsed.hostname or "localhost", port),
                                        _CallbackHandler)
    except OSError as exc:
        sys.exit("Can't listen on {} ({}).\nFree that port, or set "
                 "TWITCH_REDIRECT_URI to another URL registered on your Twitch "
                 "app.".format(REDIRECT_URI, exc))

    _CallbackHandler.result = {}
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

    result = _CallbackHandler.result
    if not result:
        sys.exit("Timed out after 5 minutes with no response from Twitch.")
    if "error" in result:
        sys.exit("Twitch returned an error: {} — {}".format(
            result["error"], result.get("error_description", "")))
    if result.get("state") != state:
        sys.exit("State mismatch — discarding this response as a safety measure.")

    try:
        raw = _post_form(config.TOKEN_URL, {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": result["code"],
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        })
    except urllib.error.HTTPError as exc:
        sys.exit("Could not exchange the code for a token: HTTP {}\n{}".format(
            exc.code, exc.read().decode("utf-8", "replace")[:300]))

    payload = _store(raw, scopes)
    print("\nAuthorized as {} (user id {}).".format(
        payload.get("login", "?"), payload.get("user_id", "?")))
    return payload


def user_token(scopes, interactive=True):
    """A valid user token carrying every scope in `scopes`.

    Reuses the stored token, refreshes when stale, and only falls back to a
    browser login when there is no other way. interactive=False raises instead,
    which is what the pollers use so they never block on a prompt.
    """
    client_id, client_secret = config.load_credentials()
    payload = load_token()

    if payload:
        missing = [s for s in scopes if s not in (payload.get("scopes") or [])]
        if missing:
            combined = sorted(set(scopes) | set(payload.get("scopes") or []))
            if not interactive:
                raise SystemExit(
                    "The stored token lacks the {} scope. Run:  {} auth {}".format(
                        ", ".join(missing), config.invocation(),
                        " ".join("--scope " + s for s in combined)))
            # Carry already-granted scopes into the new login so adding one
            # doesn't silently drop another command's access.
            scopes = combined
            print("Stored token is missing the {} scope.".format(", ".join(missing)))
            print("Re-authorizing for: {}\n".format(", ".join(scopes)))
            payload = None
        elif payload["expires_at"] - time.time() < REFRESH_MARGIN:
            payload = refresh(payload, client_id, client_secret, scopes)

    if payload and not validate(payload["access_token"]):
        payload = refresh(payload, client_id, client_secret, scopes)

    if not payload:
        if not interactive:
            raise SystemExit("No usable user token. Run:  {} auth".format(config.invocation()))
        payload = authorize(client_id, client_secret, scopes)
    return payload


def describe(payload):
    if not payload:
        print("No user token stored. Run:  {} auth".format(config.invocation()))
        return
    left = payload["expires_at"] - time.time()
    print("  user        {} (id {})".format(payload.get("login", "?"),
                                            payload.get("user_id", "?")))
    print("  scopes      {}".format(", ".join(payload.get("scopes") or []) or "—"))
    print("  expires in  {}".format("{:.0f} min".format(left / 60) if left > 0 else "expired"))
    print("  refreshable {}".format("yes" if payload.get("refresh_token") else "no"))
    print("  accepted    {}".format(
        "yes" if validate(payload["access_token"]) else "no — will refresh on next use"))
    print("  stored at   {}".format(config.USER_TOKEN_PATH))
