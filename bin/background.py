#!/usr/bin/env python3
"""Lists a program an assistant runs in the background, so its person's /stop ends it too
(lib/multiclaude/background.py). `assistant background` runs it.

Usage: background.py NAME PID WHAT
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from multiclaude import background  # noqa: E402


def main(args):
    if len(args) != 3 or not args[1].isdigit():
        print(__doc__.strip(), file=sys.stderr)
        return 2
    name, pid, what = args
    if not background.add(name, int(pid), what):
        print(f"error: no process {pid} is running", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
