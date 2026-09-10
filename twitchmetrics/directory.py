"""Where a stream sits in a category listing.

Arithmetic only, like trends.py: no HTTP, no database, nothing to mock. That is
what lets the smoke suite check the counting -- which is the part with a real bug
in it -- against a hand-built listing, offline.

THE BUG THIS MODULE EXISTS TO AVOID. The obvious implementation is
`[s["user_login"] for s in streams].index(login)`, and it is wrong. Viewer counts
in the tail of a category are tied in enormous blocks: measured on IRL, 207 of
532 live streams had exactly 1 viewer. The index of a one-viewer stream is
therefore not a fact about that stream, it is whichever order Helix emitted the
tie block in -- anywhere from 295 to 501 for the same channel in the same second.
Two passes taken seconds apart moved 486 of 488 streams.

So position is counted rather than looked up: how many streams had strictly more
viewers, and how many shared the same count. Both are stable under any
permutation of a tie block, which is the property the list index lacks.
"""


def dedupe(streams):
    """One row per channel, keeping the last seen.

    NOT fussiness -- measured. A category is churning while the cursor walks it,
    and a stream that shifts across a page boundary between two requests arrives
    twice. Three consecutive passes over IRL returned 521 rows for 508 channels,
    515 for 492, and 513 for 501: twelve to twenty-two duplicates every time, a
    few of them appearing three times.

    Left in, they corrupt all three numbers rather than just the total. A
    duplicated stream ahead of us is counted twice in streams_ahead, a
    duplicated stream level with us inflates tie_count, and total_streams comes
    out two to four percent high -- so the denominator, the numerator and the
    tie band are each wrong by a different amount.

    Called by rank_in() rather than left to the caller, so it cannot be
    forgotten. The caller that needs the count should count what this returns.
    """
    seen = {}
    for stream in streams:
        key = stream.get("user_id") or stream.get("user_login")
        if key is None:
            continue
        seen[str(key)] = stream
    return list(seen.values())


def rank_in(streams, login):
    """Where `login` sits in a category listing, or None if it is not in one.

    `streams` is Helix's data array for one category, and it is deduped here
    before anything is counted -- see dedupe(). It arrives sorted by
    viewer_count descending, but nothing here relies on that: counting works on
    an arbitrarily ordered list, which is one fewer assumption to be wrong about.

    Returns a dict of:
        streams_ahead   streams with strictly MORE viewers
        tie_count       streams with the same count, this one included
        viewer_count    as this listing reported it
        total_streams   rows in the listing
        stream_id       Twitch's id for the broadcast, to attach the rank to
        game_id         the category as the listing labelled it

    None means the channel was not in the listing at all. That is not
    necessarily an error -- the directory is eventually consistent, and a channel
    can be live and briefly missing from its own category -- so it is a value the
    caller handles rather than an exception, and it is never a fabricated rank.
    """
    wanted = (login or "").strip().lower()
    if not wanted:
        return None

    streams = dedupe(streams)
    mine = None
    for stream in streams:
        if (stream.get("user_login") or "").strip().lower() == wanted:
            mine = stream
            break
    if mine is None:
        return None

    # The channel's OWN row from this listing, not from a separate
    # /streams?user_login= call. The two are seconds apart and the number moves,
    # and a viewer_count that disagrees with the streams_ahead stored beside it
    # would make the row unauditable.
    viewers = mine.get("viewer_count")
    if viewers is None:
        return None

    ahead = 0
    tied = 0
    for stream in streams:
        count = stream.get("viewer_count")
        if count is None:
            continue
        if count > viewers:
            ahead += 1
        elif count == viewers:
            tied += 1

    return {
        "streams_ahead": ahead,
        # Includes this stream, so 1 means "no tie". The generated rank_worst
        # column is streams_ahead + tie_count, which only works on that reading.
        "tie_count": tied,
        "viewer_count": viewers,
        "total_streams": len(streams),
        "stream_id": mine.get("id"),
        "game_id": mine.get("game_id"),
    }


def describe(rank, login=None, game_name=None):
    """One log line. "#295 of 532, tied with 206" rather than "#303".

    A rank inside a tie block is an interval, and printing only its top edge
    reads as more precise than the measurement is.
    """
    where = login or "channel"
    if rank is None:
        return "{}  not in its own category listing".format(where)

    label = game_name or rank.get("game_id") or "?"
    best = rank["streams_ahead"] + 1
    tied = rank["tie_count"] - 1
    line = "{}  {}  #{:,} of {:,}".format(where, label, best, rank["total_streams"])
    if tied:
        line += ", tied with {:,}".format(tied)
    viewers = rank["viewer_count"]
    return line + "  {:,} viewer{}".format(viewers, "" if viewers == 1 else "s")
