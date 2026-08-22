#!/usr/bin/env python3
"""How many people are connected to a channel's chat right now.

    python3 chatters.py themeparkgiant

Needs a user access token for a moderator of that channel, with the
moderator:read:chatters scope — user_auth.py handles that. The app access token
the rest of the project uses is rejected by this endpoint.
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

import twitch_viewers as tv
import user_auth
import user_info

CHATTERS_URL = "https://api.twitch.tv/helix/chat/chatters"
SCOPE = "moderator:read:chatters"
PAGE_SIZE = 100  # endpoint maximum


def resolve_id(value, client_id, client_secret):
    """Accept either a numeric user ID or a login name."""
    value = str(value).strip().lstrip("@")
    if value.isdigit():
        return value, None
    users = user_info.get_users([value], client_id, client_secret)
    if not users:
        sys.exit("No Twitch account called '{}'.".format(value))
    return users[0]["id"], users[0]["login"]


def fetch_page(broadcaster_id, moderator_id, token, client_id, cursor=None):
    params = {
        "broadcaster_id": broadcaster_id,
        "moderator_id": moderator_id,
        "first": PAGE_SIZE,
    }
    if cursor:
        params["after"] = cursor
    request = urllib.request.Request(
        "{}?{}".format(CHATTERS_URL, urllib.parse.urlencode(params)))
    request.add_header("Authorization", "Bearer {}".format(token))
    request.add_header("Client-Id", client_id)
    with urllib.request.urlopen(request, timeout=tv.HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def explain_failure(exc, broadcaster_label, moderator_label, mismatch=False):
    detail = exc.read().decode("utf-8", "replace")[:300]
    if exc.code == 401:
        # Twitch answers a moderator_id/token mismatch with 401, not 403.
        if mismatch or "must match the user ID" in detail:
            return ("Twitch rejected the request (401).\n{}\n\n"
                    "moderator_id has to be the account the token belongs to.\n"
                    "Drop --moderator to use it automatically, or re-authorize as {}:\n"
                    "  python3 user_auth.py --force".format(detail, moderator_label))
        return ("Twitch rejected the token (401).\n{}\n\n"
                "Re-authorize with:  python3 user_auth.py --force".format(detail))
    if exc.code == 403:
        return ("Forbidden (403).\n{}\n\n"
                "This endpoint only answers for a moderator of the channel.\n"
                "  - {} must be a moderator of {} (or be the broadcaster)\n"
                "  - the stored token must belong to {}\n\n"
                "Check who the token is for:  python3 user_auth.py --status".format(
                    detail, moderator_label, broadcaster_label, moderator_label))
    return "HTTP {} from Twitch.\n{}".format(exc.code, detail)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  python3 chatters.py themeparkgiant\n"
               "  python3 chatters.py 1457737753 --list\n"
               "  python3 chatters.py themeparkgiant --json\n")
    parser.add_argument("broadcaster", nargs="?", default=None,
                        help="channel login or user ID (default: TWITCH_CHANNEL, else "
                             "{})".format(tv.DEFAULT_CHANNEL))
    parser.add_argument("--moderator", default=None,
                        help="login or ID of the moderator (default: whoever the stored "
                             "token belongs to)")
    parser.add_argument("--list", dest="show_list", action="store_true",
                        help="also list every chatter (pages through them all)")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="print the raw first page instead")
    parser.add_argument("--count-only", action="store_true",
                        help="print just the number, for scripting")
    args = parser.parse_args()

    client_id, client_secret = tv.load_credentials()
    token_payload = user_auth.get_user_token([SCOPE])
    token = token_payload["access_token"]

    broadcaster = args.broadcaster or tv.resolve_channel(None)
    broadcaster_id, broadcaster_login = resolve_id(broadcaster, client_id, client_secret)
    broadcaster_label = broadcaster_login or broadcaster

    # The endpoint demands moderator_id == the token's own user, so default to
    # exactly that rather than making it something to get wrong.
    mismatch = False
    if args.moderator:
        moderator_id, moderator_login = resolve_id(args.moderator, client_id, client_secret)
        moderator_label = moderator_login or args.moderator
        mismatch = moderator_id != str(token_payload.get("user_id"))
        if mismatch:
            print("Warning: --moderator is {} but the stored token belongs to {}.\n"
                  "         Twitch requires them to match and will reject this.\n".format(
                      moderator_label, token_payload.get("login")), file=sys.stderr)
    else:
        moderator_id = str(token_payload.get("user_id"))
        moderator_label = token_payload.get("login", moderator_id)

    try:
        page = fetch_page(broadcaster_id, moderator_id, token, client_id)
    except urllib.error.HTTPError as exc:
        sys.exit(explain_failure(exc, broadcaster_label, moderator_label, mismatch))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        sys.exit("Could not reach Twitch: {}".format(exc))

    if args.as_json:
        print(json.dumps(page, indent=2))
        return

    total = page.get("total", 0)

    if args.count_only:
        print(total)
        return

    print("\n{} — {} {} in chat".format(
        broadcaster_label, total, "person" if total == 1 else "people"))
    print("  (as moderator {})".format(moderator_label))

    if args.show_list:
        names = [c["user_name"] for c in page.get("data", [])]
        cursor = (page.get("pagination") or {}).get("cursor")
        while cursor:
            try:
                page = fetch_page(broadcaster_id, moderator_id, token, client_id, cursor)
            except urllib.error.HTTPError as exc:
                print("\n(stopped early: HTTP {})".format(exc.code), file=sys.stderr)
                break
            names += [c["user_name"] for c in page.get("data", [])]
            cursor = (page.get("pagination") or {}).get("cursor")

        if names:
            print()
            width = max(len(n) for n in names) + 2
            per_row = max(1, 76 // width)
            for i in range(0, len(names), per_row):
                print("  " + "".join(n.ljust(width) for n in names[i:i + per_row]))
            if len(names) != total:
                print("\n  (listed {}, total reported {} — chat changed while paging)".format(
                    len(names), total))


if __name__ == "__main__":
    main()
