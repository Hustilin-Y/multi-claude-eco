"""Tests for the service-notify add-in (lib/multiclaude/addons/service_notify): service keys and
the state on disk, sending, editing and deleting messages, taps on their buttons, the local HTTP
API, the command, the teardown and the Python client, and nothing of it while the add-in is off."""
import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from conftest import ALICE_CHAT, BOB_CHAT, REPO, channel, read_json, shown, write_json


@pytest.fixture
def store(lib):
    """The add-in's state on disk, in tmp_path."""
    from multiclaude.addons.service_notify import store
    return store


# --- the store ---------------------------------------------------------------------------------

def test_a_service_key_is_kept_only_as_its_hash(store):
    key = store.new_key("printer")

    assert store.service_of(key) == "printer"
    assert key not in store.KEYS.path.read_text()
    assert store.service_of("") is None
    assert store.service_of("nonsense") is None
    assert store.names() == ["printer"]


def test_presses_are_numbered_handed_over_once_and_gone(store):
    service = store.Service("printer")

    assert service.add_press({"button_id": "a"}, 10) == 1
    assert service.add_press({"button_id": "b"}, 11) == 2
    assert [p["button_id"] for p in service.take_presses(0)] == ["a", "b"]
    assert service.take_presses(0) == []
    assert not service.has_presses()


def test_a_press_not_taken_within_two_minutes_is_dropped(store):
    service = store.Service("printer")
    service.add_press({"button_id": "a"}, 1000)

    assert service.drop_stale(1119) == []
    assert [p["seq"] for p in service.drop_stale(1121)] == [1]
    assert service.take_presses(0) == []


def test_sequence_numbers_survive_a_restart(store):
    store.Service("printer").add_press({"button_id": "a"}, 0)
    store.Service("printer").take_presses(0)

    assert store.Service("printer").add_press({"button_id": "b"}, 1) == 2


def test_dedupe_keys_last_seven_days(store):
    service = store.Service("printer")
    service.dedupe_put("k", 7, now=0)

    assert service.dedupe_get("k", 6 * 86400) == 7
    assert service.dedupe_get("k", 8 * 86400) is None
    assert store.Service("builder").dedupe_get("k", 0) is None


def test_sent_messages_are_known_by_id_and_chat(store):
    service = store.Service("printer")
    service.remember_sent(501, "100", 0)

    assert service.chat_of(501) == "100"
    assert service.chat_of("501") == "100"
    assert service.chat_of(502) is None
    assert store.Service("builder").chat_of(501) is None


def test_live_buttons_are_due_at_their_expiry(store):
    store.live_add("100", 501, 50)
    store.live_add("100", 502, 500)

    assert store.live_due(100) == [("100", 501)]
    store.live_remove("100", 501)
    assert store.live_all() == [("100", 502)]


# --- sending, editing, deleting ----------------------------------------------------------------

PNG = b"\x89PNG fake picture"
PRINT = [[{"id": "print:tok", "text": "Print plate 1"}, {"id": "cancel:tok", "text": "Cancel"}]]


def b64(data):
    return base64.b64encode(data).decode()


@pytest.fixture
def svc(r, desk, addons_on):
    """The add-in on, for a desk whose owner is Alice (and Bob a member), its modules as attributes."""
    addons_on("service-notify")
    from multiclaude.addons import service_notify
    from multiclaude.addons.service_notify import messages, store
    return SimpleNamespace(addon=service_notify, messages=messages, store=store)


def refused(fn, *args):
    from multiclaude.addons.service_notify.messages import Refused
    with pytest.raises(Refused) as e:
        fn(*args)
    return e.value


def kept(r, msg_id, chat=ALICE_CHAT):
    return read_json(r.paths.ASKS / chat / f"{msg_id}.json")


def test_a_message_goes_to_the_owner_as_a_rich_message(r, svc):
    assert svc.messages.send("printer", {"text": "Started"}, 0) == {"message_id": 901}

    method, params = r.api_calls[0]
    assert (method, params["chat_id"], shown(params)) == ("sendRichMessage", ALICE_CHAT, "Started")
    assert (r.paths.STATE / "messages" / ALICE_CHAT).read_text().split()[0] == "901"  # so /clear deletes it


def test_buttons_carry_ask_numbers_and_the_ids_stay_on_the_machine(r, svc):
    kept_when_attached = []
    r.results["editMessageReplyMarkup"] = None
    real_call = svc.messages.telegram.bot().call

    def call(method, files=None, **params):
        if method == "editMessageReplyMarkup":
            kept_when_attached.append(kept(r, params["message_id"]) is not None)
        return real_call(method, files=files, **params)
    svc.messages.telegram.bot().call = call

    svc.messages.send("printer", {"text": "Print?", "buttons": PRINT}, 0)

    (send, sent), (attach, params) = r.api_calls
    assert (send, "reply_markup" in sent, attach) == ("sendRichMessage", False, "editMessageReplyMarkup")
    assert kept_when_attached == [True]  # a button never exists before the message is kept
    assert params["reply_markup"] == {"inline_keyboard": [[{"text": "Print plate 1", "callback_data": "ask:0"},
                                                           {"text": "Cancel", "callback_data": "ask:1"}]]}
    assert "tok" not in json.dumps(r.api_calls)
    question = kept(r, 901)
    assert (question["kind"], question["service"], question["expires"], question["once"]) == (
        "service", "printer", 86400, True)
    assert question["options"] == ["Print plate 1", "Cancel"]
    assert svc.store.live_all() == [(ALICE_CHAT, 901)]


def test_pictures_go_as_media_blocks_below_the_text(r, svc):
    svc.messages.send("printer", {"text": "First layer done", "files": [
        {"name": "snap.jpg", "data": b64(PNG)}, {"name": "log.txt", "data": b64(b"log")}]}, 0)

    params = r.api_calls[0][1]
    assert params["rich_message"]["markdown"] == (
        "First layer done\n\n![](tg://photo?id=m0)\n\n![](tg://document?id=m1)")
    assert params["rich_message"]["media"] == [{"id": "m0", "media": {"type": "photo", "media": "attach://file0"}},
                                               {"id": "m1", "media": {"type": "document", "media": "attach://file1"}}]
    assert r.uploads[0] == {"file0": PNG, "file1": b"log"}


def test_a_repeated_dedupe_key_sends_nothing_and_gives_the_first_id(r, svc):
    first = svc.messages.send("printer", {"text": "Started", "dedupe": "started:job42"}, 0)
    again = svc.messages.send("printer", {"text": "Started", "dedupe": "started:job42"}, 60)

    assert (first, again) == ({"message_id": 901}, {"message_id": 901, "duplicate": True})
    assert r.api_calls[1:] == []


def test_another_person_only_when_the_owner_allowed_it(r, svc):
    assert refused(svc.messages.send, "printer", {"text": "Hi", "to": "bob"}, 0).status == 403
    assert refused(svc.messages.send, "printer", {"text": "Hi", "to": "nobody"}, 0).status == 403
    write_json(r.paths.CONFIG, {**read_json(r.paths.CONFIG),
                                "service_notify": {"services": {"printer": {"to": ["bob"]}}}})

    svc.messages.send("printer", {"text": "Hi", "to": "bob"}, 0)
    svc.messages.send("printer", {"text": "Hi", "to": "alice"}, 0)  # the owner always

    assert [p["chat_id"] for _, p in r.api_calls] == [BOB_CHAT, ALICE_CHAT]


def test_silent_messages_ask_for_no_notification(r, svc):
    svc.messages.send("printer", {"text": "Progress 40%", "silent": True}, 0)
    svc.messages.send("printer", {"text": "Failed"}, 0)

    assert [p.get("disable_notification") for _, p in r.api_calls] == [True, None]


def test_an_edit_changes_only_the_services_own_message(r, svc):
    svc.messages.send("printer", {"text": "Printing 10%"}, 0)

    assert svc.messages.edit("printer", "901", {"text": "Printing 50%"}, 1) == {"message_id": 901}
    method, params = r.api_calls[-1]
    assert (method, params["message_id"], shown(params)) == ("editMessageText", 901, "Printing 50%")
    assert refused(svc.messages.edit, "builder", "901", {"text": "x"}, 1).status == 404
    assert refused(svc.messages.edit, "printer", "777", {"text": "x"}, 1).status == 404


def test_an_edit_with_no_buttons_takes_them_off_and_ends_the_live_entry(r, svc):
    svc.messages.send("printer", {"text": "Print?", "buttons": PRINT}, 0)

    svc.messages.edit("printer", 901, {"text": "Cancelled by the printer", "buttons": []}, 1)

    params = r.api_calls[-1][1]
    assert params["reply_markup"] == {"inline_keyboard": []}
    assert svc.store.live_all() == []


def test_an_edit_that_keeps_the_text_puts_new_buttons_on(r, svc):
    svc.messages.send("printer", {"text": "How did it come out?"}, 0)

    svc.messages.edit("printer", 901, {"buttons": [[{"id": "ok", "text": "OK"}]], "expires_in": 600}, 10)

    params = r.api_calls[-1][1]
    assert shown(params) == "How did it come out?"
    # A set of buttons put on by an edit is the message's next set: its taps can't be an older set's.
    assert params["reply_markup"] == {"inline_keyboard": [[{"text": "OK", "callback_data": "ask:100"}]]}
    assert kept(r, 901)["expires"] == 610
    assert svc.store.live_all() == [(ALICE_CHAT, 901)]


def test_new_buttons_on_an_old_message_get_a_fresh_day(r, svc):
    svc.messages.send("printer", {"text": "Finished: plate 1"}, 0)

    svc.messages.edit("printer", 901, {"buttons": [[{"id": "ok", "text": "OK"}]]}, 100000)

    assert kept(r, 901)["expires"] == 100000 + 86400


def test_an_edit_keeps_the_pictures_by_their_file_ids(r, svc):
    r.results["sendRichMessage"] = {"rich_message": {"blocks": [
        {"type": "paragraph", "text": "x"}, {"type": "photo", "photo": [{"file_id": "S"}, {"file_id": "L"}]}]}}
    svc.messages.send("printer", {"text": "Done", "files": [{"name": "snap.png", "data": b64(PNG)}]}, 0)

    svc.messages.edit("printer", 901, {"text": "Done ✅"}, 1)

    rich = r.api_calls[-1][1]["rich_message"]
    assert rich["markdown"] == "Done ✅\n\n![](tg://photo?id=m0)"
    assert rich["media"] == [{"id": "m0", "media": {"type": "photo", "media": "L"}}]


def test_telegram_out_of_reach_is_503_and_nothing_is_held(r, svc):
    r.unreachable = True

    assert refused(svc.messages.send, "printer", {"text": "Started"}, 0).status == 503
    assert not r.telegram.Outbox(ALICE_CHAT).waiting()


def test_telegram_asking_to_slow_down_is_429_and_a_refusal_502(r, svc):
    r.results["sendRichMessage"] = r.telegram.TelegramError("Too Many Requests", retry_after=7)
    slow = refused(svc.messages.send, "printer", {"text": "x"}, 0)
    r.results["sendRichMessage"] = r.telegram.TelegramError("Bad Request: chat not found")
    bad = refused(svc.messages.send, "printer", {"text": "x"}, 0)

    assert (slow.status, slow.retry_after, bad.status, bad.code) == (429, 7, 502, "telegram_refused")


@pytest.mark.parametrize("request_", [
    {"text": ""},
    {"text": 5},
    {"text": "x" * 32001},
    {"text": "x", "buttons": [[{"id": str(n), "text": "b"} for n in range(9)]]},
    {"text": "x", "buttons": [[{"id": f"{row}-{n}", "text": "b"} for n in range(5)] for row in range(5)]},
    {"text": "x", "buttons": [[{"id": "a", "text": "b"}, {"id": "a", "text": "c"}]]},
    {"text": "x", "buttons": [[{"id": "", "text": "b"}]]},
    {"text": "x", "buttons": [[{"id": "a" * 129, "text": "b"}]]},
    {"text": "x", "buttons": [[{"id": "a", "text": ""}]]},
    {"text": "x", "buttons": [{"id": "a", "text": "b"}]},
    {"text": "x", "files": [{"name": f"{n}.png", "data": "AA=="} for n in range(11)]},
    {"text": "x", "files": [{"name": "a.png", "data": "not base64!"}]},
    {"text": "x", "files": [{"name": "../a.png", "data": "AA=="}]},
    {"text": "x", "expires_in": 0},
    {"text": "x", "expires_in": 604801},
    {"text": "x", "dedupe": "k" * 129},
    {"text": "x", "silent": "yes"},
    ["not", "an", "object"],
])
def test_bad_requests_are_400_and_send_nothing(r, svc, request_):
    assert refused(svc.messages.send, "printer", request_, 0).status == 400
    assert r.api_calls == []


def test_a_delete_removes_the_message_and_its_live_buttons(r, svc):
    svc.messages.send("printer", {"text": "Print?", "buttons": PRINT}, 0)

    assert svc.messages.delete("printer", "901") == {"message_id": 901}
    assert r.api_calls[-1] == ("deleteMessage", {"chat_id": ALICE_CHAT, "message_id": 901})
    assert svc.store.live_all() == []
    assert refused(svc.messages.delete, "printer", "901").status == 404


@pytest.mark.parametrize("failure", [OSError("disk full"), ValueError("not an object")])
def test_keeping_the_question_failing_never_puts_the_buttons_on_and_says_so(r, svc, monkeypatch, failure):
    def broken(*args, **kwargs):
        raise failure
    monkeypatch.setattr(svc.messages.questions, "save", broken)

    assert refused(svc.messages.send, "printer", {"text": "Print?", "buttons": PRINT}, 0).code == "not_kept"
    assert [(m, "reply_markup" in p) for m, p in r.api_calls] == [("sendRichMessage", False)]
    assert svc.store.live_all() == []


def test_a_send_whose_answer_is_lost_leaves_no_buttons(r, svc):
    r.results["sendRichMessage"] = r.telegram.TelegramError("sendRichMessage: timed out")

    assert refused(svc.messages.send, "printer", {"text": "Print?", "buttons": PRINT}, 0).status == 502
    assert [(m, "reply_markup" in p) for m, p in r.api_calls] == [("sendRichMessage", False)]


def test_buttons_that_cant_be_put_on_stay_due_so_the_sweep_takes_them_off_later(r, svc):
    r.results["editMessageReplyMarkup"] = r.telegram.Unreachable("no network")

    error = refused(svc.messages.send, "printer", {"text": "Print?", "buttons": PRINT, "expires_in": 600}, 0)

    assert (error.status, error.code) == (503, "telegram_unreachable") and "901" in error.detail
    assert svc.store.live_all() == [(ALICE_CHAT, 901)]  # Telegram may have put them on all the same
    assert kept(r, 901)["kind"] == "service"


def test_bookkeeping_that_fails_after_the_send_still_keeps_the_message(r, svc):
    (svc.store.Service("printer").dir).mkdir(parents=True)
    (svc.store.Service("printer").dedupe).write_text("[not an object]")

    assert svc.messages.send("printer", {"text": "Print?", "buttons": PRINT, "dedupe": "k"}, 0) == {"message_id": 901}
    assert kept(r, 901)["kind"] == "service" and svc.store.live_all() == [(ALICE_CHAT, 901)]


# --- taps, expiry and presses the service never took -------------------------------------------

@pytest.fixture
def taps(svc):
    from multiclaude.addons.service_notify import taps
    return taps


def tap(r, desk, label, n, now, msg_id=901):
    """Alice taps button n of message msg_id, as the router hands a tap on."""
    msg = r.messages.parse(channel(label, msg_id=str(msg_id), callback_id=f"cb-{n}-{now}", callback_data=f"ask:{n}"))
    r.asks.answer(desk.users["alice"], msg, now)


def presses(svc, name="printer", now=0):
    """What the service takes at now (0: before any press here is two minutes old)."""
    return svc.store.Service(name).take_presses(now)


def test_a_tap_queues_a_press_for_the_service_and_never_the_assistant(r, desk, svc, taps):
    now = time.time()
    svc.messages.send("printer", {"text": "Print?", "buttons": PRINT}, now)

    desk.inbound(channel("Print plate 1", msg_id="901", callback_id="cb1", callback_data="ask:0"))

    assert desk.users["alice"].queue == []
    [press] = presses(svc, now=time.time())
    assert (press["button_id"], press["label"], press["by"], press["message_id"]) == (
        "print:tok", "Print plate 1", "alice", 901)
    method, params = r.api_calls[-1]
    assert (method, shown(params)) == ("editMessageText", "Print?\n\n⏳ Print plate 1…")
    assert params["reply_markup"] == {"inline_keyboard": []}
    assert svc.store.live_all() == []


def test_a_second_tap_on_a_once_message_is_ignored(r, desk, svc, taps):
    svc.messages.send("printer", {"text": "Print?", "buttons": PRINT}, 0)

    tap(r, desk, "Print plate 1", 0, 10)
    tap(r, desk, "Cancel", 1, 11)

    assert [p["button_id"] for p in presses(svc)] == ["print:tok"]


def test_two_taps_at_once_queue_one_press(r, desk, svc, taps):
    svc.messages.send("printer", {"text": "Print?", "buttons": PRINT}, 0)
    threads = [threading.Thread(target=tap, args=(r, desk, "Print plate 1", 0, 10)) for _ in range(2)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(presses(svc)) == 1


def test_a_tap_after_expiry_says_so_and_queues_nothing(r, desk, svc, taps):
    svc.messages.send("printer", {"text": "Print?", "buttons": PRINT, "expires_in": 600}, 0)

    tap(r, desk, "Print plate 1", 0, 601)

    assert presses(svc) == []
    assert shown(r.api_calls[-1][1]) == "Print?\n\n⌛ Expired: nothing was done."
    assert desk.users["alice"].queue == []


def test_buttons_that_stay_queue_every_tap(r, desk, svc, taps):
    svc.messages.send("printer", {"text": "Camera", "buttons": [[{"id": "snap", "text": "📷 Snapshot"}]],
                                "once": False}, 0)

    tap(r, desk, "📷 Snapshot", 0, 10)
    tap(r, desk, "📷 Snapshot", 0, 20)

    assert [p["at"] for p in presses(svc)] == [10, 20]
    assert [m for m, _ in r.api_calls] == ["sendRichMessage", "editMessageReplyMarkup"]


def test_a_tap_on_a_button_number_it_doesnt_have_queues_nothing(r, desk, svc, taps):
    svc.messages.send("printer", {"text": "Print?", "buttons": PRINT}, 0)

    tap(r, desk, "forged", 7, 10)

    assert presses(svc) == []


def test_expired_buttons_come_off_by_themselves(r, desk, svc, taps):
    svc.messages.send("printer", {"text": "Print?", "buttons": PRINT, "expires_in": 600}, 0)

    taps.sweep(599)
    assert [m for m, _ in r.api_calls] == ["sendRichMessage", "editMessageReplyMarkup"]
    taps.sweep(601)

    method, params = r.api_calls[-1]
    assert (method, shown(params)) == ("editMessageText", "Print?\n\n⌛ Expired: nothing was done.")
    assert svc.store.live_all() == []
    tap(r, desk, "Print plate 1", 0, 602)  # a tap that raced the edit
    assert presses(svc) == []


def test_a_press_the_service_never_took_is_dropped_and_the_owner_told(r, desk, svc, taps):
    svc.messages.send("printer", {"text": "Print?", "buttons": PRINT}, 0)
    tap(r, desk, "Print plate 1", 0, 10)

    taps.sweep(129)
    assert len(r.api_calls) == 3
    taps.sweep(131)

    assert presses(svc) == []
    assert shown(r.api_calls[-1][1]) == "Print?\n\n⚠️ printer didn't pick this up in time: nothing was done."


def test_a_tap_is_handled_off_the_router_loop(r, desk, svc, taps, monkeypatch):
    svc.messages.send("printer", {"text": "Print?", "buttons": PRINT}, 0)
    deferred = []
    monkeypatch.setattr(r.telegram, "later", lambda fn, *a, **k: deferred.append((fn, a, k)))

    tap(r, desk, "Print plate 1", 0, 10)
    assert presses(svc) == [] and len(r.api_calls) == 2  # the loop went on before anything was done

    for fn, a, k in deferred:
        fn(*a, **k)
    assert len(presses(svc)) == 1 and shown(r.api_calls[-1][1]) == "Print?\n\n⏳ Print plate 1…"


def test_a_pending_edit_that_fails_leaves_the_buttons_due_to_come_off(r, desk, svc, taps):
    svc.messages.send("printer", {"text": "Print?", "buttons": PRINT, "expires_in": 600}, 0)
    r.results["editMessageText"] = r.telegram.TelegramError("Bad Gateway")

    tap(r, desk, "Print plate 1", 0, 10)
    assert len(presses(svc)) == 1 and svc.store.live_all() == [(ALICE_CHAT, 901)]
    r.results.pop("editMessageText")
    taps.sweep(601)

    assert r.api_calls[-1] == ("editMessageReplyMarkup", {"chat_id": ALICE_CHAT, "message_id": 901,
                                                          "reply_markup": {"inline_keyboard": []}})
    assert svc.store.live_all() == []


def test_expired_buttons_whose_edit_fails_are_tried_again(r, desk, svc, taps):
    svc.messages.send("printer", {"text": "Print?", "buttons": PRINT, "expires_in": 600}, 0)
    r.results["editMessageText"] = r.telegram.TelegramError("Bad Gateway")

    taps.sweep(601)
    assert svc.store.live_all() == [(ALICE_CHAT, 901)]
    r.results.pop("editMessageText")
    taps.sweep(602)

    assert shown(r.api_calls[-1][1]) == "Print?\n\n⌛ Expired: nothing was done."
    assert svc.store.live_all() == []


def test_a_message_deleted_meanwhile_counts_as_its_buttons_gone(r, desk, svc, taps):
    svc.messages.send("printer", {"text": "Print?", "buttons": PRINT, "expires_in": 600}, 0)
    r.results["editMessageText"] = r.telegram.TelegramError("Bad Request: message to edit not found")

    taps.sweep(601)

    assert svc.store.live_all() == []


def test_a_press_older_than_two_minutes_is_never_handed_over(svc):
    service = svc.store.Service("printer")
    service.add_press({"button_id": "a", "chat": ALICE_CHAT, "message_id": 901}, 0)

    assert service.take_presses(121) == []
    assert [p["button_id"] for p in service.drop_stale(121)] == ["a"]


def test_expiry_of_buttons_that_stay_drops_their_waiting_presses(r, desk, svc, taps):
    svc.messages.send("printer", {"text": "Camera", "buttons": [[{"id": "snap", "text": "📷 Snapshot"}]],
                                "once": False, "expires_in": 600}, 0)
    tap(r, desk, "📷 Snapshot", 0, 590)

    taps.sweep(601)

    assert presses(svc, now=601) == []
    assert shown(r.api_calls[-1][1]) == "Camera\n\n⌛ Expired."


def test_a_press_handed_over_is_never_called_missed(r, desk, svc, taps):
    svc.messages.send("printer", {"text": "Print?", "buttons": PRINT}, 0)
    tap(r, desk, "Print plate 1", 0, 10)
    presses(svc)

    taps.sweep(500)

    assert len(r.api_calls) == 3


def test_a_waiting_long_poll_wakes_on_a_press(r, desk, svc, taps):
    svc.messages.send("printer", {"text": "Print?", "buttons": PRINT}, 0)
    woke, ready = [], threading.Event()

    def wait():
        wake = taps.wake("printer")
        with wake:
            ready.set()  # the tap's notify needs the lock, which wait_for gives up only once it waits
            started = time.monotonic()
            wake.wait_for(svc.store.Service("printer").has_presses, timeout=5)
            woke.append(time.monotonic() - started)

    waiter = threading.Thread(target=wait)
    waiter.start()
    ready.wait()
    tap(r, desk, "Print plate 1", 0, 10)
    waiter.join()

    assert woke and woke[0] < 2


def test_the_router_loop_sweeps_every_few_seconds_in_the_background(svc, taps, monkeypatch):
    swept = []
    monkeypatch.setattr(taps, "sweep", swept.append)
    monkeypatch.setattr(svc.addon.api, "start_once", lambda desk, now: None)

    for now in (1000, 1001, 1006):
        svc.addon.on_tick(None, now)
        with svc.addon._sweeping:  # the sweep's thread is done once its lock is free
            pass

    assert swept == [1000, 1006]


def test_a_service_button_tapped_while_the_add_in_is_off_never_reaches_anyone(r, desk, svc, addons_on):
    svc.messages.send("printer", {"text": "Print?", "buttons": PRINT}, 0)
    addons_on()

    tap(r, desk, "Print plate 1", 0, 10)

    assert presses(svc) == [] and desk.users["alice"].queue == []
    assert r.api_calls[-1] == ("editMessageReplyMarkup", {"chat_id": ALICE_CHAT, "message_id": "901"})


# --- the HTTP API ------------------------------------------------------------------------------

class Online:
    def __init__(self, online=True):
        self.up = online

    def online(self):
        return self.up


@pytest.fixture
def http(svc, taps):
    """The add-in's listener on a free port, a token for printer, and call(method, path, body=None,
    token=…) -> (status, JSON answer)."""
    from multiclaude.addons.service_notify import api
    listener = api.Listener(0, network=Online())
    assert listener.start()
    token = svc.store.new_key("printer")

    def call(method, path, body=None, token=token, headers=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{listener.port}{path}", data=data, method=method,
                                         headers={"Authorization": f"Bearer {token}", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    yield SimpleNamespace(api=api, listener=listener, token=token, call=call)
    listener.close()


def test_a_request_without_a_valid_token_is_401_and_the_token_is_not_logged(r, http, capsys):
    assert http.call("GET", "/v1/health", token="wrong-token-xyz") == (401, {"error": "unauthorized"})
    assert http.call("GET", "/v1/health", token="")[0] == 401
    assert r.api_calls == []
    assert "wrong-token-xyz" not in capsys.readouterr().err


def test_send_edit_delete_over_http(r, http):
    assert http.call("POST", "/v1/messages", {"text": "Started"}) == (200, {"message_id": 901})
    assert http.call("PATCH", "/v1/messages/901", {"text": "Done"}) == (200, {"message_id": 901})
    assert http.call("DELETE", "/v1/messages/901") == (200, {"message_id": 901})
    assert http.call("PATCH", "/v1/messages/901", {"text": "x"}) == (404, {"error": "not_found"})
    assert [m for m, _ in r.api_calls] == ["sendRichMessage", "editMessageText", "deleteMessage"]


def test_a_bad_request_says_why(http):
    status, answer = http.call("POST", "/v1/messages", {"text": ""})

    assert (status, answer["error"]) == (400, "bad_request") and answer["detail"]


def test_unknown_routes_are_404(http):
    assert http.call("GET", "/v1/nothing") == (404, {"error": "not_found"})
    assert http.call("POST", "/v1/messages/901", {"text": "x"})[0] == 404


def test_presses_long_poll_returns_as_soon_as_a_tap_comes(r, desk, svc, http):
    http.call("POST", "/v1/messages", {"text": "Print?", "buttons": PRINT})
    threading.Timer(0.3, tap, args=(r, desk, "Print plate 1", 0, time.time())).start()

    started = time.monotonic()
    status, answer = http.call("GET", "/v1/presses?wait=5")

    assert status == 200 and time.monotonic() - started < 3
    assert [(p["button_id"], p["message_id"], p["by"]) for p in answer["presses"]] == [("print:tok", 901, "alice")]
    assert http.call("GET", "/v1/presses?wait=0") == (200, {"presses": []})


def raw_request(port, text):
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(text.encode())
    return sock


def test_a_long_poll_whose_service_went_away_leaves_the_press_for_the_next(r, desk, svc, http):
    http.call("POST", "/v1/messages", {"text": "Print?", "buttons": PRINT})
    gone = raw_request(http.listener.port, f"GET /v1/presses?wait=5 HTTP/1.1\r\nHost: x\r\n"
                                           f"Authorization: Bearer {http.token}\r\n\r\n")
    deadline = time.monotonic() + 3
    while http.api.polls("printer") < 1 and time.monotonic() < deadline:
        pass
    gone.close()  # the service restarted while its long-poll waited

    tap(r, desk, "Print plate 1", 0, time.time())
    deadline = time.monotonic() + 3
    while http.api.polls("printer") and time.monotonic() < deadline:
        pass

    status, answer = http.call("GET", "/v1/presses?wait=0")
    assert [p["button_id"] for p in answer["presses"]] == ["print:tok"]


def test_a_negative_content_length_is_400(http):
    sock = raw_request(http.listener.port, f"POST /v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n"
                                           f"Authorization: Bearer {http.token}\r\n\r\n")
    assert sock.recv(100).startswith(b"HTTP/1.1 400")
    sock.close()


def test_an_idle_connection_is_dropped(http, monkeypatch):
    monkeypatch.setattr(http.api, "IDLE_TIMEOUT", 0.3)
    sock = raw_request(http.listener.port, "")

    assert sock.recv(100) == b""  # closed by the server, not left holding a thread
    sock.close()


def test_connections_past_the_cap_are_refused(http, monkeypatch):
    monkeypatch.setattr(http.listener, "slots", threading.BoundedSemaphore(1))
    idle = raw_request(http.listener.port, "")
    deadline = time.monotonic() + 3
    while http.listener.slots._value and time.monotonic() < deadline:
        pass

    with pytest.raises((urllib.error.URLError, ConnectionError, http_client_errors())):
        http.call("GET", "/v1/health")
    idle.close()


def http_client_errors():
    import http.client
    return http.client.RemoteDisconnected


def test_the_fifth_long_poll_is_429(http):
    waiting = [threading.Thread(target=http.call, args=("GET", "/v1/presses?wait=2")) for _ in range(4)]
    for thread in waiting:
        thread.start()
    deadline = time.monotonic() + 3
    while http.api.polls("printer") < 4 and time.monotonic() < deadline:
        pass

    status, answer = http.call("GET", "/v1/presses?wait=2")

    assert (status, answer["error"]) == (429, "rate_limited")
    for thread in waiting:
        thread.join()


def test_sends_over_twenty_a_minute_are_429_with_retry_after(r, http):
    for n in range(20):
        assert http.call("POST", "/v1/messages", {"text": f"event {n}"})[0] == 200

    status, answer = http.call("POST", "/v1/messages", {"text": "one more"})

    assert (status, answer["error"]) == (429, "rate_limited") and 1 <= answer["retry_after"] <= 60
    assert len(r.api_calls) == 20


def test_a_body_over_20_mb_is_413_without_reading_it(http):
    status, answer = http.call("POST", "/v1/messages", {"text": "x"}, headers={"Content-Length": str(21 * 2**20)})

    assert (status, answer) == (413, {"error": "too_large"})


def test_health_says_whether_telegram_is_reachable(http):
    assert http.call("GET", "/v1/health") == (200, {"ok": True, "telegram": "reachable"})
    http.listener.network.up = False
    assert http.call("GET", "/v1/health") == (200, {"ok": True, "telegram": "unreachable"})


def test_the_listener_binds_only_the_loopback(svc):
    from multiclaude.addons.service_notify import api
    with pytest.raises(ValueError):
        api.Listener(0, host="0.0.0.0")


def test_a_taken_port_is_logged_and_start_says_false(http, capsys):
    from multiclaude.addons.service_notify import api
    second = api.Listener(http.listener.port)

    assert second.start() is False
    assert f"port {http.listener.port}" in capsys.readouterr().err


def test_the_log_has_no_text_no_ids_and_no_token(http, capsys):
    http.call("POST", "/v1/messages", {"text": "SECRET-TEXT", "buttons": [[{"id": "tok-123", "text": "Go"}]]})

    err = capsys.readouterr().err
    assert "service-notify: printer POST /v1/messages 200" in err and "text=11 files=0" in err
    assert "SECRET-TEXT" not in err and "tok-123" not in err and http.token not in err


def test_the_router_loop_starts_the_listener_once_on_the_configured_port(r, svc, monkeypatch):
    from multiclaude.addons.service_notify import api
    started = []
    monkeypatch.setattr(api.Listener, "start", lambda self: started.append(self.port) or True)
    write_json(r.paths.CONFIG, {**read_json(r.paths.CONFIG), "service_notify": {"port": 8899}})
    desk = SimpleNamespace(network=Online())

    svc.addon.on_tick(desk, 1000)
    svc.addon.on_tick(desk, 1001)

    assert started == [8899]


# --- assistant service, the teardown -----------------------------------------------------------

FOLDER = REPO / "addons" / "service-notify"


def run_script(name, *args, env):
    done = subprocess.run([sys.executable, "-I", str(FOLDER / name), *args], env=env, capture_output=True, text=True,
                          timeout=30)
    return done.returncode, done.stdout, done.stderr


def test_assistant_service_key_prints_a_token_once_and_list_names_the_service(tmp_path):
    env = {**os.environ, "ASSISTANT_ROOT": str(tmp_path)}

    status, token, _ = run_script("assistant-service", "key", "printer", env=env)
    assert status == 0 and len(token.strip()) >= 40
    assert token.strip() not in (tmp_path / "state" / "service-keys.json").read_text()
    assert run_script("assistant-service", "list", env=env)[:2] == (0, "printer\n")
    assert run_script("assistant-service", "revoke", "printer", env=env)[0] == 0
    assert run_script("assistant-service", "list", env=env)[1] == ""
    assert run_script("assistant-service", "key", "Bad Name", env=env)[0] == 1
    assert run_script("assistant-service", "nonsense", env=env)[0] == 2


def test_teardown_takes_the_buttons_off_every_live_message(tmp_path, bot_api):
    root, channel_dir = tmp_path / "assistant", tmp_path / "channel"
    channel_dir.mkdir()
    (channel_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=123:abc\n")
    write_json(root / "state" / "service-notify" / "live.json", {f"{ALICE_CHAT}:901": 9e9, f"{BOB_CHAT}:902": 9e9})
    for chat, msg_id in ((ALICE_CHAT, 901), (BOB_CHAT, 902)):
        write_json(root / "state" / "asks" / chat / f"{msg_id}.json",
                   {"question": "Print?", "options": ["Print"], "kind": "service", "service": "printer",
                    "buttons": [[{"id": "p", "text": "Print"}]], "once": True, "expires": 9e9, "media": []})
    env = {**os.environ, "ASSISTANT_ROOT": str(root), "TELEGRAM_STATE_DIR": str(channel_dir),
           "TG_API_BASE": bot_api.url}

    status, _, err = run_script("teardown", env=env)

    assert status == 0, err
    edits = [fields for method, fields, _ in bot_api.calls if method == "editMessageText"]
    assert sorted(f["message_id"] for f in edits) == ["901", "902"]
    assert all(json.loads(f["reply_markup"]) == {"inline_keyboard": []} for f in edits)
    assert json.loads(edits[0]["rich_message"])["markdown"] == "Print?\n\n⌛ Expired: nothing was done."
    assert read_json(root / "state" / "service-notify" / "live.json") == {}


def test_teardown_with_telegram_out_of_reach_still_ends_well(tmp_path):
    root = tmp_path / "assistant"
    write_json(root / "state" / "service-notify" / "live.json", {f"{ALICE_CHAT}:901": 9e9})
    env = {**os.environ, "ASSISTANT_ROOT": str(root), "TELEGRAM_STATE_DIR": str(tmp_path),
           "TG_API_BASE": "http://127.0.0.1:9"}

    assert run_script("teardown", env=env)[0] == 0


# --- the Python client -------------------------------------------------------------------------

@pytest.fixture
def client(http, monkeypatch):
    """The client module, as a service would copy it in."""
    monkeypatch.syspath_prepend(str(FOLDER / "client"))
    sys.modules.pop("multiclaude_service", None)
    import multiclaude_service
    return multiclaude_service


def real_sleep(seconds):
    threading.Event().wait(seconds)  # the router's fixture makes time.sleep do nothing


def test_the_client_sends_edits_and_gets_a_press(r, desk, http, client, tmp_path):
    picture = tmp_path / "preview.png"
    picture.write_bytes(PNG)
    svc = client.Service(f"http://127.0.0.1:{http.listener.port}", http.token)

    msg_id = svc.send("Plate 1 ready", files=[picture], buttons=[[("print:tok", "Print plate 1"), ("cancel:tok", "Cancel")]],
                      expires_in=600, dedupe="confirm:42")
    assert msg_id == 901 and svc.send("Plate 1 ready", dedupe="confirm:42") == 901
    assert r.uploads[0] == {"file0": PNG}
    threading.Timer(0.2, tap, args=(r, desk, "Print plate 1", 0, time.time())).start()
    press = next(svc.presses(wait=5))
    assert (press["button_id"], press["message_id"]) == ("print:tok", 901)
    assert svc.edit(msg_id, "✅ Printing plate 1") == 901
    assert svc.health() == {"ok": True, "telegram": "reachable"}


def test_the_client_raises_what_the_api_refused(http, client):
    svc = client.Service(f"http://127.0.0.1:{http.listener.port}", "wrong")

    with pytest.raises(client.ServiceError) as e:
        svc.send("x")

    assert (e.value.status, e.value.code) == (401, "unauthorized")


def test_the_client_retries_while_the_listener_is_down(svc, client):
    from multiclaude.addons.service_notify import api
    token = svc.store.new_key("builder")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    listener = api.Listener(port)
    threading.Timer(0.5, listener.start).start()

    try:
        health = client.Service(f"http://127.0.0.1:{port}", token, retries=6, sleep=real_sleep).health()
    finally:
        listener.close()

    assert health["ok"] is True


def test_the_client_needs_nothing_outside_the_standard_library():
    done = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                           "import multiclaude_service; print(multiclaude_service.Service)", str(FOLDER / "client")],
                          capture_output=True, text=True, env={"PATH": os.environ.get("PATH", "")})

    assert done.returncode == 0, done.stderr


# --- from the security review before upstream (2026-11-28) -------------------------------------

def test_a_tap_on_buttons_replaced_meanwhile_is_ignored(r, desk, svc, taps):
    svc.messages.send("printer", {"text": "Print?", "buttons": [[{"id": "cancel:a", "text": "Cancel"},
                                                               {"id": "print:a", "text": "Print"}]]}, 0)
    svc.messages.edit("printer", 901, {"buttons": [[{"id": "print:b", "text": "Print"},
                                                  {"id": "cancel:b", "text": "Cancel"}]]}, 1)
    edited = r.api_calls[-1][1]["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in edited] == ["ask:100", "ask:101"]

    tap(r, desk, "Cancel", 0, 2)  # the owner's tap on the old Cancel arrives after the edit
    assert presses(svc) == []

    tap(r, desk, "Print", 100, 3)
    [press] = presses(svc)
    assert press["button_id"] == "print:b"


def test_the_client_never_goes_through_a_proxy(http, client, monkeypatch):
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing listens there
    import urllib.request
    monkeypatch.setattr(urllib.request, "_opener", None)  # as in a fresh process started with these

    svc = client.Service(f"http://127.0.0.1:{http.listener.port}", http.token, retries=0)

    assert svc.health()["ok"] is True


def test_teardown_drops_waiting_presses_and_leaves_answered_messages_text(tmp_path, bot_api):
    root, channel_dir = tmp_path / "assistant", tmp_path / "channel"
    channel_dir.mkdir()
    (channel_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=123:abc\n")
    write_json(root / "state" / "service-notify" / "live.json", {f"{ALICE_CHAT}:901": 9e9, f"{ALICE_CHAT}:902": 9e9})
    base = {"question": "Print?", "options": ["Print"], "kind": "service", "service": "printer",
            "buttons": [[{"id": "p", "text": "Print"}]], "once": True, "expires": 9e9, "media": []}
    write_json(root / "state" / "asks" / ALICE_CHAT / "901.json", base)
    write_json(root / "state" / "asks" / ALICE_CHAT / "902.json", {**base, "answer": 0})  # pressed, edit failed
    presses_file = root / "state" / "service-notify" / "printer" / "presses.jsonl"
    presses_file.parent.mkdir(parents=True)
    presses_file.write_text(json.dumps({"seq": 1, "chat": ALICE_CHAT, "message_id": 901, "button_id": "p",
                                        "label": "Print", "by": "alice", "at": 9e9}) + "\n")
    env = {**os.environ, "ASSISTANT_ROOT": str(root), "TELEGRAM_STATE_DIR": str(channel_dir),
           "TG_API_BASE": bot_api.url}

    status, _, err = run_script("teardown", env=env)

    assert status == 0, err
    assert presses_file.read_text().strip() == ""
    assert read_json(root / "state" / "asks" / ALICE_CHAT / "901.json")["answer"] == "expired"
    texts = {f["message_id"]: (method, f) for method, f, _ in bot_api.calls}
    assert texts["901"][0] == "editMessageText"
    assert texts["902"][0] == "editMessageReplyMarkup"  # its text is the service's, not "nothing was done"
    assert read_json(root / "state" / "asks" / ALICE_CHAT / "902.json")["answer"] == 0


def test_the_add_in_state_is_private(svc):
    svc.store.new_key("printer")
    svc.store.Service("printer").add_press({"chat": ALICE_CHAT, "message_id": 1, "button_id": "p"}, 0)
    folder = svc.store.ROOT
    assert folder.stat().st_mode & 0o077 == 0
