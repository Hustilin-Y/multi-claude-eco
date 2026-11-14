"""The chat add-in's files: the outbox `tg` writes, the client ids already taken, and reading a
transcript or the outbox in bounded windows.

The machine has little memory and a transcript grows without end (one once reached 240 MB), so
nothing here reads a whole file: a window of at most WINDOW bytes from where it should end, line by
line, skipping any line longer than LINE_MAX (a pasted picture) without holding it."""
import json
import os
import re
import time

from multiclaude import jsonfile, paths

CHAT = paths.STATE / "chat"
OUTBOX = CHAT / "outbox"     # <name>.jsonl: what that person's assistant sent with tg, and <name>.jsonl.1 before it
SEEN = CHAT / "seen"         # <name>: the Mini App messages taken, "<time> <client id> <msg id>" a line
ROTATE_AT = 5 * 1024 * 1024  # an outbox this big becomes <name>.jsonl.1 (one old file kept)
SEEN_FOR = 24 * 3600         # a client id taken this long ago is forgotten
LINE_MAX = 256 * 1024        # a longer line (a pasted picture, a huge tool result) is skipped unread
CHUNK = 64 * 1024
TRANSCRIPT_WINDOW = 3 * 1024 * 1024 - LINE_MAX  # bytes of transcript one backfill or history request reads
OUTBOX_WINDOW = 1024 * 1024 - LINE_MAX          # and of outbox: together never over 4 MB (SPEC N1a)
CLIENT_ID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

opener = open  # how files are opened for reading; the tests count what is read through it


# --- reading in windows ---

def size_of(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def lines(path, start, end=None):
    """(offset, next offset, line) for each whole line that starts in [start, end) of path (to its
    end when end is None), start moved to the next line's start unless a line starts there. A line
    longer than LINE_MAX comes as None, unread."""
    try:
        f = opener(path, "rb")
    except OSError:
        return
    with f:
        if start > 0:
            f.seek(start - 1)
            if f.read(1) != b"\n":
                start = skip_line(f, start, end)
        f.seek(start)
        at, parts, length = start, [], 0  # where the line being read starts, what's kept of it, its length
        while end is None or at < end:
            chunk = f.read(CHUNK)
            if not chunk:
                return  # a last line without its newline is still being written
            pos = 0
            while True:
                newline = chunk.find(b"\n", pos)
                piece = chunk[pos:] if newline < 0 else chunk[pos:newline]
                length += len(piece)
                if length <= LINE_MAX:
                    parts.append(piece)
                elif parts:
                    parts = []  # too long: let it go
                if newline < 0:
                    break
                yield at, at + length + 1, b"".join(parts) if length <= LINE_MAX else None
                at += length + 1
                parts, length = [], 0
                if end is not None and at >= end:
                    return
                pos = newline + 1


def skip_line(f, start, end=None):
    """The offset after the line that start falls in; end at most, so a window is never read past."""
    f.seek(start)
    while end is None or start < end:
        chunk = f.read(CHUNK if end is None else min(CHUNK, end - start))
        if not chunk:
            return start
        newline = chunk.find(b"\n")
        if newline >= 0:
            return start + newline + 1
        start += len(chunk)
    return start


def entries(path, start, end=None):
    """(offset, next offset, entry) for each line, as lines() reads them; entry is None for a line
    that is too long or isn't a JSON object."""
    for offset, after, line in lines(path, start, end):
        try:
            entry = json.loads(line) if line is not None else None
        except ValueError:
            entry = None
        yield offset, after, entry if isinstance(entry, dict) else None


def window_start(end, window):
    return max(0, end - window)


# --- the outbox ---

def outbox(name):
    return OUTBOX / f"{name}.jsonl"


def rotated(name):
    return OUTBOX / f"{name}.jsonl.1"


def append_outbox(name, record):
    """Add what name's assistant sent to their outbox, which becomes the old one at ROTATE_AT."""
    path = outbox(name)
    with jsonfile.locked(OUTBOX / f"{name}.lock", wait=5):
        if size_of(path) >= ROTATE_AT:
            os.replace(path, rotated(name))
        with open(path, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def empty_outbox(name):
    """Forget everything name's assistant sent: their conversation was cleared."""
    with jsonfile.locked(OUTBOX / f"{name}.lock", wait=5):
        for path in (outbox(name), rotated(name)):
            path.unlink(missing_ok=True)


def outbox_records_since(name, since):
    """The outbox records from since (a Unix time) on, oldest first, read from the end: at most
    OUTBOX_WINDOW bytes of the outbox and, when that reaches its start, of the old one."""
    found, budget = [], OUTBOX_WINDOW
    for path in (outbox(name), rotated(name)):
        size = size_of(path)
        start = window_start(size, budget)
        part = [record for _, _, record in entries(path, start) if record and float(record.get("at") or 0) >= since]
        found = part + found
        budget -= size - start
        if start > 0 or budget <= 0:
            break
    return found


# --- Mini App messages already taken ---

def seen_messages(name, now=None):
    """[(time, client id, msg id)] of the Mini App messages taken from name in the last SEEN_FOR."""
    now = time.time() if now is None else now
    found = []
    try:
        text = (SEEN / name).read_text()
    except OSError:
        return found
    for line in text.splitlines():
        parts = line.split()
        try:
            at = float(parts[0])
        except (IndexError, ValueError):
            continue
        if now - at <= SEEN_FOR and len(parts) >= 2:
            found.append((at, parts[1], parts[2] if len(parts) > 2 else ""))
    return found


def take_message(name, client_id, now):
    """Note client_id as taken from name; False when it was already (a page's retry)."""
    with jsonfile.locked(SEEN / f".{name}.lock", wait=5):
        kept = seen_messages(name, now)
        if any(client == client_id for _, client, _ in kept):
            return False
        write_seen(name, [*kept, (now, client_id, "")])
    return True


def note_message_id(name, client_id, msg_id):
    """Note the Telegram id of the 💬 copy of a Mini App message, so its client id is known again."""
    with jsonfile.locked(SEEN / f".{name}.lock", wait=5):
        write_seen(name, [(at, client, msg_id if client == client_id else msg)
                          for at, client, msg in seen_messages(name)])


def write_seen(name, kept):
    jsonfile.write_atomically(SEEN / name, "".join(f"{at:.3f} {client} {msg}".rstrip() + "\n"
                                                   for at, client, msg in kept))


def client_ids(name):
    """{Telegram msg id of the 💬 copy: client id} of name's recent Mini App messages."""
    return {msg: client for _, client, msg in seen_messages(name) if msg}
