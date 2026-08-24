"""Thin wrappers over the YouTube Data API v3 endpoints this project uses.

Only an API key is needed — it rides along as a query parameter, so there is no
token to obtain, cache or refresh and no `auth.py` twin for this module.

Two facts shape everything here:

  * There is no "is this channel live now" endpoint. The live video has to be
    found first, and only then can its concurrent viewer count be read.
  * Every request is billed against a daily quota, so how the live video is
    found matters more than how directly it reads. See find_live_video().
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import config

CHANNELS_URL = config.YOUTUBE_API + "/channels"
PLAYLIST_ITEMS_URL = config.YOUTUBE_API + "/playlistItems"
VIDEOS_URL = config.YOUTUBE_API + "/videos"
SEARCH_URL = config.YOUTUBE_API + "/search"

MAX_IDS = 50         # videos.list accepts 50 ids per call
MAX_PAGE = 50        # per-page maximum on the paginated endpoints

# How far down the uploads playlist to look for the live broadcast. Enough to
# cover the handful of streams a channel schedules ahead plus recent uploads,
# and small enough to stay inside one page.
DEFAULT_RECENT = 15

# channels.list + playlistItems.list + videos.list, one unit each. The number
# lives in config so the interval guard there can explain itself.
UNITS_PER_SAMPLE = config.YOUTUBE_UNITS_PER_SAMPLE

# search.list has its own allowance, separate from the unit pool, and it is far
# too small to poll with. Quoted in the messages that mention --search.
SEARCH_CALLS_PER_DAY = 100

RETRY_AFTER = 60  # YouTube sends no Retry-After, so back off a flat minute

# Every one of these arrives as HTTP 403, so the body has to be read to tell an
# exhausted quota from a rejected key from a transient burst limit.
QUOTA_REASONS = ("quotaExceeded", "dailyLimitExceeded")
RATE_REASONS = ("rateLimitExceeded", "userRateLimitExceeded")
KEY_REASONS = ("keyInvalid", "keyExpired", "ipRefererBlocked",
               "accessNotConfigured", "forbidden")

# A channel id is always UC plus 22 more characters; anything else is a handle.
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


class QuotaExceeded(Exception):
    """The day's unit allowance is gone. Nothing to do but wait for midnight PT."""

    def __init__(self, reason="quotaExceeded"):
        super().__init__("quota exceeded")
        self.reason = reason


class RateLimited(Exception):
    def __init__(self, retry_after=RETRY_AFTER):
        super().__init__("rate limited")
        self.retry_after = retry_after


class KeyRejected(Exception):
    """The key itself is the problem — retrying will not help."""

    def __init__(self, reason="forbidden"):
        super().__init__("API key rejected ({})".format(reason))
        self.reason = reason


def _reason(exc):
    """The first error reason in a Google error body, or "" if unreadable.

    The body can only be read once, so this is called from the handler that is
    about to re-raise and never twice for the same response.
    """
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except (AttributeError, OSError, ValueError):
        return ""
    errors = (payload.get("error") or {}).get("errors") or []
    return errors[0].get("reason") or "" if errors else ""


def get(url, params, key):
    """GET a Data API endpoint and return the decoded JSON.

    Failures are translated into the three exceptions above so callers can tell
    "try again later" from "stop asking today" from "fix your key". Note that
    HTTPError.url carries the API key, so callers must log exc.code and never
    the exception's url.
    """
    query = urllib.parse.urlencode(dict(params, key=key))
    request = urllib.request.Request("{}?{}".format(url, query), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=config.HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            reason = _reason(exc)
            if reason in QUOTA_REASONS:
                raise QuotaExceeded(reason) from exc
            if exc.code == 429 or reason in RATE_REASONS:
                raise RateLimited() from exc
            if reason in KEY_REASONS:
                raise KeyRejected(reason) from exc
        raise


# --------------------------------------------------------------------------
# the channel
# --------------------------------------------------------------------------


def channel_filter(value):
    """The channels.list filter for either a handle or a raw UC… channel id.

    Accepts what a person would paste: "testchannel", "@testchannel", or
    the id out of a /channel/ URL.
    """
    value = str(value).strip().lstrip("@")
    if CHANNEL_ID_RE.match(value):
        return {"id": value}
    return {"forHandle": "@" + value}


def resolve_channel(value, key):
    """{"id", "title", "uploads"} for a channel, or None when there is no such one.

    The id and the uploads playlist id never change, so callers resolve once at
    startup rather than paying a unit for it on every sample.
    """
    payload = get(CHANNELS_URL,
                  dict(channel_filter(value), part="snippet,contentDetails"), key)
    items = payload.get("items") or []
    if not items:
        return None
    item = items[0]
    related = (item.get("contentDetails") or {}).get("relatedPlaylists") or {}
    return {
        "id": item.get("id"),
        "title": (item.get("snippet") or {}).get("title") or str(value),
        "uploads": related.get("uploads"),
    }


def get_subscribers(channel_id, key):
    """(count_or_None, hidden).

    YouTube rounds subscriberCount to three significant figures, so the number
    steps (1.23M then 1.24M) instead of climbing smoothly. A channel that hides
    the count reports hiddenSubscriberCount rather than a number, and that is
    not the same as zero — hence None, so the CSV column stays blank.
    """
    payload = get(CHANNELS_URL, {"part": "statistics", "id": channel_id}, key)
    items = payload.get("items") or []
    if not items:
        return None, False
    stats = items[0].get("statistics") or {}
    hidden = bool(stats.get("hiddenSubscriberCount"))
    raw = stats.get("subscriberCount")
    if hidden or raw is None:
        return None, hidden
    try:
        return int(raw), hidden
    except (TypeError, ValueError):
        return None, hidden


# --------------------------------------------------------------------------
# the live video
# --------------------------------------------------------------------------


def recent_video_ids(uploads_playlist, key, limit=DEFAULT_RECENT):
    """The newest video ids on a channel, newest first. One unit."""
    payload = get(PLAYLIST_ITEMS_URL, {
        "part": "contentDetails",
        "playlistId": uploads_playlist,
        "maxResults": max(1, min(int(limit), MAX_PAGE)),
    }, key)
    ids = []
    for item in payload.get("items") or []:
        video_id = (item.get("contentDetails") or {}).get("videoId")
        if video_id:
            ids.append(video_id)
    return ids


def get_videos(ids, key):
    """Full video resources for up to any number of ids, batched to the API limit."""
    found = []
    for start in range(0, len(ids), MAX_IDS):
        batch = ids[start:start + MAX_IDS]
        payload = get(VIDEOS_URL, {
            # statistics rides along for free: quota is charged per call, not
            # per part, so likeCount costs nothing on top of the live details.
            "part": "snippet,liveStreamingDetails,statistics",
            "id": ",".join(batch),
        }, key)
        found += payload.get("items") or []
    return found


def recent_videos(uploads_playlist, key, limit=DEFAULT_RECENT):
    """The newest videos with their broadcast state attached. Two units."""
    ids = recent_video_ids(uploads_playlist, key, limit)
    return get_videos(ids, key) if ids else []


def broadcast_state(video):
    """"live", "upcoming" or "none" — what snippet.liveBroadcastContent says."""
    return ((video.get("snippet") or {}).get("liveBroadcastContent") or "none")


def started_at(video):
    """actualStartTime as a sortable string; "" for one that hasn't started."""
    return (video.get("liveStreamingDetails") or {}).get("actualStartTime") or ""


def concurrent_viewers(video):
    """concurrentViewers as an int, or None when YouTube isn't reporting it.

    The field is missing for the first moments of a broadcast and on one whose
    owner has hidden the count, so a live video without a number is normal and
    not worth warning about.
    """
    raw = (video.get("liveStreamingDetails") or {}).get("concurrentViewers")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def likes(video):
    """likeCount as an int, or None when the creator has hidden it.

    Cumulative for the broadcast rather than a rate, so it only ever climbs —
    and unlike the subscriber count it is exact, which is the point of having
    it. Live streams sometimes report it a beat behind the viewer count.
    """
    raw = (video.get("statistics") or {}).get("likeCount")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def pick_live(videos):
    """The live one out of a list of videos, or None.

    Two at once happens on a channel running a permanent stream beside an
    event, and the one that started most recently is the event.
    """
    live = [v for v in videos if broadcast_state(v) == "live"]
    if not live:
        return None
    live.sort(key=started_at)
    return live[-1]


def find_live_video(uploads_playlist, key, limit=DEFAULT_RECENT):
    """The channel's currently live video, or None. Two units.

    Walking the uploads playlist rather than asking search.list, because
    search.list is capped at SEARCH_CALLS_PER_DAY calls a day — fewer than a
    five-minute interval needs — while this costs two units out of ten thousand.
    A scheduled broadcast sits in the playlist as "upcoming" and flips to "live"
    when it starts, which is what pick_live() keys off.
    """
    return pick_live(recent_videos(uploads_playlist, key, limit))


def search_live_video(channel_id, key):
    """The same, via search.list. Two units, but see SEARCH_CALLS_PER_DAY.

    The direct route, and the fallback for a channel whose uploads playlist is
    slow to show a broadcast that has just gone live. Do not make this the
    default: the daily call allowance cannot sustain a poll loop.
    """
    payload = get(SEARCH_URL, {
        "part": "id", "channelId": channel_id, "eventType": "live",
        "type": "video", "maxResults": 1, "order": "date",
    }, key)
    ids = [vid for vid in ((item.get("id") or {}).get("videoId")
                           for item in payload.get("items") or []) if vid]
    return pick_live(get_videos(ids, key)) if ids else None
