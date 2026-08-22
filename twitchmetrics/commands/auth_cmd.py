"""Manage the user access token."""

from .. import api, config, useroauth

DEFAULT_SCOPES = [api.SCOPE_CHATTERS, api.SCOPE_FOLLOWERS]


def add_arguments(parser):
    parser.add_argument("--scope", action="append", default=None,
                        help="scope to request (repeatable); default: {}".format(
                            " ".join(DEFAULT_SCOPES)))
    parser.add_argument("--status", action="store_true", help="show the stored token and exit")
    parser.add_argument("--force", action="store_true", help="re-run the browser login")
    parser.add_argument("--revoke", action="store_true", help="revoke and delete the token")
    parser.add_argument("--no-browser", action="store_true",
                        help="print the URL instead of opening a browser")
    parser.add_argument("--manual", action="store_true",
                        help="headless flow: paste the redirect URL back instead of "
                             "listening on the callback port. Use on a server with no "
                             "browser and no SSH tunnel.")


def run(args):
    config.ensure_dirs()
    scopes = args.scope or list(DEFAULT_SCOPES)

    if args.status:
        useroauth.describe(useroauth.load_token())
        return 0

    if args.revoke:
        print("Revoked and deleted the stored token." if useroauth.revoke()
              else "Nothing stored.")
        return 0

    if args.force or args.manual:
        client_id, client_secret = config.load_credentials()
        useroauth.authorize(client_id, client_secret, scopes,
                            open_browser=not (args.no_browser or args.manual),
                            manual=args.manual)
    else:
        useroauth.user_token(scopes)

    print()
    useroauth.describe(useroauth.load_token())
    return 0
