#!/usr/bin/env python3
"""Offline checks for the parts that don't need credentials.

Run from the project root:   python3 tests/smoke.py

Everything here works against the committed fixtures, so it needs no network,
no .env and no tokens. It won't catch API contract changes — only regressions
in parsing, session detection, chart building and CLI wiring.
"""

import glob
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from twitchmetrics import (chart, config, drive, driveoauth, png, retry, s3,  # noqa: E402
                          storage, trends, youtube)
from twitchmetrics.commands import daily  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
PLAIN = os.path.join(FIXTURES, "metrics_testchannel.csv")
BREAKS = os.path.join(FIXTURES, "metrics_breaktest.csv")
VIEWERS = os.path.join(FIXTURES, "viewers_testchannel.csv")
YOUTUBE = os.path.join(FIXTURES, "youtube_testchannel.csv")

passed = failed = 0


class no_env_file(object):
    """Point config.ENV_PATH at an empty file for the block.

    Every resolver falls back to .env after the environment, so a test that only
    clears an environment variable still reads whatever the developer happens to
    have configured. Two of these passed for months purely because .env had no
    YOUTUBE_CHANNEL in it.
    """

    def __enter__(self):
        self._saved = config.ENV_PATH
        self._dir = tempfile.TemporaryDirectory()
        config.ENV_PATH = os.path.join(self._dir.name, ".env")
        open(config.ENV_PATH, "w").close()
        return config.ENV_PATH

    def __exit__(self, *exc):
        config.ENV_PATH = self._saved
        self._dir.cleanup()
        return False


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
check("a Twitch file has no subscriber series",
      all(s["subscribers"] is None for s in samples))

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
check("stacked svg names every series it detected",
      all(m["label"] in svg for m in metrics))
check("stacked svg names nothing the file lacks",
      chart.METRIC_BY_KEY["subscribers"]["label"] not in svg)
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
with no_env_file():
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
with no_env_file():
    check("empty value falls back to the default", config.resolve_interval(None) == 300)
os.environ.pop("TWITCH_INTERVAL", None)
if _saved is not None:
    os.environ["TWITCH_INTERVAL"] = _saved

# --- youtube --------------------------------------------------------------
section("youtube")
_yt = storage.read_samples(YOUTUBE)
check("reads the youtube fixture", len(_yt) == 60, "got %d" % len(_yt))
check("subscribers parse on every row",
      all(s["subscribers"] is not None for s in _yt))
check("offline rows keep the subscriber count",
      all(s["subscribers"] is not None for s in _yt if not s["live"]))
check("video_id feeds the stream_id key",
      all(s["stream_id"] for s in _yt if s["live"]))
check("no follower or chatter series",
      all(s["followers"] is None and s["chatters"] is None for s in _yt))
_yt_likes = [s["likes"] for s in _yt if s["live"]]
check("likes parse on live rows", all(v is not None for v in _yt_likes))
check("likes only ever climb",
      all(b >= a for a, b in zip(_yt_likes, _yt_likes[1:])))
check("offline rows carry no like count",
      all(s["likes"] is None for s in _yt if not s["live"]))
check("a Twitch file has no like series",
      all(s["likes"] is None for s in storage.read_samples(PLAIN)))

_yt_sessions = chart.split_sessions(_yt)
check("one broadcast in the youtube fixture", len(_yt_sessions) == 1,
      "got %d" % len(_yt_sessions))
_yt_metrics = chart.available_metrics(_yt_sessions[0])
check("viewers, subscribers and likes detected, nothing else",
      [m["key"] for m in _yt_metrics] == ["viewers", "subscribers", "likes"],
      str([m["key"] for m in _yt_metrics]))
_yt_svg = chart.render_stacked(_yt_sessions[0], "testchannel", 10, 1300)
check("youtube svg is well-formed",
      _yt_svg.startswith("<svg") and _yt_svg.endswith("</svg>"))
check("youtube svg names every series",
      all(m["label"] in _yt_svg for m in _yt_metrics))
check("subscribers get a non-zero-based axis",
      chart.METRIC_BY_KEY["subscribers"]["zero_based"] is False)

from twitchmetrics.commands import graph_cmd as _graph  # noqa: E402
check("graph recovers the channel name from a youtube path",
      _graph.pick_source("data/youtube_foo.csv", False)[1] == "foo")

# channel_filter is the whole "handle or id" decision, and it is pure.
check("a bare handle becomes forHandle",
      youtube.channel_filter("testchannel") == {"forHandle": "@testchannel"})
check("a pasted @handle is the same thing",
      youtube.channel_filter("@testchannel") == {"forHandle": "@testchannel"})
_uc = "UC" + "a" * 22
check("a UC… id is used as an id", youtube.channel_filter(_uc) == {"id": _uc})
check("a handle that merely starts with UC is still a handle",
      youtube.channel_filter("UCsomething") == {"forHandle": "@UCsomething"})

_live = {"id": "v1", "snippet": {"liveBroadcastContent": "live"},
         "liveStreamingDetails": {"actualStartTime": "2026-08-21T18:00:00Z",
                                  "concurrentViewers": "2413"}}
_older = {"id": "v0", "snippet": {"liveBroadcastContent": "live"},
          "liveStreamingDetails": {"actualStartTime": "2026-08-01T00:00:00Z"}}
_soon = {"id": "v2", "snippet": {"liveBroadcastContent": "upcoming"},
         "liveStreamingDetails": {"scheduledStartTime": "2026-08-30T19:00:00Z"}}
check("scheduled broadcasts are not live", youtube.pick_live([_soon]) is None)
check("the live one is picked out", youtube.pick_live([_soon, _live])["id"] == "v1")
check("two at once takes the one that started last",
      youtube.pick_live([_older, _live, _soon])["id"] == "v1")
check("viewers parse to an int", youtube.concurrent_viewers(_live) == 2413)
check("likes come off the statistics part",
      youtube.likes({"statistics": {"likeCount": "482"}}) == 482)
check("hidden likes are None, not zero", youtube.likes({"statistics": {}}) is None)
check("likes ride along on the same call, costing nothing extra",
      "statistics" in open(os.path.join(root, "twitchmetrics/youtube.py")).read()
      .split("VIDEOS_URL, {")[1].split("}")[0]
      and youtube.UNITS_PER_SAMPLE == 3)
check("a missing viewer count is None, not zero",
      youtube.concurrent_viewers(_soon) is None)
check("state defaults to none", youtube.broadcast_state({}) == "none")

check("youtube channels get separate data files",
      config.youtube_csv("a") != config.youtube_csv("b"))
check("a youtube file never collides with a twitch one",
      config.youtube_csv("x") != config.metrics_csv("x"))
check("hostile youtube names stay inside data/",
      os.path.dirname(os.path.abspath(config.youtube_csv("../../etc/passwd")))
      == os.path.abspath(config.DATA_DIR))

_saved_ch = os.environ.pop("YOUTUBE_CHANNEL", None)
with no_env_file():
    check("youtube channel falls back to the default",
          config.resolve_youtube_channel(None) == config.DEFAULT_YOUTUBE_CHANNEL)
    check("the built-in default names nobody real",
          config.DEFAULT_YOUTUBE_CHANNEL == "testchannel"
          and config.DEFAULT_CHANNEL == "testchannel")
os.environ["YOUTUBE_CHANNEL"] = "@somebody"
check("YOUTUBE_CHANNEL is honoured, @ stripped",
      config.resolve_youtube_channel(None) == "somebody")
check("the argument beats the env var",
      config.resolve_youtube_channel("@other") == "other")
os.environ.pop("YOUTUBE_CHANNEL", None)
if _saved_ch is not None:
    os.environ["YOUTUBE_CHANNEL"] = _saved_ch

check("one sample costs three units", youtube.UNITS_PER_SAMPLE == 3)
check("the minimum interval keeps a day inside the quota",
      (86400 // config.MIN_YOUTUBE_INTERVAL_SECONDS) * youtube.UNITS_PER_SAMPLE
      <= config.YOUTUBE_DAILY_QUOTA)
check("the default interval leaves room for a second channel",
      2 * (86400 // config.DEFAULT_YOUTUBE_INTERVAL_SECONDS) * youtube.UNITS_PER_SAMPLE
      <= config.YOUTUBE_DAILY_QUOTA)

_saved_yi = os.environ.pop("YOUTUBE_INTERVAL", None)
_saved_ti = os.environ.pop("TWITCH_INTERVAL", None)
with no_env_file():
    check("youtube defaults to 60", config.resolve_youtube_interval(None) == 60)
os.environ["TWITCH_INTERVAL"] = "300"
with no_env_file():
    check("TWITCH_INTERVAL does not leak into the youtube budget",
          config.resolve_youtube_interval(None) == 60)
    check("twitch still reads its own", config.resolve_interval(None) == 300)
os.environ["YOUTUBE_INTERVAL"] = "180"
check("YOUTUBE_INTERVAL is honoured", config.resolve_youtube_interval(None) == 180)
check("--interval beats the env var", config.resolve_youtube_interval(600) == 600)
for _bad in ("thirty", "30", "-1", "1.5"):
    os.environ["YOUTUBE_INTERVAL"] = _bad
    try:
        config.resolve_youtube_interval(None)
        check("youtube rejects {!r}".format(_bad), False, "accepted it")
    except SystemExit:
        check("youtube rejects {!r}".format(_bad), True)
os.environ["YOUTUBE_INTERVAL"] = ""
with no_env_file():
    check("an emptied value falls back to the default",
          config.resolve_youtube_interval(None) == 60)
for _name, _saved in (("YOUTUBE_INTERVAL", _saved_yi), ("TWITCH_INTERVAL", _saved_ti)):
    os.environ.pop(_name, None)
    if _saved is not None:
        os.environ[_name] = _saved
check("search.list is not the default discovery route",
      "search_live_video" not in
      open(os.path.join(root, "twitchmetrics/youtube.py")).read().split(
          "def search_live_video")[0].split("def find_live_video")[1])

# The command's refusals, which are what keep a typo from burning the quota.
with tempfile.TemporaryDirectory() as _tmp:
    _empty = os.path.join(_tmp, ".env")
    open(_empty, "w").close()
    _env = dict(os.environ, TWITCH_DATA_DIR=_tmp, TWITCH_CHARTS_DIR=_tmp,
                TWITCH_ENV_FILE=_empty)
    _env.pop("YOUTUBE_API_KEY", None)
    _env.pop("YOUTUBE_INTERVAL", None)

    def _yt_run(argv, env=_env):
        return subprocess.run([sys.executable, "-m", "twitchmetrics", "youtube"] + argv,
                              cwd=root, capture_output=True, text=True, env=env)

    _r = _yt_run(["--help"])
    check("youtube --help works", _r.returncode == 0)
    _r = _yt_run(["--once"])
    check("a missing key is named",
          _r.returncode != 0 and "YOUTUBE_API_KEY" in (_r.stdout + _r.stderr),
          (_r.stdout + _r.stderr).strip()[-80:])
    _r = _yt_run(["--interval", "30"])
    check("an interval under the quota floor is refused",
          _r.returncode != 0 and "60 seconds" in (_r.stdout + _r.stderr))
    _r = _yt_run(["--interval", "300", "--search"])
    check("--search at poll speed is refused, before the key is even read",
          _r.returncode != 0 and "YOUTUBE_API_KEY" not in (_r.stdout + _r.stderr),
          (_r.stdout + _r.stderr).strip()[-80:])
    _r = _yt_run(["--interval", "900", "--search"])
    check("--search at a survivable interval gets as far as the key",
          "YOUTUBE_API_KEY" in (_r.stdout + _r.stderr))

check("youtube listed as a subcommand",
      "youtube" in subprocess.run([sys.executable, "-m", "twitchmetrics", "--help"],
                                  cwd=root, capture_output=True, text=True).stdout)

# --- widening an existing CSV ---------------------------------------------
section("adding a column to a file already on disk")
_narrow = storage.YOUTUBE_HEADER[:-1]          # the shape before like_count
with tempfile.TemporaryDirectory() as _tmp:
    _path = os.path.join(_tmp, "youtube_old.csv")
    storage.write_all(_path, _narrow,
                      [["2026-08-24T12:00:00Z", "true", "381", "17000",
                        "Old row", "2026-08-24T11:56:33Z", "beuPG6ZohtQ"]])
    storage.append_row(_path, storage.YOUTUBE_HEADER,
                       ["2026-08-24T12:01:00Z", "true", "395", "17000",
                        "New row", "2026-08-24T11:56:33Z", "beuPG6ZohtQ", "142"])
    _rows = storage.read_samples(_path)
    check("the old row survives", len(_rows) == 2, "got %d" % len(_rows))
    check("the old row keeps its values", _rows[0]["viewers"] == 381)
    check("the old row is blank in the new column", _rows[0]["likes"] is None)
    check("the new row's like count is readable", _rows[1]["likes"] == 142)
    check("the header on disk was widened",
          open(_path).readline().strip().endswith("like_count"))

    # A file of a different format must not be mangled into this one.
    _other = os.path.join(_tmp, "metrics_x.csv")
    storage.write_all(_other, storage.METRICS_HEADER, [])
    _before = open(_other).read()
    storage.append_row(_other, storage.METRICS_HEADER,
                       ["2026-08-24T12:00:00Z", "false", "", "10", "", "", "", "", ""])
    check("a different format keeps its own header",
          open(_other).readline() == _before.splitlines(True)[0])

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
check("the youtube unit is a per-channel template too",
      "%i" in open(os.path.join(root, "deploy/youtube-metrics@.service")).read())
for _mod in ("commands/poll.py", "commands/youtube_cmd.py"):
    check("{} runs on the shared loop".format(_mod),
          "runloop.loop" in open(os.path.join(root, "twitchmetrics", _mod)).read())

# The whole project's promise is that there is nothing to install.
import re as _re  # noqa: E402
_forbidden = _re.compile(
    r"^\s*(import|from)\s+"
    r"(google|googleapiclient|google_auth\w*|requests|httplib2|oauth2client|matplotlib)\b",
    _re.M)
for _mod in ("youtube.py", "runloop.py", "commands/youtube_cmd.py"):
    _src = open(os.path.join(root, "twitchmetrics", _mod)).read()
    check("{} imports nothing third-party".format(_mod), not _forbidden.search(_src))

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
                  "youtube-metrics@alpha.service", "youtube-metrics@gamma.service",
                  "unrelated.service", "twitch-metrics@.service"):
        open(os.path.join(_wants, _name), "w").close()
    check("finds enabled instances of both pollers",
          daily.units_in(_wants) == ["alpha", "beta", "gamma"],
          str(daily.units_in(_wants)))
    check("a channel polled on both platforms is one channel, not two",
          daily.units_in(_wants).count("alpha") == 1)
    check("ignores unrelated units", "unrelated" not in daily.units_in(_wants))
    check("ignores the bare template", "" not in daily.units_in(_wants))
    _found, _source = daily.discover_channels(wants_dirs=[_wants])
    check("discovery finds them", _found == ["alpha", "beta", "gamma"], str(_found))
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
with tempfile.TemporaryDirectory() as tmp:
    import shutil as _shutil
    # One channel polled on both platforms: the Twitch fixture's live day is
    # 2026-08-19, the YouTube fixture's is 2026-08-21, so each date exercises
    # "one platform has data, the other doesn't".
    _shutil.copy(BREAKS, os.path.join(tmp, "metrics_breaktest.csv"))
    _shutil.copy(YOUTUBE, os.path.join(tmp, "youtube_breaktest.csv"))
    _env = dict(os.environ, TWITCH_DATA_DIR=tmp, TWITCH_CHARTS_DIR=tmp,
                TWITCH_SYSTEMD_WANTS_DIR=os.path.join(tmp, "none"),
                TWITCH_ENV_FILE=os.path.join(tmp, ".env"))
    for _k in ("TWITCH_DAILY_CHANNELS", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        _env.pop(_k, None)

    def _daily(argv, env=_env):
        return subprocess.run([sys.executable, "-m", "twitchmetrics", "daily"] + argv,
                              cwd=root, capture_output=True, text=True, env=env)

    _r = _daily(["breaktest", "--date", "2026-08-21", "--dry-run"])
    check("daily --dry-run exits 0 when a platform has data", _r.returncode == 0,
          (_r.stderr or _r.stdout).strip()[-200:])
    check("it renders the YouTube day",
          os.path.exists(os.path.join(tmp, "chart_breaktest_youtube_2026-08-21.svg")))
    check("charts are named per platform, so the two can't overwrite each other",
          "chart_breaktest_twitch_2026-08-21.svg"
          != "chart_breaktest_youtube_2026-08-21.svg")
    check("no PNG is written — the website serves SVG",
          not glob.glob(os.path.join(tmp, "*.png")))
    check("no .part file is left behind", not glob.glob(os.path.join(tmp, "*.part")))
    check("--dry-run needs no AWS credentials",
          "AWS_ACCESS_KEY_ID" not in (_r.stdout + _r.stderr))
    check("--dry-run publishes nothing", "s3-website" not in (_r.stdout + _r.stderr))
    check("the summary is the last line", "stop " in _r.stdout.strip().splitlines()[-1])
    check("it logs to data/daily.log", os.path.exists(os.path.join(tmp, "daily.log")))

    _r = _daily(["breaktest", "--date", "2026-08-19", "--dry-run"])
    check("the other platform's day renders too",
          os.path.exists(os.path.join(tmp, "chart_breaktest_twitch_2026-08-19.svg")))

    # A channel with no CSV for one platform at all: not applicable, not broken.
    _shutil.copy(YOUTUBE, os.path.join(tmp, "youtube_ytonly.csv"))
    _r = _daily(["ytonly", "--date", "2026-08-21", "--dry-run"])
    check("a channel polled on one platform only still exits 0", _r.returncode == 0,
          (_r.stderr or _r.stdout).strip()[-200:])
    check("and says nothing at all about the platform it isn't polled on",
          "WARN" not in _r.stdout, _r.stdout.strip()[-200:])
    check("backfilling a day one platform has no rows for is a skip, not a fault",
          "nothing to backfill" in _r.stdout or "WARN" not in _r.stdout)

    _r = _daily(["breaktest", "nosuchchannel", "--date", "2026-08-21", "--dry-run"])
    check("one bad channel makes it exit 1", _r.returncode == 1)
    check("the good channel still ran", "breaktest" in _r.stdout)
    check("and the failure is named", "nosuchchannel" in _r.stdout)

# --- daily: preflight fails loudly ----------------------------------------
section("daily preflight")
with tempfile.TemporaryDirectory() as tmp:
    import shutil as _shutil2
    _shutil2.copy(YOUTUBE, os.path.join(tmp, "youtube_breaktest.csv"))
    # sys.executable is absolute, so emptying PATH hides rsvg-convert only.
    # It used to be a hard requirement; publishing SVG means it is now nothing
    # to do with the report, and this proves it.
    _bare = dict(os.environ, PATH=tmp, TWITCH_DATA_DIR=tmp, TWITCH_CHARTS_DIR=tmp,
                 TWITCH_ENV_FILE=os.path.join(tmp, ".env"),
                 TWITCH_SYSTEMD_WANTS_DIR=os.path.join(tmp, "none"))
    _bare.pop("TWITCH_DAILY_CHANNELS", None)
    _r = subprocess.run([sys.executable, "-m", "twitchmetrics", "daily", "breaktest",
                         "--date", "2026-08-21", "--dry-run"],
                        cwd=root, capture_output=True, text=True, env=_bare)
    check("a missing rsvg-convert no longer matters", _r.returncode == 0,
          (_r.stderr or _r.stdout).strip()[-200:])
    check("and nothing mentions it", "librsvg" not in (_r.stdout + _r.stderr))
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
    check("--list-channels needs no credentials and no boto3",
          "Traceback" not in _r.stderr, _r.stderr.strip()[-200:])

# --- cross-platform chart -------------------------------------------------
section("cross-platform chart")
_t0 = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def _pts(start_min, count, value, every=1):
    return [(_t0 + timedelta(minutes=start_min + i * every), value) for i in range(count)]


# Twitch from 12:00, YouTube joining at 12:10 — the real shape of a day where
# one poller was started later than the other.
_series = [dict(chart.PLATFORMS[0], points=_pts(0, 30, 50)),
           dict(chart.PLATFORMS[1], points=_pts(10, 20, 500))]
_grid, _vals, _comb = chart.align_platforms(_series)
check("the grid spans both platforms", len(_grid) == 30, str(len(_grid)))
check("a platform with no data yet reads unknown, not zero",
      _vals["youtube"][0] is None)
check("and the total waits for it", _comb[0] is None)
check("the total appears once both are known", _comb[10] == 550)
check("peak combined is the sum, not either alone",
      max(v for v in _comb if v is not None) == 550)

# A genuine zero must survive: a stream that just went live has no viewers yet.
_zero = [dict(chart.PLATFORMS[0], points=[(_t0, 0), (_t0 + timedelta(minutes=1), 7),
                                          (_t0 + timedelta(minutes=2), 9)])]
check("a real zero is kept, not treated as missing",
      chart.align_platforms(_zero)[1]["twitch"][0] == 0)

# A poller sampling off the minute must not punch holes in its own curve.
_ragged = [(_t0 + timedelta(minutes=i, seconds=13 * (i % 3)), 100) for i in range(20)]
_col = chart.align_platforms([dict(chart.PLATFORMS[0], points=_ragged)])[1]["twitch"]
check("off-the-minute sampling doesn't create gaps",
      all(v == 100 for v in _col), str(_col[:6]))

# But a real outage does break the line rather than bridging it.
_gapped = _pts(0, 5, 40) + _pts(60, 5, 40)
_g_grid, _g_vals, _ = chart.align_platforms([dict(chart.PLATFORMS[0], points=_gapped)])
check("an hour-long hole is not bridged",
      any(v is None for v in _g_vals["twitch"]))
check("and the line is drawn as two runs",
      len(chart._runs(_g_grid, _g_vals["twitch"])) == 2)

_svg = chart.render_platforms(_series, "testchannel", date(2026, 8, 24))
check("the chart renders", _svg.startswith("<svg") and _svg.endswith("</svg>"))
check("it names both platforms", "Twitch" in _svg and "YouTube" in _svg)
check("it shows the combined peak", "550" in _svg and "Peak combined" in _svg)
check("one platform alone gets no combined line",
      "Combined" not in chart.render_platforms(_series[:1], "t", date(2026, 8, 24)))
check("a single sample is not a chart",
      chart.render_platforms([dict(chart.PLATFORMS[0], points=_pts(0, 1, 5))],
                             "t", date(2026, 8, 24)) is None)
check("no data at all is not a chart",
      chart.render_platforms([], "t", date(2026, 8, 24)) is None)
check("the combined key sorts to the top of the page",
      s3.PLATFORMS[0] == "combined")
check("and it parses like any other chart key",
      s3.parse_key("combined/2026-08-24.svg") == ("combined", "2026-08-24"))
_cross_page = s3.render_index("t", date(2026, 8, 24),
                              {"2026-08-24": ["combined", "twitch", "youtube"]})
check("the page leads with the combined chart",
      _cross_page.index("combined/2026-08-24.svg") < _cross_page.index("twitch/2026-08-24.svg"))
check("and marks it as the lead panel", 'class="panel lead"' in _cross_page)

# --- multi-day charts -----------------------------------------------------
section("trends")


def _day_samples(day, hour, minutes, viewers):
    """Live samples every minute from `hour` local on `day`."""
    base = datetime.combine(day, datetime.min.time()).replace(hour=hour).astimezone()
    return [{"when": (base + timedelta(minutes=i)).astimezone(timezone.utc),
             "live": True, "viewers": viewers(i), "title": "", "game": "",
             "stream_id": day.isoformat()}
            for i in range(minutes)]


_end = date(2026, 8, 25)
# Streamed today and three days ago, nothing in between.
_history = (_day_samples(_end, 19, 120, lambda i: 100 + i)
            + _day_samples(_end - timedelta(days=3), 19, 120, lambda i: 300 + i))
_peaks = trends.daily_peaks(_history, _end, 10)
check("a ten-day window has ten entries", len(_peaks) == 10)
check("oldest first, ending today",
      _peaks[0]["day"] == _end - timedelta(days=9) and _peaks[-1]["day"] == _end)
check("a day never streamed has no peak, not a zero",
      _peaks[-2]["peak"] is None)
check("a day that streamed carries its highest count",
      _peaks[-1]["peak"] == 219 and _peaks[-4]["peak"] == 419)
check("and when it happened", _peaks[-1]["at"] is not None)

# The whole reason this isn't chart.bucket_averages(): two streams that started
# an hour apart must still line their 8pm samples up in the same block.
_late = _day_samples(_end, 20, 60, lambda i: 10)
_early = _day_samples(_end - timedelta(days=1), 19, 120, lambda i: 10)
_late_slots = trends.clock_buckets(_late, _end)
_early_slots = trends.clock_buckets(_early, _end - timedelta(days=1))
check("buckets are keyed to the clock, not to when the stream began",
      set(_late_slots) == {40, 41} and {40, 41} <= set(_early_slots))
check("a slot's value is that half hour's average",
      _late_slots[40] == 10)
check("and a day off has no slots at all",
      trends.clock_buckets(_history, _end - timedelta(days=1)) == {})

_slots, _per_day, _dropped = trends.compare_slots(_history, _end, 5)
check("the comparison covers today and five days back", len(_per_day) == 6)
check("oldest first, today last", _per_day[-1][0] == _end)
check("every day in the window gets an entry, data or not",
      sum(1 for _, buckets in _per_day if buckets) == 2)
check("a short evening is not trimmed", _dropped == 0)

_long = _day_samples(_end, 4, 20 * 60, lambda i: 10 + (500 if 9 * 60 < i < 14 * 60 else 0))
_kept, _, _lost = trends.compare_slots(_long, _end, 5)
check("a twenty-hour day is trimmed to half a day of slots",
      len(_kept) == trends.MAX_SLOTS and _lost == 40 - trends.MAX_SLOTS)
check("and what is kept is contiguous, not the fullest slots scattered about",
      _kept == list(range(_kept[0], _kept[0] + len(_kept))))

_peaks_svg = trends.render_peaks(_peaks, "testchannel", "twitch", _end)
check("the peaks chart renders",
      _peaks_svg.startswith("<svg") and _peaks_svg.endswith("</svg>"))
check("it names the best day and its figure",
      "Best day" in _peaks_svg and "419" in _peaks_svg)
check("a day with no stream is drawn as absent, not as nobody watching",
      trends.DASH in _peaks_svg)
check("one day of history is still a chart",
      trends.render_peaks(trends.daily_peaks(_day_samples(_end, 19, 60, lambda i: 5),
                                             _end, 10), "t", "twitch", _end) is not None)
check("no history at all is not a chart",
      trends.render_peaks(trends.daily_peaks([], _end, 10), "t", "twitch", _end) is None)

_typical_svg = trends.render_typical(_slots, _per_day, "testchannel", "twitch", _end)
check("the comparison chart renders",
      _typical_svg.startswith("<svg") and _typical_svg.endswith("</svg>"))
check("today is called today", ">Today<" in _typical_svg)
check("only the days that drew a bar are in the legend",
      ">{}<".format(trends.fmt_day(_end - timedelta(days=3))) in _typical_svg
      and ">{}<".format(trends.fmt_day(_end - timedelta(days=1))) not in _typical_svg)
check("no data at all is not a chart",
      trends.render_typical(*trends.compare_slots([], _end, 5)[:2],
                            "t", "twitch", _end) is None)
check("a platform with nothing gets no charts at all",
      trends.render_all([], "t", "twitch", _end) == {})
check("and one with history gets both",
      sorted(trends.render_all(_history, "t", "twitch", _end)) == ["peaks", "typical"])
check("the channel name is escaped into the chart",
      "&lt;b&gt;" in trends.render_peaks(_peaks, "<b>", "twitch", _end))

# --- s3 naming ------------------------------------------------------------
section("s3 naming")
check("a bucket name carries the shared prefix",
      s3.bucket_name("testchannel").startswith(config.BUCKET_PREFIX))
check("no underscore survives — a bucket name is a DNS label",
      "_" not in s3.bucket_slug("some_channel_name"))
check("a slug can't start or end with a hyphen",
      not s3.bucket_slug("_weird_").startswith("-")
      and not s3.bucket_slug("_weird_").endswith("-"))
check("runs of hyphens collapse", "--" not in s3.bucket_slug("a___b___c"))
check("a hostile channel can't escape into a path",
      "/" not in s3.bucket_slug("../../etc/passwd")
      and ".." not in s3.bucket_slug("../../etc/passwd"))
check("an empty slug still yields a usable name", s3.bucket_slug("///") == "channel")
check("names stay inside the 63-character limit",
      len(s3.bucket_name("x" * 200)) <= s3.MAX_BUCKET_NAME,
      str(len(s3.bucket_name("x" * 200))))
check("a long name is still prefixed and suffixed correctly",
      s3.bucket_name("x" * 200).startswith("tm-")
      and len(s3.bucket_name("x" * 200).rsplit("-", 1)[1]) == s3.SUFFIX_BYTES * 2)
check("two calls give different buckets — the namespace is global",
      s3.bucket_name("same") != s3.bucket_name("same"))
check("IGN and ign share one bucket slug",
      s3.bucket_slug("IGN") == s3.bucket_slug("ign"))

check("a chart key is platform/date.svg",
      s3.object_key("twitch", date(2026, 8, 24)) == "twitch/2026-08-24.svg")
check("the date component can't contain a slash",
      "/" not in s3.object_key("twitch", date(2026, 8, 24)).split("/", 1)[1])
check("a key round-trips through parse_key",
      s3.parse_key(s3.object_key("youtube", date(2026, 8, 24)))
      == ("youtube", "2026-08-24"))
check("index.html is not mistaken for a chart", s3.parse_key("index.html") is None)
check("nor is a stray upload", s3.parse_key("twitch/notes.txt") is None)
check("nor a nearly-right key", s3.parse_key("twitch/2026-8-4.svg") is None)

# The dash/dot split is frozen history, so it is worth pinning every one.
for _region in ("us-east-1", "us-west-1", "us-west-2", "eu-west-1", "ap-southeast-1",
                "ap-southeast-2", "ap-northeast-1", "sa-east-1", "us-gov-west-1"):
    check("{} uses the dash endpoint".format(_region),
          s3.website_url("b", _region) == "http://b.s3-website-{}.amazonaws.com".format(_region))
for _region in ("us-east-2", "eu-west-2", "eu-central-1", "ca-central-1", "ap-south-1"):
    check("{} uses the dot endpoint".format(_region),
          s3.website_url("b", _region) == "http://b.s3-website.{}.amazonaws.com".format(_region))
check("website endpoints are http — S3 does not serve TLS on them",
      s3.website_url("b", "us-east-1").startswith("http://"))

check("the read policy grants exactly GetObject",
      s3.public_read_policy("b")["Statement"][0]["Action"] == ["s3:GetObject"])
check("and only inside that bucket",
      s3.public_read_policy("b")["Statement"][0]["Resource"] == ["arn:aws:s3:::b/*"])
check("the policy is JSON-serialisable as-is",
      "PublicReadGetObject" in json.dumps(s3.public_read_policy("b")))

# --- s3 the page ----------------------------------------------------------
section("s3 index page")
_days = {"2026-08-24": ["twitch", "youtube"],
         "2026-08-23": ["youtube"],
         "2026-08-22": ["twitch", "youtube"]}
_page = s3.render_index("testchannel", date(2026, 8, 24), _days)
check("the page is a whole document",
      _page.startswith("<!doctype html>") and _page.rstrip().endswith("</html>"))
check("the channel is the heading", "<h1>testchannel</h1>" in _page)
check("today's charts are displayed", _page.count("<img") == 2)
check("today's twitch chart by relative key", 'src="twitch/2026-08-24.svg"' in _page)
check("today's youtube chart too", 'src="youtube/2026-08-24.svg"' in _page)
check("past days are links, not images",
      'href="youtube/2026-08-23.svg"' in _page
      and 'src="youtube/2026-08-23.svg"' not in _page)
check("every past day is linked", _page.count("<li>") == 2)
check("today is not repeated in the list", "<li><span>2026-08-24" not in _page)
check("newest past day comes first",
      _page.index("2026-08-23") < _page.index("2026-08-22"))
check("the page is self-contained — no external asset",
      "http://" not in _page.replace("http://www.w3.org", "") and "https://" not in _page)
check("no script anywhere", "<script" not in _page.lower())
check("a channel name is escaped",
      "&lt;script&gt;" in s3.render_index("<script>", date(2026, 8, 24), {}))
_empty_page = s3.render_index("x", date(2026, 8, 24), {})
check("a day with no charts says so rather than showing a broken image",
      "<img" not in _empty_page and "No graph for" in _empty_page)
check("and the first day says the list is empty",
      "first day" in s3.render_index("x", date(2026, 8, 24),
                                     {"2026-08-24": ["twitch"]}))
check("the CSS survived not being run through str.format",
      "{ color-scheme: dark; }" in _page)

_tw = "LIVE: Hollywood Studios - Rides & More!"
_yt = "LIVE: Slinky Dog Dash & MORE!"
_titled = s3.render_index("t", date(2026, 8, 24), _days, {"twitch": _tw, "youtube": _yt})
check("the stream title is shown", 'class="stream"' in _titled)
check("Twitch's title wins when they differ",
      "Hollywood" in _titled and "Slinky" not in _titled)
check("it sits with the date, above the charts",
      _titled.index("stream") < _titled.index("<img"))
check("YouTube is the fallback, not a second line",
      "Slinky" in s3.render_index("t", date(2026, 8, 24), _days, {"youtube": _yt}))
check("only one title line ever",
      s3.render_index("t", date(2026, 8, 24), _days,
                      {"twitch": _tw, "youtube": _yt}).count('class="stream"') == 1)
check("a title is escaped, not injected",
      "&amp;" in _titled and "<b>" not in
      s3.render_index("t", date(2026, 8, 24), _days, {"twitch": "<b>x</b>"}))
check("no titles means no empty line", 'class="stream"' not in _page)
check("a blank title is not a title",
      'class="stream"' not in s3.render_index("t", date(2026, 8, 24), _days,
                                              {"twitch": "   "}))
check("titles live in the bucket so --publish-index still renders them",
      "TITLES_KEY" in open(os.path.join(root, "twitchmetrics/s3.py")).read())
check("an unreadable titles.json is not fatal",
      "return {}" in open(os.path.join(root, "twitchmetrics/s3.py")).read()
      .split("def load_titles")[1].split("def save_titles")[0])

# --- s3 trends page -------------------------------------------------------
section("s3 trends page")
check("a trend chart has a fixed key, no date in it",
      s3.trend_key("peaks", "twitch") == "trends/peaks-twitch.svg")
check("and it parses back", s3.parse_trend_key("trends/peaks-twitch.svg")
      == ("peaks", "twitch"))
# The whole reason the filename is not a date: the index is built by matching
# keys, and a trend chart mistaken for a platform would put a "trends" panel on
# the front page and a nonsense row in Past days.
check("the index's key matcher ignores a trend chart",
      all(s3.parse_key(s3.trend_key(kind, platform)) is None
          for kind in s3.TREND_KINDS for platform in ("twitch", "youtube")))
check("and the trend matcher ignores a day's chart",
      s3.parse_trend_key("twitch/2026-08-24.svg") is None)
check("as it does the page itself", s3.parse_trend_key(s3.TRENDS_KEY) is None)

_trends_page = s3.render_trends("testchannel", date(2026, 8, 25),
                                [("peaks", "twitch"), ("typical", "youtube")])
check("the trends page is a whole document",
      _trends_page.startswith("<!doctype html>")
      and _trends_page.rstrip().endswith("</html>"))
check("it shows a panel per chart the bucket holds",
      _trends_page.count("<img") == 2)
check("by relative key", 'src="trends/peaks-twitch.svg"' in _trends_page)
check("it never links a chart that isn't there",
      "typical-twitch" not in _trends_page)
check("it links back to today", 'href="index.html"' in _trends_page)
check("it has no Past days section — every chart on it already spans days",
      "Past days" not in _trends_page)
check("the channel name is escaped",
      "&lt;script&gt;" in s3.render_trends("<script>", date(2026, 8, 25), []))
check("an empty bucket says so rather than showing broken images",
      "<img" not in s3.render_trends("t", date(2026, 8, 25), [])
      and "No trend charts yet" in s3.render_trends("t", date(2026, 8, 25), []))
check("it is self-contained — no external asset",
      "http://" not in _trends_page.replace("http://www.w3.org", ""))
check("no script anywhere", "<script" not in _trends_page.lower())

check("the front page links to it once the charts exist",
      'href="trends.html"' in s3.render_index("t", date(2026, 8, 24), _days, trends=True))
check("and does not, before they do",
      "trends.html" not in s3.render_index("t", date(2026, 8, 24), _days))

# --- s3 the bucket registry -----------------------------------------------
section("s3 bucket registry")
check("the registry sits in data/ with the other state",
      os.path.dirname(os.path.abspath(config.S3_BUCKETS_PATH))
      == os.path.abspath(config.DATA_DIR))
_real_buckets = config.S3_BUCKETS_PATH
with tempfile.TemporaryDirectory() as tmp:
    config.S3_BUCKETS_PATH = os.path.join(tmp, ".s3_buckets.json")
    check("an absent registry reads as empty", s3._load_buckets() == {})
    check("an unknown channel has no bucket", s3.bucket_for("nobody") is None)
    s3.remember("IGN", "tm-ign-abc123", "us-east-1")
    check("a bucket round-trips", s3.bucket_for("ign")["bucket"] == "tm-ign-abc123")
    check("lookup is case-insensitive, like the CSVs",
          s3.bucket_for("IGN") == s3.bucket_for("ign"))
    check("the registry is written 0600",
          stat.S_IMODE(os.stat(config.S3_BUCKETS_PATH).st_mode) == 0o600)
    check("no temp file is left behind",
          not [f for f in os.listdir(tmp) if ".tmp" in f])
    check("target_path names the real bucket",
          s3.target_path("ign", date(2026, 8, 24))
          == "tm-ign-abc123/twitch/2026-08-24.svg")
    with open(config.S3_BUCKETS_PATH, "w") as _h:
        _h.write("{")
    check("a half-written registry reads as empty, not a crash", s3._load_buckets() == {})
    check("and require_bucket exits with the command to run",
          raises(SystemExit, s3.require_bucket, "ign"))
config.S3_BUCKETS_PATH = _real_buckets

# --- s3 retries -----------------------------------------------------------
section("s3 retries")
check("throttling is retried", s3._is_retryable(200, "SlowDown"))
check("a 503 is retried", s3._is_retryable(503, ""))
check("a 500 is retried", s3._is_retryable(500, "InternalError"))
check("a clock skew is retried", s3._is_retryable(200, "RequestTimeTooSkewed"))
check("AccessDenied is not retried", not s3._is_retryable(403, "AccessDenied"))
check("a missing bucket is not retried", not s3._is_retryable(404, "NoSuchBucket"))
check("an invalid region is not retried",
      not s3._is_retryable(400, "InvalidLocationConstraint"))
_waits = [retry.retry_after(i) for i in range(retry.MAX_ATTEMPTS)]
check("backoff grows", _waits == sorted(_waits), str(_waits))
check("backoff stays bounded", max(_waits) < 20, str(_waits))


class _FakeClientError(Exception):
    def __init__(self, code, status):
        super().__init__(code)
        self.response = {"Error": {"Code": code},
                         "ResponseMetadata": {"HTTPStatusCode": status}}


check("an error code is read off a botocore-shaped exception",
      s3._error_code(_FakeClientError("SlowDown", 503)) == "SlowDown")
check("and the status with it", s3._status(_FakeClientError("SlowDown", 503)) == 503)
check("a plain exception has no code", s3._error_code(ValueError("nope")) == "")
check("an unrecognised exception is not classified, so it is re-raised",
      s3._classify(ValueError("nope")) is None)
check("a throttle is classified as retryable",
      s3._classify(_FakeClientError("SlowDown", 503)) == (True, None))
check("a permission error is classified as final",
      s3._classify(_FakeClientError("AccessDenied", 403)) == (False, None))
check("an unclassifiable error escapes with_backoff untouched",
      raises(ValueError, retry.with_backoff, "x",
             lambda: (_ for _ in ()).throw(ValueError("nope")), s3._classify))
check("a final error is raised, not retried four times",
      raises(_FakeClientError, retry.with_backoff, "x",
             lambda: (_ for _ in ()).throw(_FakeClientError("AccessDenied", 403)),
             s3._classify))
check("a call that works is simply returned", retry.with_backoff("x", lambda: 7) == 7)
check("giving up is its own exception type", issubclass(retry.GaveUp, RuntimeError))
check("s3 translates it so daily can catch one type",
      issubclass(s3.S3Error, Exception) and "GaveUp" in
      open(os.path.join(root, "twitchmetrics/s3.py")).read())

# --- boto3 stays optional -------------------------------------------------
section("boto3 stays optional")
_s3_src = open(os.path.join(root, "twitchmetrics/s3.py")).read()
check("boto3 is imported inside a function, never at module scope",
      "\nimport boto3" not in _s3_src and "    import boto3" in _s3_src)
check("the failure names the fix", "pip install boto3" in _s3_src)
_imports_boto3 = re.compile(r"^\s*(import|from)\s+boto3\b", re.M)
for _mod in ("commands/daily.py", "commands/s3_cmd.py", "cli.py"):
    check("{} never imports boto3 itself".format(_mod),
          not _imports_boto3.search(
              open(os.path.join(root, "twitchmetrics", _mod)).read()))
check("only s3.py imports it at all",
      [m for m in ("s3.py", "commands/daily.py", "commands/s3_cmd.py", "cli.py",
                   "commands/graph_cmd.py", "chart.py")
       if _imports_boto3.search(open(os.path.join(root, "twitchmetrics", m)).read())]
      == ["s3.py"])
check("daily imports s3 lazily too",
      "    from .. import s3" in
      open(os.path.join(root, "twitchmetrics/commands/daily.py")).read())

# Source-text checks can be defeated, so prove it: a boto3.py that refuses to
# import, earlier on the path than any real one, must not break the CLI.
with tempfile.TemporaryDirectory() as tmp:
    with open(os.path.join(tmp, "boto3.py"), "w") as _h:
        _h.write("raise ImportError('boto3 is not installed')\n")
    import shutil as _shutil3
    _shutil3.copy(YOUTUBE, os.path.join(tmp, "youtube_breaktest.csv"))
    _poisoned = dict(os.environ, PYTHONPATH=tmp, TWITCH_DATA_DIR=tmp,
                     TWITCH_CHARTS_DIR=tmp, TWITCH_ENV_FILE=os.path.join(tmp, ".env"),
                     TWITCH_SYSTEMD_WANTS_DIR=os.path.join(tmp, "none"),
                     TWITCH_CLIENT_ID="x", TWITCH_CLIENT_SECRET="y")
    _poisoned.pop("TWITCH_DAILY_CHANNELS", None)
    for _label, _argv in [
        ("the help still works", ["--help"]),
        ("graph still works", ["graph", YOUTUBE, "--output", os.path.join(tmp, "a.svg")]),
        ("youtube --help still works", ["youtube", "--help"]),
        ("poll --once still runs", ["poll", "nochannel", "--once", "--viewers-only"]),
        ("daily --list-channels still works", ["daily", "--list-channels", "breaktest"]),
        ("daily --dry-run still renders", ["daily", "breaktest", "--date", "2026-08-21",
                                           "--dry-run"]),
    ]:
        _r = subprocess.run([sys.executable, "-m", "twitchmetrics"] + _argv,
                            cwd=root, capture_output=True, text=True, env=_poisoned)
        check("without boto3, " + _label, _r.returncode == 0,
              (_r.stderr or _r.stdout).strip()[-200:])
        check("without boto3, " + _label + " — and it isn't asked for",
              "pip install boto3" not in (_r.stdout + _r.stderr))
    # `s3 --list` reports "nothing yet" with exit 1, like daily --list-channels;
    # the claim here is only that boto3 is not what stopped it.
    _r = subprocess.run([sys.executable, "-m", "twitchmetrics", "s3", "--list"],
                        cwd=root, capture_output=True, text=True, env=_poisoned)
    check("without boto3, s3 --list still reads the registry",
          "pip install boto3" not in (_r.stdout + _r.stderr)
          and "Traceback" not in _r.stderr)
    _r = subprocess.run([sys.executable, "-m", "twitchmetrics", "s3", "--setup", "x"],
                        cwd=root, capture_output=True, text=True, env=_poisoned)
    check("but publishing says to install it", _r.returncode != 0
          and "pip install boto3" in (_r.stdout + _r.stderr),
          (_r.stdout + _r.stderr).strip()[-160:])
    check("and doesn't traceback about it", "Traceback" not in _r.stderr)

# --- s3 command -----------------------------------------------------------
section("s3 command")
with tempfile.TemporaryDirectory() as tmp:
    _env = dict(os.environ, TWITCH_DATA_DIR=tmp, TWITCH_CHARTS_DIR=tmp,
                TWITCH_ENV_FILE=os.path.join(tmp, ".env"))
    for _k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        _env.pop(_k, None)
    for _label, _argv, _want_zero in [
        ("--list on an empty registry is not a crash", ["s3", "--list"], False),
        ("--dry-run --setup needs no network", ["s3", "x", "--setup", "--dry-run"], True),
        ("no mode says how to set up", ["s3", "x"], False),
        ("--url without a bucket fails cleanly", ["s3", "x", "--url"], False),
    ]:
        _r = subprocess.run([sys.executable, "-m", "twitchmetrics"] + _argv,
                            cwd=root, capture_output=True, text=True, env=_env)
        check(_label, (_r.returncode == 0) == _want_zero,
              "exit {}: {}".format(_r.returncode, (_r.stderr or _r.stdout).strip()[-140:]))
        check(_label + " — no traceback", "Traceback" not in _r.stderr,
              _r.stderr.strip()[-200:])
    _r = subprocess.run([sys.executable, "-m", "twitchmetrics", "s3", "x", "--url"],
                        cwd=root, capture_output=True, text=True, env=_env)
    check("and points at --setup", "--setup" in (_r.stdout + _r.stderr))

# --- aws configuration ----------------------------------------------------
section("aws configuration")
_saved_aws = {k: os.environ.pop(k, None) for k in
              ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION",
               "AWS_DEFAULT_REGION", "AWS_ACCOUNT_ID")}
_real_env2 = config.ENV_PATH
with tempfile.TemporaryDirectory() as tmp:
    config.ENV_PATH = os.path.join(tmp, ".env")
    check("no keys is not an error — boto3 has its own chain",
          config.load_aws_credentials() == (None, None))
    check("but asking for them insists", raises(SystemExit, config.load_aws_credentials,
                                                True))
    check("the region defaults", config.resolve_aws_region() == config.DEFAULT_AWS_REGION)
    os.environ["AWS_DEFAULT_REGION"] = "eu-west-2"
    check("AWS_DEFAULT_REGION is honoured, as the AWS CLI spells it",
          config.resolve_aws_region() == "eu-west-2")
    os.environ["AWS_REGION"] = "us-west-2"
    check("AWS_REGION wins over it", config.resolve_aws_region() == "us-west-2")
    check("--region wins over both", config.resolve_aws_region("ap-south-1") == "ap-south-1")
    check("no account pin by default", config.expected_aws_account() is None)
    os.environ["AWS_ACCOUNT_ID"] = "123456789012"
    check("a pinned account is read back",
          config.expected_aws_account() == "123456789012")
    os.environ["AWS_ACCOUNT_ID"] = "  "
    check("a blank pin means no pin", config.expected_aws_account() is None)
    os.environ.pop("AWS_ACCOUNT_ID", None)
    check("the mismatch message names both accounts",
          "{got}" in s3.WRONG_ACCOUNT and "{want}" in s3.WRONG_ACCOUNT)
    check("preflight checks the pin before anything is created",
          open(os.path.join(root, "twitchmetrics/s3.py")).read()
          .split("def preflight")[1].split("def ")[0].count("WRONG_ACCOUNT") == 1)
    os.environ["AWS_ACCESS_KEY_ID"] = "AKIAEXAMPLE"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "secret"
    check("keys are read from the environment",
          config.load_aws_credentials(required=True) == ("AKIAEXAMPLE", "secret"))
config.ENV_PATH = _real_env2
for _k, _v in _saved_aws.items():
    os.environ.pop(_k, None)
    if _v is not None:
        os.environ[_k] = _v

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
    # Drive is retired: unregistered from the CLI, but the module is kept and
    # must still import cleanly, so it can be brought back with one line and
    # cannot rot unnoticed in the meantime.
    _r = subprocess.run([sys.executable, "-m", "twitchmetrics", "drive", "--status"],
                        cwd=root, capture_output=True, text=True, env=_env)
    check("the drive subcommand is retired", _r.returncode != 0)
    check("and argparse says so rather than crashing", "Traceback" not in _r.stderr)
    check("drive is not offered in the help",
          "drive " not in subprocess.run(
              [sys.executable, "-m", "twitchmetrics", "--help"],
              cwd=root, capture_output=True, text=True).stdout)
    check("but drive.py still imports", hasattr(drive, "upload_chart"))
    check("and driveoauth.py with it", hasattr(driveoauth, "drive_token"))
    check("drive.py says it is retired and how to restore it",
          "RETIRED" in (drive.__doc__ or "") and "cli.py" in (drive.__doc__ or ""))
    check("png.py is kept too, though nothing calls it",
          hasattr(png, "to_png") and "png.require_converter" not in
          open(os.path.join(root, "twitchmetrics/commands/daily.py")).read())

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
check("it no longer demands rsvg-convert", "librsvg2-bin" not in _svc)
check("nor a font package", "fonts-dejavu" not in _svc)
check("it names what publishing does need", "boto3" in _svc)
check("and where the AWS keys come from", "AWS_ACCESS_KEY_ID" in _svc)

# --- no dependencies ------------------------------------------------------
section("no dependencies")
_forbidden = re.compile(
    r"^\s*(import|from)\s+"
    r"(google|googleapiclient|google_auth\w*|requests|httplib2|oauth2client|matplotlib)\b",
    re.M)
for _mod in ("driveoauth.py", "drive.py", "png.py", "trends.py",
             "commands/drive_cmd.py", "commands/daily.py"):
    _src = open(os.path.join(root, "twitchmetrics", _mod)).read()
    check("{} imports nothing third-party".format(_mod), not _forbidden.search(_src))

print("\n{} passed, {} failed".format(passed, failed))
sys.exit(1 if failed else 0)
