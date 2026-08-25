"""The sampling loop the pollers share.

Both the Twitch and the YouTube poller want the same three behaviours: samples
that land on wall-clock boundaries instead of drifting, a `systemctl stop` that
finishes the current sample and exits rather than dying mid-write, and a closing
line saying how many samples were taken. Only the sampling differs, so that is
the one thing passed in.
"""

import signal
import threading
from datetime import datetime, timedelta

from .logging import log

# Set by SIGTERM/SIGHUP so the loop can finish the current sample and exit
# cleanly. An Event rather than a flag because it also interrupts the sleep —
# otherwise `systemctl stop` would wait out the whole interval and then SIGKILL.
_stop = threading.Event()


def install_stop_handlers():
    """Treat a service-manager stop like Ctrl-C. Returns the event it sets."""
    def handler(signum, _frame):
        _stop.set()
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (OSError, ValueError):
            pass  # not the main thread, or unsupported on this platform
    return _stop


def seconds_until_next_tick(interval):
    """Sleep to the next wall-clock boundary so samples don't drift."""
    now = datetime.now()
    secs = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1e6
    return interval - (secs % interval)


def loop(interval, sample, label, destination):
    """Call `sample` every `interval` seconds until stopped. Always returns 0.

    `sample` takes no arguments and returns True when it wrote a row, so a
    failed request is logged by the caller and simply not counted here.

    `destination` is a phrase saying where the samples go, already formatted by
    the caller. It used to be the CSV path, which this shortened to a basename;
    now that samples go to a database with the CSV underneath it as a spool,
    "where they go" is a sentence rather than a filename.
    """
    _stop.clear()
    install_stop_handlers()

    log("start    polling {} every {}s -> {}".format(label, interval, destination))
    log("start    Ctrl-C or SIGTERM to stop")

    samples = 0
    reason = "stopped"
    try:
        while not _stop.is_set():
            if sample():
                samples += 1
            if _stop.is_set():
                break
            delay = seconds_until_next_tick(interval)
            log("sleep    next poll at {}".format(
                (datetime.now() + timedelta(seconds=delay)).strftime("%H:%M:%S")))
            if _stop.wait(delay):  # returns early when asked to stop
                break
        if _stop.is_set():
            reason = "signalled"
    except KeyboardInterrupt:
        print()
        reason = "interrupted"

    log("stop     {} after {} sample(s) -> {}".format(reason, samples, destination))
    return 0
