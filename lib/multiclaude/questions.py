"""Questions with buttons, kept so the router can answer a tap on one (router/asks.py): `tg ask`
keeps its own (and add-ins' apps theirs), and the router its own, such as the Cancel under a
waiting /clear. A `tg checklist` is kept the same way, as kind CHECKLIST with the items ticked: its
buttons carry "ask:<n>" too, so the Telegram plugin passes their taps on unchanged."""
import time

from multiclaude import jsonfile

KEPT_FOR = 30 * 86400
CHECKLIST = "checklist"  # the kind of a kept `tg checklist`
UNTICKED, TICKED = "☐", "✅"


def option_buttons(options):
    """Telegram's inline keyboard for a question: one button per option, each carrying "ask:<n>"."""
    return {"inline_keyboard": [[{"text": o, "callback_data": f"ask:{n}"}] for n, o in enumerate(options)]}


def checklist_buttons(items, ticked):
    """A checklist's inline keyboard: one button per item, ✅ when ticked, ☐ when not."""
    return {"inline_keyboard": [[{"text": f"{TICKED if n in ticked else UNTICKED} {item}", "callback_data": f"ask:{n}"}]
                                for n, item in enumerate(items)]}


def save(folder, message_id, question, options, kind=None, **more):
    """Keep a question in folder (paths.ASKS/<chat>) by its message id, and drop the month-old
    ones. kind marks a question the router answers itself (its own, or a checklist); a tap on it
    isn't typed to the assistant. more: what else that kind keeps, such as a checklist's ticked."""
    jsonfile.write_json(folder / f"{message_id}.json",
                        {"question": question, "options": options, **({"kind": kind} if kind else {}), **more})
    cutoff = time.time() - KEPT_FOR
    for old in folder.glob("*.json"):
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
