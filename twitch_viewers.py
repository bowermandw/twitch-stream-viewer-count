#!/usr/bin/env python3
"""Poll the Twitch Helix API for a channel's live viewer count and log it to CSV.

Uses the OAuth client credentials grant (app access token) — no user login,
no scopes required, since live stream data is public.
"""

import argparse
import csv
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_CHANNEL = "themeparkgiant"
INTERVAL_SECONDS = 300  # 5 minutes

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STREAMS_URL = "https://api.twitch.tv/helix/streams"

HTTP_TIMEOUT = 20  # seconds; keeps a hung socket from stalling the loop
TOKEN_REFRESH_MARGIN = 300  # refresh when under 5 min of life remains

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
# The token is per-application, not per-channel, so it is shared across channels.
TOKEN_CACHE_PATH = os.path.join(BASE_DIR, ".token_cache.json")

# Set per-run by configure_paths() so each channel gets its own files.
CSV_PATH = None
LOG_PATH = None

CSV_HEADER = [
    "timestamp_utc",
    "is_live",
    "viewer_count",
    "title",
    "game",
    "started_at",
    "stream_id",
]


# --------------------------------------------------------------------------
# per-channel paths
# --------------------------------------------------------------------------


def channel_slug(channel):
    """Filesystem-safe form of a channel login.

    Twitch logins are already alphanumeric + underscore, but --channel accepts
    arbitrary input, so anything else collapses to an underscore.
    """
    slug = re.sub(r"[^A-Za-z0-9_-]", "_", channel.strip().lower())
    return slug or "channel"


def paths_for(channel):
    """(csv_path, log_path) for a channel — each channel logs to its own files."""
    slug = channel_slug(channel)
    return (
        os.path.join(BASE_DIR, "viewers_{}.csv".format(slug)),
        os.path.join(BASE_DIR, "poll_{}.log".format(slug)),
    )


def configure_paths(channel):
    """Point the module's output files at this channel. Call before logging."""
    global CSV_PATH, LOG_PATH
    CSV_PATH, LOG_PATH = paths_for(channel)


configure_paths(DEFAULT_CHANNEL)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def load_env_file(path):
    """Minimal .env parser: KEY=value, skipping blanks and # comments."""
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values


def load_credentials():
    """Environment wins; .env fills the gaps. Exits naming any missing key."""
    file_values = load_env_file(ENV_PATH)
    client_id = os.environ.get("TWITCH_CLIENT_ID") or file_values.get("TWITCH_CLIENT_ID")
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET") or file_values.get(
        "TWITCH_CLIENT_SECRET"
    )

    missing = [
        name
        for name, value in (
            ("TWITCH_CLIENT_ID", client_id),
            ("TWITCH_CLIENT_SECRET", client_secret),
        )
        if not value
    ]
    if missing:
        sys.exit(
            "Missing credential(s): {}\n"
            "Add them to {} as:\n"
            "  TWITCH_CLIENT_ID=...\n"
            "  TWITCH_CLIENT_SECRET=...\n"
            "See README.md for how to get them from https://dev.twitch.tv/console/apps".format(
                ", ".join(missing), ENV_PATH
            )
        )
    return client_id, client_secret


def resolve_channel(cli_value=None):
    """Channel precedence: command line > TWITCH_CHANNEL env/.env > default."""
    return (
        cli_value
        or os.environ.get("TWITCH_CHANNEL")
        or load_env_file(ENV_PATH).get("TWITCH_CHANNEL")
        or DEFAULT_CHANNEL
    ).strip()


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------


def log(message):
    """Print to console and append the same line to poll.log."""
    stamped = "[{}] {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message)
    print(stamped, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(stamped + "\n")
    except OSError as exc:
        print("(could not write to log file: {})".format(exc), flush=True)


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------


def read_cached_token():
    try:
        with open(TOKEN_CACHE_PATH, encoding="utf-8") as handle:
            cached = json.load(handle)
    except (OSError, ValueError):
        return None
    token = cached.get("access_token")
    expires_at = cached.get("expires_at", 0)
    if not token or expires_at - time.time() < TOKEN_REFRESH_MARGIN:
        return None
    return token


def write_cached_token(token, expires_in):
    payload = {"access_token": token, "expires_at": time.time() + expires_in}
    try:
        with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.chmod(TOKEN_CACHE_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError as exc:
        log("WARN     could not cache token: {}".format(exc))


def request_new_token(client_id, client_secret):
    """Client credentials grant. Raises on failure."""
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }
    ).encode("utf-8")
    request = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
    token = payload["access_token"]
    write_cached_token(token, payload.get("expires_in", 3600))
    log("auth     obtained new app access token (valid ~{} days)".format(
        round(payload.get("expires_in", 3600) / 86400)
    ))
    return token


def get_app_token(client_id, client_secret, force_refresh=False):
    if not force_refresh:
        cached = read_cached_token()
        if cached:
            return cached
    return request_new_token(client_id, client_secret)


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------


class RateLimited(Exception):
    def __init__(self, retry_after):
        super().__init__("rate limited")
        self.retry_after = retry_after


def fetch_stream(login, token, client_id):
    """Return the stream dict, or None when the channel is offline.

    Twitch returns HTTP 200 with an empty data array for an offline channel.
    """
    url = "{}?{}".format(
        STREAMS_URL, urllib.parse.urlencode({"user_login": login, "first": 1})
    )
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", "Bearer {}".format(token))
    request.add_header("Client-Id", client_id)

    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            reset = exc.headers.get("Ratelimit-Reset")
            retry_after = 60
            if reset:
                try:
                    retry_after = max(1, int(float(reset) - time.time()))
                except ValueError:
                    pass
            raise RateLimited(retry_after) from exc
        raise

    data = payload.get("data") or []
    return data[0] if data else None


# --------------------------------------------------------------------------
# csv
# --------------------------------------------------------------------------


def append_row(row):
    """Append one row, writing the header only if the file is new."""
    need_header = not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if need_header:
            writer.writerow(CSV_HEADER)
        writer.writerow(row)


def record(stream, channel):
    """Write one sample and return the console description of it."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if stream is None:
        append_row([now, "false", "", "", "", "", ""])
        return "{}  offline".format(channel)

    viewers = stream.get("viewer_count", "")
    title = stream.get("title", "")
    append_row(
        [
            now,
            "true",
            viewers,
            title,
            stream.get("game_name", ""),
            stream.get("started_at", ""),
            stream.get("id", ""),
        ]
    )
    return '{}  LIVE  {:>6} viewers  "{}"'.format(channel, viewers, title)


# --------------------------------------------------------------------------
# polling
# --------------------------------------------------------------------------


def poll_once(channel, client_id, client_secret):
    """One sample. Returns True if a row was written, False if it was skipped."""
    try:
        token = get_app_token(client_id, client_secret)
    except Exception as exc:  # noqa: BLE001 - never die mid-run over one failure
        log("ERROR    could not get access token: {} — skipping sample".format(exc))
        return False

    try:
        stream = fetch_stream(channel, token, client_id)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            # Token revoked or invalidated early: refresh once and retry.
            log("auth     token rejected (401), refreshing and retrying once")
            try:
                token = get_app_token(client_id, client_secret, force_refresh=True)
                stream = fetch_stream(channel, token, client_id)
            except Exception as retry_exc:  # noqa: BLE001
                log(
                    "ERROR    auth still failing after refresh: {} — check "
                    "TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET in .env".format(retry_exc)
                )
                return False
        else:
            log("ERROR    HTTP {} from Twitch, skipping sample".format(exc.code))
            return False
    except RateLimited as exc:
        log("WARN     rate limited, backing off {}s, skipping sample".format(exc.retry_after))
        time.sleep(min(exc.retry_after, INTERVAL_SECONDS))
        return False
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        log("ERROR    request failed: {} — skipping sample".format(exc))
        return False

    try:
        log(record(stream, channel))
    except OSError as exc:
        log("ERROR    could not write CSV: {}".format(exc))
        return False
    return True


def seconds_until_next_tick():
    """Sleep to the next wall-clock interval boundary so samples don't drift."""
    now = datetime.now()
    seconds_today = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6
    return INTERVAL_SECONDS - (seconds_today % INTERVAL_SECONDS)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  %(prog)s                    poll the default channel (%(default_channel)s)\n"
            "  %(prog)s IGN                poll IGN\n"
            "  %(prog)s IGN --once         one sample from IGN, then exit\n"
            "\n"
            "Each channel writes to its own viewers_<channel>.csv and poll_<channel>.log,\n"
            "so several channels can be polled side by side in separate terminals.\n"
            "The default channel can also be set with TWITCH_CHANNEL in .env."
        ) % {"prog": "python3 twitch_viewers.py", "default_channel": DEFAULT_CHANNEL},
    )
    parser.add_argument(
        "channel",
        nargs="?",
        default=None,
        help="Twitch login name to poll (default: TWITCH_CHANNEL from .env, "
        "else {})".format(DEFAULT_CHANNEL),
    )
    parser.add_argument(
        "--channel",
        dest="channel_flag",
        default=None,
        help="same as the positional argument",
    )
    parser.add_argument("--once", action="store_true", help="poll a single time and exit")
    args = parser.parse_args()

    channel = resolve_channel(args.channel or args.channel_flag)
    configure_paths(channel)

    client_id, client_secret = load_credentials()

    if args.once:
        poll_once(channel, client_id, client_secret)
        return

    log("start    polling {} every {} minutes -> {}".format(
        channel, INTERVAL_SECONDS // 60, os.path.basename(CSV_PATH)
    ))
    log("start    Ctrl-C to stop")

    samples = 0
    try:
        while True:
            if poll_once(channel, client_id, client_secret):
                samples += 1
            delay = seconds_until_next_tick()
            next_at = (datetime.now() + timedelta(seconds=delay)).strftime("%H:%M:%S")
            log("sleep    next poll at {}".format(next_at))
            time.sleep(delay)
    except KeyboardInterrupt:
        print()
        log("stop     stopped after {} sample(s) written to {}".format(
            samples, os.path.basename(CSV_PATH)
        ))


if __name__ == "__main__":
    main()
