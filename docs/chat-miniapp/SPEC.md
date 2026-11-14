# Chat Mini App: specification

Status: draft 4, 2026-11-14 (self-review R1–R15 and review round 2 by other models, M1…M14, from [REVIEW.md](REVIEW.md) applied; open questions decided, section 11). Owner: Hustilin-Y. Target: the user's fork `Hustilin-Y/multi-claude-eco`, later offered upstream.

## 1. Goal

Give each person on the bot a chat screen inside Telegram that looks and works like a current AI chat app (Claude.ai, ChatGPT): a message box with model and effort pickers, answers rendered as rich Markdown, the assistant's work steps visible as it runs, a stop button, and a new-chat button. It talks to the same Claude Code assistant as the Telegram DM, so the person can switch between the DM and the Mini App at any moment without losing anything.

### 1.1 Why a Mini App when the DM already works

- The DM shows only what the assistant chose to `tg send`. The person can't see what it is doing between messages beyond "typing…".
- Model and effort are changed with `/model` and `/effort` commands or in the control panel, away from where the message is written.
- Telegram messages are capped at 4096 characters, have no syntax highlighting, and long answers arrive as several messages.
- The control panel is already a Mini App served by the relay; a chat tab next to it reuses its sign-in, transport and theme.

### 1.2 Non-goals

- Token-by-token streaming. The assistant runs in a tmux Claude Code session; the router sees its work only through the JSONL transcript, one content block at a time. Progress is shown per step (section 6.4), not per token.
- Several parallel conversations per person. Each person has one Claude Code session. "New chat" is `/clear`; past conversations are read-only (P2, section 5).
- Seeing another person's assistant. Every view is the signed-in person's own.
- Replacing the Telegram DM. The DM keeps working unchanged; the Mini App is another window onto the same conversation.
- A native app, a web app outside Telegram, or sign-in other than Telegram's.

## 2. Context: what exists today

| Piece | Today | What the Mini App uses |
| --- | --- | --- |
| Assistant | One tmux Claude Code session per person (`u-<name>`), may be asleep (`hibernate_after_minutes`) | Unchanged; receives Mini App messages like any other |
| Router | `bin/watcher.py`; queues inbound messages into the session, tails transcripts, handles `/clear`, `/stop`, `/model`, `/effort`, `/usage`; on workshop it polls Telegram itself (`"frontdesk": "router"`) | New inbound source `chat` envelopes; new outbound feed built from the transcript |
| Replies | The assistant calls `tg send/ask/edit` (Bash tool) → Bot API → DM | Unchanged; the Mini App shows the same replies, read from the transcript |
| Relay | Cloudflare Worker + one Durable Object (SQLite) in `relay/`; serves the control panel at `/app/`, checks Telegram's init data, queues envelopes, holds the router's WebSocket | Hosts the chat page and its API |
| Control panel | `relay/public/app/` (vanilla JS, no build step), long-polls `/app/state` for a snapshot, posts taps to `/app/action` | The chat becomes a tab of this page |
| Model and effort | `switching.py`: models `opus, sonnet, haiku, fable`; efforts `low, medium, high, xhigh, max`; session-only or saved with `default` | The composer's pickers send the same switches |

**Prerequisite on workshop:** the relay is not deployed there yet (`config.json` has no relay settings). It needs a free Cloudflare account and Node.js 22 on the machine that deploys it (`assistant relay deploy`), at the Worker's `*.workers.dev` address (Q1, Q1a). P1 attachments also need R2 turned on in that account (Q6).

## 3. Users and roles

- **Member:** a paired person (`users.json`, role `member`). Uses the chat with their own assistant.
- **Owner:** role `owner`. Same as a member, plus the machine's state in the header (asleep/awake count, RAM) as the panel already shows.
- **Stranger:** anyone else who opens the page (link forwarded, opened from another bot). Sees "This bot doesn't know you yet" and nothing else.

## 4. Architecture

```
Telegram client (Mini App page, relay/public/app/)
   │  HTTPS, "Authorization: tma <initData>"
   ▼
Relay Worker (relay/src/chat.js) ── Durable Object (SQLite)
   │  "chat" envelopes down          ▲  feed events up (PUT, relay token)
   ▼                                 │
Router (lib/multiclaude/router/chat.py) — WebSocket /queue (existing)
   │  queues Inbound(app="Mini App")  ▲  reads transcript entries (existing Tail)
   ▼                                  │
Assistant's tmux Claude Code session ─┘  replies with tg send → Telegram DM too
```

### 4.1 Sending a message

1. The page posts `{client_id, text, attachments?}` to `POST /app/chat/send`; `client_id` is a random UUID made by the page. Messages carry no model or effort: a pick in the composer's sheet is sent at once as the panel's existing `model`/`effort` tap (R6), and the router's per-session queue puts it before any message sent after it.
2. The Worker checks the init data signature (≤ 1 h old for sending, R3), that the Telegram id is a person on the bot (M3), and size limits, and queues a `chat` envelope with `envelope_id = chat:<telegram id>:<client_id>` (M5) and `queued_at`, the relay's time.
3. The router checks the signature again (`panel.signed_sender`), and that `auth_date` was at most 1 h before `queued_at` and `queued_at` at most 24 h ago, so a message that waited at the relay while the machine was offline still goes (N8, M1). It drops a `client_id` it already took for that person in the last 24 h (`state/chat/seen/<name>`), since the relay forgets an envelope once acknowledged and a page retry after that would queue it again (M6). Then it finds the person, and:
   - posts the text silently into the person's DM as "💬 <text>" (`disable_notification`), as the Siri add-in does with "🎙️", so the DM shows the whole conversation (R2). A text over 3,500 characters is cut there to its first 3,000 with "… (full text in the Mini App)", since a Telegram message holds 4,096; the assistant still gets all of it (M4). A message with only attachments posts "💬 📎 <file names>";
   - wakes the assistant if it is asleep;
   - queues an `Inbound` with `app="Mini App"` and header `[Mini App from <Name> · msg <telegram message id of the 💬 copy> · <time>]`, so `tg send --reply-to`, reactions and quoting work as for a DM message.
4. The router puts a `user_message` event (state `queued`, then `delivered` when typed in) on the feed.

### 4.2 Showing the answer

Two sources, both on the machine:

- **Outbox (answers):** `tg send`, `tg ask`, `tg edit` and `tg checklist` append each message they sent (Telegram message id, final Markdown, files, buttons, copy buttons, target chat) to `state/chat/outbox/<name>.jsonl`, after the Bot API call succeeded; a failure to write is logged and never fails the send. Parsing the Bash command instead would miss text piped on stdin (`tg send <<'EOF'`, which the assistants' rules recommend for long answers), variables and `$(…)` (R1).
- **Transcript (steps and status):** only the session's current transcript, the one the router already treats as the session's. Job runs (`claude -p` in the same folder) and subagents' own transcripts are left out; a subagent shows as one step "Ran agent `<description>`" (R4).

The feed is built only on demand (R5). When the page opens, its first `feed` read (`after: 0`) makes the relay queue a `chat_open` envelope (no separate route, M9); the router answers with a backfill of the last 50 events, built from the outbox and the current transcript, then keeps turning new entries into events while that page polls (a `feed` read in the last 2 minutes), PUTting them to the relay batched every ≤1 second. Nothing is pushed for people whose page is closed. Opening, polling and backfill read files only and never wake a sleeping assistant (R9). The page long-polls `POST /app/chat/feed {after: seq}` (held up to 25 s, like the panel's `/app/state`). A WebSocket from the page is a later optimisation (Q4).

The assistant answers a Mini App message the same way as a Telegram one: `tg send`. Its rules get one line (section 9): a `[Mini App …]` message is answered with `tg`, as usual. So every answer lands in the DM, under the 💬 copy of the question, and in the Mini App, rendered from the same Markdown.

### 4.3 Why the transcript and not a new reply channel

- No change to how assistants reply, so nothing breaks in the DM, Slack or jobs; `tg` only also records what it sent.
- The transcript also carries the work steps (tool calls, results, subagents), which a reply channel would not.
- The router already reads it; the cost is formatting, not a new watcher. No new process on a 3 GB machine.

## 5. Features and priorities

P0 is the first release; P1 follows right after; P2 is later and may be dropped.

| ID | Feature | Pri |
| --- | --- | --- |
| F1 | Chat tab in the existing Mini App, next to the panel; opens on the chat by default when launched from the new "Chat" menu button or `/chat` | P0 |
| F2 | Composer: multi-line text box that grows to 40% of the viewport, Send button, Enter sends on desktop, Shift+Enter new line; on mobile Enter is a new line | P0 |
| F3 | Model picker in the composer (opus, sonnet, haiku, fable), showing the current one; change applies to this session, with "keep after restarts" as in the panel | P0 |
| F4 | Effort picker (low, medium, high, xhigh, max); `max` is never kept after restarts | P0 |
| F5 | Message list: the person's messages and the assistant's `tg send` texts as bubbles, Markdown rendered (headings, lists, tables, code blocks with highlighting and a Copy button, collapsible sections) | P0 |
| F6 | Status line while a turn runs: "Waking up…", "Thinking…", "Running: `<tool>`", with elapsed time | P0 |
| F7 | Stop button in place of Send while a turn runs; sends `/stop` | P0 |
| F8 | New chat button: confirms, then `/clear`; the list empties | P0 |
| F9 | History on open: the last 50 events of the current conversation, more on scroll up (up to the start of the current transcript) | P0 |
| F10 | Telegram theme (light/dark from `themeParams`, re-applied on the `themeChanged` event), safe areas (`--tg-safe-area-inset-*` and `--tg-content-safe-area-inset-*`, Mini App API 8.0+; `env(safe-area-inset-*)` before that), full-height viewport (`expand()`; `requestFullscreen()` only on 8.0+), BackButton closes sheets | P0 |
| F11 | Delivery states on the person's messages: sending, queued, delivered, failed (with Retry) | P0 |
| F12 | Work steps: each tool call as a collapsed row ("Ran `npm test`", "Edited `app.js`", "Searched the web"), expandable to its input and a trimmed result (first 2 KB) | P1 |
| F13 | Buttons from `tg ask` rendered under the question; a tap goes back as the same tap the DM would send | P1 |
| F14 | Approval dialogs (`router/approvals.py`) shown inline with Approve / Deny | P1 |
| F15 | Slash commands: typing `/` opens a list of the router's commands (`/clear`, `/stop`, `/usage`, `/model`, `/effort`, `/mcp`, `/skills`, `/help`) | P1 |
| F16 | Context and plan meter by the composer (from the panel snapshot): context %, 5-hour and weekly limit %; warns at 80% | P1 |
| F17 | Draft kept per person in Telegram `CloudStorage` (survives closing the app and switching devices). A value there holds at most 4,096 characters, so a longer draft is split over keys `draft.0`…`draft.3` (16,000 characters at most, the composer's limit) (M7) | P1 |
| F18 | Attachments: photos and files up to 10 MB, picked or pasted; they reach the assistant's inbox as DM photos do | P1 |
| F19 | Saved prompts: the person keeps up to 20 short prompts in `CloudStorage` and inserts one with a tap | P2 |
| F20 | Voice input: hold to record; transcribed by the voice-notes add-in when it is on | P2 |
| F21 | Past conversations: list of earlier transcripts with their first message and date, read-only | P2 |
| F22 | Export the current conversation as a Markdown file sent to the DM | P2 |
| F23 | Search in the current conversation | P2 |
| F24 | Haptic feedback on send, stop and errors | P2 |

## 6. Detailed requirements

### 6.1 Sign-in and access

- Every page request carries `Authorization: tma <initData>`. The Worker and the router both check the signature with the bot's key. Reads accept init data up to 24 h old (as the panel does). Sending a message, uploading, and answering an approval accept it only up to 1 h old, since a chat message runs commands as a full admin of the machine (R3); past that the page shows "For safety, reopen the chat to keep sending" and keeps the draft.
- The Telegram user id must belong to a person in `users.json`. The Worker can't read `users.json`, so the router PUTs the list of people's Telegram ids to the relay at start and whenever `users.json` changes (`PUT /chat/members`, relay token), and the Worker answers every chat route for anyone else with HTTP 403, before anything is queued or stored (M3). The page then shows the "doesn't know you" screen. The router still checks the person itself.
- A person can read and write only their own feed. The feed key is the Telegram id taken from the checked init data, never from the request body.
- The page never receives other people's names, ids or messages, and never the bot token, relay token or any key.

### 6.2 Composer

- Text limit: 16,000 characters; the counter appears from 14,000. Over the limit Send is disabled with the reason shown.
- Empty or whitespace-only text with no attachment can't be sent.
- Send while offline: the message stays in the list as "failed" with Retry; the draft is not lost.
- Model and effort pickers are chips above or inside the composer (as in Claude.ai). Each opens a bottom sheet with the options taken from the snapshot (`models`, `efforts`, already sent by `panel.py`), the current one ticked, a one-line hint per option (e.g. "haiku — fastest, cheapest"), and a "Keep after restarts" switch. An effort level the router has refused for the current model is greyed out with the router's reason (R11).
- A pick is sent at once as the panel's `model`/`effort` tap (R6). While a turn runs, it takes effect after that turn, before the next message; the chip shows the new value with a "pending" dot until a `settings` event confirms it.
- If the switch fails (the router's result doesn't start with "Set model to"/"Set effort level to"), the chip goes back to the old value and the router's error text is shown under the composer.

### 6.3 Messages

- The person's messages: right-aligned bubbles, plain text with line breaks kept, time on long-press/hover.
- The assistant's messages: left-aligned, full width, no bubble background (as in current chat apps), Markdown rendered with a sanitiser; links open with `Telegram.WebApp.openLink`; `tg send --file` attachments show as file chips (name, size, type). Telegram has no links to a message in a private chat, so a tap on a chip opens the bot's DM (`openTelegramLink("https://t.me/<bot username>")`), where the file is, and says so in a toast (M8).
- A message the assistant later edits with `tg edit` is updated in place.
- Messages that came from the DM (typed in Telegram) also appear, marked with a small "from Telegram" label (or "from Slack"), so the history is the whole conversation.
- `tg send --copy` buttons render as buttons that copy their text; `tg checklist` renders as a list with ticks, tappable through the same path as F13 (R13).
- Messages the assistant sent to another person (`tg --chat <person>`) show as collapsed "Sent to <Name>" cards; messages from another person's assistant show as "From <Name>'s assistant" (R10).
- Router notices that the person would see in the DM (switch results, limit warnings, `/clear` notes) appear as centred system lines.

### 6.4 Feed events (router → relay → page)

```json
{"seq": 1042, "at": "2026-11-14T10:00:00Z", "turn": "t-77", "kind": "...", ...}
```

| kind | fields | from |
| --- | --- | --- |
| `user_message` | `client_id`, `msg_id`, `text`, `source` (`miniapp`/`telegram`/`slack`/`crosschat`), `state` | the queue, or the transcript's user entry and its header |
| `assistant_message` | `msg_id`, `markdown`, `files[]` (`name`, `size`, `mime`), `buttons[]`, `copy[]`, `checklist?`, `to?` (another person's name) | the outbox |
| `message_edited` | `msg_id`, `markdown` | the outbox (`tg edit`) |
| `step` | `tool`, `title`, `input_preview` (≤500 chars), `result_preview` (≤2 KB), `status` (`running`/`ok`/`error`) | tool_use and tool_result entries, other than `tg` calls; a subagent is one step |
| `status` | `state` (`asleep`/`waking`/`idle`/`thinking`/`tool`/`waiting_approval`), `since` | the router's view of the session |
| `approval` | `question_id`, `text`, `answered` | `router/approvals.py` |
| `settings` | `model`, `effort`, `context_pct`, `limits` | the status-line snapshot |
| `notice` | `text` | router notices to the person |
| `cleared` | — | `/clear` or `/restart` |

- `seq` is per person, strictly increasing, assigned by the router and kept in `state/chat/<name>.seq` so it survives restarts; `/clear` does not reset it. A page counts a gap only between two events it received (the next `seq` is more than one past the last it has); its first read takes whatever comes (M10).
- Thinking blocks are never sent to the page (they can hold anything the model considered, and they're not shown in the DM either).
- Tool inputs and results are masked, then trimmed, before leaving the machine; masking runs on the full text so a cut can't split a secret past the patterns (R7, M12):
  - values after `(?i)(token|secret|password|passwd|api[_-]?key)\s*[:=]`, `Bearer \S+`, `sk-[A-Za-z0-9_-]{20,}`, JWT-shaped strings (`eyJ…\.…\.…`) and PEM blocks become `•••`, in inputs and results alike;
  - sensitive paths are `.env*`, `*credential*`, `*secret*`, `id_*`, `*.pem`, `*.key`, `~/.claude/channels/**` and `~/assistant/.env`. The transcript doesn't say which files a Bash command touched, so the check is on what it does show: a Read, Write or Edit whose `file_path` matches, or a Bash command whose text names a matching path. For those, both the input preview (Write/Edit content) and the result preview are replaced as a whole with "[hidden: may contain secrets]" (M11). A command that prints a secret without naming such a file (`printenv`, `git diff`) is caught only by the value patterns above.
- Every event carries `at` (router time); the page measures send→queued and entry→shown from it (N3, N4) and shows the last 50 timings to the owner in a debug sheet (R12).

### 6.5 Status and stop

- While the session is asleep, the first message shows "Waking up…" until the assistant's first transcript entry, then "Thinking…".
- Stop (F7) sends `/stop`; the button shows a spinner until the status becomes `idle`; after 15 s without that, it shows "Couldn't stop — try again".
- A turn longer than 10 minutes keeps its status; nothing times out on the page.

### 6.6 New chat

- Confirm sheet: "Start a new chat? The assistant forgets this conversation; its memory files and your folder stay." Buttons: "New chat", "Cancel".
- On confirm: `/clear` through the router (same as the DM; it also empties the DM as today). The feed gets `cleared`; the page empties and shows the empty state with 4 suggestion chips (editable later in F19).

### 6.7 Relay storage

- The relay is a cache, not an archive (R5). The Durable Object keeps per person at most the last 200 feed events; older history (F9, scrolling up) is asked from the router with a `chat_history` envelope and answered like Siri's requests (`askVm`), when the machine is online.
- `cleared` drops that person's events.
- Events older than 24 hours are dropped by an alarm.
- Each event is at most 16 KB after trimming; a bigger `assistant_message` is cut with "… (full text in Telegram)".

### 6.8 Limits and abuse

- `POST /app/chat/send`: at most 20 messages a minute per person (DO counter); over that, HTTP 429 and "Slow down a bit".
- Request body at most 64 KB (text); attachments go to `POST /app/chat/upload` (F18), at most 10 MB each and 20 MB pending per person, stored in an R2 bucket (`chat-uploads`, key `<telegram id>/<upload id>`, Q6) until the router fetches them with the relay token, then deleted; an R2 lifecycle rule deletes anything older than 1 day (R8). A failed upload shows on its chip as "failed" with Retry, and the message can't be sent until each chip is uploaded or removed; a file the router can't fetch is reported to the person as a system line (M13). R2's free tier (10 GB stored, 1 M writes and 10 M reads a month) is far above what N7 needs. Fallback if R2 can't be turned on: Durable Object storage in ≤1 MB chunks (a SQLite value is capped at 2 MB).

## 7. Non-functional requirements

| ID | Requirement |
| --- | --- |
| N1 | No new long-running process on the machine; the router's added memory is under 20 MB with 4 people. The machine has 3 GB, so no step may hold a whole file whose size grows with use (N1a–N1c) |
| N1a | Backfill and older history read the session's transcript from its end only, at most the last 4 MB (`transcript.RECENT`, as start-up recovery does), and look further back only for `chat_history`, again 4 MB at a time; never the whole file (a transcript once grew to 240 MB and took 750 MB to read) |
| N1b | Uploads are fetched from the relay straight to the person's inbox on disk, in chunks of at most 1 MB; the router never holds a whole file in memory |
| N1c | The outbox is read from its end like the transcript and rotated at 5 MB (one old file kept); feed events waiting for the next PUT are capped at 200 per person, the oldest dropped first, with a `feed gap` the page fills by re-reading |
| N2 | Page weight under 300 KB (gzip) without the Telegram script; no build step, no framework, as the panel; Markdown and highlighting libraries vendored, not from a CDN at runtime |
| N3 | From Send to "queued" on the page: under 1.5 s at the 90th percentile with the machine online, measured by the page (6.4) |
| N4 | From a transcript or outbox entry to its event on the page: under 3 s at the 90th percentile, measured from the event's `at` |
| N11 | Two devices open at once see the same feed; a message sent on one shows on the other |
| N5 | Works on Telegram iOS, Android, Desktop (Windows/macOS) and Web K/A, Mini App API 7.0+; fullscreen and Telegram's safe-area variables need 8.0 and are skipped below it; without CloudStorage the draft is kept in `localStorage` |
| N6 | Usable at 320 px width; touch targets at least 44 px; text contrast WCAG AA in both themes |
| N7 | The Worker stays within Cloudflare's free plan with 5 people sending 300 messages a day. Requests: about 1,200 long-poll reads per person per 8-hour day plus sends and the router's PUTs, well under 100,000 a day. Duration: the relay has one Durable Object for everyone and duration is billed per instance while it is in memory, so even awake all day it is 86,400 s × 128 MB = 10,800 GB-s, under the free plan's daily allowance; long-polls held in it add no instances (checked in review round 2). Watch the Workers dashboard in the first week |
| N8 | Machine offline: sent messages wait at the relay for up to 24 h and go in order when it's back; the page says "The machine is offline, your message will go when it's back" |
| N9 | English UI strings, kept in one table so a translation can be added later |
| N10 | Tests: Worker routes and storage in `relay/test`, router feed building and envelope handling in `tests/`, run by `./check` |

## 8. Error states

| Situation | Page shows |
| --- | --- |
| Not a person on the bot | "This bot doesn't know you yet. Message it in Telegram to get a pairing code." |
| Init data over 24 h old | "This window is too old. Close it and open the chat again." |
| Init data over 1 h old, on Send | "For safety, reopen the chat to keep sending." Draft kept |
| Relay unreachable | Offline banner; messages queue locally and retry with backoff (1, 2, 4… up to 30 s) |
| Machine offline (router not connected for over 30 s) | "The machine is offline" banner; sending still allowed (N8) |
| Assistant failed to start | System line with the router's text; Retry |
| Approval timed out (Claude Code denies after 2 min) | The approval card says "Timed out — denied" |
| Feed gap (seq jumped) | Page re-reads the last 50 events |

## 9. Changes by component

- **relay/src/chat.js** (new): `/app/chat/send`, `/app/chat/feed` (its first read queues `chat_open`), `/app/chat/upload`, `/app/chat/history`; from the router `PUT /chat/<telegram id>` (events), `PUT /chat/members` and `GET`/`DELETE /chat/upload/<id>`; DO tables `chat_events(user, seq, body, at)`, `chat_members(user)`, `chat_rate(user, minute, count)`, `chat_uploads(user, id, size, at)`.
- **relay/wrangler.toml**: the R2 bucket binding `CHAT_UPLOADS`. The add-in's `relay-deploy` creates the bucket and its 1-day lifecycle rule with `wrangler r2 bucket create` and `wrangler r2 bucket lifecycle add`, since `wrangler.toml` can't hold lifecycle rules (M14).
- **relay/public/app/**: tab bar (Chat / Panel), `chat.js`, `chat.css`, vendored `marked` + `DOMPurify` + a small highlighter.
- **lib/multiclaude/router/chat.py** (new): envelope handling with the `queued_at` check and the per-person `client_id` dedupe, the members list PUT to the relay, the silent 💬 copy in the DM, backfill and feed building from the outbox and the session's transcript, secret masking, `seq` keeping, PUT with retries.
- **bin/tg**: after each successful send, ask, edit and checklist, append a record to `state/chat/outbox/<name>.jsonl` (rotated at 5 MB, emptied on `/clear`); never fails the command.
- **router**: `Inbound.app = "Mini App"`; `panel.set_menu_button` gets a "Chat" option (Q3).
- **system/workspaces-CLAUDE.md**: one line — a `[Mini App from …]` message is answered with `tg`, as a Telegram one; keep using Markdown.
- **Packaging:** an add-in `chat` (`addons/chat/`, off by default) with `relay_envelopes = ["chat"]` and relay routes in `ADDON_ROUTES`, so upstream can take it without turning it on for everyone. When implemented, this spec's content moves into `addons/chat/README.md`, `addons/chat/rules.md` and an ARCHITECTURE.md section, as the other add-ins do (R15).

## 10. Rollout

1. Deploy the relay on workshop (Q1) and check the existing panel works.
2. P0 behind the add-in, owner only (`users.json` `chat_beta: true`) for a week.
3. All people on the bot; then P1.
4. Offer upstream as a PR after P1 has run for two weeks.

## 11. Decisions

All questions of draft 2 were decided by the owner on 2026-11-14.

| # | Question | Decision |
| --- | --- | --- |
| Q1 | Where the page is served | The Cloudflare relay (free Workers plan), as the panel is; the machine stays closed. The GitHub Student Pack was checked: its cloud credits (Azure, DigitalOcean, Heroku) expire after a year and give nothing the free Worker doesn't |
| Q1a | The page's address | The Worker's own `*.workers.dev` address. A custom domain (e.g. the Student Pack's free `.me`, which is paid after its first year) can be added later without code changes |
| Q2 | Conversation text on Cloudflare | Cached up to 24 h, trimmed and masked, only while the Mini App is used; `/clear` deletes it (6.7) |
| Q3 | Menu button | "Chat"; the panel is the neighbouring tab, and `/config` still opens the panel |
| Q4 | Page transport | Long-poll in P0, like the panel; WebSocket only if N4 (3 s) isn't met |
| Q5 | Upstream | An add-in, off by default |
| Q6 | Where uploads wait | R2 (6.8). To check when deploying: whether Cloudflare asks for a payment method to turn R2 on. If the owner won't add one, fall back to Durable Object chunks, which needs no card |
