"""Chat (add-in chat): a chat screen in the Mini App, next to the control panel, onto the same
assistant as the person's Telegram chat. The page (relay/public/app/chat.js) posts messages to the
relay (relay/src/chat.js), which queues them as "chat" envelopes; the router posts each silently in
the person's Telegram chat ("💬 …"), so the chat there shows the whole conversation, and types it
into their assistant under a [Mini App from …] header. The assistant answers with tg, as always:
tg passes what it sent on (tg_sent) to the outbox here, and the feed (feed.py) shows it on the page
with the assistant's work steps from its transcript, while the page is open.

Files: state/chat/outbox/<name>.jsonl (what tg sent), state/chat/seen/<name> (the Mini App messages
taken in the last day, so a retry isn't run twice), state/chat/<name>.seq (files.py, feed.py).

Other add-ins may extend the chat, as add-ins offer hooks to each other (addons/__init__.py). The chat
asks the add-ins that are on, by these names, and knows each add-in only by the name it's on under:
- chat_tabs(user) -> [{"id", "title"}]: tabs after Chat and Panel; the page loads the add-in's
  addons/<name>/tab.js (from its relay-public/ folder, which `assistant relay deploy` uploads), an ES
  module exporting mount(root, api). Public: no secrets or addresses in it.
- chat_models(user) -> [{"id", "label", "hint", "group", "badge"}]: more models in the model sheet. Picking
  one makes "<name>:<id>" the composer's target.
- chat_actions(user) -> [{"id", "label", "icon", "template", "form": [{"name", "label", "type": "select",
  "text", "textarea" or "switch", "options", "default", "on", "off"}]}]: forms behind the composer's ⚡
  chip; the page fills the composer from template ("{field}" for each field) and sends nothing.
- chat_message(user, target, text, client_id) -> bool: a message the person sent to one of the
  add-in's targets, after every check a message gets; True once taken. It never reaches the
  assistant, the transcript or the Telegram chat. "/clear" sent to a target comes here too.
- chat_stop(user, target): Stop while that target answers.
- chat_backfill(user) -> [event]: the add-in's own events for a page that opens.
is_open(user) says whether the person's page reads the feed now, and feed_add(user, event) puts an
add-in's event on an open page: "model_message" (key, model,
markdown, done, meta), "addon_state" (addon, state; the page's tab for that add-in gets it, the
newest only; at most ADDON_STATE_MAX bytes) or "status" with a target and a text. The hooks run on
the router's loop (tabs, models and actions every EXTRAS_EVERY), so they keep what's slow to get."""
import json
import os
import re
import threading
import time
import urllib.parse

from multiclaude import bot, telegram
from multiclaude.addons.chat import events, files
from multiclaude.log import log

APP = "Mini App"
MENU_BUTTON = "Chat"
COPY_OVER, COPY_CUT = 3500, 3000           # a longer message is cut to its start in Telegram's copy
COPY_MORE = "… (full text in the Mini App)"
TEXT_MAX = 16_000                          # characters of a message, as the page allows
SEND_SIGNED_WITHIN = 3600                  # a message's init data was signed at most this before the relay queued it
QUEUED_WITHIN = 24 * 3600                  # and the relay queued it at most this long ago
CLOCK_SKEW = 300
TARGET_RE = re.compile(r"[a-z0-9][a-z0-9-]*:[\w.:-]{1,80}")  # "<add-in>:<its own id>"
ADDON_STATE_MAX = 64 * 1024                # bytes of an add-in's state for its tab
NOT_AVAILABLE = "That model isn't available now."
MEMBERS_EVERY = 10 * 60                    # the members list is put again this often, in case the relay lost it
MEMBERS_RETRY = 30
CHAT_COMMAND_RE = re.compile(r"/chat(@\w+)?")
NAME_RE = re.compile(r"[A-Za-z0-9_][\w.-]*")
OFFER = "💬 **Chat**: talk to {bot} in a chat screen, with your model and effort at hand."

_feeds = {}                                 # name -> Feed: only in the router, once that person's page opened
_members = {"ids": None, "next": 0.0, "busy": False}
_posting = threading.local()                # set while the router posts a 💬 copy, which is no notice


def later(fn, *args):
    """Run fn(*args) in the background; a failure is logged."""
    def run():
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001 — a background step of an add-in never takes the router down
            log(f"chat: {getattr(fn, '__name__', fn)} failed: {e!r}")
    threading.Thread(target=run, daemon=True).start()


# --- hooks ---

def relay_envelopes():
    return {"chat": take_message, "chat_open": open_feed, "chat_history": answer_history}


def help_info(user, app):
    return {"ways": [f"the {MENU_BUTTON} button (a {APP})"]}


def menu_button(button):
    """The button by the message box opens the chat; the control panel is its neighbouring tab."""
    return {"text": MENU_BUTTON, "url": chat_address(button["url"])}


def chat_address(url):
    """The Mini App's address, opening on the Chat tab."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.urlencode([*urllib.parse.parse_qsl(parts.query), ("tab", "chat")])
    return parts._replace(query=query).geturl()


def on_message(user, msg, now):
    """/chat: a button that opens the chat."""
    if msg.via or not CHAT_COMMAND_RE.fullmatch(msg.text.strip()):
        return False
    from multiclaude.router import panel
    url = panel.address()
    if not url:
        telegram.later(user.say, "The chat needs the relay, which isn't set up: run `assistant relay deploy` on the VM.")
        return True
    button = {"inline_keyboard": [[{"text": "💬 Open the chat", "web_app": {"url": chat_address(url)}}]]}
    telegram.later(telegram.send, user.chat_id, OFFER.format(bot=bot.name()), reply_markup=button)
    return True


def tg_sent(sender, chat, record):
    """What an assistant sent with tg goes to its outbox, for the feed. Not a job's (ASSISTANT_JOB),
    nor what `assistant notify`, cron or a script sends (no sender): those aren't the conversation."""
    if not sender or os.environ.get("ASSISTANT_JOB") or not NAME_RE.fullmatch(sender):
        return
    try:
        files.append_outbox(sender, {"at": time.time(), "chat": str(chat), **record})
    except (OSError, TimeoutError) as e:
        log(f"chat: couldn't keep message {record.get('msg_id')} for {sender}'s Mini App ({e}); it was sent all the same")


def on_tick(desk, now):
    put_members(desk, now)
    for name, user in list(desk.users.items()):
        feed = _feeds.get(name)
        if feed and feed.user is user:
            feed.tick(now, later)


def after_wipe(user, before, keep):
    """/clear or /restart: the outbox and what the relay holds of the old conversation go."""
    try:
        files.empty_outbox(user.name)
    except (OSError, TimeoutError) as e:
        log(f"{user.name}: can't empty the chat's outbox: {e}")
    feed_of(user).cleared(time.time(), later)


def before_telegram_send(chat_id, method, params):
    """A notice of the router's (plain text, as telegram.send posts it) to someone whose page is
    open also shows there, as a line of its own. Only in the router: elsewhere no feed is open."""
    if not _feeds or method != "sendRichMessage" or getattr(_posting, "on", False):
        return None
    blocks = (params.get("rich_message") or {}).get("blocks")
    feed = next((f for f in list(_feeds.values()) if str(f.user.chat_id) == str(chat_id)), None)
    if not blocks or not feed or not feed.watching(time.time()):
        return None
    text = "\n\n".join(plain(block.get("text")) for block in blocks if isinstance(block, dict))
    now = time.time()
    feed.add({"kind": "notice", "key": f"n:{now:.3f}", "ts": events.iso(now), "text": text[:events.TEXT_MAX]}, now)
    return None


def plain(text):
    if isinstance(text, str):
        return text
    return "".join(t if isinstance(t, str) else f"`{t.get('text', '')}`" for t in text or [] if isinstance(t, (str, dict)))


# --- the feed ---

def feed_of(user):
    from multiclaude.addons.chat.feed import Feed
    feed = _feeds.get(user.name)
    if not feed or feed.user is not user:
        feed = _feeds[user.name] = Feed(user)
    return feed


def person(desk, chat):
    name = desk.by_chat.get(str(chat))
    return desk.users.get(name) if name else None


def open_feed(desk, payload):
    """A "chat_open" envelope: the page read the feed, first ({"backfill": true}) or again."""
    user = person(desk, payload.get("user"))
    if user:
        feed_of(user).open(time.time(), payload.get("backfill") is True, later)


def answer_history(desk, payload):
    """A "chat_history" envelope: the page scrolled up; the events before its cursor go back to the
    relay, which holds the page's request for them."""
    request_id, user = str(payload.get("id") or ""), person(desk, payload.get("user"))
    if not re.fullmatch(r"[0-9a-f]{8}", request_id) or not user:
        return
    feed = feed_of(user)

    def answer():
        from multiclaude.addons.chat.feed import older
        from multiclaude.router import relay_entrance
        try:
            relay_entrance.answer(f"/chat/history/{request_id}", older(user, payload.get("cursor"), feed.left))
        except OSError as e:
            log(f"{user.name}: can't put older chat history on the relay: {e}")
    later(answer)


# --- messages from the page ---

def take_message(desk, payload, now=None):
    """A "chat" envelope: {init_data, client_id, text, queued_at}. Checked as the relay checked it,
    against the bot's own token; then posted silently in the person's Telegram chat and given to
    their assistant like a message sent there. Fields it doesn't know (a model, an effort) are
    ignored: a message never switches settings."""
    now = time.time() if now is None else now
    chat = signed_recently(payload, now)
    user = person(desk, chat) if chat else None
    client_id, text = str(payload.get("client_id") or ""), payload.get("text")
    if not user:
        log("chat: a message Telegram didn't sign recently enough for anyone here; dropped")
        return
    if not files.CLIENT_ID_RE.fullmatch(client_id) or not isinstance(text, str) or not text.strip() \
            or len(text) > TEXT_MAX:
        log(f"{user.name}: a chat message that isn't one; dropped")
        return
    target = payload.get("target")
    if target is not None and not TARGET_RE.fullmatch(str(target)):
        log(f"{user.name}: a chat message to {target!r}, which is no target; dropped")
        return
    if not files.take_message(user.name, client_id, now):
        log(f"{user.name}: chat message {client_id} came again; it was taken already")
        return
    if target:
        to_target(user, target, text, client_id, now)
        return
    from multiclaude.channels import TelegramChat
    from multiclaude.router import inbound
    user.reply_to = TelegramChat(user.chat_id)
    feed = feed_of(user)
    feed.message_came(now)
    msg_id = ""
    if not is_command(text):
        msg_id = post_copy(user, text)
        files.note_message_id(user.name, client_id, msg_id)
        feed.add({"kind": "user_message", "key": events.user_key(msg_id) if msg_id else f"u:c{client_id}",
                  "ts": events.iso(now), "client_id": client_id, "msg_id": msg_id, "text": text,
                  "source": "miniapp", "state": "queued"}, now)
    log(f"{user.name}: chat message {client_id} ({len(text)} characters)")
    msg = inbound.Inbound(user.chat_id, user.chat_id, msg_id, text, {"ts": events.iso(now)}, app=APP)
    desk.dispatch(user, msg, now)


def to_target(user, target, text, client_id, now):
    """A message to an add-in's target (X3): to that add-in, never to the assistant: no copy in the
    Telegram chat, no Inbound, nothing woken. Stop goes to chat_stop instead (X6)."""
    feed, name = feed_of(user), target.split(":", 1)[0]
    feed.message_came(now, wake=False)
    if text.strip() == "/stop":
        log(f"{user.name}: stop for {target}")
        offer(name, "chat_stop", user, target)
        return
    if not is_command(text):
        feed.add({"kind": "user_message", "key": f"u:c{client_id}", "ts": events.iso(now), "client_id": client_id,
                  "text": text, "source": "miniapp", "target": target, "state": "delivered"}, now)
    log(f"{user.name}: chat message {client_id} for {target} ({len(text)} characters)")
    if not offer(name, "chat_message", user, target, text, client_id):
        feed.add({"kind": "notice", "key": f"n:{client_id}", "ts": events.iso(now), "text": NOT_AVAILABLE}, now)


# --- what other add-ins bring to the chat ---

def addon_name(module):
    return module.__name__.rsplit(".", 1)[-1].replace("_", "-")


def offered(hook, *args):
    """(add-in, what it gave) for each add-in that's on and offers hook; one that fails is logged."""
    from multiclaude import addons
    found = []
    for module in addons.active():
        fn = getattr(module, hook, None)
        if fn is None:
            continue
        try:
            found.append((addon_name(module), fn(*args)))
        except Exception as e:  # noqa: BLE001 — an add-in never breaks the chat
            log(f"chat: {addon_name(module)}'s {hook} failed: {e!r}")
    return found


def offer(name, hook, *args):
    """What the add-in called name gives for hook, if it's on and offers it; else None."""
    return next((given for addon, given in offered(hook, *args) if addon == name), None)


def extras(user):
    """The tabs, models and actions the add-ins that are on bring to user's chat (X1, X2, X5)."""
    def listed(hook):
        return [(name, item) for name, items in offered(hook, user) for item in items or []
                if isinstance(item, dict) and item.get("id")]
    return {"tabs": [{"id": f"{name}:{t['id']}", "title": str(t.get("title") or t["id"]), "addon": name,
                      "module": f"addons/{name}/tab.js"} for name, t in listed("chat_tabs")],
            "models": [{"target": f"{name}:{m['id']}", "label": str(m.get("label") or m["id"]),
                        "hint": m.get("hint") or "", "group": m.get("group") or name, "badge": m.get("badge") or ""}
                       for name, m in listed("chat_models")],
            "actions": [{**a, "id": f"{name}:{a['id']}", "addon": name} for name, a in listed("chat_actions")]}


def backfilled(user):
    """The add-ins' own events for a page that opens (chat_backfill)."""
    return [event for _, given in offered("chat_backfill", user) for event in given or [] if isinstance(event, dict)]


def is_open(user):
    """For other add-ins: whether user's page reads the feed now, so there's someone to build for."""
    feed = _feeds.get(user.name)
    return bool(feed and feed.user is user and feed.watching(time.time()))


def feed_add(user, event):
    """For other add-ins: put event on user's page while it's open (see above). Whether it went."""
    kind = event.get("kind")
    if kind not in ("model_message", "addon_state", "status") or kind == "status" and not event.get("target"):
        raise ValueError(f"chat: an add-in may add model_message, addon_state or a target's status, not {kind}")
    if kind == "addon_state" and len(json.dumps(event, ensure_ascii=False).encode()) > ADDON_STATE_MAX:
        log(f"{user.name}: {event.get('addon')}'s state for the chat is over {ADDON_STATE_MAX} bytes; left out")
        return False
    if not is_open(user):
        return False
    _feeds[user.name].add(event)
    return True


def signed_recently(payload, now):
    """The Telegram id whose init data the message carries, when Telegram signed it at most
    SEND_SIGNED_WITHIN before the relay queued it, and the relay queued it at most QUEUED_WITHIN
    ago: a message that waited at the relay while the machine was offline still goes. Else None."""
    from multiclaude.router import panel
    init_data = str(payload.get("init_data") or "")
    try:
        queued_at = float(payload.get("queued_at"))
        signed_at = int(dict(urllib.parse.parse_qsl(init_data)).get("auth_date", ""))
    except (TypeError, ValueError):
        return None
    if not now - QUEUED_WITHIN <= queued_at <= now + CLOCK_SKEW:
        return None
    if not queued_at - SEND_SIGNED_WITHIN <= signed_at <= queued_at + CLOCK_SKEW:
        return None
    return panel.signed_sender(init_data, telegram.bot_token() or "", queued_at)


def is_command(text):
    """Whether text is one of the bot's commands (/stop, /clear …), which the router carries out
    rather than the assistant, and which isn't part of the conversation."""
    from multiclaude.router import inbound
    text = inbound.in_latin_layout(text.strip())
    return any(p.fullmatch(text) for p in (*inbound.DESK_COMMAND_RES, inbound.MCP_LOGIN_RE))


def dm_copy(text, attachments=()):
    """What Telegram's chat shows of a Mini App message: its text, cut when Telegram couldn't hold
    it, or the names of the files it carries."""
    if not text.strip() and attachments:
        return "💬 📎 " + ", ".join(attachments)
    shown = text if len(text) <= COPY_OVER else text[:COPY_CUT] + COPY_MORE
    return f"💬 {shown}"


def post_copy(user, text):
    """Post the message silently in the person's Telegram chat; its message id, "" if it couldn't go."""
    _posting.on = True
    try:
        record = telegram.send(user.chat_id, dm_copy(text), disable_notification=True) or {}
    finally:
        _posting.on = False
    return str(record.get("message_id") or "")


# --- who may use the chat ---

def members(users):
    """The Telegram ids the relay lets in: everyone's, or while anyone has "chat_beta": true in
    users.json, only theirs (to try it out first)."""
    trying = [u for u in users.values() if u.entry.get("chat_beta") is True]
    return sorted(str(u.chat_id) for u in (trying or users.values()) if u.chat_id)


def put_members(desk, now):
    """Put the members list on the relay when it changed, and every MEMBERS_EVERY (MEMBERS_RETRY
    after a failure)."""
    ids = members(desk.users)
    if _members["busy"] or (ids == _members["ids"] and now < _members["next"]):
        return
    _members.update(ids=ids, busy=True)

    def put():
        from multiclaude.router import relay_entrance
        ok = False
        try:
            url, token = relay_entrance.address("/chat/members"), relay_entrance.token()
            if url and token:
                relay_entrance.put(url, token, json.dumps({"ids": ids}).encode())
                ok = True
        except OSError as e:
            log(f"chat: can't put the members list on the relay: {e}")
        finally:
            _members.update(busy=False, next=time.time() + (MEMBERS_EVERY if ok else MEMBERS_RETRY))
    later(put)
