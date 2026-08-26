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

# A placeholder, not anyone's channel. The real one belongs in .env, which is
# gitignored — this file is published, and a default here would name a real
# channel in the repo. TWITCH_CHANNEL and YOUTUBE_CHANNEL override it.
DEFAULT_CHANNEL = "testchannel"
# A minute. Twitch's rate limit is 800 points a minute and a sample costs a
# handful, so this is a resolution decision rather than a budget one -- unlike
# the YouTube interval below, which is bounded by a fixed daily quota. Five
# minutes was the old default and it visibly under-samples: a raid or a clip
# going round moves the viewer count faster than that, and the chart drew the
# recovery as a straight line because nothing was recorded on the way down.
DEFAULT_INTERVAL_SECONDS = 60
MIN_INTERVAL_SECONDS = 10
HTTP_TIMEOUT = 20       # stops a hung socket stalling a poll loop
UPLOAD_TIMEOUT = 120    # a chart is small, but 20s is tight on a slow uplink

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"

# --- YouTube --------------------------------------------------------------
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"

DEFAULT_YOUTUBE_CHANNEL = "testchannel"   # as above: set YOUTUBE_CHANNEL in .env

# YouTube bills every request against a fixed daily pool rather than a rate
# limit, so the interval here is a budget, not a preference. One sample calls
# three endpoints at a unit each, which at 60s is 4,320 units a day — room for
# two channels. A 10-second interval, fine against Twitch, would exhaust the
# day before lunch, so it is refused rather than quietly overspending.
DEFAULT_YOUTUBE_INTERVAL_SECONDS = 60
MIN_YOUTUBE_INTERVAL_SECONDS = 60
YOUTUBE_UNITS_PER_SAMPLE = 3
YOUTUBE_DAILY_QUOTA = 10000

# --- Google Drive ---------------------------------------------------------
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"

DEFAULT_DRIVE_FOLDER = "Twitch Metrics"

# --- AWS S3 ---------------------------------------------------------------
DEFAULT_AWS_REGION = "us-east-1"

# Bucket names are global across every AWS account, so a channel's bucket gets
# a random suffix and has to be remembered rather than recomputed.
S3_BUCKETS_PATH = os.path.join(DATA_DIR, ".s3_buckets.json")

# Every bucket this project creates starts with this, so an IAM policy can be
# scoped to "arn:aws:s3:::tm-*" and a bug here cannot touch anything else in
# the account.
BUCKET_PREFIX = "tm-"


def expected_aws_account():
    """The account id this project is allowed to touch, or None for "any".

    Worth setting on any machine that can reach more than one AWS account. A
    developer laptop often has SSO profiles for a dozen of them, several with
    administrator access, and boto3's credential chain will cheerfully resolve
    to whichever one AWS_PROFILE happens to name. Pinning the id turns
    "published a stream chart into a client's production account" from a typo
    into a refusal.
    """
    value = (os.environ.get("AWS_ACCOUNT_ID")
             or load_env_file().get("AWS_ACCOUNT_ID") or "").strip()
    return value or None


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


def load_youtube_key(required=True):
    """The YouTube Data API key; environment wins, .env fills the gap.

    Unlike load_credentials(), absence is only fatal when `required`, so the
    Twitch commands keep working on a machine that never set YouTube up.
    """
    key = (os.environ.get("YOUTUBE_API_KEY")
           or load_env_file().get("YOUTUBE_API_KEY"))
    if not key and required:
        sys.exit(
            "Missing YOUTUBE_API_KEY.\n"
            "Create one at https://console.cloud.google.com/apis/credentials\n"
            "(enable \"YouTube Data API v3\" for the project first), then add it to\n"
            "{} as:\n"
            "  YOUTUBE_API_KEY=...".format(ENV_PATH))
    return key or None


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


def load_aws_credentials(required=False):
    """AWS access key and secret; environment wins, .env fills the gaps.

    (None, None) is a meaningful answer, not a failure: it means "no explicit
    keys", and boto3 then falls back to its own credential chain — an instance
    role, or ~/.aws/credentials. So this defaults to required=False, unlike
    load_credentials(), and only the commands that cannot proceed without a
    named key ask for required=True.
    """
    from_file = load_env_file()
    key = os.environ.get("AWS_ACCESS_KEY_ID") or from_file.get("AWS_ACCESS_KEY_ID")
    secret = (os.environ.get("AWS_SECRET_ACCESS_KEY")
              or from_file.get("AWS_SECRET_ACCESS_KEY"))

    missing = [name for name, value in (("AWS_ACCESS_KEY_ID", key),
                                        ("AWS_SECRET_ACCESS_KEY", secret)) if not value]
    if missing and required:
        sys.exit(
            "Missing AWS credential(s): {}\n"
            "Add them to {} as:\n"
            "  AWS_ACCESS_KEY_ID=...\n"
            "  AWS_SECRET_ACCESS_KEY=...\n"
            "Or configure them the AWS way, in ~/.aws/credentials.".format(
                ", ".join(missing), ENV_PATH))
    if missing:
        return None, None
    return key, secret


def resolve_aws_region(cli_value=None):
    """Precedence: --region > AWS_REGION > AWS_DEFAULT_REGION > DEFAULT_AWS_REGION.

    AWS_DEFAULT_REGION is honoured because that is the name the AWS CLI and the
    SDKs use, and a machine that already has one shouldn't need a second.
    """
    from_file = load_env_file()
    return (cli_value
            or os.environ.get("AWS_REGION") or from_file.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION") or from_file.get("AWS_DEFAULT_REGION")
            or DEFAULT_AWS_REGION).strip()


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


def resolve_youtube_channel(cli_value=None):
    """Precedence: command line > YOUTUBE_CHANNEL env/.env > DEFAULT_YOUTUBE_CHANNEL.

    The @ of a handle is stripped, so a value copied straight out of a channel
    URL (@testchannel) resolves the same as the bare name.
    """
    return (cli_value
            or os.environ.get("YOUTUBE_CHANNEL")
            or load_env_file().get("YOUTUBE_CHANNEL")
            or DEFAULT_YOUTUBE_CHANNEL).strip().lstrip("@")


def _interval(cli_value, name, default, minimum, note=""):
    """Shared body of the interval resolvers.

    An unparseable value is reported rather than silently falling back, since a
    typo in a service file would otherwise poll at the wrong rate unnoticed.
    """
    if cli_value is not None:
        raw, source = cli_value, "--interval"
    else:
        raw = os.environ.get(name) or load_env_file().get(name)
        source = name
        if raw is None:
            return default
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError):
        sys.exit("{} must be a whole number of seconds, got {!r}.".format(source, raw))
    if seconds < minimum:
        sys.exit("{} must be at least {} seconds, got {}.{}".format(
            source, minimum, seconds, note))
    return seconds


def resolve_interval(cli_value=None):
    """Seconds between Twitch samples.

    Precedence: --interval > TWITCH_INTERVAL env/.env > DEFAULT_INTERVAL_SECONDS.
    """
    return _interval(cli_value, "TWITCH_INTERVAL",
                     DEFAULT_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS)


def resolve_youtube_interval(cli_value=None):
    """Seconds between YouTube samples.

    Precedence: --interval > YOUTUBE_INTERVAL env/.env >
    DEFAULT_YOUTUBE_INTERVAL_SECONDS. Deliberately not falling back to
    TWITCH_INTERVAL: an interval chosen there is a rate-limit decision, and
    reusing it here would silently make it a quota decision instead.
    """
    return _interval(cli_value, "YOUTUBE_INTERVAL",
                     DEFAULT_YOUTUBE_INTERVAL_SECONDS, MIN_YOUTUBE_INTERVAL_SECONDS,
                     note="\nEach sample costs {} API quota units, out of {:,} a "
                          "day.".format(YOUTUBE_UNITS_PER_SAMPLE, YOUTUBE_DAILY_QUOTA))


# --------------------------------------------------------------------------
# the database
# --------------------------------------------------------------------------
#
# The samples live in Postgres now; data/*.csv is the spool the pollers fall
# back to while it is unreachable. Neither value below has a built-in default,
# because "no database on this machine" is a real and supported state -- it is
# what the project shipped with, and keeping it working is what lets the
# migration be done one box at a time.

DEFAULT_DB_TIMEZONE = "UTC"


def resolve_database_url():
    """The Postgres connection string, or "" when none is configured.

    TWITCH_DATABASE_URL first, then a bare DATABASE_URL. The second is honoured
    for the same reason resolve_aws_region() honours AWS_DEFAULT_REGION: it is
    the name hosts, compose files and migration tools already set, and a machine
    that has the value should not need a second copy under our own prefix.

    Prefer host:port to a Unix socket. The shipped units set
    ProtectSystem=strict with ReadWritePaths covering only data/ and charts/,
    and connecting to a socket needs write access to /run/postgresql -- so a
    postgresql:///name DSN fails there with an error that reads like an
    authentication problem. host:port also survives the database moving.
    """
    from_file = load_env_file()
    return (os.environ.get("TWITCH_DATABASE_URL")
            or from_file.get("TWITCH_DATABASE_URL")
            or os.environ.get("DATABASE_URL")
            or from_file.get("DATABASE_URL")
            or "").strip()


def resolve_db_timezone(cli_value=None):
    """The IANA zone the report tables call a day, e.g. Europe/London.

    This has to agree with Python, and it is the likeliest source of quiet
    wrongness in the whole migration. chart.select_day(), chart.days_present()
    and trends._live_on() all decide what day a sample belongs to with
    `when.astimezone().date()` -- the MACHINE's local zone. If the report tables
    bucket in a different one, they and the charts disagree about every stream
    that crosses midnight, and nothing anywhere raises.

    So the default is discovered rather than assumed, in the order that yields a
    name Postgres will actually accept:

        TWITCH_TIME_ZONE   said explicitly, and it wins
        TZ                 what the environment already says
        /etc/timezone      where Debian keeps the name, and Debian is the deploy
                           target. datetime knows the local OFFSET but often only
                           an abbreviation ("BST") for the name, and Postgres
                           will not take an abbreviation.
        /etc/localtime     macOS and systemd boxes have no /etc/timezone and
                           point this symlink into the zoneinfo tree instead.
        UTC                a last resort, at least never ambiguous

    `db --init` validates whatever comes out of this against the server, so a
    typo fails at setup instead of shifting days silently for a month.
    """
    explicit = (cli_value
                or os.environ.get("TWITCH_TIME_ZONE")
                or load_env_file().get("TWITCH_TIME_ZONE"))
    if explicit:
        return explicit.strip()
    from_env = (os.environ.get("TZ") or "").strip()
    if from_env:
        return from_env
    try:
        with open("/etc/timezone", encoding="utf-8") as handle:
            found = handle.read().strip()
        if found:
            return found
    except OSError:
        pass
    return _zone_from_localtime() or DEFAULT_DB_TIMEZONE


# Everything after the zoneinfo directory is the name, however many components
# it has. Taking a fixed two would turn America/Indiana/Indianapolis -- a real
# zone, and this developer's own -- into Indiana/Indianapolis, which Postgres
# rejects, so `db --init` would fail rather than quietly using the wrong day
# boundary. Loud either way, but there is no reason to be wrong first.
_ZONEINFO_MARKER = "zoneinfo/"


def _zone_from_localtime(path="/etc/localtime"):
    """The zone name /etc/localtime points at, or "" if it points nowhere useful."""
    try:
        target = os.path.realpath(path)
    except OSError:
        return ""
    marker = target.rfind(_ZONEINFO_MARKER)
    if marker < 0:
        return ""
    return target[marker + len(_ZONEINFO_MARKER):].strip("/")


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


def youtube_csv(channel):
    return os.path.join(DATA_DIR, "youtube_{}.csv".format(channel_slug(channel)))


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
