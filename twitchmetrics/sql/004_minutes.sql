-- The cross-platform minute grid: chart.align_platforms(), in SQL.
--
-- This is the last thing standing between "the website is built by reading the
-- database" and reality. It is also the only chart that needs both sample
-- tables at once, which in Python means daily.py reading two sources and
-- passing dicts around, and here is a UNION ALL that already exists as a view.
--
-- What it stores is the carry-forward and its PROVENANCE -- the value, where it
-- came from, how stale it is, and that platform's own polling interval -- and
-- not the freshness verdict. The tolerance (GAP_TOLERANCE = 2.5) lives in
-- chart.py next to the palette because it is a presentation decision, and
-- baking it into stored rows would turn changing it into a backfill. Applying
-- it at read time keeps it a parameter.
--
-- `combined` is computed here rather than stored for a related reason: "every
-- platform is known" is data-dependent, so a channel that starts a YouTube
-- poller at noon changes what a combined total MEANS for that morning. It has
-- to be recomputed against whatever platform set the caller cares about.

SET LOCAL search_path = tm, public;

CREATE OR REPLACE FUNCTION tm.channel_minutes(
    p_channel_id    bigint,
    p_day           date,
    p_tz            text    DEFAULT NULL,
    p_gap_tolerance numeric DEFAULT 2.5
) RETURNS TABLE (minute_at timestamptz, platform tm.platform_kind,
                 viewers integer, combined_viewers integer,
                 source_sampled_at timestamptz, age_seconds numeric,
                 median_gap_seconds numeric)
LANGUAGE sql STABLE AS $$
WITH zone AS (
    SELECT coalesce(p_tz, c.report_timezone) AS name
      FROM tm.channel c WHERE c.channel_id = p_channel_id),
bounds AS (
    SELECT (p_day::timestamp)     AT TIME ZONE z.name AS lo,
           ((p_day + 1)::timestamp) AT TIME ZONE z.name AS hi
      FROM zone z),
-- Exactly what daily.day_points() hands to align_platforms(): live samples that
-- actually carry a number. The viewers_live CHECK makes "live" redundant here --
-- a viewer count cannot exist without a stream.
pt AS (
    SELECT s.platform, s.sampled_at, s.viewer_count,
           date_trunc('minute', s.sampled_at) AS minute_at
      FROM tm.sample_all s CROSS JOIN bounds b
     WHERE s.channel_id = p_channel_id
       AND s.sampled_at >= b.lo AND s.sampled_at < b.hi
       AND s.viewer_count IS NOT NULL),
-- chart._sample_gap(): gaps sorted, then gaps[len(gaps) // 2]. On an even count
-- that is the UPPER median, which is neither percentile_cont (interpolates) nor
-- percentile_disc (takes the lower) -- for [10,20,30,40] Python gives 30 where
-- they give 25 and 20. Indexing the sorted array reproduces it exactly.
-- 60.0 is the Python default for a platform with a single sample and no gaps.
gap AS (
    SELECT platform,
           coalesce((array_agg(secs ORDER BY secs))[count(*) / 2 + 1], 60.0) AS median_gap
      FROM (SELECT platform,
                   extract(epoch FROM sampled_at
                       - lag(sampled_at) OVER (PARTITION BY platform
                                               ORDER BY sampled_at))::numeric AS secs
              FROM pt) q
     WHERE secs IS NOT NULL
     GROUP BY platform),
part AS (
    SELECT p.platform, coalesce(g.median_gap, 60.0) AS median_gap
      FROM (SELECT DISTINCT platform FROM pt) p
      LEFT JOIN gap g USING (platform)),
-- A CONTINUOUS minute axis from the first sampled minute to the last, and not
-- the union of minutes that have samples: an hour with no data at all has to
-- exist as a hole, or the line is drawn straight across it as though the
-- audience had drifted rather than the poller died.
grid AS (
    SELECT generate_series((SELECT min(minute_at) FROM pt),
                           (SELECT max(minute_at) FROM pt),
                           interval '1 minute') AS minute_at),
carried AS (
    SELECT g.minute_at, p.platform, p.median_gap,
           -- The last sample at or before this minute; before the first one
           -- there is none, and Python leaves its cursor on points[0] -- so the
           -- first sample stands in, and the abs() below lets it reach BACK one
           -- tolerance as well as forward. Reproduced rather than dropped,
           -- because it is what the existing charts already show.
           coalesce(prev.sampled_at, firstpt.sampled_at)   AS sampled_at,
           coalesce(prev.viewer_count, firstpt.viewer_count) AS viewer_count
      FROM grid g
     CROSS JOIN part p
      LEFT JOIN LATERAL (
        SELECT x.viewer_count, x.sampled_at
          FROM pt x
         WHERE x.platform = p.platform
           -- The RAW timestamp, not the truncated minute: Python compares
           -- points[index + 1][0] <= minute, so a sample at 12:00:30 does not
           -- satisfy minute 12:00.
           AND x.sampled_at <= g.minute_at
         ORDER BY x.sampled_at DESC LIMIT 1) prev ON true
      LEFT JOIN LATERAL (
        SELECT x.viewer_count, x.sampled_at
          FROM pt x WHERE x.platform = p.platform
         ORDER BY x.sampled_at ASC LIMIT 1) firstpt ON true),
fresh AS (
    SELECT c.minute_at, c.platform, c.sampled_at, c.median_gap,
           abs(extract(epoch FROM c.minute_at - c.sampled_at))::numeric AS age,
           CASE WHEN c.sampled_at IS NOT NULL
                 AND abs(extract(epoch FROM c.minute_at - c.sampled_at))
                     <= c.median_gap * p_gap_tolerance
                THEN c.viewer_count END AS viewers
      FROM carried c),
-- Only where EVERY participating platform is known. count(viewers) counts
-- non-nulls, so this is "nothing is missing" without a subquery. A total built
-- from whichever happened to be polled would undercount.
combined AS (
    SELECT minute_at,
           CASE WHEN count(*) = count(viewers) THEN sum(viewers)::integer END AS total
      FROM fresh GROUP BY minute_at)
SELECT f.minute_at::timestamptz, f.platform, f.viewers, c.total,
       f.sampled_at, f.age, f.median_gap
  FROM fresh f JOIN combined c USING (minute_at)
 ORDER BY f.minute_at, f.platform;
$$;
