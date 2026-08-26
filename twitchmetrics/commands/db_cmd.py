"""Create the sample database and report what it can see.

The order this is meant to be run in, once per machine:

    twitch-metrics db --status      what is configured, and what answers
    twitch-metrics db --init        create the tables; safe to run again
    twitch-metrics db --import      load the CSVs already in data/
    twitch-metrics db --verify      prove the database agrees with them

--verify is the point of the exercise. storage.read_samples() is the function
every chart in this project reads through, and the one tests/smoke.py pins down
line by line; if the database gives back what it gives back, for every row of
every archive, then nothing downstream can have changed.

--status is the one to reach for first, because it is the only one that works
when nothing else does: it never raises, and it distinguishes the three failures
that look alike from the outside -- nothing configured, psycopg missing, and a
database that will not answer.
"""

import glob
import os
import sys

from .. import config, db, store


def add_arguments(parser):
    parser.add_argument("--status", action="store_true",
                        help="what is configured, what answers, and which "
                             "schema files have been applied")
    parser.add_argument("--init", action="store_true",
                        help="apply any schema files the database has not seen; "
                             "safe to re-run")
    parser.add_argument("--import", dest="do_import", action="store_true",
                        help="load data/*.csv into the database; safe to re-run")
    parser.add_argument("--verify", action="store_true",
                        help="compare the database against storage.read_samples()")
    parser.add_argument("--all", action="store_true",
                        help="with --import, every data/*.csv rather than only "
                             "the channels a poller is enabled for")
    parser.add_argument("--limit", type=int, default=10, metavar="N",
                        help="differences to print per file in --verify (default 10)")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="read and count, but write nothing")


# --------------------------------------------------------------------------
# --status
# --------------------------------------------------------------------------


def status():
    """Everything worth knowing, and never an exception. Returns an exit code.

    Deliberately keeps going after a failure rather than stopping at the first
    one: the reason somebody runs this is that something is wrong, and a page
    that stops at "cannot connect" hides the schema state they were about to
    ask about next.
    """
    zone = config.resolve_db_timezone()
    print("connection   {}".format(db.where()))
    print("timezone     {}".format(zone))

    if not db.configured():
        print("\nNothing is configured, so the pollers will write to data/*.csv "
              "as they\nalways have. Charts and the website need a database:\n")
        print(db.NOT_CONFIGURED.format(env=config.ENV_PATH, prog=config.invocation()))
        return 1

    ok, why = db.probe()
    print("reachable    {}".format("yes" if ok else "no -- {}".format(why)))
    if not ok:
        return 1

    try:
        server, server_zone = db.execute(
            "SELECT current_setting('server_version'), current_setting('TimeZone')",
            fetch=True)[0]
        print("server       PostgreSQL {} (its own TimeZone is {})".format(
            server, server_zone))
    except db.Unreachable as exc:
        print("server       could not be read: {}".format(exc))

    # Checked here as well as in --init, because a zone that stopped resolving
    # -- a tzdata package removed, a name retired upstream -- would otherwise
    # only show up as reports quietly landing on the wrong day.
    try:
        db.execute("SELECT now() AT TIME ZONE %s", (zone,))
        print("zone check   {} is known to the server".format(zone))
    except db.Unreachable:
        print("zone check   FAILED -- the server does not recognise {!r}".format(zone))
        print("             Set TWITCH_TIME_ZONE in {} to an IANA name.".format(
            config.ENV_PATH))
        return 1

    print("\nschema")
    pending = 0
    for version, name, applied_at in db.schema_state():
        if applied_at:
            print("  {:>3}  {:<24} applied {}".format(
                version, name, applied_at.strftime("%Y-%m-%d %H:%M")))
        else:
            pending += 1
            print("  {:>3}  {:<24} NOT APPLIED".format(version, name))
    if pending:
        print("\n{} file(s) pending. Apply them with:  {} db --init".format(
            pending, config.invocation()))
        return 1
    return 0


# --------------------------------------------------------------------------
# --init
# --------------------------------------------------------------------------


def init(dry_run=False):
    """Apply the schema files. Idempotent, so re-running it is a no-op."""
    if dry_run:
        # Prints the SQL rather than pretending to run it. This is the one
        # command where "what would you do" has an exact and readable answer,
        # and it also means the schema can be read without going to find the
        # files it lives in.
        db.apply_schema(dry_run=True)
        return 0

    applied = db.apply_schema()
    if not applied:
        print("schema already up to date")
    else:
        print("{} file(s) applied".format(applied))

    # Validated here rather than trusted, because a typo does not fail -- it
    # silently files every stream that crosses midnight under the wrong date,
    # in a table nobody reads directly.
    zone = config.resolve_db_timezone()
    try:
        db.execute("SELECT now() AT TIME ZONE %s", (zone,))
    except db.Unreachable as exc:
        raise SystemExit(
            "PostgreSQL does not recognise the timezone {!r}.\n"
            "It needs an IANA name such as Europe/London, not an abbreviation "
            "like BST.\nSet TWITCH_TIME_ZONE in {}\n  ({})".format(
                zone, config.ENV_PATH, exc))
    print("reporting timezone {}".format(zone))
    return 0


# --------------------------------------------------------------------------
# --import and --verify
# --------------------------------------------------------------------------


def archives(everything=False):
    """The data/*.csv files to work on, oldest format last.

    data/ accumulates. It holds a file for every channel anyone ever looked at
    -- metrics_ign.csv here is 138 bytes of a channel nothing polls -- so
    globbing it blindly would import other people's channels into this one's
    database. The default is therefore the channels the daily report knows
    about, and sweeping the directory has to be asked for.

    metrics_ before viewers_ so the superset lands first. The merging upsert in
    record_twitch_sample() means the order cannot change the result, but reading
    the better file first keeps the log honest about what came from where.
    """
    from . import daily  # noqa: PLC0415 - only needed here, and it imports chart

    found = sorted(glob.glob(os.path.join(config.DATA_DIR, "*.csv")))
    ordered = []
    for prefix, _ in store.PREFIXES:
        ordered += [p for p in found
                    if os.path.basename(p).startswith(prefix)]
    if everything:
        return ordered

    # The same list `daily` reports on, and from the same places: the command
    # line is not one of them here, so this is TWITCH_DAILY_CHANNELS, then the
    # enabled systemd units, then whatever systemctl says is running.
    names, _source = daily.discover_channels()
    wanted = {config.channel_slug(name) for name in names}
    if not wanted:
        # Nothing is enabled -- a laptop, or a server before the units are
        # installed. Falling back to everything is right here: there is no
        # smaller honest answer, and --dry-run will show what it would take.
        return ordered
    keep = []
    for path in ordered:
        try:
            _, slug, _ = store.source_of(path)
        except store.Skipped:
            continue
        if slug in wanted:
            keep.append(path)
    return keep


def import_archives(paths, dry_run=False, limit=10):
    """Load the archives, one merged history per account, then say what landed."""
    if not paths:
        print("No data/*.csv to import. Use --all to include channels that no "
              "poller is enabled for.")
        return 0

    groups = store.group_archives(paths)
    if not groups:
        print("None of those files is a metrics_, viewers_ or youtube_ archive.")
        return 0

    total = 0
    for (platform, slug), files in sorted(groups.items(), key=lambda kv: kv[0][::-1]):
        tally = store.import_archive(platform, slug, files, dry_run=dry_run)
        total += tally["stored"]
        names = ", ".join(sorted(os.path.basename(p) for p in files))
        print("{:<8} {:<16} {:>5} row(s)  {}{}".format(
            platform, slug, tally["rows"], names,
            "" if dry_run else
            "  -> {} stored, {} skipped".format(tally["stored"], tally["skipped"])))

    if dry_run:
        print("\nNothing was written. Drop --dry-run to import.")
        return 0

    print("\n{} sample(s) stored".format(total))
    # An archive nearly always ends mid-broadcast -- the last row of the last
    # file is often a live one -- which would otherwise leave a stream from
    # weeks ago sitting in the live-lookup cache, and the poller would spend a
    # quota unit per tick asking about a video that stopped long ago.
    closed = db.execute("SELECT tm.close_stale_streams()", fetch=True)[0][0]
    if closed:
        print("{} stream(s) left open by the archive were closed as stale".format(closed))

    # Built here rather than left for the first `daily` run. An import that
    # leaves the report tables empty looks like it worked and then produces a
    # Trends page with nothing on it, which is a confusing way to find out that
    # a second command was needed.
    rebuilt = _rebuild_reports()
    if rebuilt:
        print("report tables rebuilt for {} channel(s)".format(rebuilt))
    return 0


def _rebuild_reports():
    """Recompute every report table over whatever range the samples cover."""
    spans = db.execute("""
        SELECT s.channel_id,
               min(s.sampled_at AT TIME ZONE c.report_timezone)::date,
               max(s.sampled_at AT TIME ZONE c.report_timezone)::date
          FROM tm.sample_all s JOIN tm.channel c ON c.channel_id = s.channel_id
         GROUP BY s.channel_id""", fetch=True)
    for channel_id, first, last in spans:
        db.execute("SELECT tm.refresh_range(%s, %s, %s, NULL, 30)",
                   (channel_id, first, last))
    return len(spans)


def verify_archives(paths, limit=10):
    """Compare every archive against the database. Non-zero if any disagrees."""
    if not paths:
        print("Nothing to verify.")
        return 0

    worst = 0
    for path in paths:
        try:
            report = store.verify_csv(path, limit=limit)
        except store.Skipped as exc:
            print("skip     {}  ({})".format(os.path.basename(path), exc))
            continue
        name = os.path.basename(path)
        if not report["total"]:
            # db_rows may legitimately exceed csv_rows: metrics_ and viewers_
            # for one channel are one table, and a poller has been running since.
            print("ok       {:<32} {} row(s) agree".format(name, report["csv_rows"]))
            continue
        worst = 1
        print("DIFFERS  {:<32} {} of {} row(s)".format(
            name, report["total"], report["csv_rows"]))
        for when, field, want, got in report["differences"]:
            print("           {}  {}: csv {!r} != db {!r}".format(
                when.strftime("%Y-%m-%dT%H:%M:%SZ"), field, want, got))
        if report["total"] > len(report["differences"]):
            print("           ... and {} more".format(
                report["total"] - len(report["differences"])))
    return worst


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def run(args):
    if args.status:
        return status()
    try:
        if args.init:
            code = init(dry_run=args.dry_run)
            if code or not (args.do_import or args.verify):
                return code
        if args.do_import:
            code = import_archives(archives(args.all), dry_run=args.dry_run,
                                   limit=args.limit)
            if code or not args.verify:
                return code
        if args.verify:
            return verify_archives(archives(args.all), limit=args.limit)
    except db.NotConfigured as exc:
        sys.exit(str(exc))
    except db.Unreachable as exc:
        sys.exit(db.UNREACHABLE.format(where=db.where(), why=exc,
                                       prog=config.invocation()))
    # No action asked for. --status is the safe default: it is read-only and it
    # is what someone typing `db` on its own almost certainly wanted.
    return status()
