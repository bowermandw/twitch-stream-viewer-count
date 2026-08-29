"""Rendering the sample data as SVG.

Styled after the YouTube Studio "Concurrent viewers" chart, extended with the
time each peak happened and per-block average lines. Hand-built SVG rather than
a plotting library, which is why this package needs nothing installed.
"""

import html
import math
import os
from datetime import datetime, timedelta

from . import config

# --- palette (YouTube Studio dark) ----------------------------------------
BG = "#0f0f0f"
FG = "#f1f1f1"
MUTED = "#aaaaaa"
DIM = "#717171"
LINE = "#4fb3e8"
GRID = "#303030"
DIVIDER = "#303030"
BUCKET_LINE = "#ffd166"

# Each metric gets its own colour and axis treatment. Followers and subscribers
# are not zero-based: on a 0..750 axis, a 40-follower gain is an invisible flat
# line, and YouTube's three-significant-figure subscriber count is worse still.
# available_metrics() drops the ones a file has no data for, so a Twitch CSV
# charts followers and a YouTube one charts subscribers without being asked.
METRICS = [
    {"key": "viewers",     "label": "Concurrent viewers", "color": "#4fb3e8", "zero_based": True},
    {"key": "chatters",    "label": "In chat",            "color": "#4ade80", "zero_based": True},
    {"key": "followers",   "label": "Followers",          "color": "#ffd166", "zero_based": False},
    {"key": "subscribers", "label": "Subscribers",        "color": "#ff6b6b", "zero_based": False},
    {"key": "likes",       "label": "Likes",              "color": "#c084fc", "zero_based": False},
]
METRIC_BY_KEY = {m["key"]: m for m in METRICS}

# Watch time is nobody's sampled series, so it takes no colour from METRICS.
# Teal keeps it distinct from all five of them at a glance, which is the point:
# every other figure on these charts was read off an API, and this one was
# worked out.
WATCH_COLOR = "#2dd4bf"

PANEL_LINE = "#ffffff"  # bucket averages inside a panel, over any series colour

# The cross-platform chart. Each platform keeps its own brand colour, softened
# for a dark ground, because that is the one association a viewer already has.
PLATFORMS = [
    {"key": "twitch",  "label": "Twitch",  "color": "#a970ff"},
    {"key": "youtube", "label": "YouTube", "color": "#ff5c5c"},
]
COMBINED_LINE = "#f1f1f1"

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
# watch time
# --------------------------------------------------------------------------


def watch_time(points):
    """(estimated_viewer_minutes, covered_seconds) under a viewer curve.

    `points` is [(when, viewers)] in any order; anything with no number is
    dropped. The area under the curve IS watch time, and a trapezoid between
    each consecutive pair is the whole of the arithmetic:

        (viewers_before + viewers_after) / 2 * gap_seconds / 60

    A trapezoid rather than "viewers x the poll interval", because the interval
    is not a guarantee -- a restart, a slow API call and a spooled backfill all
    make gaps of their own, and the trapezoid is right for any of them.

    A gap wider than GAP_TOLERANCE times the median one is NOT integrated.
    Crediting three hours of viewers to a poller that was down for three hours
    would be inventing an audience, and this number is quoted. That is what the
    second return value is for: covered_seconds says how much of the stream the
    estimate actually rests on, so a small total from an outage stays
    distinguishable from a small total from a quiet night.

    Returns (None, 0) when nothing could be integrated -- a single sample, or a
    session whose every gap was too wide. None and never 0, for the reason a day
    off charts as a dash: "we do not know" is not "nobody watched".

    tm.stream_watch_slices() implements the identical rule in SQL, down to the
    upper-median tie-break, and tests/smoke.py holds the two together.
    """
    ordered = sorted((when, value) for when, value in points if value is not None)
    gaps = []
    for (before, first), (after, second) in zip(ordered, ordered[1:]):
        seconds = (after - before).total_seconds()
        if seconds > 0:
            gaps.append((seconds, first, second))
    if not gaps:
        return None, 0

    # median_step()'s rule: sorted, then indexed at len // 2, which is the UPPER
    # median on an even count. Not a mean, so one three-hour outage cannot widen
    # the cap enough to admit itself.
    widths = sorted(gap for gap, _, _ in gaps)
    cap = widths[len(widths) // 2] * GAP_TOLERANCE

    minutes, covered = 0.0, 0.0
    for seconds, first, second in gaps:
        if seconds <= cap:
            minutes += (first + second) / 2.0 * seconds / 60.0
            covered += seconds
    if not covered:
        return None, 0
    return minutes, covered


def session_watch_time(session, key="viewers"):
    """watch_time() over a session of sample dicts, which is what chart.py holds."""
    return watch_time([(s["when"], s.get(key)) for s in session])


DASH_HOURS = "\u2014"   # what fmt_hours() prints when nothing could be integrated


def fmt_hours(minutes):
    """Watch minutes as hours: '4.2 h' while it is small, '1,204 h' once it is not.

    One decimal below a hundred because a broadcast is single or double figures
    of watch hours and the decimal is most of the signal; none above it, because
    at four figures the tenth is noise and the comma is what a reader needs.
    """
    if minutes is None:
        return DASH_HOURS
    hours = minutes / 60.0
    return "{:.1f} h".format(hours) if hours < 100 else "{:,.0f} h".format(hours)


def live_seconds(window):
    """Seconds the channel was actually live, offline stretches taken out.

    The denominator coverage needs, and NOT the window's wall-clock duration.
    A `session` here can be a whole day, and select_day() deliberately keeps the
    offline rows between two broadcasts so the day charts as one continuous
    axis. On a day with a morning stream and an evening one, the wall clock
    therefore includes the afternoon he spent not streaming -- and measuring
    integrated time against that reports a poller failure where there was a
    lunch break.

    Measured across each contiguous run of LIVE samples, first to last, which is
    exactly the span watch_time() can integrate over. Deliberately not
    `duration - sum(offline_spans())`: those spans open at the first OFFLINE row
    rather than at the last live one, because they are drawn as shaded bands and
    a band has to start where the data stops. Reusing them here would credit one
    poll interval of live time per gap -- harmless on a chart, wrong in a
    percentage that is supposed to read 100%.

    Note this is the time the channel was LIVE, not the time that could be
    integrated. The difference is the point: a poller that died mid-broadcast
    leaves no rows at all rather than offline ones, so its outage stays inside
    this figure and coverage correctly drops, while an advertised break does
    not. A live sample YouTube reported no viewer count for counts here too,
    and so shows up as the shortfall it is.
    """
    total, opened, last = 0.0, None, None
    for sample in window:
        if sample.get("live"):
            if opened is None:
                opened = sample["when"]
            last = sample["when"]
        elif opened is not None:
            total += (last - opened).total_seconds()
            opened = None
    if opened is not None:
        total += (last - opened).total_seconds()
    return total


def fmt_coverage(covered, duration):
    """'98% covered', or "" when the whole stream was.

    Silent at 100%, which is the ordinary case: a caption that says nothing is
    wrong on every chart trains the eye to skip the one where something is.
    """
    if not duration or covered is None:
        return ""
    share = covered / float(duration)
    return "" if share >= 0.995 else "{:.0f}% covered".format(share * 100)


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
    add(text(peak_x, 92, "at {}  ·  {} in".format(
        fmt_clock(peak_sample["when"]), fmt_elapsed(peak_offset)),
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



def parse_day(text):
    """A local date from 'YYYY-MM-DD', 'today' or 'yesterday'.

    Shared by `graph --date` and the daily report so the two can't drift on
    what "today" means.
    """
    keyword = str(text).strip().lower()
    today = datetime.now().astimezone().date()
    if keyword == "today":
        return today
    if keyword == "yesterday":
        return today - timedelta(days=1)
    try:
        return datetime.strptime(str(text).strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(
            "Bad date '{}'. Use YYYY-MM-DD, 'today' or 'yesterday'.".format(text))


def select_day(samples, day):
    """Every sample from the first live one of `day` to the last, inclusive.

    Offline rows *between* those two are kept, so a stream that dropped out and
    came back charts as one continuous day rather than several broadcasts.
    Dates are matched in local time, since that is the day the user means.
    """
    same_day = [s for s in samples if s["when"].astimezone().date() == day]
    live = [i for i, s in enumerate(same_day) if s["live"]]
    if not live:
        return []
    return same_day[live[0]:live[-1] + 1]


def days_present(samples):
    """Local dates that have at least one live sample."""
    return sorted({s["when"].astimezone().date() for s in samples if s["live"]})


def runs_of(window, key):
    """Contiguous (elapsed, value) runs, broken wherever the metric is missing.

    Viewers are absent from offline rows, so the line breaks over a downtime
    instead of drawing a straight segment across it as if it were data.
    """
    start = window[0]["when"]
    runs, current = [], []
    for sample in window:
        value = sample.get(key)
        if value is None:
            if current:
                runs.append(current)
                current = []
        else:
            current.append(((sample["when"] - start).total_seconds(), value))
    if current:
        runs.append(current)
    return runs


def offline_spans(window):
    """(from, to) elapsed ranges where the channel was offline."""
    start = window[0]["when"]
    spans, opened = [], None
    for sample in window:
        elapsed = (sample["when"] - start).total_seconds()
        if not sample["live"] and opened is None:
            opened = elapsed
        elif sample["live"] and opened is not None:
            spans.append((opened, elapsed))
            opened = None
    if opened is not None:
        spans.append((opened, (window[-1]["when"] - start).total_seconds()))
    return spans


def clock_ticks(start, duration, target=8):
    """(elapsed, "H:MM PM") ticks on tidy clock boundaries."""
    for step in (900, 1800, 3600, 7200, 10800, 14400, 21600):
        if duration / step <= target:
            break
    ticks = []
    first = start.astimezone()
    offset = (step - (first.hour * 3600 + first.minute * 60 + first.second) % step) % step
    elapsed = offset
    while elapsed <= duration:
        ticks.append((elapsed, fmt_clock(start + timedelta(seconds=elapsed))))
        elapsed += step
    return ticks


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


def draw_panel(out, metric, session, geom, bucket_minutes, show_buckets, duration,
               spans=(), ticks=None):
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

    # Shade downtime before the series, so the line sits on top of it.
    for span_from, span_to in spans:
        x1, x2 = sx(span_from), sx(span_to)
        if x2 - x1 >= 1:
            out.append('<rect x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" '
                       'fill="#ffffff" opacity="0.055"/>'.format(x1, top, x2 - x1, bottom - top))

    # One path per contiguous run, so a gap stays a gap.
    for run in runs_of(session, metric["key"]):
        coords = " ".join("{:.1f},{:.1f}".format(sx(t), sy(v)) for t, v in run)
        if len(run) > 1:
            out.append('<path d="M {:.1f},{:.1f} L {} L {:.1f},{:.1f} Z" '
                       'fill="url(#{})"/>'.format(sx(run[0][0]), bottom,
                                                  coords.replace(" ", " L "),
                                                  sx(run[-1][0]), bottom, gradient_id))
            out.append('<polyline points="{}" fill="none" stroke="{}" stroke-width="2" '
                       'stroke-linejoin="round" stroke-linecap="round"/>'.format(
                           coords, metric["color"]))
        else:
            out.append('<circle cx="{:.1f}" cy="{:.1f}" r="2" fill="{}"/>'.format(
                sx(run[0][0]), sy(run[0][1]), metric["color"]))

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
        peak_when = session[0]["when"] + timedelta(seconds=stats["peak_at"])
        caption = "peak {} at {} ({} in)   ·   avg {}".format(
            fmt_count(stats["peak"]), fmt_clock(peak_when),
            fmt_elapsed(stats["peak_at"]), fmt_count(stats["avg"]))
    out.append(text(right, top - 12, caption, size=12, fill=MUTED, anchor="end"))
    out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
               'stroke-width="1"/>'.format(left, bottom, right, bottom, GRID))


def render_stacked(session, channel, bucket_minutes, width, show_buckets=True,
                   metrics=None, day=None):
    """One panel per metric, sharing an x-axis."""
    metrics = metrics or available_metrics(session)
    start = session[0]["when"]
    duration = (session[-1]["when"] - start).total_seconds()
    spans = offline_spans(session) if day else []
    ticks = clock_ticks(start, duration) if day else None
    height = HEADER_H + len(metrics) * (PANEL_H + PANEL_GAP) + FOOT_H
    left, right = PAD_L, width - PAD_R

    out = []
    out.append('<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}" '
               'viewBox="0 0 {} {}" font-family="Roboto, -apple-system, '
               'BlinkMacSystemFont, &quot;Helvetica Neue&quot;, Arial, sans-serif">'.format(
                   width, height, width, height))
    out.append('<rect width="{}" height="{}" fill="{}"/>'.format(width, height, BG))

    out.append(text(PAD_L, 46, "Stream metrics", size=21, fill=FG, weight="700"))
    subtitle = ("{} · {}".format(channel, day.strftime("%a %-d %b %Y")) if day
                else "While live · {}".format(channel))
    if day and spans:
        offline_total = sum(b - a for a, b in spans)
        subtitle += "  ·  {} offline mid-day".format(fmt_elapsed(offline_total))
    out.append(text(PAD_L, 74, subtitle, size=14, fill=MUTED))

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
        if metric["key"] != "followers":
            out.append(text(tile_x, 91, "at {}".format(
                fmt_clock(start + timedelta(seconds=stats["peak_at"]))),
                size=11, fill=DIM, anchor="end"))
        tile_x -= 190

    # Leftmost, after the metric tiles, because it is the only figure here that
    # is DERIVED rather than sampled -- an integral under the viewer curve and
    # not a reading off it. "Estimated" is in the label rather than in a footnote
    # for the same reason: this number gets quoted.
    watched, covered = session_watch_time(session)
    out.append(text(tile_x, 52, fmt_hours(watched), size=27, fill=WATCH_COLOR,
                    weight="700", anchor="end"))
    out.append(text(tile_x, 74, "Est. watch time", size=12, fill=MUTED, anchor="end"))
    out.append(text(tile_x, 91,
                    fmt_coverage(covered, live_seconds(session)) or "live, this stream",
                    size=11, fill=DIM, anchor="end"))

    for index, metric in enumerate(metrics):
        top = HEADER_H + index * (PANEL_H + PANEL_GAP)
        draw_panel(out, metric, session, (left, right, top, top + PANEL_H),
                   bucket_minutes, show_buckets, duration, spans=spans, ticks=ticks)

    baseline = HEADER_H + len(metrics) * (PANEL_H + PANEL_GAP) - PANEL_GAP
    if ticks:
        for elapsed, label in ticks:
            x = left + (elapsed / duration) * (right - left) if duration else left
            out.append(text(x, baseline + 24, label, size=12, fill=MUTED, anchor="middle"))
    else:
        out.append(text(left, baseline + 24, "0:00", size=13, fill=MUTED))
        out.append(text(right, baseline + 24, fmt_elapsed(duration), size=13,
                        fill=MUTED, anchor="end"))
        out.append(text((left + right) / 2, baseline + 24, "{} → {}".format(
            fmt_clock(start), fmt_clock(session[-1]["when"])), size=13, fill=DIM,
            anchor="middle"))
    legend_x = left
    if show_buckets:
        out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
                   'stroke-width="1.5" opacity="0.5"/>'.format(
                       legend_x, height - 14, legend_x + 18, height - 14, PANEL_LINE))
        out.append(text(legend_x + 25, height - 10,
                        "{}-minute average".format(bucket_minutes), size=12, fill=DIM))
        legend_x += 170
    if spans:
        out.append('<rect x="{:.1f}" y="{:.1f}" width="18" height="10" fill="#ffffff" '
                   'opacity="0.09"/>'.format(legend_x, height - 20))
        out.append(text(legend_x + 25, height - 10, "stream offline", size=12, fill=DIM))
    out.append(text(right, height - 10, start.astimezone().strftime("%a %-d %b %Y"),
                    size=12, fill=DIM, anchor="end"))
    out.append("</svg>")
    return "\n".join(out)


def render_composite(session, channel, width, height, metrics=None, day=None):
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
    out.append(text(PAD_L, 74, "{} · each series scaled to its own range".format(
        "{} · {}".format(channel, day.strftime("%a %-d %b %Y")) if day
        else "While live · " + channel), size=14, fill=MUTED))

    spans = offline_spans(session) if day else []
    for span_from, span_to in spans:
        x1 = left + (span_from / duration) * (right - left) if duration else left
        x2 = left + (span_to / duration) * (right - left) if duration else left
        if x2 - x1 >= 1:
            out.append('<rect x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" '
                       'fill="#ffffff" opacity="0.055"/>'.format(x1, top, x2 - x1, bottom - top))

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
        if metric["key"] != "followers":
            out.append(text(tile_x, 91, "at {}".format(
                fmt_clock(start + timedelta(seconds=stats["peak_at"]))),
                size=11, fill=DIM, anchor="end"))
        tile_x -= 190

    watched, covered = session_watch_time(session)
    out.append(text(tile_x, 52, fmt_hours(watched), size=27, fill=WATCH_COLOR,
                    weight="700", anchor="end"))
    out.append(text(tile_x, 74, "Est. watch time", size=12, fill=MUTED, anchor="end"))
    out.append(text(tile_x, 91,
                    fmt_coverage(covered, live_seconds(session)) or "live, this stream",
                    size=11, fill=DIM, anchor="end"))

    legend_x = left
    for metric in metrics:
        points = series_of(session, metric["key"])
        if not points:
            continue
        values = [v for _, v in points]
        low, high = min(values), max(values)
        span = (high - low) or 1

        for run in runs_of(session, metric["key"]):
            if len(run) < 2:
                continue
            coords = " ".join("{:.1f},{:.1f}".format(
                left + (t / duration) * (right - left) if duration else left,
                bottom - ((v - low) / span) * (bottom - top)) for t, v in run)
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


def _sample_gap(points):
    """The median seconds between one platform's samples; its polling interval."""
    gaps = sorted((b - a).total_seconds()
                  for (a, _), (b, _) in zip(points, points[1:]))
    return gaps[len(gaps) // 2] if gaps else 60.0


def align_platforms(series):
    """(minutes, per_platform_values, combined) on one shared time grid.

    The platforms are polled independently and not always on the same second,
    so the grid has minutes where one of them simply wasn't sampled. Those are
    carried forward — but only as far as that platform's own polling interval
    stretches, after which it reads zero.

    Both halves matter. Without the carry, a poller sampling a few seconds off
    the minute punches spurious holes through its own curve. Without the
    cutoff, a Twitch stream that ended at noon stays propped up at its closing
    number for the rest of the day, and the combined line invents an audience
    that had gone home.

    Missing reads None, never 0, because 0 is a real answer — a stream that has
    just gone live genuinely has no viewers yet, and drawing "we don't know"
    the same as "nobody was watching" would put a cliff in the curve.
    """
    # A continuous minute axis, not just the minutes that happen to have samples.
    # Built from the union it would have no room for a hole: an hour with no
    # data at all would simply not exist, and the line would be drawn straight
    # across it as though the audience had drifted rather than the poller died.
    stamps = [when.replace(second=0, microsecond=0)
              for entry in series for when, _ in entry["points"]]
    if not stamps:
        return [], {entry["key"]: [] for entry in series}, []
    first, last = min(stamps), max(stamps)
    span = int((last - first).total_seconds() // 60)
    grid = [first + timedelta(minutes=i) for i in range(span + 1)]
    values = {}
    for entry in series:
        points = sorted(entry["points"])
        tolerance = _sample_gap(points) * GAP_TOLERANCE
        column = []
        index = 0
        for minute in grid:
            while index + 1 < len(points) and points[index + 1][0] <= minute:
                index += 1
            when, viewers = points[index] if points else (None, 0)
            fresh = when is not None and abs((minute - when).total_seconds()) <= tolerance
            column.append(viewers if fresh else None)
        values[entry["key"]] = column

    # Only where *every* platform is known. A total built from whichever
    # happened to be polled would undercount — before the YouTube poller was
    # started, "combined" would have been Twitch alone, drawn as if it were the
    # whole audience.
    combined = []
    for i in range(len(grid)):
        known = [values[entry["key"]][i] for entry in series]
        combined.append(sum(known) if all(v is not None for v in known) else None)
    return grid, values, combined


def _runs(grid, column):
    """Split a column into runs of consecutive known values, so gaps stay gaps."""
    runs, current = [], []
    for when, value in zip(grid, column):
        if value is None:
            if len(current) > 1:
                runs.append(current)
            current = []
        else:
            current.append((when, value))
    if len(current) > 1:
        runs.append(current)
    return runs


def render_platforms(series, channel, day, width=1300, height=430, show_combined=True,
                     aligned=None):
    """Concurrent viewers from every platform, on one shared axis.

    Deliberately not normalised the way render_composite() is. There the point
    was to compare the *shapes* of metrics on different scales; here the whole
    question is how the platforms compare in size, and scaling each to its own
    range would answer it backwards — a channel with fifty Twitch viewers and
    five hundred on YouTube would show two curves of equal height.

    Gaps are real. A platform not streaming at a given minute is drawn at zero
    rather than bridged, so a chart of two broadcasts that only half overlap
    looks like exactly that.

    `aligned` is an already-built (grid, values, combined), for a caller that
    got the grid from somewhere other than these samples — the database builds
    the same thing in SQL, so the site can be generated from stored rows rather
    than by re-reading every sample. Left None, the grid is built here from
    `series`, which is what charting a CSV still does. The two are held to being
    the same thing by a parity check in tests/smoke.py; this parameter is what
    lets both exist without a second copy of the drawing code.
    """
    series = [entry for entry in series if entry["points"]]
    if not series:
        return None

    grid, values, combined = aligned or align_platforms(series)
    if len(grid) < 2:
        return None
    # A total is only worth drawing when there is more than one thing in it;
    # otherwise it would trace the single platform's line exactly.
    show_combined = show_combined and len(series) > 1 and any(
        v is not None for v in combined)
    start, finish = grid[0], grid[-1]
    duration = (finish - start).total_seconds() or 1

    known_total = [v for v in combined if v is not None]
    if not known_total:
        return None
    peak_total = max(known_total)
    top_value, step = nice_axis(peak_total if show_combined else max(
        max(v for v in column if v is not None) for column in values.values()))

    left, right = PAD_L, width - PAD_R
    top, bottom = HEADER_H, height - FOOT_H - 26

    def x_of(when):
        return left + ((when - start).total_seconds() / duration) * (right - left)

    def y_of(value):
        return bottom - (value / top_value) * (bottom - top)

    out = []
    out.append('<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}" '
               'viewBox="0 0 {} {}" font-family="Roboto, -apple-system, '
               'BlinkMacSystemFont, &quot;Helvetica Neue&quot;, Arial, sans-serif">'.format(
                   width, height, width, height))
    out.append('<rect width="{}" height="{}" fill="{}"/>'.format(width, height, BG))
    out.append(text(PAD_L, 46, "Concurrent viewers, everywhere", size=21, fill=FG,
                    weight="700"))
    # Watch time goes in the subtitle rather than into a tile of its own: the
    # tiles here are already one per platform plus the combined peak, and a
    # seventh at 175px apart would run off the left edge on a three-platform
    # channel. Summed across platforms, since that is what this chart is for.
    #
    # Deliberately from `series` and not from the aligned grid. The grid carries
    # a value forward to paint a continuous line, so summing it would count a
    # carried minute as watched -- and `series` is what both callers pass
    # identically, which is what keeps the two paths' SVG byte-for-byte equal.
    watched = [watch_time(entry["points"])[0] for entry in series]
    total = sum(v for v in watched if v is not None) if any(
        v is not None for v in watched) else None
    subtitle = "{} · {}".format(channel, day.strftime("%a %-d %b %Y"))
    if total is not None:
        subtitle += "  ·  {} estimated watch time, live".format(fmt_hours(total))
    out.append(text(PAD_L, 74, subtitle, size=14, fill=MUTED))

    # Headline tiles, right to left: the combined peak first, because "how many
    # people were watching at once" is the number this chart exists to answer.
    tile_x = width - PAD_R + 84
    if show_combined:
        out.append(text(tile_x, 52, fmt_count(peak_total), size=27, fill=FG,
                        weight="700", anchor="end"))
        out.append(text(tile_x, 74, "Peak combined", size=12, fill=MUTED, anchor="end"))
        out.append(text(tile_x, 91, "at {}".format(
            fmt_clock(grid[combined.index(peak_total)])), size=11, fill=DIM, anchor="end"))
        tile_x -= 175
    for entry in reversed(series):
        column = values[entry["key"]]
        peak = max(v for v in column if v is not None)
        out.append(text(tile_x, 52, fmt_count(peak), size=27,
                        fill=entry["color"], weight="700", anchor="end"))
        out.append(text(tile_x, 74, "Peak {}".format(entry["label"]), size=12,
                        fill=MUTED, anchor="end"))
        out.append(text(tile_x, 91, "at {}".format(
            fmt_clock(grid[column.index(peak)])), size=11, fill=DIM, anchor="end"))
        tile_x -= 175

    gridline = 0
    while gridline <= top_value:
        y = y_of(gridline)
        out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
                   'stroke-width="1"/>'.format(left, y, right, y, GRID))
        out.append(text(right + 12, y + 4, fmt_count(gridline), size=12, fill=MUTED))
        gridline += step

    for elapsed, label in clock_ticks(start, duration):
        x = left + (elapsed / duration) * (right - left)
        out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
                   'stroke-width="1" opacity="0.55"/>'.format(x, top, x, bottom, GRID))
        out.append(text(x, bottom + 24, label, size=12, fill=DIM, anchor="middle"))

    for entry in series:
        for run in _runs(grid, values[entry["key"]]):
            points = " ".join("{:.1f},{:.1f}".format(x_of(when), y_of(value))
                              for when, value in run)
            out.append('<polygon points="{} {:.1f},{:.1f} {:.1f},{:.1f}" fill="{}" '
                       'opacity="0.13"/>'.format(points, x_of(run[-1][0]), bottom,
                                                 x_of(run[0][0]), bottom, entry["color"]))
            out.append('<polyline points="{}" fill="none" stroke="{}" stroke-width="2.2" '
                       'stroke-linejoin="round" stroke-linecap="round"/>'.format(
                           points, entry["color"]))

    if show_combined:
        for run in _runs(grid, combined):
            points = " ".join("{:.1f},{:.1f}".format(x_of(when), y_of(value))
                              for when, value in run)
            out.append('<polyline points="{}" fill="none" stroke="{}" stroke-width="1.6" '
                       'stroke-dasharray="5 4" opacity="0.75"/>'.format(
                           points, COMBINED_LINE))

    legend_x = left
    entries = [(entry["label"], entry["color"]) for entry in series]
    if show_combined:
        entries.append(("Combined", COMBINED_LINE))
    for label, colour in entries:
        dash = ' stroke-dasharray="5 4"' if label == "Combined" else ""
        out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
                   'stroke-width="2.5" stroke-linecap="round"{}/>'.format(
                       legend_x, height - 22, legend_x + 20, height - 22, colour, dash))
        out.append(text(legend_x + 27, height - 18, label, size=12, fill=MUTED))
        legend_x += 27 + len(label) * 7.2 + 30

    out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
               'stroke-width="1"/>'.format(left, bottom, right, bottom, GRID))
    out.append(text(right, height - 18, "{} → {}".format(
        fmt_clock(start), fmt_clock(finish)), size=12, fill=DIM, anchor="end"))
    out.append("</svg>")
    return "\n".join(out)


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------


def print_summary(session, bucket_minutes, metrics=None):
    start = session[0]["when"]
    duration = (session[-1]["when"] - start).total_seconds()
    metrics = metrics or available_metrics(session)

    watched, covered = session_watch_time(session)
    print("  started   {}".format(start.astimezone().strftime("%a %-d %b %Y, %-I:%M %p")))
    print("  duration  {}   ({} samples)".format(fmt_elapsed(duration), len(session)))
    print("  watched   {} estimated   ({})".format(
        fmt_hours(watched),
        fmt_coverage(covered, live_seconds(session)) or "whole stream integrated"))
    if watched is not None:
        # Spelled out once, in the one place a person reads numbers rather than
        # looks at them. Neither platform reports watch time -- this is the area
        # under the concurrent-viewer curve, so it counts the live audience and
        # nothing that watched the replay afterwards.
        print("            live only; excludes replay watch time")

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
            print("    peak    {}  at {}  ({} into the stream)".format(
                fmt_count(stats["peak"]),
                fmt_clock(start + timedelta(seconds=stats["peak_at"])),
                fmt_elapsed(stats["peak_at"])))
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


