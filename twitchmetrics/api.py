"""Thin wrappers over the Twitch Helix endpoints this project uses.

Every request carries both headers Twitch requires:
    Authorization: Bearer <token>
    Client-Id: <client id>
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

STREAMS_URL = config.HELIX + "/streams"
USERS_URL = config.HELIX + "/users"
GAMES_URL = config.HELIX + "/games"
FOLLOWERS_URL = config.HELIX + "/channels/followers"
CHATTERS_URL = config.HELIX + "/chat/chatters"

MAX_IDS = 100        # Twitch's limit for repeated login/id parameters
MAX_PAGE = 100       # per-page maximum on the paginated endpoints

# A runaway guard and not a budget. Measured: IRL is 7 pages, Just Chatting -- the
# largest category there is -- is 66. At a ten-minute cadence even the latter is
# under one percent of the 800-per-minute allowance, so there is no reason to stop
# early; this exists only to bound a cursor that has stopped advancing.
MAX_CATEGORY_PAGES = 200

SCOPE_CHATTERS = "moderator:read:chatters"
SCOPE_FOLLOWERS = "moderator:read:followers"


class RateLimited(Exception):
    def __init__(self, retry_after):
        super().__init__("rate limited")
        self.retry_after = retry_after


def get(url, params, token, client_id):
    """GET a Helix endpoint and return the decoded JSON."""
    query = urllib.parse.urlencode(params, doseq=True)
    request = urllib.request.Request("{}?{}".format(url, query), method="GET")
    request.add_header("Authorization", "Bearer {}".format(token))
    request.add_header("Client-Id", client_id)
    try:
        with urllib.request.urlopen(request, timeout=config.HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            reset = exc.headers.get("Ratelimit-Reset")
            retry_after = 60
            if reset:
                try:
                    retry_after = max(1, int(float(reset) - time.time()))
                except ValueError:
                    pass
            raise RateLimited(retry_after) from exc
        raise


def get_stream(login, token, client_id):
    """The stream dict, or None when the channel is offline.

    Twitch answers an offline channel with HTTP 200 and an empty data array,
    not an error, so None here means "not streaming" rather than "failed".
    """
    payload = get(STREAMS_URL, {"user_login": login, "first": 1}, token, client_id)
    data = payload.get("data") or []
    return data[0] if data else None


def get_streams(logins, token, client_id):
    """Live streams for many logins at once, batched to Twitch's limit.

    Offline channels are simply absent from the response, the same way
    get_users() omits unknown accounts, so callers diff what they asked for
    against what came back rather than expecting a placeholder.

    One request covers a hundred channels, which is what makes ranking cheap:
    liveness, category and viewer count for every tracked channel arrive
    together, before any category is walked.
    """
    found = []
    for start in range(0, len(logins), MAX_IDS):
        batch = logins[start:start + MAX_IDS]
        payload = get(STREAMS_URL, {"user_login": batch, "first": MAX_PAGE},
                      token, client_id)
        found += payload.get("data") or []
    return found


def category_streams(game_id, token, client_id, max_pages=MAX_CATEGORY_PAGES):
    """(streams, pages_read, complete) for one category, viewer_count DESC.

    Helix returns a category ordered by viewer count, descending, which is the
    ordering the directory page shows under "Sort by: Viewers (High to Low)" --
    so walking this cursor is the supported way to ask how far down the page a
    channel sits, and there is nothing to scrape.

    `complete` is True when the cursor ran out on its own. False means max_pages
    stopped the walk, and the caller has to treat the totals as lower bounds
    rather than counts. It is returned rather than inferred from
    pages_read == max_pages because a category that is exactly max_pages long is
    complete and would otherwise look truncated.
    """
    streams = []
    cursor = None
    pages = 0
    while pages < max_pages:
        params = {"game_id": game_id, "first": MAX_PAGE}
        if cursor:
            params["after"] = cursor
        payload = get(STREAMS_URL, params, token, client_id)
        pages += 1
        data = payload.get("data") or []
        streams += data
        cursor = (payload.get("pagination") or {}).get("cursor")
        # Twitch signals the end either by dropping the cursor or by answering
        # with an empty page while still handing one back. Both mean stop, and
        # both mean the listing is complete.
        if not cursor or not data:
            return streams, pages, True
    return streams, pages, False


def get_users(values, token, client_id, by_id=False):
    """Look up accounts by login (default) or id, batched to Twitch's limit.

    Unknown accounts are omitted from the response rather than raising, so
    callers should diff what they asked for against what came back.
    """
    key = "id" if by_id else "login"
    found = []
    for start in range(0, len(values), MAX_IDS):
        batch = values[start:start + MAX_IDS]
        found += get(USERS_URL, {key: batch}, token, client_id).get("data", [])
    return found


def resolve_user_id(value, token, client_id):
    """(user_id, login) from either a numeric id or a login name."""
    value = str(value).strip().lstrip("@")
    if value.isdigit():
        return value, None
    found = get_users([value], token, client_id)
    if not found:
        return None, None
    return found[0]["id"], found[0]["login"]


def get_followers(broadcaster_id, token, client_id, cursor=None, user_id=None,
                  first=MAX_PAGE):
    params = {"broadcaster_id": broadcaster_id, "first": first}
    if cursor:
        params["after"] = cursor
    if user_id:
        params["user_id"] = user_id
    return get(FOLLOWERS_URL, params, token, client_id)


def get_chatters(broadcaster_id, moderator_id, token, client_id, cursor=None,
                 first=MAX_PAGE):
    params = {"broadcaster_id": broadcaster_id, "moderator_id": moderator_id,
              "first": first}
    if cursor:
        params["after"] = cursor
    return get(CHATTERS_URL, params, token, client_id)
