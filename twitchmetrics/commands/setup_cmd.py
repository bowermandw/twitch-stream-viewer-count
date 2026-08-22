"""Collect Twitch API credentials, verify them, write .env."""

import getpass
import urllib.error
import webbrowser

from .. import api, auth, config
from ..logging import note

CONSOLE_URL = "https://dev.twitch.tv/console/apps/create"

INSTRUCTIONS = """
Twitch credentials setup
========================

You need a Client ID and Client Secret from a Twitch application. Twitch has no
API for this — it has to be done in a browser, once. It takes about 2 minutes.

  1. Open  {url}
     (log in; the dev console requires 2FA on your Twitch account)

  2. Fill in the form:
       Name                 anything unique, e.g. themeparkgiant-viewer-log
       OAuth Redirect URLs  http://localhost:3000
                            (required field; used only by `auth`)
       Category             Analytics Tool
       Client Type          Confidential

  3. Click Create, then Manage on the new app.

  4. Copy the Client ID, then click New Secret and copy that too.
     The secret is shown ONLY ONCE.

Then paste both below.
""".format(url=CONSOLE_URL)


def add_arguments(parser):
    parser.add_argument("--client-id", help="skip the prompt and use this value")
    parser.add_argument("--client-secret", help="skip the prompt and use this value")
    parser.add_argument("--channel", default=None, help="channel to test against")
    parser.add_argument("--no-browser", action="store_true",
                        help="don't try to open the dev console")


def confirm(prompt, default_yes=True):
    try:
        answer = input("{} {} ".format(prompt, "[Y/n]" if default_yes else "[y/N]")).strip().lower()
    except EOFError:
        return default_yes
    return default_yes if not answer else answer in ("y", "yes")


def verify(client_id, client_secret, channel):
    """Prove the credentials work with a token request and a real API call."""
    print("\nVerifying against the Twitch API...")
    try:
        token = auth.request_new_token(client_id, client_secret)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        if exc.code in (400, 401, 403):
            print("\n  FAILED: Twitch rejected these credentials (HTTP {}).".format(exc.code))
            print("  {}".format(detail))
            print("\n  Check for a typo, or generate a New Secret in the console")
            print("  (generating one invalidates the previous secret).")
        else:
            print("\n  FAILED: HTTP {} from Twitch. {}".format(exc.code, detail))
        return False
    except (urllib.error.URLError, OSError) as exc:
        print("\n  FAILED: could not reach Twitch: {}".format(exc))
        return False

    print("  Access token obtained.")
    try:
        stream = api.get_stream(channel, token, client_id)
    except Exception as exc:  # noqa: BLE001
        print("  FAILED: token works but the streams call failed: {}".format(exc))
        return False

    if stream is None:
        print("  Streams API reachable — {} is currently OFFLINE.".format(channel))
        print("  (A valid result, and it confirms everything works.)")
    else:
        print("  Streams API reachable — {} is LIVE with {} viewers.".format(
            channel, stream.get("viewer_count")))
    return True


def run(args):
    config.ensure_dirs()
    channel = config.resolve_channel(args.channel)
    non_interactive = bool(args.client_id and args.client_secret)

    existing = config.load_env_file()
    if existing.get("TWITCH_CLIENT_ID") and not non_interactive:
        shown = existing["TWITCH_CLIENT_ID"]
        print("An existing .env was found with client ID: {}...{}".format(shown[:6], shown[-4:]))
        if not confirm("Replace it?", default_yes=False):
            if verify(shown, existing.get("TWITCH_CLIENT_SECRET", ""), channel):
                print("\nExisting credentials are valid. Nothing to do.")
                print("Run:  twitch-metrics poll {}".format(channel))
                return 0
            print("\nExisting credentials do NOT work — let's replace them.")
        print()

    if non_interactive:
        client_id, client_secret = args.client_id.strip(), args.client_secret.strip()
    else:
        print(INSTRUCTIONS)
        if not args.no_browser and confirm("Open the Twitch dev console now?"):
            try:
                webbrowser.open(CONSOLE_URL)
            except Exception:  # noqa: BLE001
                print("Couldn't open a browser — visit {} manually.".format(CONSOLE_URL))
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

    for label, value in (("Client ID", client_id), ("Client Secret", client_secret)):
        if len(value) < 20 or not value.isalnum():
            print("\nWarning: that {} doesn't look like a Twitch credential "
                  "(expected ~30 alphanumeric characters, got {}).".format(label, len(value)))
            if not non_interactive and not confirm("Continue anyway?", default_yes=False):
                print("Cancelled. Nothing was written.")
                return 1

    if not verify(client_id, client_secret, channel):
        print("\nNothing was written to .env. Fix the above and re-run:  twitch-metrics setup")
        return 1

    config.write_env(client_id, client_secret)
    print("\nWrote {} (permissions 0600).".format(config.ENV_PATH))
    print("\nSetup complete. Start collecting data with:\n")
    print("    twitch-metrics poll {}\n".format(channel))
    return 0
