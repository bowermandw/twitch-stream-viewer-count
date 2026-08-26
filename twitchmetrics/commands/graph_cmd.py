"""Render collected samples as an SVG chart."""

import argparse
import os
import subprocess
import sys

from .. import chart, config, db, store


def add_arguments(parser):
    parser.add_argument("channel", nargs="?", default=None,
                        help="channel name, or a path to a CSV")
    parser.add_argument("--bucket", type=int, default=30, metavar="MIN",
                        help="block size for the average lines (default 30)")
    parser.add_argument("--no-buckets", action="store_true", help="hide the average lines")
    parser.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                        help="chart one calendar day (local time), first live sample to "
                             "last, keeping any offline stretch in between. "
                             "Accepts 'today' and 'yesterday'.")
    parser.add_argument("--list-days", action="store_true",
                        help="list the days with live data and exit")
    parser.add_argument("--session", type=int, default=None, metavar="N",
                        help="which broadcast to chart (default: the most recent)")
    parser.add_argument("--list-sessions", action="store_true",
                        help="list the broadcasts in the file and exit")
    parser.add_argument("--composite", action="store_true",
                        help="all metrics on one plot instead of separate panels")
    parser.add_argument("--only", default=None, metavar="METRIC",
                        help="chart just one metric: {}".format(
                            ", ".join(m["key"] for m in chart.METRICS)))
    parser.add_argument("--platform", default="twitch", choices=("twitch", "youtube"),
                        help="which poller's samples to chart (default twitch); "
                             "ignored when a path to a CSV is given")
    parser.add_argument("--viewers-only", action="store_true",
                        help="chart the viewer line alone. Against a CSV this reads "
                             "viewers_<channel>.csv; against the database there is no "
                             "such split, so it means the same as --only viewers")
    parser.add_argument("--output", default=None, help="output .svg path")
    parser.add_argument("--width", type=int, default=1300)
    parser.add_argument("--height", type=int, default=470)
    parser.add_argument("--open", dest="open_it", action="store_true",
                        help="open the chart when done")


def default_args(**overrides):
    """The Namespace argparse would build for `graph`, for callers that aren't the CLI.

    Lets the daily report drive this command instead of duplicating the render
    branches below: argparse supplies every default, so a flag added here later
    can never leave that caller with a missing attribute.
    """
    parser = argparse.ArgumentParser()
    add_arguments(parser)
    args = parser.parse_args([])  # safe: channel is nargs="?" and the rest optional
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def pick_source(channel, viewers_only, platform="twitch"):
    """Where to read this channel's samples, and what to call it on the chart.

    The .csv test comes first and is unchanged, because two things lean on it:
    charting a fixture or a spool by hand must need no database at all, and the
    smoke tests pass paths in directly.

    Anything else is an account in Postgres. metrics_*.csv used to be preferred
    over viewers_*.csv here because it is a superset; the database has no such
    split -- a viewers-only poller simply leaves the follower and chatter
    columns NULL -- so --viewers-only becomes a question about what to draw
    rather than about which file to open. run() maps it to --only viewers.
    """
    if str(channel).lower().endswith(".csv"):
        # Recover the channel name from the filename so the chart isn't titled
        # "metrics_foo.csv" and named chart_metrics_foo_metrics.svg.
        stem = os.path.basename(channel)[:-4]
        for prefix in ("metrics_", "viewers_", "youtube_"):
            if stem.startswith(prefix):
                stem = stem[len(prefix):]
                break
        return channel, stem
    return store.locator(platform, channel), channel


def run(args):
    config.ensure_dirs()
    channel = args.channel or config.resolve_channel(None)
    source, label = pick_source(channel, args.viewers_only,
                                getattr(args, "platform", "twitch"))

    # Against the database, "viewers only" is about the chart and not the file.
    if args.viewers_only and not store.is_file(source):
        args.only = args.only or "viewers"

    if store.is_file(source):
        if not os.path.exists(source):
            sys.exit("No data file at {}\n"
                     "Collect some first:  {prog} poll {}\n"
                     "Or make fake data:   {prog} testdata testchannel".format(
                         source, label, prog=config.invocation()))
    else:
        # Ahead of the read, so "no database" and "no tables" are told apart
        # from "no such channel" rather than all arriving as an empty chart.
        db.require_readable()
    samples = store.load(source)
    if not samples:
        sys.exit("No samples for {}\n"
                 "Collect some first:  {prog} poll {}\n"
                 "Import an archive:   {prog} db --import\n"
                 "Or make fake data:   {prog} testdata testchannel".format(
                     store.describe(source), label, prog=config.invocation()))

    if args.list_days:
        days = chart.days_present(samples)
        if not days:
            sys.exit("No live samples in {}.".format(store.describe(source)))
        print("{} day(s) with live data in {}:\n".format(len(days), store.describe(source)))
        for day in days:
            window = chart.select_day(samples, day)
            counts = [s["viewers"] for s in window if s["viewers"] is not None]
            down = chart.offline_spans(window)
            print("  {}  {:>9}  peak {:>6}  {} sample{}{}".format(
                day.isoformat(),
                chart.fmt_elapsed((window[-1]["when"] - window[0]["when"]).total_seconds()),
                chart.fmt_count(max(counts)) if counts else "—", len(window),
                "s" * (len(window) != 1),
                "   ({} offline mid-day)".format(
                    chart.fmt_elapsed(sum(b - a for a, b in down))) if down else ""))
        print("\nChart one with --date YYYY-MM-DD.")
        return 0

    day = None
    if args.date:
        try:
            day = chart.parse_day(args.date)
        except ValueError as exc:
            sys.exit(str(exc).replace("Bad date", "Bad --date"))
        window = chart.select_day(samples, day)
        if not window:
            available = chart.days_present(samples)
            sys.exit("No live samples on {} in {}.{}".format(
                day.isoformat(), store.describe(source),
                "\nDays with data: " + ", ".join(d.isoformat() for d in available)
                if available else ""))
        sessions = [window]
    else:
        sessions = chart.split_sessions(samples)

    if not sessions:
        sys.exit("{} has no complete broadcast yet (need 2+ consecutive live samples).\n"
                 "Rows found: {}".format(store.describe(source), len(samples)))

    if args.list_sessions:
        print("{} broadcast(s) in {}:\n".format(len(sessions), store.describe(source)))
        for i, session in enumerate(sessions):
            counts = [x["viewers"] for x in session]
            print("  [{}] {}  {:>9}  peak {:>6}  avg {:>6}  ({} samples)".format(
                i, session[0]["when"].astimezone().strftime("%a %-d %b %-I:%M %p"),
                chart.fmt_elapsed((session[-1]["when"] - session[0]["when"]).total_seconds()),
                chart.fmt_count(max(counts)),
                chart.fmt_count(sum(counts) / len(counts)), len(counts)))
        print("\nChart one with --session N (default is the most recent).")
        return 0

    if day and args.session is not None:
        sys.exit("--date and --session select different things; use one or the other.")
    index = args.session if args.session is not None else len(sessions) - 1
    if not 0 <= index < len(sessions):
        sys.exit("No session {} — the file has {} (0-{}). Try --list-sessions.".format(
            index, len(sessions), len(sessions) - 1))
    session = sessions[index]

    metrics = chart.available_metrics(session)
    if args.only:
        if args.only not in chart.METRIC_BY_KEY:
            sys.exit("Unknown metric '{}'. Choose from: {}".format(
                args.only, ", ".join(m["key"] for m in chart.METRICS)))
        metrics = [m for m in metrics if m["key"] == args.only]
        if not metrics:
            sys.exit("No {} data in {}.".format(args.only, store.describe(source)))

    multi = len(metrics) > 1
    suffix = ""
    if args.composite and multi:
        svg = chart.render_composite(session, label, args.width, max(args.height, 470),
                                     metrics=metrics, day=day)
        suffix = "_composite"
    elif multi:
        svg = chart.render_stacked(session, label, args.bucket, args.width,
                                   show_buckets=not args.no_buckets, metrics=metrics, day=day)
        suffix = "_metrics"
    elif metrics and (day or metrics[0]["key"] != "viewers"):
        # A day window can contain offline rows that only the panel renderer handles.
        svg = chart.render_stacked(session, label, args.bucket, args.width,
                                   show_buckets=not args.no_buckets, metrics=metrics, day=day)
        suffix = "" if (day and metrics[0]["key"] == "viewers") else "_" + metrics[0]["key"]
    else:
        svg = chart.render(session, label, args.bucket, args.width, args.height,
                           show_buckets=not args.no_buckets)

    if day:
        suffix += "_" + day.isoformat()
    out_path = args.output or config.chart_path(label, suffix)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(svg)

    if day:
        down = chart.offline_spans(session)
        print("{}  — {}  ({})".format(label, day.strftime("%a %-d %b %Y"),
                                      store.describe(source)))
        if down:
            print("  {} offline in {} stretch{} mid-day, kept in the chart\n".format(
                chart.fmt_elapsed(sum(b - a for a, b in down)), len(down),
                "es" if len(down) != 1 else ""))
        else:
            print()
    else:
        print("{}  — broadcast {} of {}  ({})\n".format(
            label, index + 1, len(sessions), store.describe(source)))

    chart.print_summary(session, args.bucket, metrics)
    print("\n  chart     {}".format(out_path))

    if args.open_it:
        subprocess.run(["open", out_path], check=False)
    return 0
