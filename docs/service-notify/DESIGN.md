# Service messages add-in: design

Status: draft for the owner's approval, 2026-11-22, after a builder check (its questions are answered below, marked "builder check"). Add-in name `service-notify`, module `multiclaude.addons.service_notify`.

## Intent

A program on the machine that isn't a Claude session (a "service") needs to reach the owner on Telegram through the bot and to hear back from buttons, with no assistant in the loop. The first one is `bambu-service`, a planned Python service on the same machine that owns a Bambu Lab P2S printer over LAN (MQTT/FTPS). The printer runs LAN-only with Developer Mode, so Bambu Handy is gone and Telegram is the owner's phone interface to it.

The reason it must bypass the assistant: an independent design review of bambu-mcp (`bambu-mcp-design-review.md`, findings 3 and 11) found that "explicit yes" said in a chat can't be told apart from a model that decided the owner meant yes. Starting a print heats a bed and a nozzle in a room nobody may be watching. So the confirmation has to be a button the service itself sent, whose press goes to the service and never through Claude: Claude may ask for a confirmation, it can't produce one.

What bambu-service needs, and what any service gets:

- push events (print started, first layer done, paused, HMS error in words, finished, failed), optionally with a camera snapshot (JPEG) and a sliced-plate preview (PNG), each event sent once even if the service restarts mid-way;
- a print confirmation with buttons ("Print plate 1", "Cancel"), bound by the service to a token that expires in about 10 minutes; a press after expiry, or a second press, ends with the message saying what happened;
- short follow-ups with buttons after a print ("How did it come out?": ok / tight / loose / failed) and the answer back;
- editing an earlier message (progress, final state) and deleting one.

It is generic: `orchestra-gateway` or any other local service can use it the same way. It stays a self-contained add-in so it can be offered upstream (korsicheg/multiclaude).

## Goals

- A local HTTP API on `127.0.0.1` for services that run under systemd as the same Linux user as multiclaude, with one token per service.
- Messages with Markdown text, pictures and inline buttons, to the owner by default.
- Button presses go to the service that sent the message, never to an assistant, delivered at least once and never lost to a router or service restart.
- Edits and deletes of the service's own messages.
- Sending and pressing never wake a sleeping assistant and work in both front desk modes (`plugin` and `router`).
- A minimal Python client (standard library only) that a service author copies in.

## Non-goals

- Free-text replies from the owner to a service (only buttons). A service that needs text asks through the assistant like anyone else.
- Services on other machines, in Docker without host networking, or under another Linux user. The listener binds the loopback only.
- Messages to groups or to strangers.
- Holding a service's message while Telegram is unreachable (the service decides whether to retry).
- A Mini App screen for services, or service messages in the chat add-in's feed. The owner wants a tab later (2026-11-22: "v1 only buttons in the chat, the tab later"): the chat add-in's `chat_tabs`, `addon_state` and `chat_tab_action` hooks are where it would go, so v1 keeps its state per service in files a later tab can read, and nothing in v1 should make that harder.

## Anti-goals (it fails even if it works)

- A press reaches an assistant's terminal, or anything a model reads before the service acts on it.
- A service message or press wakes a sleeping assistant or spends a Claude turn of a person's assistant.
- A token, or a button's service-side id (which may embed a confirmation token), appears in a log, in Telegram's callback data or in a Telegram message.
- A press the owner was told had no effect ("didn't pick this up in time") is acted on later anyway.
- Turning the add-in off leaves buttons whose press would go to an assistant.

## Constraints

- Router: `bin/watcher.py`, standard library only, one process (`assistant-watcher.service`). The machine ("workshop") has about 3 GB of RAM: no new process, no dependency.
- The core never imports an add-in; it calls hooks (`lib/multiclaude/addons/__init__.py`). A failing hook is logged and skipped.
- Taps on inline buttons reach the router only for callback data `ask:<n>`: the Telegram plugin's patch passes only those in plugin mode, and `telegram_entrance.tapped` only those in router mode, both only from a paired person in their private chat.
- Every message the bot posts is a rich message (`sendRichMessage`), and goes through `BotApi.call`, so quiet hours (`before_telegram_send`) apply.
- Tests run with `./check`, never touch the live system, and each add-in has a check that it does nothing while it's off.
- Docs change in the same commit as the code (ARCHITECTURE.md, README.md, the add-in's README.md and rules.md).

## Approaches considered

| | A. Listener thread in the router (chosen) | B. Separate daemon | C. File spool, no socket |
|---|---|---|---|
| How | The add-in starts a `ThreadingHTTPServer` on `127.0.0.1` from its first `on_tick`. It sends with the core's Bot API client; presses come to it from `asks.answer` through a new hook and wait on disk for the service's long-poll. | Its own systemd unit with the HTTP API; the router hands it presses through files. | The service writes JSON into `state/…/out/`, the router sends it on its tick, presses go into `in/`. |
| For | No new process; presses already land in the router; self-contained add-in. | Survives router restarts. | No port. |
| Against | The API is down for the seconds a router restart takes (the client retries; presses are on disk, so none is lost). An add-in bug shares the router's process (as every add-in does). | Another Python process (~20 MB) and a unit `assistant install` must write: core changes, not an add-in. Presses still pass through the router. | No message id comes back synchronously, and edits need it; auth by file permissions only; polling delay. |

The owner picked A (2026-11-22), with services under systemd as the same user and HTTP on `127.0.0.1`.

## Decided

### Placement and lifecycle

- Code in `lib/multiclaude/addons/service_notify/` (`__init__.py` with the hooks, `api.py` the HTTP handler, `store.py` the state on disk, `keys.py`). Repo folder `addons/service-notify/` with `README.md` (for the owner and service authors), `rules.md` (for assistants), `assistant-service` (CLI), `next-steps`, and `client/multiclaude_service.py` (the Python client).
- Added to `addons.ALL`. Enabled with `assistant addon enable service-notify`; the router restart that follows starts the listener.
- The listener starts on the add-in's first `on_tick` in a daemon thread, binds `127.0.0.1:<port>` (default 8790; `config.json` `"service_notify": {"port": 8790}`). Only `127.0.0.1` or `::1`, never another address. If the port is taken, it logs once and tries again every 60 seconds. It stops with the router.

### Auth

- One token per service: `assistant service key <svc>` makes one (replacing the old), prints it once; `assistant service revoke <svc>`; `assistant service list` names the services with a key. `<svc>` is `[a-z0-9-]{1,32}`.
- Kept as SHA-256 hashes only, in `state/service-keys.json`, through `keyring.KeyRing` (as the siri, locations and reminders add-ins keep theirs).
- Every request: `Authorization: Bearer <token>`; unknown or missing gives 401. The service name in every record and log line comes from the token, never from the request.

### Who receives

- The owner (`role: owner` in `users.json`) by default. A request may name `to`, a person in `users.json`, only if `config.json` allows it for that service: `"service_notify": {"services": {"bambu": {"to": ["ada", "bea"]}}}`. Anything else gives 403. The owner is always allowed.

### The API

JSON in and out, `/v1/` prefix, request body at most 20 MB.

- `POST /v1/messages`: `{"text": Markdown, "to"?: name, "buttons"?: [[{"id", "text"}]], "files"?: [{"name", "data": base64}], "silent"?: bool, "dedupe"?: key, "expires_in"?: seconds, "once"?: bool (default true)}` → `{"message_id"}`, or `{"message_id", "duplicate": true}` when a message with the same `dedupe` key was sent by this service in the last 7 days (it sends nothing then). `buttons` are rows of at most 8 buttons each, at most 24 in all; `id` is the service's own (at most 128 characters), `text` the label. Pictures (`.jpg`, `.jpeg`, `.png`, `.webp`) go as photo media blocks below the text, anything else as a document, at most 10 files, as `tg send` builds them. `silent: true` sets `disable_notification`; otherwise quiet hours decide. `expires_in` (at most 7 days) is when the buttons stop working on the router's side too.
- `PATCH /v1/messages/<id>`: `{"text"?, "buttons"?, "expires_in"?}`. `buttons: []` takes them off. Only a message this service sent; another one gives 404. Editing buttons back onto a message makes it pressable again (a new `once`).
- `DELETE /v1/messages/<id>`: deletes the service's own message (Telegram allows it for 48 hours).
- `GET /v1/presses?wait=<s>`: the presses waiting for this service, oldest first, held up to `wait` seconds (at most 25) while there are none. A press is handed over once: it leaves the queue in the same locked step that writes the response (at most once, see Buttons and presses). Each press: `{"seq", "message_id", "button_id", "label", "by": name, "at": unix time}`.
- `GET /v1/health`: `{"ok": true, "telegram": "reachable" | "unreachable"}` (the router's network check), for a service's own checks. It needs the token too, so a scan of the port learns nothing.

Errors are `{"error": code, "detail"?}`: 400 `bad_request`, 401 `unauthorized`, 403 `recipient_not_allowed`, 404 `not_found`, 413 `too_large`, 429 `rate_limited` with `retry_after`, 502 `telegram_refused` (Telegram's description), 503 `telegram_unreachable`. A service message is never held in the core's outbox: a confirmation sent late only confuses.

### Buttons and presses

- Telegram never sees a service's button ids. Each button's callback data is `ask:<n>`, as `tg ask` makes, so taps pass the plugin patch and `telegram_entrance` unchanged in both modes. The message is kept with `questions.save` in `state/asks/<chat>/<msg>.json` as kind `service`, with `service`, the buttons (`id`, `text`), `once`, `expires` and `answer`.
- A tap reaches `asks.answer`, which (new) asks the add-ins first through a hook (below). The add-in, under a lock per message:
  - checks the kept kind is `service` and the button index is in range;
  - if the message is past `expires`: edits it to its text plus "⌛ Expired: nothing was done." with no buttons, records nothing for the service;
  - if `once` and already answered: ignores the tap (its buttons are already gone; this catches a race);
  - otherwise marks it answered (when `once`), appends the press to the service's queue on disk, wakes the service's long-poll, and edits the message at once: buttons off (when `once`), text plus "⏳ <label>…". The service then edits it to the outcome.
- A press not handed over to the service within 2 minutes is dropped from the queue under the same lock the hand-over takes, and the message is edited to "⚠️ <svc> didn't pick this up in time: nothing was done." So a press the owner was told had no effect can never reach the service later. The check runs on `on_tick` and also after a router restart, from the presses on disk.
- Delivery is at most once (builder check, my call). Once a press is in a response, the router never says "nothing was done" about it and never hands it over again, so slow work after a press (an FTPS upload before a print starts) can't be contradicted by the router. If the service dies between receiving and acting, the press is lost and the message stays at "⏳ <label>…": for a print confirmation a lost press is the safe failure, and the owner presses again on a new confirmation. A service that wants more does its own bookkeeping.
- `once: false` keeps the buttons on after a press, for buttons such as "📷 Snapshot now"; each press is queued.
- Every message with buttons expires (builder check, my call): `expires_in` defaults to 24 hours and is at most 7 days, for `once: false` buttons too. On `on_tick` the add-in takes the buttons off each message past its expiry ("⌛ Expired: nothing was done." added to its text), at most a few edits a second. The kept file in `state/asks/` lives 30 days, so a service button is never left on a message after its file is gone (a tap on an `ask:` button with no kept file is typed into the assistant, which is how `tg ask` works). If keeping the file fails after Telegram took the message, the add-in takes the buttons off at once and answers 500 `not_kept`. The add-in's `teardown` takes the buttons off every live service message, so turning it off leaves none.
- Everything else a service does with a press (its own token's expiry, its own "already done") is the service's.

### Presses from the Mini App (2026-11-28)

- A router add-in may press a service's button for the person whose Mini App page asked: `taps.press_from_app(user, service, message_id, index, now)`. It runs the same checks as a Telegram tap, under the same message lock (the message is that service's and in that person's chat, the button exists, it isn't answered when `once`, it hasn't expired), queues the press, wakes the service's long-poll and edits the message to "⏳ <label>… (Mini App)". It answers None when queued, else `unknown`, `other_service`, `no_button`, `answered` or `expired`.
- Every press carries `via`: `telegram` for a tap, `miniapp` for this. `GET /v1/presses` hands it over, so a service can log where a confirmation came from. To the service a press is a press: the same button, the same checks.
- The button ids never leave the router: the page sees labels and positions only, which the add-in gets from its own service. The add-in checks the person may press (bambu-mcp: the owner only), and the relay has already checked the page's signature.
- A tap after a press from the app, or the reverse, is the existing "already answered".

### Core changes

1. A new hook, `question_tapped(user, tap, path, ask, now) -> bool`, called in `asks.answer` for a kept question whose `kind` the core doesn't know, before anything else is done with it. True means an add-in took it.
2. Fail closed: a kept question with a `kind` the core doesn't know that no add-in takes is never delivered to an assistant. Its buttons are taken off and it's logged. Today such a question with a text would be typed into the assistant.
3. `service-notify` in `addons.ALL`, the hook in the hooks' docstring.

No other core change. Nothing touches `work`, sessions or `asleep`, so no assistant is woken.

### State on disk

`state/service-notify/<svc>/`: `presses.jsonl` (the presses not yet handed over), `seq` (the last press number), `sent.json` (message id → chat, kept 30 days, for the ownership check on edit and delete), `dedupe.json` (key → message id, kept 7 days). Edited under `jsonfile` locks, so the HTTP threads and the tick don't race.

### Limits

Per service: 20 new messages a minute, 60 edits and deletes a minute (a sliding window in memory; a restart forgets it). On top of that, Telegram's own `retry_after` is passed back as 429. At most 4 long-polls held at once per service; a fifth gets 429.

### Logging

One line per request: the service (from the token), method, path with the message id, status and duration, and text length and file count for a send. Never the token, the Authorization header, button ids, labels' service ids, file contents or message text. A rejected token is logged as "unknown token" with nothing of it.

### /clear and quiet hours

- Service messages are recorded with `ChatHistory.remember`, so the owner's `/clear` deletes them like any of the bot's messages. A press on a deleted message can't happen; the service sees its token expire.
- Quiet hours apply through `before_telegram_send` like for every sender; `silent` only adds silence.

### Threat model

The button stops what the review was about: a model deciding the owner meant yes. The press never passes through an assistant, the button ids never reach Telegram or a transcript, and the press is checked against the kept message, its chat and its expiry.

It does not stop an assistant set on forging a confirmation. Assistants are full admins under the same Linux user: one could read a service's token from the service's config, read `state/`, or, in plugin mode, append a forged tap to the front desk's transcript, which the router trusts. Closing those needs the router and services under users the assistants aren't, which is outside this add-in. The README says this plainly, and the assistants' rules say that service buttons are the owner's alone.

Front desk mode: in plugin mode each tap first reaches the front desk session (Haiku, no tools) as an inbound message with the button's label, so a model reads it, and the transcript-forging path above exists. In router mode (`"frontdesk": "router"`) taps come from `getUpdates` straight into the router and no model sees them. Workshop runs router mode (owner, 2026-11-22). The add-in works in both modes; its README and `next-steps` say confirmations are trustworthy only in router mode, and `next-steps` warns when `config.frontdesk()` is "plugin".

### Python client

`addons/service-notify/client/multiclaude_service.py`, one file, standard library only (`urllib`, `json`, `base64`), for a service to copy:

```python
from multiclaude_service import Service

svc = Service("http://127.0.0.1:8790", token)   # retries connection errors (router restarting)
mid = svc.send("🖨️ Plate 1 ready: 2 h 14 m, 38 g PLA", files=["preview.png"],
               buttons=[[("print:7f3a", "Print plate 1"), ("cancel:7f3a", "Cancel")]],
               expires_in=600, dedupe="confirm:job42")
svc.edit(mid, "✅ Printing plate 1")
for press in svc.presses():                       # long-polls; each press comes once
    ...
```

## Left to the builder

- Whether `editMessageText` with a rich message keeps the media blocks of a message sent with pictures. If not, keep each media block's `file_id` from the send result and send them again with the edit.
- The exact Markdown of the router's own notes on a message ("⏳ …", "⌛ …", "⚠️ …").
- Thread and lock layout inside the add-in, and how the long-poll is woken (a `threading.Condition` per service is the obvious one).
- The test split between `tests/addons/test_service_notify.py` (with a real listener on an ephemeral port and the fake Bot API) and a core test for the fail-closed rule.
- Wording of `rules.md`, `README.md` and `next-steps`, and the enable/deploy note for workshop.
