// The chat's part of the relay (src/chat.js), mounted while env.ADDONS names chat. IDs in brackets are
// the acceptance criteria of docs/chat-miniapp/ACCEPTANCE.md.
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";

import { BACKFILL, EVENTS_KEPT, EVENTS_KEPT_FOR, EVENT_MAX, SENDS_PER_MINUTE, chatEnvelope, chatMessage, fitted } from "../src/chat.js";
import relay from "../src/index.js";
import { fakeSocket, namespaceOf, newRelay } from "./fakes.js";

const BOT_TOKEN = "123:bot-token";
const WEBAPP_KEY = createHmac("sha256", "WebAppData").update(BOT_TOKEN).digest("hex");
const NOW = 1_791_530_000_000;
const ALICE = 100;
const BOB = 101;
const STRANGER = 666;
const CID = "0b9f3c2e-5d4a-4c1b-9e8f-7a6b5c4d3e2f";

function signed({ id = ALICE, token = BOT_TOKEN, authDate = NOW / 1000 } = {}) {
  const fields = { auth_date: String(authDate), query_id: "AAH", signature: "sig", user: JSON.stringify({ id, first_name: "A" }) };
  const checked = Object.keys(fields).sort().map((key) => `${key}=${fields[key]}`).join("\n");
  const secret = createHmac("sha256", "WebAppData").update(token).digest();
  return new URLSearchParams({ ...fields, hash: createHmac("sha256", secret).update(checked).digest("hex") }).toString();
}

function fromPage(path, initData, body = {}) {
  return new Request(`https://relay.test/app/chat/${path}`, { method: "POST", body: JSON.stringify(body),
    headers: { authorization: `tma ${initData}`, "content-type": "application/json" } });
}

function fromVm(path, body, token = "vm-token") {
  return new Request(`https://relay.test/chat/${path}`, { method: "PUT", body: JSON.stringify(body),
    headers: { authorization: `Bearer ${token}` } });
}

/* A relay with the chat add-in on, the VM connected, and Alice and Bob on the bot. */
async function chatRelay(t, { members = [ALICE, BOB], addons = "chat" } = {}) {
  t.mock.method(Date, "now", () => NOW);
  const vm = fakeSocket();
  const queue = newRelay([vm]);
  const env = { ADDONS: addons, TELEGRAM_WEBAPP_KEY: WEBAPP_KEY, RELAY_TOKEN: "vm-token", RELAY: namespaceOf(queue) };
  if (members) assert.equal((await relay.fetch(fromVm("members", { ids: members.map(String) }), env)).status, 204);
  return { vm, queue, env };
}

function send(env, text = "hello", { initData = signed(), clientId = CID, extra = {} } = {}) {
  return relay.fetch(fromPage("send", initData, { client_id: clientId, text, ...extra }), env);
}

function event(seq, kind = "assistant_message", extra = {}) {
  return { seq, kind, key: `a:${seq}`, at: "2026-11-14T10:00:00.000Z", ts: "2026-11-14T10:00:00.000Z",
    markdown: `answer ${seq}`, ...extra };
}

function sentChats(vm) {
  return vm.sent.filter((envelope) => envelope.type === "chat");
}

test("a message is queued for the VM with who sent it, when, and the init data to check again", async (t) => {
  const { vm, env } = await chatRelay(t);
  const initData = signed();
  const reply = await send(env, "hello\nthere", { initData, extra: { model: "haiku", effort: "max" } });

  assert.equal(reply.status, 202);
  assert.deepEqual(await reply.json(), { queued_at: NOW / 1000 });
  assert.deepEqual(sentChats(vm), [{ envelope_id: `chat:${ALICE}:${CID}`, type: "chat",
    payload: { init_data: initData, client_id: CID, text: "hello\nthere", queued_at: NOW / 1000 } }]);  // [AC-3.9]
});

test("a retry is the same envelope, and the same id from two people two envelopes", async (t) => {
  // [AC-2.4] The relay keeps one while it waits for the VM; the router drops one it already took.
  const { queue, env } = await chatRelay(t);
  await send(env);
  await send(env);
  await send(env, "hello", { initData: signed({ id: BOB }) });
  const vm = fakeSocket();
  queue.greet(vm);
  assert.deepEqual(sentChats(vm).map((e) => e.envelope_id), [`chat:${ALICE}:${CID}`, `chat:${BOB}:${CID}`]);
});

test("what isn't a message is turned away", async (t) => {
  const { vm, env } = await chatRelay(t);
  assert.equal((await relay.fetch(fromPage("send", signed(), { client_id: "not-a-uuid", text: "x" }), env)).status, 400);
  assert.equal((await send(env, "   ")).status, 400);
  assert.equal((await send(env, "x".repeat(16001))).status, 400);
  assert.equal((await relay.fetch(new Request("https://relay.test/app/chat/send", { method: "POST", body: "x".repeat(70000),
    headers: { authorization: `tma ${signed()}` } }), env)).status, 413);
  assert.deepEqual(sentChats(vm), []);
  assert.equal(chatMessage(JSON.stringify({ client_id: CID, text: "x".repeat(16000) })).text.length, 16000);
});

test("a stranger gets 403 everywhere, and nothing is queued or kept for them", async (t) => {
  // [AC-11.1]
  const { vm, queue, env } = await chatRelay(t);
  const initData = signed({ id: STRANGER });
  for (const [path, body] of [["send", { client_id: CID, text: "hi" }], ["feed", { after: 0 }], ["history", { cursor: null }]]) {
    const reply = await relay.fetch(fromPage(path, initData, body), env);
    assert.equal(reply.status, 403, path);
    assert.deepEqual(await reply.json(), { error: "stranger" });
  }
  assert.deepEqual(vm.sent, []);
  const kept = (table) => queue.sql.exec(`SELECT * FROM ${table}`).toArray();
  assert.deepEqual([kept("envelopes"), kept("chat_events"), kept("chat_rate"), kept("chat_open")], [[], [], [], []]);
});

test("before the router has said who's on the bot, nobody gets in", async (t) => {
  const { env } = await chatRelay(t, { members: null });
  assert.equal((await send(env)).status, 403);
});

test("someone newly paired gets in once the router puts the list, and someone removed is out", async (t) => {
  // [AC-11.5]
  const { env } = await chatRelay(t, { members: [ALICE] });
  assert.equal((await send(env, "hi", { initData: signed({ id: BOB }) })).status, 403);
  await relay.fetch(fromVm("members", { ids: [String(ALICE), String(BOB)] }), env);
  assert.equal((await send(env, "hi", { initData: signed({ id: BOB }) })).status, 202);
  await relay.fetch(fromVm("members", { ids: [String(BOB)] }), env);
  assert.equal((await send(env, "hi", { clientId: "1c0a4d3f-6e5b-4d2c-8f9a-8b7c6d5e4f30" })).status, 403);
});

test("init data with a wrong hash or over a day old gets 401", async (t) => {
  // [AC-11.2]
  const { vm, env } = await chatRelay(t);
  for (const initData of [signed({ token: "999:another-bot" }), signed({ authDate: NOW / 1000 - 25 * 3600 }),
    signed().replace("query_id=AAH", "query_id=AAX")]) {
    assert.equal((await send(env, "hi", { initData })).status, 401);
    assert.equal((await relay.fetch(fromPage("feed", initData, { after: 0 }), env)).status, 401);
  }
  assert.deepEqual(vm.sent, []);
});

test("init data two hours old reads the feed but can't send", async (t) => {
  // [AC-11.4]
  const { vm, env } = await chatRelay(t);
  const old = signed({ authDate: NOW / 1000 - 2 * 3600 });
  await relay.fetch(fromVm(String(ALICE), { events: [event(1)] }), env);

  const read = await relay.fetch(fromPage("feed", old, { after: 0 }), env);
  assert.equal(read.status, 200);
  const sent = await send(env, "hi", { initData: old });
  assert.equal(sent.status, 401);
  assert.deepEqual(await sent.json(), { error: "reopen" });
  assert.deepEqual(sentChats(vm), []);
});

test("a body naming another person changes nothing: it is always the signer's own feed", async (t) => {
  // [AC-11.3]
  const { vm, env } = await chatRelay(t);
  await relay.fetch(fromVm(String(BOB), { events: [event(1, "assistant_message", { markdown: "Bob's secret" })] }), env);
  await relay.fetch(fromVm(String(ALICE), { events: [event(1)] }), env);

  const read = await relay.fetch(fromPage("feed", signed(), { after: 0, user: BOB, chat: BOB }), env);
  assert.deepEqual((await read.json()).events.map((e) => e.markdown), ["answer 1"]);
  await relay.fetch(fromPage("send", signed(), { client_id: CID, text: "hi", user: BOB }), env);
  assert.deepEqual(sentChats(vm).map((e) => e.envelope_id), [`chat:${ALICE}:${CID}`]);
});

test("the 21st message in a minute is told to slow down and isn't queued", async (t) => {
  // [AC-12.3]
  const { vm, env } = await chatRelay(t);
  for (let n = 0; n < SENDS_PER_MINUTE; n++) {
    const id = `0b9f3c2e-5d4a-4c1b-9e8f-${String(n).padStart(12, "0")}`;
    assert.equal((await send(env, `m${n}`, { clientId: id })).status, 202);
  }
  const over = await send(env, "one too many", { clientId: "0b9f3c2e-5d4a-4c1b-9e8f-999999999999" });
  assert.equal(over.status, 429);
  assert.equal(sentChats(vm).length, SENDS_PER_MINUTE);
});

test("a page's first read asks the router for the conversation, and later reads keep it coming once a minute", async (t) => {
  const { vm, queue, env } = await chatRelay(t);
  await relay.fetch(fromVm(String(ALICE), { events: [event(1)] }), env);
  await relay.fetch(fromPage("feed", signed(), { after: 0 }), env);
  await relay.fetch(fromPage("feed", signed(), { after: 0 }), env);  // a re-read after a gap: deduped while waiting
  assert.deepEqual(vm.sent.filter((e) => e.type === "chat_open").map((e) => e.payload),
    [{ user: String(ALICE), backfill: true }]);

  const chat = queue.chat;
  chat.tellRouter(String(ALICE), false, NOW + 30_000);
  chat.tellRouter(String(ALICE), false, NOW + 61_000);
  assert.deepEqual(vm.sent.filter((e) => e.type === "chat_open").map((e) => e.payload.backfill), [true, false]);
});

test("a read waits for the events the router puts, and gets only the reader's", async (t) => {
  const { queue, env } = await chatRelay(t);
  await relay.fetch(fromVm(String(ALICE), { events: [event(1)] }), env);
  const reading = queue.chat.read(String(ALICE), 1, NOW, 60_000);
  queue.chat.keepEvents(String(BOB), { events: [event(2)] }, NOW);
  queue.chat.keepEvents(String(ALICE), { events: [event(2), event(3)] }, NOW);
  const read = await (await reading).json();
  assert.deepEqual(read.events.map((e) => e.seq), [2, 3]);
  assert.equal(read.online, true);
  assert.equal(queue.chat.waiting.size, 0);
});

test("a read says when the VM isn't connected", async (t) => {
  t.mock.method(Date, "now", () => NOW);
  const queue = newRelay([]);
  const env = { ADDONS: "chat", TELEGRAM_WEBAPP_KEY: WEBAPP_KEY, RELAY_TOKEN: "vm-token", RELAY: namespaceOf(queue) };
  await relay.fetch(fromVm("members", { ids: [String(ALICE)] }), env);
  await relay.fetch(fromVm(String(ALICE), { events: [event(1)] }), env);
  const read = await (await relay.fetch(fromPage("feed", signed(), { after: 0 }), env)).json();
  assert.equal(read.online, false);
});

test("the relay keeps a person's last 200 events for a day, the last 50 for a first read", async (t) => {
  const { queue, env } = await chatRelay(t);
  const events = Array.from({ length: EVENTS_KEPT + 30 }, (_, n) => event(n + 1));
  await relay.fetch(fromVm(String(ALICE), { events }), env);
  const seqs = queue.sql.exec("SELECT seq FROM chat_events WHERE user = ? ORDER BY seq", String(ALICE)).toArray().map((r) => r.seq);
  assert.deepEqual([seqs.length, seqs[0]], [EVENTS_KEPT, 31]);

  const first = await (await relay.fetch(fromPage("feed", signed(), { after: 0 }), env)).json();
  assert.deepEqual([first.events.length, first.events[0].seq], [BACKFILL, EVENTS_KEPT + 30 - BACKFILL + 1]);

  queue.chat.forgetOld(NOW + EVENTS_KEPT_FOR + 1);
  assert.equal(queue.sql.exec("SELECT seq FROM chat_events").toArray().length, 0);
});

test("a cleared event drops the old conversation, and a backfill replaces what was kept", async (t) => {
  // [AC-6.2] Nothing of the old conversation stays on the relay.
  const { queue, env } = await chatRelay(t);
  await relay.fetch(fromVm(String(ALICE), { events: [event(1), event(2)] }), env);
  await relay.fetch(fromVm(String(ALICE), { events: [{ seq: 3, kind: "cleared", at: "x" }, event(4)] }), env);
  const kept = () => queue.sql.exec("SELECT seq FROM chat_events ORDER BY seq").toArray().map((r) => r.seq);
  assert.deepEqual(kept(), [3, 4]);

  await relay.fetch(fromVm(String(ALICE), { events: [event(5), event(6)], replace: true }), env);
  assert.deepEqual(kept(), [5, 6]);
});

test("an event too big is cut with a note, or left out", () => {
  const big = fitted(event(1, "assistant_message", { markdown: "x".repeat(40_000) }));
  assert.ok(new TextEncoder().encode(big).length <= EVENT_MAX);
  assert.ok(JSON.parse(big).markdown.endsWith("… (full text in Telegram)"));
});

test("an event too big with no text to cut is left out, said in the log, and the rest of its batch kept", async (t) => {
  const { queue, env } = await chatRelay(t);
  const warned = t.mock.method(console, "warn", () => {});
  const huge = { seq: 2, kind: "step", key: "s:1", input_preview: "y".repeat(40_000) };
  assert.equal((await relay.fetch(fromVm(String(ALICE), { events: [event(1), huge, event(3)] }), env)).status, 204);

  assert.deepEqual(queue.sql.exec("SELECT seq FROM chat_events ORDER BY seq").toArray().map((r) => r.seq), [1, 3]);
  assert.deepEqual(warned.mock.calls.map((call) => call.arguments[0]),
    [`chat: a step event (seq 2) over ${EVENT_MAX} bytes was left out`]);
  assert.doesNotMatch(warned.mock.calls[0].arguments[0], /yyy/);
});

test("only the VM puts events and the members list", async (t) => {
  const { env } = await chatRelay(t);
  assert.equal((await relay.fetch(fromVm(String(ALICE), { events: [event(1)] }, "guess"), env)).status, 401);
  assert.equal((await relay.fetch(fromVm("members", { ids: [String(STRANGER)] }, "guess"), env)).status, 401);
  assert.equal((await send(env, "hi", { initData: signed({ id: STRANGER }) })).status, 403);
});

test("older history is asked of the VM and its answer goes to the page", async (t) => {
  const { vm, env } = await chatRelay(t);
  const asked = relay.fetch(fromPage("history", signed(), { cursor: { file: "s.jsonl", end: 100, before: "x" } }), env);
  let envelope;
  for (let tries = 0; tries < 100 && !envelope; tries++) {
    envelope = vm.sent.find((e) => e.type === "chat_history");
    if (!envelope) await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.deepEqual(envelope.payload.cursor, { file: "s.jsonl", end: 100, before: "x" });
  assert.equal(envelope.payload.user, String(ALICE));
  const put = await relay.fetch(fromVm(`history/${envelope.payload.id}`, { events: [event(1)], cursor: null }), env);
  assert.equal(put.status, 204);
  assert.deepEqual((await (await asked).json()).events.map((e) => e.seq), [1]);
});

test("the envelope of a message is per person and message", () => {
  assert.equal(chatEnvelope("100", "init", { client_id: CID, text: "x" }, NOW).envelope_id, `chat:100:${CID}`);
});

test("while the chat add-in is off, its routes aren't there", async (t) => {
  const { env } = await chatRelay(t, { members: null, addons: "slack" });
  assert.equal((await send(env)).status, 404);
  assert.equal((await relay.fetch(fromVm("members", { ids: [] }), env)).status, 404);
});

test("a message to another add-in's model carries its target; anything that isn't one is turned away", async (t) => {
  // [AC-X3] The same checks as any message: signature, members, rate, then the router's.
  const { vm, env } = await chatRelay(t);
  assert.equal((await send(env, "hi", { extra: { target: "ext:m1" } })).status, 202);
  assert.equal(sentChats(vm)[0].payload.target, "ext:m1");
  assert.equal((await send(env, "hi", { clientId: "1c0a4d3f-6e5b-4d2c-8f9a-8b7c6d5e4f30", extra: { target: "../x" } })).status, 400);
  assert.equal((await send(env, "hi", { initData: signed({ id: STRANGER }), extra: { target: "ext:m1" } })).status, 403);
  assert.equal(sentChats(vm).length, 1);
});

test("an add-in's state for its tab is kept once per add-in, the newest, up to 64 KB", async (t) => {
  // [AC-X4]
  const { queue, env } = await chatRelay(t);
  const state = (seq, runs) => ({ seq, kind: "addon_state", addon: "ext", at: "x", state: { runs } });
  await relay.fetch(fromVm(String(ALICE), { events: [event(1), state(2, ["a"]), event(3), state(4, ["b".repeat(40_000)])] }), env);

  const first = await (await relay.fetch(fromPage("feed", signed(), { after: 0 }), env)).json();
  assert.deepEqual(first.events.map((e) => e.seq), [1, 3, 4]);
  assert.equal(first.events[2].state.runs[0].length, 40_000);
  const later = queue.chat.events(String(ALICE), 3);
  assert.deepEqual(later.map((body) => JSON.parse(body).seq), [4]);

  t.mock.method(console, "warn", () => {});
  await relay.fetch(fromVm(String(ALICE), { events: [state(5, ["c".repeat(70_000)])] }), env);
  assert.equal(JSON.parse(queue.chat.events(String(ALICE), 0).at(-1)).seq, 4);
});

test("a form's request to improve a field carries its action, field and values; anything else is turned away", async (t) => {
  // [AC-F3.1] Every check a message gets, then a chat envelope of kind assist.
  const { vm, env } = await chatRelay(t);
  const request = { kind: "assist", action: "ext:launch", field: "task", values: { task: "look", private: true } };
  assert.equal((await send(env, "look", { extra: request })).status, 202);
  const { payload } = sentChats(vm)[0];
  assert.deepEqual([payload.kind, payload.action, payload.field, payload.values], ["assist", "ext:launch", "task", request.values]);
  for (const [n, bad] of [{ ...request, kind: "other" }, { ...request, field: "../x" }, { ...request, values: [1] },
    { ...request, values: { task: { nested: 1 } } }].entries()) {
    const reply = await send(env, "look", { clientId: `0b9f3c2e-5d4a-4c1b-9e8f-00000000000${n}`, extra: bad });
    assert.equal(reply.status, 400);
  }
  assert.equal(sentChats(vm).length, 1);
});
