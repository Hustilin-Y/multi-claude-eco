/*
 * The chat's part of the relay, mounted while the chat add-in is on (the router's side is
 * lib/multiclaude/addons/chat, the page public/app/chat.js).
 *
 * The page sends Telegram's init data as the control panel does. Reads trust it for a day; sending a
 * message, which runs commands as a full admin of the machine, only for an hour after Telegram
 * signed it. Only the people on the bot get anything: the router puts their Telegram ids at
 * /chat/members, and anyone else is answered 403 before anything is queued or kept.
 *
 * A message goes to the VM as a "chat" envelope stamped with when it was queued. What the page shows
 * comes back as feed events the router puts at /chat/<telegram id>, while the page reads them. The
 * Durable Object keeps them as a cache, not an archive: a person's last EVENTS_KEPT, for a day, and
 * none of the old conversation after a "cleared" one. A page's first read asks the router for the
 * conversation so far (a "chat_open" envelope, with backfill), and a read once a minute after that
 * keeps it coming; older history is asked for when the page scrolls up (askVm).
 */
import { initDataOf, webAppUser } from "./panel.js";
import { REQUEST_ID_RE, answerFromVm, askVm, fromTheVm, newRequestId, relayObject } from "./vm.js";

export const SEND_SIGNED_FOR = 60 * 60;          // s: how old init data may be to send a message
export const FEED_WAIT_FOR = 25 * 1000;          // ms a read waits for events newer than the ones it has
export const HISTORY_WAIT_FOR = 10 * 1000;       // ms a request for older history waits for the VM
export const EVENTS_KEPT = 200;                  // per person
export const EVENTS_KEPT_FOR = 24 * 60 * 60 * 1000;
export const EVENT_MAX = 16 * 1024;              // bytes of one event's JSON
export const ADDON_STATE_MAX = 64 * 1024;        // and of an add-in's state for its tab, kept one per add-in
export const BODY_MAX = 64 * 1024;               // bytes of a message the page posts
export const TEXT_MAX = 16000;                   // characters of a message
export const SENDS_PER_MINUTE = 20;
export const BACKFILL = 50;                      // events a page's first read gets
const KEEP_OPEN_EVERY = 60 * 1000;               // ms between the reads that tell the router the page is still open
const PUT_MAX = 8 * 1024 * 1024;                 // bytes of what the VM puts at once
const CUT_NOTE = "… (full text in Telegram)";
const CLIENT_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const TARGET_RE = /^[a-z0-9][a-z0-9-]*:[\w.:-]{1,80}$/;
const FIELD_RE = /^\w{1,40}$/;  // "<add-in>:<its own id>": a model another add-in brings

export const routes = [
  { method: "POST", path: "/app/chat/send", handle: send },
  { method: "POST", path: "/app/chat/feed", handle: feed },
  { method: "POST", path: "/app/chat/history", handle: history },
  { method: "PUT", path: "/chat/members", handle: fromVm },
  { method: "PUT", path: /^\/chat\/history\/([^/]+)$/, handle: historyFromVm },
  { method: "PUT", path: /^\/chat\/(\d+)$/, handle: fromVm },
];

/* --- the page's requests --- */

/* Who sent the request, or the response that refuses it: 401 for init data Telegram didn't sign or
 * signed over a day ago (or, with sending, over an hour ago: {"error": "reopen"}). */
async function signedPerson(request, env, sending) {
  const initData = initDataOf(request.headers);
  const user = await webAppUser(initData, env.TELEGRAM_WEBAPP_KEY);
  if (!user) return { refused: Response.json({ error: "unsigned" }, { status: 401 }) };
  if (sending && !(await webAppUser(initData, env.TELEGRAM_WEBAPP_KEY, Date.now(), SEND_SIGNED_FOR))) {
    return { refused: Response.json({ error: "reopen" }, { status: 401 }) };
  }
  return { user: String(user.id), initData };
}

async function send(request, env) {
  const { user, initData, refused } = await signedPerson(request, env, true);
  if (refused) return refused;
  const body = await request.text();
  if (body.length > BODY_MAX) return Response.json({ error: "too large" }, { status: 413 });
  const message = chatMessage(body);
  if (!message) return Response.json({ error: "not a message" }, { status: 400 });
  return inObject(env, "send", user, { envelope: chatEnvelope(user, initData, message) });
}

/* {client_id, text, target?} from what the page posted, or null. target names a model another add-in
 * brings, which the message goes to instead of the assistant. Anything else in it (a model, an
 * effort) is left behind: a message never switches settings. */
export function chatMessage(body) {
  let posted;
  try {
    posted = JSON.parse(body);
  } catch {
    return null;
  }
  const clientId = String(posted?.client_id || "");
  const text = posted?.text;
  if (!CLIENT_ID_RE.test(clientId) || typeof text !== "string" || !text.trim() || text.length > TEXT_MAX) return null;
  const target = posted?.target;
  if (target != null && !(typeof target === "string" && TARGET_RE.test(target))) return null;
  const message = { client_id: clientId.toLowerCase(), text, ...(target ? { target } : {}) };
  if (posted?.kind == null) return message;
  return ["assist", "tab_action"].includes(posted.kind) ? assistRequest(message, posted) : null;
}

/* A request to the add-in that brings a form or a tab: an assist (one of the form's fields improved:
 * its action "<add-in>:<id>", the field, the form's values) or a tab's action (its id and values).
 * Values are strings and switches, as the page has them. */
function assistRequest(message, { kind, action, field, values }) {
  if (typeof action !== "string" || !TARGET_RE.test(action)) return null;
  if (kind === "assist" && !(typeof field === "string" && FIELD_RE.test(field))) return null;
  if (!values || typeof values !== "object" || Array.isArray(values)) return null;
  const entries = Object.entries(values);
  if (entries.length > 20 || !entries.every(([name, value]) => FIELD_RE.test(name)
    && (typeof value === "boolean" || typeof value === "string" && value.length <= TEXT_MAX))) return null;
  return { ...message, kind, action, ...(kind === "assist" ? { field } : {}), values };
}

/* The envelope that takes a message to the VM: one per person and message (a retry is the same one),
 * with the init data for the VM to check again and when it was queued. */
export function chatEnvelope(user, initData, { client_id: clientId, text, target, kind, action, field, values },
  now = Date.now()) {
  return { envelope_id: `chat:${user}:${clientId}`, type: "chat",
    payload: { init_data: initData, client_id: clientId, text, ...(target ? { target } : {}),
      ...(kind ? { kind, action, field, values } : {}), queued_at: now / 1000 } };
}

async function feed(request, env) {
  const { user, refused } = await signedPerson(request, env, false);
  if (refused) return refused;
  const { after } = await request.json().catch(() => ({}));
  return inObject(env, "feed", user, { after: Math.max(0, Number(after) || 0) });
}

/* Older events, asked of the VM: {events, cursor}, or 504 when it doesn't answer in time. */
async function history(request, env) {
  const { user, refused } = await signedPerson(request, env, false);
  if (refused) return refused;
  const { cursor } = await request.json().catch(() => ({}));
  const member = await inObject(env, "member", user, {});
  if (member.status !== 204) return member;
  const id = newRequestId();
  const { answer, late } = await askVm(env, { envelope_id: historyEnvelopeId(id), type: "chat_history",
    payload: { id, user, cursor: cursor ?? null } }, HISTORY_WAIT_FOR);
  return late ? Response.json({ error: "late" }, { status: 504 }) : Response.json(answer);
}

function historyEnvelopeId(id) {
  return `chat_history:${id}`;
}

/* --- the VM's --- */

async function fromVm(request, env, [, user]) {
  if (!fromTheVm(request.headers, env)) return new Response("unauthorized", { status: 401 });
  const body = await request.text();
  if (body.length > PUT_MAX) return new Response("too large", { status: 413 });
  let put;
  try {
    put = JSON.parse(body);
  } catch {
    return new Response("not JSON", { status: 400 });
  }
  return user ? inObject(env, "events", user, put) : inObject(env, "members", "", put);
}

async function historyFromVm(request, env, [, id]) {
  if (!fromTheVm(request.headers, env)) return new Response("unauthorized", { status: 401 });
  if (!REQUEST_ID_RE.test(id)) return new Response("not a request id", { status: 400 });
  const body = await request.text();
  if (body.length > PUT_MAX) return new Response("too large", { status: 413 });
  return answerFromVm(env, historyEnvelopeId(id), body);
}

function inObject(env, what, user, body) {
  return relayObject(env).fetch(`https://relay/chat/${what}?user=${encodeURIComponent(user)}`,
    { method: "POST", body: JSON.stringify(body) });
}

/* --- in the Durable Object (index.js, Relay): what it keeps for the chat --- */

/* A request index.js's Relay passes on for a path under /chat/. */
export async function inRelay(relay, url, request) {
  const chat = chatOf(relay);
  const user = url.searchParams.get("user") || "";
  const body = await request.json().catch(() => ({}));
  const what = url.pathname.slice("/chat/".length);
  if (what === "members") return chat.keepMembers(body.ids);
  if (what === "events") return chat.keepEvents(user, body);
  if (!chat.member(user)) return Response.json({ error: "stranger" }, { status: 403 });
  if (what === "member") return new Response(null, { status: 204 });
  if (what === "send") return chat.queue(user, body.envelope);
  if (what === "feed") return chat.read(user, body.after);
  return new Response("not found", { status: 404 });
}

/* The Relay's alarm: events older than a day go, also for people whose page stays closed. */
export function alarm(relay) {
  chatOf(relay).forgetOld();
}

function chatOf(relay) {
  relay.chat ??= new ChatStore(relay);
  return relay.chat;
}

export class ChatStore {
  constructor(relay) {
    this.relay = relay;
    this.sql = relay.sql;
    this.sql.exec("CREATE TABLE IF NOT EXISTS chat_events (user TEXT NOT NULL, seq INTEGER NOT NULL, body TEXT NOT NULL, "
      + "kept INTEGER NOT NULL, PRIMARY KEY (user, seq))");
    this.sql.exec("CREATE TABLE IF NOT EXISTS chat_members (user TEXT PRIMARY KEY)");
    this.sql.exec("CREATE TABLE IF NOT EXISTS chat_rate (user TEXT NOT NULL, minute INTEGER NOT NULL, count INTEGER NOT NULL, "
      + "PRIMARY KEY (user, minute))");
    this.sql.exec("CREATE TABLE IF NOT EXISTS chat_open (user TEXT PRIMARY KEY, told INTEGER NOT NULL)");
    this.sql.exec("CREATE TABLE IF NOT EXISTS chat_slots (user TEXT NOT NULL, slot TEXT NOT NULL, seq INTEGER NOT NULL, "
      + "body TEXT NOT NULL, kept INTEGER NOT NULL, PRIMARY KEY (user, slot))");
    this.waiting = new Set();  // reads waiting for newer events: {user, wake}
  }

  member(user) {
    return Boolean(user) && this.sql.exec("SELECT user FROM chat_members WHERE user = ?", user).toArray().length > 0;
  }

  keepMembers(ids) {
    if (!Array.isArray(ids) || !ids.every((id) => /^\d+$/.test(String(id)))) return new Response("not a list of ids", { status: 400 });
    this.sql.exec("DELETE FROM chat_members");
    for (const id of ids) this.sql.exec("INSERT OR IGNORE INTO chat_members (user) VALUES (?)", String(id));
    return new Response(null, { status: 204 });
  }

  /* A message for the VM, unless the person sent SENDS_PER_MINUTE this minute already. */
  queue(user, envelope, now = Date.now()) {
    const minute = Math.floor(now / 60000);
    this.sql.exec("DELETE FROM chat_rate WHERE minute < ?", minute);
    const [row] = this.sql.exec("SELECT count FROM chat_rate WHERE user = ? AND minute = ?", user, minute).toArray();
    if ((row?.count || 0) >= SENDS_PER_MINUTE) return Response.json({ error: "slow down" }, { status: 429 });
    this.sql.exec("INSERT INTO chat_rate (user, minute, count) VALUES (?, ?, 1) "
      + "ON CONFLICT (user, minute) DO UPDATE SET count = count + 1", user, minute);
    this.relay.keep(envelope, now);
    return Response.json({ queued_at: envelope.payload.queued_at }, { status: 202 });
  }

  /* {events, online}: with after 0 (a first read) the last BACKFILL events, and the router is asked
   * for the conversation so far; otherwise those after it, waiting up to waitFor for some. */
  async read(user, after, now = Date.now(), waitFor = FEED_WAIT_FOR) {
    this.forgetOld(now);
    this.tellRouter(user, after === 0, now);
    let events = this.events(user, after);
    if (!events.length) {
      await this.newer(user, waitFor);
      events = this.events(user, after);
    }
    return new Response(`{"events":[${events.join(",")}],"online":${this.online()}}`,
      { headers: { "content-type": "application/json" } });
  }

  /* The events after seq after (the last BACKFILL for 0), with each add-in's latest state among them. */
  events(user, after) {
    const rows = after
      ? this.sql.exec("SELECT seq, body FROM chat_events WHERE user = ? AND seq > ? ORDER BY seq", user, after).toArray()
      : this.sql.exec("SELECT seq, body FROM chat_events WHERE user = ? ORDER BY seq DESC LIMIT ?", user, BACKFILL).toArray().reverse();
    const slots = this.sql.exec("SELECT seq, body FROM chat_slots WHERE user = ? AND seq > ?", user, after).toArray();
    return [...rows, ...slots].sort((a, b) => a.seq - b.seq).map((row) => row.body);
  }

  /* The router follows a person's feed only while their page reads it: a "chat_open" envelope on
   * the first read (backfill), then at most once every KEEP_OPEN_EVERY. */
  tellRouter(user, backfill, now) {
    const [row] = this.sql.exec("SELECT told FROM chat_open WHERE user = ?", user).toArray();
    if (!backfill && row && now - row.told < KEEP_OPEN_EVERY) return;
    this.sql.exec("INSERT OR REPLACE INTO chat_open (user, told) VALUES (?, ?)", user, now);
    this.relay.keep({ envelope_id: `chat_open:${user}:${backfill ? 1 : 0}`, type: "chat_open",
      payload: { user, backfill } }, now);
  }

  /* Whether the VM is connected now. */
  online() {
    return this.relay.ctx.getWebSockets().length > 0;
  }

  newer(user, waitFor) {
    return new Promise((resolve) => {
      const read = { user, wake: () => {
        clearTimeout(timer);
        this.waiting.delete(read);
        resolve();
      } };
      const timer = setTimeout(read.wake, waitFor);
      this.waiting.add(read);
    });
  }

  /* Events the VM put ({events, replace}): replace (a backfill) drops what was kept first, and a
   * "cleared" event drops everything before it. */
  keepEvents(user, { events, replace } = {}, now = Date.now()) {
    if (!/^\d+$/.test(user) || !Array.isArray(events)) return new Response("not events", { status: 400 });
    if (replace) this.sql.exec("DELETE FROM chat_events WHERE user = ?", user);
    for (const event of events) {
      if (!event || !Number.isSafeInteger(event.seq) || event.seq <= 0) continue;
      if (event.kind === "cleared") this.sql.exec("DELETE FROM chat_events WHERE user = ? AND seq < ?", user, event.seq);
      const body = fitted(event);
      if (!body) continue;
      if (event.kind === "addon_state") {
        this.sql.exec("INSERT OR REPLACE INTO chat_slots (user, slot, seq, body, kept) VALUES (?, ?, ?, ?, ?)",
          user, String(event.addon || ""), event.seq, body, now);
      } else {
        this.sql.exec("INSERT OR REPLACE INTO chat_events (user, seq, body, kept) VALUES (?, ?, ?, ?)", user, event.seq, body, now);
      }
    }
    this.sql.exec("DELETE FROM chat_events WHERE user = ? AND seq NOT IN "
      + "(SELECT seq FROM chat_events WHERE user = ? ORDER BY seq DESC LIMIT ?)", user, user, EVENTS_KEPT);
    this.forgetOld(now);
    this.relay.ctx.storage.setAlarm?.(now + EVENTS_KEPT_FOR + 60 * 1000);
    for (const read of this.waiting) if (read.user === user) read.wake();
    return new Response(null, { status: 204 });
  }

  forgetOld(now = Date.now()) {
    this.sql.exec("DELETE FROM chat_events WHERE kept < ?", now - EVENTS_KEPT_FOR);
    this.sql.exec("DELETE FROM chat_slots WHERE kept < ?", now - EVENTS_KEPT_FOR);
  }
}

/* An event's JSON, cut to EVENT_MAX (an add-in's state: ADDON_STATE_MAX): a long message's text is
 * shortened with a note; anything else that big is left out (null), with a warning in the Worker's
 * log naming it, not what it holds. */
export function fitted(event) {
  const max = event.kind === "addon_state" ? ADDON_STATE_MAX : EVENT_MAX;
  const body = cut(event, max);
  if (body === null) console.warn(`chat: a ${event.kind} event (seq ${event.seq}) over ${max} bytes was left out`);
  return body;
}

function cut(event, max) {
  let body = JSON.stringify(event);
  if (bytes(body) <= max) return body;
  if (event.kind === "addon_state") return null;
  const field = ["markdown", "text"].find((name) => typeof event[name] === "string");
  if (!field) return null;
  let text = event[field];
  while (text && bytes(body) > EVENT_MAX) {
    text = text.slice(0, Math.max(0, text.length - Math.ceil((bytes(body) - EVENT_MAX) / 2) - CUT_NOTE.length - 16));
    body = JSON.stringify({ ...event, [field]: text + CUT_NOTE });
  }
  return bytes(body) <= EVENT_MAX ? body : null;
}

function bytes(text) {
  return new TextEncoder().encode(text).length;
}
