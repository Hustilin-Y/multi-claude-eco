#!/usr/bin/env python3
"""Adds and removes a person's jobs in the shared crontab (lib/multiclaude/cronjobs.py), one edit at a
time, never touching anyone else's lines, and keeping the crontab as it was in state/crontab/.
`assistant cron` runs it.

Usage: cron.py add NAME PURPOSE LINE [LINE...]
       cron.py remove NAME TEXT
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from multiclaude import cronjobs, users  # noqa: E402


def main(args):
    command, *rest = args or [""]
    try:
        if command == "add" and len(rest) >= 3:
            name, purpose, *lines = rest
            cronjobs.add_job(known(name), purpose, lines)
        elif command == "remove" and len(rest) == 2 and rest[1]:
            name, text = rest
            if not cronjobs.remove_job(known(name), text):
                print(f"error: no job of {name}'s holds {text!r}", file=sys.stderr)
                return 1
        else:
            print(__doc__.strip(), file=sys.stderr)
            return 2
    except (cronjobs.CrontabError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def known(name):
    """name, when it's someone in users.json or the bot's own lines."""
    if name != cronjobs.BOT_LINES and name not in users.load_users():
        raise cronjobs.CrontabError(f"{name} isn't anyone in users.json")
    return name


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
