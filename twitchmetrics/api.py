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
FOLLOWERS_URL = config.HELIX + "/channels/followers"
CHATTERS_URL = config.HELIX + "/chat/chatters"

MAX_IDS = 100        # Twitch's limit for repeated login/id parameters
MAX_PAGE = 100       # per-page maximum on the paginated endpoints

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
