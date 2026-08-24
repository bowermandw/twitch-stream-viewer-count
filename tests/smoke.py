#!/usr/bin/env python3
"""Offline checks for the parts that don't need credentials.

Run from the project root:   python3 tests/smoke.py

Everything here works against the committed fixtures, so it needs no network,
no .env and no tokens. It won't catch API contract changes — only regressions
in parsing, session detection, chart building and CLI wiring.
"""

import glob
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from twitchmetrics import chart, config, drive, driveoauth, png, storage  # noqa: E402
from twitchmetrics.commands import daily  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
PLAIN = os.path.join(FIXTURES, "metrics_testchannel.csv")
BREAKS = os.path.join(FIXTURES, "metrics_breaktest.csv")
VIEWERS = os.path.join(FIXTURES, "viewers_testchannel.csv")

passed = failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  ok    {}".format(label))
    else:
        failed += 1
        print("  FAIL  {}{}".format(label, "  ({})".format(detail) if detail else ""))


def section(name):
    print("\n{}".format(name))


def skipped(label, why):
    """Neither pass nor fail — the machine can't run this one."""
    print("  skip  {}  ({})".format(label, why))


def raises(kind, function, *args, **kwargs):
    try:
        function(*args, **kwargs)
    except kind:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def sample(when, live):
    return {"when": when, "live": live, "viewers": 1 if live else None,
            "followers": 1, "chatters": 1, "title": "", "game": "", "stream_id": ""}


# --- parsing --------------------------------------------------------------
section("storage")
samples = storage.read_samples(PLAIN)
check("reads the metrics fixture", len(samples) == 116, "got %d" % len(samples))
check("offline rows keep followers",
      all(s["followers"] is not None for s in samples if not s["live"]))
check("offline rows have no viewer count",
      all(s["viewers"] is None for s in samples if not s["live"]))
check("viewers-only fixture parses with empty extras",
      all(s["followers"] is None for s in storage.read_samples(VIEWERS)))
check("a title containing commas survives",
      any("," in s["title"] for s in samples if s["live"]))

# --- session detection ----------------------------------------------------
section("sessions")
sessions = chart.split_sessions(samples)
check("finds both broadcasts", len(sessions) == 2, "got %d" % len(sessions))
check("charts only live samples", all(s["live"] for x in sessions for s in x))
check("breaks split into four broadcasts",
      len(chart.split_sessions(storage.read_samples(BREAKS))) == 4)

# --- day selection --------------------------------------------------------
section("day selection")
break_samples = storage.read_samples(BREAKS)
days = chart.days_present(break_samples)
check("two days present", len(days) == 2, str(days))
window = chart.select_day(break_samples, date(2026, 8, 19))
check("day window spans the breaks", len(window) == 112, "got %d" % len(window))
check("day window keeps offline rows", any(not s["live"] for s in window))
check("day window starts and ends live", window[0]["live"] and window[-1]["live"])
spans = chart.offline_spans(window)
check("two offline stretches found", len(spans) == 2, str(len(spans)))
check("unknown day returns nothing", chart.select_day(break_samples, date(2020, 1, 1)) == [])

# --- gaps -----------------------------------------------------------------
section("gap handling")
runs = chart.runs_of(window, "viewers")
check("viewer line breaks into three runs", len(runs) == 3, "got %d" % len(runs))
check("follower line stays unbroken", len(chart.runs_of(window, "followers")) == 1)

# --- axes -----------------------------------------------------------------
section("axes")
low, high, step = chart.axis_bounds([700, 743], zero_based=False)
check("follower axis is not zero-based", low > 100, "low=%s" % low)
low0, high0, _ = chart.axis_bounds([35, 740], zero_based=True)
check("viewer axis starts at zero", low0 == 0)
check("flat series still gets a band", chart.axis_bounds([5, 5], False)[0] < 5)

# --- paths ----------------------------------------------------------------
section("paths")
check("IGN and ign share a file", config.channel_slug("IGN") == config.channel_slug("ign"))
check("hostile names stay inside data/",
      os.path.dirname(os.path.abspath(config.metrics_csv("../../etc/passwd")))
      == os.path.abspath(config.DATA_DIR))

# --- rendering ------------------------------------------------------------
section("rendering")
session = sessions[-1]
metrics = chart.available_metrics(session)
check("three metrics detected", len(metrics) == 3, str([m["key"] for m in metrics]))
svg = chart.render_stacked(session, "testchannel", 30, 1300)
check("stacked svg is well-formed", svg.startswith("<svg") and svg.endswith("</svg>"))
check("stacked svg names all three series",
      all(m["label"] in svg for m in chart.METRICS))
one = chart.render_stacked(session, "t", 30, 1300, metrics=[chart.METRIC_BY_KEY["chatters"]])
check("--only really filters", "Concurrent viewers" not in one and "In chat" in one)
check("composite renders", chart.render_composite(session, "t", 1300, 470).endswith("</svg>"))
check("peaks show a clock time", "AM" in svg or "PM" in svg)

# --- cli ------------------------------------------------------------------
section("cli")
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with tempfile.TemporaryDirectory() as tmp:
    for label, argv in [
        ("--help", ["--help"]),
        ("graph a fixture", ["graph", PLAIN, "--output", os.path.join(tmp, "a.svg")]),
        ("graph --composite", ["graph", PLAIN, "--composite", "--output",
                               os.path.join(tmp, "b.svg")]),
        ("graph --date", ["graph", BREAKS, "--date", "2026-08-19", "--output",
                          os.path.join(tmp, "c.svg")]),
        ("graph --list-days", ["graph", BREAKS, "--list-days"]),
        ("graph --list-sessions", ["graph", BREAKS, "--list-sessions"]),
        ("testdata", ["testdata", "smoketest", "--out-dir", tmp]),
    ]:
        result = subprocess.run([sys.executable, "-m", "twitchmetrics"] + argv,
                                cwd=root, capture_output=True, text=True)
        check(label, result.returncode == 0,
              (result.stderr or result.stdout).strip().splitlines()[-1:] or "")

    for label, argv in [
        ("bad --date rejected", ["graph", PLAIN, "--date", "nonsense"]),
        ("unknown metric rejected", ["graph", PLAIN, "--only", "sandwiches"]),
        ("--date with --session rejected", ["graph", BREAKS, "--date", "2026-08-19",
                                            "--session", "0"]),
        ("missing file reported", ["graph", os.path.join(tmp, "nope.csv")]),
    ]:
        result = subprocess.run([sys.executable, "-m", "twitchmetrics"] + argv,
                                cwd=root, capture_output=True, text=True)
        check(label, result.returncode != 0, "expected non-zero exit")

# --- interval configuration -----------------------------------------------
section("interval")
_saved = os.environ.pop("TWITCH_INTERVAL", None)
check("defaults to 300", config.resolve_interval(None) == 300)
os.environ["TWITCH_INTERVAL"] = "60"
check("TWITCH_INTERVAL is honoured", config.resolve_interval(None) == 60)
check("--interval beats the env var", config.resolve_interval(120) == 120)
for _bad in ("sixty", "5", "-1", "1.5"):
    os.environ["TWITCH_INTERVAL"] = _bad
    try:
        config.resolve_interval(None)
        check("rejects {!r}".format(_bad), False, "accepted it")
    except SystemExit:
        check("rejects {!r}".format(_bad), True)
os.environ["TWITCH_INTERVAL"] = ""   # emptying a line means "unset", as for TWITCH_CHANNEL
check("empty value falls back to the default", config.resolve_interval(None) == 300)
os.environ.pop("TWITCH_INTERVAL", None)
if _saved is not None:
    os.environ["TWITCH_INTERVAL"] = _saved

# --- concurrency safety ---------------------------------------------------
section("concurrency")
from twitchmetrics import useroauth as _uo  # noqa: E402
check("token writes are atomic (temp + rename)",
      "os.replace" in open(os.path.join(root, "twitchmetrics/useroauth.py")).read())
check("app token writes are atomic",
      "os.replace" in open(os.path.join(root, "twitchmetrics/auth.py")).read())
check("refresh is lock-protected", hasattr(_uo, "_refresh_lock"))
check("poller re-reads the token each tick, not once at startup",
      "_current_user_token" in open(os.path.join(root, "twitchmetrics/commands/poll.py")).read())
check("channels get separate data files",
      config.metrics_csv("a") != config.metrics_csv("b"))
check("systemd unit is a per-channel template",
      "%i" in open(os.path.join(root, "deploy/twitch-metrics@.service")).read())

# --- signals --------------------------------------------------------------
section("shutdown signals")
import signal as _signal  # noqa: E402
import time as _time      # noqa: E402
for _name, _sig in (("SIGTERM", _signal.SIGTERM), ("SIGINT", _signal.SIGINT)):
    with tempfile.TemporaryDirectory() as _tmp:
        _env = dict(os.environ, TWITCH_DATA_DIR=_tmp, TWITCH_CHARTS_DIR=_tmp,
                    TWITCH_CLIENT_ID="x", TWITCH_CLIENT_SECRET="y")
        _p = subprocess.Popen([sys.executable, "-u", "-m", "twitchmetrics", "poll",
                               "nochannel", "--interval", "60", "--viewers-only"],
                              cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, env=_env)
        _time.sleep(3)
        _started = _time.time()
        _p.send_signal(_sig)
        try:
            _out, _ = _p.communicate(timeout=12)
            _took = _time.time() - _started
        except subprocess.TimeoutExpired:
            _p.kill(); _out, _took = _p.communicate()[0], 99
        check("{} exits 0".format(_name), _p.returncode == 0, "got %s" % _p.returncode)
        check("{} logs a summary".format(_name), "stop " in _out)
        check("{} doesn't wait out the interval".format(_name), _took < 10,
              "took %.1fs" % _took)

# --- invocation name ------------------------------------------------------
section("invocation name")
result = subprocess.run([sys.executable, "-m", "twitchmetrics", "--help"],
                        cwd=root, capture_output=True, text=True)
check("-m help says 'python3 -m twitchmetrics'",
      "usage: python3 -m twitchmetrics" in result.stdout)
check("-m help doesn't claim the console script name",
      "twitch-metrics setup" not in result.stdout)
result = subprocess.run([sys.executable, "-m", "twitchmetrics", "graph", "no_such_channel_xyz"],
                        cwd=root, capture_output=True, text=True)
check("runtime hints use the same invocation",
      "python3 -m twitchmetrics poll" in (result.stdout + result.stderr))

# --- date keywords --------------------------------------------------------
section("date keywords")
check("'today' is a local date",
      chart.parse_day("today") == datetime.now().astimezone().date())
check("'yesterday' is one day back",
      (chart.parse_day("today") - chart.parse_day("yesterday")).days == 1)
check("an explicit date parses", chart.parse_day("2026-08-19") == date(2026, 8, 19))
check("case and spacing don't matter", chart.parse_day("  TODAY ") == chart.parse_day("today"))
for _bad in ("nonsense", "2026-13-01", "", "19/08/2026"):
    check("rejects {!r}".format(_bad), raises(ValueError, chart.parse_day, _bad))
check("graph and daily share one parser",
      "chart.parse_day" in open(os.path.join(root, "twitchmetrics/commands/graph_cmd.py")).read())

# --- daily: channel discovery ---------------------------------------------
section("daily discovery")
with tempfile.TemporaryDirectory() as _wants:
    for _name in ("twitch-metrics@alpha.service", "twitch-metrics@beta.service",
                  "unrelated.service", "twitch-metrics@.service"):
        open(os.path.join(_wants, _name), "w").close()
    check("finds enabled instances", daily.units_in(_wants) == ["alpha", "beta"],
          str(daily.units_in(_wants)))
    check("ignores unrelated units", "unrelated" not in daily.units_in(_wants))
    check("ignores the bare template", "" not in daily.units_in(_wants))
    _found, _source = daily.discover_channels(wants_dirs=[_wants])
    check("discovery finds them", _found == ["alpha", "beta"], str(_found))
    check("discovery reports its source", "systemd" in _source, _source)
    check("an empty wants dir discovers nothing",
          daily.discover_channels(wants_dirs=[os.path.join(_wants, "nope")])[0] == [])
check("explicit channels win", daily.discover_channels(["one"], wants_dirs=[])[0] == ["one"])
check("the source phrase reads after 'from'",
      daily.discover_channels(["one"], wants_dirs=[])[1] == "the command line")
check("the env list splits on commas and spaces", daily.split_list("a, b  c") == ["a", "b", "c"])
check("duplicates collapse case-insensitively",
      daily.discover_channels(["IGN", "ign"])[0] == ["IGN"])
check("an impossible login is rejected, not half-unescaped",
      daily.discover_channels(["ok_name", "../../etc/passwd"])[0] == ["ok_name"])
_saved_daily = os.environ.pop("TWITCH_DAILY_CHANNELS", None)
os.environ["TWITCH_DAILY_CHANNELS"] = "alpha, beta"
os.environ["TWITCH_SYSTEMD_WANTS_DIR"] = os.path.join(tempfile.gettempdir(), "no_such_wants")
check("TWITCH_DAILY_CHANNELS is honoured",
      daily.discover_channels()[0] == ["alpha", "beta"])
check("and it says so", daily.discover_channels()[1] == "TWITCH_DAILY_CHANNELS")
os.environ["TWITCH_DAILY_CHANNELS"] = ""
check("an empty list is not a channel list", daily.discover_channels()[0] == [])
os.environ.pop("TWITCH_DAILY_CHANNELS", None)
os.environ.pop("TWITCH_SYSTEMD_WANTS_DIR", None)
if _saved_daily is not None:
    os.environ["TWITCH_DAILY_CHANNELS"] = _saved_daily

# --- daily: dark vs silent ------------------------------------------------
section("daily day status")
_brk = storage.read_samples(BREAKS)
check("a day with live rows is live", daily.classify_day(_brk, date(2026, 8, 19)) == "live")
check("a day with no rows is silent", daily.classify_day(_brk, date(2020, 1, 1)) == "silent")
_noon = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
check("a day with only offline rows is dark",
      daily.classify_day([sample(_noon, False)], _noon.astimezone().date()) == "dark")
check("one live row is enough to be live",
      daily.classify_day([sample(_noon, False), sample(_noon, True)],
                         _noon.astimezone().date()) == "live")
check("a dark day is not counted as a failure",
      'log("skip' in open(os.path.join(root, "twitchmetrics/commands/daily.py")).read())

# --- daily: the render and convert path -----------------------------------
section("daily render")
if not png.converter_path():
    skipped("daily renders a PNG", "rsvg-convert not installed")
else:
    with tempfile.TemporaryDirectory() as tmp:
        import shutil as _shutil
        _shutil.copy(BREAKS, os.path.join(tmp, "metrics_breaktest.csv"))
        _env = dict(os.environ, TWITCH_DATA_DIR=tmp, TWITCH_CHARTS_DIR=tmp,
                    TWITCH_SYSTEMD_WANTS_DIR=os.path.join(tmp, "none"),
                    TWITCH_ENV_FILE=os.path.join(tmp, ".env"))
        for _k in ("TWITCH_DAILY_CHANNELS", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
            _env.pop(_k, None)
        _r = subprocess.run([sys.executable, "-m", "twitchmetrics", "daily", "breaktest",
                             "--date", "2026-08-19", "--dry-run"],
                            cwd=root, capture_output=True, text=True, env=_env)
        _out = os.path.join(tmp, "chart_breaktest_2026-08-19.png")
        check("daily --dry-run exits 0", _r.returncode == 0,
              (_r.stderr or _r.stdout).strip()[-200:])
        check("daily wrote a PNG next to the SVG", os.path.exists(_out))
        check("the PNG is really a PNG",
              os.path.exists(_out) and open(_out, "rb").read(4) == b"\x89PNG")
        check("the SVG is kept too",
              os.path.exists(os.path.join(tmp, "chart_breaktest_2026-08-19.svg")))
        check("no .part file is left behind", not glob.glob(os.path.join(tmp, "*.part")))
        check("--dry-run needs no Google credentials",
              "GOOGLE_CLIENT" not in (_r.stdout + _r.stderr))
        check("--dry-run doesn't try to authorize",
              "drive --auth" not in (_r.stdout + _r.stderr))
        check("the summary is the last line", "stop " in _r.stdout.strip().splitlines()[-1])
        check("it logs to data/daily.log", os.path.exists(os.path.join(tmp, "daily.log")))

        _r2 = subprocess.run([sys.executable, "-m", "twitchmetrics", "daily", "breaktest",
                              "nosuchchannel", "--date", "2026-08-19", "--dry-run"],
                             cwd=root, capture_output=True, text=True, env=_env)
        check("one bad channel makes it exit 1", _r2.returncode == 1)
        check("the good channel still ran", "chart_breaktest" in _r2.stdout)
        check("and the failure is named", "nosuchchannel" in _r2.stdout)

# --- daily: preflight fails loudly ----------------------------------------
section("daily preflight")
with tempfile.TemporaryDirectory() as tmp:
    # sys.executable is absolute, so emptying PATH hides rsvg-convert only.
    _bare = dict(os.environ, PATH=tmp, TWITCH_DATA_DIR=tmp, TWITCH_CHARTS_DIR=tmp,
                 TWITCH_SYSTEMD_WANTS_DIR=os.path.join(tmp, "none"))
    _r = subprocess.run([sys.executable, "-m", "twitchmetrics", "daily", "breaktest",
                         "--dry-run"], cwd=root, capture_output=True, text=True, env=_bare)
    check("a missing rsvg-convert exits non-zero", _r.returncode != 0)
    check("and says which package provides it",
          "librsvg2-bin" in (_r.stdout + _r.stderr))
    _empty = dict(os.environ, TWITCH_DATA_DIR=tmp, TWITCH_CHARTS_DIR=tmp,
                  TWITCH_DAILY_CHANNELS="",
                  TWITCH_SYSTEMD_WANTS_DIR=os.path.join(tmp, "none"))
    _r = subprocess.run([sys.executable, "-m", "twitchmetrics", "daily"],
                        cwd=root, capture_output=True, text=True, env=_empty)
    check("no channels is not silent success", _r.returncode != 0)
    check("and it suggests how to name them",
          "TWITCH_DAILY_CHANNELS" in (_r.stdout + _r.stderr))
    _r = subprocess.run([sys.executable, "-m", "twitchmetrics", "daily", "--list-channels"],
                        cwd=root, capture_output=True, text=True, env=_empty)
    check("--list-channels needs no converter and no token",
          "Traceback" not in _r.stderr, _r.stderr.strip()[-200:])

# --- drive query building -------------------------------------------------
section("drive queries")
_q = drive.folder_query("Twitch Metrics", "root")
check("folder query excludes trashed items", "trashed = false" in _q)
check("folder query pins the folder mime type", drive.FOLDER_MIME in _q)
check("folder query scopes to the parent", "'root' in parents" in _q)
check("an apostrophe in a folder name is escaped",
      drive.folder_query("Doug's Charts", "root").count("\\'") == 1)
check("a backslash is escaped before the quote", drive._escape("a\\'b") == "a\\\\\\'b",
      repr(drive._escape("a\\'b")))
check("a hostile folder name can't inject a clause",
      drive.folder_query("x' or trashed = true or name = 'y", "root").count(" and ") == 3)
check("the query survives URL encoding intact",
      urllib.parse.parse_qs(drive._list_url(_q).split("?", 1)[1])["q"][0] == _q)
check("file query does not constrain the mime type",
      drive.FOLDER_MIME not in drive.file_query("2026-08-23.png", "abc"))

# --- drive multipart body -------------------------------------------------
section("drive multipart")
_safe = b"\x00\x01\xff\xfe"   # no bare LF, so the CRLF check below can't lie
_body = drive._multipart_body({"name": "2026-08-23.png", "parents": ["abc"]},
                              _safe, "image/png", "BOUND")
check("the body is bytes", isinstance(_body, bytes))
check("every line ends CRLF", b"\n" not in _body.replace(b"\r\n", b""))
check("it opens with the boundary", _body.startswith(b"--BOUND\r\n"))
check("it closes with the terminator", _body.endswith(b"--BOUND--\r\n"))
check("it has exactly two parts", _body.count(b"--BOUND") == 3)
check("the metadata part declares JSON",
      b"Content-Type: application/json; charset=UTF-8\r\n\r\n" in _body)
check("the content part declares its own type", b"Content-Type: image/png\r\n\r\n" in _body)
check("a blank line separates headers from each body", _body.count(b"\r\n\r\n") == 2)
check("metadata carries the name and the parent",
      b'"name": "2026-08-23.png"' in _body and b'"parents": ["abc"]' in _body)
_png_bytes = b"\x89PNG\r\n\x1a\n\x00IDAT\xff\xd9"   # contains CRLF and a bare LF
check("binary content survives verbatim",
      _png_bytes in drive._multipart_body({}, _png_bytes, "image/png", "B"))
check("a str payload is refused, not silently mangled",
      raises(TypeError, drive._multipart_body, {}, "not bytes", "image/png", "B"))
check("boundaries are unique per upload", drive._new_boundary() != drive._new_boundary())
check("the boundary is long enough to never collide", len(drive._new_boundary()) > 30)

# --- drive naming ---------------------------------------------------------
section("drive naming")
check("chart files are named by date", drive.remote_name(date(2026, 8, 23)) == "2026-08-23.png")
check("the extension is honoured",
      drive.remote_name(date(2026, 8, 23), ".svg") == "2026-08-23.svg")
check("png gets the right content type", drive.content_type_for("a/b.PNG") == "image/png")
check("svg gets the right content type", drive.content_type_for("x.svg") == "image/svg+xml")
check("an unknown extension falls back to octet-stream",
      drive.content_type_for("x.weird") == "application/octet-stream")
check("Drive folders use the same slug as the CSVs",
      drive.channel_folder_name("IGN") == config.channel_slug("ign"))
check("a hostile channel can't escape its Drive folder",
      "/" not in drive.channel_folder_name("../../etc/passwd"))
check("the target path is folder/channel/date",
      drive.target_path("IGN", date(2026, 8, 23), "Charts") == "Charts/ign/2026-08-23.png")

# --- drive retry policy ---------------------------------------------------
section("drive retries")
check("rate limits are retried", drive._is_retryable(403, "rateLimitExceeded"))
check("quota exhaustion is retried", drive._is_retryable(403, "quotaExceeded"))
check("429 is retried", drive._is_retryable(429, ""))
check("5xx is retried", drive._is_retryable(503, ""))
check("a permission 403 is not retried",
      not drive._is_retryable(403, "insufficientFilePermissions"))
check("404 is not retried", not drive._is_retryable(404, "notFound"))
check("400 is not retried", not drive._is_retryable(400, "badRequest"))
_waits = [drive._retry_after(i) for i in range(drive.MAX_ATTEMPTS)]
check("backoff grows", _waits == sorted(_waits), str(_waits))
check("backoff stays bounded", max(_waits) < 20, str(_waits))
check("Retry-After wins when Drive sends one", drive._retry_after(0, "7") >= 7)
check("a nonsense Retry-After is ignored", drive._retry_after(0, "soon") < 2)
check("a non-JSON error body doesn't explode",
      drive._error_reason_from_bytes(b"<html>502</html>") == "")
check("the reason is read out of a real error body",
      drive._error_reason_from_bytes(
          b'{"error":{"code":403,"errors":[{"reason":"rateLimitExceeded"}]}}')
      == "rateLimitExceeded")
check("retries wrap find-then-write, not a bare request",
      "_with_backoff" in open(os.path.join(root, "twitchmetrics/drive.py")).read())

# --- drive token storage --------------------------------------------------
section("drive token storage")
check("the Google token lives in data/ with the others",
      os.path.dirname(os.path.abspath(config.GOOGLE_TOKEN_PATH))
      == os.path.abspath(config.DATA_DIR))
check("it is a separate file from the Twitch token",
      config.GOOGLE_TOKEN_PATH != config.USER_TOKEN_PATH)
check("the folder cache lives in data/ too",
      os.path.dirname(os.path.abspath(config.DRIVE_FOLDERS_PATH))
      == os.path.abspath(config.DATA_DIR))
check("Google token writes are atomic (temp + rename)",
      "os.replace" in open(os.path.join(root, "twitchmetrics/driveoauth.py")).read())
check("Google refresh is lock-protected", hasattr(driveoauth, "_refresh_lock"))
check("offline access is requested",
      "\"access_type\": \"offline\"" in open(
          os.path.join(root, "twitchmetrics/driveoauth.py")).read())
check("re-consent still returns a refresh token",
      "\"prompt\": \"consent\"" in open(
          os.path.join(root, "twitchmetrics/driveoauth.py")).read())
check("a refresh response without a refresh_token keeps the old one",
      driveoauth._store.__doc__ and "keeping the old refresh token"
      in driveoauth._store.__doc__)

_real_token_path = config.GOOGLE_TOKEN_PATH
with tempfile.TemporaryDirectory() as tmp:
    config.GOOGLE_TOKEN_PATH = os.path.join(tmp, ".google_token.json")
    driveoauth.save_token({"access_token": "x", "expires_at": 0})
    check("the token file is written 0600",
          stat.S_IMODE(os.stat(config.GOOGLE_TOKEN_PATH).st_mode) == 0o600)
    check("a token round-trips through the file",
          driveoauth.load_token()["access_token"] == "x")
    check("no temp files are left behind",
          not [f for f in os.listdir(tmp) if ".tmp" in f])
    with open(config.GOOGLE_TOKEN_PATH, "w") as _h:
        _h.write("{")
    check("a half-written token file reads as absent", driveoauth.load_token() is None)
config.GOOGLE_TOKEN_PATH = _real_token_path

# --- drive without credentials --------------------------------------------
section("drive without credentials")
_real_env = config.ENV_PATH
_saved_google = {k: os.environ.pop(k, None)
                 for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")}
with tempfile.TemporaryDirectory() as tmp:
    config.ENV_PATH = os.path.join(tmp, ".env")
    check("missing Google credentials are not fatal when optional",
          config.load_google_credentials(required=False) == (None, None))
    check("requiring them exits with an actionable message",
          raises(SystemExit, config.load_google_credentials))
    config.update_env({"TWITCH_CLIENT_ID": "x", "TWITCH_CHANNEL": "keepme"})
    config.update_env({"GOOGLE_CLIENT_ID": "g", "GOOGLE_CLIENT_SECRET": "s"})
    _reloaded = config.load_env_file()
    check("update_env keeps the keys it wasn't asked about",
          _reloaded.get("TWITCH_CHANNEL") == "keepme" and _reloaded.get("TWITCH_CLIENT_ID") == "x",
          str(_reloaded))
    check("update_env adds the new ones", _reloaded.get("GOOGLE_CLIENT_ID") == "g")
    config.update_env({"GOOGLE_CLIENT_ID": "g2"})
    check("update_env rewrites in place rather than appending a duplicate",
          config.load_env_file().get("GOOGLE_CLIENT_ID") == "g2"
          and open(config.ENV_PATH).read().count("GOOGLE_CLIENT_ID") == 1)
    check("the merged .env is still 0600",
          stat.S_IMODE(os.stat(config.ENV_PATH).st_mode) == 0o600)
config.ENV_PATH = _real_env
for _k, _v in _saved_google.items():
    if _v is not None:
        os.environ[_k] = _v

with tempfile.TemporaryDirectory() as tmp:
    _env = dict(os.environ, TWITCH_DATA_DIR=tmp, TWITCH_CHARTS_DIR=tmp,
                TWITCH_ENV_FILE=os.path.join(tmp, ".env"))
    for _label, _argv in [
        ("drive --status works with no token", ["drive", "--status"]),
        ("drive --dry-run needs no network",
         ["drive", "testchannel", "--file", PLAIN, "--dry-run"]),
    ]:
        _r = subprocess.run([sys.executable, "-m", "twitchmetrics"] + _argv,
                            cwd=root, capture_output=True, text=True, env=_env)
        check(_label, _r.returncode == 0, (_r.stderr or _r.stdout).strip()[-200:])
        check(_label + " — no traceback", "Traceback" not in _r.stderr)
    for _label, _argv in [
        ("drive rejects a bad --date", ["drive", "x", "--date", "nonsense"]),
        ("drive reports a missing file",
         ["drive", "x", "--file", os.path.join(tmp, "nope.png")]),
        ("drive upload without a token fails cleanly", ["drive", "x", "--file", PLAIN]),
    ]:
        _r = subprocess.run([sys.executable, "-m", "twitchmetrics"] + _argv,
                            cwd=root, capture_output=True, text=True, env=_env)
        check(_label, _r.returncode != 0, "expected non-zero exit")
        check(_label + " — no traceback", "Traceback" not in _r.stderr,
              _r.stderr.strip()[-200:])

# --- drive setup ordering -------------------------------------------------
section("drive setup")
_setup_src = open(os.path.join(root, "twitchmetrics/commands/drive_cmd.py")).read()
# The bug this guards: _setup withheld .env until a Drive call succeeded, but
# that call went through connect(), which reads the credentials back out of
# .env — so setup always failed right after a successful browser login.
# Scoped to _setup's body: _check() uses connect() on purpose, because it
# exists to behave exactly as the unattended service will.
_setup_body = _setup_src.split("def _setup")[1].split("\ndef ")[0]
check("setup doesn't verify through connect(), which would re-read .env",
      "drive.connect(" not in _setup_body)
check("setup builds its client from the token it already holds",
      "drive.client_for(" in _setup_body)
check("--check still goes through connect(), as the service does",
      "drive.connect(" in _setup_src.split("def _check")[1])
check("client_for needs no stored credentials",
      "load_google_credentials" not in
      open(os.path.join(root, "twitchmetrics/drive.py")).read().split(
          "def client_for")[1].split("def connect")[0])
check("setup writes .env before the folder check",
      _setup_src.index("update_env") < _setup_src.index("Verifying against Drive"))
check("a fresh token payload is enough to build a client",
      drive.client_for({"access_token": "t", "email": "a@b"})["token"] == "t")

# --- deploy units ---------------------------------------------------------
section("daily units")
_svc = open(os.path.join(root, "deploy/twitch-metrics-daily.service")).read()
_tmr = open(os.path.join(root, "deploy/twitch-metrics-daily.timer")).read()
check("the daily service is a oneshot", "Type=oneshot" in _svc)
check("the daily service is not a template", "%i" not in _svc)
# The service explains in a comment why it has no [Install], so test for the
# directive that would actually make it boot-activated rather than the word.
check("the timer owns activation, not the service", "WantedBy=" not in _svc)
check("it fires at 17:00", "OnCalendar=17:00" in _tmr)
check("catch-up runs are off on purpose", "Persistent=false" in _tmr)
check("the timer starts the daily service",
      "Unit=twitch-metrics-daily.service" in _tmr)
check("the timer is installed into timers.target", "WantedBy=timers.target" in _tmr)
check("data and charts are both writable",
      "ReadWritePaths=" in _svc and "/data" in _svc and "/charts" in _svc)
check("the placeholders still fail loudly", "User=twitch" in _svc)
check("it says which package provides rsvg-convert", "librsvg2-bin" in _svc)
check("it warns about missing fonts", "fonts-dejavu" in _svc)

# --- no dependencies ------------------------------------------------------
section("no dependencies")
_forbidden = re.compile(
    r"^\s*(import|from)\s+"
    r"(google|googleapiclient|google_auth\w*|requests|httplib2|oauth2client|matplotlib)\b",
    re.M)
for _mod in ("driveoauth.py", "drive.py", "png.py", "commands/drive_cmd.py",
             "commands/daily.py"):
    _src = open(os.path.join(root, "twitchmetrics", _mod)).read()
    check("{} imports nothing third-party".format(_mod), not _forbidden.search(_src))

print("\n{} passed, {} failed".format(passed, failed))
sys.exit(1 if failed else 0)
