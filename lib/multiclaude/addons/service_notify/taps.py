"""Taps on a service's buttons, which come here from the router (the question_tapped hook) and
never reach an assistant: each becomes a press queued for the service, which takes it with a
long-poll. And what the router does by itself: take the buttons off a message past its expiry,
and drop a press the service didn't take within 2 minutes, telling the owner nothing was done."""
import threading
import traceback

from multiclaude import jsonfile, telegram
from multiclaude.addons.service_notify import messages, store
from multiclaude.log import log

EXPIRED = "⌛ Expired: nothing was done."
EXPIRED_AFTER_USE = "⌛ Expired."  # buttons that stay may have been pressed before, so "nothing" would be wrong
MISSED = "⚠️ {service} didn't pick this up in time: nothing was done."
_WAKES = {}
_WAKES_LOCK = threading.Lock()


def wake(service):
    """The condition a service's long-poll waits on, notified when a press is queued for it."""
    with _WAKES_LOCK:
        return _WAKES.setdefault(service, threading.Condition())


def pending(label):
    return f"⏳ {telegram.markdown_literal(label)}…"


def mark(chat, msg_id, kept, answer):
    jsonfile.write_json(messages.kept_path(chat, msg_id), {**kept, "answer": answer})


def tapped(user, tap, path, ask, now):
    """A tap on a kept question (the question_tapped hook): True when it's a service's. The rest
    runs in the background, since it waits for the message's lock and for Telegram, and the
    router's loop routes everyone's messages."""
    if ask.get("kind") != messages.KIND:
        return False
    telegram.later(take_tap, user, tap, now)
    return True


def take_tap(user, tap, now):
    """Queue a tap on a service's button as a press for the service, under the message's lock."""
    chat, msg_id = str(user.chat_id), int(tap.msg_id)
    try:
        with messages.MESSAGE_LOCKS[(chat, msg_id)]:
            service = press_for(user, tap, chat, msg_id, now)
    except Exception:
        log(f"{user.name}: a tap on service message {msg_id} failed:\n{traceback.format_exc().rstrip()}")
        return
    if service:
        log(f"{user.name}: a press on {service}'s message {msg_id} queued for it")
        notify(service)


def press_for(user, tap, chat, msg_id, now):
    """The service a press was queued for, or None when the tap did nothing."""
    data = tap.attrs.get("callback_data") or ""
    n = int(data[4:]) if data.startswith("ask:") and data[4:].isdigit() else -1
    reason, service = queue_press(user, chat, msg_id, n, now, "telegram")
    return service if reason is None else None


def press_from_app(user, service, message_id, index, now):
    """A press on button index of service's message message_id, made in the Mini App by user (a router
    add-in calls this for the person whose page asked, never for anyone else). The same checks as a
    tap, under the message's lock, and the Telegram message shows it. None when queued, else why not:
    "unknown", "other_service", "no_button", "answered" or "expired". The button ids stay here."""
    chat, msg_id = str(user.chat_id), int(message_id)
    with messages.MESSAGE_LOCKS[(chat, msg_id)]:
        reason, _ = queue_press(user, chat, msg_id, int(index), now, "miniapp", service)
    if reason is None:
        log(f"{user.name}: a press from the Mini App on {service}'s message {msg_id} queued for it")
        notify(service)
    return reason


def queue_press(user, chat, msg_id, n, now, via, expected=None):
    """(None, service) when button n of the kept message was queued as a press, else (why not, service).
    Under the message's lock. expected, when given, is the only service whose message may be pressed."""
    kept = messages.read_kept(chat, msg_id)  # afresh, under the lock
    if not kept or kept.get("kind") != messages.KIND:
        log(f"{user.name}: press on service message {msg_id}, which isn't kept any more; ignored")
        return "unknown", None
    service = kept.get("service")
    if expected is not None and service != expected:
        log(f"{user.name}: {expected} asked to press {service}'s message {msg_id}; refused")
        return "other_service", service
    buttons = [b for row in kept.get("buttons") or [] for b in row]
    if not 0 <= n < len(buttons):
        log(f"{user.name}: press {n} on {service}'s message {msg_id} matches no button; ignored")
        return "no_button", service
    once = kept.get("once", True)
    if kept.get("answer") is not None and (once or kept.get("answer") == "expired"):
        log(f"{user.name}: {service}'s message {msg_id} was already answered; a press ignored")
        return ("expired" if kept.get("answer") == "expired" else "answered"), service
    if now > kept.get("expires", 0):
        log(f"{user.name}: press on {service}'s message {msg_id} after it expired")
        expire(chat, msg_id, kept)
        return "expired", service
    button = buttons[n]
    store.Service(service).add_press({"chat": chat, "message_id": msg_id, "button_id": button["id"],
                                      "label": button["text"], "by": user.name, "at": now, "via": via}, now)
    if once:
        mark(chat, msg_id, kept, n)
        shown = pending(button["text"]) + (" (Mini App)" if via == "miniapp" else "")
        try:
            messages.take_buttons_off(chat, msg_id, shown)
        except messages.Refused as e:  # it stays live: the sweep takes them off by its expiry
            log(f"{user.name}: can't show the press on {service}'s message {msg_id}: {e}")
    return None, service


def expire(chat, msg_id, kept):
    """Buttons past their expiry: off, with a note, and any press still waiting on them dropped.
    Refused when Telegram didn't take the edit (it stays live, for the next sweep)."""
    once = kept.get("once", True)
    store.Service(kept.get("service")).drop_presses_for(chat, msg_id)
    messages.take_buttons_off(chat, msg_id, EXPIRED if once else EXPIRED_AFTER_USE)
    mark(chat, msg_id, kept, "expired")


def notify(service):
    condition = wake(service)
    with condition:
        condition.notify_all()


def sweep(now):
    """Take the buttons off expired messages, and drop the presses no service took in time."""
    for chat, msg_id in store.live_due(now):
        try:
            with messages.MESSAGE_LOCKS[(chat, msg_id)]:
                kept = messages.read_kept(chat, msg_id)
                if not kept:
                    store.live_remove(chat, msg_id)
                elif kept.get("expires", 0) >= now:
                    continue  # given new buttons meanwhile
                elif kept.get("answer") is None or not kept.get("once", True) and kept.get("answer") != "expired":
                    expire(chat, msg_id, kept)
                else:  # pressed (or expired) already, but its buttons' edit didn't go through
                    messages.take_keyboard_off(chat, msg_id)
        except messages.Refused as e:
            log(f"service-notify: message {msg_id}'s buttons stay for the next sweep: {e}")
    for service in services():
        for press in store.Service(service).drop_stale(now):
            chat, msg_id = press.get("chat"), press.get("message_id")
            log(f"service {service}: a press on message {msg_id} wasn't taken in time; dropped")
            with messages.MESSAGE_LOCKS[(chat, msg_id)]:
                kept = messages.read_kept(chat, msg_id)
                if not kept:
                    continue
                rows = [] if kept.get("once", True) or kept.get("answer") == "expired" else kept.get("buttons") or []
                try:
                    messages.call("editMessageText", chat_id=chat, message_id=msg_id,
                                  rich_message=messages.shown(kept, MISSED.format(service=service)),
                                  reply_markup=messages.keyboard(rows))
                except messages.Refused as e:
                    log(f"service {service}: can't tell the owner a press on message {msg_id} was dropped: {e}")


def services():
    """Every service with state kept, a key or not (one revoked may still have presses waiting)."""
    try:
        return sorted(p.name for p in store.ROOT.iterdir() if p.is_dir() and store.NAME_RE.fullmatch(p.name))
    except FileNotFoundError:
        return []
