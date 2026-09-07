-- Location Trends: the broadcasts behind a venue's bar.
--
-- 007 gave every broadcast a location and rolled it up; 009 gave every venue an
-- all-time watch-hour average. Both answer "which of these places is worth going
-- back to" with ONE BAR PER VENUE, and a bar is a mean. This project already
-- refuses that trade elsewhere -- the peaks chart keeps five separate days rather
-- than a five-day average, because "an average hides its own spread; one freak
-- evening drags normal up and nothing on the chart says so". A venue's average is
-- the same shape of claim, and until now there was no way to look underneath it.
--
-- What was missing is narrow. tm.report_stream_trend already has every column a
-- per-venue chart needs, one row per broadcast, with location denormalised onto
-- it. What it had no read path for is "the last ten broadcasts AT THIS VENUE".
--
-- THAT IS NOT A FILTER OVER tm.stream_trends()' RESULT. That function takes the
-- channel's last N broadcasts and stops; filtering afterwards gives however many
-- of the channel's last ten happened to be there -- three, or none -- and calls
-- that "the last ten at Epcot". The venue predicate has to sit INSIDE the LIMIT.
--
-- WHY A PARAMETER AND NOT A SECOND FUNCTION. The obvious shape is a sibling
-- tm.location_streams() returning tm.stream_trends()' columns. It is the wrong
-- shape for one decisive reason: tm.stream_groups() is DEFINED in terms of
-- tm.stream_trends(). A filter added at the bottom of the stack is inherited by
-- every rollup above it and CANNOT disagree with them -- which is exactly the
-- property 007 was buying when it built stream_groups() on stream_trends()
-- rather than beside it, so "the bar chart and the rollup under it always
-- summarise the identical set of broadcasts". A sibling function would have
-- meant a second copy of the rollup body too, and two copies of an aggregate is
-- the drift 009's parity oracle exists to catch.
--
-- The cost is two DROPs, because a parameter changes a signature. 008 paid the
-- same price to widen p_metric and for the same kind of reason.
--
-- WHY NO TABLE, given that 007, 008 and 009 each ended in one. 009's rule was
-- about WINDOWS, not about locations:
--
--     A rollup is scoped to "the last N broadcasts", N is a command-line flag,
--     and a table keyed without N would be quietly wrong the first time anyone
--     passed --stream-count 20.
--
-- and it earned its table by being all-time, dissolving the N rather than arguing
-- with it. Everything here is either windowed by N -- so a table is forbidden by
-- that same rule -- or is a cheap aggregate over one channel's rows. This is a
-- channel with hundreds of broadcasts, not millions, and
-- report_stream_trend_recent already indexes (channel_id, tz, platform,
-- started_at DESC). Read functions all the way down.
--
-- SO THIS FILE HAS NO BACKFILL BLOCK, and its absence is deliberate rather than
-- an oversight. 007, 008 and 009 each shipped a DO $backfill$ because each
-- introduced STORAGE -- a new table or a new column -- that store.ensure_reports()
-- is structurally blind to: it judges a window by how many dates
-- tm.report_daily_peak holds, and a brand new table is healthy-looking and empty
-- for ever. 010 introduces no table and no column. There is nothing to be blind
-- to.


-- --------------------------------------------------------------------------
-- one broadcast per row, optionally at one venue
-- --------------------------------------------------------------------------

-- Two additions, both at the END of the argument list, so every existing call
-- site keeps working on position alone and only the DROP is needed.
--
--   p_location  NULL means EVERY venue -- the behaviour every caller had before
--               this file. '' means the venue that is no venue: the broadcasts
--               whose title matched no rule and whose location is NULL. Those
--               are different questions and both have to be askable. '' is
--               already how tm.stream_groups() and tm.location_watch() hand an
--               unmatched location back, so a caller can ask for what it was
--               given without learning a second convention.
--
--   p_streams / p_lookback NULL mean UNBOUNDED. The venue-history chart draws a
--               venue's whole record -- "is this place getting better or worse"
--               is a question about all of it -- and the alternative is a caller
--               passing 999999 to mean "all", which states a window it does not
--               mean and has a cliff in it: p_end_day - greatest(1, 2147483647)
--               raises `date out of range`.
--
-- BOTH NULL BRANCHES ARE SPELLED OUT rather than folded into the existing
-- greatest() clamps, because GREATEST IGNORES NULLS. greatest(0, NULL) is 0, not
-- NULL, so the obvious spelling would return no rows at all; greatest(1, NULL)
-- is 1, so the other would silently make "all time" mean "since yesterday".
-- Either reads as a channel that has never streamed.
DROP FUNCTION IF EXISTS tm.stream_trends(bigint, tm.platform_kind, date,
                                         integer, integer, text);

CREATE OR REPLACE FUNCTION tm.stream_trends(
    p_channel_id bigint,
    p_platform   tm.platform_kind,
    p_end_day    date,
    p_streams    integer DEFAULT 10,
    p_lookback   integer DEFAULT 90,
    p_tz         text    DEFAULT NULL,
    p_location   text    DEFAULT NULL
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
           AND (p_lookback IS NULL
                OR r.local_date > p_end_day - greatest(1, p_lookback))
           -- Inside the LIMIT, which is the whole point. Outside it this would
           -- be "however many of the last ten were here".
           AND (p_location IS NULL
                OR coalesce(r.location, '') = p_location)
         ORDER BY r.started_at DESC
         LIMIT CASE WHEN p_streams IS NULL THEN NULL
                    ELSE greatest(0, p_streams) END)
    SELECT p.stream_id, p.local_date, p.started_at, p.weekday, p.title,
           p.location, p.follower_delta, p.likes_peak, p.peak_viewers,
           p.watch_minutes, p.covered_seconds, p.span_seconds
      FROM picked p
     ORDER BY p.started_at;
$$;


-- --------------------------------------------------------------------------
-- the rollup, gaining a metric and inheriting the venue
-- --------------------------------------------------------------------------

-- 'peak' JOINS THE GATE AND THE CASE. peak_viewers has been on
-- tm.report_stream_trend since 007 and on tm.stream_trends()' output since 008,
-- so nothing below this had to change to allow it -- it was simply never
-- selectable, and the only peak-viewers chart on the site is per DAY. Per day
-- and per broadcast are not the same axis: a Saturday spent at two parks is one
-- bar on the daily chart and two here, and only the second can be grouped by
-- venue.
--
-- Still NOT a value on tm.metric_kind, and 008's reason now cuts one step
-- deeper. That enum is the key of tm.report_stream_metric, whose columns describe
-- a sampled series -- and peak viewers IS such a series; it is already in there
-- as 'viewers'. Adding 'peak' would be a second name for a row that already
-- exists. p_metric has been plain text since 008 precisely so this argument does
-- not have to be had.
--
-- p_location IS FORWARDED, NOT RE-IMPLEMENTED. A weekday rollup restricted to
-- one venue has to summarise exactly the broadcasts that venue's own bars draw,
-- and one window feeding both is the only way to guarantee it.
DROP FUNCTION IF EXISTS tm.stream_groups(bigint, tm.platform_kind, text, text,
                                         date, integer, integer, text);

CREATE OR REPLACE FUNCTION tm.stream_groups(
    p_channel_id bigint,
    p_platform   tm.platform_kind,
    p_metric     text,
    p_grouping   text,
    p_end_day    date,
    p_streams    integer DEFAULT 10,
    p_lookback   integer DEFAULT 90,
    p_tz         text    DEFAULT NULL,
    p_location   text    DEFAULT NULL
) RETURNS TABLE (group_key text, streams integer, total numeric,
                 average numeric, best numeric, best_stream_id bigint)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF p_grouping NOT IN ('weekday', 'location') THEN
        RAISE EXCEPTION 'unknown grouping %; expected weekday or location',
                        p_grouping;
    END IF;
    IF p_metric NOT IN ('followers', 'likes', 'watchtime', 'peak') THEN
        RAISE EXCEPTION 'unsupported metric %; expected followers, likes, '
                        'watchtime or peak', p_metric;
    END IF;

    RETURN QUERY
    WITH picked AS (
        SELECT t.stream_id, t.weekday, t.location,
               CASE p_metric WHEN 'followers' THEN t.follower_delta::numeric
                             WHEN 'likes'     THEN t.likes_peak::numeric
                             WHEN 'peak'      THEN t.peak_viewers::numeric
                             -- Hours, not minutes. Every caller of this wants a
                             -- number a person reads, and converting in one place
                             -- beats three renderers each remembering to.
                             WHEN 'watchtime' THEN t.watch_minutes / 60.0
                        END AS value
          FROM tm.stream_trends(p_channel_id, p_platform, p_end_day,
                                p_streams, p_lookback, p_tz, p_location) t),
    -- A broadcast the metric was never sampled on is dropped rather than counted
    -- as a zero: it would drag every average it touched towards nothing and the
    -- chart would say the day was bad when the poller was.
    keyed AS (
        SELECT CASE p_grouping WHEN 'weekday' THEN k.weekday::text
                               ELSE coalesce(k.location, '') END AS gkey,
               k.stream_id, k.value
          FROM picked k
         WHERE k.value IS NOT NULL),
    -- Earliest stream wins a tie, the tie-break every other report function uses.
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


-- --------------------------------------------------------------------------
-- which venues exist at all
-- --------------------------------------------------------------------------

-- store.stream_locations(): every venue the channel has on record, so the
-- publisher knows which pages to write and the picker knows what to list.
--
-- ACROSS PLATFORMS, unlike every other read in 007-010. Those all take a platform
-- because they draw a chart and a chart is per-platform. This one answers "which
-- pages does this channel need", and a venue visited only on YouTube needs one
-- exactly as much as a venue visited on both -- a location page carries both
-- platforms' panels and the renderers self-select by drawing nothing. Taking a
-- platform here would give the site two different pickers.
--
-- IT READS tm.report_stream_trend AND NOT tm.report_location_watch, which would
-- have been the shorter query. That table only holds venues with a watch
-- estimate, and a venue whose broadcasts predate viewer sampling is absent from
-- it -- while its follower and peak charts still have something to say. A page
-- is owed to anywhere the channel streamed, not only to anywhere it can be
-- credited hours for.
--
-- Busiest first, which is the picker's order and is deliberately a DIFFERENT
-- "busiest" from the charts': this counts broadcasts, where every chart axis
-- sorts by the average of whichever metric it draws. One row serving every
-- panel cannot follow a per-panel ordering, and a picker that reshuffled itself
-- when the reader looked at a different chart would be worse than useless. Name
-- breaks a tie so the order is stable between runs.
CREATE OR REPLACE FUNCTION tm.stream_locations(
    p_channel_id bigint,
    p_tz         text DEFAULT NULL
) RETURNS TABLE (location text, streams integer,
                 first_local_date date, last_local_date date,
                 platforms text[])
LANGUAGE sql STABLE AS $$
    WITH zone AS (
        SELECT coalesce(p_tz, c.report_timezone) AS name
          FROM tm.channel c WHERE c.channel_id = p_channel_id)
    SELECT coalesce(r.location, ''), count(*)::integer,
           min(r.local_date), max(r.local_date),
           array_agg(DISTINCT r.platform::text ORDER BY r.platform::text)
      FROM tm.report_stream_trend r CROSS JOIN zone z
     WHERE r.channel_id = p_channel_id
       AND r.tz         = z.name
     GROUP BY coalesce(r.location, '')
     ORDER BY count(*) DESC, coalesce(r.location, '');
$$;
