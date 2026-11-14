"""What the Mini App's chat shows, as feed events built from a person's assistant: its transcript
(what they and others wrote to it, and its work steps) and the chat outbox (what it sent with tg).

An event is a dict with "kind" and, for anything shown in the conversation, "key" (the page updates
the item with that key in place, so the same message built twice shows once) and "ts" (when it
happened, for the order). The feed adds "seq" and "at" (feed.py). Kinds: user_message,
assistant_message, message_edited, step, status, settings, notice, cleared.

Nothing that may hold a secret leaves the machine as it is: tool inputs and results are masked
over their whole text first, then trimmed (mask, preview), and a step that reads or writes a file
whose name says it holds secrets shows no input or result at all (sensitive). Thinking blocks are
never shown."""
import json
import re
import time
from datetime import datetime, timezone

EVENT_MAX = 16 * 1024        # bytes of one event's JSON; a longer message is cut (fit)
TEXT_MAX = 16_000            # characters of a person's message shown
INPUT_MAX = 500              # characters of a step's input shown
RESULT_MAX = 2048            # characters of a step's result shown
TITLE_MAX = 80
CUT_NOTE = "… (full text in Telegram)"
HIDDEN = "[hidden: may contain secrets]"
MASK = "•••"

# Values that look like secrets, in any text: a value after token=, password: …; a bearer token;
# an Anthropic or OpenAI style key; a JWT; a PEM block (one cut before its END too).
SECRET_RES = [
    re.compile(r"(?i)((?:token|secret|password|passwd|api[_-]?key)[\w-]*[\"']?\s*[:=]\s*)(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;]+)"),
    re.compile(r"(?i)(\bBearer\s+)(\S+)"),
    re.compile(r"()(\bsk-[A-Za-z0-9_-]{20,})"),
    re.compile(r"()(\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*)"),
    re.compile(r"()(-----BEGIN [A-Z0-9 ]+-----.*?(?:-----END [A-Z0-9 ]+-----|\Z))", re.S),
]
# File names that say a file holds secrets (SPEC 6.4), matched against a path's last part or all of it.
SENSITIVE_NAME_RES = [re.compile(p) for p in (r"\.env(\..*)?", r".*credential.*", r".*secret.*", r"id_.*",
                                              r".*\.pem", r".*\.key")]
SENSITIVE_PATH_RES = [re.compile(r"(^|/)\.claude/channels/"), re.compile(r"(^|/)assistant/\.env$")]
WORD_SPLIT_RE = re.compile(r"[\s;|&<>()'\"`=,]+")
TG_CALL_RE = re.compile(r"(^|[\s;&|(`$])tg\s")  # a Bash command that runs tg: what it sent comes from the outbox
SUBAGENT_TOOLS = ("Task", "Agent")
FILE_TOOLS = {"Read": "Read", "Write": "Wrote", "Edit": "Edited", "MultiEdit": "Edited", "NotebookEdit": "Edited"}

# A message typed into the assistant: "[<app> from <who> · <what>] <intro>", then the body.
HEADER_RE = re.compile(r"\[(?P<app>[A-Za-z][\w ]*?) from (?P<who>[^·\]\n]+?) · (?P<what>[^\]\n]*)\][^\n]*(?:\n(?P<body>.*))?",
                       re.S)
MSG_ID_RE = re.compile(r"\bmsg (\d+)")
SOURCES = {"Telegram": "telegram", "Mini App": "miniapp", "Slack": "slack", "Siri": "siri"}


# --- secrets ---

def mask(text):
    """text with every value that looks like a secret replaced by •••. Run on the whole text,
    before any cut, so a cut can't leave half a secret the patterns no longer match."""
    for pattern in SECRET_RES:
        text = pattern.sub(lambda m: m[1] + MASK, text)
    return text


def preview(text, limit):
    """Masked, then cut to limit characters."""
    text = mask(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def sensitive(path):
    """Whether a path names a file that may hold secrets."""
    path = str(path or "").strip()
    if not path:
        return False
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return (any(p.fullmatch(name) for p in SENSITIVE_NAME_RES)
            or any(p.search(path) for p in SENSITIVE_PATH_RES))


def names_sensitive(command):
    """Whether a shell command names a file that may hold secrets. The transcript doesn't say which
    files a command read, so this looks only at what the command says."""
    return any(sensitive(word) for word in WORD_SPLIT_RE.split(command or "") if word)


# --- times ---

def iso(when):
    """A Unix time as the feed writes times: ISO 8601 in UTC, to the millisecond."""
    return datetime.fromtimestamp(when, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def entry_time(entry):
    try:
        return datetime.fromisoformat(str(entry.get("timestamp")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


# --- the transcript ---

class StepBook:
    """The tool calls seen and not yet answered, so a result is shown (or hidden) as its call was."""

    KEPT = 200

    def __init__(self):
        self.open = {}  # tool_use id -> {"tool", "skip", "hidden", "title", "input_preview"}

    def note(self, use_id, facts):
        self.open[use_id] = facts
        while len(self.open) > self.KEPT:
            self.open.pop(next(iter(self.open)))

    def take(self, use_id):
        return self.open.pop(use_id, None)

    def running(self):
        """The tool of the latest call still waiting for its result, or None."""
        facts = next((f for f in reversed(self.open.values()) if not f["skip"]), None)
        return facts["tool"] if facts else None


def from_entry(entry, steps, client_ids=None):
    """The events a transcript entry makes (steps: the StepBook of this transcript; client_ids:
    {Telegram message id: client id} of the person's Mini App messages)."""
    if entry.get("isSidechain"):
        return []  # a subagent's own work, which shows as the one step that ran it
    kind = entry.get("type")
    if kind == "assistant":
        return [e for block in content_blocks(entry) if block.get("type") == "tool_use"
                for e in [step_called(block, entry, steps)] if e]
    if kind == "user":
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str):
            return typed_message(content, entry, client_ids)
        events = []
        for block in content_blocks(entry):
            if block.get("type") == "tool_result":
                events += [e for e in [step_answered(block, entry, steps)] if e]
            elif block.get("type") == "text":
                events += typed_message(block.get("text") or "", entry, client_ids)
        return events
    if kind == "queue-operation" and entry.get("operation") == "enqueue" and isinstance(entry.get("content"), str):
        return typed_message(entry["content"], entry, client_ids)  # typed while it worked
    return []


def content_blocks(entry):
    content = (entry.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def typed_message(text, entry, client_ids=None):
    """A message someone sent the assistant, from the header the router typed before it."""
    m = HEADER_RE.match(text)
    if not m or m["app"] not in SOURCES:
        return []  # the router's own notes, a control panel request, Claude Code's own entries
    what, msg = m["what"], MSG_ID_RE.search(m["what"])
    if what.startswith("reaction") or "tapped a button" in what:
        return []
    msg_id = msg[1] if msg else ""
    source = "crosschat" if re.search(r"\bin \S+'s chat\b", what) else SOURCES[m["app"]]
    body = (m["body"] or "").strip("\n")
    event = {"kind": "user_message", "key": user_key(msg_id, entry), "ts": iso(entry_time(entry)),
             "msg_id": msg_id, "text": body[:TEXT_MAX], "source": source, "state": "delivered"}
    if source == "crosschat":
        event["from"] = m["who"].strip()
    if client_id := (client_ids or {}).get(msg_id):
        event["client_id"] = client_id
    return [event]


def user_key(msg_id, entry=None):
    return f"u:{msg_id}" if msg_id else f"u:t{entry.get('timestamp') if entry else ''}"


def step_called(block, entry, steps):
    """A step for a tool call, or None for a tg call (what it sent comes from the outbox)."""
    tool, data = str(block.get("name") or "tool"), block.get("input") if isinstance(block.get("input"), dict) else {}
    command = str(data.get("command") or "")
    skip = tool == "Bash" and bool(TG_CALL_RE.search(" " + command))
    hidden = (tool in FILE_TOOLS and sensitive(data.get("file_path") or data.get("notebook_path"))
              or tool == "Bash" and names_sensitive(command))
    use_id = str(block.get("id") or "")
    hides_input = hidden and tool not in ("Read", "Bash")  # a path or a command is shown; written content isn't
    facts = {"tool": tool, "skip": skip, "hidden": hidden, "title": step_title(tool, data),
             "input_preview": HIDDEN if hides_input else preview(input_text(tool, data), INPUT_MAX)}
    steps.note(use_id, facts)
    if skip:
        return None
    return {"kind": "step", "key": f"s:{use_id}", "ts": iso(entry_time(entry)), "tool": tool, "title": facts["title"],
            "input_preview": facts["input_preview"], "status": "running"}


def step_answered(block, entry, steps):
    """The step a tool result finishes, or None when its call was a tg call or isn't known."""
    use_id = str(block.get("tool_use_id") or "")
    facts = steps.take(use_id)
    if not facts or facts["skip"]:
        return None
    result = HIDDEN if facts["hidden"] else preview(result_text(block.get("content")), RESULT_MAX)
    return {"kind": "step", "key": f"s:{use_id}", "ts": iso(entry_time(entry)), "tool": facts["tool"],
            "title": facts["title"], "input_preview": facts["input_preview"], "result_preview": result,
            "status": "error" if block.get("is_error") else "ok"}


def step_title(tool, data):
    """One line saying what a step did: "Ran `npm test`", "Edited `app.js`", "Ran agent `review`"."""
    def code(text):
        text = " ".join(str(text).split())
        text = text if len(text) <= TITLE_MAX else text[:TITLE_MAX - 1] + "…"
        return f"`{mask(text).replace('`', chr(39))}`"

    if tool in SUBAGENT_TOOLS:
        return f"Ran agent {code(data.get('description') or data.get('subagent_type') or 'task')}"
    if tool == "Bash":
        return f"Ran {code((str(data.get('command') or '').strip().splitlines() or [''])[0])}"
    if tool in FILE_TOOLS:
        return f"{FILE_TOOLS[tool]} {code(short_path(data.get('file_path') or data.get('notebook_path') or ''))}"
    if tool in ("Glob", "Grep"):
        return f"Searched for {code(data.get('pattern') or '')}"
    if tool == "WebSearch":
        return "Searched the web" + (f" for {code(data['query'])}" if data.get("query") else "")
    if tool == "WebFetch":
        return f"Fetched {code(data.get('url') or '')}"
    if tool == "TodoWrite":
        return "Updated its plan"
    if tool == "Skill":
        return f"Loaded the {code(data.get('skill') or data.get('command') or '')} skill"
    if tool.startswith("mcp__"):
        server, _, name = tool[5:].partition("__")
        return f"Used {code(server)} · {code(name or server)}"
    return f"Used {code(tool)}"


def short_path(path):
    """A path with the home folder as ~ and the workspace's own part only where it's long."""
    path = re.sub(r"^/home/[^/]+", "~", str(path))
    return path if len(path) <= TITLE_MAX else "…" + path[-(TITLE_MAX - 1):]


def input_text(tool, data):
    if tool == "Bash":
        return str(data.get("command") or "")
    if tool == "Write":
        return str(data.get("content") or "")
    if tool == "Edit":
        return str(data.get("new_string") or "")
    if tool in SUBAGENT_TOOLS:
        return str(data.get("prompt") or "")
    return json.dumps(data, ensure_ascii=False)


def result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") if b.get("type") == "text" else f"[{b.get('type', 'part')}]"
                         for b in content if isinstance(b, dict))
    return ""


# --- the outbox ---

def from_outbox(record, own_chat, names=None):
    """The event a chat outbox record (outbox.py) makes: an answer, or an edit of one. own_chat:
    the person's own chat; a message the assistant sent anyone else shows as sent to them
    (names: {chat id: display name})."""
    msg_id, ts = str(record.get("msg_id") or ""), iso(float(record.get("at") or time.time()))
    if record.get("kind") == "edit":
        return {"kind": "message_edited", "key": f"a:{msg_id}", "ts": ts, "msg_id": msg_id,
                "markdown": str(record.get("markdown") or ""), "copy": list(record.get("copy") or [])}
    event = {"kind": "assistant_message", "key": f"a:{msg_id}", "ts": ts, "msg_id": msg_id,
             "markdown": str(record.get("markdown") or ""), "files": list(record.get("files") or []),
             "buttons": list(record.get("buttons") or []) if record.get("kind") == "ask" else [],
             "copy": list(record.get("copy") or [])}
    if record.get("kind") == "checklist":
        event["checklist"] = list(record.get("buttons") or [])
    chat = str(record.get("chat") or own_chat)
    if chat != str(own_chat):
        event["to"] = (names or {}).get(chat) or chat
    return event


def fit(event):
    """event cut to EVENT_MAX bytes of JSON: a long message's text (or a step's previews) shortened,
    with a note where it was cut."""
    if size(event) <= EVENT_MAX:
        return event
    event = dict(event)
    field = next((f for f in ("markdown", "text", "result_preview", "input_preview") if event.get(f)), None)
    if field is None:
        return event
    over = size(event) - EVENT_MAX
    text = event[field]
    keep = max(0, len(text) - over - len(CUT_NOTE.encode()) - 64)
    while keep and size({**event, field: text[:keep] + CUT_NOTE}) > EVENT_MAX:
        keep = max(0, keep - max(64, (size({**event, field: text[:keep] + CUT_NOTE}) - EVENT_MAX)))
    event[field] = text[:keep] + CUT_NOTE
    return event


def size(event):
    return len(json.dumps(event, ensure_ascii=False).encode())
