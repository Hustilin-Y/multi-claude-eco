"""When the model's safeguards stop a reply, Claude Code asks in the terminal whether to switch to another
model from now on. Nobody watches that terminal, and nothing in the transcript says the turn ended, so the
session would sit there, "typing", with everything queued behind it, until the turn is judged stuck. The
session's worker answers Stay instead (a person's assistant keeps the model they chose, and a message that
tripped the safeguards is not handed to a weaker one), ends the turn, takes the stopped message out of the
message box so it doesn't go out again with the next one, and tells the person."""
from multiclaude import telegram
from multiclaude.log import log
from multiclaude.router import claude_ui

NOTICE = ("⚠️ The model's safeguards stopped the answer to the last message, so nothing was done for it. "
          "If it was meant, send it again in other words.")
PUT_BACK_WAIT = 5  # seconds for Claude Code to put the stopped message back in the message box


def check(session):
    """Answer the question if it is on screen. Whether it was. Runs in the session's worker thread."""
    dialog = claude_ui.safeguard_dialog(session.pane.screen())
    if dialog is None:
        return False
    problem = claude_ui.choose(session.pane, dialog, claude_ui.STAY, read=claude_ui.safeguard_dialog)
    if problem:
        log(f"{session.name}: the model's safeguards stopped a reply, and answering Stay failed: {problem}")
        return True
    log(f"{session.name}: the model's safeguards stopped a reply; answered Stay")
    if session.wait_screen(claude_ui.holds_typed_message, PUT_BACK_WAIT) is not None:
        session.empty_message_box()  # ends the turn too
    else:
        session.turn.end()
    if session.chat_id:
        try:
            telegram.api("sendRichMessage", chat_id=session.chat_id,
                         rich_message={"markdown": telegram.markdown_literal(NOTICE)})
        except Exception as e:
            log(f"{session.name}: can't tell them the safeguards stopped a reply: {e!r}")
    return True
