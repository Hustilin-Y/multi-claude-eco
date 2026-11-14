// The chat page's state (public/app/chatfeed.js), its highlighter and its Markdown's raw HTML, and the
// page's weight. IDs in brackets are the acceptance criteria of docs/chat-miniapp/ACCEPTANCE.md.
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { gzipSync } from "node:zlib";

import { STRINGS, addOlder, apply, composerCheck, enterSends, hasGap, localMessage, newConversation, setState, statusText,
  switchOutcome } from "../public/app/chatfeed.js";
import { highlight } from "../public/app/highlight.js";

const APP = new URL("../public/app/", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

function message(seq, key = `a:${seq}`, extra = {}) {
  return { seq, kind: "assistant_message", key, ts: `2026-11-14T10:00:${String(seq % 60).padStart(2, "0")}.000Z`,
    markdown: `m${seq}`, ...extra };
}

test("a first read takes whatever seq comes, even after a clear far along", () => {
  // [AC-12.5]
  const conversation = newConversation();
  const gap = apply(conversation, [message(1043), message(1044)], true);
  assert.equal(gap, false);
  assert.equal(conversation.lastSeq, 1044);
});

test("a jump in seq is a gap, and the re-read shows nothing twice", () => {
  // [AC-12.4]
  const conversation = newConversation();
  apply(conversation, [message(99), message(100)], true);
  assert.equal(apply(conversation, [message(105)]), true);
  apply(conversation, [message(100), message(101), message(102), message(103), message(104), message(105)], true);
  assert.deepEqual(conversation.items.map((i) => i.key), ["a:99", "a:100", "a:101", "a:102", "a:103", "a:104", "a:105"]);
  assert.equal(hasGap(0, [message(7)]), false);
  assert.equal(hasGap(7, [message(8), message(10)]), true);
});

test("the page's own message turns into the router's as it goes, never back", () => {
  const conversation = newConversation();
  const mine = localMessage("cid-1", "hello", Date.parse("2026-11-14T10:00:30Z"));
  conversation.items.push(mine);
  setState(conversation, "cid-1", "queued");
  apply(conversation, [{ seq: 5, kind: "user_message", key: "u:812", client_id: "cid-1", text: "hello", state: "delivered",
    ts: "2026-11-14T10:00:30.100Z", source: "miniapp" }], true);
  apply(conversation, [{ seq: 6, kind: "user_message", key: "u:812", client_id: "cid-1", text: "hello", state: "queued",
    ts: "2026-11-14T10:00:30.100Z", source: "miniapp" }]);
  assert.deepEqual(conversation.items.map((i) => [i.key, i.state, i.local]), [["u:812", "delivered", false]]);
});

test("an edit changes its message in place, and a cleared event empties the conversation", () => {
  const conversation = newConversation();
  apply(conversation, [message(1), message(2)], true);
  apply(conversation, [{ seq: 3, kind: "message_edited", key: "a:1", markdown: "changed" }]);
  assert.equal(conversation.items[0].markdown, "changed");
  apply(conversation, [{ seq: 4, kind: "cleared" }]);
  assert.deepEqual(conversation.items, []);
});

test("older history goes before what's shown, once", () => {
  const conversation = newConversation();
  apply(conversation, [message(10), { seq: 11, kind: "history", cursor: { end: 5 } }], true);
  addOlder(conversation, [message(8), message(9), message(10)], null);
  assert.deepEqual(conversation.items.map((i) => i.key), ["a:8", "a:9", "a:10"]);
  assert.equal(conversation.cursor, null);
});

test("the composer counts from 14,000 and refuses more than 16,000 or nothing", () => {
  // [AC-2.6, AC-2.7]
  assert.deepEqual(composerCheck("x".repeat(13999)), { ok: true, counter: "", reason: "" });
  assert.equal(composerCheck("x".repeat(14000)).counter, "14,000 / 16,000");
  const over = composerCheck("x".repeat(16001));
  assert.equal(over.ok, false);
  assert.equal(over.counter, "16,001 / 16,000");
  assert.equal(composerCheck("  \n ").ok, false);
});

test("the status line says what runs and for how long", () => {
  const since = "2026-11-14T10:00:00.000Z";
  const at = Date.parse(since) + 12_000;
  assert.equal(statusText({ state: "thinking", since }, at), "Thinking… 12s");
  assert.equal(statusText({ state: "tool", tool: "Bash", since }, at), "Running: Bash 12s");
  assert.equal(statusText({ state: "waking", since }, at + 60_000), "Waking up… 1m 12s");
  assert.equal(statusText({ state: "idle", since }, at), "");
});

test("Enter sends on a desktop Telegram and adds a line on a phone", () => {
  // [AC-2.8]
  assert.equal(enterSends("tdesktop"), true);
  assert.equal(enterSends("ios"), false);
  assert.equal(enterSends("android"), false);
});

test("a router notice tells a switch that failed", () => {
  assert.deepEqual(switchOutcome("⚠️ Couldn't switch effort: max isn't available"),
    { setting: "effort", failed: true, text: "Couldn't switch effort: max isn't available" });
  assert.equal(switchOutcome("✅ Model: Sonnet (this session only)").failed, false);
  assert.equal(switchOutcome("Hello"), null);
});

test("code is highlighted, and never let out as HTML", () => {
  assert.equal(highlight("<b>", "nosuchlanguage"), "&lt;b&gt;");
  const python = highlight("def f():\n    return '<x>'  # done", "python");
  assert.match(python, /<span class="hl-k">def<\/span>/);
  assert.match(python, /<span class="hl-s">'&lt;x&gt;'<\/span>/);
  assert.match(python, /<span class="hl-c"># done<\/span>/);
  assert.doesNotMatch(python, /<x>/);
});

test("raw HTML in an answer is shown as text, but for a collapsible section", async () => {
  // [AC-2.9, AC-2.10] The page's DOMPurify then removes anything that could still run.
  const { rawHtml } = await import("../public/app/markdown.js");
  assert.equal(rawHtml("<details><summary>More</summary>"), "<details><summary>More</summary>");
  assert.equal(rawHtml("<img src=x onerror=alert(1)>"), "&lt;img src=x onerror=alert(1)&gt;");
  assert.equal(rawHtml("<script>alert(1)</script>"), "&lt;script&gt;alert(1)&lt;/script&gt;");
});

test("every string the chat shows is in one table", () => {
  assert.equal(typeof STRINGS.stranger, "string");
  assert.equal(STRINGS.reopen, "For safety, reopen the chat to keep sending.");
});

test("the page weighs under 300 KB gzipped, Telegram's script aside", () => {
  // [AC-N2]
  const files = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) walk(path);
      else if (name !== "telegram-web-app.js" && /\.(js|mjs|css|html)$/.test(name)) files.push(path);
    }
  };
  walk(APP);
  const weight = files.reduce((sum, path) => sum + gzipSync(readFileSync(path)).length, 0);
  assert.ok(weight < 300 * 1024, `${weight} bytes`);
});
