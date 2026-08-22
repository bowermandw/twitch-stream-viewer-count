#!/usr/bin/env python3
"""Look up Twitch account details with the Helix Get Users endpoint.

    python3 user_info.py prgskidmark
    python3 user_info.py                  # falls back to the default channel

Reuses the credentials and cached app access token from twitch_viewers.py, so
no extra setup is needed once setup.py has been run.
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import twitch_viewers as tv

USERS_URL = "https://api.twitch.tv/helix/users"
MAX_PER_REQUEST = 100  # Twitch's documented limit for login/id parameters

BROADCASTER_TYPES = {
    "partner": "Partner",
    "affiliate": "Affiliate",
    "": "Normal (not affiliate or partner)",
}
USER_TYPES = {"admin": "Twitch admin", "global_mod": "Global moderator",
              "staff": "Twitch staff", "": "Normal user"}


def quiet_log(message):
    """twitch_viewers.log() appends to a per-channel poll log; this script has
    no channel, so keep its auth chatter on stdout instead of creating a file."""
    print("  {}".format(message))


tv.log = quiet_log


def fetch_users(values, token, client_id, by_id=False):
    """One Get Users call for up to 100 logins (or ids). Returns the data list."""
    key = "id" if by_id else "login"
    query = urllib.parse.urlencode([(key, v) for v in values])
    request = urllib.request.Request("{}?{}".format(USERS_URL, query), method="GET")
    request.add_header("Authorization", "Bearer {}".format(token))
    request.add_header("Client-Id", client_id)
    with urllib.request.urlopen(request, timeout=tv.HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8")).get("data", [])


def get_users(values, client_id, client_secret, by_id=False):
    """Fetch in batches, refreshing the token once if Twitch rejects it."""
    token = tv.get_app_token(client_id, client_secret)
    found = []

    for start in range(0, len(values), MAX_PER_REQUEST):
        batch = values[start:start + MAX_PER_REQUEST]
        try:
            found += fetch_users(batch, token, client_id, by_id)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                token = tv.get_app_token(client_id, client_secret, force_refresh=True)
                found += fetch_users(batch, token, client_id, by_id)
            elif exc.code == 400:
                sys.exit("Twitch rejected the request (400): {}".format(
                    exc.read().decode("utf-8", "replace")[:300]))
            else:
                sys.exit("HTTP {} from Twitch.".format(exc.code))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            sys.exit("Could not reach Twitch: {}".format(exc))
    return found


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
        ("Broadcaster", BROADCASTER_TYPES.get(
            user.get("broadcaster_type", ""), user.get("broadcaster_type"))),
        ("Account type", USER_TYPES.get(user.get("type", ""), user.get("type"))),
        ("Created", "{}  ({})".format(created, age) if age else created),
    ]
    # Only present with a user access token for that same user.
    if user.get("email"):
        rows.append(("Email", user["email"]))

    print("\n{}".format(user.get("display_name") or user.get("login")))
    print("-" * 60)
    for label, value in rows:
        print("  {:<14} {}".format(label, value if value not in (None, "") else "—"))

    description = (user.get("description") or "").strip()
    print("  {:<14} {}".format("Description", description or "—"))
    for label, key in (("Profile image", "profile_image_url"),
                       ("Offline banner", "offline_image_url")):
        if user.get(key):
            print("  {:<14} {}".format(label, user[key]))

    # Twitch removed view_count from this endpoint; flag it if it reappears.
    if "view_count" in user:
        print("  {:<14} {}  (deprecated field — Twitch returns 0)".format(
            "View count", user["view_count"]))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  python3 user_info.py prgskidmark\n"
               "  python3 user_info.py                 # the default channel\n"
               "  python3 user_info.py ign prgskidmark themeparkgiant\n"
               "  python3 user_info.py 141981764 --by-id\n"
               "  python3 user_info.py prgskidmark --json\n")
    parser.add_argument(
        "users", nargs="*",
        help="login name(s), or ID(s) with --by-id. Defaults to TWITCH_CHANNEL "
             "from .env, else {}.".format(tv.DEFAULT_CHANNEL))
    parser.add_argument("--by-id", action="store_true",
                        help="treat the arguments as numeric user IDs")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="print the raw API response instead")
    args = parser.parse_args()

    client_id, client_secret = tv.load_credentials()

    # Same precedence as the poller: command line > TWITCH_CHANNEL > default.
    users = args.users or ([] if args.by_id else [tv.resolve_channel(None)])
    if not users:
        sys.exit("--by-id needs at least one user ID.")
    wanted = [u.strip().lstrip("@") for u in users if u.strip()]
    found = get_users(wanted, client_id, client_secret, by_id=args.by_id)

    if args.as_json:
        print(json.dumps({"data": found}, indent=2, ensure_ascii=False))
        return

    for user in found:
        show(user)

    # Twitch silently omits unknown accounts rather than erroring.
    key = "id" if args.by_id else "login"
    returned = {str(u.get(key, "")).lower() for u in found}
    missing = [w for w in wanted if w.lower() not in returned]
    if missing:
        print("\nNot found: {}".format(", ".join(missing)))
        print("(Twitch omits accounts that don't exist, are renamed, or are banned.)")
    if not found:
        sys.exit(1)


if __name__ == "__main__":
    main()
