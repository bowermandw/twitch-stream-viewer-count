#!/usr/bin/env python3
"""Follower count (and optionally the follower list) for a Twitch channel.

    python3 followers.py themeparkgiant

The count needs nothing beyond the app token every other script here uses.
Listing *who* follows additionally needs a user access token with the
moderator:read:followers scope, belonging to the broadcaster or one of their
moderators — user_auth.py handles that, and it's only requested when asked for.
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import twitch_viewers as tv
import user_info

FOLLOWERS_URL = "https://api.twitch.tv/helix/channels/followers"
SCOPE = "moderator:read:followers"
PAGE_SIZE = 100  # endpoint maximum


def fetch_page(broadcaster_id, token, client_id, cursor=None, user_id=None, first=PAGE_SIZE):
    params = {"broadcaster_id": broadcaster_id, "first": first}
    if cursor:
        params["after"] = cursor
    if user_id:
        params["user_id"] = user_id
    request = urllib.request.Request(
        "{}?{}".format(FOLLOWERS_URL, urllib.parse.urlencode(params)))
    request.add_header("Authorization", "Bearer {}".format(token))
    request.add_header("Client-Id", client_id)
    with urllib.request.urlopen(request, timeout=tv.HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def ago(stamp):
    """'3 days ago' from an RFC3339 timestamp."""
    try:
        when = datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return stamp, ""
    delta = datetime.now(timezone.utc) - when
    days, hours = delta.days, delta.seconds // 3600
    if days >= 365:
        years, rest = divmod(days, 365)
        text = "{}y {}d ago".format(years, rest)
    elif days:
        text = "{} day{} ago".format(days, "s" * (days != 1))
    elif hours:
        text = "{} hour{} ago".format(hours, "s" * (hours != 1))
    else:
        text = "{} min ago".format(max(1, delta.seconds // 60))
    return when.astimezone().strftime("%-d %b %Y, %-I:%M %p"), text


def get_token(need_list, client_id, client_secret, prefer_scoped=False):
    """App token for a plain count; a scoped user token when names are needed.

    prefer_scoped uses an already-stored scoped token when there is one, but
    never opens a browser for it — for --json, where richer output is welcome
    but not worth interrupting the user over.
    """
    import user_auth
    if need_list:
        return user_auth.get_user_token([SCOPE])["access_token"], True
    if prefer_scoped:
        try:
            return user_auth.get_user_token([SCOPE], interactive=False)["access_token"], True
        except SystemExit:
            pass
    return tv.get_app_token(client_id, client_secret), False


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  python3 followers.py themeparkgiant\n"
               "  python3 followers.py                     # the default channel\n"
               "  python3 followers.py themeparkgiant --list\n"
               "  python3 followers.py themeparkgiant --recent 10\n"
               "  python3 followers.py themeparkgiant --check prgskidmark\n")
    parser.add_argument("channel", nargs="?", default=None,
                        help="channel login or user ID (default: TWITCH_CHANNEL, else "
                             "{})".format(tv.DEFAULT_CHANNEL))
    parser.add_argument("--list", dest="show_list", action="store_true",
                        help="list every follower (needs the moderator:read:followers scope)")
    parser.add_argument("--recent", type=int, default=None, metavar="N",
                        help="show the N most recent followers")
    parser.add_argument("--check", default=None, metavar="LOGIN",
                        help="check whether this user follows the channel")
    parser.add_argument("--count-only", action="store_true", help="print just the number")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="print the raw first page")
    args = parser.parse_args()

    client_id, client_secret = tv.load_credentials()
    tv.log = lambda message: None  # this script has no channel log to write to

    channel = args.channel or tv.resolve_channel(None)
    value = str(channel).strip().lstrip("@")
    if value.isdigit():
        broadcaster_id, label = value, value
    else:
        found = user_info.get_users([value], client_id, client_secret)
        if not found:
            sys.exit("No Twitch account called '{}'.".format(value))
        broadcaster_id, label = found[0]["id"], found[0]["display_name"]

    needs_names = bool(args.show_list or args.recent or args.check)
    token, scoped = get_token(needs_names, client_id, client_secret,
                              prefer_scoped=args.as_json)

    check_id = None
    if args.check:
        target = user_info.get_users([args.check.lstrip("@")], client_id, client_secret)
        if not target:
            sys.exit("No Twitch account called '{}'.".format(args.check))
        check_id = target[0]["id"]

    first = args.recent if args.recent else PAGE_SIZE
    try:
        page = fetch_page(broadcaster_id, token, client_id,
                          user_id=check_id, first=min(max(first, 1), PAGE_SIZE))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code in (401, 403):
            sys.exit("Twitch rejected the request ({}).\n{}\n\n"
                     "Listing followers needs a token for the broadcaster or one of\n"
                     "their moderators, with the {} scope:\n"
                     "  python3 user_auth.py --scope {}".format(
                         exc.code, detail, SCOPE, SCOPE))
        sys.exit("HTTP {} from Twitch.\n{}".format(exc.code, detail))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        sys.exit("Could not reach Twitch: {}".format(exc))

    if args.as_json:
        print(json.dumps(page, indent=2))
        return

    total = page.get("total", 0)
    rows = page.get("data", [])

    if args.count_only:
        print(total)
        return

    # --check: a hit means data contains that one user.
    if args.check:
        if rows:
            when, since = ago(rows[0]["followed_at"])
            print("\n{} follows {} — since {} ({})".format(
                rows[0]["user_name"], label, when, since))
        else:
            print("\n{} does not follow {}.".format(args.check, label))
            if not scoped:
                print("(Or the token lacks the {} scope to see it.)".format(SCOPE))
        return

    print("\n{} — {:,} follower{}".format(label, total, "" if total == 1 else "s"))

    if not needs_names:
        return

    if not rows:
        print("\nNo follower details returned. Twitch withholds them unless the token\n"
              "belongs to the broadcaster or a moderator and carries {}.\n"
              "  python3 user_auth.py --scope {}".format(SCOPE, SCOPE))
        return

    if args.recent:
        print()
        width = max(len(r["user_name"]) for r in rows) + 2
        for row in rows[:args.recent]:
            when, since = ago(row["followed_at"])
            print("  {}  {:<22} {}".format(row["user_name"].ljust(width), when, since))
        return

    # Full list: page through everything.
    names = list(rows)
    cursor = (page.get("pagination") or {}).get("cursor")
    while cursor:
        try:
            page = fetch_page(broadcaster_id, token, client_id, cursor=cursor)
        except urllib.error.HTTPError as exc:
            print("\n(stopped early: HTTP {})".format(exc.code), file=sys.stderr)
            break
        names += page.get("data", [])
        cursor = (page.get("pagination") or {}).get("cursor")

    print()
    width = max(len(r["user_name"]) for r in names) + 2
    per_row = max(1, 76 // width)
    for i in range(0, len(names), per_row):
        print("  " + "".join(r["user_name"].ljust(width) for r in names[i:i + per_row]))
    print("\n  listed {} of {} total".format(len(names), total))


if __name__ == "__main__":
    main()
