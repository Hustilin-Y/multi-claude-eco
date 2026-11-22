# Service messages

Programs on the machine that aren't Claude sessions ("services", such as a 3D printer's) message the owner on Telegram through the bot, with pictures and buttons, and hear the button presses back, with no assistant in the loop. A tap on a service's button goes straight to that service and never reaches an assistant, so a model can ask the owner to confirm something but can't produce the confirmation itself.

A service calls a small HTTP API on `127.0.0.1` with its own token. It can send a message (Markdown, up to 10 files, pictures shown below the text, inline buttons), edit or delete one it sent, and long-poll for the presses on its buttons. The API runs inside the router, so it costs no new process, and nothing it does spends a Claude turn.

## What it needs

Nothing beyond the bot. Services run on the same machine, under the same Linux user (systemd), since the API listens on the loopback only.

## Setup

1. `assistant addon enable service-notify`. The router restarts and listens on `127.0.0.1:8790`. Another port: `"service_notify": {"port": 8791}` in `config.json`, then `assistant restart`.
2. `assistant service key <service>` prints that service's token once (a name of a-z, 0-9 and -, up to 32). Put it in the service's own environment file, readable only by you (mode 600). `assistant service revoke <service>` forgets it, `assistant service list` names the services with one. Only its SHA-256 is kept, in `state/service-keys.json`.
3. Messages go to the owner. To let a service message someone else in `users.json`, list them in `config.json`: `"service_notify": {"services": {"printer": {"to": ["bob"]}}}`.

Turning it off (`assistant addon disable service-notify`) first takes the buttons off every service message whose buttons still work.

## How far a button can be trusted

The button stops a model from deciding the owner meant yes: the press never passes through an assistant, the service's button ids never reach Telegram or any transcript, and the router checks the press against the message it kept, its chat and its expiry.

It doesn't stop an assistant set on forging one. Assistants are full admins under the same Linux user: one could read a service's token from the service's settings, or read `state/`. Each tap also reaches the front desk session first, as an incoming message with the button's label, and the router reads it from that session's transcript, which an assistant could write to. Closing that would need the router and the services to run as users the assistants aren't.

The API is a TCP port on the loopback, so it's for a machine whose only users are the bot's: another local Linux user could take the port while the router restarts and see a service's token, or answer its long-poll. Services keep their token readable only by themselves (mode 600), and the Python client never goes through a proxy.

## The API

Every request carries `Authorization: Bearer <token>`; the service is whoever the token belongs to. Bodies are JSON, at most 20 MB. Answers are JSON; an error is `{"error", "detail"?, "retry_after"?}`.

| Request | Body | Answer |
| --- | --- | --- |
| `POST /v1/messages` | `text` (Markdown, required), `buttons`, `files`, `to`, `silent`, `dedupe`, `expires_in`, `once` | `{"message_id"}`, or `{"message_id", "duplicate": true}` |
| `PATCH /v1/messages/<id>` | `text`, `buttons`, `expires_in`, `once` (each optional) | `{"message_id"}` |
| `DELETE /v1/messages/<id>` | | `{"message_id"}` |
| `GET /v1/presses?wait=<s>` | | `{"presses": [{"seq", "message_id", "button_id", "label", "by", "at"}]}` |
| `GET /v1/health` | | `{"ok": true, "telegram": "reachable" or "unreachable"}` |

- `buttons`: rows of `{"id", "text"}`, at most 8 a row and 24 in all. `id` (up to 128 characters, unique in the message) is the service's own: it comes back in a press and never leaves the machine. `text` is the label, up to 64 characters.
- `files`: up to 10 `{"name", "data"}`, `data` in base64. `.jpg`, `.jpeg`, `.png` and `.webp` show as pictures below the text, anything else as a file.
- `to`: a name in `users.json` the owner allowed for this service (403 otherwise); the owner when left out.
- `silent`: no notification sound. Quiet hours, when that add-in is on, apply anyway.
- `dedupe`: a key of up to 128 characters. Sending the same key again within 7 days sends nothing and answers the first message's id with `"duplicate": true`, so an event sent again after the service restarted shows once.
- A message with buttons goes out without them and gets them a moment later, once the router has kept it, so no button of a service's is ever on Telegram without the record that says its taps are the service's. If they can't be put on, the answer is the error with the message's id in `detail`; the message stays, and if Telegram put the buttons on after all, they come off at their expiry.
- `expires_in`: how many seconds the buttons work, 24 hours unless given, 7 days at most. Then the router takes them off by itself and adds "⌛ Expired: nothing was done." to the message ("⌛ Expired." for buttons that stay, which may have been pressed before), and a tap that races it does the same. A press still waiting on expired buttons is dropped. An edit Telegram doesn't take is tried again at the next sweep.
- `once` (true unless given): the first tap takes the buttons off and shows "⏳ <label>…" under the text, so a second tap can't happen; the service then edits the message to say what it did. With `false` the buttons stay and every tap is a press (for something like "📷 Snapshot now").
- An edit changes only what it names. `buttons: []` takes them off; new buttons make the message pressable again, for `expires_in` or another 24 hours. Pictures stay. Only the service's own messages can be edited or deleted (404 otherwise), for 30 days; Telegram lets a bot delete one for 48 hours.
- Presses: `GET /v1/presses?wait=25` answers at once with what's waiting, or holds up to `wait` seconds (25 at most) for the next one. A press is handed over once, and leaves the queue as it's answered; a long-poll whose caller hung up meanwhile (the service restarted) leaves it for the next one. One the service doesn't take within 2 minutes is never handed over: it's dropped, and the message edited to "⚠️ <service> didn't pick this up in time: nothing was done.", so a press the owner was told did nothing never reaches the service later. If the service dies right after taking a press, that press is lost and the message stays at "⏳ …": for a confirmation, the safe way to fail. At most 4 long-polls at once per service.
- Errors: 400 `bad_request` (with a `detail`), 401 `unauthorized`, 403 `recipient_not_allowed`, 404 `not_found`, 413 `too_large`, 429 `rate_limited` (with `retry_after`), 500 `not_kept` (the message went out, but its buttons couldn't be kept, so they were never put on), 502 `telegram_refused` (Telegram's reason as `detail`), 503 `telegram_unreachable`. A message is never held while Telegram can't be reached: the service decides whether to send it again.
- Limits per service: 20 new messages a minute, 60 edits and deletes a minute (checked before the body is read), on top of Telegram's own. For every service together: 16 connections at once, each dropped after 30 seconds of sending nothing.
- The router's log has one line per request: the service, the method and path, the status, how long it took, and for a message the text's length and the number of files. Never a token, a text, a button id or a file.
- The router's restart takes the API away for a few seconds; the presses waiting are kept on disk.

## A Python client

[client/multiclaude_service.py](client/multiclaude_service.py) is one file, standard library only: copy it into the service. It tries a failed connection again (1, 2, 4… seconds, 5 times), waits once on a 429, and raises `ServiceError` (`status`, `code`, `detail`, `retry_after`) for anything else.

```python
from multiclaude_service import Service

svc = Service("http://127.0.0.1:8790", os.environ["MULTICLAUDE_TOKEN"])

mid = svc.send("🖨️ **Plate 1** ready: 2 h 14 m, 38 g PLA", files=["preview.png"],
               buttons=[[("print:7f3a", "Print plate 1"), ("cancel:7f3a", "Cancel")]],
               expires_in=600, dedupe="confirm:job42")

for press in svc.presses():  # long-polls forever; each press comes once
    if press["button_id"] == "print:7f3a":
        svc.edit(press["message_id"], "✅ Printing plate 1")
    elif press["button_id"] == "cancel:7f3a":
        svc.edit(press["message_id"], "✖️ Cancelled")
```

A confirmation's button id can carry the service's own token for it (as `print:7f3a` above), which the service checks when the press comes: still unexpired, not used yet, the job unchanged. The router's expiry and single press are a second line, not the only one.

## Files

- `lib/multiclaude/addons/service_notify/`: `__init__.py` (the hooks), `api.py` (the HTTP API), `messages.py` (checking, sending, editing, deleting, keeping a message with buttons), `taps.py` (presses, expiry, presses not taken), `store.py` (what's kept on disk).
- `addons/service-notify/`: `assistant-service` (the command), `teardown`, `next-steps`, `rules.md`, `client/multiclaude_service.py`.
- State: `state/service-keys.json` (token hashes), `state/service-notify/live.json` (messages whose buttons still work), `state/service-notify/<service>/` (`presses.jsonl` and `seq`, `sent.json` for 30 days, `dedupe.json` for 7), and each message with buttons kept as a question of kind `service` in `state/asks/<chat>/<message id>.json`.

## Checking it works

1. After `assistant addon enable service-notify` the router's log says `service-notify: listening on 127.0.0.1 port 8790`.
2. Put `Authorization: Bearer <token>` in a file only you can read, so the token isn't in a command line, and ask for the health: `curl -s -H @auth.txt http://127.0.0.1:8790/v1/health` answers `{"ok": true, "telegram": "reachable"}`.
3. Send one message with a picture and a button, edit it, and check the picture stays: the Bot API doesn't say whether an edit keeps a rich message's pictures, so the add-in sends them again by the file ids Telegram gave. If the router's log says `Telegram gave no file ids`, edits drop pictures on this Bot API version.
