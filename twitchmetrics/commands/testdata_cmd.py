"""Write synthetic sample files for working on the charts."""

import os
import random
import sys
from datetime import datetime, timedelta, timezone

from .. import config, storage, testdata


def add_arguments(parser):
    parser.add_argument("channel", nargs="?", default="testchannel",
                        help="channel name; sets the output filenames")
    parser.add_argument("--hours", type=float, default=8.0, help="stream length (default 8)")
    parser.add_argument("--interval", type=int, default=300,
                        help="seconds between samples (default 300)")
    parser.add_argument("--peak", type=int, default=740, help="approximate peak viewers")
    parser.add_argument("--followers", type=int, default=700,
                        help="follower count at the start (default 700)")
    parser.add_argument("--seed", type=int, default=7, help="RNG seed for reproducibility")
    parser.add_argument("--break", dest="breaks", action="append", default=None, metavar="H:M",
                        help="insert an offline gap M minutes long, H hours into the "
                             "stream (repeatable, e.g. --break 3:35)")
    parser.add_argument("--single", action="store_true",
                        help="only the main session (default also writes a short earlier one)")
    parser.add_argument("--out-dir", default=None,
                        help="where to write (default: the data directory)")


def run(args):
    config.ensure_dirs()
    rng = random.Random(args.seed)
    out_dir = args.out_dir or config.DATA_DIR
    os.makedirs(out_dir, exist_ok=True)
    slug = config.channel_slug(args.channel)
    viewers_path = os.path.join(out_dir, "viewers_{}.csv".format(slug))
    metrics_path = os.path.join(out_dir, "metrics_{}.csv".format(slug))

    # Anchored to a fixed date so regenerating gives byte-identical output.
    clock = datetime(2026, 8, 18, 9, 5, 0, tzinfo=timezone.utc)
    rows = []
    followers = args.followers

    if not args.single:
        # A short earlier broadcast, so session-picking has to choose.
        early, clock, followers = testdata.session_rows(
            clock, 45, args.interval, 180, "319000000001", rng, followers)
        rows += early
        gap, followers = testdata.offline_rows(clock, 6, args.interval, followers, rng)
        rows += gap
        clock += timedelta(hours=19)

    breaks = []
    for spec in (args.breaks or []):
        try:
            at_hours, minutes = spec.split(":")
            breaks.append((float(at_hours) * 60, float(minutes)))
        except ValueError:
            sys.exit("Bad --break '{}'. Use HOURS:MINUTES, e.g. 3:35.".format(spec))
    breaks.sort()

    segments, previous = [], 0.0
    for at_minute, _ in breaks:
        segments.append(at_minute - previous)
        previous = at_minute
    segments.append(args.hours * 60 - previous)

    for index, length in enumerate(segments):
        if length <= 0:
            continue
        # Twitch issues a new stream id each time a broadcast restarts.
        stream_id = "3199974091{:02d}".format(16 + index)
        segment, clock, followers = testdata.session_rows(
            clock, length, args.interval, args.peak, stream_id, rng, followers)
        rows += segment
        if index < len(breaks):
            gap_samples = max(1, int(breaks[index][1] * 60 // args.interval))
            gap, followers = testdata.offline_rows(clock, gap_samples, args.interval,
                                                   followers, rng)
            rows += gap
            clock += timedelta(seconds=gap_samples * args.interval)

    tail, followers = testdata.offline_rows(clock, 3, args.interval, followers, rng)
    rows += tail

    storage.write_all(viewers_path, storage.VIEWERS_HEADER, [
        [r["when"], "true" if r["live"] else "false", r["viewers"], r["title"],
         r["game"], r["started_at"], r["stream_id"]] for r in rows])
    storage.write_all(metrics_path, storage.METRICS_HEADER, [
        [r["when"], "true" if r["live"] else "false", r["viewers"], r["followers"],
         r["chatters"], r["title"], r["game"], r["started_at"], r["stream_id"]]
        for r in rows])

    live = [r for r in rows if r["live"]]
    viewers = [r["viewers"] for r in live]
    chat = [r["chatters"] for r in live]

    print("Wrote {} and {}".format(os.path.basename(viewers_path),
                                   os.path.basename(metrics_path)))
    print("  in          {}".format(out_dir))
    print("  rows        {} ({} live, {} offline)".format(
        len(rows), len(live), len(rows) - len(live)))
    print("  main        {:.0f}h at {}s intervals{}".format(
        args.hours, args.interval,
        "  ({} break{})".format(len(breaks), "s" * (len(breaks) != 1)) if breaks else ""))
    print("  viewers     peak {}  avg {}".format(
        max(viewers), round(sum(viewers) / len(viewers))))
    print("  chatters    peak {}  avg {}".format(max(chat), round(sum(chat) / len(chat))))
    print("  followers   {} -> {}  (+{})".format(
        args.followers, rows[-1]["followers"], rows[-1]["followers"] - args.followers))
    print("\nGraph it with:\n    twitch-metrics graph {}".format(args.channel))
    return 0
