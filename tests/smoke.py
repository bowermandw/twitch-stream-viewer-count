#!/usr/bin/env python3
"""Offline checks for the parts that don't need credentials.

Run from the project root:   python3 tests/smoke.py

Everything here works against the committed fixtures, so it needs no network,
no .env and no tokens. It won't catch API contract changes — only regressions
in parsing, session detection, chart building and CLI wiring.
"""

import os
import subprocess
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from twitchmetrics import chart, config, storage, youtube  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
PLAIN = os.path.join(FIXTURES, "metrics_testchannel.csv")
BREAKS = os.path.join(FIXTURES, "metrics_breaktest.csv")
VIEWERS = os.path.join(FIXTURES, "viewers_testchannel.csv")
YOUTUBE = os.path.join(FIXTURES, "youtube_testchannel.csv")

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

_yt_sessions = chart.split_sessions(_yt)
check("one broadcast in the youtube fixture", len(_yt_sessions) == 1,
      "got %d" % len(_yt_sessions))
_yt_metrics = chart.available_metrics(_yt_sessions[0])
check("viewers and subscribers detected, nothing else",
      [m["key"] for m in _yt_metrics] == ["viewers", "subscribers"],
      str([m["key"] for m in _yt_metrics]))
_yt_svg = chart.render_stacked(_yt_sessions[0], "testchannel", 10, 1300)
check("youtube svg is well-formed",
      _yt_svg.startswith("<svg") and _yt_svg.endswith("</svg>"))
check("youtube svg names both series",
      "Concurrent viewers" in _yt_svg and "Subscribers" in _yt_svg)
check("subscribers get a non-zero-based axis",
      chart.METRIC_BY_KEY["subscribers"]["zero_based"] is False)

from twitchmetrics.commands import graph_cmd as _graph  # noqa: E402
check("graph recovers the channel name from a youtube path",
      _graph.pick_source("data/youtube_foo.csv", False)[1] == "foo")

# channel_filter is the whole "handle or id" decision, and it is pure.
check("a bare handle becomes forHandle",
      youtube.channel_filter("themeparkgiant") == {"forHandle": "@themeparkgiant"})
check("a pasted @handle is the same thing",
      youtube.channel_filter("@themeparkgiant") == {"forHandle": "@themeparkgiant"})
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
check("youtube channel falls back to the default",
      config.resolve_youtube_channel(None) == config.DEFAULT_YOUTUBE_CHANNEL)
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
check("youtube defaults to 60", config.resolve_youtube_interval(None) == 60)
os.environ["TWITCH_INTERVAL"] = "300"
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

print("\n{} passed, {} failed".format(passed, failed))
sys.exit(1 if failed else 0)
