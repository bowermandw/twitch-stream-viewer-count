#!/usr/bin/env python3
"""Chart the viewer counts collected by twitch_viewers.py.

Renders an SVG in the style of the YouTube Studio "Concurrent viewers" chart:
peak and average in the header, a filled area curve, and — added here — the
time the peak happened plus a light per-block average line.

Pure standard library, so it runs on any Python 3 with nothing installed.
"""

import argparse
import csv
import html
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import twitch_viewers as tv

# --- palette (YouTube Studio dark) ----------------------------------------
BG = "#0f0f0f"
FG = "#f1f1f1"
MUTED = "#aaaaaa"
DIM = "#717171"
LINE = "#4fb3e8"
GRID = "#303030"
DIVIDER = "#303030"
BUCKET_LINE = "#ffd166"

# Each metric gets its own colour and axis treatment. Followers are not
# zero-based: on a 0..750 axis, a 40-follower gain is an invisible flat line.
METRICS = [
    {"key": "viewers",   "label": "Concurrent viewers", "color": "#4fb3e8", "zero_based": True},
    {"key": "chatters",  "label": "In chat",            "color": "#4ade80", "zero_based": True},
    {"key": "followers", "label": "Followers",          "color": "#ffd166", "zero_based": False},
]
METRIC_BY_KEY = {m["key"]: m for m in METRICS}

PANEL_LINE = "#ffffff"  # bucket averages inside a panel, over any series colour

# --- layout ---------------------------------------------------------------
PAD_L, PAD_R = 34, 104
HEADER_H = 150
FOOT_H = 48
PANEL_H = 210      # height of one metric panel in stacked mode
PANEL_GAP = 46

# A live sample this far after the previous one means the poller was stopped,
# so the samples either side belong to different sittings.
GAP_TOLERANCE = 2.5


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------


def read_samples(path):
    """Parse a viewers_*.csv into dicts, skipping rows that can't be read."""
    if not os.path.exists(path):
        sys.exit(
            "No data file at {}\n"
            "Collect some first:  python3 twitch_viewers.py <channel>\n"
            "Or make fake data:   python3 make_test_data.py testchannel".format(path)
        )

    samples = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                when = datetime.strptime(
                    row["timestamp_utc"], "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=timezone.utc)
            except (ValueError, KeyError, TypeError):
                continue
            live = (row.get("is_live") or "").strip().lower() == "true"
            try:
                viewers = int(row["viewer_count"]) if live else None
            except (ValueError, KeyError, TypeError):
                continue

            def optional(name):
                """metrics_*.csv has these columns; viewers_*.csv does not."""
                raw = (row.get(name) or "").strip()
                try:
                    return int(raw) if raw else None
                except ValueError:
                    return None

            samples.append({
                "when": when,
                "live": live,
                "viewers": viewers,
                "followers": optional("follower_count"),
                "chatters": optional("chatter_count"),
                "title": row.get("title") or "",
                "game": row.get("game") or "",
                "stream_id": row.get("stream_id") or "",
            })
    samples.sort(key=lambda s: s["when"])
    return samples


def split_sessions(samples):
    """Group consecutive live samples into broadcasts.

    A session breaks on an offline row, a change of stream_id, or a gap that
    means the poller wasn't running.
    """
    step = median_step(samples)
    sessions, current = [], []

    for sample in samples:
        if not sample["live"]:
            if current:
                sessions.append(current)
                current = []
            continue
        if current:
            changed_stream = sample["stream_id"] != current[-1]["stream_id"]
            gap = (sample["when"] - current[-1]["when"]).total_seconds()
            if changed_stream or gap > step * GAP_TOLERANCE:
                sessions.append(current)
                current = []
        current.append(sample)

    if current:
        sessions.append(current)
    return [s for s in sessions if len(s) >= 2]


def median_step(samples):
    """Typical seconds between samples, used to detect gaps."""
    deltas = sorted(
        (b["when"] - a["when"]).total_seconds()
        for a, b in zip(samples, samples[1:])
        if (b["when"] - a["when"]).total_seconds() > 0
    )
    return deltas[len(deltas) // 2] if deltas else 300.0


def bucket_averages(session, bucket_minutes, key="viewers"):
    """Average of one metric per fixed-width block of stream time."""
    start = session[0]["when"]
    width = bucket_minutes * 60
    buckets = {}
    for sample in session:
        if sample.get(key) is None:
            continue
        idx = int((sample["when"] - start).total_seconds() // width)
        buckets.setdefault(idx, []).append(sample[key])
    if not buckets:
        return []

    out = []
    for idx in sorted(buckets):
        values = buckets[idx]
        out.append({
            "start": idx * width,
            "end": (idx + 1) * width,
            "avg": sum(values) / len(values),
            "n": len(values),
        })
    return out


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------


def fmt_elapsed(seconds):
    seconds = int(round(seconds))
    return "{}:{:02d}:{:02d}".format(seconds // 3600, seconds % 3600 // 60, seconds % 60)


def fmt_clock(when):
    return when.astimezone().strftime("%-I:%M %p")


def fmt_count(value):
    return "{:,}".format(int(round(value)))


def axis_bounds(values, zero_based):
    """(low, high, step) for an axis, padded so the curve never grazes an edge."""
    low_value, high_value = min(values), max(values)
    if zero_based:
        top, step = nice_axis(high_value)
        return 0, top, step
    if high_value == low_value:  # a flat line still needs a visible band
        pad = max(1, abs(high_value) * 0.02)
        return low_value - pad, high_value + pad, max(1, round(pad))
    span = high_value - low_value
    step = max(1, round(span / 3.0))
    magnitude = 10 ** math.floor(math.log10(step)) if step > 0 else 1
    for mult in (1, 2, 2.5, 5, 10):
        if step <= mult * magnitude:
            step = mult * magnitude
            break
    low = math.floor((low_value - span * 0.12) / step) * step
    high = math.ceil((high_value + span * 0.12) / step) * step
    return low, high, step


def nice_axis(peak):
    """Pick a round axis maximum and gridline step, like 0/250/500/750."""
    if peak <= 0:
        return 10, 5
    raw = peak / 4.0
    magnitude = 10 ** math.floor(math.log10(raw))
    for mult in (1, 2, 2.5, 5, 10):
        step = mult * magnitude
        if raw <= step:
            break
    top = math.ceil(peak / step) * step
    if top - peak < step * 0.12:  # don't let the curve graze the ceiling
        top += step
    return top, step


# --------------------------------------------------------------------------
# svg
# --------------------------------------------------------------------------


def esc(text):
    return html.escape(str(text), quote=True)


def text(x, y, content, size=14, fill=MUTED, weight="normal", anchor="start", opacity=None):
    extra = ' opacity="{}"'.format(opacity) if opacity is not None else ""
    return (
        '<text x="{:.1f}" y="{:.1f}" font-size="{}" fill="{}" font-weight="{}" '
        'text-anchor="{}"{}>{}</text>'.format(
            x, y, size, fill, weight, anchor, extra, esc(content)
        )
    )


def render(session, channel, bucket_minutes, width, height, show_buckets=True):
    start = session[0]["when"]
    duration = (session[-1]["when"] - start).total_seconds()
    counts = [s["viewers"] for s in session]

    peak = max(counts)
    peak_sample = session[counts.index(peak)]
    peak_offset = (peak_sample["when"] - start).total_seconds()
    average = sum(counts) / len(counts)

    top, step = nice_axis(peak)
    plot_l, plot_r = PAD_L, width - PAD_R
    plot_t, plot_b = HEADER_H, height - FOOT_H

    def sx(seconds):
        if duration <= 0:
            return plot_l
        return plot_l + (seconds / duration) * (plot_r - plot_l)

    def sy(value):
        return plot_b - (value / top) * (plot_b - plot_t)

    out = []
    add = out.append

    add('<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}" '
        'viewBox="0 0 {} {}" font-family="Roboto, -apple-system, BlinkMacSystemFont, '
        '&quot;Helvetica Neue&quot;, Arial, sans-serif">'.format(width, height, width, height))
    add('<defs><linearGradient id="fade" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0%" stop-color="{c}" stop-opacity="0.34"/>'
        '<stop offset="55%" stop-color="{c}" stop-opacity="0.13"/>'
        '<stop offset="100%" stop-color="{c}" stop-opacity="0.02"/>'
        '</linearGradient></defs>'.format(c=LINE))
    add('<rect width="{}" height="{}" fill="{}"/>'.format(width, height, BG))

    # ---- header ----------------------------------------------------------
    add(text(PAD_L, 46, "Concurrent viewers", size=21, fill=FG, weight="700"))
    add(text(PAD_L, 74, "While live · {}".format(channel), size=14, fill=MUTED))

    avg_x = width - PAD_R + 84
    peak_x = avg_x - 176
    add('<line x1="{:.1f}" y1="22" x2="{:.1f}" y2="92" stroke="{}" stroke-width="1"/>'.format(
        peak_x + 26, peak_x + 26, DIVIDER))

    add(text(peak_x, 52, fmt_count(peak), size=30, fill=FG, weight="700", anchor="end"))
    add(text(peak_x, 74, "Peak", size=13, fill=MUTED, anchor="end"))
    # The reference chart stops at the number; the time is the useful addition.
    add(text(peak_x, 92, "at {}  ·  {}".format(
        fmt_elapsed(peak_offset), fmt_clock(peak_sample["when"])),
        size=12, fill=DIM, anchor="end"))

    add(text(avg_x, 52, fmt_count(average), size=30, fill=FG, weight="700", anchor="end"))
    add(text(avg_x, 74, "Average", size=13, fill=MUTED, anchor="end"))
    add(text(avg_x, 92, "over {}".format(fmt_elapsed(duration)), size=12, fill=DIM, anchor="end"))

    # ---- gridlines -------------------------------------------------------
    value = 0.0
    while value <= top + 1e-9:
        y = sy(value)
        add('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
            'stroke-width="1"/>'.format(plot_l, y, plot_r, y, GRID))
        add(text(plot_r + 18, y + 5, fmt_count(value), size=13, fill=MUTED))
        value += step

    # ---- faint separator at each block boundary --------------------------
    if show_buckets:
        edge = bucket_minutes * 60
        while edge < duration:
            x = sx(edge)
            add('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="#ffffff" '
                'stroke-width="1" opacity="0.05"/>'.format(x, plot_t, x, plot_b))
            edge += bucket_minutes * 60

    # ---- area + line -----------------------------------------------------
    points = [(sx((s["when"] - start).total_seconds()), sy(s["viewers"])) for s in session]
    coords = " ".join("{:.1f},{:.1f}".format(x, y) for x, y in points)

    add('<path d="M {:.1f},{:.1f} L {} L {:.1f},{:.1f} Z" fill="url(#fade)"/>'.format(
        points[0][0], plot_b, coords.replace(" ", " L "), points[-1][0], plot_b))
    add('<polyline points="{}" fill="none" stroke="{}" stroke-width="2" '
        'stroke-linejoin="round" stroke-linecap="round"/>'.format(coords, LINE))

    # ---- per-block average lines ----------------------------------------
    if show_buckets:
        for bucket in bucket_averages(session, bucket_minutes):
            x1, x2 = sx(bucket["start"]), sx(min(bucket["end"], duration))
            if x2 - x1 < 2:
                continue
            y = sy(bucket["avg"])
            add('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
                'stroke-width="1.6" opacity="0.75" stroke-linecap="round"/>'.format(
                    x1, y, x2, y, BUCKET_LINE))

    # ---- peak marker -----------------------------------------------------
    px, py = sx(peak_offset), sy(peak)
    add('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
        'stroke-width="1" opacity="0.35" stroke-dasharray="3 3"/>'.format(px, py, px, plot_b, FG))
    add('<circle cx="{:.1f}" cy="{:.1f}" r="4" fill="{}" stroke="{}" '
        'stroke-width="2"/>'.format(px, py, BG, LINE))
    label_anchor = "end" if px > (plot_l + plot_r) / 2 else "start"
    label_dx = -9 if label_anchor == "end" else 9
    add(text(px + label_dx, py - 9, "peak {}".format(fmt_count(peak)),
             size=12, fill=FG, weight="700", anchor=label_anchor))

    # ---- x axis ----------------------------------------------------------
    add('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
        'stroke-width="1"/>'.format(plot_l, plot_b, plot_r, plot_b, GRID))
    add(text(plot_l, plot_b + 24, "0:00", size=13, fill=MUTED))
    add(text(plot_r, plot_b + 24, fmt_elapsed(duration), size=13, fill=MUTED, anchor="end"))
    add(text((plot_l + plot_r) / 2, plot_b + 24,
             "{} → {}".format(fmt_clock(start), fmt_clock(session[-1]["when"])),
             size=13, fill=DIM, anchor="middle"))

    if show_buckets:
        add('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
            'stroke-width="1.6" opacity="0.75"/>'.format(
                plot_l, height - 14, plot_l + 18, height - 14, BUCKET_LINE))
        add(text(plot_l + 25, height - 10,
                 "{}-minute average".format(bucket_minutes), size=12, fill=DIM))

    add(text(plot_r, height - 10, start.astimezone().strftime("%a %-d %b %Y"),
             size=12, fill=DIM, anchor="end"))
    add("</svg>")
    return "\n".join(out)



# --------------------------------------------------------------------------
# multi-metric rendering
# --------------------------------------------------------------------------


def available_metrics(session):
    """Which metrics actually have data in this session, in display order."""
    out = []
    for metric in METRICS:
        if any(s.get(metric["key"]) is not None for s in session):
            out.append(metric)
    return out


def series_of(session, key):
    """(elapsed_seconds, value) pairs for one metric, skipping missing samples."""
    start = session[0]["when"]
    return [((s["when"] - start).total_seconds(), s[key])
            for s in session if s.get(key) is not None]


def summarise(session, key):
    points = series_of(session, key)
    if not points:
        return None
    values = [v for _, v in points]
    peak = max(values)
    peak_at = next(t for t, v in points if v == peak)
    return {
        "peak": peak, "peak_at": peak_at, "low": min(values),
        "avg": sum(values) / len(values),
        "first": values[0], "last": values[-1], "n": len(values),
    }


def draw_panel(out, metric, session, geom, bucket_minutes, show_buckets, duration):
    """Render one metric into a panel. geom is (left, right, top, bottom)."""
    left, right, top, bottom = geom
    points = series_of(session, metric["key"])
    if not points:
        return
    low, high, step = axis_bounds([v for _, v in points], metric["zero_based"])
    span = (high - low) or 1

    def sx(seconds):
        return left + (seconds / duration) * (right - left) if duration else left

    def sy(value):
        return bottom - ((value - low) / span) * (bottom - top)

    gradient_id = "fade_{}".format(metric["key"])
    out.append('<defs><linearGradient id="{}" x1="0" y1="0" x2="0" y2="1">'
               '<stop offset="0%" stop-color="{c}" stop-opacity="0.32"/>'
               '<stop offset="100%" stop-color="{c}" stop-opacity="0.02"/>'
               '</linearGradient></defs>'.format(gradient_id, c=metric["color"]))

    value = low
    while value <= high + 1e-9:
        y = sy(value)
        out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
                   'stroke-width="1"/>'.format(left, y, right, y, GRID))
        out.append(text(right + 14, y + 4, fmt_count(value), size=12, fill=MUTED))
        value += step

    if show_buckets:
        edge = bucket_minutes * 60
        while edge < duration:
            x = sx(edge)
            out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" '
                       'stroke="#ffffff" stroke-width="1" opacity="0.04"/>'.format(
                           x, top, x, bottom))
            edge += bucket_minutes * 60

    coords = " ".join("{:.1f},{:.1f}".format(sx(t), sy(v)) for t, v in points)
    out.append('<path d="M {:.1f},{:.1f} L {} L {:.1f},{:.1f} Z" fill="url(#{})"/>'.format(
        sx(points[0][0]), bottom, coords.replace(" ", " L "),
        sx(points[-1][0]), bottom, gradient_id))
    out.append('<polyline points="{}" fill="none" stroke="{}" stroke-width="2" '
               'stroke-linejoin="round" stroke-linecap="round"/>'.format(
                   coords, metric["color"]))

    if show_buckets:
        for bucket in bucket_averages(session, bucket_minutes, metric["key"]):
            x1, x2 = sx(bucket["start"]), sx(min(bucket["end"], duration))
            if x2 - x1 >= 2:
                out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" '
                           'stroke="{}" stroke-width="1.5" opacity="0.5" '
                           'stroke-linecap="round"/>'.format(
                               x1, sy(bucket["avg"]), x2, sy(bucket["avg"]), PANEL_LINE))

    stats = summarise(session, metric["key"])
    px, py = sx(stats["peak_at"]), sy(stats["peak"])
    out.append('<circle cx="{:.1f}" cy="{:.1f}" r="3.5" fill="{}" stroke="{}" '
               'stroke-width="2"/>'.format(px, py, BG, metric["color"]))

    # Panel caption: name on the left, the numbers that matter on the right.
    out.append(text(left, top - 12, metric["label"], size=14, fill=FG, weight="700"))
    if metric["key"] == "followers":
        gained = stats["last"] - stats["first"]
        caption = "{:+,} over the stream   ·   {} → {}".format(
            gained, fmt_count(stats["first"]), fmt_count(stats["last"]))
    else:
        caption = "peak {} at {}   ·   avg {}".format(
            fmt_count(stats["peak"]), fmt_elapsed(stats["peak_at"]), fmt_count(stats["avg"]))
    out.append(text(right, top - 12, caption, size=12, fill=MUTED, anchor="end"))
    out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
               'stroke-width="1"/>'.format(left, bottom, right, bottom, GRID))


def render_stacked(session, channel, bucket_minutes, width, show_buckets=True,
                   metrics=None):
    """One panel per metric, sharing an x-axis."""
    metrics = metrics or available_metrics(session)
    start = session[0]["when"]
    duration = (session[-1]["when"] - start).total_seconds()
    height = HEADER_H + len(metrics) * (PANEL_H + PANEL_GAP) + FOOT_H
    left, right = PAD_L, width - PAD_R

    out = []
    out.append('<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}" '
               'viewBox="0 0 {} {}" font-family="Roboto, -apple-system, '
               'BlinkMacSystemFont, &quot;Helvetica Neue&quot;, Arial, sans-serif">'.format(
                   width, height, width, height))
    out.append('<rect width="{}" height="{}" fill="{}"/>'.format(width, height, BG))

    out.append(text(PAD_L, 46, "Stream metrics", size=21, fill=FG, weight="700"))
    out.append(text(PAD_L, 74, "While live · {}".format(channel), size=14, fill=MUTED))

    # Header tiles, one per metric, right-aligned like the reference chart.
    tile_x = width - PAD_R + 84
    for metric in reversed(metrics):
        stats = summarise(session, metric["key"])
        if metric["key"] == "followers":
            big, label = "{:+,}".format(stats["last"] - stats["first"]), "Followers gained"
        else:
            big, label = fmt_count(stats["peak"]), "Peak {}".format(metric["label"].lower())
        out.append(text(tile_x, 52, big, size=27, fill=metric["color"],
                        weight="700", anchor="end"))
        out.append(text(tile_x, 74, label, size=12, fill=MUTED, anchor="end"))
        tile_x -= 190

    for index, metric in enumerate(metrics):
        top = HEADER_H + index * (PANEL_H + PANEL_GAP)
        draw_panel(out, metric, session, (left, right, top, top + PANEL_H),
                   bucket_minutes, show_buckets, duration)

    baseline = HEADER_H + len(metrics) * (PANEL_H + PANEL_GAP) - PANEL_GAP
    out.append(text(left, baseline + 24, "0:00", size=13, fill=MUTED))
    out.append(text(right, baseline + 24, fmt_elapsed(duration), size=13,
                    fill=MUTED, anchor="end"))
    out.append(text((left + right) / 2, baseline + 24, "{} → {}".format(
        fmt_clock(start), fmt_clock(session[-1]["when"])), size=13, fill=DIM, anchor="middle"))
    if show_buckets:
        out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
                   'stroke-width="1.5" opacity="0.5"/>'.format(
                       left, height - 14, left + 18, height - 14, PANEL_LINE))
        out.append(text(left + 25, height - 10,
                        "{}-minute average".format(bucket_minutes), size=12, fill=DIM))
    out.append(text(right, height - 10, start.astimezone().strftime("%a %-d %b %Y"),
                    size=12, fill=DIM, anchor="end"))
    out.append("</svg>")
    return "\n".join(out)


def render_composite(session, channel, width, height, metrics=None):
    """All metrics on one plot, each scaled to its own range.

    The three live on wildly different scales (hundreds of viewers, dozens of
    chatters, hundreds of followers barely moving), so a shared axis would
    flatten two of them. Each is normalised and its real range is stated in the
    legend instead.
    """
    metrics = metrics or available_metrics(session)
    start = session[0]["when"]
    duration = (session[-1]["when"] - start).total_seconds()
    left, right = PAD_L, width - PAD_R
    top, bottom = HEADER_H, height - FOOT_H - 26

    out = []
    out.append('<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}" '
               'viewBox="0 0 {} {}" font-family="Roboto, -apple-system, '
               'BlinkMacSystemFont, &quot;Helvetica Neue&quot;, Arial, sans-serif">'.format(
                   width, height, width, height))
    out.append('<rect width="{}" height="{}" fill="{}"/>'.format(width, height, BG))
    out.append(text(PAD_L, 46, "Stream metrics", size=21, fill=FG, weight="700"))
    out.append(text(PAD_L, 74, "While live · {}  ·  each series scaled to its own range".format(
        channel), size=14, fill=MUTED))

    for fraction in (0, 0.25, 0.5, 0.75, 1.0):
        y = bottom - fraction * (bottom - top)
        out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
                   'stroke-width="1"/>'.format(left, y, right, y, GRID))

    tile_x = width - PAD_R + 84
    for metric in reversed(metrics):
        stats = summarise(session, metric["key"])
        big = ("{:+,}".format(stats["last"] - stats["first"])
               if metric["key"] == "followers" else fmt_count(stats["peak"]))
        out.append(text(tile_x, 52, big, size=27, fill=metric["color"],
                        weight="700", anchor="end"))
        out.append(text(tile_x, 74, "Followers gained" if metric["key"] == "followers"
                        else "Peak {}".format(metric["label"].lower()),
                        size=12, fill=MUTED, anchor="end"))
        tile_x -= 190

    legend_x = left
    for metric in metrics:
        points = series_of(session, metric["key"])
        if not points:
            continue
        values = [v for _, v in points]
        low, high = min(values), max(values)
        span = (high - low) or 1

        coords = " ".join("{:.1f},{:.1f}".format(
            left + (t / duration) * (right - left) if duration else left,
            bottom - ((v - low) / span) * (bottom - top)) for t, v in points)
        out.append('<polyline points="{}" fill="none" stroke="{}" stroke-width="2" '
                   'stroke-linejoin="round" stroke-linecap="round" opacity="0.95"/>'.format(
                       coords, metric["color"]))

        out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
                   'stroke-width="2.5" stroke-linecap="round"/>'.format(
                       legend_x, height - 22, legend_x + 20, height - 22, metric["color"]))
        entry = "{}  {} – {}".format(metric["label"], fmt_count(low), fmt_count(high))
        out.append(text(legend_x + 27, height - 18, entry, size=12, fill=MUTED))
        legend_x += 27 + len(entry) * 6.6 + 34

    out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
               'stroke-width="1"/>'.format(left, bottom, right, bottom, GRID))
    out.append(text(left, bottom + 24, "0:00", size=13, fill=MUTED))
    out.append(text(right, bottom + 24, fmt_elapsed(duration), size=13, fill=MUTED,
                    anchor="end"))
    out.append(text((left + right) / 2, bottom + 24, "{} → {}".format(
        fmt_clock(start), fmt_clock(session[-1]["when"])), size=13, fill=DIM, anchor="middle"))
    out.append("</svg>")
    return "\n".join(out)


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------


def print_summary(session, bucket_minutes, metrics=None):
    start = session[0]["when"]
    duration = (session[-1]["when"] - start).total_seconds()
    metrics = metrics or available_metrics(session)

    print("  started   {}".format(start.astimezone().strftime("%a %-d %b %Y, %-I:%M %p")))
    print("  duration  {}   ({} samples)".format(fmt_elapsed(duration), len(session)))

    for metric in metrics:
        stats = summarise(session, metric["key"])
        if not stats:
            continue
        print("\n  {}".format(metric["label"]))
        if metric["key"] == "followers":
            print("    start   {}".format(fmt_count(stats["first"])))
            print("    end     {}".format(fmt_count(stats["last"])))
            print("    gained  {:+,}".format(stats["last"] - stats["first"]))
        else:
            print("    peak    {}  at {} ({})".format(
                fmt_count(stats["peak"]), fmt_elapsed(stats["peak_at"]),
                fmt_clock(start + timedelta(seconds=stats["peak_at"]))))
            print("    average {}".format(fmt_count(stats["avg"])))
            print("    low     {}".format(fmt_count(stats["low"])))

    primary = metrics[0]["key"] if metrics else "viewers"
    buckets = bucket_averages(session, bucket_minutes, primary)
    if buckets:
        print("\n  {}-minute averages — {}".format(bucket_minutes, METRIC_BY_KEY[primary]["label"]))
        widest = max(b["avg"] for b in buckets) or 1
        for bucket in buckets:
            bar = "█" * max(1, int(round(bucket["avg"] / widest * 34)))
            print("    {:>8}  {:>6}  {}".format(
                fmt_elapsed(bucket["start"]), fmt_count(bucket["avg"]), bar))


# --------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  python3 graph.py IGN\n"
               "  python3 graph.py testchannel --bucket 60\n"
               "  python3 graph.py IGN --session 1 --open\n")
    parser.add_argument("channel", nargs="?", default=None,
                        help="channel name, or a path to a viewers_*.csv")
    parser.add_argument("--bucket", type=int, default=30, metavar="MIN",
                        help="block size for the light average lines (default 30)")
    parser.add_argument("--no-buckets", action="store_true", help="hide the average lines")
    parser.add_argument("--session", type=int, default=None, metavar="N",
                        help="which broadcast to chart (default: the most recent)")
    parser.add_argument("--list-sessions", action="store_true",
                        help="list the broadcasts in the file and exit")
    parser.add_argument("--composite", action="store_true",
                        help="all metrics on one plot instead of separate panels")
    parser.add_argument("--only", default=None, metavar="METRIC",
                        help="chart just one metric: {}".format(
                            ", ".join(m["key"] for m in METRICS)))
    parser.add_argument("--viewers-only", action="store_true",
                        help="read viewers_<channel>.csv even if metrics data exists")
    parser.add_argument("--output", default=None, help="output .svg path")
    parser.add_argument("--width", type=int, default=1300)
    parser.add_argument("--height", type=int, default=470)
    parser.add_argument("--open", dest="open_it", action="store_true",
                        help="open the chart when done")
    args = parser.parse_args()

    channel = args.channel or tv.resolve_channel(None)
    if channel.lower().endswith(".csv"):
        path, label = channel, os.path.basename(channel)
    else:
        label = channel
        import metrics as metrics_mod
        metrics_path, _ = metrics_mod.paths_for(channel)
        viewers_path, _ = tv.paths_for(channel)
        # metrics_*.csv is a superset, so prefer it unless asked otherwise.
        if args.viewers_only or not os.path.exists(metrics_path):
            path = viewers_path
        else:
            path = metrics_path

    samples = read_samples(path)
    sessions = split_sessions(samples)
    if not sessions:
        sys.exit(
            "{} has no complete broadcast yet (need 2+ consecutive live samples).\n"
            "Rows found: {}".format(os.path.basename(path), len(samples))
        )

    if args.list_sessions:
        print("{} broadcast(s) in {}:\n".format(len(sessions), os.path.basename(path)))
        for i, s in enumerate(sessions):
            counts = [x["viewers"] for x in s]
            print("  [{}] {}  {:>9}  peak {:>6}  avg {:>6}  ({} samples)".format(
                i, s[0]["when"].astimezone().strftime("%a %-d %b %-I:%M %p"),
                fmt_elapsed((s[-1]["when"] - s[0]["when"]).total_seconds()),
                fmt_count(max(counts)), fmt_count(sum(counts) / len(counts)), len(counts)))
        print("\nChart one with --session N (default is the most recent).")
        return

    index = args.session if args.session is not None else len(sessions) - 1
    if not 0 <= index < len(sessions):
        sys.exit("No session {} — the file has {} (0-{}). Try --list-sessions.".format(
            index, len(sessions), len(sessions) - 1))
    session = sessions[index]

    metrics = available_metrics(session)
    if args.only:
        if args.only not in METRIC_BY_KEY:
            sys.exit("Unknown metric '{}'. Choose from: {}".format(
                args.only, ", ".join(m["key"] for m in METRICS)))
        metrics = [m for m in metrics if m["key"] == args.only]
        if not metrics:
            sys.exit("No {} data in {}.".format(args.only, os.path.basename(path)))

    multi = len(metrics) > 1
    suffix = ""
    if args.composite and multi:
        svg = render_composite(session, label, args.width, max(args.height, 470),
                               metrics=metrics)
        suffix = "_composite"
    elif multi:
        svg = render_stacked(session, label, args.bucket, args.width,
                             show_buckets=not args.no_buckets, metrics=metrics)
        suffix = "_metrics"
    elif metrics and metrics[0]["key"] != "viewers":
        # A single non-viewer metric still reads best as one panel.
        svg = render_stacked(session, label, args.bucket, args.width,
                             show_buckets=not args.no_buckets, metrics=metrics)
        suffix = "_" + metrics[0]["key"]
    else:
        svg = render(session, label, args.bucket, args.width, args.height,
                     show_buckets=not args.no_buckets)

    out_path = args.output or os.path.join(
        tv.BASE_DIR, "chart_{}{}.svg".format(
            tv.channel_slug(label.replace(".csv", "")), suffix))
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(svg)

    print("{}  — broadcast {} of {}  ({})\n".format(
        label, index + 1, len(sessions), os.path.basename(path)))
    print_summary(session, args.bucket, metrics)
    print("\n  chart     {}".format(out_path))

    if args.open_it:
        subprocess.run(["open", out_path], check=False)


if __name__ == "__main__":
    main()
