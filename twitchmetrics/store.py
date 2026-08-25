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

from . import config, db, storage

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
