"""Google Drive v3 over urllib.

Enough of the REST API to put one PNG a day in a per-channel folder: resolve or
create the folder, then create the file or replace the bytes of the one already
there. Failures log a WARN and let the other channels through, the way the
poller treats a missing chat token.
"""

import json
import os
import secrets
import stat
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, driveoauth
from .logging import log

FILES_URL = config.DRIVE_API + "/files"
UPLOAD_URL = config.DRIVE_UPLOAD_API + "/files"
FOLDER_MIME = "application/vnd.google-apps.folder"

MIME_BY_EXT = {".png": "image/png", ".svg": "image/svg+xml",
               ".csv": "text/csv", ".json": "application/json"}
DEFAULT_MIME = "application/octet-stream"

MAX_ATTEMPTS = 4
# 403 means two completely different things. These reasons are "slow down";
# anything else (insufficientFilePermissions, forbidden) is "you may not", and
# retrying that eight times just wastes a minute.
RETRY_REASONS = ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded",
                 "backendError", "internalError")

CRLF = b"\r\n"


class DriveError(Exception):
    """A Drive call that failed in a way the caller should log and move past."""


class DriveHTTPError(DriveError):
    """An HTTP error from Drive, with the body already read.

    urllib lets an HTTPError body be read exactly once, so it is read here and
    carried along — otherwise the retry logic and the log line would be
    fighting over it.
    """

    def __init__(self, status, reason, detail, retry_after=None):
        super().__init__("HTTP {}{} {}".format(status, " " + reason if reason else "", detail))
        self.status = status
        self.reason = reason
        self.detail = detail
        self.retry_after = retry_after


# --------------------------------------------------------------------------
# queries and names
# --------------------------------------------------------------------------


def _escape(value):
    """Escape a value for a single-quoted Drive query literal."""
    # Backslash first, then quote — reversing the order double-escapes.
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def folder_query(name, parent):
    """The files.list `q` that finds one non-trashed folder by name under `parent`."""
    return ("mimeType = '{}' and name = '{}' and '{}' in parents "
            "and trashed = false".format(FOLDER_MIME, _escape(name), _escape(parent)))


def file_query(name, parent):
    """The files.list `q` that finds one non-trashed file by name in `parent`."""
    return ("name = '{}' and '{}' in parents and trashed = false"
            .format(_escape(name), _escape(parent)))


def _list_url(query, fields="files(id,name)", page_size=10):
    """A files.list URL with the query properly encoded."""
    return "{}?{}".format(FILES_URL, urllib.parse.urlencode({
        "q": query, "fields": fields, "spaces": "drive",
        "corpora": "user", "pageSize": page_size}))


def content_type_for(path):
    """Content type from the file extension, octet-stream when unrecognised."""
    return MIME_BY_EXT.get(os.path.splitext(str(path))[1].lower(), DEFAULT_MIME)


def remote_name(day, extension=".png"):
    """The Drive file name for a day: '2026-08-23.png'."""
    return "{}{}".format(day.isoformat(), extension)


def channel_folder_name(channel):
    """The Drive subfolder for a channel — the same slug the CSVs use.

    A Drive name query is case-sensitive, so using the raw login would put IGN
    and ign in two folders while they share one CSV.
    """
    return config.channel_slug(channel)


# --------------------------------------------------------------------------
# retries
# --------------------------------------------------------------------------


def _is_retryable(status, reason):
    """Whether Drive's answer is worth another attempt after a pause."""
    if status in (429, 500, 502, 503, 504):
        return True
    return status == 403 and reason in RETRY_REASONS


def _error_reason_from_bytes(raw):
    """The `reason` out of a Drive JSON error body, or '' if it isn't one."""
    try:
        errors = (json.loads(raw.decode("utf-8", "replace")).get("error") or {})
    except (ValueError, AttributeError):
        return ""
    details = errors.get("errors") or []
    if details and isinstance(details, list):
        return (details[0] or {}).get("reason", "") or ""
    return errors.get("status", "") or ""


def _retry_after(attempt, header=None):
    """Backoff seconds: Retry-After if Drive sent one, else 1, 2, 4, 8 with jitter."""
    if header:
        try:
            return max(1.0, float(str(header).strip()))
        except (TypeError, ValueError):
            pass
    # Jitter kept under half a second so the sequence stays monotonic.
    return 2.0 ** attempt + secrets.randbelow(500) / 1000.0


def _with_backoff(what, call):
    """Retry an idempotent operation through rate limits and 5xx, then give up.

    Wraps the find-then-write helpers, never a bare mutating request: a create
    that timed out may well have succeeded server-side, and replaying its body
    would leave a duplicate folder or a second 2026-08-23.png. Because each
    attempt re-runs its existence check first, a partly-completed earlier
    attempt is discovered and turned into a PATCH or a reuse.
    """
    for attempt in range(MAX_ATTEMPTS):
        try:
            return call()
        except DriveHTTPError as exc:
            last = exc
            if not _is_retryable(exc.status, exc.reason) or attempt == MAX_ATTEMPTS - 1:
                raise
            delay = _retry_after(attempt, exc.retry_after)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt == MAX_ATTEMPTS - 1:
                raise
            delay = _retry_after(attempt)
        log("WARN     {} failed ({}), retrying in {:.0f}s".format(what, last, delay))
        time.sleep(delay)
    raise DriveError("{} gave up after {} attempts".format(what, MAX_ATTEMPTS))


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


def client_for(payload, interactive=False):
    """A client from a token payload already in hand.

    `setup` needs this: it holds a freshly authorized token but has not yet
    written the client id and secret to .env, so it cannot go through connect()
    — which reads them back from there. A just-issued access token needs no
    refresh, so nothing is lost.
    """
    return {"token": payload["access_token"], "email": payload.get("email", ""),
            "interactive": interactive}


def connect(interactive=False):
    """A live access token and the identity behind it, for a run of Drive calls."""
    return client_for(driveoauth.drive_token(interactive=interactive), interactive)


def _call(method, url, client, data=None, content_type=None, timeout=None):
    """One Drive request, refreshing the token once if Google answers 401.

    Mirrors the poller's single refresh-and-retry: a 401 means nothing happened
    server-side, so exactly one retry is safe and one is enough.
    """
    for attempt in (0, 1):
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", "Bearer {}".format(client["token"]))
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(
                    request, timeout=timeout or config.HTTP_TIMEOUT) as response:
                body = response.read()
                return json.loads(body.decode("utf-8")) if body.strip() else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            if exc.code == 401 and attempt == 0:
                log("auth     Google token rejected, refreshing and retrying once")
                client["token"] = driveoauth.drive_token(
                    interactive=False, force_refresh=True)["access_token"]
                continue
            raise DriveHTTPError(
                exc.code, _error_reason_from_bytes(raw),
                raw.decode("utf-8", "replace")[:300],
                exc.headers.get("Retry-After") if exc.headers else None)
    raise DriveError("unreachable")


# --------------------------------------------------------------------------
# folders
# --------------------------------------------------------------------------


def find_folder(client, name, parent="root"):
    """The id of a non-trashed folder called `name` directly under `parent`, or None."""
    files = _call("GET", _list_url(folder_query(name, parent)), client).get("files") or []
    return files[0]["id"] if files else None


def create_folder(client, name, parent="root"):
    """Create a folder under `parent` and return its id."""
    body = json.dumps({"name": name, "mimeType": FOLDER_MIME,
                       "parents": [parent]}).encode("utf-8")
    url = FILES_URL + "?" + urllib.parse.urlencode({"fields": "id,name"})
    return _call("POST", url, client, data=body,
                 content_type="application/json; charset=UTF-8")["id"]


def _load_folder_cache():
    """Remembered folder ids, so a folder renamed by hand is still reused."""
    try:
        with open(config.DRIVE_FOLDERS_PATH, encoding="utf-8") as handle:
            cache = json.load(handle)
        return cache if isinstance(cache, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_folder_cache(cache):
    config.ensure_dirs()
    temporary = config.DRIVE_FOLDERS_PATH + ".tmp{}".format(os.getpid())
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, indent=2)
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    os.replace(temporary, config.DRIVE_FOLDERS_PATH)


def _still_usable(client, folder_id):
    """Whether a cached folder id still names a folder we can write to."""
    url = "{}/{}?{}".format(FILES_URL, urllib.parse.quote(folder_id),
                            urllib.parse.urlencode({"fields": "id,name,trashed"}))
    try:
        info = _call("GET", url, client)
    except DriveHTTPError as exc:
        if exc.status in (403, 404):
            return False
        raise
    return not info.get("trashed")


def folder_path(client, parts):
    """Resolve or create nested folders, e.g. ['Twitch Metrics', 'themeparkgiant'].

    Returns the leaf id. A cached id is verified with files.get before use, so a
    folder you moved or renamed in the Drive UI is followed rather than
    duplicated, and a trashed one is replaced.
    """
    cache = _load_folder_cache()
    dirty = False
    parent = "root"
    trail = []
    for name in parts:
        trail.append(name)
        key = "/".join(trail)
        cached = cache.get(key)
        if cached and _still_usable(client, cached):
            parent = cached
            continue
        if cached:
            dirty = True
        found = find_folder(client, name, parent) or create_folder(client, name, parent)
        if cache.get(key) != found:
            cache[key] = found
            dirty = True
        parent = found
    if dirty:
        _save_folder_cache(cache)
    return parent


def channel_folder(client, channel, folder_name=None):
    """Resolve or create '<folder>/<channel>', returning the subfolder id."""
    top = config.resolve_drive_folder(folder_name)
    return folder_path(client, [top, channel_folder_name(channel)])


# --------------------------------------------------------------------------
# upload
# --------------------------------------------------------------------------


def _new_boundary():
    """A boundary that cannot occur inside arbitrary binary content."""
    return "twitchmetrics-" + secrets.token_hex(16)


def _multipart_body(metadata, content, content_type, boundary):
    """Build a multipart/related body: JSON metadata part, then the raw bytes."""
    if not isinstance(content, bytes):
        raise TypeError("content must be bytes — open the file with 'rb'")
    marker = ("--" + boundary).encode("ascii")
    # CRLF everywhere and a blank line after each part's headers. A bare LF, or
    # a missing blank line, gets "400 Invalid multipart request".
    return (marker + CRLF
            + b"Content-Type: application/json; charset=UTF-8" + CRLF
            + CRLF
            + json.dumps(metadata).encode("utf-8") + CRLF
            + marker + CRLF
            + ("Content-Type: " + content_type).encode("ascii") + CRLF
            + CRLF
            + content + CRLF
            + marker + b"--" + CRLF)


def find_file(client, name, parent):
    """The id of a non-trashed file called `name` in `parent`, or None."""
    files = _call("GET", _list_url(file_query(name, parent)), client).get("files") or []
    return files[0]["id"] if files else None


def upload_file(client, path, name, parent, content_type=None):
    """Create the file, or replace the bytes of one already there."""
    content_type = content_type or content_type_for(path)
    with open(path, "rb") as handle:
        content = handle.read()

    existing = find_file(client, name, parent)
    if existing:
        # Same day, second run. Drive happily keeps duplicate names, so
        # create-always would leave N copies of 2026-08-23.png after N retries.
        # PATCH the bytes instead: one stable address, and Drive keeps the older
        # render as a revision. `parents` must NOT appear in an update body —
        # Drive 400s; moving a file uses addParents/removeParents.
        url = "{}/{}?{}".format(UPLOAD_URL, urllib.parse.quote(existing),
                                urllib.parse.urlencode({
                                    "uploadType": "media",
                                    "fields": "id,name,webViewLink,modifiedTime"}))
        return _call("PATCH", url, client, data=content, content_type=content_type,
                     timeout=config.UPLOAD_TIMEOUT)

    boundary = _new_boundary()
    url = "{}?{}".format(UPLOAD_URL, urllib.parse.urlencode({
        "uploadType": "multipart", "fields": "id,name,webViewLink,createdTime"}))
    body = _multipart_body({"name": name, "parents": [parent]},
                           content, content_type, boundary)
    return _call("POST", url, client, data=body, timeout=config.UPLOAD_TIMEOUT,
                 content_type="multipart/related; boundary=" + boundary)


def upload_chart(client, png_path, channel, day, folder_name=None):
    """Put one chart at '<folder>/<channel>/<YYYY-MM-DD>.png'; returns the file dict."""
    parent = channel_folder(client, channel, folder_name)
    name = remote_name(day, os.path.splitext(png_path)[1] or ".png")
    return upload_file(client, png_path, name, parent)


def target_path(channel, day, folder_name=None, extension=".png"):
    """Where a chart will land, for --dry-run and log lines."""
    return "{}/{}/{}".format(config.resolve_drive_folder(folder_name),
                             channel_folder_name(channel), remote_name(day, extension))


def preflight(interactive=False):
    """Prove the Drive credential works before anything is rendered.

    Raises SystemExit with an actionable message when authorization was never
    done — which is what the systemd journal will show, so it is the whole user
    experience of a misconfigured service.
    """
    client = connect(interactive=interactive)
    log("start    drive as {}".format(client.get("email") or "an unnamed account"))
    return client


def upload_charts(items, folder_name=None, interactive=False):
    """Upload several charts, logging and skipping the ones that fail.

    Returns (uploaded, failed) so one channel's outage can't stop the rest.
    """
    if not items:
        return 0, 0
    try:
        client = preflight(interactive=interactive)
    except SystemExit as exc:
        log("WARN     drive unavailable — {}".format(str(exc).splitlines()[0]))
        return 0, len(items)

    uploaded = failed = 0
    for path, channel, day in items:
        try:
            info = _with_backoff(
                "upload {}".format(channel),
                lambda p=path, c=channel, d=day: upload_chart(client, p, c, d, folder_name))
        except (DriveError, urllib.error.HTTPError, urllib.error.URLError,
                TimeoutError, OSError, ValueError) as exc:
            log("WARN     drive upload failed for {}: {}".format(channel, exc))
            failed += 1
            continue
        log("drive    {} -> {}{}".format(
            channel, target_path(channel, day, folder_name,
                                 os.path.splitext(path)[1] or ".png"),
            "  " + info["webViewLink"] if info.get("webViewLink") else ""))
        uploaded += 1
    return uploaded, failed
