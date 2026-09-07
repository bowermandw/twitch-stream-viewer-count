-- Linked broadcasts: an afternoon simulcast to two platforms is ONE broadcast.
--
-- 010 gave every venue a page and the picker that reaches them, and the picker's
-- count came straight out of tm.stream_locations(): count(*) over
-- tm.report_stream_trend. That table is ONE ROW PER STREAM PER PLATFORM, which
-- is right for every other thing that reads it -- every chart on this site is
-- per-platform, and a Twitch bar and a YouTube bar are two honest measurements
-- of the same afternoon, not a double count.
--
-- The picker is the one read that is NOT per-platform. It answers "which pages
-- does this channel need", so it has to group across both -- and count(*) there
-- counts that afternoon twice. A channel that simulcasts everything showed
-- "2 stream(s)" under a venue it had visited once, sitting three inches above a
-- chart headed "last 1 broadcast(s)". Both numbers were right about what they
-- counted, and the page still read as a contradiction. Every venue was doubled,
-- so even the ordering the picker exists to show was only accidentally correct.
--
-- WHAT LINKS TWO ROWS INTO ONE BROADCAST: their live windows overlap. The report
-- table already carries started_at and span_seconds, so "live at the same time,
-- at the same venue" costs no new column, no new table and no backfill -- and it
-- is the definition a person would give unprompted. The streamer went to Animal
-- Kingdom once and pointed two encoders at it.
--
-- WHY NOT max() OF THE PER-PLATFORM COUNTS, which is far cheaper and gives the
-- identical answer for a channel that simulcasts everything. Because that is the
-- only channel it is right for. A venue with one Twitch-only broadcast in June
-- and one YouTube-only broadcast in July has been visited twice; max() calls it
-- once. The undercount gets worse the more complete the record is, which is the
-- wrong direction for a number whose whole job is to rank venues by how much
-- history stands behind them. Overlap is right in both cases and needs no
-- special case for the channel that never simulcasts.
--
-- WHY THE LINKING IS PARTITIONED BY VENUE rather than done channel-wide and
-- attributed afterwards. A broadcast's venue comes from its own title, and the
-- two platforms' titles are typed separately -- Animal Kingdom's Twitch title
-- named the park and its YouTube title said "A Chill Day at Animal Kingdom",
-- which only matched because a rule looks anywhere in the string. When one of
-- them drifts out of its rule the two rows land in different venues and each
-- counts as its own broadcast, which is what SHOULD happen: the picker then
-- shows an "Unknown" row with something in it, and that row is the to-do. A
-- channel-wide link would produce one broadcast belonging to two venues, which
-- is not a thing either the picker or a venue page can represent.
--
-- BOTH COUNTS ARE RETURNED, and the old one keeps its name and its meaning.
-- `streams` is still rows -- what the per-platform charts draw, and what
-- `db --locations` counts when it reports what a rule claims. `broadcasts` is
-- the new one, and it is what the picker shows and orders by. Keeping `streams`
-- is also what makes this file safe to apply before the code that reads it
-- ships: the schema migrates from the Mac the moment it is written, the code
-- reaches the server only through main, and main's store.stream_locations()
-- names its columns explicitly -- so it keeps getting exactly what it got
-- yesterday from a function that now returns one column more.
--
-- No table and no backfill, for 010's reasons: this is a cheap aggregate over
-- one channel's rows, and it introduces no storage for ensure_reports() to be
-- structurally blind to.


-- The signature is unchanged; the RETURNS TABLE gains a column, and CREATE OR
-- REPLACE cannot change the shape of one. 008 paid the same price twice.
DROP FUNCTION IF EXISTS tm.stream_locations(bigint, text);

CREATE OR REPLACE FUNCTION tm.stream_locations(
    p_channel_id bigint,
    p_tz         text DEFAULT NULL
) RETURNS TABLE (location text, broadcasts integer, streams integer,
                 first_local_date date, last_local_date date,
                 platforms text[])
LANGUAGE sql STABLE AS $$
    WITH zone AS (
        SELECT coalesce(p_tz, c.report_timezone) AS name
          FROM tm.channel c WHERE c.channel_id = p_channel_id),
    live AS (
        SELECT coalesce(r.location, '') AS venue,
               r.platform::text         AS plat,
               r.local_date             AS day,
               r.started_at             AS began,
               -- A row with no span is one we know the start of and nothing
               -- else. Treated as instantaneous, so it links to whatever was
               -- already live over it and never swallows what comes after.
               r.started_at + make_interval(
                   secs => coalesce(r.span_seconds, 0)) AS ended
          FROM tm.report_stream_trend r CROSS JOIN zone z
         WHERE r.channel_id = p_channel_id
           AND r.tz         = z.name),
    -- Gaps and islands, per venue: a row opens a new broadcast when it starts
    -- after everything before it at that venue had already finished. The
    -- running max is taken over the ENDS and the frame stops at 1 PRECEDING, so
    -- a chain of overlapping rows stays one broadcast however long it gets.
    -- max() over the empty frame is NULL and so is the comparison, which drops
    -- each venue's first row to ELSE -- correct, because it opens island 0.
    marked AS (
        SELECT l.*, CASE WHEN l.began > max(l.ended) OVER (
                             PARTITION BY l.venue ORDER BY l.began
                             ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
                         THEN 1 ELSE 0 END AS opens
          FROM live l),
    linked AS (
        SELECT m.*, sum(m.opens) OVER (PARTITION BY m.venue ORDER BY m.began
                                       ROWS UNBOUNDED PRECEDING) AS broadcast
          FROM marked m)
    SELECT k.venue, count(DISTINCT k.broadcast)::integer, count(*)::integer,
           min(k.day), max(k.day),
           array_agg(DISTINCT k.plat ORDER BY k.plat)
      FROM linked k
     GROUP BY k.venue
     -- Busiest first in BROADCASTS now, which is the number the picker prints.
     -- Ordering by one number and showing another is how a ranking stops being
     -- readable as one. Rows break a tie before the name does, so a venue
     -- simulcast throughout still leads one visited the same number of times on
     -- a single platform; the name is the last resort, for a stable order
     -- between runs.
     ORDER BY count(DISTINCT k.broadcast) DESC, count(*) DESC, k.venue;
$$;
