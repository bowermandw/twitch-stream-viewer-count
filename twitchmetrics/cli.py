"""Command line entry point.

Invoked either as the installed `twitch-metrics` console script or as
`python3 -m twitchmetrics`. Help text and hints report whichever was used, so
they never suggest a command the reader doesn't have.
"""

import argparse
import sys

from . import __version__, config
from .commands import (auth_cmd, chatters, daily, db_cmd, followers, graph_cmd,
                       poll, s3_cmd, setup_cmd, testdata_cmd, users, youtube_cmd)

COMMANDS = [
    ("setup", setup_cmd, "register credentials and verify them"),
    ("auth", auth_cmd, "browser login for endpoints needing a user token"),
    ("poll", poll, "record viewers, followers and chat size on an interval"),
    ("youtube", youtube_cmd, "record YouTube live viewers and subscriber count"),
    ("graph", graph_cmd, "render collected samples as an SVG chart"),
    ("daily", daily, "chart every polled channel and publish to its website"),
    ("s3", s3_cmd, "create and inspect a channel's S3 static website"),
    ("users", users, "account details for one or more logins"),
    ("followers", followers, "follower count, list, and follow checks"),
    ("chatters", chatters, "how many accounts are joined to chat"),
    ("testdata", testdata_cmd, "write synthetic samples for working on charts"),
    ("db", db_cmd, "create the sample database and report on it"),
]

EPILOG = """
examples:
  {prog} setup
  {prog} poll testchannel
  {prog} youtube testchannel --once
  {prog} graph testchannel --date today
  {prog} daily --dry-run
  {prog} s3 --setup testchannel
  {prog} db --status
  {prog} followers testchannel --recent 10
  {prog} chatters testchannel

Every command takes the channel as its first argument, falling back to
TWITCH_CHANNEL in .env and then the built-in default — except `youtube`, which
uses YOUTUBE_CHANNEL because a YouTube handle need not match the Twitch login.
"""


def build_parser():
    prog = config.invocation()
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Poll and chart Twitch and YouTube channel metrics. "
                    "Collecting needs nothing installed.",
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
