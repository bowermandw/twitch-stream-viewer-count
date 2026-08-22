"""Console + file logging for the pollers.

The active log file is set per run, because each channel and each poller keeps
its own. Commands that aren't polling leave `path` as None and only print.
"""

from datetime import datetime

_path = None


def use_file(path):
    global _path
    _path = path


def log(message):
    stamped = "[{}] {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message)
    print(stamped, flush=True)
    if not _path:
        return
    try:
        with open(_path, "a", encoding="utf-8") as handle:
            handle.write(stamped + "\n")
    except OSError as exc:
        print("(could not write log file: {})".format(exc), flush=True)


def note(message):
    """Console-only, for one-shot commands that shouldn't create a log file."""
    print("  {}".format(message))
