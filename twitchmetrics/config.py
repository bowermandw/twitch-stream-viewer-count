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

DEFAULT_CHANNEL = "themeparkgiant"
INTERVAL_SECONDS = 300  # 5 minutes
HTTP_TIMEOUT = 20       # stops a hung socket stalling a poll loop

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"


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


def resolve_channel(cli_value=None):
    """Precedence: command line > TWITCH_CHANNEL env/.env > DEFAULT_CHANNEL."""
    return (cli_value
            or os.environ.get("TWITCH_CHANNEL")
            or load_env_file().get("TWITCH_CHANNEL")
            or DEFAULT_CHANNEL).strip()


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
