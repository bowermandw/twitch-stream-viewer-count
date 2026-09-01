-- Estimated watch hours per location, all-time.
--
-- 008 put watch time on two report tables: one row per broadcast and one row per
-- local day. Both are the right grain for "how did this stream do" and "how is
-- the channel trending". Neither answers "which of these places is worth going
-- back to", which is a question about a VENUE and not about a date.
--
-- 007 already files every broadcast under a location -- tm.stream_location_rule
-- matches the stream title, and tm.refresh_stream_trends() denormalises the
-- answer into tm.report_stream_trend.location -- and the followers chart already
-- rolls up by it. So the location axis exists. What does not exist is watch time
-- along it.
--
-- WHY THIS IS A TABLE, given what 007 said. 007 rolled weekday and location up
-- with a READ function and left this note, which is the first thing anyone will
-- raise against this file:
--
--     The rollups by weekday and by location are READ functions rather than
--     tables, and deliberately. A rollup is scoped to "the last N broadcasts", N
--     is a command-line flag, and a table keyed without N would be quietly wrong
--     the first time anyone passed --stream-count 20.
--
-- That objection is exactly right, and this table is keyed ALL-TIME to dissolve
-- it rather than to argue with it. There is no N here to be wrong about: one row
-- per location, covering every broadcast on record. tm.stream_groups() cannot
-- answer this question at all -- it is windowed by construction, and widening its
-- window is a caller's choice rather than the table's meaning.
--
-- All-time is also the honest window for THIS metric specifically. A venue's
-- worth is a property accumulated over years; the last ten broadcasts tell you
-- where the channel has been lately, which is a different question and one the
-- existing chart already answers.
--
-- Every caveat in 008's header applies unchanged and applies HARDER here, because
-- an all-time per-venue average is precisely the kind of figure that gets quoted
-- out of context. It is live-only, it is not either platform's definition, and it
-- is an estimate. One caveat is new and belongs to this file: the average is over
-- broadcasts that HAVE an estimate. A broadcast nobody's viewer count was sampled
-- during is absent from it rather than present as a zero -- so a venue whose
-- early streams predate viewer sampling reads on its later ones, and reads high
-- rather than low.
--
-- The timezone rule from 001_schema.sql applies. tz is part of the key, never an
-- assumption. There is no range to convert here, so the second half of that rule
-- -- never put AT TIME ZONE on an indexed column in a WHERE clause -- has nothing
-- to catch in this file, which is itself the point: this reads a report table and
-- not a sample.

SET LOCAL search_path = tm, public;


-- --------------------------------------------------------------------------
-- where it is stored
-- --------------------------------------------------------------------------
--
-- A table of its own rather than columns on an existing one, which is the
-- opposite of what 008 decided and for the opposite reason. 008's watch time
-- keyed on a broadcast and on a day, and both already had a row. This keys on
-- (channel, platform, tz, location), and nothing has a row for that.

CREATE TABLE IF NOT EXISTS tm.report_location_watch (
    channel_id bigint NOT NULL REFERENCES tm.channel ON DELETE CASCADE,
    tz         text   NOT NULL,
    platform   tm.platform_kind NOT NULL,
    -- '' and not NULL for "no rule matched", because it is half of the primary
    -- key and a NULL there would let the same location arrive twice. It is also
    -- the convention tm.stream_groups() already returns and db_cmd's --locations
    -- already counts by. The renderer owns the WORD shown for it, so exactly one
    -- place decides that it reads "Unknown".
    location   text   NOT NULL,
    -- Broadcasts at this location that HAVE an estimate, which is what the
    -- average divides by. Not the number filed here -- see the header.
    streams    integer NOT NULL,
    watch_minutes numeric(18,3),         -- total across all of them
    avg_minutes   numeric(18,6),         -- the number the chart draws
    best_minutes  numeric(18,3),
    -- SET NULL and not CASCADE: losing the pointer to the best broadcast must not
    -- take the venue's whole history with it.
    best_stream_id bigint REFERENCES tm.stream ON DELETE SET NULL,
    -- Summed over every broadcast, so the chart can say what share of this venue
    -- was actually watched by a running poller. bigint because these are all-time
    -- sums and integer seconds stop being obviously roomy once they are.
    covered_seconds bigint,
    span_seconds    bigint,
    first_local_date date NOT NULL,
    last_local_date  date NOT NULL,
    computed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, tz, platform, location),
    -- Sparse, like tm.report_clock_bucket: a location with nothing estimable has
    -- no row at all, so a row that exists always says something. The renderer
    -- builds its own axis from what is here.
    CONSTRAINT report_location_watch_streams CHECK (streams > 0),
    -- The pairing rule tm.report_daily_peak already enforces on its peak: either
    -- there is an estimate or there is not, and half of one is a bug that would
    -- otherwise reach a chart as a blank bar with a filled tooltip.
    CONSTRAINT report_location_watch_pair
        CHECK ((watch_minutes IS NULL) = (avg_minutes IS NULL))
);


-- --------------------------------------------------------------------------
-- filling it
-- --------------------------------------------------------------------------

-- MUST run after tm.refresh_stream_trends(), because it reads what that writes.
-- Both halves of what it reads, in fact: the location that function resolves from
-- the title, and the watch minutes 008 taught it to store. It touches no sample.
--
-- No date range, unlike every other refresh_* in this schema. All-time is the
-- meaning of the table, and a p_from/p_to here would invite a caller to refresh a
-- slice of a row that spans the whole history -- which could only ever produce a
-- wrong number, never a partial one.
CREATE OR REPLACE FUNCTION tm.refresh_location_watch(
    p_channel_id bigint,
    p_platform   tm.platform_kind,
    p_tz         text DEFAULT NULL
) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_tz   text;
    v_rows integer;
BEGIN
    SELECT coalesce(p_tz, c.report_timezone) INTO v_tz
      FROM tm.channel c WHERE c.channel_id = p_channel_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown channel_id %', p_channel_id;
    END IF;

    -- DELETE then INSERT rather than an upsert, for tm.refresh_stream_trends()'
    -- reason: the result set is sparse, and a location rule that gets dropped has
    -- to be able to remove its row rather than leave a venue standing on the
    -- chart for ever with the numbers it had the day the rule went away.
    DELETE FROM tm.report_location_watch
     WHERE channel_id = p_channel_id
       AND tz         = v_tz
       AND platform   = p_platform;

    INSERT INTO tm.report_location_watch (
        channel_id, tz, platform, location, streams, watch_minutes, avg_minutes,
        best_minutes, best_stream_id, covered_seconds, span_seconds,
        first_local_date, last_local_date, computed_at)
    WITH picked AS (
        SELECT r.stream_id, coalesce(r.location, '') AS location, r.local_date,
               r.watch_minutes, r.covered_seconds, r.span_seconds
          FROM tm.report_stream_trend r
         WHERE r.channel_id = p_channel_id
           AND r.tz         = v_tz
           AND r.platform   = p_platform
           -- A broadcast with no estimate is absent, not zero. Averaging a zero
           -- in would report a quiet venue where the truth is an unpolled one,
           -- and the CHECK on streams above is what makes the absence legible.
           -- Same filter tm.stream_groups() applies, for the same reason.
           AND r.watch_minutes IS NOT NULL),
    -- DISTINCT ON with the tie broken on the lower stream_id, lifted from
    -- tm.stream_groups() so that "best" means the same thing on both charts.
    best AS (
        SELECT DISTINCT ON (b.location) b.location, b.stream_id, b.watch_minutes
          FROM picked b
         ORDER BY b.location, b.watch_minutes DESC, b.stream_id ASC)
    SELECT p_channel_id, v_tz, p_platform, p.location,
           count(*)::integer,
           sum(p.watch_minutes)::numeric(18,3),
           avg(p.watch_minutes)::numeric(18,6),
           x.watch_minutes, x.stream_id,
           -- NULL when no broadcast here recorded one, rather than 0: a venue
           -- with no coverage figure is one we cannot judge, not one we polled
           -- perfectly. The renderer prints no caveat for NULL and a loud one
           -- for a low number, so the difference has to survive to it.
           sum(p.covered_seconds)::bigint, sum(p.span_seconds)::bigint,
           min(p.local_date), max(p.local_date), now()
      FROM picked p JOIN best x ON x.location = p.location
     GROUP BY p.location, x.watch_minutes, x.stream_id;

    GET DIAGNOSTICS v_rows = ROW_COUNT;
    RETURN v_rows;
END;
$$;


-- --------------------------------------------------------------------------
-- the entry points, again
-- --------------------------------------------------------------------------
--
-- Both bodies are 008's with one loop added. Same reasoning 007 and 008 both
-- wrote down: an applied file is history and does not change, so this is a
-- replacement in a new file and not an edit.
--
-- BOTH of them, and not just the nightly one. If tm.refresh_after_sample() were
-- left alone, the poller's steady state and db --rebuild would disagree about
-- what the table holds, and the disagreement would only show up as a chart that
-- is stale by up to a day -- which is exactly the kind of bug that gets noticed
-- months later and blamed on the arithmetic.

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

    -- Last, and in a loop of its own rather than folded into the one above: it
    -- reads the rows tm.refresh_stream_trends() has just written, and it is
    -- all-time, so it takes none of this function's range.
    FOR v_platform IN
        SELECT platform FROM tm.platform_account WHERE channel_id = p_channel_id
    LOOP
        PERFORM tm.refresh_location_watch(p_channel_id, v_platform, v_tz);
    END LOOP;
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

    -- Affordable on the poller's path, which is the only reason it is here: this
    -- scans one channel's tm.report_stream_trend, which holds one row per
    -- BROADCAST -- hundreds, where the sample table holds hundreds of thousands.
    -- The gain is that the venue chart moves while a stream is still running.
    PERFORM tm.refresh_location_watch(v_channel, a.platform, v_tz)
       FROM tm.platform_account a WHERE a.channel_id = v_channel;
END;
$$;


-- --------------------------------------------------------------------------
-- reads, for the site generator
-- --------------------------------------------------------------------------

-- store.location_watch().
--
-- HOURS, not minutes, converted here. 008's rule: every caller of this wants a
-- number a person reads, and converting in one place beats three renderers each
-- remembering to.
--
-- The first six columns are tm.stream_groups()' first six, name for name and type
-- for type, and deliberately. That is what lets trends.render_stream_groups()
-- draw this with no change to how it reads a row -- the followers-by-location
-- chart and this one are the same renderer fed from two different windows.
CREATE OR REPLACE FUNCTION tm.location_watch(
    p_channel_id bigint,
    p_platform   tm.platform_kind,
    p_tz         text DEFAULT NULL
) RETURNS TABLE (group_key text, streams integer, total numeric, average numeric,
                 best numeric, best_stream_id bigint,
                 covered_seconds bigint, span_seconds bigint,
                 first_local_date date, last_local_date date)
LANGUAGE sql STABLE AS $$
    WITH zone AS (
        SELECT coalesce(p_tz, c.report_timezone) AS name
          FROM tm.channel c WHERE c.channel_id = p_channel_id)
    SELECT r.location, r.streams,
           (r.watch_minutes / 60.0)::numeric(18,3),
           (r.avg_minutes   / 60.0)::numeric(18,6),
           (r.best_minutes  / 60.0)::numeric(18,3),
           r.best_stream_id,
           r.covered_seconds, r.span_seconds,
           r.first_local_date, r.last_local_date
      FROM tm.report_location_watch r CROSS JOIN zone z
     WHERE r.channel_id = p_channel_id
       AND r.platform   = p_platform
       AND r.tz         = z.name
     ORDER BY r.location;
$$;


-- --------------------------------------------------------------------------
-- the backfill
-- --------------------------------------------------------------------------
--
-- The table is empty and nothing would ever fill it on its own, for the reason
-- 007 and 008 both wrote down: store.ensure_reports() judges a window by how many
-- DATES tm.report_daily_peak holds, and it holds all of them already -- so it
-- reports the window healthy and refreshes only its tail, for ever. A brand new
-- table is the same blind spot as a brand new column, only total.
--
-- CHEAP, unlike 008's backfill, and worth saying so before somebody replaces this
-- with a tm.refresh_range() sweep for symmetry. 008 already filled
-- tm.report_stream_trend.watch_minutes, so every number this needs is sitting in
-- a table with one row per broadcast. This is an aggregate over that, not a
-- re-integration of every sample the project has ever taken.
DO $backfill$
DECLARE
    v_acct record;
BEGIN
    FOR v_acct IN
        SELECT a.channel_id, a.platform
          FROM tm.platform_account a
         ORDER BY a.channel_id, a.platform
    LOOP
        PERFORM tm.refresh_location_watch(v_acct.channel_id, v_acct.platform, NULL);
    END LOOP;
END;
$backfill$;
