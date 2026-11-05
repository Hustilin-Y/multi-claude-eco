"""Taps on question buttons: `tg ask` questions (kept in paths.ASKS), whose choice the router shows
on the question at once and then types into the assistant that asked, the router's own questions
(approval dialogs, the Cancel under a waiting /clear), which it answers itself, and `tg checklist`
items, which it ticks or unticks itself. An add-in that brings another
app answers taps there with the same steps: clear_cancel_tapped, full_label, save_answer and
deliver_choice."""
import re
import threading
from collections import defaultdict

from multiclaude import crosschat, jsonfile, paths, questions, telegram
from multiclaude.channels import TelegramChat
from multiclaude.log import log
from multiclaude.router import approvals, work
from multiclaude.router import inbound as messages

BUTTON_RE = re.compile(r"ask:(\d+)")
CLEAR_CANCELLED = "↩️ Cancelled: your context and our chat stay as they are."
CLEAR_TOO_LATE = "⌛ Too late: the clear had already started."
# One lock per checklist message, so its buttons are redrawn one at a time, each from the saved state.
CHECKLIST_LOCKS = defaultdict(threading.Lock)


def answer(user, tap, now, to=None):
    """A tap by user in their chat. A `tg ask` question's choice goes to to, the assistant of
    another person that asked it there (Desk.writer_of), or else to user's own."""
    to = to or user
    msg_id = tap.msg_id
    if not str(msg_id).isdigit():
        log(f"{user.name}: button tap without a question message id; ignored")
        return
    path = paths.ASKS / str(user.chat_id) / f"{msg_id}.json"
    ask = jsonfile.read_json(path, {})
    if not isinstance(ask, dict):
        ask = {}
    if ask.get("kind") == questions.CHECKLIST:
        checklist_tapped(user, path, ask, msg_id, tap.attrs.get("callback_data", ""))
        return
    if ask.get("answer") is not None:
        log(f"{user.name}: question {msg_id} was already answered; ignoring a tap on {tap.text!r}")
        return
    if ask.get("kind") == work.CANCEL_CLEAR:
        clear_cancel_tapped(user, TelegramChat(user.chat_id), path, ask, msg_id)
        return
    label = full_label(ask.get("options") or [], tap.attrs.get("callback_data", ""), tap.text)
    question = ask.get("question")
    if ask.get("kind") == "approval":
        approval_tapped(user, path, ask, msg_id, label)
        return
    if question:
        shown = f"{question}\n\n👉 {telegram.markdown_literal(label)}"
        if to is not user:
            shown = crosschat.with_footer(shown, to.display)
        telegram.later(telegram.api, "editMessageText", chat_id=user.chat_id, message_id=msg_id,
                       rich_message={"markdown": shown})
        save_answer(user, path, ask, msg_id, label)
    else:
        telegram.later(telegram.api, "editMessageReplyMarkup", chat_id=user.chat_id, message_id=msg_id)  # buttons gone
    deliver_choice(to, tap, question, label, TelegramChat.cli, now)


def deliver_choice(user, tap, question, label, tool, now):
    """Type the choice into user's assistant, which then edits the question with tool (the chat's cli)."""
    msg_id = tap.msg_id
    header = messages.header(user.display, tap, what="tapped a button on your question msg {id}",
                             intro="My answer is pasted below; it is my own choice:")
    chooser = f"{tap.via.sender}'s" if tap.via else "My"
    body = ((f"Your question: {question}\n" if question else "") + f"{chooser} choice: {label}\n\n"
            f"The buttons are gone and the question now shows the choice. Edit msg {msg_id} to say "
            f"what you will do ({messages.cli_for(tap, tool)} edit {msg_id} \"...\"), then do it.")
    if tap.via:
        body += f" Tell {tap.via.person} what came back with tg send."
    user.queue_message(work.Deliver(now, header, body, msg_id), now)
    log(f"{user.name}: button {label!r} on question {msg_id} queued for delivery")


def clear_cancel_tapped(user, chat, path, ask, msg_id):
    """A tap on the Cancel under a waiting /clear: it's dropped, unless it has already started."""
    in_time = work.cancel_clear(user, msg_id)
    log(f"{user.name}: /clear {'cancelled' if in_time else 'not cancelled: it had already started'}")
    telegram.later(chat.edit, msg_id, CLEAR_CANCELLED if in_time else CLEAR_TOO_LATE)
    save_answer(user, path, ask, msg_id, "cancelled" if in_time else "too late")


def checklist_tapped(user, path, ask, msg_id, data):
    """A tap on a checklist item: tick it, or untick it if it was ticked. Nothing is typed to the
    assistant; `tg checked` reads what's ticked."""
    items = ask.get("options") or []
    m = BUTTON_RE.fullmatch(data)
    if not m or int(m[1]) >= len(items):
        log(f"{user.name}: tap {data!r} on checklist {msg_id} matches no item; ignored")
        return
    n = int(m[1])
    ticked = set(ask.get("ticked") or []) ^ {n}
    try:
        jsonfile.write_json(path, {**ask, "ticked": sorted(ticked)})
    except OSError as e:
        log(f"{user.name}: can't save checklist {msg_id}: {e}")
        return
    log(f"{user.name}: checklist {msg_id}: item {n} {'ticked' if n in ticked else 'unticked'}")
    telegram.later(redraw_checklist, user.chat_id, msg_id, path)


def redraw_checklist(chat_id, msg_id, path):
    """Show a checklist's buttons as saved. Under its lock, so of quick taps the last redraw shows them all."""
    with CHECKLIST_LOCKS[(str(chat_id), str(msg_id))]:
        ask = jsonfile.read_json(path, {})
        if isinstance(ask, dict) and ask.get("options"):
            telegram.api("editMessageReplyMarkup", chat_id=chat_id, message_id=msg_id,
                         reply_markup=questions.checklist_buttons(ask["options"], set(ask.get("ticked") or [])))


def approval_tapped(user, path, ask, msg_id, label):
    """A tap on an approval question: the worker presses it in the dialog. Nothing is typed to
    the assistant; Claude Code tells it how the dialog was answered."""
    on_time = approvals.tapped(user, msg_id, label)
    note = f"👉 {label}" if on_time else "⌛ Too late: the dialog had already closed, so Claude Code denied it."
    approvals.show_note(user.chat_id, msg_id, ask.get("blocks") or [], note)
    save_answer(user, path, ask, msg_id, label if on_time else "expired")


def save_answer(user, path, ask, msg_id, answer):
    """Mark the kept question answered, so a second tap on it is ignored."""
    try:
        jsonfile.write_json(path, {**ask, "answer": answer})
    except OSError as e:
        log(f"{user.name}: can't save the answer to question {msg_id}: {e}")


def full_label(options, data, shown):
    """The option the button stood for, in case Telegram shortened its label."""
    m = BUTTON_RE.fullmatch(data)
    return options[int(m[1])] if m and int(m[1]) < len(options) else shown
