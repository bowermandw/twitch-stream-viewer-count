"""App access token via the client credentials grant.

Represents the application rather than a person, needs no login, and is enough
for streams, users and follower counts. Chat size needs a user token instead —
see useroauth.py.
"""

import json
import os
import stat
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config
from .logging import log

REFRESH_MARGIN = 300  # refresh when under 5 minutes of life remain


def _read_cache():
    try:
        with open(config.TOKEN_CACHE_PATH, encoding="utf-8") as handle:
            cached = json.load(handle)
    except (OSError, ValueError):
        return None
    token = cached.get("access_token")
    if not token or cached.get("expires_at", 0) - time.time() < REFRESH_MARGIN:
        return None
    return token


def _write_cache(token, expires_in):
    config.ensure_dirs()
    try:
        with open(config.TOKEN_CACHE_PATH, "w", encoding="utf-8") as handle:
            json.dump({"access_token": token, "expires_at": time.time() + expires_in}, handle)
        os.chmod(config.TOKEN_CACHE_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError as exc:
        log("WARN     could not cache token: {}".format(exc))


def request_new_token(client_id, client_secret):
    """Client credentials grant. Raises on failure."""
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    }).encode("utf-8")
    request = urllib.request.Request(config.TOKEN_URL, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(request, timeout=config.HTTP_TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))

    token = payload["access_token"]
    _write_cache(token, payload.get("expires_in", 3600))
    log("auth     obtained new app access token (valid ~{} days)".format(
        round(payload.get("expires_in", 3600) / 86400)))
    return token


def app_token(client_id, client_secret, force_refresh=False):
    if not force_refresh:
        cached = _read_cache()
        if cached:
            return cached
    return request_new_token(client_id, client_secret)
