"""Command line entry point.

Invoked either as the installed `twitch-metrics` console script or as
`python3 -m twitchmetrics`. Help text and hints report whichever was used, so
they never suggest a command the reader doesn't have.
"""

import argparse
import sys

from . import __version__, config
from .commands import (auth_cmd, chatters, followers, graph_cmd, poll, setup_cmd,
                       testdata_cmd, users)

COMMANDS = [
    ("setup", setup_cmd, "register credentials and verify them"),
    ("auth", auth_cmd, "browser login for endpoints needing a user token"),
    ("poll", poll, "record viewers, followers and chat size on an interval"),
    ("graph", graph_cmd, "render collected samples as an SVG chart"),
    ("users", users, "account details for one or more logins"),
    ("followers", followers, "follower count, list, and follow checks"),
    ("chatters", chatters, "how many accounts are joined to chat"),
    ("testdata", testdata_cmd, "write synthetic samples for working on charts"),
]

EPILOG = """
examples:
  {prog} setup
  {prog} poll themeparkgiant
  {prog} graph themeparkgiant --date today
  {prog} followers themeparkgiant --recent 10
  {prog} chatters themeparkgiant

Every command takes the channel as its first argument, falling back to
TWITCH_CHANNEL in .env and then the built-in default.
"""


def build_parser():
    prog = config.invocation()
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Poll and chart Twitch channel metrics. Standard library only.",
        epilog=EPILOG.format(prog=prog),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version",
                        version="twitch-metrics {}".format(__version__))
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for name, module, help_text in COMMANDS:
        sub = subparsers.add_parser(
            name, help=help_text, description=module.__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter)
        module.add_arguments(sub)
        sub.set_defaults(_run=module.run)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "_run", None):
        parser.print_help()
        return 1
    return args._run(args) or 0


if __name__ == "__main__":
    sys.exit(main())
