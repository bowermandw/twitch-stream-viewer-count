-- The aggregates the website is built from, and the functions that fill them.
--
-- Every function here takes its window and its timezone as PARAMETERS rather
-- than reading them from anywhere, which is the point: a new trend is a call
-- with different arguments, not new Python and not a new migration.
--
-- The timezone rule from 001_schema.sql applies to all of them. Convert the
-- parameters once, into a half-open range of instants, and never put
-- AT TIME ZONE on the indexed column in a WHERE clause -- doing that makes the
-- predicate unusable by the primary key, so a once-a-minute refresh would
-- seq-scan the channel's whole history every minute and get slower every day.

SET LOCAL search_path = tm, public;


-- --------------------------------------------------------------------------
-- peak viewers per day
-- --------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION tm.refresh_daily_peaks(
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

    v_from := (p_from::timestamp)       AT TIME ZONE v_tz;
    v_to   := ((p_to + 1)::timestamp)   AT TIME ZONE v_tz;

    WITH
    -- Pure date arithmetic, and deliberately NOT
    --     generate_series(p_from, p_to, interval '1 day')
    -- which resolves to the timestamptz overload and drags the SESSION's
    -- TimeZone into an axis that must depend only on p_tz.
    day_axis AS (
        SELECT (p_from + g)::date AS local_date
          FROM generate_series(0, p_to - p_from) AS g
    ),
    -- One pass, offline rows included: 'dark' (polled, offline) has to stay
    -- distinguishable from 'silent' (never polled), because daily.py treats a
    -- silent day today as a dead poller and fails the run over it.
    graded AS (
        SELECT (s.sampled_at AT TIME ZONE v_tz)::date AS local_date,
               s.sampled_at, s.stream_id, s.viewer_count,
               s.follower_count, s.subscriber_count
          FROM tm.sample_all s
         WHERE s.channel_id = p_channel_id
           AND s.platform   = p_platform
           AND s.sampled_at >= v_from
           AND s.sampled_at <  v_to
    ),
    per_day AS (
        SELECT local_date,
               count(*)                          AS sample_count,
               count(stream_id)                  AS live_sample_count,
               count(viewer_count)               AS viewer_sample_count,
               max(viewer_count)                 AS peak_viewers,
               min(viewer_count)                 AS low_viewers,
               avg(viewer_count)::numeric(18,6)  AS avg_viewers,
               min(sampled_at) FILTER (WHERE stream_id IS NOT NULL) AS first_live_at,
               max(sampled_at) FILTER (WHERE stream_id IS NOT NULL) AS last_live_at,
               -- Across the WHOLE day, live rows and dark ones alike. The
               -- pollers record these while a channel is offline on purpose,
               -- and a delta that ignored those rows would lose every follower
               -- gained on a day off.
               (array_agg(follower_count ORDER BY sampled_at)
                  FILTER (WHERE follower_count IS NOT NULL))[1]   AS follower_first,
               (array_agg(follower_count ORDER BY sampled_at DESC)
                  FILTER (WHERE follower_count IS NOT NULL))[1]   AS follower_last,
               (array_agg(subscriber_count ORDER BY sampled_at)
                  FILTER (WHERE subscriber_count IS NOT NULL))[1] AS subscriber_first,
               (array_agg(subscriber_count ORDER BY sampled_at DESC)
                  FILTER (WHERE subscriber_count IS NOT NULL))[1] AS subscriber_last
          FROM graded
         GROUP BY local_date
    ),
    -- EARLIEST wins a tie. Python's max(same_day, key=...) returns the first
    -- maximum and summarise() does the same, so resolving ties the other way
    -- would move the "at 8:32 pm" label under the chart for no reason.
    peak_at AS (
        SELECT DISTINCT ON (local_date) local_date, sampled_at AS peak_at
          FROM graded
         WHERE viewer_count IS NOT NULL
         ORDER BY local_date, viewer_count DESC, sampled_at ASC
    ),
    -- daily.day_title(): the LAST non-empty title of the day. With the title on
    -- the stream row, that is the title of whichever broadcast owns the day's
    -- last live sample.
    day_title AS (
        SELECT DISTINCT ON (g.local_date) g.local_date, st.title
          FROM graded g
          JOIN tm.stream st ON st.stream_id = g.stream_id
         WHERE g.stream_id IS NOT NULL AND coalesce(st.title, '') <> ''
         ORDER BY g.local_date, g.sampled_at DESC
    )
    INSERT INTO tm.report_daily_peak AS r (
        channel_id, tz, local_date, platform, status,
        peak_viewers, peak_at, avg_viewers, low_viewers,
        sample_count, live_sample_count, viewer_sample_count,
        first_live_at, last_live_at, title,
        follower_first, follower_last, subscriber_first, subscriber_last, computed_at)
    -- THE left join. day_axis drives, so a day with no samples still produces a
    -- row -- and because it arrives by LEFT JOIN, every aggregate on it is NULL
    -- rather than the 0 that coalescing would have handed back. That is what
    -- makes a day off render as a dash instead of as a catastrophe.
    SELECT p_channel_id, v_tz, a.local_date, p_platform,
           CASE WHEN d.local_date IS NULL                 THEN 'silent'::tm.day_status
                WHEN coalesce(d.live_sample_count, 0) = 0 THEN 'dark'::tm.day_status
                ELSE 'live'::tm.day_status END,
           d.peak_viewers, pa.peak_at, d.avg_viewers, d.low_viewers,
           coalesce(d.sample_count, 0),
           coalesce(d.live_sample_count, 0),
           coalesce(d.viewer_sample_count, 0),
           d.first_live_at, d.last_live_at, t.title,
           d.follower_first, d.follower_last, d.subscriber_first, d.subscriber_last,
           now()
      FROM day_axis a
      LEFT JOIN per_day   d  ON d.local_date  = a.local_date
      LEFT JOIN peak_at   pa ON pa.local_date = a.local_date
      LEFT JOIN day_title t  ON t.local_date  = a.local_date
    ON CONFLICT (channel_id, tz, local_date, platform) DO UPDATE SET
        status = EXCLUDED.status,
        peak_viewers = EXCLUDED.peak_viewers, peak_at = EXCLUDED.peak_at,
        avg_viewers = EXCLUDED.avg_viewers, low_viewers = EXCLUDED.low_viewers,
        sample_count = EXCLUDED.sample_count,
        live_sample_count = EXCLUDED.live_sample_count,
        viewer_sample_count = EXCLUDED.viewer_sample_count,
        first_live_at = EXCLUDED.first_live_at, last_live_at = EXCLUDED.last_live_at,
        title = EXCLUDED.title,
        follower_first = EXCLUDED.follower_first, follower_last = EXCLUDED.follower_last,
        subscriber_first = EXCLUDED.subscriber_first,
        subscriber_last = EXCLUDED.subscriber_last,
        computed_at = now();

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows;
END;
$$;


-- --------------------------------------------------------------------------
-- average viewers per clock bucket
-- --------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION tm.refresh_clock_buckets(
    p_channel_id     bigint,
    p_platform       tm.platform_kind,
    p_from           date,
    p_to             date,
    p_bucket_minutes integer DEFAULT 30,
    p_tz             text    DEFAULT NULL
) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_tz    text;
    v_from  timestamptz;
    v_to    timestamptz;
    -- trends.clock_buckets() does width = max(1, int(minutes)). The same clamp
    -- in the same place, so the buckets and the labels fmt_slot() prints can
    -- never disagree about how wide a slot is.
    v_width integer := greatest(1, least(1440, p_bucket_minutes));
    v_rows  integer;
BEGIN
    SELECT coalesce(p_tz, c.report_timezone) INTO v_tz
      FROM tm.channel c WHERE c.channel_id = p_channel_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown channel_id %', p_channel_id;
    END IF;

    v_from := (p_from::timestamp)     AT TIME ZONE v_tz;
    v_to   := ((p_to + 1)::timestamp) AT TIME ZONE v_tz;

    -- DELETE then INSERT rather than an upsert, because the result set is
    -- SPARSE: a correction that removes samples has to be able to remove a
    -- slot, and an upsert would leave the stale bucket standing for ever.
    DELETE FROM tm.report_clock_bucket r
     WHERE r.channel_id = p_channel_id AND r.platform = p_platform
       AND r.tz = v_tz AND r.bucket_minutes = v_width
       AND r.local_date >= p_from AND r.local_date <= p_to;

    INSERT INTO tm.report_clock_bucket (
        channel_id, tz, bucket_minutes, local_date, platform, slot,
        avg_viewers, peak_viewers, sample_count, computed_at)
    SELECT p_channel_id, v_tz, v_width, x.local_date, p_platform, x.slot,
           avg(x.viewer_count)::numeric(18,6), max(x.viewer_count),
           count(*), now()
      FROM (
        SELECT (s.sampled_at AT TIME ZONE v_tz)::date AS local_date,
               -- slot = (local.hour * 60 + local.minute) // width, exactly.
               -- Seconds are dropped the way Python drops them, and the
               -- division is integer -- so a width that does not divide 1440
               -- leaves a SHORT final slot rather than wrapping into slot 0,
               -- which is what the Python does, wart included.
               -- The ::int casts matter: extract() returns numeric on PG 14+,
               -- and without them every slot comes out fractional.
               (( extract(hour   FROM s.sampled_at AT TIME ZONE v_tz)::int * 60
                + extract(minute FROM s.sampled_at AT TIME ZONE v_tz)::int )
                / v_width)::smallint AS slot,
               s.viewer_count
          FROM tm.sample_all s
         WHERE s.channel_id = p_channel_id
           AND s.platform   = p_platform
           AND s.sampled_at >= v_from
           AND s.sampled_at <  v_to
           -- trends._live_on() is "live AND viewers is not None". The schema's
           -- viewers_live CHECK makes the first half redundant: a viewer count
           -- cannot exist without a stream.
           AND s.viewer_count IS NOT NULL
      ) x
     GROUP BY x.local_date, x.slot;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows;
END;
$$;


-- --------------------------------------------------------------------------
-- per-broadcast summaries
-- --------------------------------------------------------------------------

-- chart.summarise() for one broadcast, every metric it carries.
--
-- Long format, so the set of metrics is data. available_metrics() drops the
-- ones a platform has no numbers for, and so does the WHERE below -- a Twitch
-- broadcast simply produces no 'subscribers' row.
CREATE OR REPLACE FUNCTION tm.refresh_stream_metrics(p_stream_id bigint)
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_rows integer;
BEGIN
    DELETE FROM tm.report_stream_metric WHERE stream_id = p_stream_id;

    INSERT INTO tm.report_stream_metric (
        stream_id, metric, peak, peak_at, peak_elapsed, low, avg,
        first_value, last_value, n, computed_at)
    WITH points AS (
        SELECT m.metric, s.sampled_at, m.value,
               min(s.sampled_at) OVER (PARTITION BY m.metric) AS started
          FROM tm.sample_all s
         CROSS JOIN LATERAL (VALUES
                ('viewers'::tm.metric_kind,     s.viewer_count),
                ('chatters'::tm.metric_kind,    s.chatter_count),
                ('followers'::tm.metric_kind,   s.follower_count),
                ('subscribers'::tm.metric_kind, s.subscriber_count),
                ('likes'::tm.metric_kind,       s.like_count)
            ) AS m(metric, value)
         WHERE s.stream_id = p_stream_id AND m.value IS NOT NULL
    ),
    -- Earliest wins a tie, matching Python's
    -- next(t for t, v in points if v == peak).
    peaks AS (
        SELECT DISTINCT ON (metric) metric, sampled_at AS peak_at,
               extract(epoch FROM sampled_at - started)::integer AS peak_elapsed
          FROM points ORDER BY metric, value DESC, sampled_at ASC
    )
    SELECT p.metric, max(p.value), pk.peak_at, pk.peak_elapsed,
           min(p.value), avg(p.value)::numeric(18,6),
           (array_agg(p.value ORDER BY p.sampled_at))[1],
           (array_agg(p.value ORDER BY p.sampled_at DESC))[1],
           count(*), now()
      FROM points p JOIN peaks pk USING (metric)
     GROUP BY p.metric, pk.peak_at, pk.peak_elapsed;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows;
END;
$$;


-- --------------------------------------------------------------------------
-- the entry points
-- --------------------------------------------------------------------------

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
BEGIN
    FOR v_platform IN
        SELECT platform FROM tm.platform_account WHERE channel_id = p_channel_id
    LOOP
        PERFORM tm.refresh_daily_peaks(p_channel_id, v_platform, p_from, p_to, p_tz);
        PERFORM tm.refresh_clock_buckets(p_channel_id, v_platform, p_from, p_to,
                                         p_bucket_minutes, p_tz);
    END LOOP;
END;
$$;


CREATE OR REPLACE FUNCTION tm.refresh_day(
    p_channel_id     bigint,
    p_local_date     date,
    p_tz             text    DEFAULT NULL,
    p_bucket_minutes integer DEFAULT 30
) RETURNS void
LANGUAGE sql AS $$
    SELECT tm.refresh_range(p_channel_id, p_local_date, p_local_date,
                            p_tz, p_bucket_minutes);
$$;


-- What a poller calls after storing a sample.
--
-- TWO days, not one. A broadcast that straddles local midnight changes
-- yesterday's last_live_at and its follower_last, so a refresh that only ever
-- touched "today" would leave yesterday frozen at 23:59 for ever.
CREATE OR REPLACE FUNCTION tm.refresh_after_sample(
    p_account_id     bigint,
    p_sampled_at     timestamptz,
    p_bucket_minutes integer DEFAULT 30
) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    v_channel bigint;
    v_tz      text;
    v_day     date;
BEGIN
    SELECT a.channel_id, c.report_timezone INTO v_channel, v_tz
      FROM tm.platform_account a JOIN tm.channel c USING (channel_id)
     WHERE a.account_id = p_account_id;
    IF v_channel IS NULL THEN
        RETURN;
    END IF;
    v_day := (p_sampled_at AT TIME ZONE v_tz)::date;
    PERFORM tm.refresh_range(v_channel, v_day - 1, v_day, v_tz, p_bucket_minutes);
    PERFORM tm.refresh_stream_metrics(s.stream_id)
       FROM tm.stream s
      WHERE s.account_id = p_account_id AND s.ended_at IS NULL;
END;
$$;


-- --------------------------------------------------------------------------
-- reads, for the site generator
-- --------------------------------------------------------------------------

-- trends.daily_peaks(): one row per day in the window, oldest first.
--
-- The day axis is generated here as well as in the refresh, so asking for a
-- window wider than has ever been refreshed returns days rather than silently
-- returning fewer rows than asked for. peak_viewers stays NULL on those, which
-- is the same thing "did not stream" looks like -- `status` is what tells them
-- apart.
CREATE OR REPLACE FUNCTION tm.daily_peaks(
    p_channel_id bigint,
    p_platform   tm.platform_kind,
    p_end_day    date,
    p_days       integer DEFAULT 10,
    p_tz         text    DEFAULT NULL
) RETURNS TABLE (local_date date, status tm.day_status, peak_viewers integer,
                 peak_at timestamptz, avg_viewers numeric, title text)
LANGUAGE sql STABLE AS $$
    WITH zone AS (
        SELECT coalesce(p_tz, c.report_timezone) AS name
          FROM tm.channel c WHERE c.channel_id = p_channel_id),
    axis AS (
        SELECT (p_end_day - g)::date AS local_date
          FROM generate_series(0, greatest(p_days, 1) - 1) AS g)
    SELECT a.local_date,
           coalesce(r.status, 'silent'::tm.day_status),
           r.peak_viewers, r.peak_at, r.avg_viewers, r.title
      FROM axis a
      CROSS JOIN zone z
      LEFT JOIN tm.report_daily_peak r
             ON r.channel_id = p_channel_id AND r.platform = p_platform
            AND r.tz = z.name AND r.local_date = a.local_date
     ORDER BY a.local_date;
$$;


-- trends.compare_slots(): today and the days before it, trimmed to the busiest
-- contiguous stretch.
--
-- Returns every (day, slot) that carried a live sample, plus the counts the
-- caller needs to say "busiest 12 hours shown" rather than silently showing
-- less. Empty days are absent by design -- render_typical() sizes its bars by
-- the number of days asked for and draws a legend entry only for days that
-- have buckets -- so the caller builds the day axis and this fills it.
CREATE OR REPLACE FUNCTION tm.compare_slots(
    p_channel_id     bigint,
    p_platform       tm.platform_kind,
    p_end_day        date,
    p_days           integer DEFAULT 5,
    p_bucket_minutes integer DEFAULT 30,
    p_max_slots      integer DEFAULT 24,
    p_tz             text    DEFAULT NULL
) RETURNS TABLE (local_date date, slot smallint, avg_viewers numeric,
                 used_slots integer, dropped_slots integer)
LANGUAGE sql STABLE AS $$
    WITH zone AS (
        SELECT coalesce(p_tz, c.report_timezone) AS name
          FROM tm.channel c WHERE c.channel_id = p_channel_id),
    bucket AS (
        SELECT b.local_date, b.slot, b.avg_viewers
          FROM tm.report_clock_bucket b CROSS JOIN zone z
         WHERE b.channel_id = p_channel_id AND b.platform = p_platform
           AND b.tz = z.name
           AND b.bucket_minutes = greatest(1, least(1440, p_bucket_minutes))
           AND b.local_date >  p_end_day - greatest(p_days, 0) - 1
           AND b.local_date <= p_end_day),
    -- weight[slot] = the SUM of the per-day averages, not their mean. A slot
    -- used on six days therefore outweighs one used on a single day, which is
    -- the point: the window follows where the channel habitually streams.
    weight AS (SELECT slot, sum(avg_viewers) AS w FROM bucket GROUP BY slot),
    dense AS (
        -- ::int on both bounds, because generate_series(smallint, smallint)
        -- is ambiguous -- the planner cannot choose between the integer and
        -- the numeric overloads.
        SELECT g::smallint AS slot, coalesce(w.w, 0) AS w
          FROM generate_series((SELECT min(slot)::int FROM weight),
                               (SELECT max(slot)::int FROM weight)) g
          LEFT JOIN weight w ON w.slot = g::smallint),
    -- A self-join rather than a windowed rolling sum, because PostgreSQL will
    -- not accept a function parameter as a ROWS ... FOLLOWING frame offset.
    -- At most 48 x 24 rows, so the shape costs nothing.
    --
    -- Python only tries starts where the window still FITS inside
    -- [first, last]. A window hanging off the end is a suffix of the last
    -- fitted one and, with non-negative weights, can only tie it -- and ties go
    -- to the smaller slot, which is the fitted one. So the two agree.
    scored AS (
        SELECT s.slot AS start, sum(d.w) AS total
          FROM dense s JOIN dense d
            ON d.slot >= s.slot AND d.slot < s.slot + greatest(p_max_slots, 1)
         GROUP BY s.slot),
    best AS (SELECT start FROM scored ORDER BY total DESC, start ASC LIMIT 1),
    used AS (SELECT count(*)::integer AS n FROM weight),
    kept AS (
        SELECT w.slot FROM weight w CROSS JOIN best b CROSS JOIN used u
         WHERE u.n <= greatest(p_max_slots, 1)
            OR (w.slot >= b.start AND w.slot < b.start + greatest(p_max_slots, 1)))
    SELECT b.local_date, b.slot, b.avg_viewers,
           (SELECT n FROM used),
           (SELECT n FROM used) - (SELECT count(*)::integer FROM kept)
      FROM bucket b JOIN kept k ON k.slot = b.slot
     ORDER BY b.local_date, b.slot;
$$;
