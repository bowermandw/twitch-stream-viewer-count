#!/usr/bin/env python3
"""Generate a realistic fake viewers_*.csv so the graph can be designed and
tested without waiting for a real 8-hour stream.

Produces the exact same columns twitch_viewers.py writes, so graph.py cannot
tell the difference.
"""

import argparse
import csv
import math
import os
import random
from datetime import datetime, timedelta, timezone

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


def session_rows(start, minutes, interval, peak_level, stream_id, rng):
    rows = []
    state = {"noise": 0.0}
    started_at = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    steps = int(minutes * 60 // interval) + 1

    # Generate first, then scale so the run actually peaks at peak_level —
    # otherwise the bump and the random spikes compound well past it.
    raw = [viewer_curve(i * interval / 60.0, minutes, peak_level, rng, state)
           for i in range(steps)]
    scale = peak_level / max(raw) if max(raw) else 1.0

    for i in range(steps):
        when = start + timedelta(seconds=i * interval)
        viewers = int(round(raw[i] * scale))
        rows.append([
            when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "true",
            viewers,
            TITLE,
            GAME,
            started_at,
            stream_id,
        ])
    return rows, start + timedelta(seconds=(steps - 1) * interval)


def offline_rows(start, count, interval):
    return [
        [
            (start + timedelta(seconds=i * interval)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "false", "", "", "", "", "",
        ]
        for i in range(1, count + 1)
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel", nargs="?", default="testchannel",
                        help="channel name; sets the output filename")
    parser.add_argument("--hours", type=float, default=8.0, help="stream length (default 8)")
    parser.add_argument("--interval", type=int, default=300,
                        help="seconds between samples (default 300, matching the poller)")
    parser.add_argument("--peak", type=int, default=740, help="approximate peak viewers")
    parser.add_argument("--seed", type=int, default=7, help="RNG seed for reproducibility")
    parser.add_argument("--single", action="store_true",
                        help="only the main session (default also writes a short earlier one)")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    csv_path, _ = tv.paths_for(args.channel)

    # Anchor to a fixed date so regenerating gives byte-identical output.
    clock = datetime(2026, 8, 18, 9, 5, 0, tzinfo=timezone.utc)
    rows = []

    if not args.single:
        # A short earlier broadcast, so graph.py has to pick the right session.
        early, clock = session_rows(clock, 45, args.interval, 180, "319000000001", rng)
        rows += early
        rows += offline_rows(clock, 6, args.interval)
        clock += timedelta(hours=19)

    main_rows, end = session_rows(
        clock, args.hours * 60, args.interval, args.peak, "319997409116", rng
    )
    rows += main_rows
    rows += offline_rows(end, 3, args.interval)

    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(tv.CSV_HEADER)
        writer.writerows(rows)

    live = [r for r in rows if r[1] == "true"]
    counts = [r[2] for r in live]
    print("Wrote {} ({} rows: {} live, {} offline)".format(
        os.path.basename(csv_path), len(rows), len(live), len(rows) - len(live)))
    print("  sessions:   {}".format(1 if args.single else 2))
    print("  main:       {:.0f}h at {}s intervals".format(args.hours, args.interval))
    print("  peak:       {}   average: {}".format(max(counts), round(sum(counts) / len(counts))))
    print("\nGraph it with:\n    python3 graph.py {}".format(args.channel))


if __name__ == "__main__":
    main()
