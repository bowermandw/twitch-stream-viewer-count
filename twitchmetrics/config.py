"""Paths, credentials and channel resolution.

Everything the package writes goes under data/ (samples and logs) or charts/
(rendered SVGs), so the project root stays clean and both are easy to mount as
a volume or exclude from backups.
"""

import os
import re
import stat
import sys

# .../twitchmetrics/config.py -> project root
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PACKAGE_DIR)

# Overridable so a server can point at a mounted volume.
DATA_DIR = os.environ.get("TWITCH_DATA_DIR") or os.path.join(ROOT, "data")
CHARTS_DIR = os.environ.get("TWITCH_CHARTS_DIR") or os.path.join(ROOT, "charts")

ENV_PATH = os.environ.get("TWITCH_ENV_FILE") or os.path.join(ROOT, ".env")
TOKEN_CACHE_PATH = os.path.join(DATA_DIR, ".app_token.json")
USER_TOKEN_PATH = os.path.join(DATA_DIR, ".user_token.json")
GOOGLE_TOKEN_PATH = os.path.join(DATA_DIR, ".google_token.json")

# Drive folder ids, remembered so a folder you rename or move by hand keeps
# being used instead of a second one appearing beside it.
DRIVE_FOLDERS_PATH = os.path.join(DATA_DIR, ".drive_folders.json")

DEFAULT_CHANNEL = "themeparkgiant"
DEFAULT_INTERVAL_SECONDS = 300  # 5 minutes
MIN_INTERVAL_SECONDS = 10
HTTP_TIMEOUT = 20       # stops a hung socket stalling a poll loop
UPLOAD_TIMEOUT = 120    # a chart is small, but 20s is tight on a slow uplink

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"

DEFAULT_DRIVE_FOLDER = "Twitch Metrics"


def invocation():
    """How this program was actually started, for help text and hints.

    Running `python3 -m twitchmetrics` shouldn't print advice to type
    `twitch-metrics`, which only exists if the console script was installed.
    """
    name = os.path.basename(sys.argv[0] or "")
    if name in ("__main__.py", "-c", ""):
        return "python3 -m twitchmetrics"
    return name


def ensure_dirs():
    for path in (DATA_DIR, CHARTS_DIR):
        os.makedirs(path, exist_ok=True)


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------


def load_env_file(path=None):
    """Minimal .env parser: KEY=value, skipping blanks and # comments."""
    path = path or ENV_PATH
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values


def load_credentials():
    """Environment wins; .env fills the gaps. Exits naming any missing key."""
    from_file = load_env_file()
    client_id = os.environ.get("TWITCH_CLIENT_ID") or from_file.get("TWITCH_CLIENT_ID")
    client_secret = (os.environ.get("TWITCH_CLIENT_SECRET")
                     or from_file.get("TWITCH_CLIENT_SECRET"))

    missing = [name for name, value in (("TWITCH_CLIENT_ID", client_id),
                                        ("TWITCH_CLIENT_SECRET", client_secret)) if not value]
    if missing:
        sys.exit(
            "Missing credential(s): {}\n"
            "Add them to {} as:\n"
            "  TWITCH_CLIENT_ID=...\n"
            "  TWITCH_CLIENT_SECRET=...\n"
            "Or run:  {} setup".format(", ".join(missing), ENV_PATH, invocation()))
    return client_id, client_secret


def write_env(client_id, client_secret):
    with open(ENV_PATH, "w", encoding="utf-8") as handle:
        handle.write("# Twitch API credentials — created by `{} setup`\n".format(invocation()))
        handle.write("# Keep this file private; it is gitignored.\n")
        handle.write("TWITCH_CLIENT_ID={}\n".format(client_id))
        handle.write("TWITCH_CLIENT_SECRET={}\n".format(client_secret))
    os.chmod(ENV_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600


def load_google_credentials(required=True):
    """Google OAuth client id/secret; environment wins, .env fills the gaps.

    Unlike load_credentials(), absence is only fatal when `required`. Every
    other command has to keep working on a machine that never set up Drive, so
    a missing key here can't be allowed to become a global failure.
    """
    from_file = load_env_file()
    client_id = os.environ.get("GOOGLE_CLIENT_ID") or from_file.get("GOOGLE_CLIENT_ID")
    client_secret = (os.environ.get("GOOGLE_CLIENT_SECRET")
                     or from_file.get("GOOGLE_CLIENT_SECRET"))

    missing = [name for name, value in (("GOOGLE_CLIENT_ID", client_id),
                                        ("GOOGLE_CLIENT_SECRET", client_secret)) if not value]
    if missing and required:
        sys.exit(
            "Missing Google credential(s): {}\n"
            "Add them to {} as:\n"
            "  GOOGLE_CLIENT_ID=...\n"
            "  GOOGLE_CLIENT_SECRET=...\n"
            "Or run:  {} drive --setup".format(", ".join(missing), ENV_PATH, invocation()))
    if missing:
        return None, None
    return client_id, client_secret


def update_env(values):
    """Merge KEY=value pairs into .env, preserving every other line, at 0600.

    write_env() truncates, which is right for `setup` (it owns the whole file
    on a fresh install) and wrong for anything added later — reusing it to store
    the Google keys would silently delete TWITCH_CHANNEL and TWITCH_INTERVAL.
    """
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as handle:
            lines = handle.read().splitlines()

    remaining = dict(values)
    out = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip()
        if stripped and not stripped.startswith("#") and "=" in stripped and key in remaining:
            out.append("{}={}".format(key, remaining.pop(key)))
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append("{}={}".format(key, value))

    temporary = ENV_PATH + ".tmp{}".format(os.getpid())
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write("\n".join(out).rstrip("\n") + "\n")
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    os.replace(temporary, ENV_PATH)


def resolve_drive_folder(cli_value=None):
    """Precedence: --folder > GOOGLE_DRIVE_FOLDER env/.env > DEFAULT_DRIVE_FOLDER."""
    return (cli_value
            or os.environ.get("GOOGLE_DRIVE_FOLDER")
            or load_env_file().get("GOOGLE_DRIVE_FOLDER")
            or DEFAULT_DRIVE_FOLDER).strip()


def resolve_channel(cli_value=None):
    """Precedence: command line > TWITCH_CHANNEL env/.env > DEFAULT_CHANNEL."""
    return (cli_value
            or os.environ.get("TWITCH_CHANNEL")
            or load_env_file().get("TWITCH_CHANNEL")
            or DEFAULT_CHANNEL).strip()


def resolve_interval(cli_value=None):
    """Seconds between samples.

    Precedence: --interval > TWITCH_INTERVAL env/.env > DEFAULT_INTERVAL_SECONDS.
    An unparseable value is reported rather than silently falling back, since a
    typo in a service file would otherwise poll at the wrong rate unnoticed.
    """
    if cli_value is not None:
        raw, source = cli_value, "--interval"
    else:
        raw = os.environ.get("TWITCH_INTERVAL") or load_env_file().get("TWITCH_INTERVAL")
        source = "TWITCH_INTERVAL"
        if raw is None:
            return DEFAULT_INTERVAL_SECONDS
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError):
        sys.exit("{} must be a whole number of seconds, got {!r}.".format(source, raw))
    if seconds < MIN_INTERVAL_SECONDS:
        sys.exit("{} must be at least {} seconds, got {}.".format(
            source, MIN_INTERVAL_SECONDS, seconds))
    return seconds


# --------------------------------------------------------------------------
# per-channel file names
# --------------------------------------------------------------------------


def channel_slug(channel):
    """Filesystem-safe form of a channel login, lowercased so IGN == ign."""
    slug = re.sub(r"[^A-Za-z0-9_-]", "_", str(channel).strip().lower())
    return slug or "channel"


def viewers_csv(channel):
    return os.path.join(DATA_DIR, "viewers_{}.csv".format(channel_slug(channel)))


def metrics_csv(channel):
    return os.path.join(DATA_DIR, "metrics_{}.csv".format(channel_slug(channel)))


def log_path(channel, kind="poll"):
    return os.path.join(DATA_DIR, "{}_{}.log".format(kind, channel_slug(channel)))


def chart_path(channel, suffix=""):
    return os.path.join(CHARTS_DIR, "chart_{}{}.svg".format(channel_slug(channel), suffix))


def chart_png_path(channel, suffix=""):
    """The PNG twin of chart_path() — where rsvg-convert writes."""
    return os.path.join(CHARTS_DIR, "chart_{}{}.png".format(channel_slug(channel), suffix))


def daily_log_path():
    """One log for the whole daily report; it isn't per-channel like the pollers'."""
    return os.path.join(DATA_DIR, "daily.log")
