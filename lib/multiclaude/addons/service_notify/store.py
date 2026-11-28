"""What the service-notify add-in keeps on disk: each service's key (as its hash), and per service
the presses waiting for it, the messages it sent and its dedupe keys; and, for every service, the
messages whose buttons still work and when they expire. Everything is read and written under a
lock, since the HTTP API's threads and the router's loop both use it."""
import json
import os
import re

from multiclaude import jsonfile, keyring, paths

NAME_RE = re.compile(r"[a-z0-9-]{1,32}")
ROOT = paths.STATE / "service-notify"
KEYS = keyring.KeyRing(paths.STATE / "service-keys.json")
LIVE = ROOT / "live.json"
LIVE_LOCK = ROOT / "live.lock"
SENT_FOR = 30 * 86400    # how long a service may edit or delete what it sent
DEDUPE_FOR = 7 * 86400   # how long a dedupe key stands
STALE_AFTER = 120        # a press not handed over by then is dropped


def private_root():
    """state/service-notify readable by its user only: presses carry the services' button ids."""
    ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(ROOT, 0o700)


def new_key(name):
    """A token for a service that has none; only its hash is kept. Never silently in place of
    one it has: whoever holds the new token gets the service's presses."""
    if KEYS.has_key(checked(name)):
        raise ValueError(f"{name} has a token already; --replace makes a new one, which cuts {name} off until it has it")
    return KEYS.new_key(name)


def replace_key(name):
    """A fresh token for the service, replacing the one it had."""
    return KEYS.new_key(checked(name))


def checked(name):
    if not NAME_RE.fullmatch(name or ""):
        raise ValueError(f"a service name is 1 to 32 of a-z, 0-9 and -, not {name!r}")
    return name


def revoke(name):
    return KEYS.revoke(name)


def names():
    """The services that have a key."""
    return sorted(set(jsonfile.read_object(KEYS.path).values()))


def service_of(token):
    """The service whose token this is, or None."""
    return KEYS.owner(keyring.digest(token)) if token else None


class Service:
    """One service's presses, sent messages and dedupe keys."""

    def __init__(self, name):
        if not NAME_RE.fullmatch(name or ""):
            raise ValueError(f"not a service name: {name!r}")
        self.name = name
        private_root()
        self.dir = ROOT / name
        self.lock = self.dir / "presses.lock"
        self.presses = self.dir / "presses.jsonl"
        self.seq = self.dir / "seq"
        self.sent = self.dir / "sent.json"
        self.dedupe = self.dir / "dedupe.json"
        self.state_lock = self.dir / "state.lock"

    # --- presses: kept until handed over, or dropped once stale ---

    def _read_presses(self):
        try:
            lines = self.presses.read_text().splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in lines if line.strip()]

    def add_press(self, press, now):
        """Queue a press for the service; its number."""
        with jsonfile.locked(self.lock):
            try:
                seq = int(self.seq.read_text().strip() or 0) + 1
            except (FileNotFoundError, ValueError):
                seq = 1
            jsonfile.write_atomically(self.seq, f"{seq}\n")
            jsonfile.write_lines(self.presses, [*self._read_presses(), {**press, "seq": seq, "queued": now}])
        return seq

    def take_presses(self, now, after=STALE_AFTER):
        """The presses waiting, oldest first, which leave the queue as they're returned. One that
        waited longer than after seconds is never handed over: it stays for drop_stale, which
        tells the owner nothing was done."""
        with jsonfile.locked(self.lock):
            presses = self._read_presses()
            fresh = [p for p in presses if now - p.get("queued", 0) <= after]
            if fresh:
                jsonfile.write_lines(self.presses, [p for p in presses if p not in fresh])
        return fresh

    def drop_presses_for(self, chat, msg_id):
        """Drop the presses waiting on one message (its buttons expired); how many."""
        with jsonfile.locked(self.lock):
            presses = self._read_presses()
            kept = [p for p in presses if (str(p.get("chat")), p.get("message_id")) != (str(chat), int(msg_id))]
            if len(kept) != len(presses):
                jsonfile.write_lines(self.presses, kept)
        return len(presses) - len(kept)

    def drop_stale(self, now, after=STALE_AFTER):
        """Drop and return the presses that waited longer than after seconds."""
        with jsonfile.locked(self.lock):
            presses = self._read_presses()
            stale = [p for p in presses if now - p.get("queued", 0) > after]
            if stale:
                jsonfile.write_lines(self.presses, [p for p in presses if p not in stale])
        return stale

    def has_presses(self, now=None, after=STALE_AFTER):
        """Whether presses wait; with now, only those take_presses(now) would hand over."""
        with jsonfile.locked(self.lock):
            return any(now is None or now - p.get("queued", 0) <= after for p in self._read_presses())

    # --- what it sent, and its dedupe keys ---

    def _edit(self, path, change):
        with jsonfile.locked(self.state_lock):
            jsonfile.edit_object(path, change)

    def remember_sent(self, msg_id, chat, now):
        def add(sent):
            for old in [m for m, entry in sent.items() if now - entry.get("at", 0) > SENT_FOR]:
                del sent[old]
            sent[str(msg_id)] = {"chat": str(chat), "at": now}
        self._edit(self.sent, add)

    def forget_sent(self, msg_id):
        self._edit(self.sent, lambda sent: sent.pop(str(msg_id), None))

    def chat_of(self, msg_id):
        """The chat of a message this service sent, or None if it sent none by that id."""
        entry = read_object(self.sent).get(str(msg_id))
        return entry.get("chat") if isinstance(entry, dict) else None

    def dedupe_get(self, key, now):
        entry = read_object(self.dedupe).get(key)
        if isinstance(entry, dict) and now - entry.get("at", 0) <= DEDUPE_FOR:
            return entry.get("message_id")
        return None

    def dedupe_put(self, key, msg_id, now):
        def add(keys):
            for old in [k for k, entry in keys.items() if now - entry.get("at", 0) > DEDUPE_FOR]:
                del keys[old]
            keys[key] = {"message_id": msg_id, "at": now}
        self._edit(self.dedupe, add)


def read_object(path):
    """A JSON object from a file; {} when it's missing or holds anything else."""
    data = jsonfile.read_json(path, {})
    return data if isinstance(data, dict) else {}


# --- the messages whose buttons still work, for every service ---

def _live_key(chat, msg_id):
    return f"{chat}:{msg_id}"


def _live_pairs(live):
    return [(key.rsplit(":", 1)[0], int(key.rsplit(":", 1)[1])) for key in live]


def live_add(chat, msg_id, expires):
    with jsonfile.locked(LIVE_LOCK):
        jsonfile.edit_object(LIVE, lambda live: live.__setitem__(_live_key(chat, msg_id), expires))


def live_remove(chat, msg_id):
    with jsonfile.locked(LIVE_LOCK):
        jsonfile.edit_object(LIVE, lambda live: live.pop(_live_key(chat, msg_id), None))


def live_due(now):
    """(chat, message id) of each message whose buttons are past their expiry."""
    live = jsonfile.read_json(LIVE, {})
    return _live_pairs([key for key, expires in live.items() if expires < now])


def live_all():
    return _live_pairs(jsonfile.read_json(LIVE, {}))
