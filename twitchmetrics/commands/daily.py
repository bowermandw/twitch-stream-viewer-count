"""Chart every polled channel for today and publish it to that channel's website.

One run, one page per channel, driven by a systemd timer at 17:00. The channel
list comes from the enabled pollers — twitch-metrics@ and youtube-metrics@ — so
enabling one is the only step needed to add a channel to the report.

A channel polled on both platforms gets a graph each; one polled on only one
gets one graph, which is not a failure. The SVG is published as-is, because a
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

from .. import chart, config, storage
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

# Where each platform's samples live, in the order the page shows them.
PLATFORMS = (("twitch", config.metrics_csv), ("youtube", config.youtube_csv))

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


def read_day(channel, day, source=config.metrics_csv):
    """(status, path) for one channel, day and platform; 'missing' when there's no CSV.

    `source` is the platform's path function — config.metrics_csv or
    config.youtube_csv — so the four statuses answer per platform rather than
    per channel.
    """
    path = source(channel)
    if not os.path.exists(path):
        return "missing", path
    try:
        samples = storage.read_samples(path)
    except OSError as exc:
        log("WARN     {} — could not read {}: {}".format(channel, os.path.basename(path), exc))
        return "missing", path
    return classify_day(samples, day), path


# --------------------------------------------------------------------------
# per-channel work
# --------------------------------------------------------------------------


def render_svg(channel, day, args, platform, path):
    """Chart one platform's day exactly as `graph --date` would; returns the SVG path.

    The CSV path is handed to graph as its `channel`, which pick_source()
    already accepts for anything ending in .csv — and already recovers the
    channel name from, so the chart is titled 'testchannel' and not
    'youtube_testchannel'.
    """
    out = config.chart_path(channel, "_{}_{}".format(platform, day.isoformat()))
    graph_cmd.run(graph_cmd.default_args(
        channel=path, date=day.isoformat(), output=out,
        bucket=args.bucket, no_buckets=args.no_buckets))
    return out


def _render_platform(channel, day, args, platform, source):
    """(svg_path_or_None, outcome). None means there is nothing to publish.

    A platform this channel isn't polled on is 'absent', not 'failed' — a
    Twitch-only channel must not fail the run for having no YouTube data.
    """
    status, path = read_day(channel, day, source)
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
        svg = render_svg(channel, day, args, platform, path)
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


def report_channel(channel, day, args):
    """Chart every platform this channel has, publish them, rebuild its page.

    Returns an outcome word for the run's tally. 'dark' when the channel simply
    didn't stream anywhere — a day off is not a failure.
    """
    from .. import s3  # noqa: PLC0415 - lazy on purpose; see the module comment

    rendered = []
    outcomes = []
    for platform, source in PLATFORMS:
        svg, outcome = _render_platform(channel, day, args, platform, source)
        outcomes.append(outcome)
        if svg:
            rendered.append((platform, svg))

    if not rendered:
        if "failed" in outcomes:
            return "failed"
        if "dark" in outcomes:
            return "dark"
        log("WARN     {} — no data for either platform; has it ever been polled?".format(
            channel))
        return "failed"

    if args.no_upload:
        log("{}  {} chart(s) rendered, not published".format(channel, len(rendered)))
        return "failed" if "failed" in outcomes else "rendered"

    try:
        for platform, svg in rendered:
            info = s3.upload_chart(channel, svg, platform, day)
            log("{}  {:<8} -> {}".format(channel, platform, info["url"]))
        page = s3.publish_index(channel, day)
    except SystemExit as exc:
        log("WARN     {} — {}".format(channel, str(exc).splitlines()[0]))
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


def _list_channels(channels, source, day):
    """One line per channel per platform. Needs no credentials and no network."""
    print("{} channel(s) from {}:\n".format(len(channels), source))
    words = {"live": "live on {}".format(day.isoformat()),
             "dark": "offline all day",
             "silent": "no samples on {}".format(day.isoformat()),
             "missing": "not polled here"}
    for channel in channels:
        for platform, csv_for in PLATFORMS:
            status, path = read_day(channel, day, csv_for)
            print("  {:<18} {:<8} {:<34} {}".format(
                channel, platform,
                os.path.basename(path) if os.path.exists(path) else "(no CSV)",
                words[status]))
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
