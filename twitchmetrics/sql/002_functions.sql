-- Ingest: identity, broadcasts, and the two functions a poller actually calls.
--
-- Everything a sample needs to do -- resolve the broadcast, open or close it,
-- insert the row -- happens inside one function call, so a poller makes one
-- round trip per tick rather than four. That matters more than it looks: the
-- poll loop ticks on wall-clock boundaries, and four round trips to a database
-- on another host is four chances to drift off one.

SET LOCAL search_path = tm, public;


-- --------------------------------------------------------------------------
-- identity
-- --------------------------------------------------------------------------

-- The timezone is validated here rather than by a CHECK constraint, because
-- verifying one needs AT TIME ZONE, which is STABLE, and a CHECK may only call
-- IMMUTABLE functions. Doing it at the one door every channel comes through is
-- the next best thing -- and a typo'd zone is worth failing loudly for, since
-- its only other symptom is every report quietly landing on the wrong day.
CREATE OR REPLACE FUNCTION tm.upsert_channel(
    p_slug          text,
    p_display_name  text DEFAULT NULL,
    p_timezone      text DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    v_id bigint;
BEGIN
    IF p_timezone IS NOT NULL THEN
        BEGIN
            PERFORM now() AT TIME ZONE p_timezone;
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION 'unknown timezone %; it needs an IANA name such as '
                            'Europe/London, not an abbreviation', p_timezone;
        END;
    END IF;

    INSERT INTO tm.channel AS c (slug, display_name, report_timezone)
    VALUES (p_slug,
            coalesce(p_display_name, p_slug),
            coalesce(p_timezone, 'UTC'))
    ON CONFLICT (slug) DO UPDATE SET
        -- coalesce so that calling this without a display name or a zone -- the
        -- way a poller does, every time it starts -- cannot blank out what
        -- somebody set deliberately.
        display_name    = coalesce(EXCLUDED.display_name, c.display_name),
        report_timezone = coalesce(p_timezone, c.report_timezone)
    RETURNING c.channel_id INTO v_id;
    RETURN v_id;
END;
$$;


CREATE OR REPLACE FUNCTION tm.upsert_account(
    p_channel_id          bigint,
    p_platform            tm.platform_kind,
    p_handle              text,
    p_platform_ref        text DEFAULT NULL,
    p_uploads_playlist_id text DEFAULT NULL,
    p_platform_title      text DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    v_id bigint;
BEGIN
    INSERT INTO tm.platform_account AS a (
        channel_id, platform, handle, platform_ref,
        uploads_playlist_id, platform_title, resolved_at)
    VALUES (p_channel_id, p_platform, p_handle, p_platform_ref,
            p_uploads_playlist_id, p_platform_title,
            CASE WHEN p_platform_ref IS NOT NULL THEN now() END)
    ON CONFLICT (channel_id, platform) DO UPDATE SET
        handle              = EXCLUDED.handle,
        -- Every one of these is coalesced for the same reason as above: the
        -- YouTube poller resolves the UC id and the uploads playlist at
        -- startup and the Twitch poller resolves neither, so an upsert from
        -- one must not erase what the other stored.
        platform_ref        = coalesce(EXCLUDED.platform_ref, a.platform_ref),
        uploads_playlist_id = coalesce(EXCLUDED.uploads_playlist_id, a.uploads_playlist_id),
        platform_title      = coalesce(EXCLUDED.platform_title, a.platform_title),
        resolved_at         = coalesce(EXCLUDED.resolved_at, a.resolved_at)
    RETURNING a.account_id INTO v_id;
    RETURN v_id;
END;
$$;


-- The poller's "who am I" lookup, and the reason platform_account exists: this
-- replaces an API call that both pollers currently make on every restart.
CREATE OR REPLACE FUNCTION tm.account_for(p_slug text, p_platform tm.platform_kind)
RETURNS bigint
LANGUAGE sql STABLE AS $$
    SELECT a.account_id
      FROM tm.platform_account a
      JOIN tm.channel c USING (channel_id)
     WHERE c.slug = p_slug AND a.platform = p_platform;
$$;


-- --------------------------------------------------------------------------
-- broadcasts, and the live-lookup cache
-- --------------------------------------------------------------------------

-- What the poller asks BEFORE it decides how much quota to spend.
--
-- With an answer it can call videos.list on a known id for one unit; without
-- one it has to walk the uploads playlist for two. That is the whole point of
-- the partial unique index this reads through -- it is a single index probe.
--
-- It also fixes a latent bug in the walk: find_live_video() only looks at the
-- fifteen most recent uploads, so a long broadcast on a channel that uploads
-- often can fall out of that window and look as though it ended.
CREATE OR REPLACE FUNCTION tm.live_stream(p_account_id bigint)
RETURNS TABLE (stream_id bigint, platform_stream_id text, title text,
               started_at timestamptz, last_sample_at timestamptz,
               miss_streak smallint)
LANGUAGE sql STABLE AS $$
    SELECT s.stream_id, s.platform_stream_id, s.title,
           s.started_at, s.last_sample_at, s.miss_streak
      FROM tm.stream s
     WHERE s.account_id = p_account_id AND s.ended_at IS NULL;
$$;


CREATE OR REPLACE FUNCTION tm.close_stream(
    p_stream_id bigint,
    p_ended_at  timestamptz DEFAULT NULL,
    p_reason    tm.stream_end DEFAULT 'offline'
) RETURNS void
LANGUAGE sql AS $$
    UPDATE tm.stream
       -- Anchored to the last LIVE sample rather than to the offline one that
       -- revealed the end. The broadcast stopped somewhere in between, and
       -- anchoring here keeps a stream's recorded duration independent of the
       -- poll interval, at the cost of under-reporting by up to one interval.
       SET ended_at    = coalesce(p_ended_at, last_sample_at),
           ended_reason = p_reason
     WHERE stream_id = p_stream_id AND ended_at IS NULL;
$$;


-- Open the broadcast, or touch the one already open.
--
-- Three cases, and the third is the one worth reading: seeing a DIFFERENT
-- stream id while one is still open means the channel started a second
-- broadcast without us observing the first one end. youtube.pick_live()
-- already handles that shape (a permanent stream running beside an event);
-- here it closes the old one as 'superseded' rather than leaving two open,
-- which the partial unique index would refuse anyway.
CREATE OR REPLACE FUNCTION tm.open_or_touch_stream(
    p_account_id         bigint,
    p_platform_stream_id text,
    p_title              text,
    p_game               text,
    p_started_at         timestamptz,
    p_sampled_at         timestamptz
) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    v_open_id  bigint;
    v_open_ref text;
    v_id       bigint;
BEGIN
    SELECT s.stream_id, s.platform_stream_id INTO v_open_id, v_open_ref
      FROM tm.stream s
     WHERE s.account_id = p_account_id AND s.ended_at IS NULL;

    IF v_open_id IS NOT NULL AND v_open_ref IS DISTINCT FROM p_platform_stream_id THEN
        PERFORM tm.close_stream(v_open_id, NULL, 'superseded');
    END IF;

    INSERT INTO tm.stream AS s (
        account_id, platform_stream_id, title, game, started_at,
        first_sample_at, last_sample_at, sample_count, miss_streak)
    VALUES (p_account_id, p_platform_stream_id,
            nullif(p_title, ''), nullif(p_game, ''), p_started_at,
            p_sampled_at, p_sampled_at, 1, 0)
    ON CONFLICT (account_id, platform_stream_id) DO UPDATE SET
        -- Last observed wins, which is exactly daily.day_title()'s rule: "a
        -- title edited mid-broadcast is usually being corrected, so what it
        -- ended as is what the day was called". nullif keeps an empty string
        -- from erasing a real title.
        title      = coalesce(nullif(EXCLUDED.title, ''), s.title),
        game       = coalesce(nullif(EXCLUDED.game, ''), s.game),
        started_at = coalesce(EXCLUDED.started_at, s.started_at),
        -- least/greatest rather than assignment, so replaying a spooled row
        -- from the middle of a broadcast cannot drag the window inwards.
        first_sample_at = least(s.first_sample_at, EXCLUDED.first_sample_at),
        last_sample_at  = greatest(s.last_sample_at, EXCLUDED.last_sample_at),
        sample_count    = s.sample_count + 1,
        miss_streak     = 0,
        -- Seeing it live again reopens it. A poller restarted mid-broadcast,
        -- or a close_stale_streams() that fired early, must not leave the rest
        -- of the stream orphaned into a second row it cannot have.
        ended_at     = NULL,
        ended_reason = NULL
    RETURNING s.stream_id INTO v_id;
    RETURN v_id;
END;
$$;


-- One poll that did not see the stream live. True while it is still cached.
--
-- Not closing on the first miss is deliberate: videos.list reports
-- liveBroadcastContent 'none' for a beat during a live broadcast, and evicting
-- the cache over that sends the next poll back to the two-unit playlist walk --
-- spending quota to punish a transient. Two misses at a sixty-second interval
-- is a minute of doubt before believing it.
CREATE OR REPLACE FUNCTION tm.note_live_miss(
    p_account_id  bigint,
    p_observed_at timestamptz DEFAULT NULL,
    p_max_misses  integer DEFAULT 2
) RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE
    v_id     bigint;
    v_misses smallint;
BEGIN
    UPDATE tm.stream
       SET miss_streak = miss_streak + 1
     WHERE account_id = p_account_id AND ended_at IS NULL
    RETURNING stream_id, miss_streak INTO v_id, v_misses;

    IF v_id IS NULL THEN
        RETURN false;              -- nothing was cached; nothing to do
    END IF;
    IF v_misses >= p_max_misses THEN
        PERFORM tm.close_stream(v_id, NULL, 'offline');
        RETURN false;
    END IF;
    RETURN true;
END;
$$;


-- The janitor, for the daily timer.
--
-- A poller killed mid-broadcast leaves a stream open for ever, and the cache
-- would go on handing out a dead video id -- spending a quota unit per poll on
-- a videos.list that can never answer "live". 'stale' rather than 'offline'
-- because the end time is a guess, and a duration derived from a guess should
-- be readable as one.
CREATE OR REPLACE FUNCTION tm.close_stale_streams(
    p_max_gap interval DEFAULT interval '30 minutes'
) RETURNS integer
LANGUAGE sql AS $$
    WITH closed AS (
        UPDATE tm.stream
           SET ended_at = last_sample_at, ended_reason = 'stale'
         WHERE ended_at IS NULL AND last_sample_at < now() - p_max_gap
        RETURNING 1)
    SELECT count(*)::integer FROM closed;
$$;


-- --------------------------------------------------------------------------
-- the two functions a poller calls
-- --------------------------------------------------------------------------
--
-- Both return the stream_id the sample was attached to, or NULL for an offline
-- one, so the caller can log what it recorded without asking a second question.
--
-- Both use a MERGING upsert rather than ON CONFLICT DO NOTHING, and that is a
-- decision rather than a default. Three things arrive at the same
-- (account, second) and each knows something the others do not:
--
--   * `poll` and `poll --viewers-only` wrote two separate files before, so they
--     could never collide; against one table the first writer would otherwise
--     win and the richer row would be silently dropped.
--   * the archive import reads metrics_*.csv and viewers_*.csv for the same
--     channel, and whichever was read first would decide which columns survived.
--   * a spooled row replayed after an outage is re-offering something already
--     stored, and must be a no-op rather than a conflict.
--
-- coalesce(EXCLUDED.x, existing.x) satisfies all three: new information fills
-- gaps, and nothing already known is ever overwritten with a NULL.

CREATE OR REPLACE FUNCTION tm.record_twitch_sample(
    p_account_id         bigint,
    p_sampled_at         timestamptz,
    p_platform_stream_id text    DEFAULT NULL,   -- NULL means offline
    p_viewer_count       integer DEFAULT NULL,
    p_follower_count     integer DEFAULT NULL,
    p_chatter_count      integer DEFAULT NULL,
    p_title              text    DEFAULT NULL,
    p_game               text    DEFAULT NULL,
    p_started_at         timestamptz DEFAULT NULL,
    p_source             text    DEFAULT 'poller',
    p_max_misses         integer DEFAULT 2
) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    v_channel_id bigint;
    v_stream_id  bigint;
BEGIN
    SELECT channel_id INTO v_channel_id
      FROM tm.platform_account WHERE account_id = p_account_id;
    IF v_channel_id IS NULL THEN
        RAISE EXCEPTION 'unknown account_id %', p_account_id;
    END IF;

    IF nullif(p_platform_stream_id, '') IS NOT NULL THEN
        v_stream_id := tm.open_or_touch_stream(
            p_account_id, p_platform_stream_id, p_title, p_game,
            p_started_at, p_sampled_at);
    ELSE
        PERFORM tm.note_live_miss(p_account_id, p_sampled_at, p_max_misses);
    END IF;

    INSERT INTO tm.twitch_sample AS t (
        channel_id, account_id, sampled_at, stream_id,
        viewer_count, follower_count, chatter_count, ingest_source)
    VALUES (v_channel_id, p_account_id, p_sampled_at, v_stream_id,
            p_viewer_count, p_follower_count, p_chatter_count, p_source)
    ON CONFLICT (account_id, sampled_at) DO UPDATE SET
        stream_id      = coalesce(EXCLUDED.stream_id,      t.stream_id),
        viewer_count   = coalesce(EXCLUDED.viewer_count,   t.viewer_count),
        follower_count = coalesce(EXCLUDED.follower_count, t.follower_count),
        chatter_count  = coalesce(EXCLUDED.chatter_count,  t.chatter_count);
    RETURN v_stream_id;
END;
$$;


CREATE OR REPLACE FUNCTION tm.record_youtube_sample(
    p_account_id       bigint,
    p_sampled_at       timestamptz,
    p_video_id         text    DEFAULT NULL,     -- NULL means offline
    p_viewer_count     integer DEFAULT NULL,
    p_subscriber_count integer DEFAULT NULL,
    p_like_count       integer DEFAULT NULL,
    p_title            text    DEFAULT NULL,
    p_started_at       timestamptz DEFAULT NULL,
    p_source           text    DEFAULT 'poller',
    p_max_misses       integer DEFAULT 2
) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    v_channel_id bigint;
    v_stream_id  bigint;
BEGIN
    SELECT channel_id INTO v_channel_id
      FROM tm.platform_account WHERE account_id = p_account_id;
    IF v_channel_id IS NULL THEN
        RAISE EXCEPTION 'unknown account_id %', p_account_id;
    END IF;

    IF nullif(p_video_id, '') IS NOT NULL THEN
        -- No game: YouTube has no equivalent of a Twitch category, which is
        -- why the column is Twitch-only rather than nullable-for-both.
        v_stream_id := tm.open_or_touch_stream(
            p_account_id, p_video_id, p_title, NULL, p_started_at, p_sampled_at);
    ELSE
        PERFORM tm.note_live_miss(p_account_id, p_sampled_at, p_max_misses);
    END IF;

    INSERT INTO tm.youtube_sample AS y (
        channel_id, account_id, sampled_at, stream_id,
        viewer_count, subscriber_count, like_count, ingest_source)
    VALUES (v_channel_id, p_account_id, p_sampled_at, v_stream_id,
            p_viewer_count, p_subscriber_count, p_like_count, p_source)
    ON CONFLICT (account_id, sampled_at) DO UPDATE SET
        stream_id        = coalesce(EXCLUDED.stream_id,        y.stream_id),
        viewer_count     = coalesce(EXCLUDED.viewer_count,     y.viewer_count),
        subscriber_count = coalesce(EXCLUDED.subscriber_count, y.subscriber_count),
        like_count       = coalesce(EXCLUDED.like_count,       y.like_count);
    RETURN v_stream_id;
END;
$$;
