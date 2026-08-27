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

from datetime import time, timedelta

from .chart import (BG, DIM, FG, GRID, MUTED, PAD_L, PAD_R, PLATFORMS,
                    esc, fmt_count, nice_axis, text)

PEAK_DAYS = 10          # days on the peaks chart
COMPARE_DAYS = 5        # days shown *behind* today on the comparison chart
BUCKET_MINUTES = 30

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

# Where a bar sends the reader. Relative, and one level up, because the browser
# resolves it against the SVG's own URL -- these charts live under trends/.
DAY_PREFIX = "day/"

# The size each chart renders at, in one place because the page needs it too:
# an <object> has to be told its aspect ratio, where an <img> works it out.
SIZES = {"peaks": (1300, 380), "typical": (1300, 430)}

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
