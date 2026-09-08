"""Create and inspect a channel's S3 static website.

One bucket per channel, serving one page. `--setup` does the four calls a public
website needs — create the bucket, clear its Block Public Access, grant
public read, turn on website hosting — and remembers the name, which is random
because bucket names are global.

There is no login flow here. An access key is a pair of strings in .env, or
boto3 finds one in ~/.aws/credentials or an instance role, so the browser dance
the Drive command needed has no equivalent.

`--prune-trends` is the one mode that removes anything: the objects a retired
chart kind left behind, which the Trends page already stops linking but nothing
else deletes. It lists them unless told `--yes`.
"""

import sys

from .. import chart, config, s3
from ..logging import log


def add_arguments(parser):
    parser.add_argument("channel", nargs="?", default=None,
                        help="channel to act on (default: the configured channel)")
    parser.add_argument("--region", default=None, metavar="NAME",
                        help="AWS region (default: AWS_REGION, else {})".format(
                            config.DEFAULT_AWS_REGION))
    parser.add_argument("--setup", action="store_true",
                        help="create and configure this channel's website bucket")
    parser.add_argument("--check", action="store_true",
                        help="prove the credentials work and name the bucket")
    parser.add_argument("--url", action="store_true",
                        help="print just the website URL, for scripts")
    parser.add_argument("--publish-index", dest="publish_index", action="store_true",
                        help="rebuild index.html from the bucket, uploading nothing else")
    parser.add_argument("--list", dest="list_all", action="store_true",
                        help="list every channel that has a bucket, then exit")
    parser.add_argument("--prune-trends", dest="prune_trends", action="store_true",
                        help="list the trend charts left behind by a retired "
                             "chart kind; add --yes to delete them")
    parser.add_argument("--yes", action="store_true",
                        help="with --prune-trends, actually delete rather than list")
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would happen, touch no network")


def _channel(args):
    return config.resolve_channel(args.channel)


def _list_all():
    """The registry, which needs no credentials to read."""
    known = s3._load_buckets()
    if not known:
        print("No buckets yet.  {} s3 --setup <channel>".format(config.invocation()))
        return 1
    print("{} channel(s) with a bucket:\n".format(len(known)))
    for slug in sorted(known):
        entry = known[slug]
        print("  {:<18} {:<40} {}".format(
            slug, entry.get("bucket", "?"),
            s3.website_url(entry.get("bucket", ""), entry.get("region", ""))))
    print()
    return 0


def _setup(args, channel):
    if args.dry_run:
        print("Would create a bucket named {}<slug>-<random> in {}, clear its\n"
              "Block Public Access, grant public read and enable website hosting."
              .format(config.BUCKET_PREFIX, config.resolve_aws_region(args.region)))
        return 0

    who = s3.preflight(args.region)
    # Checked before creating anything: the account-level block is what actually
    # stops this working, and it is a console click rather than a code problem.
    s3.warn_if_account_blocked(who.get("Account"), args.region)

    entry, created = s3.create_site(channel, args.region)
    url = s3.website_url(entry["bucket"], entry["region"])
    if created:
        log("s3       created {} in {}".format(entry["bucket"], entry["region"]))
    else:
        log("s3       {} already set up in {}".format(entry["bucket"], entry["region"]))
    print("\n{}\n  {}\n".format(channel, url))
    print("Empty until the first report:\n  {} daily {}".format(
        config.invocation(), channel))
    return 0


def _check(args, channel):
    s3.preflight(args.region)
    known = s3.bucket_for(channel)
    if not known:
        print("\n{} has no bucket yet.\n  {} s3 --setup {}".format(
            channel, config.invocation(), channel))
        return 1
    print("\n  channel   {}\n  bucket    {}\n  region    {}\n  url       {}".format(
        channel, known["bucket"], known["region"],
        s3.website_url(known["bucket"], known["region"])))
    print("  today     {}\n".format(
        s3.target_path(channel, chart.parse_day("today"))))
    return 0


def _prune_trends(args, channel):
    """Delete the trends/ charts whose kind nothing renders any more.

    A one-off after a chart is retired, not part of any run. The Trends page is
    built from a listing filtered by TREND_KINDS, so a retired kind stops being
    shown the moment the code lands -- the objects simply stay in the bucket,
    reachable by anyone who kept the URL. This is what removes them.

    Two steps on purpose. Without --yes it lists and exits 0, which is the
    preview; the deletion needs the flag spelled out, because this is the one
    command here that destroys anything. A run with nothing to do says so and
    deletes nothing, so it is safe to repeat.

    The registry lives on the machine that created the buckets, so this has to
    run there -- from somewhere else, require_bucket() will simply not know the
    channel.
    """
    if args.dry_run:
        print("Would list {} in {} and delete the charts whose kind is retired."
              .format(s3.TRENDS_PREFIX, s3.require_bucket(channel)["bucket"]))
        return 0

    stale = s3.stale_trends(channel)
    if not stale:
        print("{}: no stale trend charts.".format(channel))
        return 0

    print("\n{}: {} stale trend chart(s) in {}:\n".format(
        channel, len(stale), s3.require_bucket(channel)["bucket"]))
    for key in stale:
        print("  {}".format(key))
    if not args.yes:
        print("\nNothing deleted. To delete:\n  {} s3 {} --prune-trends --yes\n"
              .format(config.invocation(), channel))
        return 0

    # Caught here and nowhere else in this module: every other mode either
    # publishes something a later run will publish again, or is a read. This one
    # is a person deleting things by hand, and "AccessDenied" is worth a sentence
    # rather than a stack trace ending in botocore.
    try:
        results = s3.delete_trend_keys(channel, stale)
    except s3.S3Error as exc:
        sys.exit("Nothing deleted — {}".format(exc))
    failed = [(key, why) for key, why in results if why]
    for key, why in failed:
        log("WARN     {} — {} not deleted: {}".format(channel, key, why))
    log("s3       {} stale trend chart(s) deleted for {}".format(
        len(results) - len(failed), channel))
    return 1 if failed else 0


def run(args):
    config.ensure_dirs()

    # Ahead of anything that reads a credential or opens a socket, so a machine
    # with no keys can still be asked what it knows.
    if args.list_all:
        return _list_all()

    channel = _channel(args)

    if args.url:
        known = s3.bucket_for(channel)
        if not known:
            sys.exit("No bucket for '{}'. Run:  {} s3 --setup {}".format(
                channel, config.invocation(), channel))
        print(s3.website_url(known["bucket"], known["region"]))
        return 0

    if args.setup:
        return _setup(args, channel)

    if args.check:
        return _check(args, channel)

    if args.prune_trends:
        return _prune_trends(args, channel)

    if args.publish_index:
        if args.dry_run:
            print("Would rebuild {} in {}".format(
                s3.INDEX_KEY, s3.require_bucket(channel)["bucket"]))
            return 0
        info = s3.publish_index(channel, chart.parse_day("today"))
        log("s3       {} rebuilt from {} day(s), {} day page(s) written".format(
            s3.INDEX_KEY, info["days"], info.get("pages", 0)))
        print(info["url"])
        return 0

    # No mode given: say what is known, without needing credentials.
    known = s3.bucket_for(channel)
    if not known:
        print("{} has no bucket yet.\n\nCreate one:\n  {} s3 --setup {}".format(
            channel, config.invocation(), channel))
        return 1
    print("{}\n  {}".format(channel, s3.website_url(known["bucket"], known["region"])))
    return 0
