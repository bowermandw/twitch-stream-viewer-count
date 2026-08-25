"""The Postgres connection, and the SQL files that build the schema.

The samples live in Postgres now. The CSVs under data/ stay as a spool for the
minutes it is unreachable, so a poller never dies over a database and never
loses an evening to one.

This module owns three things and nothing above them: the connection string,
the connection, and the difference between "no database is configured" and "the
database is down". That last one matters because the two have opposite answers
depending on who is asking -- a poller shrugs and spools, a chart must stop.

psycopg is imported lazily, inside _psycopg(). cli.py imports every command
module at startup, so a module-scope import here would take `--help` down on a
machine that only collects, and collecting is meant to need nothing installed.
That is the same arrangement s3.py has for boto3, for the same reason.
"""

import os
import re
import time

from . import config
from .config import invocation

# Well under config.MIN_INTERVAL_SECONDS, and that is a requirement rather than
# a preference. runloop.loop() ticks on wall-clock boundaries, so a connect
# attempt able to outlast a tick would push every following sample off its
# boundary and eventually skip one. The default is no timeout at all: a
# black-holed packet filter then takes the kernel around 130 seconds to give up,
# which is a dozen missed samples for a database nobody was waiting on.
CONNECT_TIMEOUT = 3

# How long to stop trying after a failed connect. Without it, a database that is
# down for an hour costs CONNECT_TIMEOUT out of every single tick. Deliberately
# shorter than the shortest poll interval, so one that comes back is noticed on
# the next sample rather than the one after it.
RETRY_COOLDOWN = 30

# Shows up in pg_stat_activity, so `select application_name, state, query from
# pg_stat_activity` on the server says which poller is holding what. Costs
# nothing, and is the first thing anyone wants at two in the morning.
APP_NAME = "twitch-metrics"

SQL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sql")

NO_PSYCOPG = """Postgres storage needs psycopg, which isn't installed.

    pip install 'psycopg[binary]'
    pip install -e '.[pg]'        # from a checkout

Collecting still works without it: samples go to the CSV spool exactly as they
did before there was a database. Only charting and publishing require it."""

NOT_CONFIGURED = """No database is configured.

Charts and the website read from Postgres, so one is needed. Put the connection
string in {env}:

    TWITCH_DATABASE_URL=postgresql://twitch:PASSWORD@127.0.0.1:5432/twitchmetrics

then create the tables:

    {prog} db --init

DATABASE_URL is honoured too, if this machine already sets one."""

UNREACHABLE = """Cannot reach the database at {where}
    {why}

Charts and the website read from Postgres and deliberately do not fall back to
the CSVs -- a chart quietly built from a stale spool is worse than no chart.

Nothing is being lost meanwhile: the pollers spool to data/ and replay by
themselves once the database answers again.

    {prog} db --status"""


class NotConfigured(Exception):
    """No connection string. Not a failure -- a poller carries on, a chart cannot."""


class Unreachable(Exception):
    """Configured, but the connect or the statement failed."""


def _psycopg():
    """The psycopg module, or exit saying how to install it."""
    try:
        import psycopg  # noqa: PLC0415 - lazy on purpose; see the module docstring
    except ImportError as exc:
        raise SystemExit(NO_PSYCOPG) from exc
    return psycopg


# --------------------------------------------------------------------------
# the connection string
# --------------------------------------------------------------------------


def dsn():
    """The connection string, or "" when none is configured.

    TWITCH_DATABASE_URL first, then a bare DATABASE_URL -- the name every host,
    compose file and migration tool already sets. Honoured for the same reason
    resolve_aws_region() honours AWS_DEFAULT_REGION: a machine that already has
    the value should not need a second copy of it under our own prefix.
    """
    return config.resolve_database_url()


def configured():
    return bool(dsn())


# The password sits inside a URL-form DSN, and every error path below names the
# connection it failed on.
_PASSWORD_RE = re.compile(r"(?<=://)([^/@\s]*):([^/@\s]*)(?=@)")


def redact(text):
    """A DSN safe to log or print.

    The same trap youtube._guarded() avoids by reporting HTTPError.code and
    never the exception's url, which carries the API key.
    """
    return _PASSWORD_RE.sub(lambda m: m.group(1) + ":***", str(text))


def where():
    return redact(dsn()) or "(nothing configured)"


def _first_line(exc):
    """psycopg errors carry a multi-line HINT/DETAIL block; a log line wants one."""
    text = str(exc).strip()
    return text.splitlines()[0] if text else repr(exc)


# --------------------------------------------------------------------------
# the connection
# --------------------------------------------------------------------------

# One connection per process, opened on first use. No lock, unlike the token
# refresh in useroauth: the pollers are single threaded -- runloop's Event is
# for signalling, and sampling happens on the main thread -- so there is
# nothing here for two threads to race over.
_conn = None
_failed_at = 0.0    # time.monotonic() of the last failed connect; 0 when fine


def connection():
    """The live connection, opening one if needed.

    Raises NotConfigured when there is no DSN and Unreachable when there is one
    that will not answer. Callers decide which of those is fatal.
    """
    global _conn, _failed_at

    if not configured():
        raise NotConfigured(NOT_CONFIGURED.format(env=config.ENV_PATH,
                                                  prog=config.invocation()))
    if _conn is not None and not _conn.closed:
        return _conn
    _conn = None

    # Inside the cooldown, fail without touching the network. This is what stops
    # an hour-long outage costing CONNECT_TIMEOUT on every tick.
    waited = time.monotonic() - _failed_at
    if _failed_at and waited < RETRY_COOLDOWN:
        raise Unreachable("last attempt failed {:.0f}s ago, not retrying for "
                          "another {:.0f}s".format(waited, RETRY_COOLDOWN - waited))

    psycopg = _psycopg()
    try:
        _conn = psycopg.connect(
            dsn(),
            # Per-statement commit. A writer that inserts one row a minute has
            # nothing to group into a transaction, and psycopg's default -- an
            # implicit BEGIN held open until somebody commits -- does two bad
            # things to a process that lives for months: it pins the server's
            # oldest snapshot so vacuum can never reclaim anything, and it turns
            # ONE failed statement into every later statement failing with
            # InFailedSqlTransaction until someone rolls back.
            autocommit=True,
            connect_timeout=CONNECT_TIMEOUT,
            application_name=APP_NAME)
        # Pinned rather than inherited. storage.read_samples() has always
        # returned aware UTC datetimes and everything downstream calls
        # .astimezone() on them, so whatever the server's TimeZone happens to be
        # must not be able to change what a chart looks like. The report
        # functions take the reporting zone as an explicit parameter instead.
        _conn.execute("SET TIME ZONE 'UTC'")
    except (psycopg.Error, OSError) as exc:
        _failed_at = time.monotonic()
        _conn = None
        raise Unreachable(_first_line(exc)) from exc
    _failed_at = 0.0
    return _conn


def _discard():
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:  # noqa: BLE001 - it is already broken
            pass
    _conn = None


def close():
    """Drop the connection. For tests, and for a command that is finished."""
    _discard()


def reset():
    """Forget a previous failure, so a cooldown from earlier in this process
    cannot make an explicit `db --status` lie about the database."""
    global _failed_at
    _failed_at = 0.0


# --------------------------------------------------------------------------
# statements
# --------------------------------------------------------------------------


def execute(sql, params=None, fetch=False):
    """Run one statement, reconnecting once if the connection had gone stale.

    A connection idle for a minute between samples is one that a server
    restart, a pgbouncer reload or a NAT table can have closed without saying
    so; libpq only finds out when it writes the next statement into a dead
    socket. So the first failure is worth exactly one silent reconnect and
    retry, and the second one is real.

    A syntax error or a constraint violation is NOT retried: it would fail
    identically, and the message is the useful part.
    """
    psycopg = _psycopg()
    for attempt in (1, 2):
        conn = connection()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or ())
                return cur.fetchall() if fetch else cur.rowcount
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            _discard()
            if attempt == 2:
                raise Unreachable(_first_line(exc)) from exc
        except psycopg.Error as exc:
            raise Unreachable("{}: {}".format(type(exc).__name__,
                                              _first_line(exc))) from exc
    return None  # unreachable; keeps the linter quiet about the loop


def execute_many(sql, rows, batch=1000):
    """The same, batched, for the archive import. Returns how many rows ran.

    Batched rather than one call with fifty thousand parameter sets, so a
    failure halfway through has already committed what came before it and
    re-running finishes the job instead of starting it again. The unique
    constraint makes that overlap free.
    """
    psycopg = _psycopg()
    rows = list(rows)
    done = 0
    for start in range(0, len(rows), batch):
        chunk = rows[start:start + batch]
        conn = connection()
        try:
            with conn.cursor() as cur:
                cur.executemany(sql, chunk)
        except psycopg.Error as exc:
            raise Unreachable(_first_line(exc)) from exc
        done += len(chunk)
    return done


NOT_MIGRATED = """The database at {where} has no tables yet.

    {prog} db --init

Reachable-and-empty is a different problem from unreachable, and it has a
one-line fix, so it is worth being told which one you have."""


def probe(require_schema=False):
    """(ok, why) -- never raises. For `db --status` and the preflights.

    Everything else here signals by exception, because the callers have
    genuinely different responses to the failures. A status line just wants the
    sentence.

    `require_schema` also checks the tables exist. A database that answers
    SELECT 1 but has never been migrated is reachable and useless, and without
    this the first thing to notice is a query failing deep inside a render --
    which reads as a bug rather than as a setup step nobody ran.
    """
    reset()
    try:
        execute("SELECT 1")
        if require_schema and not execute(
                "SELECT to_regclass('tm.sample_all') IS NOT NULL", fetch=True)[0][0]:
            return False, "no tables yet -- run: {} db --init".format(invocation())
    except NotConfigured:
        return False, "no TWITCH_DATABASE_URL is set"
    except Unreachable as exc:
        return False, str(exc)
    except SystemExit:
        return False, "psycopg is not installed"
    return True, "connected to {}".format(where())


# --------------------------------------------------------------------------
# applying the schema
# --------------------------------------------------------------------------

# Created before any numbered file runs, because it is how we know which of them
# already have. Kept deliberately trivial -- a version and when it landed. This
# project should not grow a migration framework to manage three files.
VERSION_TABLE = """
CREATE SCHEMA IF NOT EXISTS tm;
CREATE TABLE IF NOT EXISTS tm.schema_version (
    version    integer NOT NULL PRIMARY KEY,
    filename   text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""

_FILE_RE = re.compile(r"^(\d+)_.*\.sql$")


def sql_files():
    """[(version, filename, path)] for every numbered file in sql/, in order.

    A missing directory is an error worth being loud about rather than an empty
    list: it means the package was installed without its package-data, and the
    symptom otherwise is `db --init` cheerfully creating nothing at all.
    """
    if not os.path.isdir(SQL_DIR):
        raise SystemExit(
            "The SQL files are missing from {}\n"
            "The package was installed without them. From a checkout:\n"
            "    pip install -e '.[pg]'".format(SQL_DIR))
    found = []
    for name in sorted(os.listdir(SQL_DIR)):
        match = _FILE_RE.match(name)
        if match:
            found.append((int(match.group(1)), name, os.path.join(SQL_DIR, name)))
    if not found:
        raise SystemExit("No numbered .sql files in {}".format(SQL_DIR))
    return found


def applied_versions():
    """The versions already recorded, as a set. Empty on a fresh database."""
    execute(VERSION_TABLE)
    rows = execute("SELECT version FROM tm.schema_version", fetch=True)
    return {row[0] for row in rows}


def apply_schema(dry_run=False, log=print):
    """Apply every numbered file the database has not seen. Returns how many.

    One transaction per file, which PostgreSQL makes worth doing: DDL here is
    transactional, so a file that fails halfway leaves nothing behind and can
    simply be fixed and re-run. Recording the version inside that same
    transaction is what stops a file being counted as applied when it wasn't.
    """
    pending = sql_files()
    if not dry_run:
        already = applied_versions()
        pending = [entry for entry in pending if entry[0] not in already]

    if dry_run:
        for _, name, path in pending:
            log("-- {}".format(name))
            with open(path, encoding="utf-8") as handle:
                log(handle.read().rstrip() + "\n")
        return len(pending)

    psycopg = _psycopg()
    for version, name, path in pending:
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        conn = connection()
        try:
            # autocommit is on for the poller's sake, so a file that must be
            # all-or-nothing has to say so explicitly.
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(body)
                    cur.execute("INSERT INTO tm.schema_version (version, filename) "
                                "VALUES (%s, %s)", (version, name))
        except psycopg.Error as exc:
            raise SystemExit("{} failed, and was rolled back:\n    {}".format(
                name, _first_line(exc))) from exc
        log("applied  {}".format(name))
    return len(pending)


def schema_state():
    """[(version, filename, applied_at_or_None)] for `db --status`."""
    try:
        already = {row[0]: row[1] for row in execute(
            "SELECT version, applied_at FROM tm.schema_version", fetch=True)}
    except Unreachable:
        already = {}
    return [(version, name, already.get(version))
            for version, name, _ in sql_files()]


def require_readable():
    """Exit with the right message unless the samples can actually be read.

    The gate for every reader -- `graph`, `daily` -- and the reason it exists is
    that three different things all look like "it didn't work" from the outside,
    and each has a different fix:

        nothing configured    put a URL in .env
        configured, down      start it, or fix the host
        up, but never migrated    db --init

    Lumping the third under "cannot reach the database" would be a lie, and the
    one it would send you looking in the wrong place for the longest.
    """
    if not configured():
        raise SystemExit(NOT_CONFIGURED.format(env=config.ENV_PATH,
                                               prog=invocation()))
    ok, why = probe()
    if not ok:
        raise SystemExit(UNREACHABLE.format(where=where(), why=why, prog=invocation()))
    try:
        migrated = execute("SELECT to_regclass('tm.sample_all') IS NOT NULL",
                           fetch=True)[0][0]
    except Unreachable as exc:
        raise SystemExit(UNREACHABLE.format(where=where(), why=exc, prog=invocation()))
    if not migrated:
        raise SystemExit(NOT_MIGRATED.format(where=where(), prog=invocation()))
