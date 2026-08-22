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

from twitchmetrics import chart, config, storage  # noqa: E402

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

print("\n{} passed, {} failed".format(passed, failed))
sys.exit(1 if failed else 0)
