"""How many accounts are joined to a channel's chat."""

import json
import sys
import urllib.error

from .. import api, auth, config, useroauth


def add_arguments(parser):
    parser.add_argument("channel", nargs="?", default=None,
                        help="channel login or user ID (default: the configured channel)")
    parser.add_argument("--moderator", default=None,
                        help="login or ID of the moderator (default: whoever the stored "
                             "token belongs to)")
    parser.add_argument("--list", dest="show_list", action="store_true",
                        help="also list every chatter")
    parser.add_argument("--count-only", action="store_true", help="print just the number")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="print the raw first page")


def explain(exc, broadcaster_label, moderator_label, mismatch=False):
    detail = exc.read().decode("utf-8", "replace")[:300]
    if exc.code == 401:
        # Twitch answers a moderator_id/token mismatch with 401, not 403.
        if mismatch or "must match the user ID" in detail:
            return ("Twitch rejected the request (401).\n{}\n\n"
                    "moderator_id has to be the account the token belongs to.\n"
                    "Drop --moderator to use it automatically, or re-authorize as {}:\n"
                    "  twitch-metrics auth --force".format(detail, moderator_label))
        return ("Twitch rejected the token (401).\n{}\n\n"
                "Re-authorize with:  twitch-metrics auth --force".format(detail))
    if exc.code == 403:
        return ("Forbidden (403).\n{}\n\n"
                "This endpoint only answers for a moderator of the channel.\n"
                "  - {} must be a moderator of {} (or be the broadcaster)\n"
                "  - the stored token must belong to {}\n\n"
                "Check who the token is for:  twitch-metrics auth --status".format(
                    detail, moderator_label, broadcaster_label, moderator_label))
    return "HTTP {} from Twitch.\n{}".format(exc.code, detail)


def run(args):
    client_id, client_secret = config.load_credentials()
    payload = useroauth.user_token([api.SCOPE_CHATTERS])
    token = payload["access_token"]

    app = auth.app_token(client_id, client_secret)
    channel = config.resolve_channel(args.channel)
    broadcaster_id, login = api.resolve_user_id(channel, app, client_id)
    if not broadcaster_id:
        sys.exit("No Twitch account called '{}'.".format(channel))
    broadcaster_label = login or channel

    # The endpoint demands moderator_id == the token's own user, so default to
    # exactly that rather than making it something to get wrong.
    mismatch = False
    if args.moderator:
        moderator_id, mod_login = api.resolve_user_id(args.moderator, app, client_id)
        moderator_label = mod_login or args.moderator
        mismatch = moderator_id != str(payload.get("user_id"))
        if mismatch:
            print("Warning: --moderator is {} but the stored token belongs to {}.\n"
                  "         Twitch requires them to match and will reject this.\n".format(
                      moderator_label, payload.get("login")), file=sys.stderr)
    else:
        moderator_id = str(payload.get("user_id"))
        moderator_label = payload.get("login", moderator_id)

    try:
        page = api.get_chatters(broadcaster_id, moderator_id, token, client_id)
    except urllib.error.HTTPError as exc:
        sys.exit(explain(exc, broadcaster_label, moderator_label, mismatch))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        sys.exit("Could not reach Twitch: {}".format(exc))

    if args.as_json:
        print(json.dumps(page, indent=2))
        return 0

    total = page.get("total", 0)
    if args.count_only:
        print(total)
        return 0

    print("\n{} — {} {} in chat".format(broadcaster_label, total,
                                        "person" if total == 1 else "people"))
    print("  (as moderator {})".format(moderator_label))

    if args.show_list:
        names = [c["user_name"] for c in page.get("data", [])]
        cursor = (page.get("pagination") or {}).get("cursor")
        while cursor:
            try:
                page = api.get_chatters(broadcaster_id, moderator_id, token, client_id,
                                        cursor=cursor)
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
    return 0
