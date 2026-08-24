"""Record YouTube concurrent viewers and subscriber count on an interval.

Two numbers per sample, into data/youtube_<channel>.csv:

  viewers      liveStreamingDetails.concurrentViewers of whichever video is
               live right now, blank when the channel is offline.
  subscribers  channels.list statistics.subscriberCount. YouTube rounds this to
               three significant figures, so it steps (1.23M then 1.24M) rather
               than climbing smoothly — the chart is not broken.

Chat size has no equivalent here. YouTube's totalChatCount lives on the
liveBroadcasts resource, which needs OAuth as the channel's own Google account,
so an API key cannot reach it and the column does not exist.
"""

import sys
import urllib.error

from .. import config, runloop, storage, youtube
from ..logging import log, use_file


def add_arguments(parser):
    parser.add_argument("channel", nargs="?", default=None,
                        help="channel handle or UC… id (default: the configured channel)")
    parser.add_argument("--once", action="store_true", help="one sample, then exit")
    parser.add_argument("--interval", type=int, default=None, metavar="SEC",
                        help="seconds between samples (default: YOUTUBE_INTERVAL, "
                             "else {}; minimum {} because of the API quota)".format(
                                 config.DEFAULT_YOUTUBE_INTERVAL_SECONDS,
                                 config.MIN_YOUTUBE_INTERVAL_SECONDS))
    parser.add_argument("--recent", type=int, default=youtube.DEFAULT_RECENT, metavar="N",
                        help="how many recent uploads to check for the live broadcast "
                             "(default {})".format(youtube.DEFAULT_RECENT))
    parser.add_argument("--search", action="store_true",
                        help="find the live video with search.list instead of the uploads "
                             "playlist — more direct, but only {} calls a day".format(
                                 youtube.SEARCH_CALLS_PER_DAY))
    parser.add_argument("--list-recent", dest="list_recent", action="store_true",
                        help="print the recent uploads and their broadcast state, then exit")


# --------------------------------------------------------------------------
# sampling
# --------------------------------------------------------------------------


def _guarded(what, call, reported):
    """(value, ok). Every failure becomes ok=False and at most one WARN line.

    Nothing re-raises: a poller that dies on a bad afternoon loses the whole
    evening, so one failed request is worth exactly one log line. `reported`
    is a set carried across ticks so a standing condition — an exhausted quota,
    a rejected key — is logged once rather than once per interval for the rest
    of the day. HTTPError is reported by code because its .url carries the key.
    """
    try:
        value = call()
    except youtube.QuotaExceeded:
        if "quota" not in reported:
            reported.add("quota")
            log("WARN     {}: quota exhausted — no more samples until it resets at "
                "midnight Pacific".format(what))
    except youtube.KeyRejected as exc:
        if "key" not in reported:
            reported.add("key")
            log("WARN     {}: {} — samples keep failing until the key is fixed".format(
                what, exc))
    except youtube.RateLimited as exc:
        log("WARN     {} rate limited ({}s)".format(what, exc.retry_after))
    except urllib.error.HTTPError as exc:
        log("WARN     {} HTTP {}".format(what, exc.code))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log("WARN     {} failed: {}".format(what, exc))
    else:
        reported.clear()  # a success means any standing condition is over
        return value, True
    return None, False


def _find_live(state):
    if state["search"]:
        return youtube.search_live_video(state["channel_id"], state["key"])
    return youtube.find_live_video(state["uploads"], state["key"], state["recent"])


def poll_once(state):
    """Sample both metrics and append one row. True if a row was written."""
    video, found = _guarded("viewers", lambda: _find_live(state), state["reported"])
    if not found:
        # Unlike Twitch, "no live video" and "the lookup failed" are the same
        # empty answer here, so a failed lookup writes nothing rather than
        # recording an offline stretch that may not have happened.
        return False

    subscribers, _ = _guarded(
        "subscribers",
        lambda: youtube.get_subscribers(state["channel_id"], state["key"])[0],
        state["reported"])

    live = video is not None
    viewers = youtube.concurrent_viewers(video) if live else None
    details = (video.get("liveStreamingDetails") or {}) if live else {}
    snippet = (video.get("snippet") or {}) if live else {}

    row = [storage.utc_stamp(), "true" if live else "false",
           viewers if viewers is not None else "",
           subscribers if subscribers is not None else "",
           snippet.get("title", "") if live else "",
           details.get("actualStartTime", "") if live else "",
           video.get("id", "") if live else ""]
    try:
        storage.append_row(state["csv_path"], storage.YOUTUBE_HEADER, row)
    except OSError as exc:
        log("ERROR    could not write CSV: {}".format(exc))
        return False

    def show(value):
        return "—" if value in (None, "") else "{:,}".format(int(value))

    log("{}  {}  viewers {:>7}  subscribers {:>9}".format(
        state["channel"], "LIVE   " if live else "offline", show(row[2]), show(row[3])))
    return True


# --------------------------------------------------------------------------
# --list-recent
# --------------------------------------------------------------------------


def list_recent(found, key, limit):
    """Show what the uploads playlist knows, so the discovery method can be checked."""
    videos = youtube.recent_videos(found["uploads"], key, limit)
    if not videos:
        sys.exit("{} has no videos in its uploads playlist.".format(found["title"]))

    print("{}  — {} most recent upload(s)\n".format(found["title"], len(videos)))
    for video in videos:
        details = video.get("liveStreamingDetails") or {}
        when = details.get("actualStartTime") or details.get("scheduledStartTime") or ""
        viewers = youtube.concurrent_viewers(video)
        print("  {:<9} {:<17} {:>8}  {}".format(
            youtube.broadcast_state(video),
            when[:16].replace("T", " "),
            "{:,}".format(viewers) if viewers is not None else "—",
            (video.get("snippet") or {}).get("title") or video.get("id") or ""))

    live = youtube.pick_live(videos)
    if live:
        print("\nLive now: {}  (https://youtu.be/{})".format(
            (live.get("snippet") or {}).get("title") or "", live.get("id") or ""))
    else:
        print("\nNothing live in these {}. Either the channel is offline, or the uploads\n"
              "playlist hasn't caught up with a broadcast that just started — compare\n"
              "against --search to tell those apart.".format(len(videos)))
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def _resolve(channel, key):
    """The channel, or exit with something the reader can act on."""
    try:
        found = youtube.resolve_channel(channel, key)
    except youtube.KeyRejected as exc:
        sys.exit("YouTube rejected the API key ({}).\n"
                 "Check YOUTUBE_API_KEY, and that \"YouTube Data API v3\" is enabled\n"
                 "for its project at https://console.cloud.google.com/apis/library"
                 .format(exc.reason))
    except youtube.QuotaExceeded:
        sys.exit("YouTube's daily quota for this key is exhausted; it resets at "
                 "midnight Pacific.")
    except youtube.RateLimited:
        sys.exit("YouTube is rate limiting this key. Try again in a minute.")
    except urllib.error.HTTPError as exc:
        sys.exit("YouTube API returned HTTP {}.".format(exc.code))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        sys.exit("Could not reach the YouTube API: {}".format(exc))

    if not found:
        sys.exit("No YouTube channel with the handle or id '{}'.\n"
                 "The handle is the @name in the channel URL.".format(channel))
    return found


def run(args):
    config.ensure_dirs()

    interval = config.resolve_youtube_interval(args.interval)
    samples_per_day = int(round(86400.0 / interval))
    if args.search and samples_per_day > youtube.SEARCH_CALLS_PER_DAY:
        sys.exit("--search allows only {} calls a day, and a {}s interval needs {}.\n"
                 "Either poll no faster than every {}s, or drop --search and use the\n"
                 "uploads playlist.".format(
                     youtube.SEARCH_CALLS_PER_DAY, interval, samples_per_day,
                     -(-86400 // youtube.SEARCH_CALLS_PER_DAY)))

    channel = config.resolve_youtube_channel(args.channel)
    csv_path = config.youtube_csv(channel)
    use_file(config.log_path(channel, "youtube"))

    key = config.load_youtube_key()
    found = _resolve(channel, key)
    # --search is the one mode that doesn't need the playlist, and --list-recent
    # is entirely about the playlist even when --search is also given.
    if not found["uploads"] and (args.list_recent or not args.search):
        sys.exit("{} has no uploads playlist, so the live video can't be found that "
                 "way.\nTry --search instead.".format(found["title"]))

    if args.list_recent:
        return list_recent(found, key, args.recent)

    state = {
        "channel": channel, "key": key, "channel_id": found["id"],
        "uploads": found["uploads"], "csv_path": csv_path,
        "recent": args.recent, "search": args.search,
        "reported": set(),
    }

    log("start    {} ({})".format(found["title"], found["id"]))
    if not args.once:
        log("start    {} units per sample, about {:,} of {:,} quota units a day".format(
            youtube.UNITS_PER_SAMPLE,
            samples_per_day * youtube.UNITS_PER_SAMPLE, config.YOUTUBE_DAILY_QUOTA))

    if args.once:
        poll_once(state)
        return 0

    return runloop.loop(interval, lambda: poll_once(state), channel, csv_path)
