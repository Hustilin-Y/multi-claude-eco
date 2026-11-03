"""Work an assistant runs in the background, such as an OpenCode run, that its person's /stop ends
too: Escape stops only the assistant's turn. A program lists itself with `assistant background PID
WHAT` while it runs. /stop ends what is listed that still runs under the person's live session, so a
scheduled job's run goes on, and the start time kept with each pid keeps a process the kernel has
since given that pid from being ended."""
import contextlib
import json
import os
import signal

from . import paths
from .jsonfile import read_json

# Positions in stat_fields(): fields 4 (the parent's pid) and 22 (the start time) of /proc/<pid>/stat.
PARENT = 1
START = 19
FIRST_PROCESS = 1  # init: no session runs above it


def stat_fields(pid):
    """The fields of /proc/<pid>/stat after the process's name, which may hold spaces, or None once
    the process has ended."""
    try:
        stat = (paths.PROC / str(pid) / "stat").read_text()
    except OSError:
        return None
    return stat[stat.rindex(")") + 1:].split()


def descends_from(pid, ancestor):
    while pid > FIRST_PROCESS:
        if pid == ancestor:
            return True
        fields = stat_fields(pid)
        if fields is None:
            return False
        pid = int(fields[PARENT])
    return False


def listing(name):
    return paths.BACKGROUND / name


def add(name, pid, what):
    """List the running process pid, called what, for name's /stop to end. False if it isn't running."""
    fields = stat_fields(pid)
    if fields is None:
        return False
    still_running(name)  # forgets the ended ones, so the listing stays short
    listing(name).mkdir(parents=True, exist_ok=True)
    (listing(name) / f"{pid}.json").write_text(json.dumps({"what": what, "started": fields[START]}))
    return True


def still_running(name):
    """(pid, what, its file) for each process listed for name that still runs as the one listed;
    the rest are forgotten."""
    running = []
    for entry_file in sorted(listing(name).glob("*.json")):
        entry = read_json(entry_file, {})
        fields = stat_fields(entry_file.stem)
        if fields and fields[START] == entry.get("started"):
            running.append((int(entry_file.stem), entry.get("what", "a program"), entry_file))
        else:
            entry_file.unlink(missing_ok=True)
    return running


def end(name, session_pid):
    """End what name's assistant listed that runs under its live session (whose first process is
    session_pid) with SIGTERM, so each can say it was stopped. Returns what was ended."""
    ended = []
    for pid, what, entry_file in still_running(name):
        if descends_from(pid, session_pid):
            entry_file.unlink(missing_ok=True)
            with contextlib.suppress(ProcessLookupError):  # it ended just now
                os.kill(pid, signal.SIGTERM)
                ended.append(what)
    return ended
