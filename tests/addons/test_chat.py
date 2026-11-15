"""Tests for the chat add-in (lib/multiclaude/addons/chat): what tg keeps in the outbox, messages
from the Mini App, the feed built from the transcript and the outbox, secrets kept off it, the
memory it may use, and nothing of it while the add-in is off. IDs in brackets are the acceptance
criteria of docs/chat-miniapp/ACCEPTANCE.md."""
import json
import os
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import ALICE_CHAT, BOB_CHAT, RELAY, TOKEN, append, channel, signed, write_json

CID = "0b9f3c2e-5d4a-4c1b-9e8f-7a6b5c4d3e2f"
OTHER_CID = "1c0a4d3f-6e5b-4d2c-8f9a-8b7c6d5e4f30"


# --- the router's side ---------------------------------------------------------------------------

@pytest.fixture
def chat(r, desk, addons_on, monkeypatch):
    """The chat add-in on for the router, with the relay set up, every PUT on it recorded in
    chat.puts as (path, body), and background work done at once."""
    addons_on("chat")
    from multiclaude.addons import chat
    from multiclaude.addons.chat import events, feed, files
    from multiclaude.router import relay_entrance
    monkeypatch.setattr(relay_entrance, "settings", lambda: RELAY)
    chat.puts = []
    monkeypatch.setattr(relay_entrance, "put", lambda url, token, body: chat.puts.append(
        (url.removeprefix("https://relay.test"), json.loads(body))))
    monkeypatch.setattr(chat, "later", lambda fn, *args: fn(*args))
    monkeypatch.setattr(r.telegram, "bot_token", lambda: TOKEN)
    monkeypatch.setattr(feed, "PUT_EVERY", 0)
    monkeypatch.setattr(feed, "PUT_RETRY", 0)
    chat.events, chat.feed, chat.files = events, feed, files
    return chat


def message(text="hello", chat=ALICE_CHAT, client_id=CID, signed_at=None, queued_at=None, **extra):
    """A "chat" envelope as the relay queues it."""
    now = time.time()
    return {"type": "chat", "envelope_id": f"chat:{chat}:{client_id}", "payload": {
        "init_data": signed(chat, auth_date=int(signed_at or now)), "client_id": client_id, "text": text,
        "queued_at": queued_at or now, **extra}}


def delivered(user):
    return [w for w in user.queue if type(w).__name__ == "Deliver"]


def dm_copies(r, chat=ALICE_CHAT):
    return [(p["rich_message"]["blocks"], p.get("disable_notification")) for m, p in r.api_calls
            if m == "sendRichMessage" and p["chat_id"] == chat]


def copy_text(blocks):
    return "\n\n".join(b["text"] if isinstance(b["text"], str) else "".join(
        t if isinstance(t, str) else f"`{t['text']}`" for t in b["text"]) for b in blocks)


def test_a_message_from_the_mini_app_is_shown_in_the_dm_and_typed_into_the_assistant(r, desk, chat, monkeypatch):
    """[AC-2.3]"""
    alice = desk.users["alice"]
    desk.relay_inbound(message("hello\nsecond line"))

    ((blocks, quiet),) = dm_copies(r)
    assert copy_text(blocks) == "💬 hello\nsecond line" and quiet is True
    (item,) = delivered(alice)
    assert item.msg_id == "901"
    assert item.header.startswith("[Mini App from Alice · msg 901 · ")
    assert item.header.endswith(" UTC] My message is pasted below; it is my own request to you:")
    assert item.body == "hello\nsecond line"

    monkeypatch.setattr(alice, "ready", lambda: True)
    r.work.serve(alice, time.time())
    pasted = [stdin for args, stdin in r.keystrokes if args[0] == "load-buffer"]
    typed = [args[-1] for args, _ in r.keystrokes if args[0] == "send-keys" and "-l" in args]
    assert pasted == [b"\nhello\nsecond line"]
    assert typed == [item.header]
    assert [args[1] for args, _ in r.keystrokes if args[0] == "paste-buffer"] == ["-p"]


def test_a_retry_is_taken_once_and_the_same_id_from_two_people_twice(r, desk, chat):
    """[AC-2.4] The relay forgets an envelope once the router has it, so a retry comes again."""
    desk.relay_inbound(message())
    desk.relay_inbound(message())
    desk.relay_inbound(message(chat=BOB_CHAT))

    assert len(delivered(desk.users["alice"])) == 1
    assert len(delivered(desk.users["bob"])) == 1
    assert len(dm_copies(r)) == 1


def test_a_long_message_is_cut_in_the_dm_but_reaches_the_assistant_whole(r, desk, chat):
    """[AC-2.14]"""
    text = "x" * 10_000
    desk.relay_inbound(message(text))

    ((blocks, _),) = dm_copies(r)
    assert copy_text(blocks) == "💬 " + "x" * 3000 + "… (full text in the Mini App)"
    assert delivered(desk.users["alice"])[0].body == text
    assert chat.dm_copy("", ["IMG_0042.jpg"]) == "💬 📎 IMG_0042.jpg"
    assert chat.dm_copy("y" * 3500) == "💬 " + "y" * 3500


def test_a_message_never_switches_model_or_effort(r, desk, chat):
    """[AC-3.9]"""
    desk.relay_inbound(message("hi", model="haiku", effort="max"))

    assert [type(w).__name__ for w in desk.users["alice"].queue] == ["Deliver"]


def test_a_model_picked_before_a_message_switches_before_it(r, desk, chat, panel):
    """[AC-3.2] The chip sends the control panel's own tap; the queue keeps the order."""
    from conftest import tap
    tap(panel, "model", target="sonnet", keep=False)
    desk.relay_inbound(message("after the switch"))

    assert [type(w).__name__ for w in desk.users["alice"].queue] == ["Switch", "Deliver"]


@pytest.mark.parametrize("init_data", [signed(ALICE_CHAT, token="999:another-bot"),
                                       signed(ALICE_CHAT, auth_date=int(time.time()) - 25 * 3600)])
def test_a_message_telegram_didnt_sign_for_this_bot_or_lately_is_ignored(r, desk, chat, init_data):
    """[AC-11.2]"""
    envelope = message()
    envelope["payload"]["init_data"] = init_data
    desk.relay_inbound(envelope)

    assert desk.users["alice"].queue == [] and r.api_calls == []


def test_a_message_signed_two_hours_before_it_was_queued_is_refused(r, desk, chat):
    """[AC-11.4] Reads may use a day-old signature; a message, which runs as an admin, an hour-old one."""
    now = time.time()
    desk.relay_inbound(message(signed_at=now - 2 * 3600, queued_at=now))

    assert desk.users["alice"].queue == [] and r.api_calls == []


def test_a_message_that_waited_at_the_relay_while_the_machine_was_offline_still_goes(r, desk, chat):
    """[AC-12.2] Signed 5 hours ago, queued then; the router is back now."""
    now = time.time()
    desk.relay_inbound(message("sent while offline", signed_at=now - 5 * 3600, queued_at=now - 5 * 3600 + 60))
    desk.relay_inbound(message("too old", client_id=OTHER_CID, signed_at=now - 25 * 3600, queued_at=now - 25 * 3600))

    assert [w.body for w in delivered(desk.users["alice"])] == ["sent while offline"]


def test_stop_and_new_chat_are_the_bots_commands_and_no_part_of_the_conversation(r, desk, chat):
    desk.relay_inbound(message("/clear"))

    assert dm_copies(r) == []
    assert [type(w).__name__ for w in desk.users["alice"].queue] == ["Clear"]


# --- the feed ------------------------------------------------------------------------------------

def stamp(n):
    """Transcript time n seconds into the conversation."""
    return datetime.fromtimestamp(1_794_650_400 + n, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def called(use_id, name, data, n):
    return {"type": "assistant", "timestamp": stamp(n), "message": {"role": "assistant", "model": "claude-opus-5-5",
                                                                     "content": [{"type": "tool_use", "id": use_id,
                                                                                  "name": name, "input": data}]}}


def answered(use_id, content, n, error=False):
    return {"type": "user", "timestamp": stamp(n), "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": use_id, "content": content, "is_error": error}]}}


def thought(text, n):
    return {"type": "assistant", "timestamp": stamp(n), "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": text}, {"type": "text", "text": "Let me check."}]}}


def said(text, n):
    return {"type": "user", "timestamp": stamp(n), "message": {"role": "user", "content": text}}


def statusline(r, user, path, **extra):
    """The status line's payload for user, naming path as the session's transcript."""
    write_json(r.paths.CLAUDE_DIR / "state" / f"statusline-{user.name}.json",
               {"transcript_path": str(path), "model": {"display_name": "Opus 5.5"}, "effort": {"level": "high"},
                **extra})


def session_transcript(r, user, name="session.jsonl"):
    folder = Path(user.tail.dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.touch()
    statusline(r, user, path)
    return path


def outbox_record(chat, name, markdown, msg_id, at=None, **extra):
    chat.files.append_outbox(name, {"at": at or time.time(), "chat": ALICE_CHAT, "kind": "send", "msg_id": str(msg_id),
                                    "markdown": markdown, "files": [], "buttons": [], "copy": [], **extra})


def open_page(desk, chat, backfill=True, now=None):
    desk.relay_inbound({"type": "chat_open", "payload": {"user": ALICE_CHAT, "backfill": backfill}})
    chat.on_tick(desk, now or time.time())


def feed_events(chat, chat_id=ALICE_CHAT):
    return [e for path, body in chat.puts if path == f"/chat/{chat_id}" for e in body["events"]]


def shown(chat, kind=None):
    return [e for e in feed_events(chat) if kind is None or e["kind"] == kind]


def test_a_tool_call_is_one_step_a_tg_answer_one_message_and_thinking_nothing(r, desk, chat):
    """[AC-4.3]"""
    alice = desk.users["alice"]
    path = session_transcript(r, alice)
    append(path, said("[Telegram from Alice · msg 5 · 2026-11-14 10:00 UTC] My message is pasted below; it is my own "
                      "request to you:\nrun the tests", 0),
           thought("SECRET PLAN: the user wants tests", 1),
           called("t1", "Bash", {"command": "npm test"}, 2),
           answered("t1", "12 passing", 3),
           called("t2", "Bash", {"command": "tg send <<'EOF'\nAll 12 pass.\nEOF"}, 4),
           answered("t2", "777", 5))
    outbox_record(chat, "alice", "All 12 pass.", 777)

    open_page(desk, chat)

    events = feed_events(chat)
    assert [(e["kind"], e.get("title")) for e in events if e["kind"] == "step"] == [("step", "Ran `npm test`")]
    (answer,) = shown(chat, "assistant_message")
    assert (answer["markdown"], answer["msg_id"]) == ("All 12 pass.", "777")
    (question,) = shown(chat, "user_message")
    assert (question["text"], question["source"]) == ("run the tests", "telegram")
    assert "SECRET PLAN" not in json.dumps(events)
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)


def test_new_entries_and_answers_follow_while_the_page_reads(r, desk, chat):
    alice = desk.users["alice"]
    path = session_transcript(r, alice)
    open_page(desk, chat)
    chat.puts.clear()

    append(path, called("t1", "Read", {"file_path": "/home/ada/workspaces/alice/app.js"}, 1))
    chat.on_tick(desk, time.time())
    append(path, answered("t1", "const x = 1;", 2))
    outbox_record(chat, "alice", "Done.", 812)
    outbox_record(chat, "alice", "Done, edited.", 812, kind="edit")
    chat.on_tick(desk, time.time())

    steps = shown(chat, "step")
    assert [(s["title"], s["status"]) for s in steps] == [("Read `~/workspaces/alice/app.js`", "running"),
                                                         ("Read `~/workspaces/alice/app.js`", "ok")]
    assert len({s["key"] for s in steps}) == 1
    assert [e["kind"] for e in feed_events(chat) if "markdown" in e] == ["assistant_message", "message_edited"]


SECRETS = ("ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123\n"
           "Authorization: Bearer abc.def-ghi\n"
           "password: hunter2\n"
           "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U\n"
           "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----\n")


def test_secrets_in_a_result_are_masked(chat):
    """[AC-4.4]"""
    events, steps = chat.events, chat.events.StepBook()
    events.from_entry(called("t1", "Bash", {"command": "printenv; cat notes"}, 1), steps)
    (step,) = events.from_entry(answered("t1", SECRETS, 2), steps)

    result = step["result_preview"]
    for secret in ("sk-ant-api03", "abc.def-ghi", "hunter2", "eyJhbGci", "b3BlbnNzaC1rZXkt"):
        assert secret not in result
    assert result.count("•••") >= 5


def test_a_secret_across_the_2_kb_cut_is_masked_before_the_cut(chat):
    """[AC-4.4] Masking runs on the whole text; a cut through the key would leave half of it unmatched."""
    events, steps = chat.events, chat.events.StepBook()
    events.from_entry(called("t1", "Bash", {"command": "env"}, 1), steps)
    padding = "x" * (events.RESULT_MAX - 20)
    (step,) = events.from_entry(answered("t1", f"{padding} key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ", 2), steps)

    assert "ABCDEFGH" not in step["result_preview"] and "sk-ant" not in step["result_preview"]
    assert len(step["result_preview"]) <= events.RESULT_MAX


@pytest.mark.parametrize("name, data", [("Read", {"file_path": "/home/ada/.ssh/id_ed25519"}),
                                        ("Bash", {"command": "cat .env"}),
                                        ("Bash", {"command": "cat ~/.claude/channels/telegram/.env"}),
                                        ("Read", {"file_path": "/srv/app/credentials.json"})])
def test_a_file_that_may_hold_secrets_shows_no_result(chat, name, data):
    """[AC-4.4]"""
    events, steps = chat.events, chat.events.StepBook()
    events.from_entry(called("t1", name, data, 1), steps)
    (step,) = events.from_entry(answered("t1", "AKIA… what the file holds", 2), steps)

    assert step["result_preview"] == "[hidden: may contain secrets]"


def test_writing_a_secrets_file_hides_what_was_written_too(chat):
    """[AC-4.4]"""
    events, steps = chat.events, chat.events.StepBook()
    (call,) = events.from_entry(called("t1", "Write", {"file_path": "/w/.env.local", "content": "KEY=plain-value"}, 1),
                                steps)
    (step,) = events.from_entry(answered("t1", "File created", 2), steps)

    assert call["input_preview"] == step["input_preview"] == "[hidden: may contain secrets]"
    assert "plain-value" not in json.dumps([call, step])


def test_a_subagent_is_one_step_however_much_it_did(r, desk, chat):
    """[AC-4.7] Its own transcript holds its 30 calls; the session's has the one that ran it."""
    alice = desk.users["alice"]
    path = session_transcript(r, alice)
    subagents = Path(alice.tail.dir) / "session" / "subagents"
    subagents.mkdir(parents=True)
    inner = [e for n in range(30) for e in (called(f"s{n}", "Grep", {"pattern": "x"}, n),
                                            answered(f"s{n}", "match", n))]
    append(subagents / "agent-1.jsonl", *inner)
    append(path, called("a1", "Task", {"description": "review the diff", "prompt": "Look at it"}, 1),
           *[{**e, "isSidechain": True} for e in inner[:4]],
           answered("a1", "Looks good.", 40))

    open_page(desk, chat)

    assert [s["title"] for s in shown(chat, "step")] == ["Ran agent `review the diff`"]


def test_a_jobs_transcript_in_the_same_folder_stays_out(r, desk, chat):
    """[AC-7.3] A claude -p run writes its own transcript, newer than the session's."""
    alice = desk.users["alice"]
    path = session_transcript(r, alice)
    append(path, called("t1", "Bash", {"command": "ls"}, 1), answered("t1", "a b", 2))
    job = Path(alice.tail.dir) / "job.jsonl"
    append(job, called("j1", "Bash", {"command": "job-only-command"}, 3), answered("j1", "job output", 4))
    os.utime(job, (time.time() + 60, time.time() + 60))

    open_page(desk, chat)
    append(job, called("j2", "Bash", {"command": "job-only-again"}, 5))
    chat.on_tick(desk, time.time())

    assert "job-only" not in json.dumps(feed_events(chat))
    assert [s["title"] for s in shown(chat, "step")] == ["Ran `ls`"]


def test_opening_and_reading_for_ten_minutes_never_wakes_a_sleeping_assistant(r, desk, chat):
    """[AC-1.6]"""
    alice = desk.users["alice"]
    session_transcript(r, alice)
    r.asleep.mark("alice")
    start = time.time()

    open_page(desk, chat, now=start)
    for minute in range(11):
        chat.feed_of(alice).open(start + 60 * minute, False, chat.later)  # the relay's chat_open, once a minute
        for second in range(0, 60, 5):
            chat.on_tick(desk, start + 60 * minute + second)

    assert r.asleep.marked("alice")
    assert alice.queue == [] and r.keystrokes == []
    assert {e["state"] for e in shown(chat, "status")} == {"asleep"}


def test_someone_who_never_opens_the_mini_app_has_nothing_on_the_relay(r, desk, chat):
    """[AC-1.7] A day of Telegram use, /clear included."""
    alice = desk.users["alice"]
    path = session_transcript(r, alice)
    for n in range(20):
        desk.inbound(channel(f"message {n}", msg_id=str(10 + n)))
        append(path, called(f"t{n}", "Bash", {"command": "date"}, n), answered(f"t{n}", "today", n))
        outbox_record(chat, "alice", f"answer {n}", 100 + n)
        chat.on_tick(desk, time.time())
    chat.after_wipe(alice, time.time(), set())

    assert [path for path, _ in chat.puts] == ["/chat/members"]


def test_new_chat_empties_the_outbox_and_tells_the_relay(r, desk, chat):
    """[AC-6.2]"""
    alice = desk.users["alice"]
    session_transcript(r, alice)
    outbox_record(chat, "alice", "old answer", 700)
    open_page(desk, chat)
    chat.puts.clear()

    chat.after_wipe(alice, time.time(), set())

    assert not chat.files.outbox("alice").exists()
    assert [e["kind"] for e in feed_events(chat)] == ["cleared"]


def test_seq_goes_on_after_a_restart_and_a_clear(r, desk, chat):
    alice = desk.users["alice"]
    session_transcript(r, alice)
    open_page(desk, chat)
    last = feed_events(chat)[-1]["seq"]
    chat.after_wipe(alice, time.time(), set())

    restarted = chat.feed.Feed(alice)
    assert restarted.seq >= last + 1


def test_events_wait_for_the_relay_two_hundred_at_most(r, desk, chat, monkeypatch, capsys):
    """[AC-N1c] The oldest go; the page sees seq jump and reads the feed afresh."""
    alice = desk.users["alice"]
    path = session_transcript(r, alice)
    open_page(desk, chat)
    feed = chat._feeds["alice"]
    from multiclaude.router import relay_entrance

    def unreachable(url, token, body):
        raise OSError("relay down")

    monkeypatch.setattr(relay_entrance, "put", unreachable)
    for n in range(250):
        append(path, called(f"t{n}", "Bash", {"command": f"echo {n}"}, n % 3000), answered(f"t{n}", "ok", n % 3000))
    chat.on_tick(desk, time.time())

    assert len(feed.pending) == chat.feed.PENDING_MAX
    assert feed.pending[-1]["seq"] - feed.pending[0]["seq"] == chat.feed.PENDING_MAX - 1
    assert "chat event(s) dropped" in capsys.readouterr().err


def router_posts(r, text, chat_id=ALICE_CHAT):
    """What BotApi.call does with a message the router posts, before it goes (the tests' bot skips it)."""
    from multiclaude import addons
    params = {"chat_id": chat_id, "rich_message": {"blocks": r.telegram.rich_blocks(text)}}
    return addons.through("before_telegram_send", params, chat_id, "sendRichMessage")


def test_a_router_notice_shows_as_a_line_of_its_own_while_the_page_is_open(r, desk, chat):
    alice = desk.users["alice"]
    session_transcript(r, alice)
    router_posts(r, "⚠️ Couldn't switch model: before the page opened")
    open_page(desk, chat)
    router_posts(r, "⚠️ Couldn't switch model: `nope` isn't one")
    chat._posting.on = True  # the 💬 copy of a message is no notice
    router_posts(r, "💬 hello")
    chat._posting.on = False
    router_posts(r, "Bob's notice", BOB_CHAT)
    chat.on_tick(desk, time.time())

    assert [e["text"] for e in shown(chat, "notice")] == ["⚠️ Couldn't switch model: `nope` isn't one"]


def test_the_relay_hears_who_may_use_the_chat_when_users_json_changes(r, desk, chat):
    """[AC-11.5] Within a poll of the router's loop: it rereads users.json on every one."""
    chat.on_tick(desk, time.time())
    write_json(r.paths.USERS, {"alice": {"telegram_id": ALICE_CHAT, "display": "Alice", "role": "owner"}})
    desk.load_users()
    chat.on_tick(desk, time.time())

    assert [body["ids"] for path, body in chat.puts if path == "/chat/members"] == [[ALICE_CHAT, BOB_CHAT], [ALICE_CHAT]]


def test_while_people_try_it_out_only_they_may_use_it(r, desk, chat):
    write_json(r.paths.USERS, {"alice": {"telegram_id": ALICE_CHAT, "display": "Alice", "chat_beta": True},
                               "bob": {"telegram_id": BOB_CHAT, "display": "Bob"}})
    desk.load_users()
    chat.on_tick(desk, time.time())

    assert chat.puts[-1] == ("/chat/members", {"ids": [ALICE_CHAT]})


def test_older_history_reads_a_huge_transcript_a_window_at_a_time(r, desk, chat, monkeypatch):
    """[AC-N1a] A 300 MB transcript: the backfill and three scrolls up read at most 4 MB each, and
    the router's memory stays under 20 MB."""
    alice = desk.users["alice"]
    path = session_transcript(r, alice)
    with open(path, "wb") as f:
        f.truncate(290 * 1024 * 1024)  # a sparse start, so the test doesn't write it all
        f.seek(0, 2)
        f.write(b"\n")
        n = 0
        while f.tell() < 300 * 1024 * 1024:
            for entry in (called(f"t{n}", "Bash", {"command": f"step {n}"}, n % 3000),
                          answered(f"t{n}", "output line\n" * 60, n % 3000)):
                f.write(json.dumps(entry).encode() + b"\n")
            n += 1
    counted = []

    class Counting:
        def __init__(self, f):
            self.f = f

        def read(self, size=-1):
            data = self.f.read(size)
            counted[-1] += len(data)
            return data

        def __getattr__(self, name):
            return getattr(self.f, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

    monkeypatch.setattr(chat.files, "opener", lambda p, mode: Counting(open(p, mode)))

    tracemalloc.start()
    try:
        counted.append(0)
        built, cursor, _ = chat.feed.history(alice, str(path), os.path.getsize(path), None)
        for _ in range(3):
            assert cursor
            counted.append(0)
            older = chat.feed.older(alice, cursor)
            assert older["events"]
            cursor = older["cursor"]
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(built) == chat.feed.BACKFILL
    assert max(counted) <= 4 * 1024 * 1024, counted
    assert peak < 20 * 1024 * 1024


def test_a_huge_line_running_past_a_window_isnt_read_to_its_end(chat, tmp_path, monkeypatch):
    """[AC-N1a] A last line of 20 MB with no newline early on: the window's read stops soon past its end."""
    files, read = chat.files, []
    path = tmp_path / "t.jsonl"
    with open(path, "wb") as f:
        f.write(json.dumps(said("before", 1)).encode() + b"\n")
        f.write(b"x" * (20 * 1024 * 1024) + b"\n")

    def counting(p, mode):
        f = open(p, mode)
        real = f.read

        def read_counted(size=-1):
            data = real(size)
            read.append(len(data))
            return data
        f.read = read_counted
        return f

    monkeypatch.setattr(files, "opener", counting)
    window = 64 * 1024
    found = list(files.lines(path, 0, window))

    assert [line is None for _, _, line in found] == [False]
    assert sum(read) <= window + files.LINE_MAX + files.CHUNK


def test_history_goes_back_to_where_the_shown_events_start(r, desk, chat):
    alice = desk.users["alice"]
    path = session_transcript(r, alice)
    for n in range(80):
        append(path, called(f"t{n}", "Bash", {"command": f"step {n}"}, n), answered(f"t{n}", "ok", n))

    built, cursor, _ = chat.feed.history(alice, str(path), os.path.getsize(path), None)
    older = chat.feed.older(alice, cursor)

    assert [e["title"] for e in older["events"]] + [e["title"] for e in built] == [f"Ran `step {n}`" for n in range(80)]
    assert older["cursor"] is None


# --- tg's side: the outbox -----------------------------------------------------------------------

ALICE = {"ASSISTANT_USER": "alice"}


@pytest.fixture
def tg_chat(tg):
    """bin/tg with the chat add-in on."""
    write_json(tg.root / "config.json", {"addons": ["chat"]})
    return tg


def outbox(tg, name="alice"):
    path = tg.root / "state" / "chat" / "outbox" / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_what_tg_sends_on_stdin_is_kept_exactly(tg_chat):
    """[AC-2.12] tg send <<'EOF' with 3,000 characters and a code block."""
    start = "Report:\n\n```python\nprint('hi')\n```\n"
    text = start + "a" * (3000 - len(start))
    tg_chat("send", stdin=text, env=ALICE)

    (record,) = outbox(tg_chat)
    assert (record["kind"], record["markdown"], record["chat"]) == ("send", text, "100")
    assert record["msg_id"] == str(tg_chat.api.next_id)


def test_text_from_a_command_and_a_file_are_kept_as_sent(tg_chat, tmp_path):
    """[AC-2.12] tg send "$(cat report.md)" and tg send --file x.png "caption"."""
    picture = tmp_path / "x.png"
    picture.write_bytes(b"\x89PNG" + b"0" * 96)
    tg_chat("send", "# Report\n\n- one\n- two", env=ALICE)
    tg_chat("send", "--file", str(picture), "caption", env=ALICE)

    first, second = outbox(tg_chat)
    assert first["markdown"] == "# Report\n\n- one\n- two"
    assert second["markdown"] == "caption"
    assert second["files"] == [{"name": "x.png", "size": 100, "mime": "image/png"}]


def test_questions_checklists_copy_buttons_and_edits_are_kept(tg_chat):
    tg_chat("send", "--copy", "npm test", "Run it", env=ALICE)
    tg_chat("ask", "Deploy now?", "Yes", "No", env=ALICE)
    tg_chat("checklist", "Packing", "socks", "charger", env=ALICE)
    tg_chat("edit", "501", "Run it, please", env=ALICE)

    kinds = [(r["kind"], r["markdown"], r["buttons"], r["copy"]) for r in outbox(tg_chat)]
    assert kinds == [("send", "Run it", [], ["npm test"]), ("ask", "Deploy now?", ["Yes", "No"], []),
                     ("checklist", "Packing", ["socks", "charger"], []), ("edit", "Run it, please", [], [])]
    assert outbox(tg_chat)[-1]["msg_id"] == "501"


def test_a_message_to_another_person_is_kept_with_where_it_went(tg_chat):
    write_json(tg_chat.root / "users.json", {"alice": {"telegram_id": "100", "display": "Alice"},
                                             "bob": {"telegram_id": "101", "display": "Bob"}})
    tg_chat("--chat", "bob", "send", "Hi Bob", env=ALICE)

    (record,) = outbox(tg_chat)
    assert record["chat"] == "101"


def test_an_outbox_that_cant_be_written_never_fails_the_send(tg_chat):
    """[AC-2.13] Here the outbox's folder is a file, which stops root as well as anyone."""
    (tg_chat.root / "state" / "chat").mkdir(parents=True)
    (tg_chat.root / "state" / "chat" / "outbox").write_text("not a folder")

    result = tg_chat("send", "still goes", env=ALICE)

    assert result.returncode == 0
    assert [m for m, _, _ in tg_chat.api.calls] == ["sendRichMessage"]
    assert "couldn't keep message" in result.stderr


def test_jobs_notify_and_scripts_keep_nothing(tg_chat):
    """[AC-7.3] A job's answer, and anything sent without an assistant behind it, isn't the conversation."""
    tg_chat("send", "from a job", env={**ALICE, "ASSISTANT_JOB": "1"})
    tg_chat("send", "from assistant notify", env={"ASSISTANT_USER": ""})

    assert outbox(tg_chat) == []


def test_the_outbox_turns_over_at_5_mb(tg_chat):
    """[AC-N1c]"""
    folder = tg_chat.root / "state" / "chat" / "outbox"
    folder.mkdir(parents=True)
    (folder / "alice.jsonl").write_text("{}\n" * (5 * 1024 * 1024 // 3 + 1))

    tg_chat("send", "after the turn", env=ALICE)

    assert (folder / "alice.jsonl.1").stat().st_size >= 5 * 1024 * 1024
    assert [r["markdown"] for r in outbox(tg_chat)] == ["after the turn"]


# --- off -----------------------------------------------------------------------------------------

def test_while_the_add_in_is_off_tg_keeps_nothing(tg):
    tg("send", "hello", env=ALICE)

    assert not (tg.root / "state" / "chat").exists()


def test_while_the_add_in_is_off_the_router_takes_no_chat_envelopes(r, desk, addons_on):
    addons_on()
    desk.relay_inbound(message())
    desk.relay_inbound({"type": "chat_open", "payload": {"user": ALICE_CHAT, "backfill": True}})

    assert desk.users["alice"].queue == [] and r.api_calls == []


def test_the_menu_button_opens_the_chat_while_it_is_on(r, desk, chat):
    from multiclaude.router import panel
    panel.set_menu_button()

    ((method, params),) = [c for c in r.api_calls if c[0] == "setChatMenuButton"]
    assert params["menu_button"]["text"] == "Chat"
    assert params["menu_button"]["web_app"]["url"] == "https://relay.test/app/?tab=chat"


def test_slash_chat_offers_a_button_that_opens_it(r, desk, chat):
    desk.inbound(channel("/chat"))

    (call,) = [p for m, p in r.api_calls if m == "sendRichMessage"]
    (button,) = call["reply_markup"]["inline_keyboard"][0]
    assert button["web_app"]["url"] == "https://relay.test/app/?tab=chat"
    assert desk.users["alice"].queue == []


# --- what other add-ins bring to the chat (X1–X6) -------------------------------------------------

class Ext:
    """A stand-in add-in "ext" that extends the chat, recording what reaches it."""

    def __init__(self):
        self.__name__ = "multiclaude.addons.ext"
        self.messages, self.stops = [], []
        self.takes = True
        self.assists, self.assist_answer = [], {"text": "Goal: an English brief."}
        self.tab_actions, self.more_tabs = [], []

    def chat_tabs(self, user):
        return [{"id": "runs", "title": "Runs"}, *self.more_tabs]

    def chat_models(self, user):
        return [{"id": "m1", "label": "Model One", "hint": "free", "group": "Elsewhere", "badge": "free"}]

    def chat_actions(self, user):
        return [{"id": "launch", "label": "Launch", "template": "/go {what}",
                 "form": [{"name": "what", "type": "text"}]}]

    def chat_message(self, user, target, text, client_id):
        self.messages.append((user.name, target, text, client_id))
        return self.takes

    def chat_stop(self, user, target):
        self.stops.append((user.name, target))

    def chat_tab_action(self, user, action, values):
        self.tab_actions.append((user.name, action, values))
        return {"text": "started"}

    def chat_assist(self, user, action, field, values):
        self.assists.append((user.name, action, field, values))
        return self.assist_answer

    def chat_backfill(self, user):
        return [{"kind": "model_message", "key": "ext:old", "ts": "2026-11-14T09:00:00.000Z", "target": "ext:m1",
                 "markdown": "an earlier answer", "done": True}]


@pytest.fixture
def ext(r, chat, monkeypatch):
    """The stand-in add-in on, next to the chat."""
    from multiclaude import addons
    fake = Ext()
    real = addons._module
    monkeypatch.setattr(addons, "ALL", (*addons.ALL, "ext"))
    monkeypatch.setattr(addons, "_module", lambda name: fake if name == "ext" else real(name))
    write_json(r.paths.CONFIG, {"addons": ["chat", "ext"]})
    return fake


def test_an_add_ins_tabs_models_and_actions_are_in_the_settings(r, desk, chat, ext):
    """[AC-X1, AC-X2, AC-X5]"""
    session_transcript(r, desk.users["alice"])
    open_page(desk, chat)

    (settings,) = shown(chat, "settings")
    assert settings["tabs"] == [{"id": "ext:runs", "title": "Runs", "addon": "ext", "module": "addons/ext/tab.js"}]
    assert settings["models"] == [{"target": "ext:m1", "label": "Model One", "hint": "free", "group": "Elsewhere",
                                   "badge": "free"}]
    assert [(a["id"], a["template"]) for a in settings["actions"]] == [("ext:launch", "/go {what}")]


def test_with_the_add_in_off_the_chat_offers_none_of_it(r, desk, chat):
    """[AC-X1]"""
    session_transcript(r, desk.users["alice"])
    open_page(desk, chat)

    (settings,) = shown(chat, "settings")
    assert (settings["tabs"], settings["models"], settings["actions"]) == ([], [], [])


def test_a_message_to_a_target_goes_to_its_add_in_and_not_the_assistant(r, desk, chat, ext):
    """[AC-X3] Checked like any message, then no copy in the DM, no Inbound, nothing woken."""
    alice = desk.users["alice"]
    session_transcript(r, alice)
    r.asleep.mark("alice")
    open_page(desk, chat)
    desk.relay_inbound(message("hi there", target="ext:m1"))
    desk.relay_inbound(message("hi there", target="ext:m1"))  # a retry
    chat.on_tick(desk, time.time())

    assert ext.messages == [("alice", "ext:m1", "hi there", CID)]
    assert alice.queue == [] and dm_copies(r) == [] and r.asleep.marked("alice")
    (mine,) = shown(chat, "user_message")
    assert (mine["target"], mine["text"]) == ("ext:m1", "hi there")
    assert shown(chat, "notice") == []


def test_a_target_nobody_takes_says_so(r, desk, chat, ext):
    """[AC-X3]"""
    session_transcript(r, desk.users["alice"])
    open_page(desk, chat)
    ext.takes = False
    desk.relay_inbound(message("hello", target="ext:m1"))
    desk.relay_inbound(message("hello", client_id=OTHER_CID, target="gone:m2"))
    chat.on_tick(desk, time.time())

    assert [e["text"] for e in shown(chat, "notice")] == ["That model isn't available now."] * 2
    assert desk.users["alice"].queue == []


def test_a_message_signed_long_ago_doesnt_reach_a_target_either(r, desk, chat, ext):
    """[AC-X3] The same checks as any message."""
    now = time.time()
    desk.relay_inbound(message("late", target="ext:m1", signed_at=now - 2 * 3600, queued_at=now))
    desk.relay_inbound(message("odd", client_id=OTHER_CID, target="../etc"))

    assert ext.messages == []


def test_stop_with_a_target_stops_that_add_ins_answer(r, desk, chat, ext):
    """[AC-X6]"""
    alice = desk.users["alice"]
    alice.turn.busy = True
    desk.relay_inbound(message("/stop", target="ext:m1"))

    assert ext.stops == [("alice", "ext:m1")]
    assert not alice.stop_requested and ext.messages == []


def test_an_add_ins_events_reach_the_open_page_and_its_backfill(r, desk, chat, ext, capsys):
    """[AC-X4]"""
    alice = desk.users["alice"]
    session_transcript(r, alice)
    assert chat.feed_add(alice, {"kind": "model_message", "key": "ext:1", "markdown": "x", "done": False}) is False
    assert not chat.is_open(alice)
    open_page(desk, chat)
    assert chat.is_open(alice)
    assert chat.feed_add(alice, {"kind": "model_message", "key": "ext:1", "ts": "2026-11-14T10:00:00.000Z",
                                 "target": "ext:m1", "markdown": "Hel", "done": False})
    assert chat.feed_add(alice, {"kind": "addon_state", "addon": "ext", "state": {"runs": []}})
    assert chat.feed_add(alice, {"kind": "status", "target": "ext:m1", "state": "running", "text": "Loading the model…"})
    assert not chat.feed_add(alice, {"kind": "addon_state", "addon": "ext", "state": {"big": "x" * 70_000}})
    with pytest.raises(ValueError):
        chat.feed_add(alice, {"kind": "assistant_message", "markdown": "pretending"})
    chat.on_tick(desk, time.time())

    kinds = [(e["kind"], e.get("key") or e.get("addon") or e.get("target")) for e in feed_events(chat)
             if e["kind"] in ("model_message", "addon_state") or e.get("target")]
    assert kinds == [("model_message", "ext:old"), ("model_message", "ext:1"), ("addon_state", "ext"),
                     ("status", "ext:m1")]
    assert all(e["seq"] for e in feed_events(chat))
    assert "over 65536 bytes; left out" in capsys.readouterr().err


def assist(text="look at the repo", client_id=CID, **extra):
    return message(text, client_id=client_id, kind="assist", action="ext:launch", field="what",
                   values={"what": text, "private": False}, **extra)


def test_a_form_field_is_improved_by_the_add_in_that_brings_it_never_by_the_assistant(r, desk, chat, ext):
    """[AC-F3.1] The button shows while the add-in has chat_assist; a press reaches it, not the assistant."""
    alice = desk.users["alice"]
    session_transcript(r, alice)
    open_page(desk, chat)
    (settings,) = shown(chat, "settings")
    assert settings["assist"] == ["ext"]

    desk.relay_inbound(assist())
    desk.relay_inbound(assist())  # a retry
    chat.on_tick(desk, time.time())

    assert ext.assists == [("alice", "launch", "what", {"what": "look at the repo", "private": False})]
    (result,) = shown(chat, "assist_result")
    assert (result["client_id"], result["text"]) == (CID, "Goal: an English brief.")
    assert alice.queue == [] and dm_copies(r) == [] and shown(chat, "user_message") == []


def test_an_add_in_that_cant_improve_it_leaves_the_text_and_says_why(r, desk, chat, ext):
    """[AC-F3.4] The page keeps the person's text and shows the reason."""
    session_transcript(r, desk.users["alice"])
    open_page(desk, chat)
    ext.assist_answer = {"error": "the gateway timed out"}
    desk.relay_inbound(assist())
    chat._assisting.add("alice")
    desk.relay_inbound(assist(client_id=OTHER_CID))
    chat.on_tick(desk, time.time())

    assert [r["error"] for r in shown(chat, "assist_result")] == ["the gateway timed out",
                                                                  "one at a time: the last one is still being improved"]


def test_without_an_add_in_that_assists_there_is_no_button_and_a_request_goes_nowhere(r, desk, chat):
    """[AC-F3.1]"""
    session_transcript(r, desk.users["alice"])
    open_page(desk, chat)
    desk.relay_inbound(assist())
    desk.relay_inbound(message("odd", client_id=OTHER_CID, kind="assist", action="ext:launch", field="../x", values={}))
    chat.on_tick(desk, time.time())

    assert shown(chat, "settings")[0]["assist"] == []
    assert [r["error"] for r in shown(chat, "assist_result")] == ["that add-in can't do it now"]
    assert desk.users["alice"].queue == []


def test_a_tab_names_its_own_module_and_its_requests_reach_its_add_in(r, desk, chat, ext):
    """A tab module's api.request: a chat message of kind tab_action, to chat_tab_action alone."""
    ext.more_tabs = [{"id": "lists", "title": "Lists", "module": "lists.js"}, {"id": "odd", "title": "Odd", "module": "../x.js"}]
    session_transcript(r, desk.users["alice"])
    open_page(desk, chat)
    assert [t["module"] for t in shown(chat, "settings")[0]["tabs"]] == [
        "addons/ext/tab.js", "addons/ext/lists.js", "addons/ext/tab.js"]

    desk.relay_inbound(message("review", kind="tab_action", action="ext:review", values={"repo": "a/b", "branch": "main"}))
    chat.on_tick(desk, time.time())

    assert ext.tab_actions == [("alice", "review", {"repo": "a/b", "branch": "main"})]
    assert [(r["client_id"], r["text"]) for r in shown(chat, "assist_result")] == [(CID, "started")]
    assert desk.users["alice"].queue == []
