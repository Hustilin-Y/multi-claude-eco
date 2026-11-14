# Chat Mini App: review of SPEC draft 1 and ACCEPTANCE draft 1

Reviewed 2026-11-14 against the code on branch `memory-rework` (25d9a0a). Each finding was checked in the code where it makes a claim about it. Severity: **High** = the design would not work or opens a hole; **Medium** = wrong or ambiguous behaviour a tester would hit; **Low** = clarity, testability. Status shows what was changed in draft 2.

## High

### R1. Reading `tg send` text from the Bash command is unreliable
SPEC 4.2 and 6.4 build `assistant_message` from the `tg send` Bash call in the transcript. The assistants' rules tell them to pipe long text on stdin (`system/workspaces-CLAUDE.md:37`: `tg send <<'EOF'`), and they also use variables, `$(cat file)` and `--file`. The command string is shell, not the text that was sent; parsing it gives wrong or empty answers, exactly for the long answers the Mini App is meant to show better.
**Fix:** `tg` itself appends each sent/edited/asked message (Telegram message id, final Markdown, files, buttons, copy buttons) to `state/chat/outbox/<name>.jsonl`. The feed reads that file, not the command. Transcript is still the source for steps.
**Status:** fixed in SPEC 4.2, 6.4, 9.

### R2. Answers in the DM to questions the DM never saw
A message sent from the Mini App is answered with `tg send` into the DM, where the question doesn't appear. The DM becomes a list of answers to invisible questions, and `tg send --reply-to` has no Telegram message to point at. The Siri add-in already solves this: it posts the words silently in the chat ("🎙️ …") before queuing them.
**Fix:** the router posts the Mini App message silently into the DM ("💬 …", `disable_notification`) and uses that Telegram message id as `msg` in the header. Reply-to, reactions and quoting then work as for a DM message, and both views show the same conversation.
**Status:** fixed in SPEC 4.1, AC-2.3, AC-7.1.

### R3. A 24-hour-old signature is enough to run commands as admin
Every assistant is a full admin of the machine. The panel trusts init data for 24 h, which is acceptable for switches and settings, but a chat message is arbitrary command execution. Init data that leaks (a screenshot of devtools, a proxy log, a shared HAR file) stays usable for a day.
**Fix:** `/app/chat/send` and `/app/chat/upload` accept init data signed ≤ 1 h ago; reads keep 24 h. Past 1 h the page asks to reopen ("For safety, reopen the chat to keep sending"); the draft is kept (F17). The router checks the same 1 h.
**Status:** fixed in SPEC 6.1, AC-11.4 added.

### R4. Job runs would leak into the chat
The router follows every transcript in the person's project folder. Scheduled jobs (`bin/assistant:477`, `claude -p` in the person's folder) write their own transcripts there, and so do subagents. A feed built from "new transcript entries" would show job runs as if they were part of the chat, contradicting AC-7.3.
**Fix:** the feed reads only the session's current transcript (the one the router already treats as the session's: newest, `--continue`'d). Subagent work shows as one step "Ran agent `<description>`", not its inner steps. AC-7.3 gets an **[auto]** test.
**Status:** fixed in SPEC 4.2, 6.4, AC-7.3.

### R5. History contradicts "feed only while open"
SPEC 4.2 builds feed events only while the Mini App is open, but F9 promises the last 50 events on open, and 6.7 keeps a 7-day ring. Whatever happened while the app was closed would be missing, or the relay would hold every conversation for a week even for people who never open it.
**Fix:** on open the page sends `chat_open`; the router answers with a backfill of the last 50 events built from the current transcript and the outbox, then streams while the page polls. Older history on scroll is asked the same way. The relay keeps events only as a cache: last 200 per person, 24 h, dropped on `cleared`. Nothing is pushed for people with the app closed. Q2 becomes "24 h".
**Status:** fixed in SPEC 4.2, 6.7, 11 (Q2), AC-1.1.

## Medium

### R6. Sending model/effort with every message fights other switches
SPEC 4.1 sends `model`/`effort` with each message and switches when they differ. A chip that is stale (the person switched in the DM a second ago) would switch the session back. AC-3.5 ("sent on opus, high") also needs extra state for little value.
**Fix:** a pick in the sheet is sent at once as the panel's existing `model`/`effort` tap (`router/panel.py` → `switching.switch`). The router's per-session queue already orders it before any message sent after it, also while a turn runs. Messages carry no settings. AC-3.2/3.5/3.6 rewritten.
**Status:** fixed.

### R7. Secret masking is too narrow
The regex in 6.4 catches `key=value` lines but not `cat ~/.ssh/id_ed25519`, `cat .env` with bare values, or a JSON credentials file. These are tool results the assistant routinely produces.
**Fix:** add whole-result redaction ("[hidden: may contain secrets]") for Read/Bash results that touched paths matching `.env*`, `*credential*`, `*secret*`, `id_*`, `*.pem`, `*.key`, `~/.claude/channels/**`, `~/assistant/.env`; plus PEM blocks and JWT-shaped strings in any result. Result previews stay on for everyone: it is the person's own assistant; the masking is there so the relay never stores secrets.
**Status:** fixed in SPEC 6.4, AC-4.4 extended.

### R8. 10 MB uploads don't fit the DO as written
A Durable Object SQLite value is capped at 2 MB; "R2 or DO storage" leaves the choice open, and R2 needs a payment method on the Cloudflare account even on its free tier (to verify when deploying).
**Fix:** the storage is chosen explicitly: at most 10 MB per file and 20 MB pending per person.
**Status:** fixed in SPEC 6.8. Q6 added, then decided by the owner: R2, with DO chunks as the fallback if R2 needs a card the owner won't add.

### R9. Opening the chat must not wake a sleeping assistant
With `hibernate_after_minutes`, waking costs 350–600 MB. Nothing in draft 1 says whether opening the app or the backfill wakes the session.
**Fix:** opening, polling and backfill never wake it (backfill reads files only). Only Send, Stop and an approval tap do. AC-1.6 added.
**Status:** fixed.

### R10. Messages to other people
`tg send --chat <person>` messages are the assistant's actions in this conversation; draft 1 would show them as answers to the person.
**Fix:** they show as "Sent to <Name>" cards, collapsed. Messages that arrive from another person's assistant (crosschat headers) show as "From <Name>'s assistant".
**Status:** fixed in SPEC 6.3.

### R11. Effort options per model
Not every model takes every effort level; draft 1 lists all five for all models and relies on failure (AC-3.5).
**Fix:** the sheet takes the lists from the snapshot (`models`, `efforts` already sent by `panel.py:124`) and greys out a level the router reports as refused for that model; the failure path stays as the fallback.
**Status:** fixed in SPEC 6.2.

## Low

### R12. N3/N4 have no way to measure
**Fix:** feed events carry `at` (router time) and the page keeps, for the owner, a debug sheet with send→queued and entry→shown times of the last 50 events. **Status:** fixed in N3/N4.

### R13. `--copy` buttons and checklists aren't mentioned
`tg send --copy` and `tg checklist` are part of what assistants send. **Fix:** copy buttons render as buttons that copy; checklists render as a list with ticks, tappable (P1, same tap path as F13). **Status:** fixed in SPEC 6.3.

### R14. Two devices open at once
Not covered. **Fix:** both poll the same feed; a send from one shows on the other via the `user_message` event. AC-7.4 added. **Status:** fixed.

### R15. Upstream fit
The repo describes changes in long prose commit messages and keeps add-in docs in `addons/<name>/README.md` + `rules.md`. **Fix:** SPEC 9 names those files; the final docs move from `docs/chat-miniapp/` into `addons/chat/README.md` and ARCHITECTURE.md when implemented. **Status:** noted in SPEC 9.

## Not changed, on purpose

- No token streaming (non-goal). The tmux session gives no tokens; per-step progress is what can be delivered honestly.
- Long-poll instead of WebSocket from the page (Q4): same pattern as the panel, fewer moving parts; revisit if N4 isn't met.
- Steps shown to the person in full (trimmed and masked): it's their own assistant on their own data.

## Remaining risks

- Cloudflare free-plan limits on Durable Object requests: long-polling 1 page for 8 h a day is ~1,200 requests plus the router's PUTs; fine for 5 people, needs a check with real usage (N7).
- `tg` writing the outbox is a change to a core script; it must never fail the send (write after the API call, errors logged and ignored).
- Telegram clients differ in Mini App API versions; fullscreen and CloudStorage need fallbacks (N5), to be tested on a real iPhone and Android.

---

# Round 2: review by other models (2026-11-14)

Run `1114-1953-ac58` of llm-orchestra (`.orchestra/runs/2026-11-14-review-1114-1953-ac58.md`). Reviewer A: Gemini (gemini-3.8-flash-high through agy), with read access to the repo. Reviewer B: the `second_reviewer` role; DeepSeek's chain fell back to Qwen 3.8 27B on Cerebras, which spent its whole output budget thinking and returned nothing, so it was asked once more on `free-general` (GPT-OSS 120B on Cerebras). Claude checked each finding against the relay and router code and against Telegram and Cloudflare documentation; the one contested finding also went through PAL `challenge`.

Of 15 blockers raised, 8 hold, 3 hold as nits, 4 are false alarms. IDs below (`M…`) are the ones SPEC.md cites.

## Held

| ID | From | Finding | Change |
| --- | --- | --- | --- |
| M1 | Gemini | The router's 1 h check of init data dropped messages that waited at the relay while the machine was offline, against N8's 24 h | The relay stamps `queued_at`; the router wants `auth_date` ≤ 1 h before `queued_at` and `queued_at` ≤ 24 h ago (SPEC 4.1, AC-12.2) |
| M3 | Gemini | The Worker can't refuse strangers: it checks Telegram's signature, which every Telegram user has, but doesn't know `users.json`. A stranger could fill R2 and the queue | The router PUTs people's Telegram ids to the relay; the Worker answers 403 to everyone else before storing anything (SPEC 6.1, AC-11.1, AC-11.5) |
| M4 | Gemini | The 💬 copy in the DM fails for text over Telegram's 4,096 characters, and the composer allows 16,000 | Cut the copy at 3,000 characters with a pointer; the assistant gets it all; attachment-only messages get "💬 📎 names" (SPEC 4.1, AC-2.14) |
| M6 | Gemini | `relay/src/index.js:205` deletes an envelope once the router acknowledges it, so a page's retry after that is queued and run again: a command run twice | The router drops a `client_id` it took in the last 24 h (SPEC 4.1, AC-2.4) |
| M7 | Gemini | Telegram `CloudStorage` values hold 4,096 characters; drafts may be 16,000 | Split over four keys (F17, AC-10.6) |
| M8 | Gemini | `openTelegramLink` can't open a message in a private chat; such links exist only for channels and supergroups | A file chip opens the bot's DM with a toast (SPEC 6.3, AC-2.15) |
| M11 | Gemini | The transcript doesn't show which files a Bash command read, so "a result that touched a path" can't be checked; Write/Edit inputs weren't masked | Check `file_path` of Read/Write/Edit and the Bash command's text; hide input and result; say plainly what is still missed (SPEC 6.4, AC-4.4) |
| M12 | GPT-OSS | Trimming before masking can cut a secret so the pattern no longer matches | Mask the full text, then trim (SPEC 6.4, AC-4.4) |

## Held as nits

| ID | From | Finding | Change |
| --- | --- | --- | --- |
| M5 | Gemini | `envelope_id = chat:<client_id>` isn't per person. With a random UUID a collision is practically impossible and guessing another person's is infeasible, so this is hygiene, not a hole | `chat:<telegram id>:<client_id>`, UUID required (SPEC 4.1) |
| M9 | Gemini | `chat_open` had no route in SPEC 9 | The first `feed` read queues it (SPEC 4.2, 9) |
| M13 | GPT-OSS | No behaviour for failed uploads | Failed chip with Retry; Send waits (SPEC 6.8, AC-10.7) |

Also taken from the nits: R2 lifecycle rules are set with `wrangler r2 bucket lifecycle add`, not in `wrangler.toml` (M14); fullscreen and Telegram's safe-area variables need Mini App API 8.0 (N5, F10); the theme follows the `themeChanged` event (F10); file chips carry name, size and type (SPEC 6.4).

## False alarms

| From | Finding | Why it doesn't hold |
| --- | --- | --- |
| Gemini | Long-polling exhausts the free Durable Objects duration (5 people × 8 h × 128 MB = 18,000 GB-s/day) | The relay has one Durable Object for everyone, and duration is billed per instance while it is in memory, not per held request. Awake all day it is at most 86,400 s × 128 MB = 10,800 GB-s. The arithmetic now stands in N7, with a note to watch the dashboard in the first week |
| Gemini (M10) | After `/clear`, a continuing `seq` makes pages loop on a "gap" back to 0 | A page compares a new `seq` with the last one it received, not with 0, and its first read takes what comes. Said explicitly now (SPEC 6.4, AC-12.5) |
| GPT-OSS | Reads at 24 h (SPEC 6.1) contradict AC-11.4 | They agree: AC-11.4 has reads succeed with 2 h old init data, sends refused |
| GPT-OSS | The theme requirement has no mechanism | It's an implementation detail; the event name was added to F10 anyway |

Other GPT-OSS nits (exact UI of the "Sent to" cards and the command palette, a configurable cache size, notification previews of silent messages) were left for the implementation.
