-- Two gaps in the refresh path, found the first time the whole flow was run
-- against a real server.
--
-- 001-004 are applied in production now, so this is a new file rather than an
-- edit to them: a numbered file that has shipped is history, and rewriting one
-- would leave every database that already ran it silently different from a
-- database built from scratch.

SET LOCAL search_path = tm, public;

-- refresh_range() filled the two per-day tables and never report_stream_metric,
-- so per-broadcast summaries stayed empty unless a stream happened to be live
-- when refresh_after_sample() ran -- which no imported archive ever is.
--
-- Bounded by the same instant range as everything else, and matched on the
-- broadcast's SAMPLES rather than on started_at: a stream that began before
-- midnight and ran past it belongs to both days, and keying off its start would
-- drop it from the second one.
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
END;
$$;


-- What a poller calls after each sample, made cheap enough to mean it.
--
-- refresh_after_sample() already refreshed two days, for the reason its own
-- comment gives. This adds the broadcast the sample actually belongs to, rather
-- than "whichever stream is currently open" -- during a replay of spooled rows
-- those are not the same thing, and the open one is the wrong answer.
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
BEGIN
    SELECT a.channel_id, c.report_timezone INTO v_channel, v_tz
      FROM tm.platform_account a JOIN tm.channel c USING (channel_id)
     WHERE a.account_id = p_account_id;
    IF v_channel IS NULL THEN
        RETURN;
    END IF;

    v_day := (p_sampled_at AT TIME ZONE v_tz)::date;
    FOR v_day IN SELECT unnest(ARRAY[v_day - 1, v_day]) LOOP
        PERFORM tm.refresh_daily_peaks(v_channel, a.platform, v_day, v_day, v_tz)
           FROM tm.platform_account a WHERE a.channel_id = v_channel;
        PERFORM tm.refresh_clock_buckets(v_channel, a.platform, v_day, v_day,
                                         p_bucket_minutes, v_tz)
           FROM tm.platform_account a WHERE a.channel_id = v_channel;
    END LOOP;

    IF p_stream_id IS NOT NULL THEN
        PERFORM tm.refresh_stream_metrics(p_stream_id);
    END IF;
END;
$$;
