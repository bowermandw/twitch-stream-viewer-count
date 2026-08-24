"""Poll viewers, followers and chat size into one CSV.

Viewers and followers need only the app token. Chat size needs a user token for
a channel you moderate, so it degrades rather than blocking: without one the
column is left blank and the other two carry on.
"""

import sys
import urllib.error

from .. import api, auth, config, runloop, storage, useroauth
from ..logging import log, use_file

# The loop itself, the tick alignment and the SIGTERM handling live in runloop,
# shared with the YouTube poller. Only the sampling below is Twitch-specific.
CHATTER_FAILURE_LIMIT = 3  # stop asking after this many consecutive failures


def add_arguments(parser):
    parser.add_argument("channel", nargs="?", default=None,
                        help="channel login (default: the configured channel)")
    parser.add_argument("--once", action="store_true", help="one sample, then exit")
    parser.add_argument("--no-chatters", action="store_true",
                        help="skip chat size (avoids needing a user token)")
    parser.add_argument("--viewers-only", action="store_true",
                        help="record viewers alone, to viewers_<channel>.csv")
    parser.add_argument("--interval", type=int, default=None, metavar="SEC",
                        help="seconds between samples (default: TWITCH_INTERVAL, "
                             "else {})".format(config.DEFAULT_INTERVAL_SECONDS))


def _sample_viewers(channel, token, client_id):
    """(stream_or_None, ok). ok is False when the request itself failed."""
    try:
        return api.get_stream(channel, token, client_id), True
    except api.RateLimited as exc:
        log("WARN     viewers rate limited ({}s)".format(exc.retry_after))
    except urllib.error.HTTPError as exc:
        log("WARN     viewers HTTP {}".format(exc.code))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log("WARN     viewers failed: {}".format(exc))
    return None, False


def _sample_total(what, call):
    try:
        return call().get("total")
    except urllib.error.HTTPError as exc:
        log("WARN     {} HTTP {}".format(what, exc.code))
    except (api.RateLimited, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log("WARN     {} failed: {}".format(what, exc))
    return None


def _current_user_token(state, force_refresh=False):
    """A live user token, refreshed as needed.

    Re-read every tick rather than cached for the life of the process: user
    tokens last about four hours, so a long-running service that held the
    startup token would quietly stop collecting chat size after the first
    afternoon. Re-reading also picks up a refresh performed by a sibling
    poller for another channel.
    """
    try:
        payload = useroauth.user_token([api.SCOPE_CHATTERS], interactive=False,
                                       force_refresh=force_refresh)
    except SystemExit as exc:
        log("WARN     user token unavailable — {}".format(str(exc).splitlines()[0]))
        return None
    state["moderator_id"] = str(payload.get("user_id"))
    return payload["access_token"]


def _sample_chatters(state):
    """Chat size, refreshing the user token once if Twitch rejects it."""
    token = _current_user_token(state)
    if not token:
        return None
    try:
        return api.get_chatters(state["broadcaster_id"], state["moderator_id"],
                                token, state["client_id"]).get("total")
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            log("WARN     chatters HTTP {}".format(exc.code))
            return None
        log("auth     user token rejected, refreshing and retrying once")
        token = _current_user_token(state, force_refresh=True)
        if not token:
            return None
        return _sample_total("chatters", lambda: api.get_chatters(
            state["broadcaster_id"], state["moderator_id"], token, state["client_id"]))
    except (api.RateLimited, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log("WARN     chatters failed: {}".format(exc))
    return None


def poll_once(state):
    """Sample everything enabled and append one row. True if a row was written."""
    try:
        app = auth.app_token(state["client_id"], state["client_secret"])
    except Exception as exc:  # noqa: BLE001 - never die mid-run over one failure
        log("ERROR    no app token: {} — skipping sample".format(exc))
        return False

    stream, ok = _sample_viewers(state["channel"], app, state["client_id"])
    if not ok:
        # A rejected token is worth exactly one refresh-and-retry.
        try:
            app = auth.app_token(state["client_id"], state["client_secret"],
                                 force_refresh=True)
            stream, ok = _sample_viewers(state["channel"], app, state["client_id"])
        except Exception as exc:  # noqa: BLE001
            log("ERROR    token refresh failed: {}".format(exc))
            return False

    live = stream is not None
    now = storage.utc_stamp()

    if state["viewers_only"]:
        row = [now, "true" if live else "false",
               stream.get("viewer_count", "") if live else "",
               stream.get("title", "") if live else "",
               stream.get("game_name", "") if live else "",
               stream.get("started_at", "") if live else "",
               stream.get("id", "") if live else ""]
        try:
            storage.append_row(state["csv_path"], storage.VIEWERS_HEADER, row)
        except OSError as exc:
            log("ERROR    could not write CSV: {}".format(exc))
            return False
        log("{}  {}{}".format(state["channel"], "LIVE   " if live else "offline",
                              "  {:>7} viewers".format(row[2]) if live else ""))
        return True

    followers = _sample_total("followers", lambda: api.get_followers(
        state["broadcaster_id"], app, state["client_id"], first=1))

    chatters = None
    if state["chatters_enabled"]:
        chatters = _sample_chatters(state)
        if chatters is None:
            state["chatter_failures"] += 1
            if state["chatter_failures"] >= CHATTER_FAILURE_LIMIT:
                log("WARN     chatters failing repeatedly — leaving the column blank")
                state["chatters_enabled"] = False
        else:
            state["chatter_failures"] = 0

    row = [now, "true" if live else "false",
           stream.get("viewer_count", "") if live else "",
           followers if followers is not None else "",
           chatters if chatters is not None else "",
           stream.get("title", "") if live else "",
           stream.get("game_name", "") if live else "",
           stream.get("started_at", "") if live else "",
           stream.get("id", "") if live else ""]
    try:
        storage.append_row(state["csv_path"], storage.METRICS_HEADER, row)
    except OSError as exc:
        log("ERROR    could not write CSV: {}".format(exc))
        return False

    def show(value):
        return "—" if value in (None, "") else "{:,}".format(int(value))

    log("{}  {}  viewers {:>7}  followers {:>8}  chat {:>5}".format(
        state["channel"], "LIVE   " if live else "offline",
        show(row[2]), show(row[3]), show(row[4])))
    return True


def run(args):
    config.ensure_dirs()
    interval = config.resolve_interval(args.interval)
    channel = config.resolve_channel(args.channel)

    kind = "viewers" if args.viewers_only else "metrics"
    csv_path = (config.viewers_csv(channel) if args.viewers_only
                else config.metrics_csv(channel))
    use_file(config.log_path(channel, kind))

    client_id, client_secret = config.load_credentials()

    broadcaster_id = None
    if not args.viewers_only:
        app = auth.app_token(client_id, client_secret)
        broadcaster_id, _ = api.resolve_user_id(channel, app, client_id)
        if not broadcaster_id:
            sys.exit("No Twitch account called '{}'.".format(channel))

    state = {
        "channel": channel, "client_id": client_id, "client_secret": client_secret,
        "broadcaster_id": broadcaster_id, "csv_path": csv_path,
        "viewers_only": args.viewers_only,
        "chatters_enabled": False, "chatter_failures": 0,
        "moderator_id": None,
    }

    if not args.viewers_only and not args.no_chatters:
        try:
            payload = useroauth.user_token([api.SCOPE_CHATTERS], interactive=False)
            state.update(moderator_id=str(payload.get("user_id")), chatters_enabled=True)
            log("start    chat size enabled as moderator {}".format(payload.get("login")))
        except SystemExit as exc:
            log("start    chat size disabled — {}".format(str(exc).splitlines()[0]))
            log("start    (authorize with: {} auth)".format(config.invocation()))

    if args.once:
        poll_once(state)
        return 0

    return runloop.loop(interval, lambda: poll_once(state), channel, csv_path)
