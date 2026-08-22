"""Follower count, list, and follow checks."""

import json
import sys
import urllib.error
from datetime import datetime, timezone

from .. import api, auth, config, useroauth


def add_arguments(parser):
    parser.add_argument("channel", nargs="?", default=None,
                        help="channel login or user ID (default: the configured channel)")
    parser.add_argument("--list", dest="show_list", action="store_true",
                        help="list every follower (needs the {} scope)".format(
                            api.SCOPE_FOLLOWERS))
    parser.add_argument("--recent", type=int, default=None, metavar="N",
                        help="show the N most recent followers")
    parser.add_argument("--check", default=None, metavar="LOGIN",
                        help="check whether this user follows the channel")
    parser.add_argument("--count-only", action="store_true", help="print just the number")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="print the raw first page")


def ago(stamp):
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


def pick_token(need_names, client_id, client_secret, prefer_scoped=False):
    """App token for a plain count; a scoped user token when names are needed.

    prefer_scoped reuses an already-stored scoped token but never opens a
    browser for one — richer --json output isn't worth interrupting for.
    """
    if need_names:
        return useroauth.user_token([api.SCOPE_FOLLOWERS])["access_token"], True
    if prefer_scoped:
        try:
            return useroauth.user_token([api.SCOPE_FOLLOWERS],
                                        interactive=False)["access_token"], True
        except SystemExit:
            pass
    return auth.app_token(client_id, client_secret), False


def run(args):
    client_id, client_secret = config.load_credentials()
    channel = config.resolve_channel(args.channel)

    app = auth.app_token(client_id, client_secret)
    broadcaster_id, login = api.resolve_user_id(channel, app, client_id)
    if not broadcaster_id:
        sys.exit("No Twitch account called '{}'.".format(channel))
    label = login or channel

    need_names = bool(args.show_list or args.recent or args.check)
    token, scoped = pick_token(need_names, client_id, client_secret,
                               prefer_scoped=args.as_json)

    check_id = None
    if args.check:
        check_id, _ = api.resolve_user_id(args.check, app, client_id)
        if not check_id:
            sys.exit("No Twitch account called '{}'.".format(args.check))

    first = min(max(args.recent or api.MAX_PAGE, 1), api.MAX_PAGE)
    try:
        page = api.get_followers(broadcaster_id, token, client_id,
                                 user_id=check_id, first=first)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code in (401, 403):
            sys.exit("Twitch rejected the request ({}).\n{}\n\n"
                     "Listing followers needs a token for the broadcaster or one of\n"
                     "their moderators, with the {} scope:\n"
                     "  twitch-metrics auth --scope {}".format(
                         exc.code, detail, api.SCOPE_FOLLOWERS, api.SCOPE_FOLLOWERS))
        sys.exit("HTTP {} from Twitch.\n{}".format(exc.code, detail))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        sys.exit("Could not reach Twitch: {}".format(exc))

    if args.as_json:
        print(json.dumps(page, indent=2))
        return 0

    total, rows = page.get("total", 0), page.get("data", [])

    if args.count_only:
        print(total)
        return 0

    if args.check:
        if rows:
            when, since = ago(rows[0]["followed_at"])
            print("\n{} follows {} — since {} ({})".format(
                rows[0]["user_name"], label, when, since))
        else:
            print("\n{} does not follow {}.".format(args.check, label))
            if not scoped:
                print("(Or the token lacks the {} scope to see it.)".format(
                    api.SCOPE_FOLLOWERS))
        return 0

    print("\n{} — {:,} follower{}".format(label, total, "" if total == 1 else "s"))
    if not need_names:
        return 0

    if not rows:
        print("\nNo follower details returned. Twitch withholds them unless the token\n"
              "belongs to the broadcaster or a moderator and carries {}.\n"
              "  twitch-metrics auth --scope {}".format(api.SCOPE_FOLLOWERS,
                                                        api.SCOPE_FOLLOWERS))
        return 0

    if args.recent:
        print()
        width = max(len(r["user_name"]) for r in rows) + 2
        for row in rows[:args.recent]:
            when, since = ago(row["followed_at"])
            print("  {}  {:<22} {}".format(row["user_name"].ljust(width), when, since))
        return 0

    names = list(rows)
    cursor = (page.get("pagination") or {}).get("cursor")
    while cursor:
        try:
            page = api.get_followers(broadcaster_id, token, client_id, cursor=cursor)
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
    return 0
