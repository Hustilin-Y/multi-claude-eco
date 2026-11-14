"""A person's chat feed: the events their open Mini App shows, put on the relay as they happen.

Nothing is built for someone whose page is closed. The page's first read of the feed makes the relay
queue a "chat_open" envelope, and so does a later read once a minute; the router then backfills
the last BACKFILL events from the session's current transcript and the outbox (the relay keeps them
in place of what it had), and follows both while a read came in the last WATCH_FOR, putting new
events on the relay at most once a second. Opening, following and the backfill read files only, so
they never wake a sleeping assistant.

Each event gets the next "seq" of the person's own, kept in state/chat/<name>.seq beyond restarts
(/clear doesn't reset it), and "at", the router's time. At most PENDING_MAX wait for the relay:
when it can't be reached the oldest go, and the page, seeing seq jump, reads the feed afresh, which
backfills again."""
import collections
import json
import os
import threading
import time

from multiclaude import bot, config, jsonfile, paths
from multiclaude.addons.chat import events, files
from multiclaude.log import log
from multiclaude.router import relay_entrance, switching, usage

SEQ = paths.STATE / "chat"   # <name>.seq: {"seq": the last given, "watched": when the page last read the feed}
WATCH_FOR = 120              # seconds after the page's last read that the feed is still followed
BACKFILL = 50                # events a page gets when it opens, and per history request
WINDOW_KEPT = 4 * BACKFILL   # the newest events of a window kept while it's read, so memory stays small
PENDING_MAX = 200            # events waiting for the relay; older ones go first (a gap the page fills)
PUT_EVERY = 1.0              # seconds between PUTs while events come
PUT_RETRY = 5.0              # after a PUT that failed
LOOK_EVERY = 2.0             # seconds between looks at the status line (settings, the current transcript)
EXTRAS_EVERY = 30.0          # and at what other add-ins bring to the chat (tabs, models, actions)
RELAY_KEEPS = 24 * 3600      # the relay drops events this old, so after that it holds nothing to clear


def model_alias(model):
    """opus for "Opus 5.5" or "claude-opus-5-5", as /model names it; None for none."""
    return next((m for m in switching.MODELS if m in (model or "").lower()), None)


def current_transcript(user, snap=None, left=()):
    """The session's own transcript: the one its status line names, else the folder's newest. Not
    a job's (`claude -p` in the same folder) or a subagent's, nor one /clear left (left)."""
    snap = usage.snapshot(user) if snap is None else snap
    path = (snap or {}).get("transcript_path")
    if path and path not in left and os.path.exists(path):
        return path
    newest = user.tail.newest()
    return newest if newest not in left else None


class Feed:
    def __init__(self, user):
        self.user = user                  # the person's router Session
        self.lock = threading.RLock()
        saved = jsonfile.read_json(SEQ / f"{user.name}.seq", {})
        saved = saved if isinstance(saved, dict) else {}
        self.seq = int(saved.get("seq") or 0)
        self.saved = (self.seq, float(saved.get("watched") or 0))  # what the seq file holds
        self.watched = self.saved[1]      # when the page last read the feed (so the relay may hold events)
        self.watching_until = 0.0
        self.pending = []                 # events with their seq, waiting for the relay
        self.replace = False              # the next PUT replaces what the relay holds (a backfill)
        self.ready = False                # backfilled, and following the files
        self.backfilling = False
        self.putting = False
        self.next_put = 0.0
        self.transcript = None            # the transcript followed, and how far it's read
        self.transcript_at = 0
        self.left = set()                 # transcripts a /clear left behind, never followed again
        self.outbox_at = 0
        self.steps = events.StepBook()
        self.status = None                # (state, tool) last put
        self.settings = None
        self.next_look = 0.0
        self.next_extras, self.extras = 0.0, {}  # what other add-ins bring (chat.extras)
        self.waking_since = None          # a message came for it asleep: "waking" until its transcript moves

    @property
    def name(self):
        return self.user.name

    # --- being watched ---

    def watching(self, now):
        return now < self.watching_until

    def open(self, now, backfill, later):
        """The page read the feed: follow it for WATCH_FOR more; with backfill (its first read, or a
        gap), build the last events afresh first. later(fn, *args) runs fn in the background."""
        with self.lock:
            self.watching_until = now + WATCH_FOR
            self.watched = now
            if (backfill or not self.ready) and not self.backfilling:
                self.backfilling = True
                later(self.backfill)

    def backfill(self):
        """The last BACKFILL events of the current conversation, in place of what the relay holds."""
        try:
            with self.lock:
                path = current_transcript(self.user, left=self.left)
                built, cursor, read_to = history(self.user, path, files.size_of(path) if path else 0, None)
                self.transcript, self.transcript_at, self.steps = path, read_to, events.StepBook()
                self.outbox_at = files.size_of(files.outbox(self.name))
                self.pending, self.status, self.settings = [], None, None
                now = time.time()
                from multiclaude.addons import chat
                for event in merged(built + chat.backfilled(self.user)):
                    self.add(event, now)
                self.add({"kind": "history", "cursor": cursor}, now)
                self.look(now, force=True)
                self.replace, self.ready, self.next_put = True, True, 0.0
        finally:
            self.backfilling = False

    # --- events ---

    def add(self, event, now=None):
        """Give event the next seq and queue it for the relay."""
        with self.lock:
            self.seq += 1
            self.pending.append({**events.fit(event), "seq": self.seq, "at": events.iso(now or time.time())})
            if len(self.pending) > PENDING_MAX:
                dropped = len(self.pending) - PENDING_MAX
                del self.pending[:dropped]
                log(f"{self.name}: the relay is out of reach; {dropped} chat event(s) dropped, the page fills the gap")

    def tick(self, now, later):
        """From the router's loop: follow the transcript and the outbox, then put what's new on the relay."""
        if not self.watching(now):
            if self.ready:
                with self.lock:
                    self.ready, self.pending = False, []  # the next open backfills again
            return
        if not self.ready or not self.lock.acquire(blocking=False):
            return  # still backfilling
        try:
            self.follow(now)
            self.look(now)
        finally:
            self.lock.release()
        self.flush(now, later)

    def follow(self, now):
        """New transcript entries and outbox records as events."""
        if self.transcript:
            client_ids = None
            for _, after, entry in files.entries(self.transcript, self.transcript_at):
                self.transcript_at = after
                if entry is None:
                    continue
                self.waking_since = None  # it's up
                client_ids = files.client_ids(self.name) if client_ids is None else client_ids
                for event in events.from_entry(entry, self.steps, client_ids):
                    self.add(event, now)
        box = files.outbox(self.name)
        size = files.size_of(box)
        if size < self.outbox_at:
            self.outbox_at = 0  # rotated or emptied
        if size > self.outbox_at:
            names = None
            for _, after, record in files.entries(box, self.outbox_at):
                self.outbox_at = after
                if record:
                    names = chat_names() if names is None else names
                    self.add(events.from_outbox(record, self.user.chat_id, names), now)

    def look(self, now, force=False):
        """The session's status, and every LOOK_EVERY its settings and current transcript."""
        state = self.state()
        if state != self.status:
            self.status = state
            self.add({"kind": "status", "state": state[0], "tool": state[1], "since": events.iso(now)}, now)
        if not force and now < self.next_look:
            return
        self.next_look = now + LOOK_EVERY
        if force or now >= self.next_extras:
            from multiclaude.addons import chat
            self.next_extras, self.extras = now + EXTRAS_EVERY, chat.extras(self.user)
        snap = usage.snapshot(self.user) or {}
        model, effort = usage.model_and_effort(snap)
        _, _, percent = usage.context(snap)
        settings = {"model": model_alias(model), "model_name": model, "effort": effort,
                    "context_pct": None if percent is None else round(percent),
                    "bot": bot.name(), "bot_username": config.read().get("username"), **self.extras}
        if settings != self.settings:
            self.settings = settings
            self.add({"kind": "settings", **settings}, now)
        path = current_transcript(self.user, snap, self.left)
        if path and path != self.transcript:
            log(f"{self.name}: the chat follows {os.path.basename(path)}")
            self.transcript, self.steps = path, events.StepBook()
            self.transcript_at = files.window_start(files.size_of(path), files.TRANSCRIPT_WINDOW)

    def state(self):
        """(state, tool) as the page's status line shows it."""
        user = self.user
        if user.asleep():
            return ("waking" if user.queue else "asleep"), None
        if self.waking_since:
            return "waking", None
        if user.approval:
            return "waiting_approval", None
        if user.turn.busy:
            tool = self.steps.running()
            return ("tool", tool) if tool else ("thinking", None)
        return "idle", None

    def message_came(self, now, wake=True):
        """A Mini App message was queued: follow the feed, and if it's for the assistant (wake) and
        the assistant sleeps, say it's waking until its transcript moves."""
        with self.lock:
            self.watching_until = max(self.watching_until, now + WATCH_FOR)
            if wake and self.user.asleep():
                self.waking_since = now

    def cleared(self, now, later):
        """/clear or /restart: the conversation starts afresh, and what the relay holds of the old one
        goes, at once if it may hold anything for them."""
        with self.lock:
            if self.transcript:
                self.left.add(self.transcript)
            self.pending = [e for e in self.pending if e["kind"] in ("status", "settings")]
            self.add({"kind": "cleared"}, now)
            self.transcript, self.steps, self.outbox_at, self.next_look = None, events.StepBook(), 0, 0.0
            relay_holds = now - self.watched < RELAY_KEEPS
            if not relay_holds:
                self.pending = []
            self.next_put = 0.0
        if relay_holds:
            self.flush(now, later)

    # --- the relay ---

    def flush(self, now, later):
        """Put the waiting events on the relay, in the background, at most every PUT_EVERY."""
        with self.lock:
            if not self.pending or self.putting or now < self.next_put:
                return
            self.putting = True
            batch, replace = list(self.pending), self.replace
            self.keep_seq()
        later(self.put, batch, replace)

    def put(self, batch, replace):
        ok = False
        try:
            ok = push(self.user.chat_id, {"events": batch, "replace": replace})
        finally:
            with self.lock:
                self.putting = False
                if ok:
                    sent = {e["seq"] for e in batch}
                    self.pending = [e for e in self.pending if e["seq"] not in sent]
                    self.replace = self.replace and not replace
                self.next_put = time.time() + (PUT_EVERY if ok else PUT_RETRY)

    def keep_seq(self):
        """Save the seq given last (and when the page last read), before anything with it is put."""
        if (self.seq, self.watched) == self.saved:
            return
        try:
            jsonfile.write_json(SEQ / f"{self.name}.seq", {"seq": self.seq, "watched": self.watched})
            self.saved = (self.seq, self.watched)
        except OSError as e:
            log(f"{self.name}: can't keep the chat's seq: {e}")


def push(chat_id, body):
    """PUT a person's events on the relay; whether they got there."""
    url, token = relay_entrance.address(f"/chat/{chat_id}"), relay_entrance.token()
    if not url or not token:
        return False
    try:
        relay_entrance.put(url, token, json.dumps(body, ensure_ascii=False).encode())
        return True
    except OSError as e:
        log(f"chat: can't put events on the relay: {e}")
        return False


def chat_names():
    """{chat id: display name} of the people on the bot, for messages sent to someone else."""
    from multiclaude import users
    try:
        return {u.chat_id: u.display for u in users.load_users().values()}
    except (OSError, ValueError):
        return {}


def history(user, path, end, before):
    """(events, cursor, where reading stopped) for the conversation in path up to byte end and
    the time before (None: up to now): the last BACKFILL events built from one window of the
    transcript and of the outbox. cursor asks for the ones before them; None at the conversation's
    start. Where reading stopped is past the window's last whole line, where following goes on."""
    if not path:
        return [], None, 0
    steps, built, more = events.StepBook(), collections.deque(maxlen=WINDOW_KEPT), False
    first_ts = first_at = None
    client_ids = files.client_ids(user.name)
    read_to = files.window_start(end, files.TRANSCRIPT_WINDOW)
    for offset, after, entry in files.entries(path, read_to, end):
        first_at = offset if first_at is None else first_at
        read_to = after
        if entry is None:
            continue
        if first_ts is None and entry.get("timestamp"):
            first_ts = events.entry_time(entry)
        for event in events.from_entry(entry, steps, client_ids):
            if before is not None and event["ts"] >= before:
                continue  # shown already
            more = more or len(built) == built.maxlen  # the oldest of this window go: more lie before
            built.append(event)
    built = list(built)
    first_at = read_to if first_at is None else first_at
    since = first_ts if first_ts is not None else (0.0 if first_at == 0 else time.time())
    names = None
    for record in files.outbox_records_since(user.name, since):
        names = chat_names() if names is None else names
        built.append(events.from_outbox(record, user.chat_id, names))
    built = merged(built)
    if before is not None:
        built = [e for e in built if e["ts"] < before]
    shown = built[-BACKFILL:]
    cursor = None
    if shown and (len(built) > BACKFILL or more):
        cursor = {"file": os.path.basename(path), "end": end, "before": shown[0]["ts"]}
    elif first_at > 0:
        cursor = {"file": os.path.basename(path), "end": first_at, "before": shown[0]["ts"] if shown else before}
    return shown, cursor, read_to


def merged(built):
    """Events in the order they happened, each key once as it ended up (a step's result over its
    call, an edit over its message); edits of messages not in view are dropped."""
    by_key, order = {}, []
    for n, event in enumerate(sorted(built, key=lambda e: e.get("ts") or "")):
        key = event.get("key") or f"#{n}"  # an event without a key (an add-in's state) stands alone
        if event["kind"] == "message_edited":
            if key in by_key:
                by_key[key] = {**by_key[key], "markdown": event["markdown"], "copy": event["copy"]}
            continue
        if key not in by_key:
            order.append(key)
        by_key[key] = {**by_key.get(key, {}), **event}
    return [by_key[key] for key in order]


def older(user, cursor, left=()):
    """{events, cursor} for a page scrolled up to cursor (a history request). Only the session's
    current transcript is read, a window at a time."""
    if not isinstance(cursor, dict):
        return {"events": [], "cursor": None}
    path = current_transcript(user, left=left)
    if not path or os.path.basename(path) != str(cursor.get("file") or ""):
        return {"events": [], "cursor": None}  # that conversation was cleared since
    try:
        end = max(0, int(cursor.get("end") or 0))
    except (TypeError, ValueError):
        return {"events": [], "cursor": None}
    before = cursor.get("before") if isinstance(cursor.get("before"), str) else None
    built, next_cursor, _ = history(user, path, min(end, files.size_of(path)), before)
    return {"events": built, "cursor": next_cursor}
