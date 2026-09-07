"""Charts that span days rather than living inside one.

chart.py answers "what happened during this broadcast". Everything here answers
"how does that compare", which needs a different axis: calendar days, and clock
time rather than time since the stream started.

Two charts, each rendered per platform:

  * peak concurrent viewers for each of the last ten days
  * average viewers per half hour, today against each of the five days before it

The palette, the SVG primitives and the axis maths all come from chart.py, so
these read as the same family as the per-day graphs rather than as a second
charting library that happens to live in the same package.
"""

import math
from datetime import time, timedelta

from .chart import (BG, DIM, FG, GRID, METRIC_BY_KEY, MUTED, PAD_L, PAD_R,
                    PLATFORMS, WATCH_COLOR, esc, fmt_count, nice_axis, text)

PEAK_DAYS = 10          # days on the peaks chart
COMPARE_DAYS = 5        # days shown *behind* today on the comparison chart
BUCKET_MINUTES = 30

# Broadcasts on the per-stream charts. Ten BROADCASTS, not ten days that
# streamed: those charts collapse a day spent at two places into one bar, which
# is exactly the reading the location chart exists to avoid.
STREAM_COUNT = 10

# What a broadcast whose title matched no location rule is called. Named rather
# than dropped -- an unlabelled bar says "a title has drifted out of its rule",
# which is something to go and fix, where a missing bar says nothing at all.
UNKNOWN_LOCATION = "Unknown"

# Monday first, matching both date.weekday() and the weekday column the report
# table stores. The axis is filled from this rather than from the rows, for the
# reason compare_slots()' per_day axis is: a renderer taking its axis from the
# data relabels itself when a day is missing.
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Days on the rolling watch-time chart, and the window its trailing total sums.
# 365 because the figure it is standing next to -- YouTube's 4,000-hour Partner
# Programme bar -- is a trailing-twelve-months one, and a rolling total measured
# over any other span would invite exactly the comparison it cannot support.
WATCH_DAYS = 30
ROLLING_DAYS = 365

# The YPP threshold, drawn as a reference line on the YouTube chart only.
#
# It is a reference and NOT a progress bar, and the chart says so in as many
# words. The real figure counts valid public watch hours across live AND VOD;
# this estimate integrates the live concurrent-viewer curve and knows nothing
# about replays or about YouTube's validity rules, so it undercounts by a margin
# nothing here can measure. Twitch has no equivalent bar, which is why the line
# is platform-gated rather than a constant on every chart.
YPP_TARGET_HOURS = 4000

# How far back to hunt for a day that streamed. The charts compare streams, not
# dates, so the axis has to be allowed to reach past a quiet fortnight -- but
# not past everything: a channel dormant since last year would otherwise make
# every run rebuild its whole history to find ten bars. Coming up short is fine
# and says something true; the chart simply has fewer bars.
LOOKBACK_DAYS = 90

# Half a day of half hours. Six bars in each of 48 groups is 288 bars across a
# 1300px chart, which is a texture rather than a reading.
MAX_SLOTS = 24

# The five earlier days, faintest first. Recency as weight, so the six days read
# in order without needing six colours the eye then has to match to a legend.
FADES = (0.32, 0.42, 0.53, 0.64, 0.78)
TODAY_FADE = 1.0

PLATFORM_BY_KEY = {spec["key"]: spec for spec in PLATFORMS}

DASH = "—"         # what a day with no stream gets instead of a bar

# The zero line, when zero is not the floor. A chart with bars hanging below the
# baseline has to say which line the baseline is, and position no longer does.
DIVIDER_ZERO = "#5a5a5a"

# Where a bar sends the reader. Relative, and one level up, because the browser
# resolves it against the SVG's own URL -- these charts live under trends/.
DAY_PREFIX = "day/"
# The venue pages, and the charts that sit flat beside them. One directory, not
# one per venue: see location_link() for why the depth is load-bearing.
LOCATION_PREFIX = "location/"

# The longest a venue slug may be. A stream title can be any length and a rule
# names a location freely, so without a cap a chatty venue name becomes a
# 300-character filename -- past the 255-byte limit a filename component has on
# every common filesystem, which would break the first person to sync the bucket
# to disk. S3 itself would take it; the person copying it out would not.
#
# Truncating makes two long names likelier to collide, and that is handled
# rather than avoided: s3.location_slugs() suffixes a whole colliding class.
MAX_SLUG = 60

# The size each chart renders at, in one place because the page needs it too:
# an <object> has to be told its aspect ratio, where an <img> works it out.
SIZES = {"peaks": (1300, 380), "typical": (1300, 430),
         "followers": (1300, 400), "likes": (1300, 400),
         "weekday": (1300, 360), "location": (1300, 360),
         "watchtime": (1300, 400), "watchrolling": (1300, 400),
         "watchlocation": (1300, 360),
         # Peak viewers, per broadcast and rolled up by venue. Named
         # "peakstream" rather than "peak" so it cannot be confused with the
         # by-day "peaks" chart sitting in the same directory.
         "peakstream": (1300, 400), "peaklocation": (1300, 360),
         # The venue pages. "compare" is this venue against every other, so it
         # is a rollup and takes the rollups' height; "history" is one bar per
         # visit across a whole record and takes the per-broadcast one.
         "compare": (1300, 360), "history": (1300, 400)}

# The room a date label needs under a bar, in pixels. "Sun 10 May" at 12px is
# about seventy, so eighty leaves a gap rather than letting two labels touch.
#
# A WIDTH AND NOT A BAR COUNT, which is the whole of the rule: how many labels
# fit is a question about pixels per bar, and a chart drawn narrower or a venue
# with three years of visits both change that. A fixed "thin above N bars" gets
# the common case right and then overlaps anyway at N+1, because N+1 bars still
# have no more room than N did.
#
# Below the threshold every bar keeps all three of its lines, which is what
# leaves every chart already on the site byte-identical: ten bars on a 1300px
# axis are 116px each, comfortably clear.
#
# The tooltips carry every figure whatever the stride, so nothing thinning drops
# is unreachable -- the same bargain render_watch_rolling() strikes when it draws
# no bar per day.
LABEL_WIDTH = 80

# Bars on a venue's whole-history chart. 0 means every broadcast on record.
#
# A ceiling rather than no ceiling, because a venue visited weekly for three
# years is 150 bars on a 1300px axis -- eight pixels each, which is a texture and
# not a reading, and no amount of label thinning fixes a bar too narrow to see.
# 120 is roughly two years of weekly visits, which is long enough for the slope
# the chart exists to show.
LOCATION_HISTORY = 120

# --- layout ---------------------------------------------------------------
HEAD_H = 118            # shorter than chart.HEADER_H: no in-stream tiles to fit
FOOT_H = 62             # room for the date labels and, on the comparison, a legend
GROUP_GAP = 0.26        # share of a group's width left empty between groups


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------


def window(end_day, days):
    """The `days` calendar dates ending at `end_day`, oldest first."""
    return [end_day - timedelta(days=n) for n in reversed(range(days))]


def streamed_window(samples, end_day, days, lookback=LOOKBACK_DAYS):
    """The last `days` dates on or before end_day that streamed, oldest first.

    The axis the trend charts use by default. window() answers "which dates
    ended here", which for a channel streaming twice a week is mostly days off
    -- eight dashes and two bars in a ten-day chart. This answers "which
    streams ended here", so ten bars mean ten streams whenever they happened.

    "Streamed" is exactly the test _live_on() applies, a live sample carrying a
    viewer count, so every date this returns has a bar to draw. Fewer than
    `days` of them is not a failure; it is the channel's whole record.
    """
    if days <= 0:
        return []
    earliest = end_day - timedelta(days=max(1, int(lookback)) - 1)
    live = {s["when"].astimezone().date() for s in samples
            if s["live"] and s["viewers"] is not None}
    return sorted(d for d in live if earliest <= d <= end_day)[-days:]


def bucket_width(minutes):
    """A slot width both sides will agree on.

    refresh_clock_buckets() clamps its own argument to
    `greatest(1, least(1440, p_bucket_minutes))`, and a width Python accepted
    but the database rounded would put the labels and the bars on different
    grids. Clamping identically here is what lets `daily --bucket` be passed
    straight through to SQL.
    """
    return min(1440, max(1, int(minutes)))


def _live_on(samples, day):
    """Live samples with a viewer count, on one local date."""
    return [s for s in samples
            if s["live"] and s["viewers"] is not None
            and s["when"].astimezone().date() == day]


def daily_peaks(samples, end_day, days=PEAK_DAYS, calendar=False,
                lookback=LOOKBACK_DAYS):
    """The highest viewer count on each of the last `days` days, oldest first.

    `calendar` picks the axis: the last `days` DATES, or the last `days` dates
    that streamed. On a streamed axis no entry can have a peak of None, because
    a date only gets on to that axis by having one -- so the rule below still
    holds, it simply stops arising.

    Every day on the axis gets an entry. A day with no live samples has a peak
    of None, never 0 — the channel was not streaming, which is a different
    statement from "nobody watched", and drawing them alike would invent a
    catastrophic day out of a day off.
    """
    axis = (window(end_day, days) if calendar
            else streamed_window(samples, end_day, days, lookback))
    out = []
    for day in axis:
        same_day = _live_on(samples, day)
        if not same_day:
            out.append({"day": day, "peak": None, "at": None})
            continue
        best = max(same_day, key=lambda s: s["viewers"])
        out.append({"day": day, "peak": best["viewers"], "at": best["when"]})
    return out


def clock_buckets(samples, day, minutes=BUCKET_MINUTES):
    """{slot: average viewers} for one day, keyed by slot of the local clock.

    Slot 0 is local midnight, slot 1 is half past, and so on. Deliberately not
    chart.bucket_averages(), which counts from the moment the stream started:
    that is the right axis for reading one broadcast and the wrong one for
    comparing days, because it would lay a stream that began at 6pm over one
    that began at 8pm and call both blocks "the first half hour".
    """
    width = bucket_width(minutes)
    totals = {}
    for sample in _live_on(samples, day):
        local = sample["when"].astimezone()
        slot = (local.hour * 60 + local.minute) // width
        seen, count = totals.get(slot, (0, 0))
        totals[slot] = (seen + sample["viewers"], count + 1)
    return {slot: total / count for slot, (total, count) in totals.items()}


def _busiest_window(slots, weight, span=MAX_SLOTS):
    """The `span` consecutive slots carrying the most viewers, as a slot list.

    Contiguous rather than "the fullest slots wherever they fall": a chart of
    the busiest twelve hours is a period, whereas a chart of scattered half
    hours puts 9am next to 11pm and quietly hides what came between.
    """
    if len(slots) <= span:
        return slots
    first, last = slots[0], slots[-1]
    best_start, best_weight = first, -1.0
    for start in range(first, last - span + 2):
        total = sum(weight.get(slot, 0.0) for slot in range(start, start + span))
        if total > best_weight:
            best_start, best_weight = start, total
    return [slot for slot in slots if best_start <= slot < best_start + span]


def compare_slots(samples, end_day, days=COMPARE_DAYS, minutes=BUCKET_MINUTES,
                  calendar=False, lookback=LOOKBACK_DAYS):
    """(slots, per_day) for today and the `days` days before it.

    `per_day` is [(date, {slot: average}), ...] oldest first. On a calendar axis
    that is one entry per DATE whether or not it has data; on the streamed axis
    it is one entry per stream, and every one of them has data.

    `slots` is every slot any of them used, trimmed to MAX_SLOTS around the
    busiest stretch; the dropped count is returned so the caller can say so
    rather than silently showing less.

    Returns (slots, per_day, dropped).
    """
    axis = (window(end_day, days + 1) if calendar
            else streamed_window(samples, end_day, days + 1, lookback))
    per_day = [(day, clock_buckets(samples, day, minutes)) for day in axis]
    used = sorted({slot for _, buckets in per_day for slot in buckets})
    weight = {}
    for _, buckets in per_day:
        for slot, average in buckets.items():
            weight[slot] = weight.get(slot, 0.0) + average
    kept = _busiest_window(used, weight)
    return kept, per_day, len(used) - len(kept)


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------


def fmt_day(day, with_month=False):
    """'Mon 18' — enough to find a day in a ten-day window.

    Not enough for a streamed axis, which can span months and would print
    "Mon 18" twice with six weeks between them. The renderers ask for the month
    when their own axis crosses one, so the short form survives the common case.
    """
    return day.strftime("%a %-d %b" if with_month else "%a %-d")


def spans_months(days):
    """True when a list of dates crosses a month, so labels need the month."""
    kept = [d for d in days if d]
    return bool(kept) and (kept[0].year, kept[0].month) != (kept[-1].year, kept[-1].month)


def fmt_span(days):
    """'Sat 2 Aug – Sat 23 Aug' for an axis, or '' when there is nothing on it."""
    kept = [d for d in days if d]
    if not kept:
        return ""
    if kept[0] == kept[-1]:
        return kept[0].strftime("%a %-d %b")
    return "{} – {}".format(kept[0].strftime("%a %-d %b"),
                            kept[-1].strftime("%a %-d %b"))


def fmt_slot(slot, minutes=BUCKET_MINUTES):
    """The clock time a slot starts at, '7:00 pm'."""
    total = slot * minutes
    at = time(hour=(total // 60) % 24, minute=total % 60)
    return at.strftime("%-I:%M %p").lower()


def _spec(platform):
    return PLATFORM_BY_KEY.get(platform, {"key": platform,
                                          "label": str(platform).title(),
                                          "color": "#4fb3e8"})


def _open_svg(width, height, title, subtitle):
    """The shell both charts share: background, heading and subheading."""
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}" '
           'viewBox="0 0 {} {}" font-family="Roboto, -apple-system, '
           'BlinkMacSystemFont, &quot;Helvetica Neue&quot;, Arial, '
           'sans-serif">'.format(width, height, width, height),
           '<rect width="{}" height="{}" fill="{}"/>'.format(width, height, BG),
           text(PAD_L, 44, title, size=21, fill=FG, weight="700"),
           text(PAD_L, 70, subtitle, size=14, fill=MUTED)]
    return out


def _tile(out, x, value, label, note, colour=FG):
    """One headline figure in the top right, laid out like chart.render_platforms."""
    out.append(text(x, 48, value, size=26, fill=colour, weight="700", anchor="end"))
    out.append(text(x, 68, label, size=12, fill=MUTED, anchor="end"))
    if note:
        out.append(text(x, 85, note, size=11, fill=DIM, anchor="end"))


def _grid_lines(out, top_value, step, left, right, y_of):
    line = 0
    while line <= top_value:
        y = y_of(line)
        out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" '
                   'stroke="{}" stroke-width="1"/>'.format(left, y, right, y, GRID))
        out.append(text(right + 12, y + 4, fmt_count(line), size=12, fill=MUTED))
        line += step


def day_link(day, known=None):
    """The day page's href for `day`, or None when there is no page for it.

    `known` is the set of 'YYYY-MM-DD' the bucket actually holds a page for.
    None means "link everything": the caller either knows they all exist or has
    no way to find out, which is the case when nothing is being uploaded.
    """
    stamp = day.isoformat()
    if known is not None and stamp not in known:
        return None
    return "../{}{}.html".format(DAY_PREFIX, stamp)


def slugify(name):
    """A venue's name as a URL segment: 'Hollywood Studios' -> 'hollywood-studios'.

    Lives here rather than in s3.py because day_link() already builds a URL here
    and s3.py already imports from this module -- putting it the other way round
    would be the import cycle smoke.py's "no dependencies" section exists to
    prevent.

    The unmatched location is "" and slugs to "unknown", which is
    UNKNOWN_LOCATION lowercased and the one venue name a reader will see that no
    rule produced.

    NOT REVERSIBLE, and callers must not try: "DHS" and "dhs" slug alike, and
    unslugging 'dhs' gives "Dhs". s3.save_locations() writes the mapping into the
    bucket for the same reason titles.json exists -- so a page can be rebuilt
    from the bucket without guessing.
    """
    out = []
    for char in (name or "").strip().casefold():
        out.append(char if char.isalnum() and char.isascii() else "-")
    slug = "-".join(part for part in "".join(out).split("-") if part)
    # Cut back to a word boundary where there is one in reach, so a truncated
    # slug still reads as words rather than ending mid-syllable.
    if len(slug) > MAX_SLUG:
        slug = slug[:MAX_SLUG]
        if "-" in slug[MAX_SLUG // 2:]:
            slug = slug[:slug.rindex("-")]
    return slug.strip("-") or "unknown"


def location_link(slug, known=None):
    """The location page's href for `slug`, or None when there is no page for it.

    day_link()'s rule, for the same reason: a bar pointing at a page nobody
    uploaded is a 404, and `known` is the set of slugs the bucket actually
    holds. None means "link everything", which is the --no-upload case.

    The "../" is day_link()'s "../" and is correct for the same reason -- every
    SVG on this site lives exactly one directory down, so a link out of one is
    always one level up. That invariant is why the location charts sit flat
    under location/ rather than nested in a directory per venue.
    """
    if known is not None and slug not in known:
        return None
    return "../{}{}.html".format(LOCATION_PREFIX, slug)


def _open_link(href, tip):
    """An <a> around whatever follows, carrying `tip` as its hover text.

    target="_top" is load-bearing: trends.html embeds these charts in an
    <object>, and without it the click would replace the chart with the day
    page rather than the page the reader is looking at.
    """
    return ('<a href="{}" target="_top" style="cursor:pointer">'
            '<title>{}</title>'.format(esc(href), esc(tip)))


def _hit(x, y, width, height):
    """An invisible rectangle, so a two-pixel bar is still worth aiming at.

    pointer-events is spelled out rather than left to the default: a shape with
    no visible paint is exactly the case the default rule is ambiguous about.
    """
    return ('<rect x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" '
            'fill="{}" fill-opacity="0" pointer-events="all"/>'.format(
                x, y, width, max(0.0, height), FG))


def _bar(x, y, width, height, colour, opacity=1.0, outline=None):
    stroke = (' stroke="{}" stroke-width="1"'.format(outline) if outline else "")
    return ('<rect x="{:.1f}" y="{:.1f}" width="{:.1f}" height="{:.1f}" rx="2" '
            'fill="{}" opacity="{:.2f}"{}/>'.format(
                x, y, width, max(0.0, height), colour, opacity, stroke))


# --------------------------------------------------------------------------
# peaks over the last ten days
# --------------------------------------------------------------------------


def render_peaks(entries, channel, platform, day, width=SIZES["peaks"][0],
                 height=SIZES["peaks"][1], known=None):
    """Peak concurrent viewers per day, one bar each. None if none were streamed.

    Today is drawn at full strength and outlined; the earlier days sit back a
    little, so the bar the reader came for is the one they see first.

    Every bar that has a day page behind it is a link to that page, with the
    figure repeated as hover text. `known` is passed through to day_link().
    """
    streamed = [e for e in entries if e["peak"] is not None]
    if not streamed:
        return None

    spec = _spec(platform)
    top_value, step = nice_axis(max(e["peak"] for e in streamed))
    left, right = PAD_L, width - PAD_R
    top, bottom = HEAD_H, height - FOOT_H

    def y_of(value):
        return bottom - (value / top_value) * (bottom - top)

    # len(streamed), not len(entries): true on either axis. A calendar axis of
    # ten dates holding two streams is "2 day(s) with a stream" and so is a
    # streamed axis of two -- whereas "last 10 days" would be a lie about a
    # streamed axis spanning six weeks.
    dated = [e["day"] for e in entries]
    with_month = spans_months(dated)
    out = _open_svg(width, height,
                    "Peak viewers, last {} day(s) with a stream".format(len(streamed)),
                    "{} · {} · to {}".format(channel, spec["label"],
                                             day.strftime("%a %-d %b %Y")))

    best = max(streamed, key=lambda e: e["peak"])
    average = sum(e["peak"] for e in streamed) / len(streamed)
    tile_x = width - PAD_R + 84
    _tile(out, tile_x, fmt_count(best["peak"]), "Best day",
          best["day"].strftime("%a %-d %b"), colour=spec["color"])
    # The span, because a streamed axis is not contiguous and the reader cannot
    # infer it from the labels the way ten consecutive dates let them.
    _tile(out, tile_x - 175, fmt_count(average), "Average peak",
          "over {} day(s) live".format(len(streamed)))

    _grid_lines(out, top_value, step, left, right, y_of)

    slot_width = (right - left) / len(entries)
    bar_width = slot_width * (1 - GROUP_GAP)
    for index, entry in enumerate(entries):
        centre = left + slot_width * (index + 0.5)
        x = centre - bar_width / 2
        label_fill = FG if entry["day"] == day else MUTED
        label = text(centre, bottom + 22, fmt_day(entry["day"], with_month),
                     size=12, fill=label_fill, anchor="middle")
        if entry["peak"] is None:
            # A dash above the baseline, not a zero-height bar: the day is
            # absent from the record, and 0 viewers is a thing that can happen.
            # No link either: there is no day page for a day that never was.
            out.append(label)
            out.append(text(centre, bottom - 10, DASH, size=13, fill=DIM,
                            anchor="middle"))
            continue
        y = y_of(entry["peak"])
        today = entry["day"] == day
        href = day_link(entry["day"], known)
        if href:
            # "·" as the separator and not DASH, which means "no stream here"
            # everywhere else on these charts and would read as one here.
            out.append(_open_link(href, "{} · {} peak".format(
                fmt_day(entry["day"], True), fmt_count(entry["peak"]))))
            # The whole column is the target, down over the date label, not
            # just the bar: a quiet day is a few pixels tall, and the date is
            # what the reader aims at anyway. Transparent, and first so that it
            # hides nothing.
            out.append(_hit(x, top, bar_width, bottom - top + 28))
        out.append(label)
        out.append(_bar(x, y, bar_width, bottom - y, spec["color"],
                        TODAY_FADE if today else 0.72,
                        outline=FG if today else None))
        out.append(text(centre, y - 9, fmt_count(entry["peak"]), size=12,
                        fill=FG if today else MUTED, weight="600" if today else "normal",
                        anchor="middle"))
        if href:
            out.append("</a>")

    out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
               'stroke-width="1"/>'.format(left, bottom, right, bottom, GRID))
    # Was a repeat of the title's count. The span says the thing the axis no
    # longer can: how long these streams took to happen.
    out.append(text(right, height - 16, fmt_span(dated),
                    size=12, fill=DIM, anchor="end"))
    out.append("</svg>")
    return "\n".join(out)


# --------------------------------------------------------------------------
# today against the days before it
# --------------------------------------------------------------------------


def render_typical(slots, per_day, channel, platform, day, minutes=BUCKET_MINUTES,
                   width=SIZES["typical"][0], height=SIZES["typical"][1],
                   dropped=0, known=None):
    """Average viewers per half hour: today beside each earlier day, slot by slot.

    Every day keeps its own bar rather than being folded into a mean, so a
    single freak evening reads as one tall bar among five rather than dragging
    "normal" up with it.

    Each bar links to its own day, and so does each swatch in the legend --
    which is the easier target, being one per day rather than one per half
    hour. `known` is passed through to day_link().
    """
    if not slots or not any(buckets for _, buckets in per_day):
        return None

    spec = _spec(platform)
    peak = max(value for _, buckets in per_day for value in buckets.values())
    top_value, step = nice_axis(peak)
    left, right = PAD_L, width - PAD_R
    top, bottom = HEAD_H, height - FOOT_H - 22

    def y_of(value):
        return bottom - (value / top_value) * (bottom - top)

    # By date, not by position. per_day[-1] is the newest entry on the axis,
    # which is only `day` when the channel streamed today -- and on the platform
    # it did NOT stream today, the positional read made this tile quote a real
    # figure while no bar anywhere got the outline that says "today".
    today_buckets = next((b for d, b in per_day if d == day), {})
    dated = [d for d, _ in per_day]
    with_month = spans_months(dated)
    live_days = sum(1 for _, buckets in per_day if buckets)
    # Days that streamed and are not today. On a streamed axis that is all of
    # them but today; on a calendar axis it skips the days off -- either way it
    # counts what the reader can actually see a bar for.
    earlier = live_days - (1 if today_buckets else 0)

    note = "{} · {} · today vs the previous {} day(s) with a stream".format(
        channel, spec["label"], earlier)
    if dropped:
        note += " · busiest {} hours shown".format(len(slots) * minutes // 60)
    out = _open_svg(width, height,
                    "{}-minute averages, today vs the last {} that streamed".format(
                        minutes, earlier),
                    note)

    tile_x = width - PAD_R + 84
    if today_buckets:
        busiest = max(today_buckets, key=lambda slot: today_buckets[slot])
        _tile(out, tile_x, fmt_count(today_buckets[busiest]), "Today's best block",
              fmt_slot(busiest, minutes), colour=spec["color"])
    else:
        _tile(out, tile_x, DASH, "Today's best block", "no stream yet",
              colour=spec["color"])
    # The span rather than "of N in the window": on a streamed axis that read
    # N of N every time, which told the reader nothing they could not count.
    _tile(out, tile_x - 175, str(live_days), "Days compared", fmt_span(dated))

    _grid_lines(out, top_value, step, left, right, y_of)

    group_width = (right - left) / len(slots)
    bar_width = max(1.5, group_width * (1 - GROUP_GAP) / len(per_day))
    # Above sixteen groups every label no longer fits; thinning beats overlap,
    # and the first group is always labelled so the axis has an anchor.
    every = 1 if len(slots) <= 16 else 2
    for index, slot in enumerate(slots):
        group_left = left + group_width * index + group_width * GROUP_GAP / 2
        if index % every == 0:
            out.append(text(group_left + group_width * (1 - GROUP_GAP) / 2,
                            bottom + 20, fmt_slot(slot, minutes), size=11,
                            fill=DIM, anchor="middle"))
        for position, (bucket_day, buckets) in enumerate(per_day):
            value = buckets.get(slot)
            if value is None:
                continue
            x = group_left + bar_width * position
            y = y_of(value)
            today = bucket_day == day
            opacity = TODAY_FADE if today else FADES[min(position, len(FADES) - 1)]
            # No column-wide hit area here, the way the peaks chart has one:
            # six days share a group, so a full-height target would sit over
            # its neighbours and the reader would open the wrong day.
            href = day_link(bucket_day, known)
            if href:
                out.append(_open_link(href, "{} · {} · {} avg".format(
                    fmt_day(bucket_day, True), fmt_slot(slot, minutes),
                    fmt_count(value))))
            out.append(_bar(x, y, bar_width * 0.88, bottom - y, spec["color"],
                            opacity, outline=FG if today else None))
            if href:
                out.append("</a>")

    out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
               'stroke-width="1"/>'.format(left, bottom, right, bottom, GRID))

    # Only the days that actually drew something. A swatch for a day off would
    # send the reader hunting for a bar that was never there.
    legend_x = left
    for position, (bucket_day, buckets) in enumerate(per_day):
        if not buckets:
            continue
        today = bucket_day == day
        opacity = TODAY_FADE if today else FADES[min(position, len(FADES) - 1)]
        label = "Today" if today else fmt_day(bucket_day, with_month)
        href = day_link(bucket_day, known)
        if href:
            out.append(_open_link(href, "Open {}".format(fmt_day(bucket_day, True))))
            out.append(_hit(legend_x, height - 34, 20 + len(label) * 7.2, 22))
        out.append(_bar(legend_x, height - 30, 13, 13, spec["color"], opacity,
                        outline=FG if today else None))
        out.append(text(legend_x + 20, height - 19, label, size=12,
                        fill=FG if today else MUTED))
        if href:
            out.append("</a>")
        legend_x += 20 + len(label) * 7.2 + 22
    out.append("</svg>")
    return "\n".join(out)


# --------------------------------------------------------------------------
# per-broadcast bars
# --------------------------------------------------------------------------


# What each metric is called on its own chart. The set is data so that a third
# metric is an entry here rather than a third renderer -- which is the same
# reason tm.report_stream_metric is stored long.
STREAM_METRICS = {
    # First because it is the metric a reader looks for first, and because it is
    # the only one both platforms always carry -- followers are Twitch's and
    # likes are YouTube's, so on any one page the other two are half absent.
    #
    # The key is "peak" because store.stream_trends() calls the field "peak" and
    # render_stream_bars() indexes a row by the metric's own name. The chart KIND
    # is "peakstream", which is not the same string and must not be: the by-day
    # chart is already "peaks", and trends/peak-twitch.svg sitting beside
    # trends/peaks-twitch.svg would be two charts one letter apart in one
    # directory, telling a reader nothing about which is which. "kind" is how a
    # metric says its chart is named something other than itself.
    #
    # There has been a peak-viewers chart since the beginning, but per DAY. A
    # Saturday spent at two parks is one bar there and two here, and only the
    # second can be grouped by venue -- which is the whole reason this axis
    # exists beside that one rather than instead of it.
    # No "color": it falls through to the platform's, which is what render_peaks()
    # already uses. The two peak charts sit on one page and should read as the
    # same measurement at two grains rather than as two different metrics.
    "peak": {"title": "Peak viewers per stream", "kind": "peakstream",
             "best": "Best stream", "average": "Average peak",
             "noun": "watching", "signed": False},
    "followers": {"title": "Followers gained per stream",
                  "best": "Best stream", "average": "Average gain",
                  "noun": "gained", "signed": True},
    "likes": {"title": "Peak likes per stream",
              "best": "Best stream", "average": "Average peak",
              "noun": "likes", "signed": False},
    # The one entry whose values are not whole people. `hours` switches the two
    # formatters below; everything else about the renderer is unchanged, which
    # is the point of the set being data.
    #
    # Both platforms carry it, unlike the two above -- so this is the one chart
    # here that draws twice, and the only one that cannot select its platform by
    # the metric simply being absent.
    "watchtime": {"title": "Estimated watch hours per stream",
                  "best": "Best stream", "average": "Average per stream",
                  "noun": "estimated watch hours", "signed": False,
                  "hours": True, "color": WATCH_COLOR},
}

GROUPINGS = {
    "weekday":  {"title": "{} by day of week", "best": "Best day"},
    "location": {"title": "{} by location",    "best": "Best location"},
}


def _fmt_avg(value):
    """'18' when a mean lands whole, '10.7' when it does not."""
    if abs(value - round(value)) < 0.05:
        return fmt_count(value)
    return "{:.1f}".format(value)


def _fmt_signed(value, signed=True):
    """'+18' / '-5' for a delta, plain for a count that cannot go backwards."""
    if not signed:
        return fmt_count(value)
    return "{}{}".format("+" if value > 0 else "", fmt_count(value))


def fmt_watch_hours(hours):
    """Watch hours for a bar or a tile: '4.2' small, '2,615' once it is not.

    Bare, with no unit. Every chart that prints these says "watch hours" in its
    heading, and repeating it on twelve bars is noise -- the same reason the
    peaks chart labels its bars with a number and not with "viewers".
    """
    if hours is None:
        return DASH
    return "{:.1f}".format(hours) if abs(hours) < 10 else fmt_count(hours)


def _coverage_note(entry, noun="the broadcast"):
    """' · 91% of the broadcast covered', or '' when all of it was.

    Silent at 100%, which is the ordinary case. A qualifier printed on every
    chart teaches the eye to skip it, and this one has to still be legible on
    the chart where a poller died mid-stream and the bar is short for a reason
    that has nothing to do with the audience.

    `noun` is what was covered, because the same ratio describes one broadcast on
    the per-stream chart and a whole venue's worth of them on the location one.
    A parameter rather than a second function: the 0.995 threshold is the part
    worth having in exactly one place.
    """
    share = entry.get("coverage")
    if share is None or share >= 0.995:
        return ""
    return " · {:.0f}% of {} covered".format(share * 100, noun)


def _fmt_value(metric, value):
    """One bar's label, in whatever unit that metric counts in."""
    spec = STREAM_METRICS[metric]
    if spec.get("hours"):
        return fmt_watch_hours(value)
    return _fmt_signed(value, spec["signed"])


def _fmt_mean(metric, value):
    """The same for an average, which followers and likes print differently."""
    spec = STREAM_METRICS[metric]
    return fmt_watch_hours(value) if spec.get("hours") else _fmt_avg(value)


def _whole_step(step):
    """The next 1/2/5-times-a-power-of-ten step at or above `step`, as an int.

    nice_axis() will hand back 2.5, which is a fine step for a viewer count in
    the hundreds and a wrong one here. These axes are small -- a follower gain
    is single or double figures -- and _signed_grid() labels its lines with
    fmt_count(), which rounds to whole numbers. A 2.5 step therefore prints
    0, 2, 5, 8, 10: gridlines evenly spaced on the page, unevenly spaced in
    their labels, and two of the labels simply untrue.

    These axes count people, so the step counts people too.
    """
    step = max(1.0, float(step))
    magnitude = 10 ** math.floor(math.log10(step))
    for mult in (1, 2, 5):
        if step <= mult * magnitude + 1e-9:
            return int(mult * magnitude)
    return int(10 * magnitude)


def _signed_axis(values):
    """(low, top, step) for an axis that may need room BELOW zero.

    Followers go down as well as up. Drawing a week that lost five as a
    zero-height bar would say "gained nothing", which is a different and
    happier fact -- the same objection the peaks chart raises against drawing a
    day off as a zero. So the baseline leaves the floor when it has to.

    Both ends are rounded onto one shared whole step, which is what puts a
    gridline exactly on zero rather than near it.
    """
    high = max(list(values) + [0])
    low = min(list(values) + [0])
    _, step = nice_axis(high)
    if low < 0:
        _, down_step = nice_axis(-low)
        step = max(step, down_step)
    step = _whole_step(step)

    top = int(math.ceil(high / step) * step) if high > 0 else 0
    floor = -int(math.ceil(-low / step) * step) if low < 0 else 0
    # nice_axis()' courtesy, kept: never let a bar graze the ceiling or the
    # floor, where it reads as clipped rather than as measured.
    if high > 0 and top - high < step * 0.12:
        top += step
    if low < 0 and low - floor < step * 0.12:
        floor -= step
    if top == floor:            # every value was exactly zero
        top = step
    return floor, top, step


def _signed_grid(out, low, top, step, left, right, y_of):
    """_grid_lines(), but starting from a floor that may be below zero.

    Zero is drawn brighter when it is not the floor: with bars on both sides of
    it, which line is the baseline stops being obvious from position alone.
    """
    line = low
    while line <= top + step * 0.001:
        y = y_of(line)
        zero = abs(line) < step * 0.001
        out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" '
                   'stroke="{}" stroke-width="1"/>'.format(
                       left, y, right, y, DIVIDER_ZERO if (zero and low < 0) else GRID))
        out.append(text(right + 12, y + 4, fmt_count(line), size=12, fill=MUTED))
        line += step


def _tip(inner, tip):
    """A hover title on something that is not a link.

    The group charts have no day page to send anyone to -- a weekday is not a
    date -- but the count behind an average still has to be reachable, and a
    <title> needs an element to be a child of.
    """
    return '<g><title>{}</title>{}</g>'.format(esc(tip), inner)


def render_stream_bars(entries, channel, platform, day, metric,
                       width=SIZES["followers"][0], height=SIZES["followers"][1],
                       known=None, span_label=None, where=None):
    """One bar per broadcast: followers gained, or likes peaked.

    `entries` is store.stream_trends()' list, oldest first. `metric` picks both
    the field and the palette -- "followers" and "likes" already carry a colour
    each on every per-day panel (chart.METRICS), and reusing them is what keeps
    four Twitch panels on one page from being four identical purple charts
    distinguishable only by their headings.

    Returns None when no broadcast carries the metric, which is what makes
    these charts select their own platform: a YouTube row has no follower delta
    and a Twitch row no likes, so the wrong pairing simply never renders and no
    caller has to test a platform's name.

    The axis counts BROADCASTS. Two on one day are two bars, which is the whole
    reason this exists next to the peaks chart rather than instead of it.

    `span_label` names the window in the heading, for the caller whose rows did
    not come from a "last N broadcasts" query -- the venue-history chart draws a
    venue's whole record, and "last 41 broadcast(s)" would be a plain untruth
    about it. `where` names the venue in the subtitle, so a chart opened on its
    own still says which place it is about. Both default None and keep the
    existing wording byte for byte, which is what lets the four charts that
    already use this go on being compared against their fixtures.
    """
    present = [e for e in entries if e.get(metric) is not None]
    if not present:
        return None

    spec = _spec(platform)
    words = STREAM_METRICS[metric]
    # The metric's own colour first, then one it declares for itself, then the
    # platform's. Watch time is in no per-panel palette because it is in no
    # sampled series, so it brings its own.
    colour = (METRIC_BY_KEY.get(metric, {}).get("color")
              or words.get("color") or spec["color"])

    low_value, top_value, step = _signed_axis([e[metric] for e in present])
    left, right = PAD_L, width - PAD_R
    top, bottom = HEAD_H, height - FOOT_H - 16

    def y_of(value):
        span = top_value - low_value
        return bottom - ((value - low_value) / span) * (bottom - top)

    dated = [e["day"] for e in entries]
    with_month = spans_months(dated)
    out = _open_svg(width, height,
                    "{}, {}".format(
                        words["title"],
                        span_label or "last {} broadcast(s)".format(len(present))),
                    "{} · {} · {}to {}".format(
                        channel, spec["label"],
                        "{} · ".format(where) if where else "",
                        day.strftime("%a %-d %b %Y")))

    best = max(present, key=lambda e: e[metric])
    average = sum(e[metric] for e in present) / len(present)
    tile_x = width - PAD_R + 84
    # The venue names the best broadcast when a chart spans several; when the
    # whole chart is one venue it names nothing, so the date does the job it was
    # brought in for -- saying WHICH broadcast this was.
    _tile(out, tile_x, _fmt_value(metric, best[metric]), words["best"],
          (fmt_day(best["day"], True) if where
           else best.get("location") or fmt_day(best["day"], True)),
          colour=colour)
    # The span, because a broadcast axis is not contiguous and the reader
    # cannot infer how long ten of them took from the labels.
    _tile(out, tile_x - 175, _fmt_mean(metric, average), words["average"],
          "over {} broadcast(s)".format(len(present)))

    _signed_grid(out, low_value, top_value, step, left, right, y_of)
    base = y_of(0)

    slot_width = (right - left) / len(entries)
    bar_width = slot_width * (1 - GROUP_GAP)
    newest = entries[-1] if entries else None
    # A venue's whole record can be a hundred broadcasts, and a hundred bars on
    # a 1300px axis leave thirteen pixels each -- not room for a date, let alone
    # a venue and a figure stacked under it. Label every `stride`th bar, where
    # the stride is however many bars it takes to clear LABEL_WIDTH.
    #
    # One rule for all three lines rather than three thresholds: a bar is either
    # labelled or it is not, and a chart where the dates thin at one rate and the
    # figures at another reads as two charts overlaid. stride == 1 is every bar
    # labelled, which is every chart already on the site.
    stride = max(1, math.ceil(LABEL_WIDTH / slot_width)) if slot_width else 1
    crowded = stride > 1
    for index, entry in enumerate(entries):
        centre = left + slot_width * (index + 0.5)
        x = centre - bar_width / 2
        latest = entry is newest
        when = entry.get("started")
        clock = (when.astimezone().strftime("%-I:%M %p").lower() if when else "")
        # Two bars can share a date, so the date alone is not a label. The
        # location says which of the two this was; the clock does when there is
        # no location to say it with.
        #
        # `where` means the whole chart is already about one venue -- it is in
        # the subtitle -- so repeating it under every bar says nothing and
        # crowds out the thing that does distinguish two visits on one day.
        second = (clock if where else entry.get("location") or clock)
        # Counted from the NEWEST bar rather than the oldest, so the right-hand
        # end is always labelled and the ragged gap, if the count does not divide,
        # falls at the left where the oldest visit is. The newest is the one a
        # reader looks for first.
        dated_here = (len(entries) - 1 - index) % stride == 0
        if dated_here:
            out.append(text(centre, bottom + 22, fmt_day(entry["day"], with_month),
                            size=12, fill=FG if latest else MUTED,
                            anchor="middle"))
        if second and not crowded:
            out.append(text(centre, bottom + 38, second[:18], size=10,
                            fill=MUTED if latest else DIM, anchor="middle"))

        value = entry.get(metric)
        if value is None:
            # A dash on the baseline, not a zero-height bar. The broadcast
            # happened; the number was never sampled, and 0 is a real reading.
            out.append(text(centre, base - 10, DASH, size=13, fill=DIM,
                            anchor="middle"))
            continue

        y = y_of(max(value, 0))
        depth = abs(base - y_of(value))
        href = day_link(entry["day"], known)
        if href:
            out.append(_open_link(href, "{}{} · {} {}{}".format(
                fmt_day(entry["day"], True),
                " · " + entry["location"] if entry.get("location") else
                (" · " + clock if clock else ""),
                _fmt_value(metric, value), words["noun"],
                # Only watch time has a coverage figure, and only when it is
                # short of the whole broadcast. It belongs in the tooltip rather
                # than under the bar: it qualifies the number for anyone who
                # goes looking, without putting an asterisk on a healthy chart.
                _coverage_note(entry) if words.get("hours") else "")))
            out.append(_hit(x, top, bar_width, bottom - top + 44))
        out.append(_bar(x, y, bar_width, depth, colour,
                        1.0 if latest else 0.72, outline=FG if latest else None))
        # Above the bar when it grows, below when it shrinks -- a label inside
        # the axis either way. On a crowded axis only the newest keeps its
        # figure; the rest are in the tooltips, which is the bargain
        # render_watch_rolling() already strikes by drawing no bar per day.
        if dated_here:
            label_y = y - 9 if value >= 0 else y_of(value) + 18
            out.append(text(centre, label_y, _fmt_value(metric, value), size=12,
                            fill=FG if latest else MUTED,
                            weight="600" if latest else "normal", anchor="middle"))
        if href:
            out.append("</a>")

    out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
               'stroke-width="1"/>'.format(left, base, right, base, GRID))
    out.append(text(right, height - 16, fmt_span(dated), size=12, fill=DIM,
                    anchor="end"))
    out.append("</svg>")
    return "\n".join(out)


# --------------------------------------------------------------------------
# which day, and which place
# --------------------------------------------------------------------------


def render_stream_groups(groups, channel, platform, day, metric, grouping,
                         width=SIZES["weekday"][0], height=SIZES["weekday"][1],
                         count=STREAM_COUNT, span_label=None, links=None,
                         highlight=None, baseline=None, where=None):
    """Average per broadcast, grouped by weekday or by location.

    `groups` is store.stream_groups()' list and is SPARSE -- a weekday nobody
    streamed on is simply absent. The axis is built here: Mon..Sun in order for
    a weekday, and busiest-first for a location, so the answer to "where is
    worth it" is the leftmost bar.

    A group with no broadcasts is drawn as a dash rather than a zero bar, for
    the reason a day off is on the peaks chart: nothing happened is not the
    same reading as nothing was gained.

    `span_label` names the window in the subtitle, for the caller whose rows did
    not come from a "last N broadcasts" query -- store.location_watch() is
    all-time, and "last 41 of them" would be a plain untruth about it. Default
    None keeps the existing wording byte for byte, which is what lets the two
    charts that already use this go on being compared against their fixtures.

    Coverage, when the rows carry it, is appended to the subtitle rather than
    printed per bar: a venue's bars are already labelled, and the one thing a
    reader needs before trusting the height of all of them is what share of the
    broadcasts behind them a poller actually saw.

    `links` maps a group key to an href and turns that bar into an anchor. Only
    the location grouping is ever given one, and the comment further down says
    why a weekday still is not.

    `highlight` is the one group key this drawing is *about* -- the venue whose
    page the chart is on. It dims the others rather than recolouring itself, and
    it deliberately leaves the best-bar outline alone: brightness answers "which
    one am I looking at" and the outline answers "which one won", so a venue
    that is both still reads as both. Recolouring would have collapsed the two
    questions into one mark.

    `where` names the venue this rollup is restricted to, so a chart opened on
    its own says which place it is about rather than reading as the whole
    channel's. It is NOT passed to the venue-comparison chart, which is about
    every venue by construction.

    `baseline` draws a dashed reference line at a value, the way
    render_watch_rolling() draws the YPP line. On a location page it is the
    channel's own average across every venue, which is what turns "4.2 hours"
    into "above where this channel usually lands" -- a bar on its own cannot say
    that, and neither can a page of bars all drawn to their own axis.
    """
    if not groups:
        return None

    spec = _spec(platform)
    words = STREAM_METRICS[metric]
    shape = GROUPINGS[grouping]
    colour = (METRIC_BY_KEY.get(metric, {}).get("color")
              or words.get("color") or spec["color"])
    found = {row["key"]: row for row in groups}

    if grouping == "weekday":
        # Every weekday, in order, whether or not it was streamed -- the point
        # of the chart is partly which days are missing.
        axis = [(str(n), name) for n, name in enumerate(WEEKDAYS)]
    else:
        axis = [(row["key"], row["key"] or UNKNOWN_LOCATION)
                for row in sorted(groups, key=lambda r: r["average"], reverse=True)]

    drawn = [found[key] for key, _ in axis if key in found]
    if not drawn:
        return None

    low_value, top_value, step = _signed_axis([row["average"] for row in drawn])
    left, right = PAD_L, width - PAD_R
    top, bottom = HEAD_H, height - FOOT_H - 16

    def y_of(value):
        span = top_value - low_value
        return bottom - ((value - low_value) / span) * (bottom - top)

    streams = sum(row["streams"] for row in drawn)
    # Weighted by broadcast rather than averaged over the groups: a venue with
    # thirty streams and one with two should not have equal say in the caveat.
    covered = sum(row.get("covered") or 0 for row in drawn)
    spanned = sum(row.get("span") or 0 for row in drawn)
    out = _open_svg(width, height,
                    shape["title"].format(words["title"].replace(" per stream", "")),
                    "{} · {} · {}average per broadcast, {}{}".format(
                        channel, spec["label"],
                        "{} · ".format(where) if where else "",
                        span_label or "last {} of them".format(streams),
                        _coverage_note({"coverage": covered / float(spanned)
                                        if spanned else None},
                                       noun="broadcasts")))

    best = max(drawn, key=lambda row: row["average"])
    best_label = dict(axis).get(best["key"], best["key"] or UNKNOWN_LOCATION)
    tile_x = width - PAD_R + 84
    _tile(out, tile_x, _fmt_mean(metric, best["average"]), shape["best"],
          "{} · {} broadcast(s)".format(best_label, best["streams"]), colour=colour)
    _tile(out, tile_x - 175, str(len(drawn)),
          "Groups" if grouping == "location" else "Days streamed",
          "of {} broadcast(s)".format(streams))

    _signed_grid(out, low_value, top_value, step, left, right, y_of)
    base = y_of(0)

    slot_width = (right - left) / len(axis)
    bar_width = slot_width * (1 - GROUP_GAP)
    for index, (key, label) in enumerate(axis):
        centre = left + slot_width * (index + 0.5)
        x = centre - bar_width / 2
        row = found.get(key)
        out.append(text(centre, bottom + 22, label[:18], size=12,
                        fill=MUTED if row else DIM, anchor="middle"))
        if row is None:
            out.append(text(centre, base - 10, DASH, size=13, fill=DIM,
                            anchor="middle"))
            continue
        out.append(text(centre, bottom + 38,
                        "{} stream(s)".format(row["streams"]), size=10,
                        fill=DIM, anchor="middle"))
        value = row["average"]
        y = y_of(max(value, 0))
        depth = abs(base - y_of(value))
        top_bar = row is best
        here = highlight is not None and key == highlight
        # A weekday is still not a date and still has no page, so it is never
        # given a link -- enforced here rather than left to the caller, because
        # the key for Saturday is "5" and a map that happened to hold "5" would
        # otherwise send a reader somewhere arbitrary. A location now does have
        # a page, which is what `links` carries. The count and the best single
        # broadcast stay in the tooltip either way, because that is what stops
        # an average being read as a certainty.
        href = (links or {}).get(key) if grouping == "location" else None
        tip = "{} · {} broadcast(s) · {} avg · best {}".format(
            label, row["streams"], _fmt_mean(metric, value),
            _fmt_value(metric, row["best"]))
        if href:
            out.append(_open_link(href, tip))
            out.append(_hit(x, top, bar_width, bottom - top + 44))
        # Brightness says "you are here", the outline says "this one won". When
        # nothing is highlighted the first term is the original 1.0/0.72 and
        # every existing chart is unchanged.
        if highlight is None:
            opacity = 1.0 if top_bar else 0.72
        else:
            opacity = 1.0 if here else 0.34
        bar = _bar(x, y, bar_width, depth, colour, opacity,
                   outline=FG if top_bar else None)
        out.append(bar if href else _tip(bar, tip))
        label_y = y - 9 if value >= 0 else y_of(value) + 18
        lit = here if highlight is not None else top_bar
        out.append(text(centre, label_y, _fmt_mean(metric, value), size=12,
                        fill=FG if lit else MUTED,
                        weight="600" if lit else "normal", anchor="middle"))
        if href:
            out.append("</a>")

    # After the bars, not before: a reference the bars drew over would be a
    # reference nobody could read against the one bar it matters most for.
    # Clamped to the axis for render_watch_rolling()'s reason -- a line off the
    # top would stretch nothing and say less than the number in the label.
    if baseline is not None and low_value <= baseline <= top_value:
        y = y_of(baseline)
        out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" '
                   'stroke="{}" stroke-width="1.4" stroke-dasharray="6 4" '
                   'opacity="0.8"/>'.format(left, y, right, y, FG))
        # Right-aligned, unlike render_watch_rolling()'s, and the axis ordering
        # is why: these bars are sorted busiest-first, so the tallest is always
        # at the left and the right end is the one place a label is not over a
        # bar. On a line chart the left is the safe end; here it is the worst.
        out.append(text(right - 6, y - 7,
                        "{} across every location".format(
                            _fmt_mean(metric, baseline)),
                        size=11, fill=DIM, anchor="end"))

    out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
               'stroke-width="1"/>'.format(left, base, right, base, GRID))
    out.append("</svg>")
    return "\n".join(out)


# --------------------------------------------------------------------------
# the trailing total
# --------------------------------------------------------------------------


def render_watch_rolling(rows, channel, platform, day,
                         width=SIZES["watchrolling"][0],
                         height=SIZES["watchrolling"][1],
                         rolling=ROLLING_DAYS, target=None):
    """Estimated watch hours over the trailing `rolling` days, at each day of the window.

    `rows` is store.watch_totals()' list, oldest first and DENSE -- a day nobody
    streamed is present with a None watch time, because the trailing total still
    moves on it as an older day falls out of the back of the window. That is the
    whole reason this is a line and not a bar per broadcast: it answers "which
    way is the twelve-month figure going", which no per-stream chart can.

    `target` draws a dashed reference line, and is the YouTube Partner Programme's
    4,000 hours when the caller passes it. It is a REFERENCE and not a goal line,
    and the footer says so: the YPP figure counts valid public watch hours across
    live and VOD, where this integrates the live concurrent-viewer curve alone.
    Anyone reading this chart as progress towards monetisation is reading a number
    that is low by a margin nothing here can measure. Passing target=None -- which
    is what Twitch gets, having no such threshold -- simply omits the line.

    Returns None when no day in the window has a trailing total, which is what
    keeps a channel with no watch history from publishing an empty chart.
    """
    present = [r for r in rows if r.get("rolling") is not None]
    if not present:
        return None

    spec = _spec(platform)
    left, right = PAD_L, width - PAD_R
    top, bottom = HEAD_H, height - FOOT_H - 16

    highest = max(r["rolling"] for r in present)
    # The reference line is only worth an axis that reaches it when it is within
    # sight. On a channel three times past it, stretching the axis to include it
    # would flatten the curve the chart exists to show; on one approaching it,
    # leaving it off the top would hide the only thing being approached.
    reach = max(highest, target) if target and target <= highest * 3 else highest
    _, step = nice_axis(reach)
    # _whole_step() for _grid_lines()' reason, which only ever bit the viewer
    # charts in theory: the labels are drawn with fmt_count(), so a fractional
    # step prints "0, 0, 0" for gridlines at 0, 0.2 and 0.4. A viewer count is
    # never small enough to produce one; a channel three days into collecting is
    # very easily under one watch hour.
    step = _whole_step(step)
    top_value = max(step, int(math.ceil(reach / step) * step))

    def x_of(index):
        return left + (index + 0.5) * ((right - left) / max(1, len(rows)))

    def y_of(value):
        return bottom - (value / top_value) * (bottom - top) if top_value else bottom

    latest = present[-1]
    streamed = sum(1 for r in rows if r.get("watch_minutes") is not None)
    out = _open_svg(
        width, height,
        "Estimated watch hours, trailing {} days".format(rolling),
        "{} · {} · live only, excludes replay watch time".format(
            channel, spec["label"]))

    tile_x = width - PAD_R + 84
    _tile(out, tile_x, fmt_watch_hours(latest["rolling"]),
          "Past {} days".format(rolling),
          "to {}".format(fmt_day(latest["day"], True)), colour=WATCH_COLOR)
    _tile(out, tile_x - 175, str(streamed), "Days with watch time",
          "of {} in view".format(len(rows)))
    if target:
        share = latest["rolling"] / float(target) * 100.0
        _tile(out, tile_x - 350, "{:.0f}%".format(share),
              "Of the {} reference".format(fmt_count(target)),
              "estimate, not the YPP figure")

    _grid_lines(out, top_value, step, left, right, y_of)

    if target and target <= top_value:
        y = y_of(target)
        out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" '
                   'stroke="{}" stroke-width="1.4" stroke-dasharray="6 4" '
                   'opacity="0.8"/>'.format(left, y, right, y, FG))
        out.append(text(left + 6, y - 7, "{} h reference".format(fmt_count(target)),
                        size=11, fill=DIM))

    # One run, not several: the trailing total is defined on every day the report
    # tables hold, including the ones with no stream, so there is no gap to keep.
    points = [(x_of(index), y_of(row["rolling"]))
              for index, row in enumerate(rows) if row.get("rolling") is not None]
    coords = " ".join("{:.1f},{:.1f}".format(x, y) for x, y in points)
    if len(points) > 1:
        out.append('<path d="M {:.1f},{:.1f} L {} L {:.1f},{:.1f} Z" fill="{}" '
                   'opacity="0.16"/>'.format(points[0][0], bottom,
                                             coords.replace(" ", " L "),
                                             points[-1][0], bottom, WATCH_COLOR))
        out.append('<polyline points="{}" fill="none" stroke="{}" stroke-width="2.2" '
                   'stroke-linejoin="round" stroke-linecap="round"/>'.format(
                       coords, WATCH_COLOR))
    else:
        out.append('<circle cx="{:.1f}" cy="{:.1f}" r="3" fill="{}"/>'.format(
            points[0][0], points[0][1], WATCH_COLOR))

    # A marker on the newest point, which is the one figure anybody came for.
    out.append('<circle cx="{:.1f}" cy="{:.1f}" r="4" fill="{}" stroke="{}" '
               'stroke-width="2"/>'.format(points[-1][0], points[-1][1], BG,
                                           WATCH_COLOR))

    dated = [r["day"] for r in rows]
    with_month = spans_months(dated)
    # Every label would collide on a 365-day window, so they thin to about a
    # dozen. The count and not the stride is fixed, so the axis reads the same
    # whether it is showing a fortnight or a year.
    stride = max(1, len(rows) // 12)
    for index, row in enumerate(rows):
        if index % stride and index != len(rows) - 1:
            continue
        out.append(text(x_of(index), bottom + 22, fmt_day(row["day"], with_month),
                        size=11, fill=MUTED, anchor="middle"))

    # Each day's own contribution, reachable but not drawn: on this axis a single
    # day is a rounding error against a year of them, and a bar for it would be
    # a pixel. The per-broadcast chart is where a day is legible.
    for index, row in enumerate(rows):
        if row.get("rolling") is None:
            continue
        own = row.get("watchtime")
        out.append(_tip(_hit(x_of(index) - 8, top, 16, bottom - top),
                        "{} · {} trailing · {} that day".format(
                            fmt_day(row["day"], True),
                            fmt_watch_hours(row["rolling"]),
                            fmt_watch_hours(own) if own is not None
                            else "no stream")))

    out.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" '
               'stroke-width="1"/>'.format(left, bottom, right, bottom, GRID))
    out.append(text(left, height - 16,
                    "Area under the concurrent-viewer curve. Not the platform's "
                    "own figure, and not comparable with it.",
                    size=11, fill=DIM))
    out.append(text(right, height - 16, fmt_span(dated), size=12, fill=DIM,
                    anchor="end"))
    out.append("</svg>")
    return "\n".join(out)


# --------------------------------------------------------------------------
# both, for one platform
# --------------------------------------------------------------------------


def render_all(samples, channel, platform, day, peak_days=PEAK_DAYS,
               compare_days=COMPARE_DAYS, minutes=BUCKET_MINUTES, calendar=False,
               lookback=LOOKBACK_DAYS, known=None):
    """{"peaks": svg, "typical": svg} for one platform; either key may be absent.

    A platform with nothing in the window produces an empty dict rather than an
    error — the same rule the daily report already follows for a channel that
    only streams on one of them.
    """
    minutes = bucket_width(minutes)   # keeps the labels and the buckets agreeing
    slots, per_day, dropped = compare_slots(samples, day, compare_days, minutes,
                                            calendar=calendar, lookback=lookback)
    return render_from(daily_peaks(samples, day, peak_days, calendar=calendar,
                                   lookback=lookback),
                       slots, per_day, channel, platform, day,
                       minutes=minutes, dropped=dropped, known=known)


def render_from(peaks, slots, per_day, channel, platform, day,
                minutes=BUCKET_MINUTES, dropped=0, known=None):
    """The same charts, from aggregates somebody else worked out.

    Everything render_all() does except the arithmetic, so the daily report can
    hand over what the report tables already hold instead of re-deriving it
    from every sample the channel has ever produced. The arguments are exactly
    daily_peaks()' return value and compare_slots()' three, whichever side of
    the database they were computed on -- which is what makes the two paths
    comparable in a test rather than merely alike. `known` goes to both charts
    for the same reason: the two paths have to agree about what is a link.
    """
    minutes = bucket_width(minutes)
    charts = {}
    drawn = render_peaks(peaks, channel, platform, day, known=known)
    if drawn:
        charts["peaks"] = drawn
    drawn = render_typical(slots, per_day, channel, platform, day,
                           minutes=minutes, dropped=dropped, known=known)
    if drawn:
        charts["typical"] = drawn
    return charts


def location_links(rows, known=None):
    """{group key: href} for the venue bars of a chart living under trends/.

    Built from the rows the chart is about rather than from a list of venues, so
    a bar can only ever link to a page for the venue that bar is: the key going
    in is the key coming out, and slugify() is applied in exactly one place.

    A venue with no page is simply absent, which is how render_stream_groups()
    tells a link from a plain bar -- day_link()'s rule, and for the same reason.
    """
    out = {}
    for row in rows or ():
        href = location_link(slugify(row["key"]), known)
        if href:
            out[row["key"]] = href
    return out


def render_streams(rows, groups, channel, platform, day, known=None, watch=(),
                   location_watch=(), known_locations=None):
    """The per-broadcast charts: {"peakstream", "followers", "likes", "weekday",
    "location", "peaklocation", "watchtime", "watchrolling", "watchlocation"}.

    Any key may be absent, and on a normal channel most of them are: `rows` is
    one platform's broadcasts, so the followers chart draws for Twitch and the
    likes chart for YouTube and each returns None for the other. Nothing here
    tests a platform's name -- the absent metric is absent from the data.

    `groups` is {"weekday": [...], "location": [...]} as store.stream_groups()
    returns them, and both roll up FOLLOWERS: "which day was worth it" is a
    question about the audience you keep, and likes are a YouTube-only measure
    of the one you had. Passing a different metric is a one-line change here
    rather than a new renderer.

    `watch` is store.watch_totals()' list, and adds the trailing-total chart when
    it is passed. Optional because it comes from a different query than `rows`
    and a caller that could not run it should still get the rest.

    `location_watch` is store.location_watch()' list and adds the watch-hours-by-
    venue chart, optional for the same reason. It draws through the same renderer
    as the followers-by-location chart, with the metric and the window being the
    only difference -- which is what STREAM_METRICS and GROUPINGS being data
    rather than renderers buys.

    `known_locations` is the set of venue slugs the bucket holds a page for, and
    turns every by-location bar into a link to that venue's page. None means no
    page exists and every bar stays plain -- the OPPOSITE of `known`'s None,
    which means "link every day". The asymmetry is deliberate: a day page can be
    inferred to exist from the date having data, and a location page cannot, so
    the safe default differs. Getting this backwards produces a page of 404s.

    Deliberately not folded into render_from(). That function's signature is
    load-bearing -- the parity harness drives the Python and SQL aggregate
    paths through it and compares the SVG byte for byte -- and these aggregates
    have no Python twin to be compared against, because there is no CSV path
    that could produce them.
    """
    charts = {}
    for metric, words in STREAM_METRICS.items():
        drawn = render_stream_bars(rows, channel, platform, day, metric,
                                   known=known)
        if drawn:
            # A metric may name its chart something other than itself; see
            # STREAM_METRICS["peak"], which must not collide with "peaks".
            charts[words.get("kind", metric)] = drawn
    for grouping in GROUPINGS:
        rolled = groups.get(grouping) or []
        drawn = render_stream_groups(
            rolled, channel, platform, day, "followers", grouping,
            links=location_links(rolled, known_locations))
        if drawn:
            charts[grouping] = drawn
    # Peak viewers by venue, which is the chart the location pages are most
    # often reached from: "where do most people watch me" is the question a
    # reader has before "and what happened on each visit".
    peaks_by_place = groups.get("peaklocation") or []
    if peaks_by_place:
        drawn = render_stream_groups(
            peaks_by_place, channel, platform, day, "peak", "location",
            width=SIZES["peaklocation"][0], height=SIZES["peaklocation"][1],
            links=location_links(peaks_by_place, known_locations))
        if drawn:
            charts["peaklocation"] = drawn
    if watch:
        # Only YouTube gets the reference line. Twitch has no watch-hour
        # threshold to be near, and drawing one there would invent a target the
        # platform does not have.
        drawn = render_watch_rolling(
            watch, channel, platform, day,
            target=YPP_TARGET_HOURS if platform == "youtube" else None)
        if drawn:
            charts["watchrolling"] = drawn
    if location_watch:
        # All-time, so it says so: this is the one chart on the page whose window
        # is not a flag, and a subtitle claiming "last N" would be wrong.
        drawn = render_stream_groups(
            location_watch, channel, platform, day, "watchtime", "location",
            width=SIZES["watchlocation"][0], height=SIZES["watchlocation"][1],
            span_label="all {} on record".format(
                sum(row["streams"] for row in location_watch)),
            links=location_links(location_watch, known_locations))
        if drawn:
            charts["watchlocation"] = drawn
    return charts


# --------------------------------------------------------------------------
# one venue's page
# --------------------------------------------------------------------------

# The charts a location page carries, in the order it shows them. Each is one
# lowercase word, because s3.location_chart_key() puts it in front of a hyphen
# and the key regex will not take a second one.
#
# The order is the reading order the front page already uses: how many watched,
# then how many stayed, then when and against what. "compare" is last of the
# rollups because it is the only chart on the page about somewhere else.
LOCATION_KINDS = ("peakstream", "followers", "likes", "watchtime",
                  "history", "weekday", "compare")


def render_location(rows, history, weekday, compare, channel, platform, day,
                    location, label=None, known=None, known_locations=None):
    """Every chart for one venue: a subset of LOCATION_KINDS.

    The sibling of render_streams(), and deliberately a separate function for
    the reason that one is separate from render_from(): a venue's charts have no
    Python twin to be held to a parity harness, and folding them in would put a
    load-bearing signature under pressure for no gain.

    Any key may be absent, and on a normal channel most are. `rows` is one
    platform's broadcasts AT THIS VENUE, so the followers chart draws for Twitch
    and the likes chart for YouTube and each returns None for the other --
    nothing here tests a platform's name, exactly as in render_streams().

    `rows` is store.stream_trends(location=...) -- the venue's last N -- and
    `history` is store.location_history(), the venue's whole record. Two queries
    and not one, because they answer different questions and a chart cannot tell
    which window it was handed: "the last ten visits" and "every visit ever" want
    different headings, and one of them would be a lie.

    `weekday` is store.stream_groups(grouping="weekday", location=...), which
    rolls up PEAK VIEWERS rather than followers. The channel-wide weekday chart
    asks "when is it worth going out" and answers it with the audience you keep;
    at one venue the question is "when is this place busy", and that is a
    question about the audience you get.

    `compare` is store.stream_groups(grouping="location") with NO venue filter --
    every venue, so this one can be seen among them. It is the only chart here
    drawn from rows that are not all about this place, which is the point: a bar
    on its own cannot say whether it is good.

    `location` is the venue as the DATABASE spells it -- "" for the broadcasts no
    rule matched -- and is what the compare chart highlights, because that is the
    key its rows are grouped by. `label` is what a READER should see, and
    defaults to UNKNOWN_LOCATION when `location` is empty. Two arguments and not
    one, because "" is the right thing to match on and the wrong thing to print.
    """
    label = label or location or UNKNOWN_LOCATION
    charts = {}
    for metric, words in STREAM_METRICS.items():
        drawn = render_stream_bars(rows, channel, platform, day, metric,
                                   known=known, where=label)
        if drawn:
            charts[words.get("kind", metric)] = drawn

    if history:
        # All-time, so the heading says so rather than claiming a window this
        # does not have. "On record" and not "ever": these are the broadcasts the
        # report tables hold, which for a channel imported before 007 is whatever
        # db --rebuild refreshed. The same phrasing the by-venue watch chart uses,
        # and for the same reason.
        drawn = render_stream_bars(
            history, channel, platform, day, "watchtime", known=known,
            width=SIZES["history"][0], height=SIZES["history"][1],
            span_label="all {} on record".format(len(history)),
            where=label)
        if drawn:
            charts["history"] = drawn

    if weekday:
        drawn = render_stream_groups(
            weekday, channel, platform, day, "peak", "weekday",
            width=SIZES["weekday"][0], height=SIZES["weekday"][1],
            where=label)
        if drawn:
            charts["weekday"] = drawn

    if compare:
        # Broadcast-weighted, NOT the mean of the venue averages. A venue with
        # thirty broadcasts and one with two must not have equal say in where the
        # line sits -- the same argument render_stream_groups() already makes
        # about its coverage note, and an easy one to get wrong by writing the
        # shorter expression.
        streams = sum(row["streams"] for row in compare)
        baseline = (sum(row["total"] for row in compare) / streams
                    if streams else None)
        drawn = render_stream_groups(
            compare, channel, platform, day, "peak", "location",
            width=SIZES["compare"][0], height=SIZES["compare"][1],
            highlight=location or "", baseline=baseline,
            # Siblings share a directory with this venue's page, so the link out
            # of a chart under location/ climbs one level to reach them -- the
            # same "../" every other chart on the site uses, because every chart
            # on the site lives exactly one level down.
            links=location_links(compare, known_locations))
        if drawn:
            charts["compare"] = drawn
    return charts
