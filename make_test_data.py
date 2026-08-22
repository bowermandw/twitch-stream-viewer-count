#!/usr/bin/env python3
"""Generate realistic fake data so the graphs can be designed and tested
without waiting for a real 8-hour stream.

Writes two files, matching what the two pollers produce, so graph.py cannot
tell them from real data:

    viewers_<channel>.csv   what twitch_viewers.py records (viewers only)
    metrics_<channel>.csv   what metrics.py records (viewers, followers, chat)

The three metrics are generated as one correlated system rather than
independently: chat size tracks viewers with a bot floor beneath it, and
followers accumulate faster while more people are watching.
"""

import argparse
import csv
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone

import metrics as metrics_mod
import twitch_viewers as tv

# Deliberately synthetic, and comma-laden so the CSV quoting stays exercised.
TITLE = "Sample Stream: Park Day, Rides, Music, and More!"
GAME = "IRL"


def viewer_curve(minutes, total_minutes, peak_level, rng, state, floor_frac=0.06):
    """A plausible stream shape: ramp, mid-stream bump, slow decline, end drop.

    Noise is AR(1) — each sample is correlated with the previous one — because
    independent per-sample noise looks like static rather than an audience.
    """
    t = minutes

    # Audience builds over the first ~30 minutes as people notice the stream,
    # starting from the handful of regulars who are already waiting.
    ramp = floor_frac + (1.0 - floor_frac) * (1.0 - math.exp(-t / 25.0))

    # A broad bump partway through (a raid, a popular segment).
    bump_at = total_minutes * 0.40
    bump = 1.0 + 0.20 * math.exp(-(((t - bump_at) / (total_minutes * 0.23)) ** 2))

    # Long tail-off over the back half.
    decline = 1.0 - 0.32 * (t / total_minutes) ** 1.7

    # People leave quickly once the stream is visibly wrapping up.
    tail = 1.0
    remaining = total_minutes - t
    if remaining < 12:
        tail = max(0.30, remaining / 12.0)

    base = peak_level * ramp * bump * decline * tail

    # AR(1) jitter
    state["noise"] = 0.82 * state["noise"] + rng.gauss(0, 0.038)
    value = base * (1.0 + state["noise"])

    # Occasional short-lived spikes (clip, raid, shoutout).
    if rng.random() < 0.03:
        value *= rng.uniform(1.06, 1.14)

    return max(0, int(round(value)))


BOT_FLOOR = 2          # bots that sit in chat whether or not anyone is watching
CHAT_RATE = 0.055      # roughly what fraction of viewers actually join chat
FOLLOW_RATE = 0.00018  # new followers per viewer per minute


def chat_size(viewers, rng):
    """Chat tracks viewers, but loosely, and never drops below the bots."""
    if viewers <= 0:
        return BOT_FLOOR
    engaged = viewers * CHAT_RATE * rng.uniform(0.72, 1.30)
    return max(BOT_FLOOR, int(round(BOT_FLOOR + engaged)))


def follower_growth(viewers, minutes_elapsed, interval, rng):
    """Followers gained in one interval — more arrive while more are watching."""
    expected = viewers * FOLLOW_RATE * (interval / 60.0)
    gained = int(expected)
    if rng.random() < (expected - gained):
        gained += 1
    # The occasional unfollow, so the line isn't suspiciously monotonic.
    if rng.random() < 0.04:
        gained -= 1
    return gained


def session_rows(start, minutes, interval, peak_level, stream_id, rng,
                 followers_start=700):
    rows = []
    state = {"noise": 0.0}
    started_at = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    steps = int(minutes * 60 // interval) + 1

    # Generate first, then scale so the run actually peaks at peak_level —
    # otherwise the bump and the random spikes compound well past it.
    raw = [viewer_curve(i * interval / 60.0, minutes, peak_level, rng, state)
           for i in range(steps)]
    scale = peak_level / max(raw) if max(raw) else 1.0

    followers = followers_start
    for i in range(steps):
        when = start + timedelta(seconds=i * interval)
        viewers = int(round(raw[i] * scale))
        followers += follower_growth(viewers, i * interval / 60.0, interval, rng)
        rows.append({
            "when": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "live": True,
            "viewers": viewers,
            "followers": followers,
            "chatters": chat_size(viewers, rng),
            "title": TITLE,
            "game": GAME,
            "started_at": started_at,
            "stream_id": stream_id,
        })
    return rows, start + timedelta(seconds=(steps - 1) * interval), followers


def offline_rows(start, count, interval, followers, rng):
    """Offline samples still carry followers and the bots idling in chat."""
    out = []
    for i in range(1, count + 1):
        followers += 1 if rng.random() < 0.25 else 0
        out.append({
            "when": (start + timedelta(seconds=i * interval)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "live": False,
            "viewers": "",
            "followers": followers,
            "chatters": BOT_FLOOR,
            "title": "", "game": "", "started_at": "", "stream_id": "",
        })
    return out, followers


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("channel", nargs="?", default="testchannel",
                        help="channel name; sets the output filenames")
    parser.add_argument("--hours", type=float, default=8.0, help="stream length (default 8)")
    parser.add_argument("--interval", type=int, default=300,
                        help="seconds between samples (default 300, matching the pollers)")
    parser.add_argument("--peak", type=int, default=740, help="approximate peak viewers")
    parser.add_argument("--followers", type=int, default=700,
                        help="follower count at the start (default 700)")
    parser.add_argument("--seed", type=int, default=7, help="RNG seed for reproducibility")
    parser.add_argument("--break", dest="breaks", action="append", default=None,
                        metavar="H:M",
                        help="insert an offline gap M minutes long, H hours into the "
                             "stream (repeatable, e.g. --break 3:35)")
    parser.add_argument("--single", action="store_true",
                        help="only the main session (default also writes a short earlier one)")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    viewers_path, _ = tv.paths_for(args.channel)
    metrics_path, _ = metrics_mod.paths_for(args.channel)

    # Anchor to a fixed date so regenerating gives byte-identical output.
    clock = datetime(2026, 8, 18, 9, 5, 0, tzinfo=timezone.utc)
    rows = []
    followers = args.followers

    if not args.single:
        # A short earlier broadcast, so the graph has to pick the right session.
        early, clock, followers = session_rows(
            clock, 45, args.interval, 180, "319000000001", rng, followers)
        rows += early
        gap, followers = offline_rows(clock, 6, args.interval, followers, rng)
        rows += gap
        clock += timedelta(hours=19)

    # A day's streaming may be one sitting or several with breaks between.
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
        segment, clock, followers = session_rows(
            clock, length, args.interval, args.peak, stream_id, rng, followers)
        rows += segment
        if index < len(breaks):
            gap_minutes = breaks[index][1]
            gap_samples = max(1, int(gap_minutes * 60 // args.interval))
            gap, followers = offline_rows(clock, gap_samples, args.interval, followers, rng)
            rows += gap
            clock += timedelta(seconds=gap_samples * args.interval)

    end = clock
    tail, followers = offline_rows(end, 3, args.interval, followers, rng)
    rows += tail

    write_csv(viewers_path, tv.CSV_HEADER, [
        [r["when"], "true" if r["live"] else "false", r["viewers"], r["title"],
         r["game"], r["started_at"], r["stream_id"]] for r in rows])

    write_csv(metrics_path, metrics_mod.CSV_HEADER, [
        [r["when"], "true" if r["live"] else "false", r["viewers"], r["followers"],
         r["chatters"], r["title"], r["game"], r["started_at"], r["stream_id"]]
        for r in rows])

    live = [r for r in rows if r["live"]]
    viewers = [r["viewers"] for r in live]
    chat = [r["chatters"] for r in live]
    gained = rows[-1]["followers"] - args.followers

    print("Wrote {} and {}".format(os.path.basename(viewers_path),
                                   os.path.basename(metrics_path)))
    print("  rows        {} ({} live, {} offline)".format(
        len(rows), len(live), len(rows) - len(live)))
    print("  sessions    {}".format(1 if args.single else 2))
    print("  main        {:.0f}h at {}s intervals{}".format(
        args.hours, args.interval,
        "  ({} break{})".format(len(breaks), "s" * (len(breaks) != 1)) if breaks else ""))
    print("  viewers     peak {}  avg {}".format(max(viewers), round(sum(viewers) / len(viewers))))
    print("  chatters    peak {}  avg {}".format(max(chat), round(sum(chat) / len(chat))))
    print("  followers   {} -> {}  (+{})".format(
        args.followers, rows[-1]["followers"], gained))
    print("\nGraph it with:\n    python3 graph.py {}".format(args.channel))


if __name__ == "__main__":
    main()
