"""Account details from the Get Users endpoint."""

import json
import sys
import urllib.error
from datetime import datetime, timezone

from .. import api, auth, config

BROADCASTER_TYPES = {"partner": "Partner", "affiliate": "Affiliate",
                     "": "Normal (not affiliate or partner)"}
USER_TYPES = {"admin": "Twitch admin", "global_mod": "Global moderator",
              "staff": "Twitch staff", "": "Normal user"}


def add_arguments(parser):
    parser.add_argument("users", nargs="*",
                        help="login name(s), or ID(s) with --by-id. Defaults to the "
                             "configured channel.")
    parser.add_argument("--by-id", action="store_true",
                        help="treat the arguments as numeric user IDs")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="print the raw API response")


def account_age(created_at):
    try:
        created = datetime.strptime(created_at[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return created_at, ""
    days = (datetime.now(timezone.utc) - created).days
    years, rest = divmod(days, 365)
    pretty = created.astimezone().strftime("%-d %b %Y")
    if years:
        return pretty, "{} year{}, {} day{} ago".format(
            years, "s" * (years != 1), rest, "s" * (rest != 1))
    return pretty, "{} day{} ago".format(days, "s" * (days != 1))


def show(user):
    created, age = account_age(user.get("created_at", ""))
    rows = [
        ("Display name", user.get("display_name")),
        ("Login", user.get("login")),
        ("User ID", user.get("id")),
        ("Broadcaster", BROADCASTER_TYPES.get(user.get("broadcaster_type", ""),
                                              user.get("broadcaster_type"))),
        ("Account type", USER_TYPES.get(user.get("type", ""), user.get("type"))),
        ("Created", "{}  ({})".format(created, age) if age else created),
    ]
    if user.get("email"):  # only ever present with that user's own token
        rows.append(("Email", user["email"]))

    print("\n{}".format(user.get("display_name") or user.get("login")))
    print("-" * 60)
    for label, value in rows:
        print("  {:<14} {}".format(label, value if value not in (None, "") else "—"))
    print("  {:<14} {}".format("Description", (user.get("description") or "").strip() or "—"))
    for label, key in (("Profile image", "profile_image_url"),
                       ("Offline banner", "offline_image_url")):
        if user.get(key):
            print("  {:<14} {}".format(label, user[key]))
    # Twitch dropped view_count from the docs but still returns it, always zero.
    if "view_count" in user:
        print("  {:<14} {}  (deprecated field — Twitch returns 0)".format(
            "View count", user["view_count"]))


def run(args):
    client_id, client_secret = config.load_credentials()
    token = auth.app_token(client_id, client_secret)

    wanted = args.users or ([] if args.by_id else [config.resolve_channel(None)])
    if not wanted:
        sys.exit("--by-id needs at least one user ID.")
    wanted = [u.strip().lstrip("@") for u in wanted if u.strip()]

    try:
        found = api.get_users(wanted, token, client_id, by_id=args.by_id)
    except urllib.error.HTTPError as exc:
        sys.exit("HTTP {} from Twitch.\n{}".format(
            exc.code, exc.read().decode("utf-8", "replace")[:300]))

    if args.as_json:
        print(json.dumps({"data": found}, indent=2, ensure_ascii=False))
        return 0

    for user in found:
        show(user)

    key = "id" if args.by_id else "login"
    returned = {str(u.get(key, "")).lower() for u in found}
    missing = [w for w in wanted if w.lower() not in returned]
    if missing:
        print("\nNot found: {}".format(", ".join(missing)))
        print("(Twitch omits accounts that don't exist, are renamed, or are banned.)")
    return 0 if found else 1
