"""Synthetic sample data, so the charts can be worked on without waiting for a
real 8-hour stream.

The three metrics are generated as one correlated system rather than
independently: chat size tracks viewers above a bot floor, and followers accrue
faster while more people are watching.
"""

import math
import random
from datetime import datetime, timedelta, timezone

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


