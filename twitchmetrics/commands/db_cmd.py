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
import time

from .. import config, db, store


def add_arguments(parser):
    parser.add_argument("channel", nargs="?", default=None,
                        help="channel the location rules belong to "
                             "(default: the configured channel)")
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
    parser.add_argument("--rebuild", action="store_true",
                        help="recompute every report table over the whole span "
                             "of samples; needed after a schema change adds a "
                             "column the nightly refresh would never backfill")
    parser.add_argument("--locations", action="store_true",
                        help="list this channel's location rules, with how many "
                             "of its broadcasts each one currently claims")
    parser.add_argument("--location-rule", dest="location_rule", action="append",
                        metavar="PATTERN[=NAME]",
                        help="add a rule matching PATTERN in a stream's title; "
                             "repeatable, and NAME defaults to PATTERN")
    parser.add_argument("--drop-location-rule", dest="drop_location_rule",
                        type=int, action="append", metavar="SEQ",
                        help="remove the rule with this seq, as --locations prints it")


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


def rebuild(dry_run=False):
    """`db --rebuild`: recompute every report table from the samples. 0 on success.

    Exists because store.ensure_reports() cannot notice this class of staleness.
    It judges a window by how many DATES tm.report_daily_peak holds, so a schema
    change that adds a COLUMN leaves every one of those dates present and every
    new column NULL -- and the nightly refresh would touch only the tail, for
    ever. 007 and 008 both shipped a one-shot DO block for exactly that, and this
    is the same pass made re-runnable and given a name.

    Cheap enough to run by hand and idempotent, so the answer to "are the report
    tables right?" is to run it rather than to reason about it.
    """
    if dry_run:
        spans = db.execute("""
            SELECT c.slug,
                   min(s.sampled_at AT TIME ZONE c.report_timezone)::date,
                   max(s.sampled_at AT TIME ZONE c.report_timezone)::date
              FROM tm.sample_all s JOIN tm.channel c ON c.channel_id = s.channel_id
             GROUP BY c.slug ORDER BY c.slug""", fetch=True)
        for slug, first, last in spans:
            print("would rebuild  {:<16} {} .. {}".format(slug, first, last))
        print("{} channel(s); nothing written".format(len(spans)))
        return 0
    started = time.monotonic()
    count = _rebuild_reports()
    print("rebuilt report tables for {} channel(s) in {:.1f}s".format(
        count, time.monotonic() - started))
    return 0


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
# --locations
# --------------------------------------------------------------------------
#
# The only configuration in the whole database. Everything else here is derived
# from a sample and can be rebuilt by deleting it; a location rule is a fact
# about the world that nothing in the API reports, so it is typed in once and
# then kept.


SEQ_STEP = 10       # room to slot a more specific rule between two others


def _channel_id(channel):
    """The channel's row id, or None when the database has never seen it."""
    rows = db.execute("SELECT channel_id FROM tm.channel WHERE slug = %s",
                      (config.channel_slug(channel),), fetch=True)
    return rows[0][0] if rows else None


def _refresh_locations(channel_id):
    """Re-file every broadcast, because a rule change rewrites history.

    The stream trends and the location rollup that reads them, and nothing else:
    a rule touches no sample and no per-day aggregate, so refreshing the whole
    range would redo a great deal of arithmetic to arrive at the same numbers.

    The rollup is not optional. It is keyed BY location, so a rule change moves
    rows between its keys rather than merely changing a column -- and left alone
    it would keep charting the old filing until the next nightly run, which is a
    stale chart nobody would think to blame on a rule they changed by hand.
    """
    span = db.execute("""
        SELECT min(s.sampled_at AT TIME ZONE c.report_timezone)::date,
               max(s.sampled_at AT TIME ZONE c.report_timezone)::date
          FROM tm.sample_all s JOIN tm.channel c ON c.channel_id = s.channel_id
         WHERE s.channel_id = %s""", (channel_id,), fetch=True)
    if not span or span[0][0] is None:
        return 0
    refiled = db.execute("SELECT tm.refresh_stream_trends(%s, %s, %s, NULL)",
                         (channel_id, span[0][0], span[0][1]), fetch=True)[0][0]
    # After the trends, because it reads what they have just written.
    db.execute("""
        SELECT tm.refresh_location_watch(a.channel_id, a.platform, NULL)
          FROM tm.platform_account a WHERE a.channel_id = %s""", (channel_id,))
    return refiled


def add_location_rules(channel, rules, dry_run=False):
    """Append rules, in the order given. Returns an exit code."""
    channel_id = _channel_id(channel)
    if channel_id is None:
        print("No channel {!r} in the database yet — poll it, or run "
              "`{} db --import`, first.".format(channel, config.invocation()))
        return 1

    highest = db.execute("SELECT coalesce(max(seq), 0) FROM tm.stream_location_rule "
                         "WHERE channel_id = %s", (channel_id,), fetch=True)[0][0]
    for offset, raw in enumerate(rules, start=1):
        pattern, _, name = str(raw).partition("=")
        pattern, name = pattern.strip(), name.strip()
        if not pattern:
            print("skipped an empty rule")
            continue
        seq = highest + offset * SEQ_STEP
        if dry_run:
            print("would add  {:>4}  {!r} -> {}".format(seq, pattern, name or pattern))
            continue
        db.execute("INSERT INTO tm.stream_location_rule "
                   "(channel_id, seq, pattern, location) VALUES (%s, %s, %s, %s)",
                   (channel_id, seq, pattern, name or pattern))
        print("added      {:>4}  {!r} -> {}".format(seq, pattern, name or pattern))
    if not dry_run:
        print("re-filed {} broadcast(s)".format(_refresh_locations(channel_id)))
    return 0


def drop_location_rules(channel, seqs, dry_run=False):
    """Remove rules by seq. Returns an exit code."""
    channel_id = _channel_id(channel)
    if channel_id is None:
        print("No channel {!r} in the database.".format(channel))
        return 1
    for seq in seqs:
        if dry_run:
            print("would drop {:>4}".format(seq))
            continue
        gone = db.execute("DELETE FROM tm.stream_location_rule "
                          "WHERE channel_id = %s AND seq = %s RETURNING pattern",
                          (channel_id, seq), fetch=True)
        print("dropped    {:>4}  {!r}".format(seq, gone[0][0]) if gone
              else "no rule with seq {}".format(seq))
    if not dry_run:
        print("re-filed {} broadcast(s)".format(_refresh_locations(channel_id)))
    return 0


def list_locations(channel):
    """The rules, and what each one currently claims. Returns an exit code.

    The counts are the point rather than a flourish. A rule that claims nothing
    is a title that has been reworded, and the only symptom otherwise is a bar
    quietly moving to "Unknown" on a chart nobody is reading that closely.
    """
    channel_id = _channel_id(channel)
    if channel_id is None:
        print("No channel {!r} in the database.".format(channel))
        return 1

    rules = db.execute("SELECT seq, pattern, location FROM tm.stream_location_rule "
                       "WHERE channel_id = %s ORDER BY seq", (channel_id,), fetch=True)
    if not rules:
        print("No location rules for {}. Add one with:\n"
              "    {} db {} --location-rule 'Magic Kingdom'".format(
                  channel, config.invocation(), channel))
        return 0

    # Counted from the report table rather than by re-running the matcher, so
    # this reports what the CHARTS say and not what they ought to say.
    claimed = dict(db.execute(
        "SELECT coalesce(location, ''), count(*) FROM tm.report_stream_trend "
        "WHERE channel_id = %s GROUP BY 1", (channel_id,), fetch=True))
    print("   seq  pattern                          location              streams")
    for seq, pattern, location in rules:
        print("  {:>4}  {:<32} {:<21} {:>7}".format(
            seq, pattern[:32], location[:21], claimed.get(location, 0)))
    unknown = claimed.get("", 0)
    total = sum(claimed.values())
    print("  {} of {} broadcast(s) matched; {} unknown".format(
        total - unknown, total, unknown))
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def run(args):
    if args.status:
        return status()
    channel = config.resolve_channel(args.channel)
    try:
        # Before --init, deliberately: a rule change is cheap and the schema is
        # almost certainly already applied, so making the reader type both
        # would be ceremony. They compose in the order they are written.
        if args.location_rule:
            code = add_location_rules(channel, args.location_rule,
                                      dry_run=args.dry_run)
            if code or not (args.drop_location_rule or args.locations):
                return code
        if args.drop_location_rule:
            code = drop_location_rules(channel, args.drop_location_rule,
                                       dry_run=args.dry_run)
            if code or not args.locations:
                return code
        if args.locations:
            return list_locations(channel)
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
            code = verify_archives(archives(args.all), limit=args.limit)
            if code or not args.rebuild:
                return code
        if args.rebuild:
            return rebuild(dry_run=args.dry_run)
    except db.NotConfigured as exc:
        sys.exit(str(exc))
    except db.Unreachable as exc:
        sys.exit(db.UNREACHABLE.format(where=db.where(), why=exc,
                                       prog=config.invocation()))
    # No action asked for. --status is the safe default: it is read-only and it
    # is what someone typing `db` on its own almost certainly wanted.
    return status()
