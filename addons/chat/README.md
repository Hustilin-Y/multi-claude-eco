# Chat

A chat screen in the bot's Mini App, next to the control panel: a message box with model and effort pickers, answers rendered as rich Markdown with highlighted code, a status line while the assistant works, a Stop button and a New chat button. It talks to the same assistant as the person's Telegram chat, so they can switch between the two at any moment: every message from the Mini App is also posted, silently, in their Telegram chat as "💬 …", and every answer arrives in both.

While it's on, the button by every private chat's message box says **Chat** and opens the Mini App on the chat; **Panel** is the neighbouring tab, and `/config` still opens the control panel. `/chat` answers with a button that opens it too.

## What it needs

- The relay, set up as in the main README ("The control panel and the relay"). It runs on Cloudflare's free plan; nothing on the machine is reachable from the internet.
- Telegram on iOS, Android, Desktop or the web (Mini Apps 7.0 or later; full screen and Telegram's safe-area variables from 8.0).

## Setup

1. `assistant addon enable chat`.
2. `assistant relay deploy`, which mounts the chat's routes on the relay and serves the new page.
3. `assistant restart`, so the router sets the **Chat** menu button.

To try it out with some people first, give them `"chat_beta": true` in `users.json`: while anyone has it, only they can use the chat (the relay answers everyone else 403). Take it out to open it to everyone. Turning the add-in off (`assistant addon disable chat`, `assistant relay deploy`, `assistant restart`) brings the **Panel** button back.

## How it works

- **Sending:** the page posts the message to the relay (`/app/chat/send`), which checks Telegram's signature (at most an hour old for sending, a day for reading, since a message runs commands as a full admin of the machine), that the sender is on the bot, and at most 20 messages a minute, then queues it for the router as a `chat` envelope stamped with when it was queued. The router checks the signature again, against when the relay queued it, so a message that waited at the relay while the machine was offline still goes within a day, and drops a message it already took (a page's retry). Then it posts the "💬" copy in the person's Telegram chat (cut to its first 3,000 characters past 3,500, since Telegram holds 4,096; the assistant gets it all) and types the message into their assistant under a `[Mini App from <Name> · msg <id of the copy> · <time>]` header. A message never switches the model or effort; the chips send the control panel's own taps. Stop and New chat send `/stop` and `/clear`, which the router carries out as from Telegram.
- **Answers:** `tg send`, `ask`, `checklist` and `edit` pass what Telegram took to the add-in (`tg_sent`), which appends it to `state/chat/outbox/<name>.jsonl` (the old one kept as `.jsonl.1` past 5 MB; both emptied by `/clear`). A failure there is logged and never fails the send. A job's messages (`ASSISTANT_JOB`, set by `assistant job`) and what `assistant notify`, cron and scripts send aren't kept.
- **The feed:** while the page is open, the router builds what it shows from the outbox and the session's current transcript (the one its status line names; never a job's or a subagent's, and a subagent shows as one step) and puts it on the relay at most once a second. The page's first read asks for the last 50 events, built afresh, and the router follows while the page reads at least once a minute (the relay passes that on as `chat_open` envelopes). Scrolling up asks the router for older history (`chat_history`), read from the transcript a window at a time. Nothing is built for anyone whose page is closed, and opening or reading never wakes a sleeping assistant.
- **Secrets:** tool inputs and results are masked over their whole text (values after `token=`, `password:` and the like, bearer tokens, `sk-` keys, JWTs, PEM blocks), then trimmed (500 characters of input, 2 KB of result). A step that reads or writes a file whose name says it holds secrets (`.env*`, `*credential*`, `*secret*`, `id_*`, `*.pem`, `*.key`, `~/.claude/channels/`) shows no result, and a write no input either. A command that prints a secret without naming such a file is caught only by the patterns.
- **The relay keeps** each person's last 200 events for a day, as a cache: a backfill replaces them, and `/clear` drops them. Thinking is never shown.

## Extending it from another add-in

The chat asks the add-ins that are on for these hooks, as add-ins offer hooks to each other, and knows each only by the name it's on under. The chat names no other add-in.

| Hook | Gives | The page |
| --- | --- | --- |
| `chat_tabs(user)` | `[{"id", "title"}]` | A tab after Chat and Panel, which loads the add-in's `addons/<name>/tab.js` and calls its `mount(root, api)`: `api.onState(fn)` hears the add-in's state, `api.fill(text)` puts text in the composer (it never sends), `api.toast`, `api.el`, `api.markdown`. The file comes from the add-in's `relay-public/` folder, which `assistant relay deploy` uploads: it's public, so it holds no secrets or addresses, and data reaches it only through the signed feed |
| `chat_models(user)` | `[{"id", "label", "hint", "group", "badge"}]` | More models in the model sheet, under their group. Picking one makes `"<name>:<id>"` the composer's target (kept per device); a Claude model sets it back to the assistant |
| `chat_message(user, target, text, client_id)` | `True` once taken | A message to the target goes to that add-in after the same checks as any message (signature at most an hour old when queued, members, rate, the client id taken once). No 💬 copy, no Inbound, nothing woken. Nobody taking it: a line "That model isn't available now." New chat with a target sends it `/clear`, for its own thread |
| `chat_stop(user, target)` | | Stop while the target answers |
| `chat_actions(user)` | `[{"id", "label", "icon", "template", "note", "form": [{"name", "label", "type", "options", "default", "on", "off"}]}]` | The ⚡ chip lists them; a form (`select`, `text`, `textarea`, `switch`) fills the composer from `template` (`{name}` for each field; a switch puts in its `on` or `off` words) and sends nothing |
| `chat_backfill(user)` | `[event]` | The add-in's own events when a page opens |
| `chat_assist(user, action, field, values)` | `{"text"}` or `{"error"}` | A form field with `"assist": {"label": "✨ Improve"}` gets that button while the add-in offers the hook. A press sends the form's values (a `chat` message of kind `assist`, with every check a message gets); the router runs the hook in the background, one at a time per person, and the answer comes back as an `assist_result` event: the field gets the new text, with Undo, or the page says why not. The field is read-only meanwhile. Nothing goes to the assistant |

`chat.is_open(user)` says whether the person's page reads the feed now (so an add-in builds nothing for a closed one), and `chat.feed_add(user, event)` puts an add-in's event on the person's open page (False when it's closed): `model_message` (`key`, `ts`, `target`, `markdown`, `done`, `meta` with `answered`, `tokens`, `seconds`, `stopped`, `error`), shown like an answer under the model's name and badge; `addon_state` (`addon`, `state`), at most 64 KB, kept by the relay once per add-in, the newest, for that add-in's tab; or `status` with a `target`, `state` (`running` or `idle`), `text` and `since`, which the status line shows while that target is chosen. The tabs, models and actions are asked for every 30 seconds while the page is open, on the router's loop: a hook keeps what's slow to get.

## Memory

The machine has little to spare, so nothing reads a whole file: a backfill or a history request reads at most 3 MB of transcript and 1 MB of outbox from where it should end, skipping any line over 256 KB (a pasted picture) unread, and keeps only the newest 200 events of a window while it reads. At most 200 events wait for the relay per person; when it's unreachable the oldest go, and the page, seeing the gap in their numbers, reads the feed afresh.

## Files

- `lib/multiclaude/addons/chat/`: `__init__.py` (the hooks, messages from the page, the members list), `feed.py` (following a person's conversation and putting it on the relay), `events.py` (transcript entries and outbox records as feed events, masking), `files.py` (the outbox, the messages taken, reading in windows).
- `relay/src/chat.js`: the relay's routes and what the Durable Object keeps.
- `relay/public/app/`: `chat.js` (the tab, and other add-ins' tabs, models and actions), `chatfeed.js` (its state and strings), `markdown.js`, `highlight.js`, `chat.css`, and in `vendor/` marked and DOMPurify.
- State: `state/chat/outbox/`, `state/chat/seen/<name>` (the Mini App messages taken in the last day), `state/chat/<name>.seq` (the feed's last number).

The full design, its acceptance criteria and its reviews are in `docs/chat-miniapp/`; what the implementation does differently is in `docs/chat-miniapp/IMPLEMENTATION-NOTES.md`.
