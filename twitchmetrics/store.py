"""Samples, in the database rather than in a file.

storage.py still owns the CSVs, and still parses them exactly as it always has.
This module owns the same samples in Postgres: the mapping from a CSV row to a
function call, the read that gives them back in the shape read_samples() returns,
and the import that moves an archive across.

The one idea worth stating before the code: read_samples() already normalises
three different CSV headers into a single dict, and every chart in this project
consumes that dict rather than a file. So the target is not "a table shaped like
a CSV" -- it is that same dict, which is why read() below can be swapped in
underneath chart.py and trends.py without either of them noticing.
"""

import os
from datetime import datetime, timedelta, timezone

from . import config, db, storage, trends

# trends is imported for its day axis, its slot width and its constants --
# never for its arithmetic. Python stays the definition of what a window is
# and the database stays the thing that fills one. No cycle: trends imports
# chart, chart imports config, and neither imports this module.

# Which poller a data/ filename belongs to. metrics_ is listed before viewers_
# because it is the superset; the merging upsert in record_twitch_sample() makes
# the order not matter, but the order still records which file is the better one.
PREFIXES = (("metrics_", "twitch"), ("viewers_", "twitch"), ("youtube_", "youtube"))

# CSV field name -> the record_*_sample parameter it feeds.
#
# The three headers in storage.py are the only description of a sample this
# project has ever had, and read_samples() already knows this mapping under
# other names. Writing it down once, keyed by FIELD NAME rather than by
# position, means all three formats arrive here identically -- and a header
# widened later by storage._widen() needs no change on this side.
TWITCH_FIELDS = {
    "viewer_count":   "viewer_count",
    "follower_count": "follower_count",
    "chatter_count":  "chatter_count",
}
YOUTUBE_FIELDS = {
    "viewer_count":     "viewer_count",
    "subscriber_count": "subscriber_count",
    "like_count":       "like_count",
}


class Skipped(Exception):
    """A row that cannot be stored, for the same reasons read_samples() drops it."""


def parse_stamp(raw):
    """The CSV's timestamp format, or None. storage.utc_stamp()'s inverse."""
    try:
        return datetime.strptime(str(raw).strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _int(raw):
    raw = "" if raw is None else str(raw).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _text(raw):
    return ("" if raw is None else str(raw).strip()) or None


def source_of(path):
    """(platform, channel_slug, ingest_source) for a data/ filename.

    The SLUG is taken from the filename, not lowercased from a login, because
    that is what the filename already is -- config.channel_slug() produced it.
    It is also why IGN and ign have always shared one file, and why keying the
    database on a raw login would quietly make them two channels.
    """
    stem = os.path.basename(path)
    if stem.endswith(".csv"):
        stem = stem[:-4]
    for prefix, platform in PREFIXES:
        if stem.startswith(prefix):
            return platform, stem[len(prefix):], prefix.rstrip("_") + "_csv"
    raise Skipped("{} is not a metrics_, viewers_ or youtube_ file".format(
        os.path.basename(path)))


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def account_id(slug, platform, display_name=None, timezone_name=None, create=True):
    """The account row's id, creating the channel and account if asked.

    Called once at poller startup rather than per sample, which is the point of
    platform_account: it replaces the API call both pollers currently make on
    every restart to re-resolve an id that never changes.
    """
    found = db.execute("SELECT tm.account_for(%s, %s)", (slug, platform), fetch=True)
    if found and found[0][0] is not None:
        return found[0][0]
    if not create:
        return None
    channel = db.execute(
        "SELECT tm.upsert_channel(%s, %s, %s)",
        (slug, display_name or slug, timezone_name or config.resolve_db_timezone()),
        fetch=True)[0][0]
    return db.execute("SELECT tm.upsert_account(%s, %s, %s)",
                      (channel, platform, slug), fetch=True)[0][0]


TWITCH_CALL = """
SELECT tm.record_twitch_sample(
    %(account_id)s, %(sampled_at)s, %(stream_id)s, %(viewer_count)s,
    %(follower_count)s, %(chatter_count)s, %(title)s, %(game)s,
    %(started_at)s, %(source)s)
"""

YOUTUBE_CALL = """
SELECT tm.record_youtube_sample(
    %(account_id)s, %(sampled_at)s, %(stream_id)s, %(viewer_count)s,
    %(subscriber_count)s, %(like_count)s, %(title)s, %(started_at)s, %(source)s)
"""


def row_params(platform, account, fields, source="poller"):
    """Call parameters from a row spelled the way the CSV spells it.

    An offline row keeps its follower, chatter and subscriber counts and loses
    only the stream: those numbers go on moving while a channel is dark, which
    is why the pollers record them either way, and dropping them here would lose
    every follower gained on a day off.
    """
    when = parse_stamp(fields.get("timestamp_utc"))
    if when is None:
        # Exactly what read_samples() does with an unparseable timestamp: there
        # is nothing to key the row on, so there is nothing to store.
        raise Skipped("unparseable timestamp {!r}".format(fields.get("timestamp_utc")))

    live = (fields.get("is_live") or "").strip().lower() == "true"
    # video_id is YouTube's name for the same thing, aliased here the way
    # read_samples() aliases it.
    stream = _text(fields.get("stream_id") or fields.get("video_id")) if live else None

    params = {
        "account_id": account,
        "sampled_at": when,
        "stream_id": stream,
        "title": _text(fields.get("title")) if live else None,
        "started_at": parse_stamp(fields.get("started_at")) if live else None,
        "source": source,
    }
    names = YOUTUBE_FIELDS if platform == "youtube" else TWITCH_FIELDS
    for csv_name, param in names.items():
        params[param] = _int(fields.get(csv_name))
    # A viewer count only means anything on a live row, and the schema refuses
    # one without a stream -- the same rule read_samples() applies when it sets
    # viewers to None for an offline sample.
    if stream is None:
        params["viewer_count"] = None
    if platform == "twitch":
        params["game"] = _text(fields.get("game")) if live else None
    return params


def call_for(platform):
    return YOUTUBE_CALL if platform == "youtube" else TWITCH_CALL


def record(platform, account, fields, source="poller", refresh=False):
    """Store one sample. Returns the stream id it was attached to, or None.

    `refresh` also rolls the report tables forward for the day the sample landed
    in. Off by default because a bulk import would then recompute the same day
    once per row; the poller turns it on, which is what keeps the website's
    aggregates current between daily runs instead of a day behind.
    """
    params = row_params(platform, account, fields, source)
    stream_id = db.execute(call_for(platform), params, fetch=True)[0][0]
    if refresh:
        db.execute("SELECT tm.refresh_after_sample(%s, %s, 30, %s)",
                   (account, params["sampled_at"], stream_id))
    return stream_id


def refresh_reports(account, sampled_at, stream_id=None):
    """Roll the report tables forward, swallowing its own failures.

    Separate from record() and deliberately quiet: the sample is already
    committed and is the thing that cannot be recreated, while a report row can
    be rebuilt at any time by `db --import` or a `daily` run. A refresh that
    fails must not turn a good sample into a lost one.
    """
    try:
        db.execute("SELECT tm.refresh_after_sample(%s, %s, 30, %s)",
                   (account, sampled_at, stream_id))
    except (db.Unreachable, db.NotConfigured, SystemExit):
        pass


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

READ_SQL = """
SELECT s.sampled_at, s.is_live, s.viewer_count, s.follower_count, s.chatter_count,
       s.subscriber_count, s.like_count, st.title, st.game, st.platform_stream_id
  FROM tm.sample_all s
  JOIN tm.platform_account a ON a.account_id = s.account_id
  JOIN tm.channel c          ON c.channel_id = a.channel_id
  LEFT JOIN tm.stream st     ON st.stream_id = s.stream_id
 WHERE c.slug = %s AND s.platform = %s
 ORDER BY s.sampled_at
"""


def read(slug, platform):
    """Every sample for one channel on one platform, shaped like read_samples().

    Deliberately the whole history rather than a window: the trend charts want
    it, chart.split_sessions() wants it, and the largest archive here is 167 KB.
    A LIMIT would be an optimisation nothing asked for and a source of charts
    quietly missing their first day.
    """
    rows = db.execute(READ_SQL, (slug, platform), fetch=True)
    return [_to_sample(row) for row in rows]


def _to_sample(row):
    """One row as the dict read_samples() would have built.

    Three of its quirks are reproduced here deliberately, because the charts
    depend on them:

    `when` is forced to UTC. read_samples() builds it with tzinfo=utc and
    psycopg builds it in the session's zone; everything downstream calls
    .astimezone() so the two behave alike, but `db --verify` compares them and a
    chart that moved because the server's TimeZone changed would be a long
    afternoon.

    `viewers` is None whenever the row is not live, which chart.runs_of() relies
    on to break the viewer line across a downtime instead of drawing a flat
    segment through it as though it were data.

    `title`, `game` and `stream_id` are "" and never None, because a CSV cannot
    tell those apart and every caller tests them for truthiness.
    """
    (when, live, viewers, followers, chatters,
     subscribers, likes, title, game, stream_id) = row
    live = bool(live)
    return {
        "when": when.astimezone(timezone.utc),
        "live": live,
        "viewers": viewers if live else None,
        "followers": followers,
        "chatters": chatters,
        "subscribers": subscribers,
        "likes": likes,
        "title": (title or "") if live else "",
        "game": (game or "") if live else "",
        "stream_id": (stream_id or "") if live else "",
    }


# --------------------------------------------------------------------------
# importing an archive
# --------------------------------------------------------------------------


def group_archives(paths):
    """{(platform, slug): [path, ...]} -- the files that describe one account.

    A channel polled before and after the follower columns were added has both
    a viewers_ and a metrics_ file, and they are two halves of one history.
    """
    groups = {}
    for path in paths:
        try:
            platform, slug, _ = source_of(path)
        except Skipped:
            continue
        groups.setdefault((platform, slug), []).append(path)
    return groups


def import_archive(platform, slug, paths, dry_run=False, timezone_name=None):
    """Load every file describing one account, as one merged history.

    Merged rather than file by file, and that is not tidiness -- it is
    correctness. The rows go through the same record_*_sample() functions a
    poller calls, so open_or_touch_stream() reconstructs the broadcasts as it
    goes; feed it a file of August 22nd after a file of August 24th and it
    reasonably concludes the older broadcast superseded the newer one, closing
    the wrong stream and leaving the archive describing a history that never
    happened. Sorting inside one file is not enough when a channel has two.

    Read through storage.read_raw() rather than read_samples(), because
    read_samples() has never parsed started_at and the import must not be the
    reason that column arrives empty.
    """
    rows = []
    for path in paths:
        _, _, source = source_of(path)
        for fields in storage.read_raw(path):
            rows.append((str(fields.get("timestamp_utc") or ""), source, fields))
    # Stable sort on the timestamp alone, so two files holding the same second
    # keep the order the file list gave them -- metrics_ before viewers_, the
    # superset first. The merging upsert makes that a preference, not a
    # requirement, but a preference worth keeping deterministic.
    rows.sort(key=lambda entry: entry[0])

    tally = {"platform": platform, "channel": slug, "paths": paths,
             "rows": len(rows), "stored": 0, "skipped": 0}
    if dry_run:
        return tally

    account = account_id(slug, platform, display_name=slug,
                         timezone_name=timezone_name)
    for _, source, fields in rows:
        try:
            record(platform, account, fields, source=source)
        except Skipped:
            tally["skipped"] += 1
        else:
            tally["stored"] += 1
    return tally


def verify_csv(path, limit=10):
    """Compare read_samples(path) against read(). Returns the differences.

    Compares the DICTS rather than the columns, because a dict is what every
    chart actually consumes -- so an equal dict is a statement about charts and
    not merely about storage. Any other kind of confidence in a data migration
    is a feeling.
    """
    platform, slug, _ = source_of(path)
    want = storage.read_samples(path)
    got = {s["when"]: s for s in read(slug, platform)}
    differences = []
    for sample in want:
        mine = got.get(sample["when"])
        if mine is None:
            differences.append((sample["when"], "row", "present", "missing"))
            continue
        for key in sample:
            if sample[key] != mine[key]:
                differences.append((sample["when"], key, sample[key], mine[key]))
    return {"path": path, "channel": slug, "platform": platform,
            "csv_rows": len(want), "db_rows": len(got),
            "differences": differences[:limit], "total": len(differences)}


# --------------------------------------------------------------------------
# where a poller's samples go
# --------------------------------------------------------------------------

# How far back to re-read the spool, relative to the newest sample the database
# already holds. Zero would be exact -- one poller appends to one file in
# strictly increasing time order -- and the hour is for the two ways that stops
# being true: a clock stepped backwards by NTP mid-outage, and an archive
# imported out of order. It costs nothing, because the unique constraint makes
# re-offering a row the database already has a no-op.
REPLAY_GRACE_SECONDS = 3600

NEWEST_SQL = """
SELECT max(sampled_at) FROM tm.sample_all
 WHERE account_id = %s AND platform = %s
"""


class Destination(object):
    """Postgres, with the CSV underneath it as a spool.

    A poller must never die because a database is down, and must never lose an
    evening to one: a failed write goes to the CSV exactly as it did before
    there was a database, and the next write that succeeds drains what built up.
    So an outage costs some log lines and nothing else, and nobody has to repair
    anything afterwards.

    Three states, and they are deliberately not the same:

      no URL configured   CSV only, silently. This is what the project shipped
                          with, and keeping it working is what lets the
                          migration happen one machine at a time.
      configured, down    CSV, and one warning per outage rather than one per
                          tick -- the `reported` idiom youtube_cmd already uses
                          for an exhausted quota.
      configured, up      Postgres, and a replay of anything spooled earlier.
    """

    def __init__(self, platform, channel, spool_path, header):
        self.platform = platform
        self.channel = channel
        self.slug = config.channel_slug(channel)
        self.spool_path = spool_path
        self.header = header
        self._account = None
        self._reported = set()
        # Set when a sample went to the CSV instead of the database. A flag
        # rather than a query, because the common case is that there is nothing
        # to replay and asking every minute to be told so is a round trip a
        # minute for nothing. It is not sufficient on its own -- see
        # startup_replay().
        self._spooled = False

    # -- identity ----------------------------------------------------------

    def account(self, **identity):
        """The account row's id, resolved once and remembered.

        Called at startup rather than per sample, which is the point of
        platform_account: it replaces an API call both pollers currently make
        on every restart to re-derive an id that never changes.
        """
        if self._account is None:
            self._account = account_id(self.slug, self.platform,
                                       display_name=identity.get("display_name"),
                                       timezone_name=identity.get("timezone_name"))
        return self._account

    def describe(self):
        """Where samples are going, for the poller's opening log line."""
        if not db.configured():
            return os.path.basename(self.spool_path)
        return "{} (spooling to {} if it is unreachable)".format(
            db.where(), os.path.basename(self.spool_path))

    # -- writing -----------------------------------------------------------

    def record(self, row, log):
        """Store one sample, spooling on failure. True if it landed somewhere.

        False means BOTH failed -- a full disk, or a permission problem -- which
        is the same answer poll_once() has always given for a CSV it could not
        write.
        """
        fields = dict(zip(self.header, row))
        if not db.configured():
            return self._spool(row, log, warn=False)
        try:
            account = self.account()
            stream_id = record(self.platform, account, fields)
            refresh_reports(account, parse_stamp(fields.get("timestamp_utc")), stream_id)
        except Skipped as exc:
            log("ERROR    not stored: {}".format(exc))
            return False
        except (db.Unreachable, db.NotConfigured, SystemExit) as exc:
            # SystemExit is psycopg missing. Fatal for a chart, not for a
            # poller: losing tonight's samples over an absent package would be
            # a worse outcome than a loud line in the journal.
            if "down" not in self._reported:
                self._reported.add("down")
                log("WARN     database unavailable, spooling to {} -- {}".format(
                    os.path.basename(self.spool_path), str(exc).splitlines()[0]))
            return self._spool(row, log, warn=False)

        if self._reported:
            log("db       database is back")
            self._reported.clear()
        if self._spooled:
            self.replay(log)
        return True

    def _spool(self, row, log, warn=True):
        try:
            storage.append_row(self.spool_path, self.header, row)
        except OSError as exc:
            log("ERROR    could not write {}: {}".format(
                os.path.basename(self.spool_path), exc))
            return False
        if warn or db.configured():
            self._spooled = True
        return True

    # -- draining the spool ------------------------------------------------

    def startup_replay(self, log):
        """Drain anything a PREVIOUS run of this poller spooled.

        Called once before the loop, and the in-process flag cannot cover this:
        the units set Restart=on-failure, so the run that spooled is very often
        not the run that gets to replay. Without this, those rows would sit in
        the CSV until somebody noticed.
        """
        if not db.configured() or not os.path.exists(self.spool_path):
            return
        self._spooled = True
        self.replay(log)

    def replay(self, log):
        """Offer the database everything in the spool it does not already have."""
        try:
            account = self.account()
            rows = db.execute(NEWEST_SQL, (account, self.platform), fetch=True)
            newest = rows[0][0] if rows else None
            after = None
            if newest is not None:
                after = newest - timedelta(seconds=REPLAY_GRACE_SECONDS)

            pending = []
            for fields in storage.read_raw(self.spool_path):
                when = parse_stamp(fields.get("timestamp_utc"))
                if when is None or (after is not None and when <= after):
                    continue
                pending.append(fields)
        except (db.Unreachable, db.NotConfigured, SystemExit) as exc:
            log("WARN     spool replay deferred -- {}".format(str(exc).splitlines()[0]))
            return
        except OSError as exc:
            log("WARN     could not read {} -- {}".format(
                os.path.basename(self.spool_path), exc))
            return

        stored = 0
        for fields in pending:
            try:
                record(self.platform, account, fields, source="spool")
            except Skipped:
                continue
            except (db.Unreachable, SystemExit) as exc:
                log("WARN     spool replay stopped after {} row(s) -- {}".format(
                    stored, str(exc).splitlines()[0]))
                return
            stored += 1

        self._spooled = False
        if stored:
            log("db       replayed {} spooled sample(s) from {}".format(
                stored, os.path.basename(self.spool_path)))


# --------------------------------------------------------------------------
# one way in, for the readers
# --------------------------------------------------------------------------

# One read per source per run.
#
# daily.py asks for the same channel's samples four times -- read_day(),
# day_title(), day_points() and render_trend_charts() -- and graph_cmd re-reads
# them a fifth when daily drives it. Against a local file that was five open()
# calls and nobody minded; against a database on another host it is five round
# trips and five copies of the whole history over the wire.
#
# Safe because nothing here reads and writes in the same process: the pollers
# only write, and every reader wants the same immutable past. forget() exists
# for the day that stops being true.
_cache = {}


def forget(source=None):
    """Drop the run cache, all of it or one source."""
    if source is None:
        _cache.clear()
    else:
        _cache.pop(source, None)


def locator(platform, channel):
    """The source string naming one account in the database."""
    return "{}:{}".format(platform, config.channel_slug(channel))


def is_file(source):
    return str(source).lower().endswith(".csv")


def load(source):
    """Samples from whatever `source` names, in read_samples() shape.

    Two kinds of source, told apart the way pick_source() has always told them
    apart -- by the .csv on the end:

        data/metrics_foo.csv    a file, read by storage.read_samples()
        twitch:foo              an account in Postgres

    One string rather than a path-or-tuple, so callers, error messages and the
    existing tests stay as they are. Charting a file still needs no database at
    all, which is how the fixtures are charted and how you look at a spool by
    hand after an outage.
    """
    if source in _cache:
        return _cache[source]
    if is_file(source):
        loaded = storage.read_samples(source)
    else:
        platform, _, slug = str(source).partition(":")
        loaded = read(slug, platform)
    _cache[source] = loaded
    return loaded


def describe(source):
    """What to call a source in a message: a filename, or the locator itself."""
    return os.path.basename(source) if is_file(source) else str(source)


MINUTES_SQL = """
SELECT minute_at, platform::text, viewers, combined_viewers
  FROM tm.channel_minutes(
      (SELECT channel_id FROM tm.channel WHERE slug = %s), %s, %s,
      -- ::numeric, because a Python float arrives as double precision and the
      -- function declares numeric -- which is not a coercion PostgreSQL will
      -- make when it is resolving which overload was meant.
      %s::numeric)
 ORDER BY minute_at, platform
"""


def channel_minutes(channel, day, keys, tolerance=2.5, timezone_name=None):
    """(grid, values, combined) for one channel-day, built by the database.

    The same three things chart.align_platforms() returns, so it can be handed
    straight to render_platforms(aligned=...) -- which is the point. The
    cross-platform chart is the one place the website needed to re-read every
    sample from both platforms just to draw a line; now it reads a grid.

    `keys` is the platform order the caller wants a column for. A platform with
    nothing that day contributes no rows and gets no column, exactly as
    align_platforms() drops an entry with no points.

    Returns ([], {}, []) when there is nothing that day, which is what
    render_platforms() already treats as "no chart".
    """
    rows = db.execute(MINUTES_SQL,
                      (config.channel_slug(channel), day,
                       timezone_name or config.resolve_db_timezone(), tolerance),
                      fetch=True)
    if not rows:
        return [], {key: [] for key in keys}, []

    grid, seen = [], set()
    for when, _platform, _viewers, _total in rows:
        # psycopg gives these back in the session zone, which is pinned to UTC;
        # align_platforms() builds them from sample timestamps, which are also
        # UTC-aware. Normalising keeps the two comparable in the parity check.
        when = when.astimezone(timezone.utc)
        if when not in seen:
            seen.add(when)
            grid.append(when)

    by_platform = {}
    combined = {}
    for when, platform, viewers, total in rows:
        when = when.astimezone(timezone.utc)
        by_platform.setdefault(platform, {})[when] = viewers
        combined[when] = total

    values = {key: [by_platform.get(key, {}).get(when) for when in grid]
              for key in keys if key in by_platform}
    return grid, values, [combined[when] for when in grid]


# --------------------------------------------------------------------------
# the report tables
# --------------------------------------------------------------------------
#
# The trend charts' aggregates, read from tm.report_daily_peak and
# tm.report_clock_bucket rather than recomputed from every sample the channel
# has ever produced. Each returns EXACTLY what its namesake in trends.py
# returns, which is what lets the two be compared row for row in a test rather
# than merely inspected for looking similar.
#
# Every one of them takes the reporting zone explicitly and never leans on the
# SQL's own DEFAULT NULL. The tables key on tz, so a refresh written under one
# zone is invisible to a read under another -- and passing the same resolved
# value everywhere is the whole of what keeps those two agreeing.


DAILY_PEAKS_SQL = """
SELECT local_date, peak_viewers, peak_at
  FROM tm.daily_peaks(
      (SELECT channel_id FROM tm.channel WHERE slug = %s), %s, %s,
      -- ::integer for the reason MINUTES_SQL casts its numeric: psycopg sends a
      -- small Python int as int2, and overload resolution will not widen it.
      %s::integer, %s)
 ORDER BY local_date
"""


def _peak_rows(channel, platform, day, days, zone):
    """(local_date, peak_viewers, peak_at) for a calendar window, oldest first."""
    return db.execute(DAILY_PEAKS_SQL,
                      (config.channel_slug(channel), platform, day, max(1, int(days)),
                       zone), fetch=True)


def _peak_entry(local_date, peak, at):
    """One row as trends.daily_peaks() would have built it.

    Normalised the way channel_minutes() normalises: the session zone is pinned
    to UTC and trends.daily_peaks() carries sample timestamps that are UTC-aware,
    so the two stay comparable in the parity check.
    """
    return {"day": local_date, "peak": peak,
            "at": at.astimezone(timezone.utc) if (at and peak is not None) else None}


def streamed_days(channel, platform, day, count, lookback=trends.LOOKBACK_DAYS,
                  timezone_name=None):
    """The last `count` dates on or before `day` that streamed, oldest first.

    trends.streamed_window()'s answer, read from the report table instead of
    from samples. A date qualifies by having a peak, which is the same test
    _live_on() applies -- and NOT by status = 'live', which also admits a day
    that was live but never returned a viewer count. Such a day has no bar to
    draw, and admitting it here would put an empty column on the chart and break
    parity with the Python path.
    """
    if count <= 0:
        return []
    rows = _peak_rows(channel, platform, day, max(1, int(lookback)),
                      timezone_name or config.resolve_db_timezone())
    return [row[0] for row in rows if row[1] is not None][-count:]


def daily_peaks(channel, platform, day, days=trends.PEAK_DAYS, calendar=False,
                lookback=trends.LOOKBACK_DAYS, timezone_name=None):
    """[{"day", "peak", "at"}, ...] oldest first -- trends.daily_peaks()' shape.

    `calendar` picks the axis, exactly as it does in trends.daily_peaks(): the
    last `days` dates, or the last `days` dates that streamed.

    On a calendar axis the days are built here rather than taken from the rows.
    tm.daily_peaks() generates its own and normally returns one row per day, but
    it returns NONE at all for a slug the channel table doesn't have -- so a
    short list would not fail, it would quietly relabel the chart. Filling a
    Python axis makes that impossible. On a streamed axis the rows ARE the axis,
    because a date only reaches it by having a row.

    A day with no live samples gets peak None and never 0, the same distinction
    trends.daily_peaks() draws and the same one report_daily_peak_dark_is_null
    enforces in the table. `status` is deliberately not consulted: the two CHECK
    constraints already guarantee a non-live day carries no peak, so reading it
    would add a branch that cannot be exercised.
    """
    if days <= 0:
        # Python's window() is empty here and the SQL's
        # generate_series(0, greatest(p_days,1)-1) is one row, so this guard is
        # what keeps `--peak-days 0` from drawing a one-bar chart on one path
        # and nothing on the other.
        return []
    zone = timezone_name or config.resolve_db_timezone()

    if not calendar:
        rows = [row for row in _peak_rows(channel, platform, day, lookback, zone)
                if row[1] is not None][-days:]
        return [_peak_entry(*row) for row in rows]

    found = {row[0]: row for row in _peak_rows(channel, platform, day, days, zone)}
    out = []
    for local_date in trends.window(day, days):
        row = found.get(local_date)
        out.append(_peak_entry(local_date, row[1] if row else None,
                               row[2] if row else None))
    return out


COMPARE_SLOTS_SQL = """
SELECT local_date, slot, avg_viewers, dropped_slots
  FROM tm.compare_slots(
      (SELECT channel_id FROM tm.channel WHERE slug = %s), %s, %s,
      %s::integer, %s::integer, %s::integer, %s)
 ORDER BY local_date, slot
"""


def compare_slots(channel, platform, day, days=trends.COMPARE_DAYS,
                  minutes=trends.BUCKET_MINUTES, calendar=False,
                  lookback=trends.LOOKBACK_DAYS, timezone_name=None):
    """(slots, per_day, dropped) -- trends.compare_slots()' three, from the table.

    On a CALENDAR axis `days` is passed through unchanged: the SQL filters
    `local_date > p_end_day - p_days - 1`, which is p_days + 1 days inclusive,
    exactly the window(end_day, days + 1) Python uses. per_day is then filled
    from a Python axis, because report_clock_bucket is SPARSE -- a day the
    channel didn't stream has no rows at all -- while render_typical() indexes
    its fade opacities by POSITION in per_day and sizes its bar groups by
    len(per_day). A missing empty day would not leave a gap in the chart; it
    would shift every colour after it and retitle the comparison.

    On a STREAMED axis the dates are looked up first and `p_days` is then set to
    span exactly them: the filter above is `local_date >= p_end_day - p_days`,
    so p_days = (day - oldest).days bounds the window at the oldest date wanted.
    Because those are the MOST RECENT dates that streamed, the oldest of them is
    the boundary and no other streamed date can fall inside -- which matters for
    more than the axis, since the busiest-window trim weights whatever the
    window holds. It weights exactly the days being drawn.
    """
    width = trends.bucket_width(minutes)
    zone = timezone_name or config.resolve_db_timezone()
    if calendar:
        axis, span = trends.window(day, days + 1), days
    else:
        axis = streamed_days(channel, platform, day, days + 1, lookback, zone)
        if not axis:
            return [], [], 0
        span = (day - axis[0]).days
    rows = db.execute(COMPARE_SLOTS_SQL,
                      (config.channel_slug(channel), platform, day, span, width,
                       trends.MAX_SLOTS, zone), fetch=True)
    by_day = {}
    for local_date, slot, average, _dropped in rows:
        # float, not Decimal: numeric arrives as Decimal, chart.nice_axis()
        # divides by 4.0 and render_typical() divides by the top value, and
        # Decimal / float raises rather than coercing.
        by_day.setdefault(local_date, {})[int(slot)] = float(average)
    # The function already worked the count out and repeats it on every row;
    # no rows means nothing was dropped, which is what Python's
    # len(used) - len(kept) says about an empty window too.
    dropped = int(rows[0][3]) if rows else 0
    return (sorted({int(row[1]) for row in rows}),
            [(local_date, by_day.get(local_date, {})) for local_date in axis],
            dropped)


# --------------------------------------------------------------------------
# per-broadcast trends
# --------------------------------------------------------------------------
#
# The two above compare DAYS. These compare BROADCASTS, which is a different
# axis and not merely a finer one: a Saturday spent at two parks is two rows
# here and one bar there, and the location rollup is only answerable on this
# side of that distinction.
#
# Neither has a pure-Python twin in trends.py, because neither re-derives
# anything -- tm.report_stream_trend is filled by the poller and by the nightly
# refresh, and there is no CSV path that could produce it. So there is no parity
# check to hold them to, and none is missing: the SQL is tested directly.


def _stream_row(row):
    """One broadcast, as both stream_trends() and location_streams() return it.

    Shared rather than duplicated because the two queries return the identical
    column list on purpose -- tm.location_streams() is tm.stream_trends() with a
    venue predicate inside the LIMIT -- and trends.render_stream_bars() draws
    either without knowing which it was handed. Two copies of this would be two
    chances for a location chart to disagree with the channel-wide one about what
    "watchtime" means.

    float, not Decimal: nice_axis() divides by 4.0 and every renderer divides by
    the top value, and Decimal / float raises rather than coercing.
    """
    return {"stream_id": row[0], "day": row[1],
            "started": row[2].astimezone(timezone.utc) if row[2] else None,
            "weekday": int(row[3]), "title": row[4], "location": row[5],
            "followers": row[6], "likes": row[7], "peak": row[8],
            "watch_minutes": float(row[9]) if row[9] is not None else None,
            "watchtime": float(row[9]) / 60.0 if row[9] is not None else None,
            "covered": row[10], "span": row[11],
            "coverage": (float(row[10]) / row[11]
                         if row[10] is not None and row[11] else None)}


def _group_row(row):
    """One rolled-up group, as stream_groups() and location_groups() return it.

    Six columns, which is the contract 009 established so that one renderer can
    draw every by-group chart on the site. float for _stream_row()'s reason.
    """
    return {"key": row[0], "streams": int(row[1]),
            "total": float(row[2]) if row[2] is not None else 0.0,
            "average": float(row[3]),
            "best": float(row[4]) if row[4] is not None else None,
            "best_stream_id": row[5]}


STREAM_TRENDS_SQL = """
SELECT stream_id, local_date, started_at, weekday, title, location,
       follower_delta, likes_peak, peak_viewers,
       watch_minutes, covered_seconds, span_seconds
  FROM tm.stream_trends(
      (SELECT channel_id FROM tm.channel WHERE slug = %s), %s, %s,
      -- ::integer for DAILY_PEAKS_SQL's reason: psycopg sends a small Python
      -- int as int2, and overload resolution will not widen it. A None goes
      -- through as a typed NULL, which is how "unbounded" is spelled.
      %s::integer, %s::integer, %s, %s)
 ORDER BY started_at
"""


def stream_trends(channel, platform, day, count=trends.STREAM_COUNT,
                  lookback=trends.LOOKBACK_DAYS, timezone_name=None,
                  location=None):
    """The last `count` broadcasts on or before `day`, oldest first.

    [{"stream_id", "day", "started", "weekday", "title", "location",
      "followers", "likes", "peak", "watch_minutes", "watchtime",
      "covered", "span", "coverage"}, ...]

    `day` is the local date the broadcast STARTED on, so a stream that ran past
    midnight is filed under the evening it belongs to rather than split.

    "followers" is the gain across the broadcast's own samples and "likes" the
    peak of a count that only climbs. Either is None when the platform does not
    carry it -- a Twitch row has no likes and a YouTube row no followers -- and
    that is what lets the renderers select themselves by returning None rather
    than by testing the platform's name.

    "watch_minutes" is the integral under the concurrent-viewer curve and
    "watchtime" the same figure in HOURS, which is the unit the charts draw and
    the one a person says out loud. Both platforms carry it, so unlike the two
    above it is not what selects a renderer. It is an ESTIMATE of live watch
    time only -- see 008_watchtime.sql for what it cannot be used for -- and
    "coverage" is the share of the broadcast that was actually integrated, which
    is what keeps a low figure from an outage readable as one.

    `location` filters to one venue. None is every venue -- what every caller
    meant before there were location pages -- and "" is the venue that is no
    venue, the broadcasts whose title matched no rule. Those are different
    questions and both are askable; "" is the spelling stream_groups() and
    location_watch() already hand back for the unmatched ones.

    THE FILTER IS APPLIED BEFORE THE LIMIT, in the SQL. Filtering these rows
    afterwards would keep whichever of the channel's last ten happened to be at
    the venue -- three bars, or none -- and label it "the last ten at Epcot".

    `count=None` and `lookback=None` mean unbounded, which is what the
    venue-history chart wants: "is this place getting better or worse" is a
    question about the whole record. Passing a huge number instead would state a
    window this does not mean, and has a cliff in it -- the date arithmetic
    overflows long before an int does.
    """
    if count is not None and count <= 0:
        # The SQL clamps with greatest(0, p_streams) and would agree, but the
        # guard keeps `--stream-count 0` from making a round trip to say so.
        return []
    rows = db.execute(STREAM_TRENDS_SQL,
                      (config.channel_slug(channel), platform, day,
                       None if count is None else max(1, int(count)),
                       None if lookback is None else max(1, int(lookback)),
                       timezone_name or config.resolve_db_timezone(),
                       location), fetch=True)
    return [_stream_row(row) for row in rows]


STREAM_GROUPS_SQL = """
SELECT group_key, streams, total, average, best, best_stream_id
  FROM tm.stream_groups(
      (SELECT channel_id FROM tm.channel WHERE slug = %s), %s, %s, %s, %s,
      %s::integer, %s::integer, %s, %s)
 ORDER BY group_key
"""


def stream_groups(channel, platform, metric, grouping, day,
                  count=trends.STREAM_COUNT, lookback=trends.LOOKBACK_DAYS,
                  timezone_name=None, location=None):
    """One row per weekday or per location over the same window, sparse.

    [{"key", "streams", "total", "average", "best", "best_stream_id"}, ...]

    `grouping` is "weekday" or "location"; the SQL raises on anything else
    rather than returning nothing, which would read as a channel that gained no
    followers. `metric` is "followers", "likes", "watchtime" or "peak" -- and
    watchtime arrives in HOURS, converted in the SQL so the renderers cannot
    each remember it differently.

    Sparse on purpose, like compare_slots(): a weekday nobody streamed on has no
    row at all. The renderer builds the Mon..Sun axis and fills it from this.

    An unmatched location arrives as "" and is left that way -- the renderer
    owns the word shown for it, so there is one place that decides.

    `location` restricts the window to one venue, and is stream_trends()'
    argument forwarded rather than a second implementation -- this function is
    built ON that one, so a weekday rollup scoped to a venue summarises exactly
    the broadcasts that venue's own bars draw and cannot drift from them.
    """
    if count is not None and count <= 0:
        return []
    rows = db.execute(STREAM_GROUPS_SQL,
                      (config.channel_slug(channel), platform, metric, grouping,
                       day,
                       None if count is None else max(1, int(count)),
                       None if lookback is None else max(1, int(lookback)),
                       timezone_name or config.resolve_db_timezone(),
                       location), fetch=True)
    # "total" and "best" are numeric rather than integers: watchtime is
    # fractional, and one shape for all four metrics beats a cast that depends
    # on which was asked for. _group_row() has the rest of the reasoning.
    return [_group_row(row) for row in rows]


# --------------------------------------------------------------------------
# watch time
# --------------------------------------------------------------------------


WATCH_TOTALS_SQL = """
SELECT local_date, status::text, watch_minutes, covered_seconds, rolling_minutes
  FROM tm.watch_totals(
      (SELECT channel_id FROM tm.channel WHERE slug = %s), %s, %s,
      %s::integer, %s::integer, %s)
 ORDER BY local_date
"""


def watch_totals(channel, platform, day, days=trends.WATCH_DAYS,
                 rolling=trends.ROLLING_DAYS, timezone_name=None):
    """Estimated watch time per local day, with the trailing total at each.

    [{"day", "status", "watch_minutes", "watchtime", "covered",
      "rolling_minutes", "rolling"}, ...] -- oldest first, one entry per day in
    the window whether or not it was streamed.

    DENSE, unlike stream_trends(): the underlying report table holds a row for
    every day in a refreshed range, and a day off arrives with watch_minutes
    None. The renderer draws that as a dash, which is the distinction the peaks
    chart already makes -- "did not stream" is not "nobody watched".

    "rolling" is the trailing `rolling`-day total in HOURS, that day inclusive,
    so the newest entry is the past-twelve-months figure when rolling is 365.
    It is an estimate of LIVE watch time and undercounts by however much of the
    audience arrived after the broadcast ended; 008_watchtime.sql says why that
    makes it unfit for judging the 4,000-hour Partner Programme threshold, and
    trends.render_watch_rolling() repeats the warning where a reader will see it.
    """
    if days <= 0:
        return []
    rows = db.execute(WATCH_TOTALS_SQL,
                      (config.channel_slug(channel), platform, day,
                       max(1, int(days)), max(1, int(rolling)),
                       timezone_name or config.resolve_db_timezone()), fetch=True)
    out = []
    for local_date, status, minutes, covered, rolling_minutes in rows:
        minutes = float(minutes) if minutes is not None else None
        rolled = float(rolling_minutes) if rolling_minutes is not None else None
        out.append({
            "day": local_date, "status": status,
            "watch_minutes": minutes,
            "watchtime": minutes / 60.0 if minutes is not None else None,
            "covered": covered,
            "rolling_minutes": rolled,
            "rolling": rolled / 60.0 if rolled is not None else None,
        })
    return out


LOCATION_WATCH_SQL = """
SELECT group_key, streams, total, average, best, best_stream_id,
       covered_seconds, span_seconds, first_local_date, last_local_date
  FROM tm.location_watch(
      (SELECT channel_id FROM tm.channel WHERE slug = %s), %s, %s)
 ORDER BY group_key
"""


def location_watch(channel, platform, timezone_name=None):
    """Estimated watch hours per location, over every broadcast on record.

    [{"key", "streams", "total", "average", "best", "best_stream_id",
      "covered", "span", "coverage", "first_day", "last_day"}, ...]

    The first six keys are stream_groups()' six, so trends.render_stream_groups()
    draws this and the followers-by-location chart with the same code. Hours, like
    stream_groups() with metric="watchtime" -- converted in the SQL so the
    renderers cannot each remember it differently.

    ALL-TIME, and that is the whole reason this exists rather than another
    stream_groups() call: that function is windowed by "the last N broadcasts",
    and a venue's worth is not. 009_location_watch.sql argues it at length.

    Sparse, like stream_groups(): a location with no estimable broadcast has no
    row. An unmatched location arrives as "" and is left that way -- the renderer
    owns the word shown for it.

    "coverage" is covered/span, the share of these broadcasts a running poller
    actually saw, or None when nothing recorded it. trends._coverage_note() turns
    it into the caveat on the chart, and None prints nothing rather than 0%.
    """
    rows = db.execute(LOCATION_WATCH_SQL,
                      (config.channel_slug(channel), platform,
                       timezone_name or config.resolve_db_timezone()), fetch=True)
    # float, not Decimal, for stream_groups()' reason: nice_axis() divides by 4.0
    # and Decimal / float raises rather than coercing.
    out = []
    for (key, streams, total, average, best, best_stream_id,
         covered, span, first_day, last_day) in rows:
        out.append({
            "key": key, "streams": int(streams),
            "total": float(total) if total is not None else 0.0,
            "average": float(average),
            "best": float(best) if best is not None else None,
            "best_stream_id": best_stream_id,
            "covered": int(covered) if covered is not None else None,
            "span": int(span) if span is not None else None,
            "coverage": (float(covered) / float(span)
                         if covered is not None and span else None),
            "first_day": first_day, "last_day": last_day,
        })
    return out


# --------------------------------------------------------------------------
# one venue at a time
# --------------------------------------------------------------------------
#
# The reads above compare the channel to itself. These support comparing ONE
# VENUE to its own history, which the by-location charts cannot: they draw a mean
# per venue, and a mean is exactly what the peaks chart refuses to draw for days.
#
# There are only two functions here, and that is the point. The venue filter
# itself lives on stream_trends() and stream_groups() as an argument, because
# stream_groups() is built on stream_trends() and a filter at the bottom of the
# stack cannot disagree with the rollups above it. 010_location_trends.sql argues
# it at length.


def location_history(channel, platform, location, day, count=None,
                     lookback=None, timezone_name=None):
    """Every broadcast at one venue, oldest first -- the venue's whole record.

    stream_trends() with the window taken off, given a name of its own so the
    caller states which question it asked. The two windows draw different charts
    and want different headings: "last 10 broadcast(s)" against "all 41 on
    record", and a renderer cannot tell which it was handed.

    `count` caps the axis for a venue with years of history -- see
    trends.LABEL_LIMIT for why an unbounded axis is a legibility problem before
    it is a performance one. None means every broadcast on record.

    "On record" is doing real work in that phrase: this reads
    tm.report_stream_trend, so it shows what the report tables hold rather than
    everything that ever happened. A channel imported before 007 has whatever
    `db --rebuild` refreshed. The chart says "on record" for that reason.
    """
    return stream_trends(channel, platform, day, count=count, lookback=lookback,
                         timezone_name=timezone_name, location=location or "")


STREAM_LOCATIONS_SQL = """
SELECT location, broadcasts, streams, first_local_date, last_local_date, platforms
  FROM tm.stream_locations(
      (SELECT channel_id FROM tm.channel WHERE slug = %s), %s)
"""


def stream_locations(channel, timezone_name=None):
    """Every location the channel has streamed from, busiest first.

    [{"key", "name", "broadcasts", "streams", "first_day", "last_day",
      "platforms"}, ...]

    ACROSS BOTH PLATFORMS, unlike every other read here. Those take a platform
    because they draw a chart and a chart is per-platform; this answers "which
    pages does this channel need", and a venue visited only on YouTube needs one
    exactly as much as a venue visited on both. Taking a platform would give the
    site two different pickers. "platforms" says which it was streamed on, so a
    caller can still skip a panel that would be empty.

    TWO COUNTS, because grouping across platforms makes them different numbers.
    "streams" is rows -- one per stream per platform, which is what the
    per-platform charts draw. "broadcasts" LINKS THE SIMULCASTS: rows whose live
    windows overlap at one venue are one afternoon the streamer spent there, not
    two. It is the count a picker wants, and the difference is not marginal for a
    channel that simulcasts everything -- there it is exactly double.
    011_location_broadcasts.sql argues the linking rule.

    "key" is the venue as the database spells it, "" for the broadcasts no rule
    matched. "name" is the same thing with trends.UNKNOWN_LOCATION substituted
    for "", so a caller building a picker does not have to know the convention.

    Ordering is the SQL's, and it is deliberately NOT the charts' ordering: this
    counts broadcasts where every chart axis sorts by the average of whichever
    metric it draws. One picker serving every panel cannot follow a per-panel
    order, and one that reshuffled itself when the reader changed charts would be
    worse than useless.
    """
    rows = db.execute(STREAM_LOCATIONS_SQL,
                      (config.channel_slug(channel),
                       timezone_name or config.resolve_db_timezone()), fetch=True)
    return [{"key": row[0], "name": row[0] or trends.UNKNOWN_LOCATION,
             "broadcasts": int(row[1]), "streams": int(row[2]),
             "first_day": row[3], "last_day": row[4],
             "platforms": list(row[5] or ())}
            for row in rows]


COVERAGE_SQL = """
SELECT c.channel_id,
       (SELECT count(DISTINCT p.local_date)
          FROM tm.report_daily_peak p
         WHERE p.channel_id = c.channel_id AND p.tz = %s
           AND p.local_date BETWEEN %s AND %s),
       (SELECT count(*)
          FROM tm.report_clock_bucket b
         WHERE b.channel_id = c.channel_id AND b.tz = %s
           AND b.bucket_minutes = %s::smallint
           AND b.local_date BETWEEN %s AND %s),
       (SELECT count(*)
          FROM tm.report_clock_bucket b
         WHERE b.channel_id = c.channel_id AND b.tz = %s
           AND b.local_date BETWEEN %s AND %s),
       -- The channel's own first day, so a window reaching back further than
       -- the channel has existed is not forever judged incomplete. Same
       -- expression db_cmd._rebuild_reports() spans a channel with.
       (SELECT min(s.sampled_at AT TIME ZONE %s)::date
          FROM tm.sample_all s WHERE s.channel_id = c.channel_id)
  FROM tm.channel c
 WHERE c.slug = %s
"""


def ensure_reports(channel, day, days, minutes=trends.BUCKET_MINUTES,
                   timezone_name=None):
    """Bring one channel's report tables up to date for a window. True if asked.

    The charts read stored rows, so something has to be responsible for the rows
    existing. The poller refreshes two days on every sample and `db --import`
    rebuilds everything it loaded, which between them cover the steady state --
    but not a channel set up today, not a window nobody has streamed in since
    the tables were built, and not a --bucket width nothing has ever written.

    So: probe what the window actually holds, then refresh either the whole span
    or just its tail. False means there is no such channel, which is not a
    failure -- a slug with no row in tm.channel has no samples either, and the
    reads that follow will draw nothing, which is right.
    """
    zone = timezone_name or config.resolve_db_timezone()
    width = trends.bucket_width(minutes)
    span = max(1, int(days))
    first = day - timedelta(days=span - 1)

    rows = db.execute(COVERAGE_SQL,
                      (zone, first, day,
                       zone, width, first, day,
                       zone, first, day,
                       zone,
                       config.channel_slug(channel)), fetch=True)
    if not rows:
        return False
    channel_id, dated, at_width, any_width, began = rows[0]

    # Never ask for report rows older than the channel's first sample. Without
    # this, a lookback of 90 days over a channel with 30 days of history is
    # short on EVERY run and refreshes all 90 forever -- the counts can never
    # reach a span that has nothing to fill it.
    if began and began > first:
        first = min(day, began)
    span = (day - first).days + 1

    # Two conditions, and the second is why the width is probed twice. A window
    # missing days needs building. A window that holds buckets at SOME width but
    # none at this one is `daily --bucket 45` asking for a width nothing has
    # written -- whereas a channel that has simply never streamed holds none at
    # any width, and must not be re-refreshed on every run for the rest of time.
    short = dated < span or (any_width and not at_width)

    # Two days even when the window looks complete. refresh_reports() swallows
    # its own failures on purpose -- the sample is the irreplaceable thing and a
    # report row can always be rebuilt -- so a poller whose refresh failed
    # leaves today's row STALE rather than missing, and no count of dates can
    # see that. Two days is exactly what refresh_after_sample() already does on
    # every sample, so the steady-state cost here is one more poll's worth.
    since = first if short else day - timedelta(days=1)
    db.execute("SELECT tm.refresh_range(%s, %s, %s, %s, %s::integer)",
               (channel_id, since, day, zone, width))
    return True
