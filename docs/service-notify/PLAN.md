# Service messages add-in: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** an add-in `service-notify` that gives local services a token-protected HTTP API on `127.0.0.1` to send the owner Telegram messages with pictures and buttons, edit and delete them, and receive button presses, with no assistant in the loop.

**Architecture:** the add-in starts a `ThreadingHTTPServer` inside the router from its first `on_tick`. It sends with the core's Bot API client. Buttons carry `ask:<n>` and the message is kept as a question of kind `service`, so taps reach the router in both front desk modes; a new core hook `question_tapped` hands them to the add-in, which queues presses on disk for the service's long-poll.

**Tech Stack:** Python 3 standard library only (`http.server`, `threading`, `json`, `base64`), pytest via `./check`.

**Spec:** [docs/service-notify/DESIGN.md](DESIGN.md) (approved 2026-11-22). Read it first: this plan does not repeat its reasons.

## Global Constraints

- Standard library only, in the router and in the client. No new process.
- The core never imports the add-in (`test_the_core_never_imports_an_add_in`). The core learns of it only through hooks.
- Bind only `127.0.0.1` or `::1`; default port 8790, `config.json` `"service_notify": {"port": N}`.
- Service names: `[a-z0-9-]{1,32}`. Tokens: `keyring.KeyRing(paths.STATE / "service-keys.json")`, hashes only.
- Never log a token, an Authorization header, a button id, file contents or message text.
- Limits per service: 20 sends a minute, 60 edits or deletes a minute, 4 long-polls held at once; body at most 20 MB; at most 10 files; at most 24 buttons, 8 a row; button id at most 128 chars; `expires_in` default 86400, max 604800; `wait` at most 25; a press not handed over within 120 s is dropped.
- Notes the router writes on a message (exact text): `⏳ <label>…`, `⌛ Expired: nothing was done.`, `⚠️ <svc> didn't pick this up in time: nothing was done.`; each added to the message's text after a blank line, the label through `telegram.markdown_literal`.
- Every change updates its docs (ARCHITECTURE.md, README.md, the add-in's README.md and rules.md) in the same commit. Repo text in English.
- Tests never touch the live system: the `r`/`desk` fixtures record the Bot API (`r.api_calls`), `bot_api` is a local fake for out-of-process code.
- Run tests in a Linux checkout with LF line endings (a clone inside WSL's file system): on the Windows checkout `bin/` scripts have CRLF and 142 baseline tests fail for that reason alone. Baseline must be green there before Task 1.
- Do not commit, push or deploy without the owner's explicit OK in the session. Commit steps below mean "stage and show"; commit only once the owner says so.

## Review Focus

1. A tap on a service button while the add-in is off, or after its kept file is gone or unreadable: must never reach an assistant (Task 1 fail-closed test; Task 4 teardown and sweep tests).
2. Two taps racing on a `once` message (double tap, two devices): exactly one press is queued (Task 4 test with two threads).
3. The service fetching at the same moment the 120 s drop runs: a press is either handed over or dropped with the "didn't pick this up" note, never both (Task 4 test calling `take_presses` and `drop_stale` under the shared lock order).
4. A service that restarts and resends the same event: `dedupe` returns the first message id and sends nothing (Task 3 test), also across a router restart (state on disk).
5. Wrong or missing token, a token of another service editing someone else's message, a request from `to` not allowed: 401, 404, 403, nothing sent, nothing logged of the token (Task 5 tests, with `capsys` checking the log).

## File structure

- `lib/multiclaude/router/asks.py` (modify): call `question_tapped`, fail closed on unknown kinds.
- `lib/multiclaude/addons/__init__.py` (modify): `ALL` gains `"service-notify"`, docstring gains the hook.
- `lib/multiclaude/addons/service_notify/__init__.py`: the hooks (`question_tapped`, `on_tick`, `help_info` not needed).
- `.../service_notify/store.py`: everything on disk: keys, the press queue, sent messages, dedupe, live buttons.
- `.../service_notify/messages.py`: building, sending, editing, deleting messages; kept questions.
- `.../service_notify/taps.py`: presses, expiry sweep, stale drop, the notes.
- `.../service_notify/api.py`: the HTTP handler, auth, limits, long-poll, the listener's start.
- `addons/service-notify/`: `README.md`, `rules.md`, `next-steps`, `assistant-service`, `teardown`, `client/multiclaude_service.py`.
- `tests/addons/test_service_notify.py`, plus a test in `tests/test_router.py` for the fail-closed rule.
- `ARCHITECTURE.md`, `README.md`: the add-in's section, the hook, the add-in list.

---

### Task 1: Core: the `question_tapped` hook and fail-closed taps

**Files:**
- Modify: `lib/multiclaude/router/asks.py` (`answer`)
- Modify: `lib/multiclaude/addons/__init__.py` (`ALL`, docstring)
- Test: `tests/test_router.py` (section "tg ask buttons")

**Interfaces:**
- Produces: hook `question_tapped(user, tap, path, ask, now) -> bool`, called by `asks.answer` for a kept question whose `kind` is not one of the core's (`questions.CHECKLIST`, `work.CANCEL_CLEAR`, `"approval"`), before any other step; `True` means taken. Constant `asks.CORE_KINDS`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_tap_on_a_question_of_a_kind_nobody_takes_never_reaches_the_assistant(r, desk):
    path = r.paths.ASKS / ALICE_CHAT / "42.json"
    write_json(path, {"question": "Print plate 1?", "options": ["Print", "Cancel"], "kind": "service"})

    desk.inbound(channel("Print", msg_id="42", callback_id="cb1", callback_data="ask:0"))

    assert desk.users["alice"].queue == []
    assert ("editMessageReplyMarkup", {"chat_id": ALICE_CHAT, "message_id": "42"}) in r.api_calls
    assert "answer" not in read_json(path)


def test_an_add_in_that_takes_a_tap_gets_it_and_the_core_does_nothing_more(r, desk, monkeypatch):
    write_json(r.paths.ASKS / ALICE_CHAT / "42.json", {"question": "Q", "options": ["A"], "kind": "service"})
    taken = []
    monkeypatch.setattr(r.asks.addons, "call",
                        lambda hook, *a: taken.append((hook, a[1].text, a[3]["kind"])) or [True])

    desk.inbound(channel("A", msg_id="42", callback_id="cb1", callback_data="ask:0"))

    assert taken == [("question_tapped", "A", "service")]
    assert r.api_calls == [] and desk.users["alice"].queue == []
```

(`asks` imports `addons` as `from multiclaude import addons`; the second test patches that module attribute.)

- [ ] **Step 2: Run, expect FAIL** (`pytest tests/test_router.py -k "kind_nobody_takes or takes_a_tap" -q`: the first queues the tap, the second never calls the hook).

- [ ] **Step 3: Implement in `asks.answer`**: right after reading `ask` and before the checklist branch, if `ask.get("kind")` is set and not in `CORE_KINDS`: `if any(addons.call("question_tapped", user, tap, path, ask, now)): return`; else log `"{user.name}: tap on question {msg_id} of kind {kind!r}, which nothing that's on takes; buttons taken off"`, `telegram.later(telegram.api, "editMessageReplyMarkup", chat_id=user.chat_id, message_id=msg_id)` and return. Add `"service-notify"` at the end of `ALL` and the hook to the module docstring in the hooks' style.

- [ ] **Step 4: Run `tests/test_router.py` and `tests/addons/test_addons.py`, expect PASS** (existing tap tests unchanged).

- [ ] **Step 5: Stage** `asks.py`, `addons/__init__.py`, `tests/test_router.py`.

---

### Task 2: The store

**Files:**
- Create: `lib/multiclaude/addons/service_notify/store.py`, `lib/multiclaude/addons/service_notify/__init__.py` (empty docstring module for now)
- Test: `tests/addons/test_service_notify.py`

**Interfaces:**
- Produces, in `store.py`:
  - `NAME_RE`, `ROOT = paths.STATE / "service-notify"`, `KEYS = KeyRing(paths.STATE / "service-keys.json")`, `LIVE = ROOT / "live.json"`.
  - `new_key(name) -> str`, `revoke(name) -> bool`, `names() -> list[str]` (services with a key), `service_of(token) -> str | None` (by `keyring.digest`; `None` for an empty or unknown token).
  - `class Service(name)`: `.dir`, `.lock` (a `jsonfile.locked` path, `dir / "presses.lock"`);
    `add_press(press: dict, now) -> int` (assigns and returns `seq`, stores `queued: now`);
    `take_presses() -> list[dict]` (all waiting, oldest first, removed in the same lock);
    `drop_stale(now, after=120) -> list[dict]` (removes and returns those with `queued` older than `after`);
    `has_presses() -> bool`;
    `remember_sent(msg_id, chat, now)`, `chat_of(msg_id) -> str | None` (entries kept 30 days);
    `dedupe_get(key, now) -> int | None`, `dedupe_put(key, msg_id, now)` (kept 7 days).
  - `live_add(chat, msg_id, expires)`, `live_remove(chat, msg_id)`, `live_due(now) -> list[(chat, msg_id)]`, `live_all() -> list[(chat, msg_id)]`.

- [ ] **Step 1: Write the failing tests** (fixture `store` = `fresh_multiclaude_in` + import `multiclaude.addons.service_notify.store`):
  - `test_a_service_key_is_kept_only_as_its_hash`: `key = store.new_key("bambu")`; `store.service_of(key) == "bambu"`; `key not in store.KEYS.path.read_text()`; `store.service_of("") is None`.
  - `test_presses_are_numbered_handed_over_once_and_gone`: two `add_press` give seq 1 and 2; `take_presses()` returns both in order; a second `take_presses() == []`.
  - `test_a_press_not_taken_within_two_minutes_is_dropped`: add at `now=1000`; `drop_stale(1119) == []`; `[p["seq"] for p in drop_stale(1121)] == [1]`; `take_presses() == []`.
  - `test_sequence_numbers_survive_a_restart`: add one, `take_presses()`, new `Service("bambu")` object, next `add_press` returns 2.
  - `test_dedupe_keys_last_seven_days`: `dedupe_put("k", 7, now=0)`; `dedupe_get("k", 6*86400) == 7`; `dedupe_get("k", 8*86400) is None`.
  - `test_sent_messages_are_known_by_id_and_chat`: `remember_sent(501, "100", 0)`; `chat_of(501) == "100"`; `chat_of(502) is None`.
  - `test_live_buttons_are_due_at_their_expiry`: `live_add("100", 501, 50)`, `live_add("100", 502, 500)`; `live_due(100) == [("100", 501)]`; after `live_remove("100", 501)`, `live_all() == [("100", 502)]`.

- [ ] **Step 2: Run, expect FAIL** (module missing).
- [ ] **Step 3: Implement `store.py`** with `jsonfile.edit_object`/`read_object` for the JSON files and `jsonfile.locked(self.lock)` around every read-modify-write of `presses.jsonl` and `seq`.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Stage.**

---

### Task 3: Sending, editing, deleting

**Files:**
- Create: `lib/multiclaude/addons/service_notify/messages.py`
- Test: `tests/addons/test_service_notify.py`

**Interfaces:**
- Consumes: `store.Service`, `store.live_add/live_remove`.
- Produces, in `messages.py`:
  - `class Refused(Exception)` with `.status` (int), `.code` (str), `.detail` (str | None), `.retry_after` (int | None).
  - `recipient(service, to) -> users.User` (owner when `to` is None; `Refused(403, "recipient_not_allowed")` unless `to` is the owner or listed in `config.read()["service_notify"]["services"][service]["to"]`).
  - `send(service, request: dict, now) -> dict` → `{"message_id": int}` or `{"message_id": int, "duplicate": True}`.
  - `edit(service, msg_id, request: dict, now) -> dict` → `{"message_id": int}`.
  - `delete(service, msg_id) -> dict` → `{"message_id": int}`.
  - `shown(kept: dict, note: str | None) -> dict`: the `rich_message` for a kept message's text plus an optional note (also used by Task 4).
  - `take_buttons_off(chat, msg_id, note)`: edit to `shown(kept, note)` with no buttons, `live_remove` (used by Tasks 4 and 6).
- Kept question (`questions.save(paths.ASKS / chat, msg_id, text, labels, kind="service", ...)`) fields: `service`, `buttons` (`[[{"id", "text"}]]`), `once`, `expires`, `chat`, `media` (the file ids for edits, see below), `answer`.

Validation, each a `Refused(400, "bad_request", detail)`: `text` a non-empty string at most `telegram.RICH_LIMIT`; `buttons` rows of at most 8, at most 24 in all, `id` 1–128 chars, `text` 1–64 chars, ids unique; `files` at most 10, `name` a plain file name, `data` valid base64; `expires_in` 1–604800; `dedupe` at most 128 chars. A message with buttons gets `expires = now + (expires_in or 86400)`.

Building: text through `telegram.without_highlights`; each file `n` as `![](tg://<kind>?id=m<n>)` after the text and a media entry `{"id": f"m{n}", "media": {"type": kind, "media": f"attach://file{n}"}}`, uploads written to a temporary directory removed after the call (`BotApi.call` takes paths); kind `photo` for `.jpg .jpeg .png .webp`, else `document`. Buttons as `{"inline_keyboard": [[{"text", "callback_data": f"ask:{n}"}]]}` numbered across rows. `silent` → `disable_notification=True`. Sent with `telegram.bot().call("sendRichMessage", ...)` (never `send_or_hold`), then `ChatHistory(chat).remember(msg_id)`, `remember_sent`, `dedupe_put`, and for buttons the kept question and `live_add`. If keeping the question fails: `take_buttons_off` and `Refused(500, "not_kept")`.

Media on edit (the Bot API docs don't say whether an edit keeps them): from the send result take, for each media block in `result.get("rich_message", {}).get("blocks", [])` in order, the photo's last `file_id` or the document's `file_id`, kept as `media`; an edit sends the same `tg://` references with those file ids as each entry's `media`. Without file ids in the result, an edit sends the text alone and logs `"service {svc}: message {id} edited without its pictures"`. Check this on the real bot at deploy (Task 7 note).

Telegram errors: `telegram.Unreachable` → `Refused(503, "telegram_unreachable")`; a `TelegramError` with `retry_after` → `Refused(429, "rate_limited", retry_after=...)`; any other → `Refused(502, "telegram_refused", detail=str(e))`.

- [ ] **Step 1: Write the failing tests** (fixture `svc`: the `desk` fixture's users, `addons_on("service-notify")`, import `messages`; `r.RecordingBot` returns `{"message_id": 900+n}`):
  - `test_a_message_goes_to_the_owner_as_a_rich_message`: `send("bambu", {"text": "Started"}, 0) == {"message_id": 901}`; the call is `sendRichMessage` to `ALICE_CHAT` with markdown `"Started"`; `"901"` is in `ChatHistory(ALICE_CHAT)` records (so `/clear` deletes it).
  - `test_buttons_carry_ask_numbers_and_the_ids_stay_on_the_machine`: buttons `[[{"id": "print:tok", "text": "Print plate 1"}, {"id": "cancel:tok", "text": "Cancel"}]]`; `reply_markup == {"inline_keyboard": [[{"text": "Print plate 1", "callback_data": "ask:0"}, {"text": "Cancel", "callback_data": "ask:1"}]]}`; `"tok"` appears nowhere in `json.dumps(r.api_calls)`; the kept file has `kind == "service"`, `service == "bambu"`, `expires == 86400`, `once is True`; `store.live_all() == [(ALICE_CHAT, 901)]`.
  - `test_pictures_go_as_media_blocks_below_the_text`: one `snap.jpg`, one `log.txt`; markdown ends with `![](tg://photo?id=m0)\n\n![](tg://document?id=m1)`; the call's `files` has `file0`, `file1` whose contents are the decoded bytes.
  - `test_a_repeated_dedupe_key_sends_nothing_and_gives_the_first_id`: two sends with `dedupe="started:job42"`; second returns `{"message_id": 901, "duplicate": True}`; one `sendRichMessage` in all.
  - `test_another_person_only_when_the_owner_allowed_it`: `to="bob"` → `Refused` with status 403; after `config.json` `{"service_notify": {"services": {"bambu": {"to": ["bob"]}}}}` it goes to `BOB_CHAT`.
  - `test_silent_messages_ask_for_no_notification`.
  - `test_an_edit_changes_only_the_services_own_message`: edit of 901 by `bambu` calls `editMessageText`; edit of 901 by `orchestra` → 404; edit of 777 → 404.
  - `test_an_edit_with_no_buttons_takes_them_off_and_ends_the_live_entry`.
  - `test_an_edit_keeps_the_pictures_by_their_file_ids`: RecordingBot result for the send includes `{"rich_message": {"blocks": [{"type": "paragraph"}, {"type": "photo", "photo": [{"file_id": "S"}, {"file_id": "L"}]}]}}`; the edit's `rich_message.media == [{"id": "m0", "media": {"type": "photo", "media": "L"}}]`.
  - `test_telegram_out_of_reach_is_503_and_nothing_is_held`: `r.unreachable = True` → status 503; `telegram.Outbox(ALICE_CHAT).waiting()` is False.
  - `test_bad_requests_are_400` (parametrized: empty text, 9 buttons in a row, 25 buttons, duplicate ids, 11 files, bad base64, `expires_in` 0 and 604801, a file name with `/`).
  - `test_a_delete_removes_the_message_and_its_live_buttons`.

RecordingBot needs to return a configurable result: extend it in `conftest.py` so `r.results[method]` (a dict merged into the default result) is honoured; keep current behaviour when unset.

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement `messages.py`.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Stage.**

---

### Task 4: Taps, expiry and stale presses

**Files:**
- Create: `lib/multiclaude/addons/service_notify/taps.py`
- Modify: `lib/multiclaude/addons/service_notify/__init__.py` (hooks `question_tapped`, `on_tick`)
- Test: `tests/addons/test_service_notify.py`

**Interfaces:**
- Consumes: `store`, `messages.shown`, `messages.take_buttons_off`, hook signature from Task 1.
- Produces: `taps.tapped(user, tap, path, ask, now) -> bool`; `taps.sweep(now)` (due live buttons off with the expired note; stale presses dropped with the missed note); `taps.WAKE` = `{service: threading.Condition}` that `add_press` paths notify (Task 5 waits on it); `__init__.question_tapped = taps.tapped`; `__init__.on_tick(desk, now)` runs `taps.sweep` at most every 5 s and starts the listener (Task 5) once.

Under `jsonfile.locked(path.with_suffix(".lock"))`, re-reading the kept file: kind not `service` → `False`; index from `tap.attrs["callback_data"]` (`ask:<n>`) out of range → log, `True`; `now > expires` → `take_buttons_off(chat, msg, "⌛ Expired: nothing was done.")`, `True`; `once` and `answer` set → `True`; else `add_press({"message_id", "button_id", "label", "by": user.name, "at": now}, now)`, notify `WAKE[service]`, and when `once`: save `answer` = button id index, edit to `shown(kept, f"⏳ {label}…")` with no buttons, `live_remove`. Lock order everywhere: the kept question's lock, then the service's press lock.

`sweep`: for each `live_due(now)` → `take_buttons_off(..., "⌛ Expired: nothing was done.")` and mark the kept file answered `"expired"`; for each service with a key, `drop_stale(now)` and for each dropped press edit its message to `shown(kept, f"⚠️ {svc} didn't pick this up in time: nothing was done.")`.

- [ ] **Step 1: Write the failing tests** (send through `messages.send` first, then tap with `desk.inbound(channel(label, msg_id="901", callback_id="cb1", callback_data="ask:0"))`):
  - `test_a_tap_queues_a_press_for_the_service_and_never_the_assistant`: `desk.users["alice"].queue == []`; `Service("bambu").take_presses()` has one press with `button_id "print:tok"`, `by "alice"`, `message_id 901`; the last edit's markdown ends with `"⏳ Print plate 1…"` and has no `reply_markup` buttons.
  - `test_a_second_tap_on_a_once_message_is_ignored`: two taps, one press.
  - `test_two_taps_at_once_queue_one_press`: two threads calling `taps.tapped` on the same kept file; one press.
  - `test_a_tap_after_expiry_says_so_and_queues_nothing`: send with `expires_in=600` at 0, tap with `now=601` (patch `time.time` in desk or call `asks.answer` directly with `now`); no press; edit ends with `"⌛ Expired: nothing was done."`.
  - `test_buttons_that_stay_queue_every_tap`: `once: False`, two taps → two presses, no edit.
  - `test_expired_buttons_come_off_by_themselves`: send with `expires_in=600`; `taps.sweep(601)` edits with the expired note; `live_all() == []`.
  - `test_a_press_the_service_never_took_is_dropped_and_the_owner_told`: tap at 0; `taps.sweep(121)` → no presses left, edit ends with `"⚠️ bambu didn't pick this up in time: nothing was done."`.
  - `test_a_press_handed_over_is_never_called_missed`: tap at 0; `take_presses()`; `sweep(121)` makes no edit.
  - `test_nothing_happens_while_the_add_in_is_off`: add-in off, kept `service` file present, tap → core fail-closed (buttons off, no queue, no press).
  - `test_a_waiting_long_poll_wakes_on_a_press`: a thread waits on `WAKE["bambu"]` with timeout 2; tap; it wakes in under 1 s.

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement `taps.py` and the hooks.**
- [ ] **Step 4: Run all of `tests/addons/test_service_notify.py` and `tests/test_router.py`, expect PASS.**
- [ ] **Step 5: Stage.**

---

### Task 5: The HTTP API

**Files:**
- Create: `lib/multiclaude/addons/service_notify/api.py`
- Modify: `lib/multiclaude/addons/service_notify/__init__.py` (start the listener from `on_tick`)
- Test: `tests/addons/test_service_notify.py`

**Interfaces:**
- Consumes: `store.service_of`, `messages.send/edit/delete/Refused`, `store.Service.take_presses`, `taps.WAKE`.
- Produces: `api.Listener(port, host="127.0.0.1", network=None)` with `.start() -> bool` (False and one log line when the port is taken), `.address`, `.close()`; `api.start_once(desk, now)` used by `on_tick` (port from config, retry every 60 s, `network=desk.network`).

Routes (JSON bodies; `Content-Type` not checked; responses `application/json`): `POST /v1/messages`, `PATCH /v1/messages/<id>`, `DELETE /v1/messages/<id>`, `GET /v1/presses?wait=<s>`, `GET /v1/health` → `{"ok": true, "telegram": "reachable"|"unreachable"}`. Anything else 404 `not_found`. Auth first on every route (401 `unauthorized`, logged `"service-notify: unknown token from 127.0.0.1"`); body over 20 MB → 413 `too_large` before reading it; invalid JSON → 400. Limits in memory per service with sliding 60 s windows (20 sends, 60 edits plus deletes) → 429 with `retry_after` seconds as an int ≥ 1. Presses: if none, wait on `taps.WAKE[svc]` up to `min(wait, 25)`; more than 4 waiting for one service → 429. One log line per request: `service-notify: <svc> <METHOD> <path without query> <status> <ms>ms` plus `text=<len> files=<n>` for a send. `log_message` overridden to log nothing else.

- [ ] **Step 1: Write the failing tests** (fixture `listener`: `svc` fixture plus `api.Listener(0).start()`, a token from `store.new_key("bambu")`, a helper `call(method, path, body=None, token=TOKEN) -> (status, json)` on `urllib.request`):
  - `test_a_request_without_a_valid_token_is_401_and_the_token_is_not_logged` (with `capsys`: the wrong token string not in output).
  - `test_send_edit_delete_over_http`: 200s, `message_id` round-trips.
  - `test_presses_long_poll_returns_as_soon_as_a_tap_comes`: a thread taps after 0.2 s; `GET /v1/presses?wait=5` returns in under 2 s with one press; a second `GET ?wait=0` returns `[]`.
  - `test_the_fifth_long_poll_is_429`.
  - `test_sends_over_twenty_a_minute_are_429_with_retry_after`.
  - `test_a_body_over_20_mb_is_413` (send a `Content-Length` header over the limit with a small body; the server must not wait for it).
  - `test_health_says_whether_telegram_is_reachable` (a stub network with `online()` False → `"unreachable"`).
  - `test_the_listener_binds_only_the_loopback`: `Listener(0, host="0.0.0.0")` raises `ValueError`.
  - `test_a_taken_port_is_logged_and_start_says_false`.
  - `test_the_log_has_no_text_no_ids_and_no_token` (send with text "SECRET-TEXT" and button id "tok-123"; neither in `capsys` output).

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement `api.py` and wire `start_once` into `on_tick`.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Stage.**

---

### Task 6: CLI, teardown, Python client, docs

**Files:**
- Create: `addons/service-notify/assistant-service` (Python, executable, second line `# assistant service key|revoke|list [<svc>]: tokens for local services that message the owner`), `addons/service-notify/teardown` (Python, executable), `addons/service-notify/next-steps`, `addons/service-notify/rules.md`, `addons/service-notify/README.md`, `addons/service-notify/client/multiclaude_service.py`
- Modify: `ARCHITECTURE.md` (hook list, an "### Service messages" section after Backup, Code layout), `README.md` (add-in list)
- Test: `tests/addons/test_service_notify.py`

**Interfaces:**
- Consumes: `store.new_key/revoke/names`, `store.live_all`, `messages.take_buttons_off`, the HTTP API.
- Produces: client `Service(url, token, timeout=35, retries=5)` with `send(text, buttons=None, files=None, to=None, silent=False, dedupe=None, expires_in=None, once=True) -> int`, `edit(message_id, text=None, buttons=..., expires_in=None) -> int`, `delete(message_id)`, `presses(wait=25)` (a generator that long-polls forever), `health() -> dict`, and `ServiceError(status, code, detail, retry_after)`. `buttons` accepts rows of `(id, text)` tuples or dicts; `files` accepts paths or `(name, bytes)`. Connection errors are retried with 1, 2, 4… s backoff up to `retries` (a router restart); 429 waits `retry_after` once; other errors raise.

`next-steps` prints: how to make a token, the port, the client's path, and when `config.frontdesk()` is `"plugin"` a warning that confirmations are trustworthy only with `"frontdesk": "router"`. `teardown`: `take_buttons_off(chat, msg, "⌛ Expired: nothing was done.")` for every `live_all()`, exit 0 even if Telegram is unreachable (log it). `rules.md`: service messages exist, their buttons are the owner's alone, a tap never reaches the assistant, never claim a confirmation was given, point the person at the button. README: what it does, the threat model paragraph from the spec, setup (`assistant addon enable service-notify`, `assistant service key <svc>`), the API reference, the Python example, limits, logging, and the deploy note (below).

- [ ] **Step 1: Write the failing tests:**
  - `test_assistant_service_key_prints_a_token_once_and_list_names_the_service` (run the script with `subprocess` and the test env).
  - `test_teardown_takes_the_buttons_off_every_live_message` (against `bot_api`, with `TELEGRAM_STATE_DIR` set so the token comes from a fake `.env`).
  - `test_the_client_sends_and_gets_a_press` (client against a `Listener(0)`; a tap through `taps.tapped`; `next(svc.presses(wait=5))["button_id"] == "print:tok"`).
  - `test_the_client_retries_while_the_listener_is_down` (start the listener 0.5 s after the first call).
  - `test_the_client_needs_nothing_outside_the_standard_library` (import the file with `python3 -I` in a subprocess with an empty `PYTHONPATH`).

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement the scripts, the client and the docs.** On Windows, mark scripts executable in git: `git update-index --chmod=+x addons/service-notify/assistant-service addons/service-notify/teardown`.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Stage.**

---

### Task 7: Full check and the deploy note

- [ ] **Step 1: Run `./check` in the WSL clone.** Expected: `✔ all checks passed`. Add `addons/service-notify/teardown` and `assistant-service` only if they are bash (they are Python: ruff covers them via `pyproject.toml`; confirm ruff includes them, else add their paths).
- [ ] **Step 2: Write the deploy note** in the add-in's README, section "Turning it on (workshop)": `assistant status` shows every assistant asleep or idle; copy the branch (git bundle + scp into `~/assistant`); `assistant install`; `assistant addon enable service-notify` (restarts the router); `assistant service key bambu` and put the token in bambu-service's environment file (mode 600); check with `curl -s -H "Authorization: Bearer $T" http://127.0.0.1:8790/v1/health`; send one message with a picture and edit it, to confirm the picture stays (Task 3's open point).
- [ ] **Step 3: Self-review the diff against DESIGN.md** (every "Decided" bullet has code and a test) and show the owner the staged change. Commit only on the owner's OK.
