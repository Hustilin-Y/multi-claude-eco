/*
 * The chat tab of the Mini App (the chat add-in): a chat with the person's own assistant, the same one
 * as their Telegram chat. Messages go to the relay (src/chat.js), which queues them for the router;
 * what the page shows comes from the feed the router puts on the relay while the page reads it,
 * long-polled like the control panel's snapshot. The model and effort chips send the control panel's
 * own taps. The conversation's state is in chatfeed.js, the Markdown in markdown.js.
 *
 * The tab bar shows only while the chat add-in is on: the router's Chat button opens the page with
 * ?tab=chat, and the control panel's snapshot lists the add-ins that are on.
 *
 * Other add-ins may bring tabs (an ES module of theirs at addons/<add-in>/tab.js, given mount(root, api)),
 * models (a message to one goes to that add-in, never to the assistant: the composer's target) and
 * actions (a form that fills the composer), all listed in the settings event (chat add-in, X1–X6).
 */
import { STOP_WITHIN, STRINGS, addOlder, apply, busy, composerCheck, enterSends, fileSize, fillTemplate, localMessage,
  modelGroups, newConversation, setState, sourceLabel, statusText, switchOutcome, targetBusy, targetStatusText }
  from "./chatfeed.js";
import { render as renderMarkdown } from "./markdown.js";

const tg = window.Telegram?.WebApp;
const KEEP_KEY = "keep-switches";      // the Keep after restarts switch, shared with the control panel
const TARGET_KEY = "chat-target";      // the model of another add-in messages go to, on this device
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
let target = stored(TARGET_KEY);       // "<add-in>:<id>" while messages go to another add-in's model, else null
let currentTab = "panel";
const addonTabs = new Map();           // tab id -> {tab, button, root, mounted, listeners, shown}
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

/* Chat, Panel, or an add-in's tab. Every tab but the Panel fills the screen and reads the feed. */
function showTab(tab) {
  currentTab = tab;
  $("chat").hidden = tab !== "chat";
  $("panel").hidden = tab !== "panel";
  for (const [id, entry] of addonTabs) entry.root.hidden = id !== tab;
  document.body.classList.toggle("in-chat", tab !== "panel");
  for (const button of document.querySelectorAll("#tabs [data-tab]")) button.setAttribute("aria-selected", String(button.dataset.tab === tab));
  if (addonTabs.has(tab)) mountTab(addonTabs.get(tab));
  if (tab !== "panel") poll();
  if (tab === "chat") scrollToEnd();
}

/* The tabs other add-ins bring, as the settings event lists them, after Chat and Panel. */
function drawTabs() {
  const listed = conversation.settings?.tabs || [];
  for (const tab of listed) {
    if (addonTabs.has(tab.id)) continue;
    const button = el("button", { type: "button", role: "tab", onclick: () => showTab(tab.id) }, tab.title);
    button.dataset.tab = tab.id;
    const root = el("section", { className: "addon-tab", hidden: true });
    $("tabs").append(button);
    $("chat").after(root);
    addonTabs.set(tab.id, { tab, button, root, mounted: false, listeners: [], shown: undefined });
  }
  for (const [id, entry] of addonTabs) {
    if (listed.some((tab) => tab.id === id)) continue;
    entry.button.remove();
    entry.root.remove();
    addonTabs.delete(id);
    if (currentTab === id) showTab("chat");
  }
}

/* An add-in's tab module, loaded from the relay the first time its tab shows: mount(root, api). It
 * gets its add-in's state through api.onState and may fill the composer, never send. */
async function mountTab(entry) {
  if (entry.mounted) return;
  entry.mounted = true;
  const { addon } = entry.tab;
  const api = {
    onState(listener) {
      entry.listeners.push(listener);
      if (conversation.addonStates[addon] !== undefined) listener(conversation.addonStates[addon]);
    },
    fill(text) {
      showTab("chat");
      fillComposer(text);
    },
    toast,
    el,
    markdown: renderMarkdown,
  };
  try {
    const module = await import(new URL(entry.tab.module, location.href).href);
    module.mount(entry.root, api);
  } catch (error) {
    entry.root.replaceChildren(el("p", { className: "chat-refused" }, `This tab couldn't load (${error.message}).`));
  }
}

/* Each add-in's tab hears its state when it changes. */
function tellTabs() {
  for (const entry of addonTabs.values()) {
    const state = conversation.addonStates[entry.tab.addon];
    if (state === undefined || state === entry.shown) continue;
    entry.shown = state;
    for (const listener of entry.listeners) {
      try {
        listener(state);
      } catch (error) {
        console.warn(`${entry.tab.addon}'s tab: ${error.message}`);
      }
    }
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
  $("chip-actions").addEventListener("click", actionsSheet);
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
    while (currentTab !== "panel" && !document.hidden) {
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
        tellTabs();
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
    if (event.kind === "settings" && target && !(event.models || []).some((m) => m.target === target)) {
      setTarget(null);  // its add-in no longer offers it
      toast(STRINGS.targetGone);
    }
  }
  if (stopping && !(target ? targetBusy(conversation, target) : busy(conversation.status))) stopDone();
}

/* --- sending --- */

function submit() {
  const text = $("chat-text").value;
  if (!composerCheck(text).ok) return;
  $("chat-text").value = "";
  composerChanged();
  const message = localMessage(crypto.randomUUID(), text);
  if (target) message.target = target;
  conversation.items.push(message);
  draw();
  scrollToEnd();
  send(message);
}

async function send(message, wait = RETRY_FIRST) {
  setState(conversation, message.client_id, "sending");
  draw();
  try {
    await relay("chat/send", { client_id: message.client_id, text: message.text,
      ...(message.target ? { target: message.target } : {}) });
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
  command("/stop", target);  // with a target, its add-in stops it (chat_stop)
  draw();
}

function stopDone() {
  clearTimeout(stopping.timer);
  stopping = null;
}

function command(text, to = null) {
  relay("chat/send", { client_id: crypto.randomUUID(), text, ...(to ? { target: to } : {}) }).catch((error) => {
    showError(error instanceof Refused && error.error === "reopen" ? STRINGS.reopen : STRINGS.relayOffline);
  });
}

/* New chat: /clear for the assistant, or, with another add-in's model chosen, that model's thread only. */
function confirmNewChat() {
  const to = target;
  openSheet(STRINGS.newChat, [
    el("p", { className: "sheet-text" }, to ? STRINGS.newThread(targetLabel(to)) : STRINGS.newChatAsk),
    el("button", { className: "sheet-button destructive", onclick: () => {
      closeSheet();
      command("/clear", to);
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
  return stored(KEEP_KEY) === "true";
}

function stored(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/* Messages go to model, another add-in's ("<add-in>:<id>"), or to the assistant (null). */
function setTarget(model) {
  target = model;
  try {
    if (model) localStorage.setItem(TARGET_KEY, model);
    else localStorage.removeItem(TARGET_KEY);
  } catch {
    // storage is off: it holds while the page is open
  }
  draw();
}

function targetModel(id) {
  return (conversation.settings?.models || []).find((m) => m.target === id);
}

function targetLabel(id) {
  return targetModel(id)?.label || id.split(":").slice(1).join(":");
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
  const claudeRow = (value) => {
    const reason = setting === "effort" ? refused[value] : null;
    const chosen = value === current && !(setting === "model" && target);
    return el("button", { className: `option${chosen ? " chosen" : ""}`, disabled: Boolean(reason),
      onclick: () => {
        if (setting === "effort" && value === "max") keep = false;
        closeSheet();
        if (setting === "model" && target) setTarget(null);  // back to the assistant
        choose(setting, value, keep);
      } },
    el("span", { className: "option-name" }, value), el("span", { className: "sub" }, reason || hints[value] || ""),
    el("span", { className: "tick" }, chosen ? "✓" : ""));
  };
  const otherRow = (model) => el("button", { className: `option${model.target === target ? " chosen" : ""}`,
    onclick: () => {
      closeSheet();
      setTarget(model.target);
    } },
  el("span", { className: "option-name" }, model.label, model.badge ? el("span", { className: "badge" }, model.badge) : null),
  el("span", { className: "sub" }, model.hint || ""), el("span", { className: "tick" }, model.target === target ? "✓" : ""));
  const rows = setting === "effort" ? options(setting).map(claudeRow)
    : modelGroups(options("model"), conversation.settings?.models).flatMap((group, n) => [
      n || conversation.settings?.models?.length ? el("h4", { className: "sheet-group" }, group.title) : null,
      ...group.models.map((model) => (n ? otherRow(model) : claudeRow(model.id)))]);
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
  drawTabs();
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
  if (item.kind === "model_message") return modelNode(item);
  if (item.kind === "assistant_message") return item.to ? sentToNode(item) : assistantNode(item);
  if (item.kind === "notice") return el("div", { className: "notice-line" }, item.text);
  if (item.kind === "step") return stepNode(item);
  return null;
}

function userNode(item) {
  const label = item.target ? STRINGS.toTarget(targetLabel(item.target)) : sourceLabel(item);
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

/* An answer of another add-in's model: like the assistant's, under the model's name and badge. */
function modelNode(item) {
  const model = targetModel(item.target) || {};
  const body = el("div", { className: "markdown" });
  body.innerHTML = rendered(item) + (item.done ? "" : '<span class="cursor">▍</span>');
  const meta = item.meta || {};
  const facts = [meta.answered, meta.tokens ? `${meta.tokens} tokens` : "", meta.seconds ? `${Math.round(meta.seconds)} s` : "",
    meta.stopped ? STRINGS.stopped : "", meta.error || ""].filter(Boolean).join(" · ");
  return el("div", { className: "msg assistant model", title: moment(item.ts) },
    el("div", { className: "model-head" }, item.model_label || model.label || item.model || "",
      (item.badge || model.badge) ? el("span", { className: "badge" }, item.badge || model.badge) : null),
    body, facts && item.done ? el("div", { className: "msg-foot" }, facts) : null);
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
  const text = stopping ? `${STRINGS.stop}…`
    : target ? targetStatusText(conversation.targets[target]) : statusText(conversation.status);
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
  if (target) $("chip-model").textContent = targetLabel(target);
  $("chip-effort").hidden = Boolean(target);  // another add-in's model has no effort to set
  $("chip-actions").hidden = !(conversation.settings?.actions || []).length;
}

/* --- actions: forms another add-in brings, which fill the composer and send nothing --- */

function actionsSheet() {
  const actions = conversation.settings?.actions || [];
  openSheet(STRINGS.actions, actions.map((action) => el("button", { className: "option", onclick: () => actionForm(action) },
    el("span", { className: "option-name" }, `${action.icon ? `${action.icon} ` : ""}${action.label || action.id}`))));
}

function actionForm(action) {
  const inputs = {};
  const fields = (action.form || []).map((field) => {
    let input;
    if (field.type === "select") {
      input = el("select", {}, ...(field.options || []).map((option) => {
        const [value, label] = typeof option === "object" ? [option.value, option.label] : [option, option];
        return el("option", { value, selected: value === field.default }, label ?? value);
      }));
    } else if (field.type === "textarea") {
      input = el("textarea", { rows: 4, value: field.default || "" });
    } else if (field.type === "switch") {
      input = el("input", { type: "checkbox", checked: Boolean(field.default) });
    } else {
      input = el("input", { type: "text", value: field.default || "" });
    }
    inputs[field.name] = input;
    return el("label", { className: `form-field ${field.type || "text"}` }, el("span", { className: "sub" }, field.label || field.name), input);
  });
  const fill = () => {
    const values = Object.fromEntries(Object.entries(inputs).map(([name, input]) => [name,
      input.type === "checkbox" ? input.checked : input.value]));
    closeSheet();
    fillComposer(fillTemplate(action, values));
  };
  openSheet(`${action.icon ? `${action.icon} ` : ""}${action.label || action.id}`, [...fields,
    action.note ? el("p", { className: "sheet-text sub" }, action.note) : null,
    el("button", { className: "sheet-button", onclick: fill }, STRINGS.fill)]);
}

/* Put text in the composer for the person to read and send. */
function fillComposer(text) {
  $("chat-text").value = text;
  composerChanged();
  $("chat-text").focus();
}

function drawSendButton() {
  const button = $("chat-send");
  const check = composerCheck($("chat-text").value);
  const running = target ? targetBusy(conversation, target) : busy(conversation.status);
  const stop = running && !$("chat-text").value.trim();
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
