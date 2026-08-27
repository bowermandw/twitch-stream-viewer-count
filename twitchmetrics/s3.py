"""Publishing the daily charts to an S3 static website, one bucket per channel.

Each channel gets its own bucket serving two pages: today's graph for every
platform it is polled on with prior days as links, and a Trends page carrying
the multi-day charts. The bucket holds nothing but those and the SVGs:

    index.html
    trends.html
    day/2026-08-24.html
    twitch/2026-08-24.svg
    youtube/2026-08-24.svg
    trends/peaks-twitch.svg

Both pages are rebuilt from a listing of the bucket rather than from anything
kept locally, so a run after a week's gap still produces a correct index, a
chart uploaded by hand shows up in it, and neither page ever links an image that
isn't there.

boto3 is imported lazily, inside _boto3(). cli.py imports every command module
at startup, so a module-scope import here would take the whole CLI down on a
machine that only polls — and polling is meant to need nothing installed.
"""

import html
import json
import os
import re
import secrets
import stat
from datetime import date

from . import config, retry
from .logging import log
from .trends import SIZES as TREND_SIZES

INDEX_KEY = "index.html"
TRENDS_KEY = "trends.html"
TITLES_KEY = "titles.json"
TRENDS_PREFIX = "trends/"
DAY_PREFIX = "day/"
SVG_TYPE = "image/svg+xml"

# Not `immutable`: re-running the report for today legitimately replaces today's
# chart, and a year-long cache would hide that from anyone who had already
# looked. Five minutes is long enough to be worth having.
CHART_CACHE = "public, max-age=300"

MAX_BUCKET_NAME = 63   # a bucket name is a DNS label, and that is the limit

# The suffix does two jobs, and the second sets the size. Bucket names are
# global, so it has to avoid a collision — a couple of bytes would do. But the
# site is public-read and its only protection is that nobody knows the URL, and
# the channel name is guessable, so this is also the whole keyspace an outsider
# would have to search. Ten hex characters is a trillion; six would be a
# weekend's worth of requests.
SUFFIX_BYTES = 5

# Display order on the page. "combined" is not a platform but shares the key
# layout, so it rides the same list and lands at the top of the page.
PLATFORMS = ("combined", "twitch", "youtube")
PLATFORM_LABELS = {"combined": "Both platforms", "twitch": "Twitch",
                   "youtube": "YouTube"}

# Whose title the page shows, most authoritative first. They differ for the
# same broadcast, so one has to win rather than both being printed.
TITLE_PREFERENCE = ("twitch", "youtube")

# The multi-day charts, in the order the Trends page shows them. They are one
# per platform and not per day — each run overwrites them, because they describe
# where the channel is now rather than what happened on a particular date.
TREND_KINDS = ("peaks", "typical")
TREND_LABELS = {
    ("peaks", "twitch"): "Peak viewers by day · Twitch",
    ("peaks", "youtube"): "Peak viewers by day · YouTube",
    ("typical", "twitch"): "Half-hour averages, today vs before · Twitch",
    ("typical", "youtube"): "Half-hour averages, today vs before · YouTube",
}

# These nine regions predate the dotted website endpoint and still answer on
# s3-website-<region>; everything since uses s3-website.<region>. It is frozen
# history rather than a rule, so it is a list and not an algorithm.
DASH_REGIONS = frozenset((
    "us-east-1", "us-west-1", "us-west-2", "eu-west-1", "ap-southeast-1",
    "ap-southeast-2", "ap-northeast-1", "sa-east-1", "us-gov-west-1",
))

# "Slow down" or "try again", as opposed to "you may not" — retrying an
# AccessDenied four times just wastes fifteen seconds and says the same thing.
RETRY_CODES = ("SlowDown", "RequestTimeout", "RequestTimeoutException",
               "InternalError", "ServiceUnavailable", "RequestTimeTooSkewed",
               "TooManyRequests", "RequestThrottled", "ThrottlingException")

KEY_RE = re.compile(r"^([a-z]+)/(\d{4}-\d{2}-\d{2})\.svg$")
TREND_KEY_RE = re.compile(r"^" + TRENDS_PREFIX + r"([a-z]+)-([a-z]+)\.svg$")
# .html, not .svg, which is what keeps a day page out of KEY_RE and so out of
# the Past days list -- the same property trend_key() is careful about.
DAY_KEY_RE = re.compile(r"^" + DAY_PREFIX + r"(\d{4}-\d{2}-\d{2})\.html$")

NO_BOTO3 = """The daily report needs boto3, which isn't installed.

  pip install boto3
  pip install -e '.[aws]'        # from a checkout

Polling and `graph` still need nothing installed — only publishing does."""

WRONG_ACCOUNT = """These credentials are for AWS account {got}, not {want}.

AWS_ACCOUNT_ID in {env} pins the account this project may touch, and nothing
will be created or uploaded while they disagree. Either AWS_PROFILE or the
credential chain is pointing somewhere else, or the pin itself is stale."""

ACCOUNT_BLOCKED = """S3 refused the public-read policy on {bucket}.

Almost always this is account-level Block Public Access, which is separate from
the per-bucket setting and wins over it. Clear it once, by hand:

  S3 console -> Block Public Access (account settings) -> Edit -> clear all four

The bucket has been created and is remembered, so re-running --setup will pick
up where this left off."""


class S3Error(Exception):
    """An S3 call that failed in a way the caller should log and move past."""


# --------------------------------------------------------------------------
# errors and retries
# --------------------------------------------------------------------------


def _error_code(exc):
    """The S3 error code out of a botocore ClientError, or "" for anything else.

    Duck-typed rather than isinstance(ClientError) on purpose: botocore is only
    imported lazily, so naming the class here would defeat that.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    return str((response.get("Error") or {}).get("Code") or "")


def _status(exc):
    """The HTTP status behind a botocore error, or 0 when there isn't one."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return 0
    try:
        return int((response.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 0)
    except (TypeError, ValueError):
        return 0


def _is_retryable(status, code):
    """Whether S3's answer is worth another attempt after a pause."""
    if status in (429, 500, 502, 503, 504):
        return True
    return code in RETRY_CODES


def _classify(exc):
    """(retryable, retry_after) for an S3 error, or None for one that isn't ours.

    S3 sends no Retry-After, so the delay is always the backoff schedule's.
    """
    code = _error_code(exc)
    if not code:
        return None
    return _is_retryable(_status(exc), code), None


def _with_backoff(what, call):
    """Retry one idempotent S3 call. PUT is keyed by path, so replay is harmless."""
    try:
        return retry.with_backoff(what, call, _classify)
    except retry.GaveUp as exc:
        raise S3Error(str(exc)) from exc


# --------------------------------------------------------------------------
# naming — all pure, all cheap to test
# --------------------------------------------------------------------------


def bucket_slug(channel):
    """The DNS-safe part of a bucket name.

    channel_slug() is right for filenames and wrong for buckets: it keeps
    underscores, which are illegal in a bucket name because the name becomes a
    DNS label. Runs of hyphens collapse and the ends are trimmed, since a name
    may not begin or end with one either.
    """
    slug = re.sub(r"[^a-z0-9-]", "-", config.channel_slug(channel).lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    room = MAX_BUCKET_NAME - len(config.BUCKET_PREFIX) - 1 - SUFFIX_BYTES * 2
    return slug[:room].strip("-") or "channel"


def bucket_name(channel):
    """A fresh globally-unique bucket name for a channel.

    Random-suffixed because bucket names are shared across every AWS account,
    so "tm-testchannel" may well belong to a stranger. Called once, by
    --setup; after that the name is read back out of the registry.
    """
    return "{}{}-{}".format(config.BUCKET_PREFIX, bucket_slug(channel),
                            secrets.token_hex(SUFFIX_BYTES))


def object_key(platform, day):
    """Where one platform's chart for one day lives: 'twitch/2026-08-24.svg'."""
    return "{}/{}.svg".format(str(platform).strip("/. "), day.isoformat())


def day_key(day):
    """One day's page: 'day/2026-08-24.html'.

    `day` may be a date or the 'YYYY-MM-DD' string list_days() keys on, since
    both callers have one and neither should have to convert.
    """
    stamp = day if isinstance(day, str) else day.isoformat()
    return "{}{}.html".format(DAY_PREFIX, stamp)


def parse_day_key(key):
    """'YYYY-MM-DD' for a day page's key, or None for anything else."""
    match = DAY_KEY_RE.match(str(key))
    return match.group(1) if match else None


def trend_key(kind, platform):
    """Where a multi-day chart lives: 'trends/peaks-twitch.svg'.

    The filename is deliberately not a date. KEY_RE only matches
    '<word>/<YYYY-MM-DD>.svg', so a trend chart can never be mistaken for a
    platform's chart for some day — which would put a "trends" panel on the
    index and a nonsense row in Past days. Anything added under this prefix
    must keep that property.
    """
    return "{}{}-{}.svg".format(TRENDS_PREFIX, str(kind).strip("/. "),
                                str(platform).strip("/. "))


def parse_trend_key(key):
    """(kind, platform) for a trend chart key, or None for anything else."""
    match = TREND_KEY_RE.match(str(key))
    return (match.group(1), match.group(2)) if match else None


def parse_key(key):
    """(platform, 'YYYY-MM-DD') for a chart key, or None for anything else.

    The index is built only from keys matching this, so index.html, the trend
    charts and any stray upload are ignored rather than turned into a broken
    link.
    """
    match = KEY_RE.match(str(key))
    return (match.group(1), match.group(2)) if match else None


def website_url(bucket, region):
    """The bucket's static-website URL.

    HTTP only: S3 website endpoints do not serve TLS. Putting CloudFront in
    front is the way to get HTTPS, and is deliberately out of scope here.
    """
    separator = "-" if region in DASH_REGIONS else "."
    return "http://{}.s3-website{}{}.amazonaws.com".format(bucket, separator, region)


def public_read_policy(bucket):
    """The one statement a static website needs: anyone may GET an object.

    A bucket policy rather than an ACL because new buckets have Object
    Ownership set to bucket-owner-enforced, which disables ACLs outright — an
    ACL request answers AccessControlListNotSupported.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "PublicReadGetObject",
            "Effect": "Allow",
            "Principal": "*",
            "Action": ["s3:GetObject"],
            "Resource": ["arn:aws:s3:::{}/*".format(bucket)],
        }],
    }


def target_path(channel, day, platform="twitch"):
    """Where the chart will land, for --dry-run and log lines."""
    known = bucket_for(channel)
    return "{}/{}".format(known["bucket"] if known else "(no bucket yet)",
                          object_key(platform, day))


# --------------------------------------------------------------------------
# the bucket registry
# --------------------------------------------------------------------------


def _load_buckets():
    """Remembered buckets. A missing or half-written file reads as empty."""
    try:
        with open(config.S3_BUCKETS_PATH, encoding="utf-8") as handle:
            known = json.load(handle)
        return known if isinstance(known, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_buckets(known):
    """Atomic, and 0600 — it names infrastructure, even if it holds no secret."""
    config.ensure_dirs()
    temporary = config.S3_BUCKETS_PATH + ".tmp{}".format(os.getpid())
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(known, handle, indent=2, sort_keys=True)
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    os.replace(temporary, config.S3_BUCKETS_PATH)


def bucket_for(channel):
    """{"bucket", "region"} for a channel, or None when --setup hasn't run."""
    entry = _load_buckets().get(config.channel_slug(channel))
    return entry if isinstance(entry, dict) and entry.get("bucket") else None


def remember(channel, bucket, region):
    """Record a channel's bucket. The random suffix cannot be recomputed."""
    known = _load_buckets()
    known[config.channel_slug(channel)] = {"bucket": bucket, "region": region}
    _save_buckets(known)
    return known[config.channel_slug(channel)]


def require_bucket(channel):
    """The channel's bucket, or exit saying how to make one."""
    known = bucket_for(channel)
    if not known:
        raise SystemExit(
            "No S3 bucket for '{}' yet.\n"
            "Create one:  {} s3 --setup {}".format(
                channel, config.invocation(), channel))
    return known


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


def _boto3():
    """The boto3 module, or exit saying how to install it."""
    try:
        import boto3
    except ImportError as exc:
        raise SystemExit(NO_BOTO3) from exc
    return boto3


def _client(service, region=None):
    """A boto3 client: explicit keys when .env has them, boto3's own chain when not."""
    boto3 = _boto3()
    region = config.resolve_aws_region(region)
    key, secret = config.load_aws_credentials()
    if key and secret:
        session = boto3.session.Session(aws_access_key_id=key,
                                        aws_secret_access_key=secret,
                                        region_name=region)
    else:
        session = boto3.session.Session(region_name=region)
    return session.client(service)


def preflight(region=None):
    """Prove the credential works and say who it is, before anything is rendered.

    Same reason the Drive report checked its token first: charting four channels
    and then discovering the key is wrong wastes the run and reads as a chart
    bug rather than a credentials one.
    """
    try:
        who = _with_backoff("aws identity",
                            lambda: _client("sts", region).get_caller_identity())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - any failure here is fatal and final
        raise SystemExit(
            "AWS rejected these credentials: {}\n"
            "Check AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in {}, or "
            "~/.aws/credentials.".format(exc, config.ENV_PATH))
    account = str(who.get("Account") or "")
    log("start    aws account {} as {}".format(
        account or "?", (who.get("Arn") or "?").rsplit("/", 1)[-1]))

    # Checked before anything is created. A laptop with SSO profiles for a dozen
    # accounts resolves the chain to whichever AWS_PROFILE names, and several of
    # those are usually administrator; this makes the wrong one a refusal.
    want = config.expected_aws_account()
    if want and account != want:
        raise SystemExit(WRONG_ACCOUNT.format(got=account or "unknown", want=want,
                                              env=config.ENV_PATH))
    return who


# --------------------------------------------------------------------------
# setting a channel's site up
# --------------------------------------------------------------------------


def account_block(account_id, region=None):
    """Account-wide Block Public Access settings; {} when none, None if unreadable.

    Account level and bucket level are separate settings and AWS applies the
    more restrictive of the two, so the bucket policy is refused while the
    account block is on no matter what this program does to the bucket. Read it
    up front so --setup can say that rather than reporting a bare AccessDenied.
    """
    try:
        answer = _client("s3control", region).get_public_access_block(
            AccountId=str(account_id))
        return answer.get("PublicAccessBlockConfiguration") or {}
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) == "NoSuchPublicAccessBlockConfiguration":
            return {}  # nothing set at all, which is what a website wants
        return None    # no permission to look; the policy call will settle it


def warn_if_account_blocked(account_id, region=None):
    """Log the account-level block, if it is on, in terms of what to click."""
    blocked = account_block(account_id, region)
    if not blocked:
        return False
    names = sorted(name for name, value in blocked.items() if value)
    if not names:
        return False
    log("WARN     account-level Block Public Access is on: {}".format(", ".join(names)))
    log("WARN     it overrides the per-bucket setting, so the policy will be refused")
    log("WARN     clear it: S3 console -> Block Public Access (account settings)")
    return True


def create_site(channel, region=None):
    """Create and configure a channel's website bucket. (entry, created_now).

    Idempotent by way of the registry: a channel that already has a bucket is
    left alone, so re-running after a half-finished setup does not strand a
    second bucket nobody knows about.
    """
    region = config.resolve_aws_region(region)
    existing = bucket_for(channel)
    if existing:
        return existing, False

    bucket = bucket_name(channel)
    s3 = _client("s3", region)

    params = {"Bucket": bucket}
    # us-east-1 is the one region that must NOT be named: it is the API default,
    # and sending LocationConstraint for it fails as InvalidLocationConstraint.
    if region != "us-east-1":
        params["CreateBucketConfiguration"] = {"LocationConstraint": region}
    _with_backoff("create bucket", lambda: s3.create_bucket(**params))
    # Remembered before it is configured: the name is random and unguessable, so
    # a failure in the next three calls must not lose track of what was created.
    remember(channel, bucket, region)

    # Order matters. The block has to be cleared before the policy, or
    # put_bucket_policy is refused; the policy is the only way to grant read,
    # because bucket-owner-enforced ownership leaves ACLs disabled.
    _with_backoff("clear block-public-access",
                  lambda: s3.delete_public_access_block(Bucket=bucket))
    try:
        _with_backoff("put bucket policy", lambda: s3.put_bucket_policy(
            Bucket=bucket, Policy=json.dumps(public_read_policy(bucket))))
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) == "AccessDenied":
            raise SystemExit(ACCOUNT_BLOCKED.format(bucket=bucket)) from exc
        raise
    _with_backoff("put website config", lambda: s3.put_bucket_website(
        Bucket=bucket,
        WebsiteConfiguration={"IndexDocument": {"Suffix": INDEX_KEY}}))

    return {"bucket": bucket, "region": region}, True


# --------------------------------------------------------------------------
# publishing
# --------------------------------------------------------------------------


def _upload_svg(channel, path, key):
    """Put one SVG at `key`. Returns {"bucket", "key", "url"}."""
    known = require_bucket(channel)
    with open(path, "rb") as handle:
        body = handle.read()
    s3 = _client("s3", known["region"])
    # No ACL argument: ACLs are disabled on a bucket-owner-enforced bucket, and
    # the bucket policy already grants the public read.
    _with_backoff("upload {} {}".format(channel, key), lambda: s3.put_object(
        Bucket=known["bucket"], Key=key, Body=body,
        ContentType=SVG_TYPE, CacheControl=CHART_CACHE))
    return {"bucket": known["bucket"], "key": key,
            "url": "{}/{}".format(website_url(known["bucket"], known["region"]), key)}


def upload_chart(channel, path, platform, day):
    """Put one platform's chart for one day at '<platform>/<date>.svg'."""
    return _upload_svg(channel, path, object_key(platform, day))


def upload_trend(channel, path, kind, platform):
    """Put one multi-day chart at 'trends/<kind>-<platform>.svg', replacing it."""
    return _upload_svg(channel, path, trend_key(kind, platform))


def _keys(channel, prefix=None):
    """Every object key in the channel's bucket, one page at a time."""
    known = require_bucket(channel)
    s3 = _client("s3", known["region"])
    token = None
    while True:
        params = {"Bucket": known["bucket"]}
        if prefix:
            params["Prefix"] = prefix
        if token:
            params["ContinuationToken"] = token
        page = _with_backoff("list {}".format(channel),
                             lambda p=dict(params): s3.list_objects_v2(**p))
        for item in page.get("Contents") or []:
            yield item.get("Key") or ""
        token = page.get("NextContinuationToken") if page.get("IsTruncated") else None
        if not token:
            break


def list_days(channel):
    """{'YYYY-MM-DD': [platform, ...]} for every chart in the bucket, newest first."""
    found = {}
    for key in _keys(channel):
        parsed = parse_key(key)
        if parsed:
            platform, day = parsed
            if platform not in found.setdefault(day, []):
                found[day].append(platform)
    # Newest first, and dicts keep insertion order, so the page can just iterate.
    return {day: sorted(found[day]) for day in sorted(found, reverse=True)}


def list_trends(channel):
    """[(kind, platform), ...] for the multi-day charts the bucket holds.

    Listed rather than remembered, so trends.html keeps the property index.html
    has: it can be rebuilt correctly from the bucket alone, with no local CSVs
    and nothing carried between runs.
    """
    found = set()
    for key in _keys(channel, TRENDS_PREFIX):
        parsed = parse_trend_key(key)
        if parsed:
            found.add(parsed)
    return [(kind, platform) for kind in TREND_KINDS
            for platform in PLATFORMS if (kind, platform) in found]


def list_day_pages(channel):
    """{'YYYY-MM-DD', ...} for the day pages the bucket already holds.

    Only so publish_days() can tell a backfill from a rewrite. Nothing is
    rendered from this — every page's content comes from list_days(), which is
    the listing that decides what the site says.
    """
    found = set()
    for key in _keys(channel, DAY_PREFIX):
        parsed = parse_day_key(key)
        if parsed:
            found.add(parsed)
    return found


def load_titles(channel):
    """{date: {platform: title}} out of the bucket, or {} if it isn't readable.

    Kept in the bucket rather than passed in, so the page keeps its one useful
    property: index.html can be rebuilt from the bucket and nothing else. A
    `s3 --publish-index` on a machine with no CSVs still renders the titles.
    """
    known = require_bucket(channel)
    s3 = _client("s3", known["region"])
    try:
        body = s3.get_object(Bucket=known["bucket"], Key=TITLES_KEY)["Body"].read()
        stored = json.loads(body.decode("utf-8"))
        return stored if isinstance(stored, dict) else {}
    except Exception:  # noqa: BLE001 - absent, unreadable or malformed are all "none yet"
        return {}


def save_titles(channel, titles):
    """Write the title index back, merged rather than replaced."""
    known = require_bucket(channel)
    s3 = _client("s3", known["region"])
    _with_backoff("save titles for {}".format(channel), lambda: s3.put_object(
        Bucket=known["bucket"], Key=TITLES_KEY,
        Body=json.dumps(titles, indent=2, sort_keys=True).encode("utf-8"),
        ContentType="application/json; charset=utf-8", CacheControl="no-cache"))


def _publish_page(channel, key, page):
    """Upload one HTML page. Returns the site's URL."""
    known = require_bucket(channel)
    s3 = _client("s3", known["region"])
    _with_backoff("publish {} for {}".format(key, channel), lambda: s3.put_object(
        Bucket=known["bucket"], Key=key, Body=page.encode("utf-8"),
        ContentType="text/html; charset=utf-8",
        # no-cache, not no-store: the browser may keep it but must revalidate,
        # or tomorrow's first visit shows today's page.
        CacheControl="no-cache"))
    return website_url(known["bucket"], known["region"])


def publish_days(channel, days, titles=None, today=None):
    """Write the day pages that don't exist yet, plus today's. Returns the count.

    Today's is rewritten every run because today is still happening — a second
    platform may have finished polling since the last one. Every other date is
    written once and then left alone, so the steady state is one PUT a day
    however many months the bucket has accumulated, and a bucket that predates
    day pages fills itself in on the next run rather than needing a migration.
    """
    existing = list_day_pages(channel)
    stamp = today.isoformat() if today is not None else None
    written = 0
    for day, platforms in days.items():
        if day in existing and day != stamp:
            continue
        _publish_page(channel, day_key(day),
                      render_day(channel, day, platforms, (titles or {}).get(day)))
        written += 1
    return written


def publish_index(channel, today, titles=None, trends=None):
    """Rebuild index.html from what is actually in the bucket and upload it.

    `trends` says whether to link the Trends page. None means look, which is
    what `s3 --publish-index` on its own needs; the daily report has just
    published them and passes a bool rather than paying for another listing.
    """
    days = list_days(channel)

    stored = load_titles(channel)
    if titles:
        merged = dict(stored)
        merged[today.isoformat()] = titles
        if merged != stored:
            save_titles(channel, merged)
        stored = merged

    if trends is None:
        trends = bool(list_trends(channel))

    # Ahead of the index, so the links it is about to draw already resolve, and
    # swallowing its own failure: the index is the page the run exists to
    # produce, and a day page that wouldn't upload must not take it down with
    # it. The next run tries again, because the backfill is driven by what is
    # missing rather than by anything remembered.
    try:
        pages = publish_days(channel, days, stored, today)
    except Exception as exc:  # noqa: BLE001 - the index still has to go out
        log("WARN     {} — day pages not published: {}".format(channel, exc))
        pages = 0

    page = render_index(channel, today, days, stored.get(today.isoformat()),
                        trends=trends)
    return {"url": _publish_page(channel, INDEX_KEY, page), "days": len(days),
            "pages": pages}


def publish_trends(channel, today):
    """Rebuild trends.html from the trend charts in the bucket and upload it.

    Uploading the charts is the caller's job — this only builds the page around
    whatever is actually there, so a channel with Twitch charts and no YouTube
    ones gets a page with two panels rather than two broken images.
    """
    charts = list_trends(channel)
    page = render_trends(channel, today, charts)
    return {"url": "{}/{}".format(_publish_page(channel, TRENDS_KEY, page), TRENDS_KEY),
            "charts": len(charts)}


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------

# Hand-built, the way chart.py builds SVG, and for the same reason: no template
# engine to install. The palette is chart.py's, so the page and the graphs it
# shows read as one thing rather than a chart pasted onto a white document.
#
# STYLE is concatenated rather than interpolated, because every brace in CSS
# would otherwise have to be doubled to survive str.format().
STYLE = """
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 32px 24px 64px;
    background: #0f0f0f; color: #f1f1f1;
    font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          Helvetica, Arial, sans-serif;
  }
  main { max-width: 1340px; margin: 0 auto; }
  header { border-bottom: 1px solid #303030; padding-bottom: 18px; margin-bottom: 32px; }
  h1 { margin: 0; font-size: 27px; font-weight: 600; letter-spacing: -0.01em; }
  .today { margin: 6px 0 0; color: #aaaaaa; font-size: 14px; }
  .stream {
    margin: 10px 0 0; color: #f1f1f1; font-size: 15px; line-height: 1.4;
    max-width: 74ch;
  }
  .nav { margin: 14px 0 0; font-size: 14px; }

  h2 {
    margin: 0 0 14px; font-size: 13px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.09em; color: #aaaaaa;
  }
  .panel { margin-bottom: 34px; }
  /* The cross-platform chart leads the page: it answers "how many people were
     watching me at once, anywhere", which is the question the per-platform
     panels below only answer half of each. */
  .panel.lead { margin-bottom: 42px; padding-bottom: 34px;
                border-bottom: 1px solid #303030; }
  .panel.lead h2 { color: #f1f1f1; font-size: 15px; letter-spacing: 0.04em; }
  .panel img, .panel object {
    display: block; width: 100%; height: auto;
    border: 1px solid #303030; border-radius: 8px; background: #0f0f0f;
  }
  .past { border-top: 1px solid #303030; padding-top: 26px; }
  .past ul { list-style: none; margin: 0; padding: 0; }
  .past li {
    display: flex; gap: 14px; align-items: baseline;
    padding: 9px 0; border-bottom: 1px solid #1c1c1c;
  }
  .past li span { color: #f1f1f1; font-variant-numeric: tabular-nums; min-width: 6.5em; }
  a { color: #4fb3e8; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .empty { color: #717171; margin: 0; }
  footer { margin-top: 40px; color: #717171; font-size: 12px; }
"""

HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{channel} — {page}</title>
<style>"""

BODY = """</style>
</head>
<body>
<main>
  <header>
    <h1>{channel}</h1>
    <p class="today">{date}</p>{titles}{nav}
  </header>
{panels}
  <section class="past">
    <h2>Past days</h2>
{past}
  </section>
  <footer>Updated {date} · {count} day(s) recorded</footer>
</main>
</body>
</html>
"""

# The Trends page. Same shell, but no Past days section: every chart on it
# already spans days, so there is nothing older to link to.
TRENDS_BODY = """</style>
</head>
<body>
<main>
  <header>
    <h1>{channel}</h1>
    <p class="today">Trends · to {date}</p>
    <p class="nav"><a href="{index}">← Today</a></p>
  </header>
{panels}
  <footer>Updated {date}</footer>
</main>
</body>
</html>
"""

PANEL = """  <section class="panel{css}">
    <h2>{label}</h2>
    <img src="{key}" alt="{label} concurrent viewers on {date}">
  </section>"""

# <object> rather than <img>, and this is the whole reason the bars are
# clickable: an SVG embedded with <img> is a picture, inert down to its
# tooltips, where one embedded with <object> is a document. The nested <img> is
# the fallback a browser that declines the object still gets -- the same chart,
# just as inert as before. aspect-ratio is spelled out because <object> does
# not infer its height from the SVG as dependably as <img> does.
TREND_PANEL = """  <section class="panel">
    <h2>{label}</h2>
    <object type="image/svg+xml" data="{key}" aria-label="{label}"
            style="aspect-ratio: {ratio}"><img src="{key}" alt="{label}"></object>
  </section>"""

# One day's page. No Past days list -- the index has it, and this page is one
# of its entries -- but links both ways out, since a reader who arrived from a
# trend chart has never seen the index.
DAY_BODY = """</style>
</head>
<body>
<main>
  <header>
    <h1>{channel}</h1>
    <p class="today">{date}</p>{titles}
    <p class="nav"><a href="{index}">← Today</a> · <a href="{trends}">Trends →</a></p>
  </header>
{panels}
  <footer>{date}</footer>
</main>
</body>
</html>
"""


def _stream_title(titles):
    """The '<p class="stream">' for one day's titles, or '' if there are none.

    The platforms carry different titles for the same broadcast — Twitch's is
    the canonical one. YouTube is the fallback rather than a second line, so a
    channel streaming only there still gets a title.
    """
    headline = next((str((titles or {}).get(key) or "").strip()
                     for key in TITLE_PREFERENCE
                     if str((titles or {}).get(key) or "").strip()), "")
    return ('\n    <p class="stream">{}</p>'.format(html.escape(headline))
            if headline else "")


def render_day(channel, day, platforms, titles=None):
    """One day's page: every chart recorded for that date, and a way back.

    It lives a directory down, at day/2026-08-24.html, so every reference out
    of it is '../'-prefixed. That is the only thing separating it from the
    index, which is why it reuses the index's PANEL wholesale.

    `platforms` is list_days()' value for the date — what the bucket actually
    holds — so the page never shows a panel for a chart that isn't there.
    """
    when = date.fromisoformat(day) if isinstance(day, str) else day
    pretty = when.strftime("%a %d %b %Y")

    panels = [PANEL.format(
        label=html.escape(PLATFORM_LABELS.get(platform, platform)),
        key=html.escape("../" + object_key(platform, when)),
        date=html.escape(pretty),
        css=" lead" if platform == "combined" else "")
        for platform in PLATFORMS if platform in (platforms or ())]
    if not panels:
        panels.append('  <p class="empty">No graph for {}.</p>'.format(
            html.escape(pretty)))

    return (HEAD.format(channel=html.escape(str(channel)), page=html.escape(pretty))
            + STYLE
            + DAY_BODY.format(channel=html.escape(str(channel)),
                              date=html.escape(pretty),
                              titles=_stream_title(titles),
                              index=html.escape("../" + INDEX_KEY),
                              trends=html.escape("../" + TRENDS_KEY),
                              panels="\n".join(panels)))


def render_index(channel, today, days, titles=None, trends=False):
    """The front page: one day's charts, no JS, no external assets.

    `days` is list_days()' mapping, newest first. Today's charts are shown;
    every earlier day is a link and nothing more, so the page stays quick to
    load however many months accumulate.

    The Trends link is only drawn when `trends` says that page exists, so a
    channel whose multi-day charts have never rendered gets no dead link.

    Everything interpolated goes through html.escape(): the channel name
    arrives from a command line argument, and the keys are built from it.
    """
    stamp = today.isoformat()
    pretty = today.strftime("%a %d %b %Y")
    todays = days.get(stamp) or []

    stream_titles = _stream_title(titles)

    panels = []
    for platform in PLATFORMS:
        if platform not in todays:
            continue
        panels.append(PANEL.format(
            label=html.escape(PLATFORM_LABELS.get(platform, platform)),
            key=html.escape(object_key(platform, today)),
            date=html.escape(pretty),
            css=" lead" if platform == "combined" else ""))
    if not panels:
        panels.append('  <p class="empty">No graph for {} — either nothing was '
                      'streamed, or the report has not run yet.</p>'.format(
                          html.escape(pretty)))

    # One link per day rather than one per platform. The link text still names
    # the platforms the date has charts for, so nothing is lost by collapsing
    # them, and the reader lands on a page with a heading and a way back
    # instead of on a bare SVG.
    rows = []
    for day, platforms in days.items():
        if day == stamp:
            continue
        # "combined" is left out of the label: it is a chart derived from the
        # others rather than somewhere the channel streamed, and naming it made
        # every row read "Both platforms · Twitch · YouTube". The day page
        # still leads with it.
        named = [p for p in platforms if p != "combined"] or list(platforms)
        label = " · ".join(PLATFORM_LABELS.get(platform, platform)
                           for platform in named)
        rows.append('      <li><span>{}</span><a href="{}">{}</a></li>'.format(
            html.escape(day), html.escape(day_key(day)), html.escape(label)))
    past = ("    <ul>\n" + "\n".join(rows) + "\n    </ul>" if rows else
            '    <p class="empty">Nothing earlier yet — this is the first day.</p>')

    nav = ('\n    <p class="nav"><a href="{}">Trends →</a></p>'.format(
        html.escape(TRENDS_KEY)) if trends else "")

    return (HEAD.format(channel=html.escape(str(channel)), page="stream metrics")
            + STYLE
            + BODY.format(channel=html.escape(str(channel)),
                          date=html.escape(pretty),
                          titles=stream_titles,
                          nav=nav,
                          panels="\n".join(panels),
                          past=past,
                          count=len(days)))


def render_trends(channel, today, charts):
    """The Trends page: the multi-day charts, one panel each.

    `charts` is list_trends()' [(kind, platform)], so the page is built from
    what the bucket actually holds and never links an image that isn't there.
    """
    pretty = today.strftime("%a %d %b %Y")
    panels = [TREND_PANEL.format(
        label=html.escape(TREND_LABELS.get(
            (kind, platform), "{} · {}".format(kind, platform))),
        key=html.escape(trend_key(kind, platform)),
        ratio="{} / {}".format(*TREND_SIZES.get(kind, TREND_SIZES["peaks"])))
        for kind, platform in charts]
    if not panels:
        panels.append('  <p class="empty">No trend charts yet — they appear once '
                      'the daily report has run.</p>')

    return (HEAD.format(channel=html.escape(str(channel)), page="trends")
            + STYLE
            + TRENDS_BODY.format(channel=html.escape(str(channel)),
                                 date=html.escape(pretty),
                                 index=html.escape(INDEX_KEY),
                                 panels="\n".join(panels)))
