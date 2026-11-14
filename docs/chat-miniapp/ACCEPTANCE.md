# Chat Mini App: use cases and acceptance criteria

Companion to [SPEC.md](SPEC.md), draft 4 (both review rounds from [REVIEW.md](REVIEW.md) applied). IDs: `UC-n` use cases, `AC-n.m` acceptance criteria (Given / When / Then). Feature IDs (`F1`…) and requirement IDs (`N1`…) refer to the spec. Each AC is meant to be checked by hand on a phone and, where marked **[auto]**, by a test run by `./check`.

## Actors

- **Person:** a paired member or the owner, in a Telegram client.
- **Stranger:** a Telegram user not in `users.json`.
- **Router:** `assistant-watcher` on the machine.
- **Relay:** the Cloudflare Worker and its Durable Object.
- **Assistant:** the person's Claude Code session.

---

## UC-1. Open the chat

**Main flow:** the person taps the bot's menu button "Chat" (or sends `/chat` and taps the button in the answer). The Mini App opens on the Chat tab with the current conversation, the model and effort chips, and the composer.

**Alternatives:** opened from `/config` → opens on the Panel tab; Chat is one tap away. Opened by a stranger → refused (UC-11).

- **AC-1.1** Given a paired person who has had the Mini App closed for a day while chatting in the DM, when they tap "Chat", then the page shows the Chat tab within 2 s on 4G (machine online), with the last ≤50 events of the current conversation including those DM messages (backfill), oldest at the top, scrolled to the bottom.
- **AC-1.6** **[auto]** Given a sleeping assistant, when the chat is opened and left polling for 10 minutes, then the assistant stays asleep (`state/asleep/<name>` still there); it wakes only on Send, Stop or an approval tap.
- **AC-1.7** **[auto]** Given a person who never opens the Mini App, then the relay holds no feed events for them after a day of DM use.
- **AC-1.2** Given an empty conversation (just after `/clear`), when the chat opens, then it shows the empty state with the bot's name and 4 suggestion chips; tapping a chip puts its text in the composer, not sends it.
- **AC-1.3** Given the Telegram dark theme, when the chat opens, then background, text, bubbles and code blocks follow `themeParams`; switching the Telegram theme while it is open updates the page without reload.
- **AC-1.4** Given an iPhone with a notch, when the chat opens full-height, then no control is under the notch or the home indicator (safe-area insets applied).
- **AC-1.5** Given the page opened from `/config`, then the Panel tab is selected and the tab bar shows Chat and Panel.

## UC-2. Send a message and get an answer

**Main flow:** the person types, taps Send. Their bubble appears at once ("sending"), turns "delivered" when the router types it into the session. The status line shows "Thinking…", then steps; the assistant's answer appears as rendered Markdown. The same answer arrives in the DM.

- **AC-2.1** Given text "hello", when Send is tapped, then the bubble appears immediately with state "sending", the composer clears, and within 1.5 s (N3) the state is "queued" or "delivered".
- **AC-2.2** Given a sent message, when the assistant answers with `tg send`, then the answer appears in the Mini App within 3 s of its outbox entry (N4) **and** in the Telegram DM, rendered from the same Markdown.
- **AC-2.3** **[auto]** Given an inbound `chat` envelope, when the router handles it, then (a) a silent "💬 <text>" message is posted to the person's DM, and (b) the assistant's terminal gets exactly one message with header `[Mini App from <Name> · msg <id of that 💬 message> · <time>]` followed by the text, bracket-pasted.
- **AC-2.12** **[auto]** Given the assistant answers with `tg send <<'EOF'` (text on stdin, 3,000 characters, with a code block), then the Mini App shows exactly that text; likewise for `tg send "$(cat report.md)"` and `tg send --file x.png "caption"` (a file chip).
- **AC-2.13** **[auto]** Given the outbox can't be written (read-only directory), then `tg send` still sends and exits 0, and the router logs the failure.
- **AC-2.4** **[auto]** Given the same `client_id` posted twice (a retry), before or after the router acknowledged the first, then the assistant gets the message once; given two people posting the same `client_id`, both messages are delivered.
- **AC-2.14** **[auto]** Given a 10,000-character message, then the DM's 💬 copy holds its first 3,000 characters and "… (full text in the Mini App)", and the assistant gets all 10,000; given a message with only a photo, the DM copy is "💬 📎 <file name>".
- **AC-2.5** Given multi-line text, then line breaks are kept in the person's bubble and in what the assistant gets.
- **AC-2.6** Given a 16,001-character text, then Send is disabled and the counter shows "16,001 / 16,000"; at 14,000 the counter first appears.
- **AC-2.7** Given an empty or whitespace-only composer and no attachment, then Send is disabled.
- **AC-2.8** On desktop Telegram, Enter sends and Shift+Enter adds a line; on iOS/Android, Enter adds a line and only the button sends.
- **AC-2.9** Given an answer with a heading, a list, a table, a code block in Python and a `<details>` section, then all render; the code block is highlighted and its Copy button copies the code without the fence; raw HTML other than `<details>/<summary>` is shown as text, never run.
- **AC-2.10** Given an answer containing `<img src=x onerror=alert(1)>` or a `javascript:` link, then nothing runs and the link isn't clickable (DOMPurify).
- **AC-2.11** Given a link in an answer, when tapped, then it opens through `Telegram.WebApp.openLink` (outside the Mini App), and `t.me` links through `openTelegramLink`.
- **AC-2.15** Given an answer sent with `tg send --file report.pdf`, then a chip shows "report.pdf", its size and type; a tap opens the bot's DM and a toast says the file is there.

## UC-3. Choose model and effort

**Main flow:** the person taps the model chip, picks "sonnet" in the sheet, optionally turns on "Keep after restarts"; the switch is sent at once, the chip says "sonnet". The next message is answered on sonnet.

- **AC-3.1** Given the current model opus, when the chat opens, then the model chip shows "opus" and the effort chip the current effort, read from the snapshot; the sheet lists the models and efforts the snapshot sends; when the snapshot has no current value, the chips say "Model" / "Effort" and still open.
- **AC-3.2** Given an idle session and "Keep after restarts" off, when the person picks sonnet, then the router runs `/model sonnet` within 5 s (before any message sent after the pick **[auto]** for the queue order), the chip confirms, and the person's workspace settings are unchanged.
- **AC-3.3** Given "Keep after restarts" on, then the switch is saved as `/model sonnet default` and survives `assistant restart`.
- **AC-3.4** Given effort `max` picked, then the "Keep after restarts" switch is disabled with the hint "max can't be kept after restarts".
- **AC-3.5** Given the switch fails (the router's result is not "Set model to …"), then the chip returns to the old value and the router's error text shows under the composer; an effort refused for that model is greyed out in the sheet with that reason until the model changes.
- **AC-3.6** Given a turn is running, when the person picks a new model, then the chip shows the new value with a "pending" dot, the running turn is not interrupted, and the switch happens after it and before the next message.
- **AC-3.9** **[auto]** Given a `chat` envelope with extra `model`/`effort` fields, then the router ignores them (messages never switch settings).
- **AC-3.7** Given the model is changed elsewhere (`/model haiku` in the DM, or the Panel tab), then the chat's chip shows "haiku" within 5 s (a `settings` event).
- **AC-3.8** Given a picker sheet is open, when Telegram's Back button is pressed, then the sheet closes and the Mini App stays open.

## UC-4. Watch the work and stop it

**Main flow:** while a turn runs, the status line shows what the assistant does; the Send button becomes Stop. The person taps Stop; the turn ends.

- **AC-4.1** Given a sleeping assistant, when a message is sent, then the status shows "Waking up…" until the session's first transcript entry, then "Thinking…".
- **AC-4.2** Given the assistant runs `Bash` with `npm test`, then a step row "Ran `npm test`" appears (P1: collapsed, expandable to input and up to 2 KB of output) and the status says "Running: Bash" with the elapsed seconds.
- **AC-4.3** **[auto]** Given transcript entries with a thinking block, a tool_use and its tool_result, and a `tg send` call with its outbox record, then the feed has one `step` and one `assistant_message` (the `tg` call is not also a step), and no event contains the thinking text.
- **AC-4.4** **[auto]** Given a tool result containing `ANTHROPIC_API_KEY=sk-ant-…`, `Authorization: Bearer abc`, `password: hunter2`, a JWT or a PEM block, then the `result_preview` sent to the relay has those values replaced with `•••`, also when the secret straddles the 2 KB cut; given `Read ~/.ssh/id_ed25519` or `cat .env`, the whole preview is "[hidden: may contain secrets]"; given `Write .env.local` with a key in its content, the input preview is hidden too.
- **AC-4.7** **[auto]** Given the assistant runs a subagent with 30 tool calls, then the feed shows one step "Ran agent `<description>`", not 30.
- **AC-4.5** Given a running turn, when Stop is tapped, then `/stop` reaches the router, the button shows a spinner, and once the status is `idle` the Send button is back; if not idle within 15 s, the page shows "Couldn't stop — try again".
- **AC-4.6** Given a turn of 12 minutes, then the status keeps updating and the page shows no timeout error.

## UC-5. Answer a question or an approval

**Main flow (P1):** the assistant asks with `tg ask` → the question shows with its buttons; a tap answers it as a DM tap would. An approval dialog shows Approve / Deny.

- **AC-5.1** Given `tg ask "Deploy now?" Yes No`, then the Mini App shows the question with buttons Yes and No; tapping Yes reaches the assistant as the same "tapped a button" message the DM sends, and the buttons in both the Mini App and the DM show the choice as taken.
- **AC-5.2** Given a button tapped in the DM, then the Mini App's copy of the question shows it as answered within 5 s.
- **AC-5.3** Given an approval dialog, then a card with its text and Approve / Deny appears; a tap is carried out by the router as from the DM; after Claude Code's 2-minute timeout the card says "Timed out — denied" and its buttons are disabled.

## UC-6. Start a new chat

- **AC-6.1** Given a conversation, when the person taps New chat, then a confirm sheet appears with the text from SPEC 6.6; Cancel leaves everything as it was.
- **AC-6.2** Given New chat confirmed, then `/clear` runs as from the DM (the DM is emptied too), the feed gets `cleared`, the page empties to the empty state, and neither the relay nor the person's outbox holds anything of the old conversation **[auto]**.
- **AC-6.3** Given `/clear` sent in the DM while the Mini App is open, then the Mini App empties within 5 s.

## UC-7. Continue between the DM and the Mini App

- **AC-7.1** Given the person writes "A" in the DM and then "B" in the Mini App, then the Mini App shows A (labelled "from Telegram"), the answer to A, B and the answer to B, in order; the DM shows A, its answer, "💬 B" and B's answer as a reply to it; the assistant treats them as one conversation.
- **AC-7.2** Given an answer the assistant later changes with `tg edit`, then the Mini App updates that message in place.
- **AC-7.3** **[auto]** Given a scheduled job runs (`assistant job`, its own `claude -p` transcript in the same folder) while the chat is open, then none of its steps or text appear in the feed; the DM shows its result as today.
- **AC-7.4** Given the chat open on a phone and on Telegram Desktop, when a message is sent from the phone, then it and its answer appear on the desktop within 5 s, once each.
- **AC-7.5** Given the assistant sends `tg send --chat ada "…"` to another person, then the person's own chat shows a collapsed "Sent to Ada" card, not an answer bubble.

## UC-8. Slash commands (P1)

- **AC-8.1** Given `/` typed as the first character, then a list of the router's commands with one-line descriptions opens above the composer; typing filters it; tapping one fills the composer.
- **AC-8.2** Given `/usage` sent, then the router's answer appears as a system line in the Mini App and in the DM.

## UC-9. Limits and context (P1)

- **AC-9.1** Given context at 82%, then the meter by the composer shows 82% in the warning colour and a hint "New chat soon frees the context".
- **AC-9.2** Given the 5-hour limit at 100%, then Send stays enabled, and a banner says when the limit resets (from the snapshot).

## UC-10. Draft and attachments (P1)

- **AC-10.1** Given a typed but unsent draft, when the Mini App is closed and opened again (same or another device), then the draft is back (Telegram `CloudStorage`); without CloudStorage support it is kept in `localStorage` on that device.
- **AC-10.6** Given a 12,000-character draft, when the Mini App is reopened on another device, then the whole draft is back (split over `draft.0`…`draft.2`), and a later shorter draft leaves no stale tail from the unused keys.
- **AC-10.7** Given an upload that fails (network drop), then its chip shows "failed" with Retry and Send stays disabled until the chip is retried or removed.
- **AC-10.2** Given a photo picked or pasted, then a thumbnail chip shows in the composer with a remove ✕; on Send it reaches the assistant's inbox as a DM photo would, and the assistant can Read it.
- **AC-10.3** Given a file over 10 MB, then it's refused before upload with "Files up to 10 MB"; given 20 MB already pending for the person, the next upload is refused with "Wait until the files above are delivered".
- **AC-10.4** **[auto]** Given an upload the router fetched, then the R2 bucket no longer holds it; an upload not fetched in a day is deleted by the bucket's lifecycle rule (checked by hand once after deploy).
- **AC-10.5** **[auto]** Given a request for another person's upload id with valid init data or the wrong relay token, then the relay answers 403/401 and returns no bytes.

## UC-11. Refused access

- **AC-11.1** Given a stranger with valid Telegram init data opens the page, then they see "This bot doesn't know you yet…"; every chat route, `send` and `upload` included, answers 403 at the Worker, and nothing is queued or stored in the DO or R2 **[auto]**.
- **AC-11.5** **[auto]** Given a person newly paired with `assistant pair`, then within 1 minute the Worker accepts their chat requests; given a person removed from `users.json`, then within 1 minute it refuses them.
- **AC-11.2** **[auto]** Given init data with a wrong hash, or signed over 24 h ago, then the Worker answers 401 and the router ignores a `chat` envelope carrying it.
- **AC-11.3** **[auto]** Given person A's valid init data and a request body naming person B, then A only ever reads and writes A's feed.
- **AC-11.4** **[auto]** Given valid init data signed 2 h ago, then reads (`/app/chat/feed`) succeed, while `/app/chat/send`, `/app/chat/upload` and an approval tap are refused by the Worker (401) and by the router; the page shows "For safety, reopen the chat to keep sending" and keeps the draft.

## UC-12. Offline and failures

- **AC-12.1** Given the phone loses network, when Send is tapped, then the bubble shows "failed" with Retry and the draft text stays in the bubble; Retry sends it with the same `client_id`.
- **AC-12.2** Given the machine is offline (router disconnected for over 30 s), then a banner says so; a message sent then is delivered once the machine is back, within 24 h, in the order sent — also when it comes back 5 h later, though the init data was then over 1 h old **[auto]** for the router's `queued_at` check.
- **AC-12.3** Given more than 20 messages in a minute, then the 21st gets "Slow down a bit" and is not queued **[auto]**.
- **AC-12.4** Given the page's feed jumps from seq 100 to 105, then it re-reads the last 50 events and shows no duplicate.
- **AC-12.5** **[auto]** Given `/clear` at seq 1042, when a page opens afterwards and its first read returns seq 1043 onward, then it counts no gap and asks for nothing more.

## Non-functional checks

- **AC-N1** With 4 people and the chat open for all, the router's RSS grows by under 20 MB versus the add-in off (`ps -o rss` before and after, 30 min of use).
- **AC-N1a** **[auto]** Given a 300 MB transcript, when a page opens (backfill) and then scrolls up three times (`chat_history`), then the router reads at most 4 MB per request (counted by a wrapped file reader in the test) and its peak memory (`tracemalloc`) stays under 20 MB.
- **AC-N1b** **[auto]** Given a 10 MB upload, when the router fetches it, then it is written to the inbox in chunks of at most 1 MB and the router's peak memory during the fetch grows by under 2 MB.
- **AC-N1c** **[auto]** Given an outbox that reaches 5 MB, then `tg` rotates it to one old file and starts a new one; given the relay unreachable for an hour while 500 events are made for one person, then at most 200 wait in memory and the page, once the relay is back, fills the gap by re-reading.
- **AC-N2** Page assets total under 300 KB gzip without `telegram-web-app.js` (`./check` measures).
- **AC-N5** UC-1 and UC-2 pass on Telegram iOS, Android, Desktop and Web.
- **AC-N6** At 320 px width nothing scrolls sideways except code blocks; all tap targets ≥ 44 px; contrast checked in both themes.
- **AC-N10** `./check` passes with the new tests.

## Traceability

| Feature | Use case | AC |
| --- | --- | --- |
| F1, F10 | UC-1 | 1.1–1.7 |
| F2, F5, F11 | UC-2 | 2.1–2.15 |
| F3, F4 | UC-3 | 3.1–3.9 |
| F6, F7, F12 | UC-4 | 4.1–4.7 |
| F13, F14 | UC-5 | 5.1–5.3 |
| F8 | UC-6 | 6.1–6.3 |
| F9, N11 | UC-7 | 7.1–7.5 |
| F15 | UC-8 | 8.1–8.2 |
| F16 | UC-9 | 9.1–9.2 |
| F17, F18 | UC-10 | 10.1–10.7 |
| 6.1 | UC-11 | 11.1–11.5 |
| N8, 6.8 | UC-12 | 12.1–12.5 |
| F19–F24 | — | P2: written when scheduled |
