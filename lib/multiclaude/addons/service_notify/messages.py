"""A service's messages: checking what it asked for, sending, editing and deleting them through
the Bot API, and keeping a message with buttons as a question of kind "service", so a tap on it
comes to this add-in (taps.py) and never to an assistant. A service's button ids never reach
Telegram: each button carries "ask:<n>", as `tg ask` makes them."""
import base64
import binascii
import re
import tempfile
import threading
import time
from collections import defaultdict
from pathlib import Path

from multiclaude import config, jsonfile, paths, questions, telegram, users
from multiclaude.addons.service_notify import store
from multiclaude.log import log

KIND = "service"
MAX_ROW, MAX_BUTTONS = 8, 24
MAX_ID, MAX_LABEL = 128, 64
MAX_FILES = 10
MAX_DEDUPE = 128
DEFAULT_EXPIRES, MAX_EXPIRES = 86400, 7 * 86400
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
BUTTONS_WAIT = 5  # seconds Telegram may ask to wait before the buttons go on, waited for within the send
# One lock per kept message (chat, message id): a tap, an edit and the expiry sweep take it.
MESSAGE_LOCKS = defaultdict(threading.Lock)


class Refused(Exception):
    """What the API answers instead: an HTTP status, an error code and maybe a detail."""

    def __init__(self, status, code, detail=None, retry_after=None, message_id=None):
        super().__init__(f"{status} {code}" + (f": {detail}" if detail else ""))
        self.status, self.code, self.detail, self.retry_after = status, code, detail, retry_after
        self.message_id = message_id  # a message that went out all the same


def bad(detail):
    return Refused(400, "bad_request", detail)


# --- who it goes to ---

def recipient(service, to):
    """The person a message goes to: the owner unless to names someone the owner allowed for the service."""
    people = users.load_users()
    owner = users.owner(people)
    if to is None or (owner and to == owner.name):
        if not owner:
            raise Refused(503, "no_owner", "users.json names no owner")
        return owner
    allowed = (((config.read().get("service_notify") or {}).get("services") or {}).get(service) or {}).get("to") or []
    if not isinstance(to, str) or to not in allowed or to not in people:
        raise Refused(403, "recipient_not_allowed")
    return people[to]


# --- checking a request ---

def check_object(request):
    if not isinstance(request, dict):
        raise bad("the body must be a JSON object")
    return request


def check_text(text):
    if not isinstance(text, str) or not text.strip():
        raise bad("text must be non-empty text")
    if len(text) > telegram.RICH_LIMIT:
        raise bad(f"text is longer than {telegram.RICH_LIMIT} characters")
    return text


def check_buttons(rows):
    """[[{"id", "text"}]], or Refused saying what's wrong."""
    if not isinstance(rows, list) or not all(isinstance(row, list) and row for row in rows):
        raise bad("buttons must be rows: a list of non-empty lists")
    flat = [button for row in rows for button in row]
    if any(len(row) > MAX_ROW for row in rows) or len(flat) > MAX_BUTTONS:
        raise bad(f"at most {MAX_ROW} buttons a row and {MAX_BUTTONS} in all")
    for button in flat:
        if not isinstance(button, dict):
            raise bad("a button is {\"id\", \"text\"}")
        button_id, label = button.get("id"), button.get("text")
        if not isinstance(button_id, str) or not 1 <= len(button_id) <= MAX_ID:
            raise bad(f"a button's id is 1 to {MAX_ID} characters")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= MAX_LABEL:
            raise bad(f"a button's text is 1 to {MAX_LABEL} characters")
    if len({button["id"] for button in flat}) != len(flat):
        raise bad("button ids must differ")
    return [[{"id": b["id"], "text": b["text"]} for b in row] for row in rows]


def check_files(files):
    """[(name, bytes)]."""
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise bad(f"files is a list of at most {MAX_FILES}")
    decoded = []
    for f in files:
        name = f.get("name") if isinstance(f, dict) else None
        if not isinstance(name, str) or not name.isprintable() or Path(name).name != name or name in ("", ".", ".."):
            raise bad("a file's name is a plain file name")
        try:
            data = base64.b64decode(f.get("data") or "", validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise bad(f"{name}: data isn't base64") from None
        decoded.append((name, data))
    return decoded


def check_expires(expires_in):
    if expires_in is None:
        return DEFAULT_EXPIRES
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or not 1 <= expires_in <= MAX_EXPIRES:
        raise bad(f"expires_in is 1 to {MAX_EXPIRES} seconds")
    return expires_in


def check_switch(request, name):
    value = request.get(name)
    if value is not None and not isinstance(value, bool):
        raise bad(f"{name} is true or false")
    return value


# --- building ---

def file_kind(name):
    return "photo" if Path(name).suffix.lower() in PHOTO_SUFFIXES else "document"


def rich(text, media):
    """The rich message for text with media below it: [{"type", "media"}] (an upload or a file id)."""
    markdown = "\n\n".join([telegram.without_highlights(text),
                            *(f"![](tg://{m['type']}?id=m{n})" for n, m in enumerate(media))])
    return {"markdown": markdown, **({"media": [{"id": f"m{n}", "media": m} for n, m in enumerate(media)]}
                                     if media else {})}


GENERATION = 100  # ask:<gen * GENERATION + n>: a set of buttons put on later is told apart (MAX_BUTTONS < it)


def keyboard(rows, gen=0):
    """Telegram's inline keyboard: each button "ask:<gen * 100 + n>", n numbered across the rows.
    gen counts the sets of buttons a message was given, so a tap on a set replaced meanwhile (one
    the owner saw before an edit) is told from a tap on the new one at the same place."""
    numbered, n = [], 0
    for row in rows:
        numbered.append([{"text": b["text"], "callback_data": f"ask:{gen * GENERATION + n + i}"}
                         for i, b in enumerate(row)])
        n += len(row)
    return {"inline_keyboard": numbered}


def tapped_button(data, gen):
    """The button number a tap's callback data names in buttons set gen, or None."""
    value = data[4:] if data.startswith("ask:") else ""
    if not value.isdigit():
        return None
    tap_gen, n = divmod(int(value), GENERATION)
    return n if tap_gen == gen else None


def file_ids(result, kinds):
    """The file id Telegram gave each media block of a sent message, in order; None if it gave none."""
    if not isinstance(result, dict):
        return None
    blocks = (result.get("rich_message") or {}).get("blocks") or []
    ids = []
    for block in blocks:
        if block.get("type") == "photo" and block.get("photo"):
            ids.append(block["photo"][-1].get("file_id"))
        elif block.get("type") in ("document", "video", "audio", "animation") and block.get(block["type"]):
            ids.append(block[block["type"]].get("file_id"))
    return ids if len(ids) == len(kinds) and all(ids) else None


def shown(kept, note=None):
    """The rich message of a kept message, its pictures kept by file id, with a note of the
    router's after its text."""
    text = kept.get("question") or ""
    if note:
        text = f"{text}\n\n{note}"
    return rich(text, [{"type": m["type"], "media": m["file_id"]} for m in kept.get("media") or []])


# --- calling Telegram ---

def call(method, files=None, **params):
    """The Bot API, with its failures as what the service is answered."""
    try:
        return (telegram.uploading_bot() if files else telegram.bot()).call(method, files=files, **params)
    except telegram.Unreachable:
        raise Refused(503, "telegram_unreachable") from None
    except telegram.TelegramError as e:
        if e.retry_after:
            raise Refused(429, "rate_limited", retry_after=int(e.retry_after)) from None
        raise Refused(502, "telegram_refused", str(e)) from None


def kept_path(chat, msg_id):
    return paths.ASKS / str(chat) / f"{msg_id}.json"


def read_kept(chat, msg_id):
    kept = jsonfile.read_json(kept_path(chat, msg_id), {})
    return kept if isinstance(kept, dict) and kept.get("kind") == KIND else None


def keep(chat, msg_id, service, text, rows, once, expires, media, answer=None, gen=0):
    """Keep a message as a question of kind "service" (answer: what was pressed, or why it's over;
    gen: which set of buttons it has, see keyboard)."""
    questions.save(paths.ASKS / str(chat), msg_id, text, [b["text"] for row in rows for b in row], kind=KIND,
                   service=service, chat=str(chat), buttons=rows, once=once, expires=expires, media=media,
                   gen=gen, **({"answer": answer} if answer is not None else {}))
    if rows:
        store.live_add(chat, msg_id, expires)
    else:
        store.live_remove(chat, msg_id)


# Telegram's answers to an edit that mean there's nothing left to take off.
GONE_RE = re.compile(r"message to edit not found|message can't be edited|message is not modified", re.I)


def call_until_gone(method, **params):
    """An edit that takes buttons off; Refused when it has to be tried again, but not when the
    message is gone or already as asked."""
    try:
        call(method, **params)
    except Refused as e:
        if e.code != "telegram_refused" or not GONE_RE.search(e.detail or ""):
            raise


def take_buttons_off(chat, msg_id, note):
    """Edit a kept message to its text and note, with no buttons. Only once Telegram took the
    edit does it stop counting as live, so the sweep tries again: Refused meanwhile."""
    kept = read_kept(chat, msg_id) or {}
    call_until_gone("editMessageText", chat_id=chat, message_id=msg_id, rich_message=shown(kept, note),
                    reply_markup={"inline_keyboard": []})
    store.live_remove(chat, msg_id)


def take_keyboard_off(chat, msg_id):
    """Take the buttons off a message, leaving its text as it is (a pressed one the service may
    have edited already). Refused when it has to be tried again."""
    call_until_gone("editMessageReplyMarkup", chat_id=chat, message_id=msg_id, reply_markup={"inline_keyboard": []})
    store.live_remove(chat, msg_id)


# --- what the API does ---

def send(service, request, now):
    request = check_object(request)
    text = check_text(request.get("text"))
    rows = check_buttons(request["buttons"]) if request.get("buttons") is not None else []
    files = check_files(request["files"]) if request.get("files") is not None else []
    expires = now + check_expires(request.get("expires_in"))
    silent, once = check_switch(request, "silent"), check_switch(request, "once")
    dedupe = request.get("dedupe")
    if dedupe is not None and (not isinstance(dedupe, str) or not 1 <= len(dedupe) <= MAX_DEDUPE):
        raise bad(f"dedupe is 1 to {MAX_DEDUPE} characters")
    person = recipient(service, request.get("to"))
    kept_for = store.Service(service)
    if dedupe and (earlier := kept_for.dedupe_get(dedupe, now)) is not None:
        return {"message_id": earlier, "duplicate": True}

    kinds = [file_kind(name) for name, _ in files]
    with tempfile.TemporaryDirectory(prefix="service-notify-") as folder:
        uploads = {}
        for n, (name, data) in enumerate(files):
            path = Path(folder) / str(n) / name  # a folder each, so Telegram gets the name as it was
            path.parent.mkdir()
            path.write_bytes(data)
            uploads[f"file{n}"] = str(path)
        # Sent without its buttons: they go on only once the message is kept, so no button of a
        # service's is ever on Telegram without the kept file that says whose its taps are.
        params = {"chat_id": person.chat_id,
                  "rich_message": rich(text, [{"type": k, "media": f"attach://file{n}"} for n, k in enumerate(kinds)])}
        if silent:
            params["disable_notification"] = True
        result = call("sendRichMessage", files=uploads or None, **params)
    chat, msg_id = str(person.chat_id), result["message_id"]
    telegram.ChatHistory(chat).remember(msg_id)
    ids = file_ids(result, kinds)
    if files and ids is None:
        log(f"service {service}: Telegram gave no file ids for message {msg_id}'s pictures; an edit drops them")
    media = [{"type": k, "file_id": i} for k, i in zip(kinds, ids or [])]
    try:
        keep(chat, msg_id, service, text, rows, once is not False, expires, media)
        kept_for.remember_sent(msg_id, chat, now)
    except Exception as e:
        log(f"service {service}: can't keep message {msg_id}: {e!r}")
        if rows:
            raise Refused(500, "not_kept", f"message {msg_id} went out without its buttons") from None
    if dedupe:  # before the buttons: a send tried again after they failed must not go out twice
        try:
            kept_for.dedupe_put(dedupe, msg_id, now)
        except Exception as e:
            log(f"service {service}: can't keep the dedupe key of message {msg_id}: {e!r}")
    if rows:
        put_buttons_on(chat, msg_id, rows)
    return {"message_id": msg_id}


def put_buttons_on(chat, msg_id, rows):
    """Put a sent message's buttons on, waiting once when Telegram asks for a few seconds. Refused
    502 buttons_failed, with the message's id, when they can't be: it went out, so it mustn't be
    sent again, and it stays live, so its buttons come off at its expiry if they went on after all."""
    def put_on():
        call("editMessageReplyMarkup", chat_id=chat, message_id=msg_id, reply_markup=keyboard(rows))

    try:
        try:
            put_on()
        except Refused as e:
            if not (e.retry_after and e.retry_after <= BUTTONS_WAIT):
                raise
            time.sleep(e.retry_after)
            put_on()
    except Refused as e:
        raise Refused(502, "buttons_failed", f"message {msg_id} went out, but its buttons may not have: "
                                             f"{e.detail or e.code}", message_id=msg_id) from None


def own_chat(service, msg_id):
    """The chat of a message the service sent, or 404."""
    chat = store.Service(service).chat_of(msg_id) if str(msg_id).isdigit() else None
    if chat is None:
        raise Refused(404, "not_found")
    return chat


def edit(service, msg_id, request, now):
    request = check_object(request)
    chat = own_chat(service, msg_id)
    msg_id = int(msg_id)
    with MESSAGE_LOCKS[(chat, msg_id)]:
        kept = read_kept(chat, msg_id) or {"question": "", "media": []}
        text = check_text(request["text"]) if request.get("text") is not None else kept.get("question")
        if not text:
            raise bad("text must be non-empty text")
        answer, gen = kept.get("answer"), kept.get("gen", 0)
        if request.get("buttons"):  # new buttons: pressable again, as a new set
            rows, answer, gen = check_buttons(request["buttons"]), None, gen + 1
        elif "buttons" in request or answer is not None:  # taken off, or already pressed
            rows = []
        else:
            rows = kept.get("buttons") or []
        if request.get("expires_in") is not None or request.get("buttons"):  # new buttons get a fresh day
            expires = now + check_expires(request.get("expires_in"))
        else:
            expires = kept.get("expires") or now + DEFAULT_EXPIRES
        once = check_switch(request, "once")
        once = kept.get("once", True) if once is None else once
        call("editMessageText", chat_id=chat, message_id=msg_id, rich_message=shown({**kept, "question": text}),
             reply_markup=keyboard(rows, gen))
        keep(chat, msg_id, service, text, rows, once, expires, kept.get("media") or [], answer, gen)
    return {"message_id": msg_id}


def delete(service, msg_id):
    chat = own_chat(service, msg_id)
    msg_id = int(msg_id)
    with MESSAGE_LOCKS[(chat, msg_id)]:
        call("deleteMessage", chat_id=chat, message_id=msg_id)
        store.live_remove(chat, msg_id)
        store.Service(service).forget_sent(msg_id)
        kept = read_kept(chat, msg_id)
        if kept:
            jsonfile.write_json(kept_path(chat, msg_id), {**kept, "answer": kept.get("answer") or "deleted"})
    return {"message_id": msg_id}
