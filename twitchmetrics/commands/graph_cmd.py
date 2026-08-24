"""Render collected samples as an SVG chart."""

import os
import subprocess
import sys
from datetime import datetime, timedelta

from .. import chart, config, storage


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
    parser.add_argument("--viewers-only", action="store_true",
                        help="read viewers_<channel>.csv even if metrics data exists")
    parser.add_argument("--output", default=None, help="output .svg path")
    parser.add_argument("--width", type=int, default=1300)
    parser.add_argument("--height", type=int, default=470)
    parser.add_argument("--open", dest="open_it", action="store_true",
                        help="open the chart when done")


def pick_source(channel, viewers_only):
    """metrics_*.csv is a superset, so prefer it unless asked otherwise."""
    if str(channel).lower().endswith(".csv"):
        # Recover the channel name from the filename so the chart isn't titled
        # "metrics_foo.csv" and named chart_metrics_foo_metrics.svg.
        stem = os.path.basename(channel)[:-4]
        for prefix in ("metrics_", "viewers_", "youtube_"):
            if stem.startswith(prefix):
                stem = stem[len(prefix):]
                break
        return channel, stem
    metrics_path = config.metrics_csv(channel)
    viewers_path = config.viewers_csv(channel)
    if viewers_only or not os.path.exists(metrics_path):
        return viewers_path, channel
    return metrics_path, channel


def run(args):
    config.ensure_dirs()
    channel = args.channel or config.resolve_channel(None)
    path, label = pick_source(channel, args.viewers_only)

    if not os.path.exists(path):
        sys.exit("No data file at {}\n"
                 "Collect some first:  {prog} poll {}\n"
                 "Or make fake data:   {prog} testdata testchannel".format(
                     path, label, prog=config.invocation()))
    samples = storage.read_samples(path)

    if args.list_days:
        days = chart.days_present(samples)
        if not days:
            sys.exit("No live samples in {}.".format(os.path.basename(path)))
        print("{} day(s) with live data in {}:\n".format(len(days), os.path.basename(path)))
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
        keyword = args.date.strip().lower()
        today = datetime.now().astimezone().date()
        if keyword == "today":
            day = today
        elif keyword == "yesterday":
            day = today - timedelta(days=1)
        else:
            try:
                day = datetime.strptime(args.date.strip(), "%Y-%m-%d").date()
            except ValueError:
                sys.exit("Bad --date '{}'. Use YYYY-MM-DD, 'today' or 'yesterday'.".format(
                    args.date))
        window = chart.select_day(samples, day)
        if not window:
            available = chart.days_present(samples)
            sys.exit("No live samples on {} in {}.{}".format(
                day.isoformat(), os.path.basename(path),
                "\nDays with data: " + ", ".join(d.isoformat() for d in available)
                if available else ""))
        sessions = [window]
    else:
        sessions = chart.split_sessions(samples)

    if not sessions:
        sys.exit("{} has no complete broadcast yet (need 2+ consecutive live samples).\n"
                 "Rows found: {}".format(os.path.basename(path), len(samples)))

    if args.list_sessions:
        print("{} broadcast(s) in {}:\n".format(len(sessions), os.path.basename(path)))
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
            sys.exit("No {} data in {}.".format(args.only, os.path.basename(path)))

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
                                      os.path.basename(path)))
        if down:
            print("  {} offline in {} stretch{} mid-day, kept in the chart\n".format(
                chart.fmt_elapsed(sum(b - a for a, b in down)), len(down),
                "es" if len(down) != 1 else ""))
        else:
            print()
    else:
        print("{}  — broadcast {} of {}  ({})\n".format(
            label, index + 1, len(sessions), os.path.basename(path)))

    chart.print_summary(session, args.bucket, metrics)
    print("\n  chart     {}".format(out_path))

    if args.open_it:
        subprocess.run(["open", out_path], check=False)
    return 0
