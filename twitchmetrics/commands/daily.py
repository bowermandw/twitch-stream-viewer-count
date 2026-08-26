"""Chart every polled channel for today and publish it to that channel's website.

One run, one page per channel, driven by a systemd timer at 17:00. The channel
list comes from the enabled pollers — twitch-metrics@ and youtube-metrics@ — so
enabling one is the only step needed to add a channel to the report.

A channel polled on both platforms gets a graph each; one polled on only one
gets one graph, which is not a failure. Each platform also gets the two
multi-day charts behind the Trends page, built from its whole CSV rather than
today's slice of it. The SVG is published as-is, because a
browser renders it natively — sharper and smaller than the PNG the Drive report
used to convert, and with no binary to install.
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import time

from .. import chart, config, db, store, trends
from ..logging import log, use_file
from . import graph_cmd

# s3 is imported inside the functions that publish, never here: --list-channels
# and --dry-run have to keep working on a machine with no boto3 installed, and
# a module-scope import would drag it in for all of them.

# Enabled template instances show up as symlinks here. Read rather than asked
# over D-Bus: ProtectSystem=strict leaves /etc read-only, not hidden, so this
# needs no privilege and no dbus socket.
WANTS_DIRS = ("/etc/systemd/system/multi-user.target.wants",
              os.path.expanduser("~/.config/systemd/user/default.target.wants"))
# One prefix per poller. A channel polled on both platforms has two units and
# still belongs in the report exactly once.
UNIT_PREFIXES = ("twitch-metrics@", "youtube-metrics@")
UNIT_PREFIX = UNIT_PREFIXES[0]   # kept for the systemctl glob below
UNIT_SUFFIX = ".service"

# The platforms a channel can be polled on, in the order the page shows them.
# This used to pair each name with the function naming its CSV; the samples are
# one table now, and store.locator() builds the address from the platform and
# the channel.
PLATFORMS = ("twitch", "youtube")

# What Twitch allows in a login. systemd-escape only escapes characters outside
# [A-Za-z0-9:_.-], so a real instance name never arrives escaped — anything
# that doesn't match this can be rejected rather than half-unescaped.
LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")

NO_CHANNELS = """No channels to report on.

The list normally comes from the enabled pollers, either platform:
    systemctl enable twitch-metrics@yourchannel
    systemctl enable youtube-metrics@yourchannel
Or name them:
    {prog} daily yourchannel otherchannel
Or set TWITCH_DAILY_CHANNELS=yourchannel,otherchannel in .env

Looked in: {looked}"""


def add_arguments(parser):
    parser.add_argument("channels", nargs="*", metavar="CHANNEL",
                        help="channels to report on (default: the enabled pollers)")
    parser.add_argument("--channel", action="append", dest="named", metavar="LOGIN",
                        help="same as a positional, repeatable — reads better in a unit file")
    parser.add_argument("--date", default="today", metavar="YYYY-MM-DD",
                        help="the day to report, in local time. Accepts 'today' "
                             "(the default) and 'yesterday', for backfilling.")
    parser.add_argument("--dry-run", "--no-upload", dest="no_upload", action="store_true",
                        help="render the charts, but publish nothing")
    parser.add_argument("--list-channels", action="store_true",
                        help="show the channels that would be reported, and exit")
    parser.add_argument("--region", default=None, metavar="NAME",
                        help="AWS region (default: AWS_REGION, else {})".format(
                            config.DEFAULT_AWS_REGION))
    parser.add_argument("--bucket", type=int, default=30, metavar="MIN",
                        help="minutes per block for the chart's average lines "
                             "(default 30) — nothing to do with an S3 bucket")
    parser.add_argument("--no-buckets", action="store_true", help="hide the average lines")
    parser.add_argument("--no-trends", action="store_true",
                        help="skip the multi-day charts and the Trends page")
    parser.add_argument("--peak-days", type=int, default=trends.PEAK_DAYS, metavar="N",
                        help="days with a stream on the peaks chart "
                             "(default {})".format(trends.PEAK_DAYS))
    parser.add_argument("--compare-days", type=int, default=trends.COMPARE_DAYS,
                        metavar="N",
                        help="days with a stream shown behind today on the "
                             "half-hour chart (default {})".format(trends.COMPARE_DAYS))
    parser.add_argument("--calendar-days", action="store_true",
                        help="count calendar days rather than days with a stream, "
                             "so a day off takes a slot and draws a dash")
    parser.add_argument("--lookback", type=int, default=trends.LOOKBACK_DAYS,
                        metavar="N",
                        help="how far back to hunt for a day with a stream "
                             "(default {}); ignored with --calendar-days".format(
                                 trends.LOOKBACK_DAYS))


# --------------------------------------------------------------------------
# which channels
# --------------------------------------------------------------------------


def units_in(directory):
    """Channels named by a poller's @<channel>.service link in one directory.

    Both prefixes are scanned and the result de-duplicated, so a channel with
    a Twitch poller and a YouTube poller is one channel, not two.
    """
    found = []
    for prefix in UNIT_PREFIXES:
        pattern = os.path.join(directory, prefix + "*" + UNIT_SUFFIX)
        for path in sorted(glob.glob(pattern)):
            name = os.path.basename(path)[len(prefix):-len(UNIT_SUFFIX)]
            if name and name not in found:
                found.append(name)
    return sorted(found)


def running_units():
    """Channels systemctl reports as running; () when it can't be asked.

    A fallback only, for a channel someone started without enabling. Any
    failure here is silence, not an error.
    """
    if not shutil.which("systemctl"):
        return ()
    try:
        result = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--no-legend",
             "--plain", UNIT_PREFIX + "*"],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ()
    if result.returncode != 0:
        return ()
    found = []
    for line in result.stdout.splitlines():
        unit = line.split()[0] if line.split() else ""
        if unit.startswith(UNIT_PREFIX) and unit.endswith(UNIT_SUFFIX):
            name = unit[len(UNIT_PREFIX):-len(UNIT_SUFFIX)]
            if name:
                found.append(name)
    return found


def split_list(text):
    """Channel logins from a comma- or space-separated string."""
    return [part for part in re.split(r"[,\s]+", str(text or "").strip()) if part]


def _accept(names):
    """Keep the plausible logins, collapsing IGN and ign into one."""
    seen = set()
    kept = []
    for name in names:
        if not LOGIN_RE.match(name):
            log("WARN     ignoring '{}' — not a channel login".format(name))
            continue
        slug = config.channel_slug(name)
        if slug in seen:
            continue
        seen.add(slug)
        kept.append(name)
    return kept


def _wants_dirs():
    override = os.environ.get("TWITCH_SYSTEMD_WANTS_DIR")
    return [override] if override else list(WANTS_DIRS)


def discover_channels(explicit=None, wants_dirs=None):
    """The channels to report on, and a phrase saying where the list came from.

    Precedence: the command line, TWITCH_DAILY_CHANNELS, the enabled systemd
    instances, then anything systemctl says is running.
    """
    if explicit:
        return _accept(explicit), "the command line"

    from_env = (os.environ.get("TWITCH_DAILY_CHANNELS")
                or config.load_env_file().get("TWITCH_DAILY_CHANNELS"))
    if from_env and split_list(from_env):
        return _accept(split_list(from_env)), "TWITCH_DAILY_CHANNELS"

    explicit_dir = wants_dirs is not None or os.environ.get("TWITCH_SYSTEMD_WANTS_DIR")
    directories = list(wants_dirs) if wants_dirs is not None else _wants_dirs()
    enabled = []
    for directory in directories:
        enabled.extend(units_in(directory))
    if enabled:
        return _accept(enabled), "the enabled systemd units"

    # An explicit wants dir means the caller is describing the whole world, so
    # don't let a real systemd on the same box make the answer non-deterministic.
    if not explicit_dir:
        running = running_units()
        if running:
            return _accept(running), "systemctl (running, not enabled)"

    return [], "nowhere"


# --------------------------------------------------------------------------
# what the day looks like
# --------------------------------------------------------------------------


def classify_day(samples, day):
    """What these rows say about `day`: 'live', 'dark' or 'silent'."""
    same_day = [s for s in samples if s["when"].astimezone().date() == day]
    if not same_day:
        return "silent"
    return "live" if any(s["live"] for s in same_day) else "dark"


def read_day(channel, day, platform="twitch"):
    """(status, source) for one channel, day and platform.

    'missing' used to mean "there is no CSV for this platform". With the samples
    in one table there is no file to be absent, so it now means "this channel
    has no samples on this platform at all" — which answers the same question a
    store without filenames can still answer, and keeps a Twitch-only channel
    reading as 'missing' on YouTube rather than as a fault.

    A database that cannot be reached is deliberately NOT 'missing': reporting
    an outage as "never polled here" would publish today's page as though today
    had no data. It raises instead, and _database_preflight() has already
    stopped the run long before this is reached.
    """
    source = store.locator(platform, channel)
    samples = store.load(source)
    if not samples:
        return "missing", source
    return classify_day(samples, day), source


# --------------------------------------------------------------------------
# per-channel work
# --------------------------------------------------------------------------


def render_svg(channel, day, args, platform, source):
    """Chart one platform's day exactly as `graph --date` would; returns the SVG path.

    Still driven through the graph command rather than duplicating its render
    branches. It used to be handed a CSV path as its `channel`, which
    pick_source() accepted; now it is handed the channel and the platform, which
    is what that hack was standing in for all along.
    """
    out = config.chart_path(channel, "_{}_{}".format(platform, day.isoformat()))
    graph_cmd.run(graph_cmd.default_args(
        channel=channel, platform=platform, date=day.isoformat(), output=out,
        bucket=args.bucket, no_buckets=args.no_buckets))
    return out


def _render_platform(channel, day, args, platform):
    """(svg_path_or_None, outcome). None means there is nothing to publish.

    A platform this channel isn't polled on is 'absent', not 'failed' — a
    Twitch-only channel must not fail the run for having no YouTube data.
    """
    status, source = read_day(channel, day, platform)
    if status == "missing":
        return None, "absent"
    if status == "silent":
        # No rows at all for that day. On today that means the poller isn't
        # running and is worth failing the run over. On an older date it just
        # means collection started later, or the channel streams on the other
        # platform that day — backfilling last week must not report a fault.
        if day != chart.parse_day("today"):
            log("skip     {} {} — no samples on {}, nothing to backfill".format(
                channel, platform, day.isoformat()))
            return None, "absent"
        log("WARN     {} {} — no samples at all today; is the poller running?".format(
            channel, platform))
        return None, "failed"
    if status == "dark":
        log("skip     {} {} — offline all day, nothing to chart".format(channel, platform))
        return None, "dark"
    try:
        svg = render_svg(channel, day, args, platform, source)
    except SystemExit as exc:
        log("WARN     {} {} — {}".format(channel, platform, str(exc).splitlines()[0]))
        return None, "failed"
    except OSError as exc:
        log("WARN     {} {} — {}".format(channel, platform, exc))
        return None, "failed"
    log("{}  {:<8} {} KB -> {}".format(channel, platform,
                                       os.path.getsize(svg) // 1024,
                                       os.path.basename(svg)))
    return svg, "rendered"


def render_cross_platform(channel, day, live_points):
    """Chart every platform's viewers together, or None if fewer than two have data.

    Written straight to charts/ rather than through graph_cmd, because `graph`
    charts one platform and this is the one chart that spans them.

    The minute grid comes from the database rather than from these samples. It
    is the same grid -- tests/smoke.py holds the SQL and chart.align_platforms()
    to producing identical output -- but building it in SQL is what makes the
    combined chart readable from stored rows instead of by re-reading every
    sample on both platforms. A store that cannot answer falls back to aligning
    them here, because a chart is worth more than where its grid came from.
    """
    series = [dict(spec, points=live_points.get(spec["key"]) or [])
              for spec in chart.PLATFORMS if live_points.get(spec["key"])]
    if len(series) < 2:
        return None
    aligned = None
    try:
        aligned = store.channel_minutes(channel, day, [e["key"] for e in series])
    except (db.Unreachable, db.NotConfigured, SystemExit) as exc:
        log("WARN     {} — minute grid from samples instead: {}".format(
            channel, str(exc).splitlines()[0]))
    if aligned and not aligned[0]:
        aligned = None
    svg = chart.render_platforms(series, channel, day, aligned=aligned)
    if not svg:
        return None
    out = config.chart_path(channel, "_combined_" + day.isoformat())
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(svg)
    return out


def render_trend_charts(channel, day, args):
    """The multi-day charts for every platform with history; [(kind, platform, path)].

    Reads the window out of the report tables rather than re-deriving it from
    every sample the channel has ever produced. The poller has been filling
    tm.report_daily_peak and tm.report_clock_bucket on every sample since the
    samples moved into Postgres; this is the first thing to read them.

    A platform with nothing in the window still contributes nothing, so a
    channel that has only ever streamed on Twitch gets two charts and not four.
    There is deliberately no "has this platform any samples" guard any more and
    none is needed: render_peaks() returns None when no day has a peak and
    render_typical() returns None when no slot has a bar, so an unpolled
    platform falls out by itself. Re-introducing the guard would mean loading
    every sample again, which is the one thing this stopped doing.

    Written straight to charts/ under a name with no date in it, because they
    describe where the channel is now: each run replaces them.
    """
    # Once per channel and ahead of the loop, because tm.refresh_range() walks
    # every platform on the channel itself -- calling it per platform would do
    # the whole job twice. Its failure is logged and swallowed: the reads below
    # report their own, and today's page is worth more than the trend charts.
    #
    # A streamed axis reaches back as far as the lookback allows, so the tables
    # have to hold that whole range or the older streams it wants are simply
    # absent. ensure_reports() clamps the request to the channel's own first
    # sample, which is what stops a young channel re-refreshing 90 days forever.
    span = (max(1, args.peak_days, args.compare_days + 1) if args.calendar_days
            else max(1, args.lookback))
    try:
        store.ensure_reports(channel, day, span, minutes=args.bucket)
    except (db.Unreachable, db.NotConfigured, SystemExit) as exc:
        log("WARN     {} — report tables not refreshed: {}".format(
            channel, str(exc).splitlines()[0]))

    made = []
    for platform in PLATFORMS:
        try:
            peaks = store.daily_peaks(channel, platform, day, args.peak_days,
                                      calendar=args.calendar_days,
                                      lookback=args.lookback)
            slots, per_day, dropped = store.compare_slots(
                channel, platform, day, args.compare_days, args.bucket,
                calendar=args.calendar_days, lookback=args.lookback)
        except (db.Unreachable, db.NotConfigured, SystemExit) as exc:
            log("WARN     {} {} — no trend charts: {}".format(
                channel, platform, str(exc).splitlines()[0]))
            continue
        charts = trends.render_from(peaks, slots, per_day, channel, platform, day,
                                    minutes=args.bucket, dropped=dropped)
        for kind, svg in charts.items():
            out = config.chart_path(channel, "_{}_{}".format(kind, platform))
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            with open(out, "w", encoding="utf-8") as handle:
                handle.write(svg)
            log("{}  {:<8} {} KB -> {}".format(channel, kind,
                                               os.path.getsize(out) // 1024,
                                               os.path.basename(out)))
            made.append((kind, platform, out))
    return made


def site_line(channel):
    """The channel's website address, or the command that would create one.

    Reads the local registry only — no credentials, no network — so this is
    safe on every path, including --list-channels and a run with no boto3.
    """
    from .. import s3  # noqa: PLC0415 - lazy on purpose; see the module comment

    known = s3.bucket_for(channel)
    if known:
        return s3.website_url(known["bucket"], known["region"])
    return "no bucket yet — {} s3 --setup {}".format(config.invocation(), channel)


def warn_exit(channel, exc):
    """Log a SystemExit in full rather than only its first line.

    require_bucket() raises two lines: what is wrong, and the command that
    fixes it. Keeping only the first threw away the actionable half, so a
    channel that had never been through `s3 --setup` reported "No S3 bucket"
    and never said how to get one.
    """
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()] or [""]
    log("WARN     {} — {}".format(channel, lines[0]))
    for line in lines[1:]:
        log("WARN     {}   {}".format(channel, line))


def _publish_trends(channel, day, charts):
    """Upload the multi-day charts and rebuild the Trends page; True if it exists.

    Failure is logged and swallowed. Today's page is what the run exists to
    produce, and a ten-day chart that wouldn't build must neither stop it nor
    turn a good run red.
    """
    from .. import s3  # noqa: PLC0415 - lazy on purpose; see the module comment

    try:
        if not charts:
            # Nothing new to upload, but an earlier run's page may still be
            # there, and the index's link to it should keep working.
            return bool(s3.list_trends(channel))
        for kind, platform, path in charts:
            info = s3.upload_trend(channel, path, kind, platform)
            log("{}  {:<8} -> {}".format(channel, kind, info["url"]))
        page = s3.publish_trends(channel, day)
        log("{}  trends page rebuilt from {} chart(s): {}".format(
            channel, page["charts"], page["url"]))
        return page["charts"] > 0
    except SystemExit as exc:
        warn_exit(channel, exc)
        return False
    except Exception as exc:  # noqa: BLE001 - the day's page still has to go out
        log("WARN     {} — trends not published: {}".format(channel, exc))
        return False


def day_title(channel, day, platform):
    """The title that platform's stream carried, or "" if there is none.

    The last one seen, not the first: a title edited mid-broadcast is usually
    being corrected, so what it ended as is what the day was called.
    """
    samples = store.load(store.locator(platform, channel))
    titles = [s["title"] for s in samples
              if s["live"] and s["title"]
              and s["when"].astimezone().date() == day]
    return titles[-1] if titles else ""


def day_points(channel, day, platform):
    """(when, viewers) for one platform's live samples on one day."""
    samples = store.load(store.locator(platform, channel))
    return [(s["when"], s["viewers"]) for s in samples
            if s["live"] and s["viewers"] is not None
            and s["when"].astimezone().date() == day]


def report_channel(channel, day, args):
    """Chart every platform this channel has, publish them, rebuild its page.

    Returns an outcome word for the run's tally. 'dark' when the channel simply
    didn't stream anywhere — a day off is not a failure.
    """
    from .. import s3  # noqa: PLC0415 - lazy on purpose; see the module comment

    # First, and unconditionally. A day the channel didn't stream, a --dry-run
    # and a failed upload all used to end without the address anywhere in the
    # output, leaving the only copy of a random bucket suffix in a JSON file
    # nobody thinks to open.
    log("{}  {:<8} {}".format(channel, "site", site_line(channel)))

    rendered = []
    outcomes = []
    live_points = {}
    titles = {}
    for platform in PLATFORMS:
        svg, outcome = _render_platform(channel, day, args, platform)
        outcomes.append(outcome)
        if svg:
            rendered.append((platform, svg))
            live_points[platform] = day_points(channel, day, platform)
            title = day_title(channel, day, platform)
            if title:
                titles[platform] = title

    # Prepended, so it is uploaded and shown before the per-platform panels.
    cross = render_cross_platform(channel, day, live_points)
    if cross:
        rendered.insert(0, ("combined", cross))
        log("{}  {:<8} {} KB -> {}".format(channel, "combined",
                                           os.path.getsize(cross) // 1024,
                                           os.path.basename(cross)))

    if not rendered:
        if "failed" in outcomes:
            return "failed"
        if "dark" in outcomes:
            return "dark"
        log("WARN     {} — no data for either platform; has it ever been polled?".format(
            channel))
        return "failed"

    trend_charts = [] if args.no_trends else render_trend_charts(channel, day, args)

    if args.no_upload:
        log("{}  {} chart(s) rendered, not published".format(
            channel, len(rendered) + len(trend_charts)))
        return "failed" if "failed" in outcomes else "rendered"

    try:
        for platform, svg in rendered:
            info = s3.upload_chart(channel, svg, platform, day)
            log("{}  {:<8} -> {}".format(channel, platform, info["url"]))
        # Ahead of the index, and swallowing its own errors: the index needs to
        # know whether there is a Trends page to link to, and must go out either
        # way.
        has_trends = _publish_trends(channel, day, trend_charts)
        page = s3.publish_index(channel, day, titles, trends=has_trends)
    except SystemExit as exc:
        warn_exit(channel, exc)
        return "failed"
    except Exception as exc:  # noqa: BLE001 - one channel's outage isn't the run's
        log("WARN     {} — publish failed: {}".format(channel, exc))
        return "failed"

    log("{}  page rebuilt from {} day(s): {}".format(channel, page["days"], page["url"]))
    # Publish first, then report the failure: a platform that went quiet must
    # still reach the exit code, or a dead poller stays invisible in the timer's
    # journal — but the platform that did work should still be on the page.
    return "failed" if "failed" in outcomes else "published"


def _publish_preflight(region):
    """Prove the AWS credential works before rendering anything.

    Ahead of the first render on purpose: charting four channels and then
    finding the key is wrong wastes the run and reads as a chart bug.
    """
    from .. import s3  # noqa: PLC0415 - lazy on purpose; see the module comment

    s3.preflight(region)
    return True


def _database_preflight():
    """Prove the samples can be read before rendering anything.

    Beside the AWS preflight and for the same reason: charting four channels and
    only then discovering the store is unreachable wastes the run and reads as a
    chart bug rather than a connectivity one.

    Deliberately no CSV fallback. The pollers keep a spool so an outage loses
    nothing, but a page quietly built from whatever happened to be spooled would
    be worse than no page — it would look complete and be wrong.
    """
    db.require_readable()
    return True


def _list_channels(channels, source, day):
    """One line per channel per platform. Needs no credentials and no network."""
    print("{} channel(s) from {}:\n".format(len(channels), source))
    words = {"live": "live on {}".format(day.isoformat()),
             "dark": "offline all day",
             "silent": "no samples on {}".format(day.isoformat()),
             "missing": "not polled here"}
    # Probed once, and reported as a heading rather than smeared across every
    # row. Diagnosing channel discovery on a fresh server is exactly when the
    # database is most likely to be the thing that is wrong, so this has to keep
    # working when it is — the same rule the module comment states for boto3 —
    # but repeating a connection error once per channel per platform buries the
    # list it was asked for.
    reachable, why = db.probe()
    if not reachable:
        print("  The sample store is unavailable, so only the channel list "
              "below is real:\n    {}\n".format(why))

    for channel in channels:
        print("  {:<18} {}".format(channel, site_line(channel)))
        for platform in PLATFORMS:
            if not reachable:
                print("  {:<18} {:<8} {}".format(channel, platform, "unknown"))
                continue
            status, _ = read_day(channel, day, platform)
            rows = len(store.load(store.locator(platform, channel)))
            held = "{:,} sample(s)".format(rows) if rows else "nothing stored"
            print("  {:<18} {:<8} {:<20} {}".format(
                channel, platform, held, words[status]))
    print()


def run(args):
    config.ensure_dirs()
    use_file(config.daily_log_path())

    try:
        day = chart.parse_day(args.date)
    except ValueError as exc:
        sys.exit(str(exc).replace("Bad date", "Bad --date"))

    explicit = list(args.channels) + list(args.named or [])
    channels, source = discover_channels(explicit)

    # Before the preflight on purpose: diagnosing discovery on a fresh server
    # shouldn't need boto3 installed or a working AWS key.
    if args.list_channels:
        _list_channels(channels, source, day)
        return 0 if channels else 1

    if not channels:
        sys.exit(NO_CHANNELS.format(
            prog=config.invocation(),
            looked=", ".join("{}{}".format(d, "" if os.path.isdir(d) else " (missing)")
                             for d in _wants_dirs())))

    # The database first: it is needed whether or not anything is published, and
    # a --dry-run that renders nothing because the store is down should say so
    # rather than reporting every channel as having no data.
    _database_preflight()

    if not args.no_upload:
        _publish_preflight(args.region)

    started = time.time()
    log("start    daily report for {} — {} channel(s) from {}".format(
        day.isoformat(), len(channels), source))

    tally = {"published": 0, "rendered": 0, "dark": 0, "failed": 0}
    for channel in channels:
        tally[report_channel(channel, day, args)] += 1

    log("stop     {} in {:.1f}s".format(
        ", ".join("{} {}".format(count, word) for word, count in tally.items() if count),
        time.time() - started))
    return 1 if tally["failed"] else 0
