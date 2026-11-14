/*
 * The chat's state, apart from the page so the relay's tests check it: the conversation as the feed
 * events build it (lib/multiclaude/addons/chat/events.py makes them), the composer's limits, the
 * status line, and every string the chat shows (one table, for a translation later).
 */

export const TEXT_MAX = 16000;
export const COUNTER_FROM = 14000;
export const STOP_WITHIN = 15000;  // ms for a turn to end after Stop before the page says it didn't

export const STRINGS = {
  stranger: "This bot doesn't know you yet. Message it in Telegram to get a pairing code.",
  tooOld: "This window is too old. Close it and open the chat again.",
  reopen: "For safety, reopen the chat to keep sending.",
  unsigned: "Telegram didn't say who opened this page. Open the chat with the Chat button in your chat with the bot.",
  machineOffline: "The machine is offline, your message will go when it's back.",
  relayOffline: "Can't reach the relay. Messages wait here and go once it's back.",
  slowDown: "Slow down a bit.",
  tooLong: (n) => `${n.toLocaleString("en-US")} / ${TEXT_MAX.toLocaleString("en-US")}: too long to send`,
  counter: (n) => `${n.toLocaleString("en-US")} / ${TEXT_MAX.toLocaleString("en-US")}`,
  placeholder: "Message",
  send: "Send",
  stop: "Stop",
  couldntStop: "Couldn't stop — try again.",
  retry: "Retry",
  newChat: "New chat",
  newChatAsk: "Start a new chat? The assistant forgets this conversation; its memory files and your folder stay.",
  cancel: "Cancel",
  model: "Model",
  effort: "Effort",
  keep: "Keep after restarts",
  keepHint: "off: this session only · on: also after restarts",
  maxNotKept: "max can't be kept after restarts",
  pending: "Switching after the current turn…",
  older: "Show earlier messages",
  start: "Start of the conversation",
  fromTelegram: "from Telegram",
  fromSlack: "from Slack",
  fromSiri: "from Siri",
  fromOther: (name) => `From ${name}`,
  sentTo: (name) => `Sent to ${name}`,
  fileToast: "The file is in your Telegram chat with the bot.",
  copied: "Copied",
  copy: "Copy",
  answerInTelegram: "Answer in Telegram",
  emptyTitle: (bot) => `Chat with ${bot}`,
  emptyHint: "Same assistant as in your Telegram chat. Ask anything.",
  suggestions: ["What can you do?", "Summarize what we did today", "Help me write a message", "What's on my schedule?"],
  states: { sending: "Sending…", queued: "Queued", delivered: "Delivered", failed: "Not sent" },
  timings: "Timings",
  timingsHint: "Send → queued and entry → shown, the last 50.",
  status: {
    asleep: "Asleep: your next message wakes it",
    waking: "Waking up…",
    thinking: "Thinking…",
    tool: (tool) => `Running: ${tool}`,
    waiting_approval: "Waiting for your approval in Telegram",
  },
  modelHints: { opus: "most capable", sonnet: "fast and capable", haiku: "fastest, cheapest", fable: "Fable" },
  effortHints: { low: "quick answers", medium: "balanced", high: "thorough", xhigh: "more thorough",
    max: "deepest; this session only" },
};

const DELIVERY = { failed: 0, sending: 1, queued: 2, delivered: 3 };
const BUSY = new Set(["waking", "thinking", "tool", "waiting_approval"]);

export function newConversation() {
  return { items: [], lastSeq: 0, status: null, settings: null, cursor: undefined };
}

/* Whether events (as they came, oldest first) skip a seq after last, the one the page had: they're
 * compared with each other and with last only, never with 0, so a first read takes what comes. */
export function hasGap(last, events) {
  let seen = last;
  for (const event of events) {
    if (seen && event.seq > seen + 1) return true;
    seen = Math.max(seen, event.seq);
  }
  return false;
}

/* Take feed events into the conversation. fresh: a first read (or a re-read), which may start
 * anywhere. Whether there was a gap, for the page to read again. */
export function apply(conversation, events, fresh = false) {
  const sorted = [...events].sort((a, b) => a.seq - b.seq);
  const gap = !fresh && hasGap(conversation.lastSeq, sorted);
  for (const event of sorted) {
    conversation.lastSeq = Math.max(conversation.lastSeq, event.seq || 0);
    take(conversation, event);
  }
  order(conversation);
  return gap;
}

function take(conversation, event) {
  switch (event.kind) {
    case "cleared":
      conversation.items = conversation.items.filter((item) => item.local);  // still on its way, so after the clear
      conversation.cursor = null;
      return;
    case "status":
      conversation.status = event;
      return;
    case "settings":
      conversation.settings = event;
      return;
    case "history":
      conversation.cursor = event.cursor;
      return;
    case "message_edited": {
      const item = conversation.items.find((i) => i.key === event.key);
      if (item) Object.assign(item, { markdown: event.markdown, copy: event.copy, edited: true });
      return;
    }
    default:
      if (event.key) upsert(conversation, event);
  }
}

/* An item in place of the one with its key, or of the page's own message it confirms (client_id). */
function upsert(conversation, event) {
  let item = conversation.items.find((i) => i.key === event.key);
  if (!item && event.client_id) item = conversation.items.find((i) => i.client_id === event.client_id);
  if (!item) {
    conversation.items.push({ ...event });
    return;
  }
  const state = item.state && event.state && DELIVERY[item.state] > DELIVERY[event.state] ? item.state : event.state;
  Object.assign(item, event, state ? { state } : {}, { local: false });
}

function order(conversation) {
  conversation.items.sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0));
}

/* Older events from a history request, before what's shown. */
export function addOlder(conversation, events, cursor) {
  for (const event of events) if (event.key && !conversation.items.some((i) => i.key === event.key)) upsert(conversation, event);
  order(conversation);
  conversation.cursor = cursor;
}

/* The page's own message, shown at once while it goes. */
export function localMessage(clientId, text, now = Date.now()) {
  const ts = new Date(now).toISOString();
  return { kind: "user_message", key: `c:${clientId}`, client_id: clientId, text, source: "miniapp", state: "sending",
    ts, at: ts, local: true, sentAt: now };
}

export function setState(conversation, clientId, state) {
  const item = conversation.items.find((i) => i.client_id === clientId);
  if (item && (state === "failed" || DELIVERY[state] > DELIVERY[item.state] || item.state === "failed")) item.state = state;
  return item;
}

/* What the composer says of its text: {ok, counter, reason}. */
export function composerCheck(text) {
  const length = text.length;
  const counter = length >= COUNTER_FROM ? STRINGS.counter(length) : "";
  if (length > TEXT_MAX) return { ok: false, counter, reason: STRINGS.tooLong(length) };
  return { ok: text.trim().length > 0, counter, reason: "" };
}

/* Whether a turn runs, as the status says. */
export function busy(status) {
  return Boolean(status && BUSY.has(status.state));
}

/* The status line: "Thinking… 12s"; "" when there's nothing to say. */
export function statusText(status, now = Date.now()) {
  if (!status || status.state === "idle") return "";
  const words = STRINGS.status[status.state];
  const text = typeof words === "function" ? words(status.tool || "a tool") : words || "";
  if (status.state === "asleep" || !status.since) return text;
  const seconds = Math.max(0, Math.round((now - Date.parse(status.since)) / 1000));
  return `${text} ${elapsed(seconds)}`;
}

export function elapsed(seconds) {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes}m ${seconds % 60}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/* Where a message came from, when it wasn't the Mini App. */
export function sourceLabel(item) {
  if (item.source === "telegram") return STRINGS.fromTelegram;
  if (item.source === "slack") return STRINGS.fromSlack;
  if (item.source === "siri") return STRINGS.fromSiri;
  if (item.source === "crosschat") return STRINGS.fromOther(item.from || "someone");
  return "";
}

/* Whether Enter sends (a desktop Telegram) or adds a line (a phone). */
export function enterSends(platform) {
  return ["tdesktop", "macos", "web", "weba", "webk", "unigram"].includes(platform);
}

/* What a model switch did, from a router notice: {setting, failed, text} or null. */
export function switchOutcome(text) {
  const failed = /^⚠️ Couldn't switch (model|effort)/.exec(text || "");
  if (failed) return { setting: failed[1], failed: true, text: text.replace(/^⚠️\s*/, "") };
  const done = /^✅ (Model|Effort):/.exec(text || "");
  return done ? { setting: done[1].toLowerCase(), failed: false, text } : null;
}

/* A file's size as people read it. */
export function fileSize(bytes) {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
