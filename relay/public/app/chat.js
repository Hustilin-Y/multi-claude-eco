/*
 * The chat tab of the Mini App (the chat add-in): a chat with the person's own assistant, the same one
 * as their Telegram chat. Messages go to the relay (src/chat.js), which queues them for the router;
 * what the page shows comes from the feed the router puts on the relay while the page reads it,
 * long-polled like the control panel's snapshot. The model and effort chips send the control panel's
 * own taps. The conversation's state is in chatfeed.js, the Markdown in markdown.js.
 *
 * The tab bar shows only while the chat add-in is on: the router's Chat button opens the page with
 * ?tab=chat, and the control panel's snapshot lists the add-ins that are on.
 */
import { STOP_WITHIN, STRINGS, addOlder, apply, busy, composerCheck, enterSends, fileSize, localMessage, newConversation,
  setState, sourceLabel, statusText, switchOutcome } from "./chatfeed.js";
import { render as renderMarkdown } from "./markdown.js";

const tg = window.Telegram?.WebApp;
const KEEP_KEY = "keep-switches";      // the Keep after restarts switch, shared with the control panel
const OFFLINE_AFTER = 30000;           // ms the VM has to be away before the page says it's offline
const RETRY_FIRST = 1000;
const RETRY_MAX = 30000;
const TOAST_FOR = 3000;
const TIMINGS_KEPT = 50;
const NEAR_BOTTOM = 120;               // px: a list scrolled this close to its end follows new messages
const MAX_COMPOSER_SHARE = 0.4;        // of the viewport's height

const $ = (id) => document.getElementById(id);
const conversation = newConversation();
let snapshot = null;                   // the control panel's snapshot: models, efforts, the bot's name
let chatOn = false;
let polling = false;
let offlineSince = null;
let relayDown = false;
let stopping = null;                   // {since, timer} while a Stop waits for the turn to end
let asked = {};                        // "model" | "effort" -> the value asked for, until a settings event has it
let refused = {};                      // effort level -> the router's reason, for the model refusedFor
let refusedFor = null;
let loadingOlder = false;
let sheetClose = null;
const timings = [];
const shownHtml = new WeakMap();       // item -> [markdown, its HTML], so a message is rendered once

start();

function start() {
  if (!tg?.initData) return;  // the control panel says what's wrong
  const wanted = new URLSearchParams(location.search).get("tab") === "chat";
  if (wanted) enableChat(true);
  readSnapshot().then(() => {
    if (snapshot?.addons?.includes("chat")) enableChat(wanted);
  }).catch(() => {});
}

/* --- the tabs --- */

function enableChat(select) {
  if (chatOn) {
    if (select) showTab("chat");
    return;
  }
  chatOn = true;
  $("tabs").hidden = false;
  for (const button of document.querySelectorAll("#tabs [data-tab]")) button.addEventListener("click", () => showTab(button.dataset.tab));
  setUp();
  showTab(select ? "chat" : "panel");
}

function showTab(tab) {
  const chat = tab === "chat";
  $("chat").hidden = !chat;
  $("panel").hidden = chat;
  document.body.classList.toggle("in-chat", chat);
  for (const button of document.querySelectorAll("#tabs [data-tab]")) button.setAttribute("aria-selected", String(button.dataset.tab === tab));
  if (chat) {
    poll();
    scrollToEnd();
  }
}

function setUp() {
  if (tg.isVersionAtLeast?.("8.0") && ["ios", "android"].includes(tg.platform)) tg.requestFullscreen?.();
  tg.disableVerticalSwipes?.();
  tg.onEvent?.("themeChanged", themed);
  tg.onEvent?.("backButtonClicked", () => sheetClose?.());
  themed();
  $("chat-text").placeholder = STRINGS.placeholder;
  $("chat-text").addEventListener("input", composerChanged);
  $("chat-text").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && enterSends(tg.platform)) {
      event.preventDefault();
      submit();
    }
  });
  $("chat-composer").addEventListener("submit", (event) => {
    event.preventDefault();
    submit();
  });
  $("chip-model").addEventListener("click", () => pickSheet("model"));
  $("chip-effort").addEventListener("click", () => pickSheet("effort"));
  $("chat-new").addEventListener("click", confirmNewChat);
  $("chat-timings").addEventListener("click", showTimings);
  $("chat-older").addEventListener("click", loadOlder);
  $("chat-list").addEventListener("scroll", () => {
    if ($("chat-list").scrollTop < 80) loadOlder();
  });
  $("chat-items").addEventListener("click", clicked);
  document.addEventListener("visibilitychange", () => document.hidden || poll());
  setInterval(drawStatus, 1000);
  composerChanged();
  draw();
}

/* Telegram's colors come as CSS variables the Telegram script keeps current; the header follows. */
function themed() {
  if (tg.isVersionAtLeast?.("6.1")) {
    tg.setHeaderColor?.("bg_color");
    tg.setBackgroundColor?.("bg_color");
  }
}

/* --- the relay --- */

class Refused extends Error {
  constructor(status, error) {
    super(`the relay answered ${status}`);
    Object.assign(this, { status, error });
  }
}

async function relay(path, body = {}) {
  const response = await fetch(`/app/${path}`, {
    method: "POST",
    headers: { authorization: `tma ${tg.initData}`, "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const json = await response.json().catch(() => ({}));
  if (!response.ok) throw new Refused(response.status, json.error);
  return json;
}

async function readSnapshot() {
  const latest = await relay("state", {});
  snapshot = latest.snapshot;
  if (chatOn) draw();
}

/* Read the feed while the chat shows, each read waiting at the relay for what's new. A first read,
 * and one after a gap, starts afresh (after 0), which also has the router build the conversation. */
async function poll() {
  if (polling) return;
  polling = true;
  let wait = RETRY_FIRST;
  let fresh = true;
  try {
    while (!$("chat").hidden && !document.hidden) {
      try {
        const read = await relay("chat/feed", { after: fresh ? 0 : conversation.lastSeq });
        relayDown = false;
        wait = RETRY_FIRST;
        offlineSince = read.online ? null : offlineSince ?? Date.now();
        const received = Date.now();
        for (const event of read.events) timing("entry → shown", received - Date.parse(event.at));
        const gap = apply(conversation, read.events, fresh);
        fresh = gap;
        afterEvents(read.events);
        draw();
      } catch (error) {
        if (error instanceof Refused && [401, 403].includes(error.status)) {
          refusedPage(error);
          return;
        }
        relayDown = true;
        draw();
        await sleep(wait);
        wait = Math.min(wait * 2, RETRY_MAX);
      }
    }
  } finally {
    polling = false;
  }
}

function refusedPage(error) {
  const text = error.status === 403 ? STRINGS.stranger : STRINGS.tooOld;
  $("chat-body").replaceChildren(el("p", { className: "chat-refused" }, text));
}

/* What new events mean beyond the list: a switch confirmed or refused, a Stop done. */
function afterEvents(events) {
  for (const event of events) {
    if (event.kind === "settings") {
      for (const setting of ["model", "effort"]) if (asked[setting] && asked[setting] === event[setting]) delete asked[setting];
      if (event.model !== refusedFor) refused = {};  // another model may take those levels
    }
    if (event.kind === "notice") {
      const outcome = switchOutcome(event.text);
      if (outcome?.failed) {
        const target = asked[outcome.setting];
        delete asked[outcome.setting];
        if (outcome.setting === "effort" && target) {
          refused[target] = outcome.text;
          refusedFor = currentSetting("model");
        }
        showError(outcome.text);
      }
    }
    if (event.kind === "cleared") $("chat-text").focus?.();
  }
  if (stopping && !busy(conversation.status)) stopDone();
}

/* --- sending --- */

function submit() {
  const text = $("chat-text").value;
  if (!composerCheck(text).ok) return;
  $("chat-text").value = "";
  composerChanged();
  const message = localMessage(crypto.randomUUID(), text);
  conversation.items.push(message);
  draw();
  scrollToEnd();
  send(message);
}

async function send(message, wait = RETRY_FIRST) {
  setState(conversation, message.client_id, "sending");
  draw();
  try {
    await relay("chat/send", { client_id: message.client_id, text: message.text });
    setState(conversation, message.client_id, "queued");
    timing("send → queued", Date.now() - message.sentAt);
  } catch (error) {
    if (error instanceof Refused && error.status === 401) {
      conversation.items = conversation.items.filter((item) => item !== message);
      if (!$("chat-text").value) $("chat-text").value = message.text;  // the draft stays
      composerChanged();
      showError(error.error === "reopen" ? STRINGS.reopen : STRINGS.tooOld);
    } else if (error instanceof Refused && error.status === 403) {
      refusedPage(error);
    } else {
      setState(conversation, message.client_id, "failed");
      message.reason = error instanceof Refused && error.status === 429 ? STRINGS.slowDown : "";
      if (!(error instanceof Refused) && wait <= RETRY_MAX) {
        message.retry = setTimeout(() => send(message, wait * 2), wait);  // the relay may be back soon
      }
    }
  }
  draw();
}

function retry(message) {
  clearTimeout(message.retry);
  send(message);  // the same client_id: the router takes it once
}

/* Stop: /stop, sent like a message but shown as nothing but the button's spinner. */
function stopTurn() {
  if (stopping) return;
  stopping = { since: Date.now(), timer: setTimeout(() => {
    stopping = null;
    showError(STRINGS.couldntStop);
    draw();
  }, STOP_WITHIN) };
  command("/stop");
  draw();
}

function stopDone() {
  clearTimeout(stopping.timer);
  stopping = null;
}

function command(text) {
  relay("chat/send", { client_id: crypto.randomUUID(), text }).catch((error) => {
    showError(error instanceof Refused && error.error === "reopen" ? STRINGS.reopen : STRINGS.relayOffline);
  });
}

function confirmNewChat() {
  openSheet(STRINGS.newChat, [
    el("p", { className: "sheet-text" }, STRINGS.newChatAsk),
    el("button", { className: "sheet-button destructive", onclick: () => {
      closeSheet();
      command("/clear");
    } }, STRINGS.newChat),
    el("button", { className: "sheet-button", onclick: closeSheet }, STRINGS.cancel),
  ]);
}

/* --- model and effort --- */

function currentSetting(setting) {
  const settings = conversation.settings;
  if (setting === "model") return settings?.model ?? snapshot?.claude?.current_model ?? null;
  return settings?.effort ?? snapshot?.claude?.effort ?? null;
}

function options(setting) {
  const claude = snapshot?.claude;
  if (setting === "model") return claude?.models || ["opus", "sonnet", "haiku", "fable"];
  return claude?.efforts || ["low", "medium", "high", "xhigh", "max"];
}

function storedKeep() {
  try {
    return localStorage.getItem(KEEP_KEY) === "true";
  } catch {
    return false;
  }
}

function storeKeep(on) {
  try {
    localStorage.setItem(KEEP_KEY, String(on));
  } catch {
    // storage is off: it holds while the page is open
  }
}

function pickSheet(setting) {
  const current = asked[setting] ?? currentSetting(setting);
  const hints = setting === "model" ? STRINGS.modelHints : STRINGS.effortHints;
  let keep = storedKeep();
  const keepNote = el("span", { className: "sub" }, STRINGS.keepHint);
  const keepSwitch = el("input", { type: "checkbox", checked: keep, onchange: () => {
    keep = keepSwitch.checked;
    storeKeep(keep);
  } });
  const rows = options(setting).map((value) => {
    const reason = setting === "effort" ? refused[value] : null;
    return el("button", { className: `option${value === current ? " chosen" : ""}`, disabled: Boolean(reason),
      onclick: () => {
        if (setting === "effort" && value === "max") keep = false;
        closeSheet();
        choose(setting, value, keep);
      } },
    el("span", { className: "option-name" }, value), el("span", { className: "sub" }, reason || hints[value] || ""),
    el("span", { className: "tick" }, value === current ? "✓" : ""));
  });
  const keepRow = el("label", { className: "option keep" }, el("span", { className: "option-name" }, STRINGS.keep, keepNote), keepSwitch);
  if (setting === "effort" && current === "max") {
    keepSwitch.disabled = true;
    keepNote.textContent = STRINGS.maxNotKept;
  }
  openSheet(setting === "model" ? STRINGS.model : STRINGS.effort, [...rows, keepRow,
    setting === "effort" ? el("p", { className: "sheet-text sub" }, `max: ${STRINGS.maxNotKept}.`) : null]);
}

/* A pick goes at once, as the control panel's own tap; the chip shows it with a dot until a
 * settings event has it. */
function choose(setting, value, keep) {
  if (value === currentSetting(setting) && !keep) return;
  asked[setting] = value;
  draw();
  relay("action", { id: crypto.randomUUID(), action: setting, args: { target: value, keep: keep && value !== "max" } })
    .catch(() => {
      delete asked[setting];
      showError(STRINGS.relayOffline);
      draw();
    });
}

/* --- older history --- */

async function loadOlder() {
  if (loadingOlder || !conversation.cursor) return;
  loadingOlder = true;
  const list = $("chat-list");
  const fromBottom = list.scrollHeight - list.scrollTop;
  try {
    const older = await relay("chat/history", { cursor: conversation.cursor });
    addOlder(conversation, older.events || [], older.cursor ?? null);
    draw();
    list.scrollTop = list.scrollHeight - fromBottom;
  } catch {
    // the VM didn't answer in time; the button stays for another try
  } finally {
    loadingOlder = false;
  }
}

/* --- drawing --- */

function draw() {
  if (!chatOn) return;
  const list = $("chat-list");
  const follow = list.scrollHeight - list.scrollTop - list.clientHeight < NEAR_BOTTOM;
  $("chat-bot").textContent = conversation.settings?.bot || snapshot?.bot || "Chat";
  $("chat-timings").hidden = !snapshot?.machine;
  drawBanner();
  drawItems();
  drawStatus();
  drawChips();
  drawSendButton();
  if (follow) scrollToEnd();
}

function drawBanner() {
  const offline = offlineSince && Date.now() - offlineSince > OFFLINE_AFTER;
  const text = relayDown ? STRINGS.relayOffline : offline ? STRINGS.machineOffline : "";
  $("chat-banner").textContent = text;
  $("chat-banner").hidden = !text;
}

function drawItems() {
  const items = conversation.items;
  $("chat-empty").hidden = items.length > 0;
  if (!items.length) drawEmpty();
  $("chat-older").hidden = !conversation.cursor;
  $("chat-items").replaceChildren(...items.map(itemNode).filter(Boolean));
}

function drawEmpty() {
  const bot = conversation.settings?.bot || snapshot?.bot || "the bot";
  $("chat-empty").replaceChildren(el("h2", {}, STRINGS.emptyTitle(bot)), el("p", { className: "sub" }, STRINGS.emptyHint),
    el("div", { className: "suggestions" }, ...STRINGS.suggestions.map((text) => el("button", { className: "suggestion",
      onclick: () => {
        $("chat-text").value = text;  // put in the composer, not sent
        composerChanged();
        $("chat-text").focus();
      } }, text))));
}

function itemNode(item) {
  if (item.kind === "user_message") return userNode(item);
  if (item.kind === "assistant_message") return item.to ? sentToNode(item) : assistantNode(item);
  if (item.kind === "notice") return el("div", { className: "notice-line" }, item.text);
  if (item.kind === "step") return stepNode(item);
  return null;
}

function userNode(item) {
  const label = sourceLabel(item);
  const state = item.source === "miniapp" && item.state && item.state !== "delivered"
    ? el("span", { className: `delivery ${item.state}` }, STRINGS.states[item.state] || item.state) : null;
  const retryButton = item.state === "failed" && item.local !== false && item.client_id
    ? el("button", { className: "link-button", onclick: () => retry(item) }, STRINGS.retry) : null;
  return el("div", { className: "msg user", title: moment(item.ts) },
    label ? el("span", { className: "from" }, label) : null,
    el("div", { className: "bubble" }, item.text),
    state || retryButton || item.reason ? el("div", { className: "msg-foot" }, state, item.reason ? ` ${item.reason}` : "", retryButton) : null);
}

function assistantNode(item) {
  const body = el("div", { className: "markdown" });
  body.innerHTML = rendered(item);
  return el("div", { className: "msg assistant", title: moment(item.ts) }, body,
    item.files?.length ? el("div", { className: "files" }, ...item.files.map(fileChip)) : null,
    item.copy?.length ? el("div", { className: "copies" }, ...item.copy.map(copyButton)) : null,
    item.buttons?.length ? el("div", { className: "asks" }, ...item.buttons.map((label) => el("span", { className: "ask" }, label)),
      el("span", { className: "sub" }, STRINGS.answerInTelegram)) : null,
    item.checklist?.length ? el("ul", { className: "checklist" }, ...item.checklist.map((label) => el("li", {}, `☐ ${label}`))) : null);
}

function sentToNode(item) {
  const body = el("div", { className: "markdown" });
  body.innerHTML = rendered(item);
  return el("details", { className: "sent-to" }, el("summary", {}, STRINGS.sentTo(item.to)), body);
}

function stepNode(item) {
  const mark = item.status === "running" ? "…" : item.status === "error" ? "✕" : "✓";
  const title = el("span", { className: "step-title" });
  title.innerHTML = renderMarkdown(item.title || item.tool).replace(/^<p>|<\/p>\s*$/g, "");
  return el("div", { className: `step ${item.status}` }, el("span", { className: "step-mark" }, mark), title);
}

function rendered(item) {
  const kept = shownHtml.get(item);
  if (kept && kept[0] === item.markdown) return kept[1];
  const html = renderMarkdown(item.markdown);
  shownHtml.set(item, [item.markdown, html]);
  return html;
}

function fileChip(file) {
  return el("button", { className: "file", onclick: openBotChat },
    el("span", { className: "file-name" }, file.name), el("span", { className: "sub" }, [fileSize(file.size), file.mime].filter(Boolean).join(" · ")));
}

function copyButton(text) {
  return el("button", { className: "copy-button", onclick: () => copy(text) }, `📋 ${text.length > 40 ? `${text.slice(0, 39)}…` : text}`);
}

/* A file sent in a message is in the bot's chat: Telegram has no link to a message in a private chat. */
function openBotChat() {
  const username = conversation.settings?.bot_username;
  if (username) tg.openTelegramLink(`https://t.me/${username}`);
  toast(STRINGS.fileToast);
}

function drawStatus() {
  const text = stopping ? `${STRINGS.stop}…` : statusText(conversation.status);
  $("chat-status").textContent = text;
  $("chat-status").hidden = !text;
  $("chat-state").textContent = conversation.status?.state === "asleep" ? "asleep" : "";
}

function drawChips() {
  for (const setting of ["model", "effort"]) {
    const chip = $(`chip-${setting}`);
    const value = asked[setting] ?? currentSetting(setting);
    chip.textContent = value || (setting === "model" ? STRINGS.model : STRINGS.effort);
    chip.classList.toggle("pending", setting in asked);
    chip.title = setting in asked ? STRINGS.pending : "";
  }
}

function drawSendButton() {
  const button = $("chat-send");
  const check = composerCheck($("chat-text").value);
  const stop = busy(conversation.status) && !$("chat-text").value.trim();
  button.classList.toggle("stop", stop);
  button.classList.toggle("spinning", Boolean(stopping));
  button.setAttribute("aria-label", stop ? STRINGS.stop : STRINGS.send);
  button.textContent = stop ? "■" : "↑";
  button.disabled = stop ? Boolean(stopping) : !check.ok;
  button.onclick = stop ? (event) => {
    event.preventDefault();
    stopTurn();
  } : null;
}

function composerChanged() {
  const area = $("chat-text");
  area.style.height = "auto";
  area.style.height = `${Math.min(area.scrollHeight, window.innerHeight * MAX_COMPOSER_SHARE)}px`;
  const check = composerCheck(area.value);
  $("chat-counter").textContent = check.counter;
  $("chat-counter").classList.toggle("over", Boolean(check.reason));
  if (check.reason) showError(check.reason);
  else if ($("chat-error").dataset.kind === "length") showError("");
  $("chat-error").dataset.kind = check.reason ? "length" : "";
  drawSendButton();
}

function showError(text) {
  $("chat-error").textContent = text;
  $("chat-error").dataset.kind = "";
}

/* Links leave the Mini App through Telegram; a code block's Copy button copies its code. */
function clicked(event) {
  const copyButton = event.target.closest(".code-copy");
  if (copyButton) {
    copy(copyButton.closest(".code").querySelector("code").textContent);
    return;
  }
  const link = event.target.closest("a[href]");
  if (!link) return;
  event.preventDefault();
  const href = link.getAttribute("href");
  if (/^https:\/\/t\.me\//i.test(href)) tg.openTelegramLink(href);
  else if (/^https?:/i.test(href)) tg.openLink(href);
}

function copy(text) {
  navigator.clipboard?.writeText(text).then(() => toast(STRINGS.copied), () => {});
}

/* --- sheets, toasts --- */

function openSheet(title, children) {
  const sheet = $("chat-sheet");
  sheet.querySelector(".sheet-card").replaceChildren(el("h3", {}, title), ...children.filter(Boolean));
  sheet.hidden = false;
  sheet.onclick = (event) => event.target === sheet && closeSheet();
  sheetClose = closeSheet;
  tg.BackButton?.show();
}

function closeSheet() {
  $("chat-sheet").hidden = true;
  sheetClose = null;
  tg.BackButton?.hide();
}

function showTimings() {
  const rows = timings.length ? timings.map(([what, ms]) => el("div", { className: "option" }, el("span", {}, what),
    el("span", { className: "sub" }, `${ms} ms`))) : [el("p", { className: "sub" }, "—")];
  openSheet(STRINGS.timings, [el("p", { className: "sheet-text sub" }, STRINGS.timingsHint), ...rows]);
}

function timing(what, ms) {
  if (!Number.isFinite(ms)) return;
  timings.unshift([what, Math.max(0, Math.round(ms))]);
  timings.length = Math.min(timings.length, TIMINGS_KEPT);
}

let toastTimer = null;
function toast(text) {
  const node = $("toast");
  node.textContent = text;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (node.hidden = true), TOAST_FOR);
}

/* --- small things --- */

function scrollToEnd() {
  const list = $("chat-list");
  list.scrollTop = list.scrollHeight;
}

function moment(ts) {
  return ts ? new Date(ts).toLocaleString() : "";
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key in node) node[key] = value;
    else node.setAttribute(key, value);
  }
  node.append(...children.flat().filter((child) => child != null && child !== false && child !== ""));
  return node;
}
