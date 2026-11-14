# Chat Mini App: implementation notes for P0

Written with the P0 implementation (add-in `chat`, F1–F11 and their plumbing), 2026-11-14. It lists where the code does something the spec ([SPEC.md](SPEC.md), draft 4) doesn't say, or says differently, and why. The spec itself is left as it was reviewed; the add-in's [README](../../addons/chat/README.md) describes the code as it is.

## Where the code differs from the spec

| # | Spec | What the code does | Why |
| --- | --- | --- | --- |
| D1 | 9: `bin/tg` appends to `state/chat/outbox/<name>.jsonl` | `bin/tg` calls a new add-in hook, `tg_sent(sender, chat, record)`, after each successful send, ask, checklist and edit; the chat add-in writes the outbox | The core never imports an add-in (a test enforces it), and with the add-in off nothing should be written. The hook mechanism logs a failure and goes on, which is exactly "never fails the command" (AC-2.13) |
| D2 | 4.2, AC-7.3: job runs stay out of the feed | `assistant job` now sets `ASSISTANT_JOB=1`; the add-in keeps nothing `tg` sends with it set, nor anything sent without `$ASSISTANT_USER` (`assistant notify`, cron, scripts) | A job sets `ASSISTANT_USER` like the live session, so its own `tg send` calls would otherwise reach the outbox. Jobs already run with `--no-session-persistence`, so they write no transcript; the feed also follows only the transcript the status line names, so any other `claude -p` in the folder stays out |
| D3 | 4.2: the router keeps building events "while that page polls (a feed read in the last 2 minutes)" | The relay passes reads on as `chat_open` envelopes: `{"backfill": true}` on a first read (after 0), `{"backfill": false}` at most once a minute after that. The router follows for 2 minutes after the last one | The router can't see the page's reads otherwise. One envelope type with a flag instead of a second type. Envelope ids are `chat_open:<telegram id>:<0 or 1>`, so reads while the machine is offline queue at most two |
| D4 | 6.4: event kinds | Events also carry `key` (the page updates the item with that key in place) and `ts` (when it happened, for the order); there is one more kind, `history`, with the cursor for scrolling up; `settings` also carries `model_name`, `bot` and `bot_username` (for the header and file chips) | A backfill builds events again under new seq numbers (seq only grows), so the page needs a stable identity to show each message once; the cursor has to reach the page somehow; the snapshot carries no bot username |
| D5 | 6.7: backfill | The backfill is put with `"replace": true`: the relay drops what it kept for that person and keeps the backfill | Keeps the 200-event cache free of duplicates. Pages already open get the new events (higher seq) and dedupe by key |
| D6 | F7: Stop in place of Send while a turn runs | Stop shows while a turn runs and the composer is empty; with text typed, Send stays, and the message waits behind the turn, as in the Telegram chat | Otherwise a person couldn't add to a running request without stopping it, which the DM allows |
| D7 | 6.3: router notices as system lines | Captured from the router's own plain-text posts to that person (`before_telegram_send`, in the router's process only) while their page is open | Notices go through `Session.say` to many places; this catches them all without touching the core |
| D8 | 4.1: every message posts a "💬" copy | Not for the bot's commands (`/stop`, `/clear`, `/model …` and the rest of the router's), which Stop and New chat send: no copy and no `user_message` event | They aren't conversation; `/clear` empties the DM anyway |
| D9 | 9: one rules line in `system/workspaces-CLAUDE.md` | In `addons/chat/rules.md`, which `assistant install` appends while the add-in is on | 9's packaging bullet asks for the add-in's own `rules.md`; the core rules shouldn't name an add-in that's off |
| D10 | 10: "owner only (`users.json` `chat_beta: true`)" | While anyone has `"chat_beta": true`, only they are on the members list the relay gets; with nobody, everyone is | One switch for the trial week, no code change to open it |
| D11 | 6.7: "Events older than 24 hours are dropped by an alarm" | Dropped on every read and write, and by a Durable Object alarm set 24 hours past the last write | The alarm alone would leave old events in place for a person who keeps reading; reads and writes are where they'd be seen |
| D12 | 8: "Init data over 1 h old, on Send" | The relay answers 401 with `{"error": "reopen"}` (over an hour) or `{"error": "unsigned"}` (bad or over a day), so the page can say which | Both are 401 in the AC |
| D13 | F18, AC-2.14 second half: attachment-only copy "💬 📎 names" | `dm_copy` makes it and is tested, but the page can't attach files in P0 and the relay passes no attachments | Attachments are P1 (R2); the seam is the copy function and the envelope's payload |
| D14 | 6.3: "a small highlighter", vendored | Written here (`relay/public/app/highlight.js`, about 80 lines): comments, strings, numbers and keywords of the common languages | Small enough to own; marked and DOMPurify are vendored from npm unchanged |
| D15 | F12 (P1) | Step events are built, masked and trimmed in P0 (AC-4.3, 4.4 and 4.7 are [auto]); the page shows each as one line with ✓, ✕ or … and doesn't expand it | The status line (F6) needs the running tool, and masking must hold from the first release; the expandable rows stay P1 |
| D16 | N1a: history reads at most 4 MB | A backfill or history request reads at most 3 MB of transcript and 1 MB of outbox (each including a last line of at most 256 KB), skips longer lines unread, and keeps only the newest 200 events of a window | A line can be megabytes (a pasted picture). Consequence: in scroll-up history, assistant answers older than the outbox's last ~768 KB don't show (the person's messages and the steps still do) |
| D17 | F10: `requestFullscreen()` on 8.0+ | Only on iOS and Android | Full screen on a desktop window only hides Telegram's own title bar |
| D18 | 4.2: "the session's current transcript" | The transcript the status line names, else the folder's newest; one a `/clear` left behind is never followed again | Right after `/clear` the status line still names the old transcript until it redraws, and following it again would replay the old conversation into the new one |
| D19 | 6.5: "Waking up…" until the first transcript entry | `waking` while the assistant is marked asleep with work queued, or after a message came for it asleep until its transcript is read again | Same, said in what the router can see |

## Not done in P0, though the spec or AC mention it

- **Messages held while Telegram can't be reached** (`tg` exits 75) aren't in the outbox: they go out later from the held-message queue, outside `tg`'s command. They reach the DM but not the Mini App. A `tg_sent` call in `telegram.Outbox.send_held` would fix it; it needs the sender's name, which the held message doesn't keep.
- **`tg ask` buttons and checklists** show as inert chips with "Answer in Telegram" (F13 is P1). Copy buttons (`--copy`) work.
- **The debug sheet (R12)** is a ⏱ button for the owner with send→queued and entry→shown times of the last 50 events; nothing is sent anywhere.
- **`assets/architecture.svg`** doesn't show the chat; ARCHITECTURE.md and README.md do.
- **Drafts** stay in the composer while the page is open and come back after a refused send; `CloudStorage` drafts are F17 (P1).

## To check on the machine

These rest on what Claude Code writes, which the tests stand in for:

- A message the router bracket-pastes shows in the transcript as the header line, then the pasted text in full (the feed shows the person's own messages, Telegram's too, from there). If a version writes a `[Pasted text …]` placeholder instead, the feed would show that.
- The status line's payload names the session's transcript (`transcript_path`), as `/usage` already relies on.
- A subagent's inner calls stay in its own transcript, or come with `isSidechain` in the session's (both are left out).

## Seams left for P1

- Events: `step` already has `input_preview` and `result_preview`; `assistant_message` has `buttons` and `checklist` (F13).
- `chat.dm_copy(text, attachments)` for F18; the relay's `send` refuses nothing it doesn't know, the page has a single composer form to add chips to.
- `chatfeed.STRINGS` holds every string (N9).

## Testing

- Python (`tests/addons/test_chat.py`, 42 tests) and the relay (`relay/test/chat.test.js`, `relay/test/chatfeed.test.js`, 33 tests) cover the [auto] ACs of P0; the add-in's README lists the files.
- `./check` was run in WSL (Ubuntu 24.04) on a copy of the worktree with LF line endings, since this checkout has CRLF and Windows can't import `fcntl`. It runs there as root, so `tests/test_shared.py::test_a_skill_copy_that_fails_halfway_leaves_nothing_behind` fails (a `chmod 0` file is still readable by root); it fails the same way without these changes.
- The page was looked at in a browser with a stand-in for Telegram and the relay (desktop width and 320 px). It has not been tried in a real Telegram client yet: see the manual ACs.
