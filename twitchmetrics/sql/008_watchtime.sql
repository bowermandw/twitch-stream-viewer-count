-- Estimated watch time, integrated from the concurrent-viewer curve.
--
-- Neither platform hands this number out. YouTube's estimatedMinutesWatched and
-- averageViewDuration live in the YouTube Analytics API, which is owner-only:
-- ids=channel==MINE, scope yt-analytics.readonly, and a Google account that owns
-- or holds a Studio permission on the channel. An API key cannot reach them at
-- any quota tier, and Twitch has no equivalent endpoint at all.
--
-- What this project HAS is the curve itself, sampled every sixty seconds. The
-- area under it is watch time:
--
--     watch minutes = SUM over consecutive samples of
--                         (viewers_before + viewers_after) / 2 * gap_seconds / 60
--
-- A trapezoid rather than a rectangle, because the poll interval is not
-- guaranteed -- a restart, a slow API call or a spooled backfill all make gaps of
-- their own -- and a trapezoid is right for any of them where "viewers x nominal
-- interval" is right only for the interval it assumed.
--
-- WHAT THIS NUMBER IS NOT. Three limits, all of them structural, and named here
-- because a figure like this gets quoted:
--
--   * It is LIVE ONLY. Watch time accumulated on the archived broadcast after the
--     stream ends is invisible to a poller that samples concurrentViewers, and on
--     a channel with a back catalogue that is frequently the larger share. This
--     number reads LOW against Studio, sometimes by a lot.
--   * It is not YouTube's definition. Studio counts partial minutes, ad breaks
--     and validity rules this has no visibility into. Treat it as a trend line to
--     compare against its own history, never as a figure to reconcile.
--   * It cannot be used to judge the 4,000-hour YouTube Partner Programme
--     threshold. That bar counts VALID PUBLIC watch hours across live and VOD in
--     the trailing twelve months; this misses VOD entirely and applies none of
--     the validity rules, so it undercounts by an unknown margin. The real figure
--     is in Studio's YPP eligibility card, visible to the channel owner only.
--     tm.watch_totals() reports the rolling total anyway, because the SHAPE of it
--     is worth watching -- but the renderer labels it an estimate, and so does
--     this comment.
--
-- COVERAGE is what makes the estimate readable rather than merely small. A gap
-- wider than the poller's own rhythm is not integrated at all: crediting three
-- hours of viewers to a poller that was down for three hours would be inventing
-- data, and the whole point of the caveats above is that this number never does
-- that. So each stream carries covered_seconds beside its watch minutes, and a
-- low total from an outage stays distinguishable from a low total from a quiet
-- night.
--
-- The cap is median_gap * tolerance, which is chart.GAP_TOLERANCE = 2.5 and the
-- same rule chart.split_sessions() already uses to decide a poller stopped. The
-- median is computed the way chart.median_step() does -- sorted, then indexed at
-- len // 2, which is the UPPER median on an even count and is neither
-- percentile_cont nor percentile_disc. 004_minutes.sql reproduces it the same way
-- and for the same reason.

SET LOCAL search_path = tm, public;


-- --------------------------------------------------------------------------
-- the integral
-- --------------------------------------------------------------------------

-- Per LOCAL DATE, which is what lets one function serve both the per-broadcast
-- total and the per-day one. A trapezoid is attributed whole to the date of its
-- LATER sample: splitting one at midnight would be arithmetically tidier and
-- would move at most sixty seconds of viewers between two days, which is below
-- the noise floor of everything above.
--
-- Deliberately NOT the minute grid tm.channel_minutes() builds. That one carries
-- a value forward to paint a continuous line and is right for drawing; summing it
-- would count a carried-forward minute as watched. This reads only the samples
-- that actually exist.
CREATE OR REPLACE FUNCTION tm.stream_watch_slices(
    p_stream_id bigint,
    p_tz        text,
    p_tolerance numeric DEFAULT 2.5
) RETURNS TABLE (local_date date, watch_minutes numeric, covered_seconds numeric)
LANGUAGE sql STABLE AS $$
    WITH pts AS (
        SELECT s.sampled_at, s.viewer_count
          FROM tm.sample_all s
         WHERE s.stream_id = p_stream_id
           -- YouTube omits concurrentViewers for the first moments of a
           -- broadcast and whenever the owner has hidden it, so a live sample
           -- with no number is normal. Dropping it here means the pair either
           -- side spans the hole -- and if that span is wider than the cap, the
           -- hole is excluded from coverage too, which is the honest reading.
           AND s.viewer_count IS NOT NULL
    ),
    stepped AS (
        SELECT sampled_at, viewer_count,
               lag(sampled_at)   OVER (ORDER BY sampled_at) AS prev_at,
               lag(viewer_count) OVER (ORDER BY sampled_at) AS prev_viewers
          FROM pts
    ),
    gaps AS (
        SELECT sampled_at, viewer_count, prev_viewers,
               extract(epoch FROM sampled_at - prev_at)::numeric AS secs
          FROM stepped
         WHERE prev_at IS NOT NULL
           -- chart.median_step() filters the same way. The unique constraint on
           -- (account_id, sampled_at) makes a zero gap unrepresentable, but the
           -- filter is what the Python does and parity is worth more than the
           -- one statement it saves.
           AND sampled_at > prev_at
    ),
    cap AS (
        SELECT coalesce((array_agg(secs ORDER BY secs))[count(*) / 2 + 1], 60.0)
               * greatest(p_tolerance, 0.0) AS limit_secs
          FROM gaps
    )
    SELECT (g.sampled_at AT TIME ZONE p_tz)::date,
           sum((g.prev_viewers + g.viewer_count) / 2.0 * g.secs / 60.0),
           sum(g.secs)
      FROM gaps g CROSS JOIN cap c
     WHERE g.secs <= c.limit_secs
     GROUP BY 1;
$$;


-- One broadcast's total. NULL watch minutes rather than zero when nothing could
-- be integrated -- a stream with a single sample, or one whose every gap exceeded
-- the cap -- for tm.report_daily_peak's reason: "we do not know" and "nobody
-- watched" are different readings and the charts draw them differently.
--
-- span_seconds comes from the stream row rather than from the samples that
-- carried a viewer count, so a broadcast that YouTube reported no number for
-- during its first ten minutes shows coverage below 100% instead of hiding the
-- hole by measuring itself against its own good part.
CREATE OR REPLACE FUNCTION tm.stream_watch_time(
    p_stream_id bigint,
    p_tolerance numeric DEFAULT 2.5
) RETURNS TABLE (watch_minutes numeric, covered_seconds integer, span_seconds integer)
LANGUAGE sql STABLE AS $$
    SELECT sum(w.watch_minutes)::numeric(18,3),
           sum(w.covered_seconds)::integer,
           (SELECT extract(epoch FROM s.last_sample_at - s.first_sample_at)::integer
              FROM tm.stream s WHERE s.stream_id = p_stream_id)
      -- 'UTC' and not the reporting zone: the total is the sum of every slice
      -- whatever the day boundaries fall, and passing a zone here would suggest
      -- otherwise.
      FROM tm.stream_watch_slices(p_stream_id, 'UTC', p_tolerance) w;
$$;


-- --------------------------------------------------------------------------
-- where it is stored
-- --------------------------------------------------------------------------
--
-- Columns on the two report tables that already exist rather than a table of
-- their own. Watch time is a property of a broadcast and of a day, and both of
-- those already have a row -- a third table would key on the same things and
-- oblige every reader to join it.

ALTER TABLE tm.report_stream_trend
    ADD COLUMN IF NOT EXISTS watch_minutes   numeric(18,3),
    ADD COLUMN IF NOT EXISTS covered_seconds integer,
    ADD COLUMN IF NOT EXISTS span_seconds    integer;

-- No coverage denominator here, unlike the stream table. A day can hold two
-- broadcasts with four dark hours between them, and "covered / wall clock" would
-- read as an outage. Coverage is exact per broadcast and is reported there.
ALTER TABLE tm.report_daily_peak
    ADD COLUMN IF NOT EXISTS watch_minutes   numeric(18,3),
    ADD COLUMN IF NOT EXISTS covered_seconds integer;


-- --------------------------------------------------------------------------
-- filling it
-- --------------------------------------------------------------------------

-- 007's body with one LATERAL join added. Replaced wholesale in a new file
-- rather than edited in place, which is the rule 005 and 007 were both written
-- under: an applied file is history and does not change.
CREATE OR REPLACE FUNCTION tm.refresh_stream_trends(
    p_channel_id bigint,
    p_from       date,
    p_to         date,
    p_tz         text DEFAULT NULL
) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_tz   text;
    v_from timestamptz;
    v_to   timestamptz;
    v_rows integer;
BEGIN
    SELECT coalesce(p_tz, c.report_timezone) INTO v_tz
      FROM tm.channel c WHERE c.channel_id = p_channel_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown channel_id %', p_channel_id;
    END IF;
    IF p_to < p_from THEN
        RAISE EXCEPTION 'empty range: % .. %', p_from, p_to;
    END IF;

    v_from := (p_from::timestamp)     AT TIME ZONE v_tz;
    v_to   := ((p_to + 1)::timestamp) AT TIME ZONE v_tz;

    DELETE FROM tm.report_stream_trend r
     WHERE r.channel_id = p_channel_id
       AND r.tz = v_tz
       AND r.stream_id IN (
           SELECT s.stream_id
             FROM tm.stream s
             JOIN tm.platform_account a ON a.account_id = s.account_id
            WHERE a.channel_id = p_channel_id
              AND s.first_sample_at <  v_to
              AND s.last_sample_at  >= v_from);

    INSERT INTO tm.report_stream_trend (
        channel_id, tz, stream_id, platform, local_date, weekday,
        started_at, ended_at, title, location,
        follower_delta, likes_peak, peak_viewers, sample_count,
        watch_minutes, covered_seconds, span_seconds, computed_at)
    SELECT p_channel_id, v_tz, s.stream_id, a.platform,
           d.local_date,
           (extract(isodow FROM d.local_date)::smallint - 1),
           coalesce(s.started_at, s.first_sample_at),
           s.ended_at, s.title,
           tm.stream_location(p_channel_id, s.title),
           f.delta, l.peak, v.peak,
           s.sample_count,
           w.watch_minutes, w.covered_seconds, w.span_seconds, now()
      FROM tm.stream s
      JOIN tm.platform_account a ON a.account_id = s.account_id
      CROSS JOIN LATERAL (
          SELECT (coalesce(s.started_at, s.first_sample_at)
                    AT TIME ZONE v_tz)::date AS local_date) d
      LEFT JOIN tm.report_stream_metric f
             ON f.stream_id = s.stream_id AND f.metric = 'followers'
      LEFT JOIN tm.report_stream_metric l
             ON l.stream_id = s.stream_id AND l.metric = 'likes'
      LEFT JOIN tm.report_stream_metric v
             ON v.stream_id = s.stream_id AND v.metric = 'viewers'
      -- LATERAL and not a correlated scalar subquery, because three columns come
      -- out of one pass over the broadcast's samples.
      CROSS JOIN LATERAL tm.stream_watch_time(s.stream_id) w
     WHERE a.channel_id = p_channel_id
       AND s.first_sample_at <  v_to
       AND s.last_sample_at  >= v_from;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows;
END;
$$;


-- The per-day figures, as an UPDATE over rows tm.refresh_daily_peaks() has
-- already written. A separate function rather than more columns in that one: it
-- is the largest function in the schema, this needs a different scan, and a
-- correction to either should not be able to break the other.
--
-- MUST run after tm.refresh_daily_peaks(), which owns whether the row exists.
CREATE OR REPLACE FUNCTION tm.refresh_daily_watch(
    p_channel_id bigint,
    p_platform   tm.platform_kind,
    p_from       date,
    p_to         date,
    p_tz         text DEFAULT NULL
) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_tz   text;
    v_from timestamptz;
    v_to   timestamptz;
    v_rows integer;
BEGIN
    SELECT coalesce(p_tz, c.report_timezone) INTO v_tz
      FROM tm.channel c WHERE c.channel_id = p_channel_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown channel_id %', p_channel_id;
    END IF;
    IF p_to < p_from THEN
        RAISE EXCEPTION 'empty range: % .. %', p_from, p_to;
    END IF;

    v_from := (p_from::timestamp)     AT TIME ZONE v_tz;
    v_to   := ((p_to + 1)::timestamp) AT TIME ZONE v_tz;

    -- Cleared first and unconditionally, which is the UPDATE equivalent of the
    -- DELETE-then-INSERT the sparse report tables use. A day whose last live
    -- sample has since been corrected away would otherwise keep the watch time
    -- it used to have for ever, and nothing downstream could tell.
    UPDATE tm.report_daily_peak r
       SET watch_minutes = NULL, covered_seconds = NULL
     WHERE r.channel_id = p_channel_id
       AND r.tz         = v_tz
       AND r.platform   = p_platform
       AND r.local_date BETWEEN p_from AND p_to;

    WITH per_day AS (
        -- Driven from tm.stream rather than from tm.sample_all: a channel has
        -- hundreds of broadcasts and millions of samples, and first/last
        -- sample_at are maintained on the stream row exactly so this stays a
        -- lookup. Same argument tm.refresh_stream_trends() makes.
        SELECT w.local_date,
               sum(w.watch_minutes)::numeric(18,3)  AS watch_minutes,
               sum(w.covered_seconds)::integer      AS covered_seconds
          FROM tm.stream s
          JOIN tm.platform_account a ON a.account_id = s.account_id
         CROSS JOIN LATERAL tm.stream_watch_slices(s.stream_id, v_tz) w
         WHERE a.channel_id = p_channel_id
           AND a.platform   = p_platform
           AND s.first_sample_at <  v_to
           AND s.last_sample_at  >= v_from
           -- A broadcast overlapping the range can hold slices outside it; those
           -- belong to the day they fell on and are refreshed with it.
           AND w.local_date BETWEEN p_from AND p_to
         GROUP BY w.local_date)
    UPDATE tm.report_daily_peak r
       SET watch_minutes   = d.watch_minutes,
           covered_seconds = d.covered_seconds
      FROM per_day d
     WHERE r.channel_id = p_channel_id
       AND r.tz         = v_tz
       AND r.platform   = p_platform
       AND r.local_date = d.local_date;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows;
END;
$$;


-- --------------------------------------------------------------------------
-- the entry points, again
-- --------------------------------------------------------------------------
--
-- Both bodies are 007's with one call added. Same reasoning as there: 007 is
-- applied in production, so this is a replacement in a new file and not an edit.

CREATE OR REPLACE FUNCTION tm.refresh_range(
    p_channel_id     bigint,
    p_from           date,
    p_to             date,
    p_tz             text    DEFAULT NULL,
    p_bucket_minutes integer DEFAULT 30
) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    v_platform tm.platform_kind;
    v_tz       text;
    v_from     timestamptz;
    v_to       timestamptz;
    v_stream   bigint;
BEGIN
    SELECT coalesce(p_tz, c.report_timezone) INTO v_tz
      FROM tm.channel c WHERE c.channel_id = p_channel_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown channel_id %', p_channel_id;
    END IF;

    FOR v_platform IN
        SELECT platform FROM tm.platform_account WHERE channel_id = p_channel_id
    LOOP
        PERFORM tm.refresh_daily_peaks(p_channel_id, v_platform, p_from, p_to, v_tz);
        -- Immediately after, and inside the same loop: it updates rows the call
        -- above has just written, and the pair is one logical refresh of the day.
        PERFORM tm.refresh_daily_watch(p_channel_id, v_platform, p_from, p_to, v_tz);
        PERFORM tm.refresh_clock_buckets(p_channel_id, v_platform, p_from, p_to,
                                         p_bucket_minutes, v_tz);
    END LOOP;

    v_from := (p_from::timestamp)     AT TIME ZONE v_tz;
    v_to   := ((p_to + 1)::timestamp) AT TIME ZONE v_tz;

    FOR v_stream IN
        SELECT DISTINCT s.stream_id
          FROM tm.sample_all s
         WHERE s.channel_id = p_channel_id
           AND s.stream_id IS NOT NULL
           AND s.sampled_at >= v_from AND s.sampled_at < v_to
    LOOP
        PERFORM tm.refresh_stream_metrics(v_stream);
    END LOOP;

    PERFORM tm.refresh_stream_trends(p_channel_id, p_from, p_to, v_tz);
END;
$$;


CREATE OR REPLACE FUNCTION tm.refresh_after_sample(
    p_account_id     bigint,
    p_sampled_at     timestamptz,
    p_bucket_minutes integer DEFAULT 30,
    p_stream_id      bigint  DEFAULT NULL
) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    v_channel bigint;
    v_tz      text;
    v_day     date;
    v_loop    date;
BEGIN
    SELECT a.channel_id, c.report_timezone INTO v_channel, v_tz
      FROM tm.platform_account a JOIN tm.channel c USING (channel_id)
     WHERE a.account_id = p_account_id;
    IF v_channel IS NULL THEN
        RETURN;
    END IF;

    v_day := (p_sampled_at AT TIME ZONE v_tz)::date;
    FOR v_loop IN SELECT unnest(ARRAY[v_day - 1, v_day]) LOOP
        PERFORM tm.refresh_daily_peaks(v_channel, a.platform, v_loop, v_loop, v_tz)
           FROM tm.platform_account a WHERE a.channel_id = v_channel;
        PERFORM tm.refresh_daily_watch(v_channel, a.platform, v_loop, v_loop, v_tz)
           FROM tm.platform_account a WHERE a.channel_id = v_channel;
        PERFORM tm.refresh_clock_buckets(v_channel, a.platform, v_loop, v_loop,
                                         p_bucket_minutes, v_tz)
           FROM tm.platform_account a WHERE a.channel_id = v_channel;
    END LOOP;

    IF p_stream_id IS NOT NULL THEN
        PERFORM tm.refresh_stream_metrics(p_stream_id);
    END IF;

    PERFORM tm.refresh_stream_trends(v_channel, v_day - 1, v_day, v_tz);
END;
$$;


-- --------------------------------------------------------------------------
-- reads, for the site generator
-- --------------------------------------------------------------------------

-- 007's tm.stream_trends() with three columns added. CREATE OR REPLACE can add a
-- column to a RETURNS TABLE only by replacing the whole function, and cannot
-- change the shape of one at all -- hence the DROP. Safe because the only caller
-- is store.stream_trends(), which ships in the same commit.
DROP FUNCTION IF EXISTS tm.stream_trends(bigint, tm.platform_kind, date,
                                         integer, integer, text);

CREATE OR REPLACE FUNCTION tm.stream_trends(
    p_channel_id bigint,
    p_platform   tm.platform_kind,
    p_end_day    date,
    p_streams    integer DEFAULT 10,
    p_lookback   integer DEFAULT 90,
    p_tz         text    DEFAULT NULL
) RETURNS TABLE (stream_id bigint, local_date date, started_at timestamptz,
                 weekday smallint, title text, location text,
                 follower_delta integer, likes_peak integer, peak_viewers integer,
                 watch_minutes numeric, covered_seconds integer,
                 span_seconds integer)
LANGUAGE sql STABLE AS $$
    WITH zone AS (
        SELECT coalesce(p_tz, c.report_timezone) AS name
          FROM tm.channel c WHERE c.channel_id = p_channel_id),
    picked AS (
        SELECT r.stream_id, r.local_date, r.started_at, r.weekday, r.title,
               r.location, r.follower_delta, r.likes_peak, r.peak_viewers,
               r.watch_minutes, r.covered_seconds, r.span_seconds
          FROM tm.report_stream_trend r CROSS JOIN zone z
         WHERE r.channel_id = p_channel_id
           AND r.platform   = p_platform
           AND r.tz         = z.name
           AND r.local_date <= p_end_day
           AND r.local_date >  p_end_day - greatest(1, p_lookback)
         ORDER BY r.started_at DESC
         LIMIT greatest(0, p_streams))
    SELECT p.stream_id, p.local_date, p.started_at, p.weekday, p.title,
           p.location, p.follower_delta, p.likes_peak, p.peak_viewers,
           p.watch_minutes, p.covered_seconds, p.span_seconds
      FROM picked p
     ORDER BY p.started_at;
$$;


-- p_metric widens from tm.metric_kind to text, so 'watchtime' can be asked for.
--
-- It is deliberately NOT a new value on the metric_kind enum. That type is
-- documented in 001 as "exactly the keys of chart.METRICS" and is the key of
-- tm.report_stream_metric, whose columns are integers describing a SAMPLED
-- series -- peak, low, first, last. Watch time is none of those things: it is an
-- integral, it is fractional, and it has no peak. Putting it in the enum would
-- make a row in that table representable that nothing could ever fill correctly.
DROP FUNCTION IF EXISTS tm.stream_groups(bigint, tm.platform_kind, tm.metric_kind,
                                         text, date, integer, integer, text);

CREATE OR REPLACE FUNCTION tm.stream_groups(
    p_channel_id bigint,
    p_platform   tm.platform_kind,
    p_metric     text,
    p_grouping   text,
    p_end_day    date,
    p_streams    integer DEFAULT 10,
    p_lookback   integer DEFAULT 90,
    p_tz         text    DEFAULT NULL
) RETURNS TABLE (group_key text, streams integer, total numeric,
                 average numeric, best numeric, best_stream_id bigint)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF p_grouping NOT IN ('weekday', 'location') THEN
        RAISE EXCEPTION 'unknown grouping %; expected weekday or location',
                        p_grouping;
    END IF;
    IF p_metric NOT IN ('followers', 'likes', 'watchtime') THEN
        RAISE EXCEPTION 'unsupported metric %; expected followers, likes or watchtime',
                        p_metric;
    END IF;

    RETURN QUERY
    WITH picked AS (
        SELECT t.stream_id, t.weekday, t.location,
               CASE p_metric WHEN 'followers' THEN t.follower_delta::numeric
                             WHEN 'likes'     THEN t.likes_peak::numeric
                             -- Hours, not minutes. Every caller of this wants a
                             -- number a person reads, and converting in one place
                             -- beats three renderers each remembering to.
                             WHEN 'watchtime' THEN t.watch_minutes / 60.0
                        END AS value
          FROM tm.stream_trends(p_channel_id, p_platform, p_end_day,
                                p_streams, p_lookback, p_tz) t),
    keyed AS (
        SELECT CASE p_grouping WHEN 'weekday' THEN k.weekday::text
                               ELSE coalesce(k.location, '') END AS gkey,
               k.stream_id, k.value
          FROM picked k
         WHERE k.value IS NOT NULL),
    best AS (
        SELECT DISTINCT ON (b.gkey) b.gkey, b.stream_id, b.value
          FROM keyed b
         ORDER BY b.gkey, b.value DESC, b.stream_id ASC)
    SELECT g.gkey, count(*)::integer, sum(g.value)::numeric(18,3),
           avg(g.value)::numeric(18,6), x.value, x.stream_id
      FROM keyed g JOIN best x ON x.gkey = g.gkey
     GROUP BY g.gkey, x.value, x.stream_id
     ORDER BY g.gkey;
END;
$$;


-- store.watch_totals(): watch time per local day, plus the trailing total at each
-- of them.
--
-- DENSE over the window, because it reads tm.report_daily_peak, which has a row
-- for every day in a refreshed range including the ones nobody streamed. A day
-- off arrives with a NULL watch_minutes and the renderer draws a dash -- the same
-- distinction the peaks chart already draws, and for the same reason.
--
-- rolling_minutes is the trailing p_rolling days INCLUDING the row's own, which
-- is what makes the last row of a 365-day window the "past twelve months" figure.
-- RANGE and not ROWS: it is a span of dates, and a day the tables have no row for
-- must not shift the window by one.
CREATE OR REPLACE FUNCTION tm.watch_totals(
    p_channel_id bigint,
    p_platform   tm.platform_kind,
    p_end_day    date,
    p_days       integer DEFAULT 30,
    p_rolling    integer DEFAULT 365,
    p_tz         text    DEFAULT NULL
) RETURNS TABLE (local_date date, status tm.day_status,
                 watch_minutes numeric, covered_seconds integer,
                 rolling_minutes numeric)
LANGUAGE sql STABLE AS $$
    WITH zone AS (
        SELECT coalesce(p_tz, c.report_timezone) AS name
          FROM tm.channel c WHERE c.channel_id = p_channel_id),
    -- Scanned back far enough that the FIRST row of the returned window already
    -- has its whole trailing period behind it. Without the extra reach the
    -- rolling line would climb from zero at the left edge and read as growth.
    scanned AS (
        SELECT r.local_date, r.status, r.watch_minutes, r.covered_seconds
          FROM tm.report_daily_peak r CROSS JOIN zone z
         WHERE r.channel_id = p_channel_id
           AND r.platform   = p_platform
           AND r.tz         = z.name
           AND r.local_date <= p_end_day
           AND r.local_date >  p_end_day - greatest(1, p_days)
                                         - greatest(1, p_rolling)),
    -- Ordered by a DAY NUMBER rather than by the date itself. PostgreSQL refuses
    -- `RANGE ... PRECEDING` over a date column -- the offset would have to be an
    -- interval, and date + interval is a timestamp, so the frame has no in_range
    -- support to use. Subtracting an epoch turns the ordering column into a plain
    -- integer, where an integer offset means exactly what it says. The epoch is
    -- arbitrary and cancels; only the differences are read.
    rolled AS (
        SELECT s.local_date, s.status, s.watch_minutes, s.covered_seconds,
               sum(s.watch_minutes) OVER (
                   ORDER BY (s.local_date - DATE '2000-01-01')
                   RANGE BETWEEN (greatest(1, p_rolling) - 1) PRECEDING
                             AND CURRENT ROW) AS rolling_minutes
          FROM scanned s)
    SELECT r.local_date, r.status, r.watch_minutes, r.covered_seconds,
           r.rolling_minutes
      FROM rolled r
     WHERE r.local_date > p_end_day - greatest(1, p_days)
     ORDER BY r.local_date;
$$;


-- --------------------------------------------------------------------------
-- the backfill
-- --------------------------------------------------------------------------
--
-- Every column added above is NULL on every existing row, and nothing would ever
-- fill them on its own: store.ensure_reports() judges a window by how many DATES
-- tm.report_daily_peak holds, and it holds all of them already -- so it would
-- report the window healthy and refresh only its tail, for ever. 007 hit exactly
-- this and left the same note.
--
-- One pass of tm.refresh_range() over each channel's whole span fills the lot,
-- which is what db_cmd's --rebuild now does on demand.
DO $backfill$
DECLARE
    v_span record;
BEGIN
    FOR v_span IN
        SELECT s.channel_id,
               min(s.sampled_at AT TIME ZONE c.report_timezone)::date AS first_day,
               max(s.sampled_at AT TIME ZONE c.report_timezone)::date AS last_day
          FROM tm.sample_all s
          JOIN tm.channel c ON c.channel_id = s.channel_id
         GROUP BY s.channel_id
    LOOP
        PERFORM tm.refresh_range(v_span.channel_id, v_span.first_day,
                                 v_span.last_day, NULL, 30);
    END LOOP;
END;
$backfill$;
