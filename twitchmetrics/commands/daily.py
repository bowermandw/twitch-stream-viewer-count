"""Chart every polled channel for today and upload the PNGs to Drive.

One run, one PNG per channel, driven by a systemd timer at 17:00. The channel
list comes from the enabled twitch-metrics@ instances, so enabling a poller is
the only step needed to add a channel to the report.
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import time

from .. import chart, config, drive, png, storage
from ..logging import log, use_file
from . import graph_cmd

# Enabled template instances show up as symlinks here. Read rather than asked
# over D-Bus: ProtectSystem=strict leaves /etc read-only, not hidden, so this
# needs no privilege and no dbus socket.
WANTS_DIRS = ("/etc/systemd/system/multi-user.target.wants",
              os.path.expanduser("~/.config/systemd/user/default.target.wants"))
UNIT_PREFIX = "twitch-metrics@"
UNIT_SUFFIX = ".service"

# What Twitch allows in a login. systemd-escape only escapes characters outside
# [A-Za-z0-9:_.-], so a real instance name never arrives escaped — anything
# that doesn't match this can be rejected rather than half-unescaped.
LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")

NO_CHANNELS = """No channels to report on.

The list normally comes from the enabled pollers:
    systemctl enable twitch-metrics@yourchannel
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
                        help="render and convert, but don't touch Drive")
    parser.add_argument("--list-channels", action="store_true",
                        help="show the channels that would be reported, and exit")
    parser.add_argument("--folder", default=None, metavar="NAME",
                        help="top-level Drive folder (default: GOOGLE_DRIVE_FOLDER, "
                             "else {})".format(config.DEFAULT_DRIVE_FOLDER))
    parser.add_argument("--width", type=int, default=png.DEFAULT_WIDTH, metavar="PX",
                        help="PNG width handed to rsvg-convert (default {})".format(
                            png.DEFAULT_WIDTH))
    parser.add_argument("--bucket", type=int, default=30, metavar="MIN",
                        help="block size for the average lines (default 30)")
    parser.add_argument("--no-buckets", action="store_true", help="hide the average lines")


# --------------------------------------------------------------------------
# which channels
# --------------------------------------------------------------------------


def units_in(directory):
    """Channels named by twitch-metrics@<channel>.service links in one directory."""
    pattern = os.path.join(directory, UNIT_PREFIX + "*" + UNIT_SUFFIX)
    found = []
    for path in sorted(glob.glob(pattern)):
        name = os.path.basename(path)[len(UNIT_PREFIX):-len(UNIT_SUFFIX)]
        if name:
            found.append(name)
    return found


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


def read_day(channel, day):
    """(status, path) for one channel and day; 'missing' when there's no CSV."""
    path = config.metrics_csv(channel)
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


def render_svg(channel, day, args):
    """Chart one channel's day exactly as `graph --date` would, returning the path."""
    out = config.chart_path(channel, "_" + day.isoformat())
    graph_cmd.run(graph_cmd.default_args(
        channel=channel, date=day.isoformat(), output=out,
        bucket=args.bucket, no_buckets=args.no_buckets))
    return out


def report_channel(channel, day, args, client, converter):
    """Chart, convert and upload one channel. Returns its outcome word."""
    status, path = read_day(channel, day)
    if status == "missing":
        log("WARN     {} — no data file at {}; has it ever been polled?".format(
            channel, os.path.basename(path)))
        return "failed"
    if status == "silent":
        log("WARN     {} — no samples at all on {}; is the poller running?".format(
            channel, day.isoformat()))
        return "failed"
    if status == "dark":
        log("skip     {} — offline all day, nothing to chart".format(channel))
        return "dark"

    try:
        svg = render_svg(channel, day, args)
        image = png.to_png(svg, config.chart_png_path(channel, "_" + day.isoformat()),
                           width=args.width, converter=converter)
    except SystemExit as exc:
        log("WARN     {} — {}".format(channel, str(exc).splitlines()[0]))
        return "failed"
    except (png.ConvertError, OSError) as exc:
        log("WARN     {} — {}".format(channel, exc))
        return "failed"

    log("{}  {} KB -> {}".format(channel, os.path.getsize(image) // 1024,
                                 os.path.basename(image)))
    if args.no_upload:
        return "rendered"

    try:
        info = drive._with_backoff(
            "upload {}".format(channel),
            lambda: drive.upload_chart(client, image, channel, day, args.folder))
    except Exception as exc:  # noqa: BLE001 - one channel's outage isn't the run's
        log("WARN     {} — upload failed: {}".format(channel, exc))
        return "failed"
    log("{}  uploaded to {}{}".format(
        channel, drive.target_path(channel, day, args.folder),
        "  " + info["webViewLink"] if info.get("webViewLink") else ""))
    return "uploaded"


def _drive_preflight(folder):
    """Prove the Drive credential works before rendering anything.

    Ahead of the first render for the same reason the converter check is: don't
    chart four channels only to find the token expired.
    """
    client = drive.preflight(interactive=False)
    log("start    drive folder {}".format(config.resolve_drive_folder(folder)))
    return client


def _list_channels(channels, source, day):
    print("{} channel(s) from {}:\n".format(len(channels), source))
    for channel in channels:
        status, path = read_day(channel, day)
        print("  {:<18} {:<32} {}".format(
            channel,
            os.path.basename(path) if os.path.exists(path) else "(no CSV)",
            {"live": "live on {}".format(day.isoformat()),
             "dark": "offline all day",
             "silent": "no samples on {}".format(day.isoformat()),
             "missing": "never polled"}[status]))
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

    # Before both preflights on purpose: diagnosing discovery on a fresh server
    # shouldn't need librsvg or a Drive token.
    if args.list_channels:
        _list_channels(channels, source, day)
        return 0 if channels else 1

    if not channels:
        sys.exit(NO_CHANNELS.format(
            prog=config.invocation(),
            looked=", ".join("{}{}".format(d, "" if os.path.isdir(d) else " (missing)")
                             for d in _wants_dirs())))

    converter = png.require_converter()
    client = None if args.no_upload else _drive_preflight(args.folder)

    started = time.time()
    log("start    daily report for {} — {} channel(s) from {}".format(
        day.isoformat(), len(channels), source))
    log("start    {} at {}px".format(converter, args.width))

    tally = {"uploaded": 0, "rendered": 0, "dark": 0, "failed": 0}
    for channel in channels:
        tally[report_channel(channel, day, args, client, converter)] += 1

    log("stop     {} in {:.1f}s".format(
        ", ".join("{} {}".format(count, word) for word, count in tally.items() if count),
        time.time() - started))
    return 1 if tally["failed"] else 0
