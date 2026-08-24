"""SVG to PNG via rsvg-convert.

The only thing in this project that needs a binary installed, and only the
daily report needs it — Drive previews PNG properly and mostly offers SVG as a
download. Polling and `graph` still need nothing but Python.
"""

import os
import shutil
import subprocess

CONVERTER = "rsvg-convert"
DEFAULT_WIDTH = 1600  # the width the README has always documented
TIMEOUT_SECONDS = 60

PNG_MAGIC = b"\x89PNG"

MISSING = """{} is not installed, and the daily report writes PNGs.

  apt install librsvg2-bin      # Debian/Ubuntu
  brew install librsvg          # macOS

Only `daily` needs it. Polling and `graph` need nothing installed."""


class ConvertError(RuntimeError):
    """rsvg-convert ran but did not produce a usable PNG."""


def converter_path():
    """Absolute path to rsvg-convert, or None when it isn't installed."""
    return shutil.which(CONVERTER)


def require_converter():
    """converter_path(), or exit naming the package that provides it.

    Called once at startup rather than per file: a typo or a missing package
    should be loud once, the way resolve_interval() reports a bad interval,
    instead of rendering every chart and then failing the same way N times.
    """
    found = converter_path()
    if not found:
        raise SystemExit(MISSING.format(CONVERTER))
    return found


def to_png(svg_path, png_path, width=DEFAULT_WIDTH, converter=None):
    """Convert one SVG, atomically, returning the PNG path."""
    command = [converter or require_converter(), "-w", str(width), svg_path, "-o", ""]
    # Written aside then renamed, so an existing PNG is never a half-written one:
    # if the file is there, it is complete.
    temporary = png_path + ".part"
    command[-1] = temporary
    try:
        # stderr is captured but NOT treated as failure: a headless box under
        # ProtectHome=read-only prints "Fontconfig error: No writable cache
        # directories" while converting perfectly well.
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        _discard(temporary)
        raise ConvertError("{} timed out after {}s on {}".format(
            CONVERTER, TIMEOUT_SECONDS, os.path.basename(svg_path)))
    except OSError as exc:
        _discard(temporary)
        raise ConvertError("could not run {}: {}".format(CONVERTER, exc))

    if result.returncode != 0:
        _discard(temporary)
        raise ConvertError("{} exited {} on {}: {}".format(
            CONVERTER, result.returncode, os.path.basename(svg_path),
            (result.stderr or "").strip().splitlines()[-1:] or "no output"))

    try:
        with open(temporary, "rb") as handle:
            head = handle.read(4)
    except OSError as exc:
        _discard(temporary)
        raise ConvertError("{} wrote nothing readable: {}".format(CONVERTER, exc))

    if head != PNG_MAGIC:
        _discard(temporary)
        raise ConvertError("{} produced something that isn't a PNG ({!r})".format(
            CONVERTER, head))

    os.replace(temporary, png_path)
    return png_path


def _discard(path):
    try:
        os.remove(path)
    except OSError:
        pass
