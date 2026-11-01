"""The reminders a person's assistant queued to add on their phone, kept in
snapshot.FOLDER/<telegram id>.outbox.json until a sync of their Reminders shortcut takes them:
{"queued": [...], "delivered": [...]}. Each is {"id", "list", "title", "notes", "due", "queued"}, and
once a sync's answer took it, it moves to the last DELIVERED_KEPT delivered with "delivered", the
Unix time of that sync. While a sync's answer carries one, it stays queued, marked "sending" (a Unix
time), so a router stopped mid-sync loses none. The assistant (through `assistant reminders`) and the
router edit the file at once, so every edit holds its lock."""
import secrets
import time

from multiclaude import jsonfile
from multiclaude.addons.reminders import snapshot

DELIVERED_KEPT = 50
SENT_FIELDS = ("id", "list", "title", "notes", "due")  # what the shortcut gets of each
SENDING_STALE_AFTER = 5 * 60  # far past the relay's 10 s wait: the router that marked it has stopped


def path(chat_id):
    return snapshot.FOLDER / f"{chat_id}.outbox.json"


def edit(chat_id, change):
    """Apply change({"queued", "delivered"}) to the person's outbox under its lock; what change returns."""
    outcome = []

    def apply(data):
        data.setdefault("queued", [])
        data.setdefault("delivered", [])
        outcome.append(change(data))

    with jsonfile.locked(path(chat_id).with_suffix(".lock")):
        jsonfile.edit_object(path(chat_id), apply, create=False)
    return outcome[0]


def read(chat_id):
    """{"queued", "delivered"}, empty before anything was queued."""
    data = jsonfile.read_json(path(chat_id), {})
    data = data if isinstance(data, dict) else {}
    return {"queued": data.get("queued") or [], "delivered": data.get("delivered") or []}


def add(chat_id, title, list_name, notes="", due=""):
    """Queue a reminder for the person's phone; its id."""
    queued = {"id": secrets.token_hex(3), "list": list_name, "title": title, "notes": notes, "due": due,
              "queued": int(time.time())}
    edit(chat_id, lambda data: data["queued"].append(queued))
    return queued["id"]


def cancel(chat_id, reminder_id):
    """Take a queued reminder out; the reminder, or None if none waits with that id."""
    def take_out(data):
        found = next((r for r in data["queued"] if r.get("id") == reminder_id), None)
        if found:
            data["queued"].remove(found)
        return found

    return edit(chat_id, take_out)


def take(chat_id):
    """The queued reminders no other sync's answer is carrying, marked "sending" for this one's."""
    now = time.time()

    def mark_free(data):
        free = [r for r in data["queued"] if now - r.get("sending", 0) > SENDING_STALE_AFTER]
        for reminder in free:
            reminder["sending"] = int(now)
        return free

    return edit(chat_id, mark_free)


def put_back(chat_id, taken):
    """Reminders a sync's answer didn't deliver after all: unmarked, for the next sync."""
    ids = ids_of(taken)

    def unmark(data):
        for reminder in data["queued"]:
            if reminder.get("id") in ids:
                reminder.pop("sending", None)

    edit(chat_id, unmark)


def delivered(chat_id, taken, when):
    """Move reminders a sync's answer at when (a Unix time) delivered from the queue to the log."""
    ids = ids_of(taken)

    def move(data):
        data["queued"] = [r for r in data["queued"] if r.get("id") not in ids]
        went = [{**unmarked(r), "delivered": int(when)} for r in taken]
        data["delivered"] = (data["delivered"] + went)[-DELIVERED_KEPT:]

    edit(chat_id, move)


def ids_of(reminders):
    return {r.get("id") for r in reminders}


def unmarked(reminder):
    return {field: value for field, value in reminder.items() if field != "sending"}


def for_shortcut(taken):
    """Taken reminders as the shortcut gets them."""
    return [{field: r.get(field, "") for field in SENT_FIELDS} for r in taken]
