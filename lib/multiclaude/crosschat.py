"""Messages one person's assistant sends into another person's chat with the bot (`tg --chat`).
tg stamps each with the writer's name next to that chat's record (telegram.ChatHistory) and ends
its text with a footer saying whose it is. The other person's answer then goes to the writer's
assistant rather than to their own (router/desk.py, Desk.writer_of)."""
from multiclaude import bot, users
from multiclaude.telegram import markdown_literal


def writer_into(chat, name):
    """The person (users.User) whose assistant, name ($ASSISTANT_USER), sends into chat when chat
    is another registered person's: what it sends there is stamped. None for its own chat, a
    stranger's, or no name (cron, the router)."""
    if not name:
        return None
    try:
        people = users.load_users()
    except (OSError, ValueError):
        return None
    writer = people.get(name)
    reader = next((person for person in people.values() if person.chat_id == str(chat)), None)
    return writer if writer and reader and reader.name != name else None


def footer(display):
    """The last line of a message display's assistant sent into someone else's chat."""
    return f"*✉️ From {markdown_literal(display)}'s {markdown_literal(bot.name())} · your answer goes to it*"


def with_footer(markdown, display):
    """markdown ending with display's footer."""
    return f"{markdown}\n\n{footer(display)}" if markdown.strip() else footer(display)
