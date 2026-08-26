-- refresh_stream_metrics() never inserted a row.
--
-- The INSERT named eleven columns and the SELECT supplied ten: stream_id, the
-- one value that does not come out of the aggregate, was missing. Every call
-- raised "INSERT has more target columns than expressions", and because nothing
-- called it until now -- refresh_range() did not, and refresh_after_sample()
-- only reached it for a stream that was live at the time -- report_stream_metric
-- simply stayed empty and looked like a table nobody had got to yet.
--
-- Worth naming the reason it survived: the parity harness covers daily_peaks,
-- clock_buckets, compare_slots and the minute grid, and this was the one
-- aggregate with no Python counterpart to be compared against. A function no
-- test calls and no caller reaches is not covered by anything.

SET LOCAL search_path = tm, public;

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
    SELECT p_stream_id,          -- the column that was missing
           p.metric, max(p.value), pk.peak_at, pk.peak_elapsed,
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
