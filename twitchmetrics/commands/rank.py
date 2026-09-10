"""Record how far down its category directory each channel sits.

The question this answers is "how many streams down twitch.tv/directory/
category/irl am I?", and it answers it from the official API rather than by
scraping the page. Helix returns a category sorted by viewer count descending,
which is the ordering the page itself shows under "Sort by: Viewers (High to
Low)" -- so walking the cursor and counting is both supported and cheap.

THE PASS IS CATEGORY-MAJOR, not channel-major, and that is the whole of why this
is affordable. One batched request tells us which tracked channels are live and
what category each is in; then each distinct category is walked exactly once, no
matter how many tracked channels are in it. Two channels in IRL cost 1 + 7
requests, not 2 + 14 -- and, more importantly, they are ranked against the same
snapshot with the same timestamp, so "who was higher at 14:20" has an answer.

The cost, measured: IRL is 532 streams in 7 requests and 1.3 seconds; Just
Chatting, the largest category there is, is 6,077 in 66 requests and 17.7. The
allowance is 800 requests per minute. A ten-minute cadence therefore uses about a
tenth of one percent of it.
"""

import time
import urllib.error

from .. import api, auth, config, db, directory, runloop, storage, store
from ..logging import log, use_file
from . import daily


def add_arguments(parser):
    parser.add_argument("channel", nargs="*", default=None,
                        help="channel logins (default: the configured channel)")
    parser.add_argument("--all", action="store_true",
                        help="every channel with a poller enabled")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="walk the directory and print, but write nothing")
    parser.add_argument("--interval", type=int, default=None, metavar="SEC",
                        help="seconds between passes (default: "
                             "TWITCH_RANK_INTERVAL, else {})".format(
                                 config.DEFAULT_RANK_INTERVAL_SECONDS))
    parser.add_argument("--max-pages", type=int, default=None, metavar="N",
                        help="stop walking a category after this many pages "
                             "(default: {})".format(api.MAX_CATEGORY_PAGES))


def _live_streams(logins, token, client_id):
    """({login: stream}, ok) for whichever of `logins` are live right now.

    One request for up to a hundred channels. `ok` is False when the request
    itself failed, which the caller distinguishes from "everybody is offline".
    """
    try:
        found = api.get_streams(logins, token, client_id)
    except api.RateLimited as exc:
        log("WARN     stream lookup rate limited ({}s)".format(exc.retry_after))
        return {}, False
    except urllib.error.HTTPError as exc:
        log("WARN     stream lookup HTTP {}".format(exc.code))
        return {}, False
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log("WARN     stream lookup failed: {}".format(exc))
        return {}, False
    return {(s.get("user_login") or "").lower(): s for s in found}, True


def _walk(game_id, token, client_id, max_pages):
    """(streams, pass_info) for one category, or (None, None) if it failed.

    A category walked but abandoned partway is still useful, and the caller is
    told so rather than being handed a total it would mistake for a count: see
    listing_complete in 012_directory_rank.sql.
    """
    started = time.time()
    try:
        streams, pages, complete = api.category_streams(
            game_id, token, client_id, max_pages=max_pages)
    except api.RateLimited as exc:
        log("WARN     category {} rate limited ({}s) — skipping".format(
            game_id, exc.retry_after))
        return None, None
    except urllib.error.HTTPError as exc:
        log("WARN     category {} HTTP {}".format(game_id, exc.code))
        return None, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log("WARN     category {} failed: {}".format(game_id, exc))
        return None, None

    # Deduped before it is counted, and before anything is ranked against it:
    # twelve to twenty-two of these rows are the same channel arriving twice
    # because the list moved under the cursor. See directory.dedupe().
    streams = directory.dedupe(streams)
    return streams, {
        "total_streams": len(streams),
        "pages_read": pages,
        "page_cap": max_pages,
        "pass_seconds": int(round(time.time() - started)),
        "listing_complete": complete,
    }


def _write(state, login, when, rank, stream, pass_info):
    """Store one channel's rank. True when a row landed."""
    slug = config.channel_slug(login)
    try:
        # create=False, unlike the pollers' startup lookup. A poller creates the
        # channel and account rows the moment it starts, so on any box that
        # actually tracks this channel they already exist -- and a ranker that
        # created them would leave an orphan channel behind every time somebody
        # mistyped a login, or ranked a channel nothing polls.
        account = store.account_id(slug, "twitch", create=False)
        if account is None:
            log("WARN     nothing polls {} — enable twitch-metrics@{} to record "
                "its rank".format(login, slug))
            return False
        rank_id = store.record_rank(
            account, when, stream.get("id"), stream.get("game_id"), pass_info,
            rank=rank, game_name=stream.get("game_name"))
    except (db.NotConfigured, db.Unreachable) as exc:
        # One line per outage rather than one per channel per tick, the
        # `reported` idiom the pollers use. Nothing is spooled: see
        # store.record_rank() for why a rank is not worth a second on-disk
        # format.
        if "db" not in state["reported"]:
            state["reported"].add("db")
            log("WARN     no database — skipping this pass ({})".format(
                str(exc).splitlines()[0]))
        return False
    state["reported"].discard("db")

    if rank_id is None:
        # The ranker saw a broadcast the poller has not recorded yet. Nothing to
        # attach the rank to, and inventing a tm.stream row here would race the
        # poller for the one-open-stream-per-account index.
        log("WARN     {} has no broadcast row yet — the poller is behind".format(
            login))
        return False
    return True


def rank_once(state):
    """One directory pass over every tracked channel. True if a row was written."""
    try:
        app = auth.app_token(state["client_id"], state["client_secret"])
    except Exception as exc:  # noqa: BLE001 - never die mid-run over one failure
        log("ERROR    no app token: {} — skipping pass".format(exc))
        return False

    live, ok = _live_streams(state["channels"], app, state["client_id"])
    if not ok:
        # A rejected token is worth exactly one refresh-and-retry, as in poll.py.
        try:
            app = auth.app_token(state["client_id"], state["client_secret"],
                                 force_refresh=True)
            live, ok = _live_streams(state["channels"], app, state["client_id"])
        except Exception as exc:  # noqa: BLE001
            log("ERROR    token refresh failed: {}".format(exc))
            return False
        if not ok:
            return False

    if not live:
        # Nothing to rank, and deliberately no directory request: a channel that
        # is not streaming is not in the directory, so there is no position to
        # record and no reason to spend 7 to 66 requests discovering that.
        #
        # Channels discovered from youtube-metrics@ units land here too, and
        # that needs no special case -- a YouTube-only channel is simply never
        # in a Twitch listing.
        log("offline  none of {} channel(s) are live".format(
            len(state["channels"])))
        return False

    # One timestamp for the whole pass, shared by every channel ranked from it.
    when = storage.utc_stamp()

    by_game = {}
    for login, stream in live.items():
        game_id = stream.get("game_id")
        if not game_id:
            # Live with no category set. Helix allows it and there is no
            # directory page to be on, so there is no rank to record.
            log("WARN     {} is live with no category set".format(login))
            continue
        by_game.setdefault(game_id, []).append(login)

    wrote = False
    for game_id, logins in sorted(by_game.items()):
        streams, pass_info = _walk(game_id, app, state["client_id"],
                                   state["max_pages"])
        if streams is None:
            continue
        if not pass_info["total_streams"]:
            # A contradiction rather than an edge case: a channel of ours is
            # live in this category. Either it went dark between the two
            # requests or Helix answered oddly; either way total_streams = 0
            # would be a fabrication, and the schema refuses it.
            log("WARN     category {} came back empty — skipping".format(game_id))
            continue
        if not pass_info["listing_complete"]:
            log("WARN     category {} hit the {}-page cap — totals are lower "
                "bounds".format(game_id, pass_info["page_cap"]))

        for login in sorted(logins):
            found = directory.rank_in(streams, login)
            log("{}  ({} pages, {}s)".format(
                directory.describe(found, login, live[login].get("game_name")),
                pass_info["pages_read"], pass_info["pass_seconds"]))
            if state["dry_run"]:
                continue
            if _write(state, login, when, found, live[login], pass_info):
                wrote = True

    return wrote


def run(args):
    # ensure_data_dir() and not ensure_dirs(): this job renders nothing, so its
    # unit grants ReadWritePaths on data/ alone.
    config.ensure_data_dir()
    interval = config.resolve_rank_interval(args.interval)
    max_pages = args.max_pages or api.MAX_CATEGORY_PAGES

    # Three ways in, and discover_channels() already owns two of them -- it
    # validates logins and collapses IGN and ign, which is worth reusing rather
    # than reimplementing. --all is what the systemd unit passes: it reads the
    # enabled twitch-metrics@* instances, so enabling a poller for a new channel
    # starts ranking it too, with no second list to remember.
    if args.channel:
        channels, where = daily.discover_channels(args.channel)
    elif args.all:
        channels, where = daily.discover_channels()
    else:
        channels, where = [config.resolve_channel(None)], "the configured channel"
    if not channels:
        log("start    no channels to rank")
        return 0

    use_file(config.rank_log_path())
    client_id, client_secret = config.load_credentials()

    state = {
        "channels": channels, "client_id": client_id,
        "client_secret": client_secret, "max_pages": max_pages,
        "dry_run": args.dry_run, "reported": set(),
    }

    log("start    ranking {} from {}".format(", ".join(channels), where))
    if args.once:
        rank_once(state)
        return 0

    return runloop.loop(interval, lambda: rank_once(state), ", ".join(channels),
                        "tm.directory_rank")
