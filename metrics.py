#!/usr/bin/env python3
"""Poll viewers, followers and chatters together into one CSV.

    python3 metrics.py themeparkgiant

Like twitch_viewers.py, but samples three metrics per tick instead of one:

  viewers    concurrent viewers      (app token; blank when offline)
  followers  total channel followers (app token)
  chatters   accounts joined to chat (user token + moderator:read:chatters)

Chatters need a user token belonging to a moderator of the channel. When that
isn't available the column is left blank and the other two carry on, so this
still works for channels you don't moderate.
"""

import argparse
import csv
import os
import re
import sys
import time
import urllib.error
from datetime import datetime, timedelta, timezone

import chatters as chatters_mod
import followers as followers_mod
import twitch_viewers as tv
import user_info

INTERVAL_SECONDS = 300  # 5 minutes, matching twitch_viewers.py

CSV_HEADER = [
    "timestamp_utc",
    "is_live",
    "viewer_count",
    "follower_count",
    "chatter_count",
    "title",
    "game",
    "started_at",
    "stream_id",
]


def paths_for(channel):
    slug = tv.channel_slug(channel)
    return (
        os.path.join(tv.BASE_DIR, "metrics_{}.csv".format(slug)),
        os.path.join(tv.BASE_DIR, "metrics_{}.log".format(slug)),
    )


# --------------------------------------------------------------------------
# one sample of each metric — each returns None on failure, never raises
# --------------------------------------------------------------------------


def sample_viewers(channel, token, client_id):
    """(stream_dict_or_None, ok) — stream is None when the channel is offline."""
    try:
        return tv.fetch_stream(channel, token, client_id), True
    except tv.RateLimited as exc:
        tv.log("WARN     viewers rate limited ({}s)".format(exc.retry_after))
    except urllib.error.HTTPError as exc:
        tv.log("WARN     viewers HTTP {}".format(exc.code))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        tv.log("WARN     viewers failed: {}".format(exc))
    return None, False


def sample_followers(broadcaster_id, token, client_id):
    try:
        page = followers_mod.fetch_page(broadcaster_id, token, client_id, first=1)
        return page.get("total")
    except urllib.error.HTTPError as exc:
        tv.log("WARN     followers HTTP {}".format(exc.code))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        tv.log("WARN     followers failed: {}".format(exc))
    return None


def sample_chatters(broadcaster_id, moderator_id, token, client_id):
    try:
        page = chatters_mod.fetch_page(broadcaster_id, moderator_id, token, client_id)
        return page.get("total")
    except urllib.error.HTTPError as exc:
        tv.log("WARN     chatters HTTP {}".format(exc.code))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        tv.log("WARN     chatters failed: {}".format(exc))
    return None


# --------------------------------------------------------------------------


def append_row(csv_path, row):
    need_header = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
    with open(csv_path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if need_header:
            writer.writerow(CSV_HEADER)
        writer.writerow(row)


def poll_once(state):
    """Sample all three metrics and append one row. Returns True if written."""
    try:
        app_token = tv.get_app_token(state["client_id"], state["client_secret"])
    except Exception as exc:  # noqa: BLE001
        tv.log("ERROR    no app token: {} — skipping sample".format(exc))
        return False

    stream, ok = sample_viewers(state["channel"], app_token, state["client_id"])
    if not ok:
        # A 401 here is worth one refresh-and-retry, as in twitch_viewers.py.
        app_token = tv.get_app_token(state["client_id"], state["client_secret"],
                                     force_refresh=True)
        stream, ok = sample_viewers(state["channel"], app_token, state["client_id"])

    followers = sample_followers(state["broadcaster_id"], app_token, state["client_id"])

    chatters = None
    if state["chatters_enabled"]:
        chatters = sample_chatters(
            state["broadcaster_id"], state["moderator_id"],
            state["user_token"], state["client_id"])
        if chatters is None:
            state["chatter_failures"] += 1
            if state["chatter_failures"] == 3:
                tv.log("WARN     chatters failing repeatedly — leaving the column blank")
                state["chatters_enabled"] = False
        else:
            state["chatter_failures"] = 0

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    live = stream is not None
    row = [
        now,
        "true" if live else "false",
        stream.get("viewer_count", "") if live else "",
        followers if followers is not None else "",
        chatters if chatters is not None else "",
        stream.get("title", "") if live else "",
        stream.get("game_name", "") if live else "",
        stream.get("started_at", "") if live else "",
        stream.get("id", "") if live else "",
    ]

    try:
        append_row(state["csv_path"], row)
    except OSError as exc:
        tv.log("ERROR    could not write CSV: {}".format(exc))
        return False

    def show(value):
        return "—" if value in (None, "") else "{:,}".format(int(value))

    tv.log("{}  {}  viewers {:>7}  followers {:>8}  chat {:>5}".format(
        state["channel"],
        "LIVE   " if live else "offline",
        show(row[2]), show(row[3]), show(row[4])))
    return True


def seconds_until_next_tick():
    now = datetime.now()
    secs = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6
    return INTERVAL_SECONDS - (secs % INTERVAL_SECONDS)


def main():
    global INTERVAL_SECONDS

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  python3 metrics.py themeparkgiant\n"
               "  python3 metrics.py themeparkgiant --once\n"
               "  python3 metrics.py ign --no-chatters\n")
    parser.add_argument("channel", nargs="?", default=None,
                        help="channel login (default: TWITCH_CHANNEL, else {})".format(
                            tv.DEFAULT_CHANNEL))
    parser.add_argument("--once", action="store_true", help="one sample, then exit")
    parser.add_argument("--no-chatters", action="store_true",
                        help="skip chat size (avoids needing a user token)")
    parser.add_argument("--interval", type=int, default=INTERVAL_SECONDS, metavar="SEC",
                        help="seconds between samples (default {})".format(INTERVAL_SECONDS))
    args = parser.parse_args()

    INTERVAL_SECONDS = max(10, args.interval)

    channel = tv.resolve_channel(args.channel)
    csv_path, log_path = paths_for(channel)
    tv.LOG_PATH = log_path  # so tv.log() writes beside this script's CSV

    client_id, client_secret = tv.load_credentials()

    users = user_info.get_users([channel], client_id, client_secret)
    if not users:
        sys.exit("No Twitch account called '{}'.".format(channel))
    broadcaster_id = users[0]["id"]

    state = {
        "channel": channel,
        "client_id": client_id,
        "client_secret": client_secret,
        "broadcaster_id": broadcaster_id,
        "csv_path": csv_path,
        "chatters_enabled": False,
        "chatter_failures": 0,
        "user_token": None,
        "moderator_id": None,
    }

    # Chat size is optional: set it up if we can, carry on quietly if not.
    if not args.no_chatters:
        try:
            import user_auth
            payload = user_auth.get_user_token([chatters_mod.SCOPE], interactive=False)
            state["user_token"] = payload["access_token"]
            state["moderator_id"] = str(payload.get("user_id"))
            state["chatters_enabled"] = True
            tv.log("start    chat size enabled as moderator {}".format(
                payload.get("login")))
        except SystemExit as exc:
            tv.log("start    chat size disabled — {}".format(str(exc).splitlines()[0]))
            tv.log("start    (authorize with: python3 user_auth.py --scope {})".format(
                chatters_mod.SCOPE))

    if args.once:
        poll_once(state)
        return

    tv.log("start    polling {} every {}s -> {}".format(
        channel, INTERVAL_SECONDS, os.path.basename(csv_path)))
    tv.log("start    Ctrl-C to stop")

    samples = 0
    try:
        while True:
            if poll_once(state):
                samples += 1
            delay = seconds_until_next_tick()
            tv.log("sleep    next poll at {}".format(
                (datetime.now() + timedelta(seconds=delay)).strftime("%H:%M:%S")))
            time.sleep(delay)
    except KeyboardInterrupt:
        print()
        tv.log("stop     stopped after {} sample(s) -> {}".format(
            samples, os.path.basename(csv_path)))


if __name__ == "__main__":
    main()
