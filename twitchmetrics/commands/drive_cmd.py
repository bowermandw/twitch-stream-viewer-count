"""Upload rendered charts to your own Google Drive."""

import getpass
import os
import sys
import webbrowser

from .. import chart, config, drive, driveoauth

PROJECT_URL = "https://console.cloud.google.com/projectcreate"

INSTRUCTIONS = """
Google Drive setup
==================

Uploading to your own Drive needs an OAuth client. Google has no API for
creating one — it has to be done in a browser, once. About five minutes.

  1. Create a project
     {project}
     Any name, e.g. twitch-metrics.

  2. Enable the Drive API (with that project selected)
     https://console.cloud.google.com/apis/library/drive.googleapis.com

  3. Configure the OAuth consent screen
     {consent}
       User type    External  (Internal exists only on a Workspace account)
       App name     anything; support email = your own address
       Scopes       add  .../auth/drive.file
                    (per-file access: this tool can only ever see the files
                     it created itself, not the rest of your Drive)
       Test users   add your own Google account

  4. IMPORTANT — click "Publish app" so the status reads "In production".
     While it says "Testing", Google expires every refresh token after
     7 DAYS. The daily upload would work all week and then stop, with no
     obvious cause. Publishing an app that asks only for drive.file needs
     no security review — you will see a one-time "Google hasn't verified
     this app" screen when you authorize; click Advanced, then continue.
     On a Workspace account, "Internal" is the better answer and has no
     7-day limit either.

  5. Credentials -> Create credentials -> OAuth client ID
       Application type   Desktop app
     Copy the Client ID and Client secret.

     A Desktop app client accepts http://localhost redirects on any port,
     so unlike Twitch there is no redirect URL to register by hand.

Then paste both below.
""".format(project=PROJECT_URL, consent=driveoauth.CONSENT_URL)


def add_arguments(parser):
    parser.add_argument("channel", nargs="?", default=None,
                        help="channel whose chart to upload (default: the configured channel)")
    parser.add_argument("--file", default=None, metavar="PATH",
                        help="upload this file instead of the day's rendered chart")
    parser.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                        help="the day the upload represents; names the file. "
                             "Accepts 'today' and 'yesterday' (default: today)")
    parser.add_argument("--folder", default=None, metavar="NAME",
                        help="top-level Drive folder (default: GOOGLE_DRIVE_FOLDER, "
                             "else {})".format(config.DEFAULT_DRIVE_FOLDER))
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be uploaded, without touching Drive")

    parser.add_argument("--setup", action="store_true",
                        help="register Google OAuth client credentials")
    parser.add_argument("--client-id", help="skip the --setup prompt and use this value")
    parser.add_argument("--client-secret", help="skip the --setup prompt and use this value")

    parser.add_argument("--auth", action="store_true", help="authorize access to your Drive")
    parser.add_argument("--scope", action="append", default=None,
                        help="scope to request (repeatable); default: {}".format(
                            driveoauth.SCOPE_DRIVE_FILE))
    parser.add_argument("--status", action="store_true", help="show the stored token and exit")
    parser.add_argument("--check", action="store_true",
                        help="prove it works: name the account and the target folder")
    parser.add_argument("--force", action="store_true", help="re-run the browser login")
    parser.add_argument("--revoke", action="store_true", help="revoke and delete the token")
    parser.add_argument("--no-browser", action="store_true",
                        help="print the URL instead of opening a browser")
    parser.add_argument("--manual", action="store_true",
                        help="headless flow: paste the redirect URL back instead of "
                             "listening on the callback port. Use on a server with no "
                             "browser and no SSH tunnel.")


def confirm(prompt, default_yes=True):
    try:
        answer = input("{} {} ".format(prompt, "[Y/n]" if default_yes else "[y/N]")).strip().lower()
    except EOFError:
        return default_yes
    return default_yes if not answer else answer in ("y", "yes")


def _setup(args, scopes):
    """Collect a Google OAuth client, prove it works, then write .env.

    setup_cmd verifies before writing by asking Twitch for a token. Google has
    no equivalent — there is no client-credentials grant for user data — so the
    proof is the authorization exchange itself, which cannot succeed without a
    correct client id AND secret. So .env is written the moment authorize()
    returns, and the Drive call after it is a bonus check: once the credentials
    are known good, a folder hiccup must not throw them away and make you type
    them again.
    """
    non_interactive = bool(args.client_id and args.client_secret)

    if non_interactive:
        client_id, client_secret = args.client_id.strip(), args.client_secret.strip()
    else:
        print(INSTRUCTIONS)
        if not args.no_browser and confirm("Open the Google Cloud console now?"):
            try:
                webbrowser.open(PROJECT_URL)
            except Exception:  # noqa: BLE001
                print("Couldn't open a browser — visit {} manually.".format(PROJECT_URL))
        print()
        try:
            client_id = input("Client ID:     ").strip()
            client_secret = getpass.getpass("Client Secret: (hidden) ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled. Nothing was written.")
            return 1

    if not client_id or not client_secret:
        print("\nBoth values are required. Nothing was written.")
        return 1

    # Warn and confirm rather than reject, the way `setup` does for Twitch's
    # values — Google could change these shapes and a hard check would then
    # block a credential that works.
    for label, value, looks_right, hint in (
            ("Client ID", client_id,
             client_id.endswith(".apps.googleusercontent.com"),
             "usually ends .apps.googleusercontent.com"),
            ("Client Secret", client_secret,
             client_secret.startswith("GOCSPX-"),
             "usually starts GOCSPX-")):
        if looks_right:
            continue
        print("\nWarning: that {} doesn't look like a Google credential ({}).".format(
            label, hint))
        if not non_interactive and not confirm("Continue anyway?", default_yes=False):
            print("Cancelled. Nothing was written.")
            return 1

    if not non_interactive and not confirm("\nAuthorize now?"):
        print("\nNothing was written — re-run  {} drive --setup  when you can reach\n"
              "a browser.".format(config.invocation()))
        return 1

    # An authorization from an earlier run that already works is worth reusing:
    # this is the recovery path when setup got as far as the browser and then
    # stopped before .env was written.
    payload = driveoauth.load_token()
    if payload and payload.get("refresh_token") and driveoauth.about(
            payload["access_token"]):
        print("\nAlready authorized as {} — keeping that token.".format(
            payload.get("email") or "?"))
        print("Re-run with --force if you want to authorize again.")
    else:
        payload = driveoauth.authorize(
            client_id, client_secret, scopes,
            open_browser=not (args.no_browser or args.manual),
            manual=args.manual)

    # Written now, not after the folder check: authorize() succeeding already
    # proves both values, since the token exchange requires the secret.
    config.update_env({"GOOGLE_CLIENT_ID": client_id,
                       "GOOGLE_CLIENT_SECRET": client_secret})
    print("\nWrote {} (permissions 0600).".format(config.ENV_PATH))

    print("\nVerifying against Drive...")
    folder = config.resolve_drive_folder(args.folder)
    try:
        drive.folder_path(drive.client_for(payload), [folder])
    except (drive.DriveError, OSError, ValueError) as exc:
        print("  Could not resolve the '{}' folder: {}".format(folder, exc))
        print("\nThe credentials are saved and valid — only the folder check failed.")
        print("Try:  {} drive --check".format(config.invocation()))
        return 1
    print("  Resolved the '{}' folder in {}'s Drive.".format(
        folder, payload.get("email") or "your account"))

    print("\nSetup complete. Try it with:\n")
    print("    {} daily --dry-run\n".format(config.invocation()))
    return 0


def _check(args):
    """Resolve the real target folder, non-interactively, as the service will."""
    client = drive.connect(interactive=False)
    print("  account     {}".format(client.get("email") or "?"))
    folder = config.resolve_drive_folder(args.folder)
    channel = config.resolve_channel(args.channel)
    parent = drive.channel_folder(client, channel, args.folder)
    print("  folder      {}/{}".format(folder, drive.channel_folder_name(channel)))
    print("  folder id   {}".format(parent))
    print("  uploads to  {}".format(drive.target_path(
        channel, chart.parse_day("today"), args.folder)))
    return 0


def run(args):
    config.ensure_dirs()
    scopes = args.scope or [driveoauth.SCOPE_DRIVE_FILE]

    if args.setup:
        return _setup(args, scopes)

    # Deliberately ahead of anything that reads credentials or opens a socket:
    # `--status` has to work on a machine that never set Drive up.
    if args.status:
        driveoauth.describe(driveoauth.load_token())
        return 0

    if args.revoke:
        print("Revoked and deleted the stored Google token." if driveoauth.revoke()
              else "Nothing stored.")
        return 0

    if args.auth or args.force or args.manual:
        client_id, client_secret = config.load_google_credentials()
        driveoauth.authorize(client_id, client_secret, scopes,
                             open_browser=not (args.no_browser or args.manual),
                             manual=args.manual)
        print()
        driveoauth.describe(driveoauth.load_token())
        return 0

    if args.check:
        return _check(args)

    # A one-off upload. Everything that can be checked locally is checked before
    # a socket is opened, so this stays testable with no network and no token.
    channel = config.resolve_channel(args.channel)
    try:
        day = chart.parse_day(args.date or "today")
    except ValueError as exc:
        sys.exit(str(exc).replace("Bad date", "Bad --date"))

    path = args.file or config.chart_png_path(channel, "_" + day.isoformat())
    if not os.path.exists(path):
        sys.exit("Nothing to upload at {}\n"
                 "Render it first:  {prog} daily {} --date {} --dry-run\n"
                 "Or name a file:   {prog} drive {} --file path/to/chart.png".format(
                     path, channel, day.isoformat(), channel, prog=config.invocation()))

    target = drive.target_path(channel, day, args.folder, os.path.splitext(path)[1] or ".png")
    if args.dry_run:
        print("Would upload {}\n          to {}".format(path, target))
        return 0

    uploaded, failed = drive.upload_charts([(path, channel, day)], args.folder)
    return 1 if failed else 0
