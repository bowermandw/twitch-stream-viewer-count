"""Google access token for Drive, via the authorization code flow.

Uploading the daily charts needs a token representing you rather than an
application, which means a real browser login, once — after which the refresh
token keeps it alive unattended, which is what makes this workable on a server.

Structurally this is `useroauth.py` against Google instead of Twitch. Where the
two genuinely differ, the difference is commented, because every one of those
differences is a way to end up with a credential that dies a week later.
"""

import contextlib
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

# A "Desktop app" client accepts http://localhost on any port, so unlike Twitch
# there is no redirect URL to register by hand. Google prefers 127.0.0.1 to
# localhost; either works, and this is overridable because port 3000 is shared
# with `twitch-metrics auth`.
REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI") or "http://localhost:3000"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
ABOUT_URL = config.DRIVE_API + "/about"

# Per-file scope: this tool can see only the files and folders it created
# itself, never the rest of your Drive. See README for why not plain `drive`.
SCOPE_DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"

REFRESH_MARGIN = 300

# An app left in "Testing" on the consent screen has its refresh tokens expired
# after 7 days. Warn a day early rather than on the day it breaks.
TESTING_WARN_AFTER = 6 * 86400

CONSENT_URL = "https://console.cloud.google.com/apis/credentials/consent"

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
        with open(config.GOOGLE_TOKEN_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def save_token(payload):
    """Write atomically, so a concurrent reader never sees a half-written file."""
    config.ensure_dirs()
    temporary = config.GOOGLE_TOKEN_PATH + ".tmp{}".format(os.getpid())
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    os.replace(temporary, config.GOOGLE_TOKEN_PATH)


@contextlib.contextmanager
def _refresh_lock():
    """Serialise refreshes across processes.

    Google does not rotate the refresh token on use, so losing this race is not
    the disaster it is for Twitch. The lock stays anyway: the *access* token is
    still rewritten, and the re-read below means the loser makes no network
    call at all rather than duplicating a refresh Google already served.
    """
    config.ensure_dirs()
    path = config.GOOGLE_TOKEN_PATH + ".lock"
    try:
        import fcntl
    except ImportError:
        yield  # not POSIX; single-process use only
        return
    handle = open(path, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _store(raw, scopes, previous=None):
    """Persist a token response, keeping the old refresh token when Google omits one."""
    # Google returns `scope` as a space-delimited string, not a list, and it is
    # authoritative: you can untick a box on the consent screen and be granted
    # less than was asked for.
    granted = (raw.get("scope") or "").split() or list(scopes)
    payload = {
        "access_token": raw["access_token"],
        # Google issues a refresh token only for access_type=offline, and omits
        # it entirely on a re-consent unless prompt=consent was sent. Taking
        # raw.get("refresh_token", "") the way the Twitch flow does would blank
        # a working credential and leave the daily upload unable to refresh.
        "refresh_token": raw.get("refresh_token") or (previous or {}).get("refresh_token", ""),
        "expires_at": time.time() + raw.get("expires_in", 3600),
        "scopes": granted,
        # Reset only by a fresh authorize(), carried across refreshes — this is
        # what lets describe() warn before the 7-day Testing-mode expiry.
        "authorized_at": (previous or {}).get("authorized_at") or time.time(),
    }
    who = about(payload["access_token"])
    if who:
        user = who.get("user") or {}
        payload["email"] = user.get("emailAddress", "")
        payload["name"] = user.get("displayName", "")
    save_token(payload)
    return payload


# --------------------------------------------------------------------------
# google calls
# --------------------------------------------------------------------------


def _post_form(url, fields):
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(request, timeout=config.HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def about(access_token):
    """Whose Drive this token opens, or None if the token is not accepted.

    `fields` is mandatory on about.get — omitting it returns 400, which reads
    like an auth failure. It works under drive.file alone, so naming the
    account in --status costs no extra scope.
    """
    request = urllib.request.Request(ABOUT_URL + "?fields=user")
    request.add_header("Authorization", "Bearer {}".format(access_token))
    try:
        with urllib.request.urlopen(request, timeout=config.HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return None


def refresh(payload, client_id, client_secret, scopes):
    """Exchange the refresh token for a new access token.

    Holds a lock and re-reads first: if a sibling process refreshed while we
    were waiting, its result is already good and asking again is just traffic.
    """
    if not payload.get("refresh_token"):
        return None
    with _refresh_lock():
        current = load_token()
        if (current and current.get("access_token") != payload.get("access_token")
                and current.get("expires_at", 0) - time.time() > REFRESH_MARGIN):
            return current  # someone else refreshed; use theirs
        return _refresh_locked(current or payload, client_id, client_secret, scopes)


def _refresh_locked(payload, client_id, client_secret, scopes):
    try:
        raw = _post_form(config.GOOGLE_TOKEN_URL, {
            "grant_type": "refresh_token",
            "refresh_token": payload["refresh_token"],
            "client_id": client_id,
            "client_secret": client_secret,
        })
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if "invalid_grant" in detail:
            log("WARN     Google rejected the refresh token (invalid_grant).")
            log("         The usual cause is an OAuth consent screen still in \"Testing\",")
            log("         which expires refresh tokens after 7 days. Publish the app, then:")
            log("           {} drive --auth --force".format(config.invocation()))
        else:
            log("WARN     Google refresh failed: HTTP {} {}".format(exc.code, detail))
        return None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log("WARN     could not reach Google to refresh: {}".format(exc))
        return None
    return _store(raw, scopes, previous=payload)


def revoke():
    """Tell Google to drop the grant, then delete the local token and folder cache."""
    payload = load_token()
    if not payload:
        return False
    token = payload.get("refresh_token") or payload.get("access_token")
    try:
        _post_form(REVOKE_URL, {"token": token})
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        pass  # already invalid; removing it locally is what matters
    os.remove(config.GOOGLE_TOKEN_PATH)
    # The cached folder ids are meaningless without the grant, and leaving them
    # would have a re-auth reuse ids it may no longer hold.
    with contextlib.suppress(OSError):
        os.remove(config.DRIVE_FOLDERS_PATH)
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
                                                   "Google returned no authorization code."))
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep the local callback server quiet


def _exchange(client_id, client_secret, code):
    try:
        return _post_form(config.GOOGLE_TOKEN_URL, {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            # String-compared by Google, so this must be byte-identical to the
            # redirect_uri sent in the authorize URL. Hence one constant.
            "redirect_uri": REDIRECT_URI,
        })
    except urllib.error.HTTPError as exc:
        sys.exit("Could not exchange the code for a token: HTTP {}\n{}".format(
            exc.code, exc.read().decode("utf-8", "replace")[:300]))


def _authorize_url(client_id, scopes, state):
    return "{}?{}".format(AUTHORIZE_URL, urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(scopes),
        "state": state,
        "access_type": "offline",  # without this there is no refresh token at all
        "prompt": "consent",       # without this a *re*-auth returns no refresh token
        "include_granted_scopes": "true",
    }))


def _authorize_manual(client_id, client_secret, scopes, url, state):
    """Headless flow: the operator pastes the redirect URL back.

    Note this still sends the loopback redirect_uri and simply doesn't listen.
    Google blocked the out-of-band redirect (urn:ietf:wg:oauth:2.0:oob) for new
    clients in 2022 and for all clients from 31 January 2023, so the old
    paste-the-code-Google-shows-you flow returns 400 invalid_request. The
    browser fails to reach localhost instead, and the address bar still holds
    everything we need.
    """
    print("Authorize in a browser on any machine:\n\n  {}\n".format(url))
    print("After approving, the browser will try to open {} and fail to".format(REDIRECT_URI))
    print("connect. That is expected — the address bar still holds the code.\n")
    print("Copy that whole URL and paste it here.\n")
    try:
        pasted = input("Redirect URL (or just the code): ").strip()
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nCancelled.")

    if not pasted:
        sys.exit("Nothing pasted.")

    # Anything URL-shaped gets parsed, so a denial or an error page is reported
    # as such rather than being posted to Google as if it were a code.
    if "://" in pasted or "?" in pasted or "=" in pasted:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query or pasted)
        if query.get("error"):
            sys.exit("Google returned an error instead of a code: {} — {}".format(
                query["error"][0], (query.get("error_description") or [""])[0]))
        code = (query.get("code") or [""])[0]
        if not code:
            sys.exit("No authorization code in what you pasted.\n"
                     "Expected a URL like {}/?code=...&state=...".format(REDIRECT_URI))
        returned_state = (query.get("state") or [""])[0]
        if returned_state and returned_state != state:
            sys.exit("State mismatch — discarding this response as a safety measure.")
        if not returned_state:
            print("\nNote: no state parameter present; skipping that check.")
    else:
        # Google's codes contain a '/', which the address bar shows as %2F. A
        # bare paste therefore arrives percent-encoded and would be exchanged
        # verbatim, failing with an opaque invalid_grant.
        code = urllib.parse.unquote(pasted) if "%" in pasted else pasted
        print("\nNote: bare code pasted, so the state check is skipped.")

    payload = _store(_exchange(client_id, client_secret, code), scopes)
    _announce(payload)
    return payload


def authorize(client_id, client_secret, scopes, open_browser=True, manual=False):
    """Run the authorization code flow and return the stored token payload."""
    state = secrets.token_urlsafe(24)
    url = _authorize_url(client_id, scopes, state)

    if manual:
        return _authorize_manual(client_id, client_secret, scopes, url, state)

    parsed = urllib.parse.urlparse(REDIRECT_URI)
    port = parsed.port or 80
    try:
        server = http.server.HTTPServer((parsed.hostname or "localhost", port),
                                        _CallbackHandler)
    except OSError as exc:
        sys.exit("Can't listen on {} ({}).\n"
                 "That port is also used by `{prog} auth`, so the two flows can't run\n"
                 "at the same moment. Free it, set GOOGLE_REDIRECT_URI to another\n"
                 "loopback URL, or paste the code back instead:\n"
                 "  {prog} drive --auth --manual".format(
                     REDIRECT_URI, exc, prog=config.invocation()))

    _CallbackHandler.result = {}
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("Opening Google to authorize.\n")
    print("  Sign in as the account whose Drive the charts should land in.")
    print("  Requesting scope(s): {}\n".format(", ".join(scopes)))
    print("  An unverified-app warning is expected for drive.file — click")
    print("  Advanced, then continue.\n")
    print("If the browser doesn't open, paste this in yourself:\n\n  {}\n".format(url))
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    print("Waiting for the redirect back to {} ...".format(REDIRECT_URI))
    print("(On a server: this needs `ssh -L 3000:localhost:3000 user@host` from the\n"
          " machine running the browser, or Ctrl-C and re-run with --manual.)")
    thread.join(timeout=300)
    server.server_close()

    result = _CallbackHandler.result
    if not result:
        sys.exit("Timed out after 5 minutes with no response from Google.")
    if "error" in result:
        sys.exit("Google returned an error: {} — {}".format(
            result["error"], result.get("error_description", "")))
    if result.get("state") != state:
        sys.exit("State mismatch — discarding this response as a safety measure.")

    payload = _store(_exchange(client_id, client_secret, result["code"]), scopes)
    _announce(payload)
    return payload


def _announce(payload):
    print("\nAuthorized as {}{}.".format(
        payload.get("email") or "?",
        " ({})".format(payload["name"]) if payload.get("name") else ""))
    if not payload.get("refresh_token"):
        print("\nWarning: Google returned no refresh token, so this will stop working")
        print("in an hour. That happens when access_type=offline or prompt=consent")
        print("is missing — re-run with --force.")


def drive_token(scopes=None, interactive=True, force_refresh=False):
    """A valid Google access token carrying every scope in `scopes`.

    Reuses the stored token, refreshes inside REFRESH_MARGIN, and only falls
    back to a browser login when there is no other way. interactive=False
    raises instead, which is what the uploader uses so a headless service never
    blocks on a prompt.

    Google's access tokens last an hour against Twitch's four, so a once-a-day
    job refreshes on essentially every run. That is correct, and costs one
    request.
    """
    scopes = list(scopes or [SCOPE_DRIVE_FILE])
    client_id, client_secret = config.load_google_credentials(required=interactive)
    payload = load_token()

    if not client_id:
        raise SystemExit(
            "No Google credentials configured. Run:  {} drive --setup".format(
                config.invocation()))

    if payload and force_refresh:
        payload = refresh(payload, client_id, client_secret, scopes)
    elif payload:
        missing = [s for s in scopes if s not in (payload.get("scopes") or [])]
        if missing:
            combined = sorted(set(scopes) | set(payload.get("scopes") or []))
            if not interactive:
                raise SystemExit(
                    "The stored Google token lacks the {} scope. Run:  {} drive --auth {}".format(
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
            if payload is None and not interactive:
                raise SystemExit(
                    "Google refused to refresh the token (see the log above).\n"
                    "If the consent screen is still in \"Testing\", publish it at\n"
                    "  {}\n"
                    "then re-authorize:  {} drive --auth --force".format(
                        CONSENT_URL, config.invocation()))

    if not payload:
        if not interactive:
            raise SystemExit("No Google Drive authorization. Run:  {} drive --auth".format(
                config.invocation()))
        payload = authorize(client_id, client_secret, scopes)
    return payload


def describe(payload):
    """Print what is stored, for `drive --status`."""
    if not payload:
        print("No Google authorization stored. Run:  {} drive --auth".format(
            config.invocation()))
        return
    left = payload["expires_at"] - time.time()
    print("  account     {}{}".format(
        payload.get("email") or "?",
        " ({})".format(payload["name"]) if payload.get("name") else ""))
    print("  scopes      {}".format(" ".join(payload.get("scopes") or []) or "—"))
    print("  expires in  {}".format("{:.0f} min".format(left / 60) if left > 0 else "expired"))
    print("  refreshable {}".format("yes" if payload.get("refresh_token") else "no"))
    since = payload.get("authorized_at")
    if since:
        print("  authorized  {:.0f} days ago".format((time.time() - since) / 86400))
    print("  accepted    {}".format(
        "yes" if about(payload["access_token"]) else "no — will refresh on next use"))
    print("  stored at   {}".format(config.GOOGLE_TOKEN_PATH))
    if since and time.time() - since > TESTING_WARN_AFTER:
        print("  note        refresh tokens from an app still in \"Testing\" expire after")
        print("              7 days. If uploads start failing with invalid_grant, publish")
        print("              the consent screen at\n              {}".format(CONSENT_URL))
