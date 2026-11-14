/*
 * The assistant's Markdown as the chat shows it: GitHub-flavored (marked, vendored), code blocks
 * highlighted with a Copy button, and every bit of HTML put through DOMPurify (vendored) before it
 * reaches the page. Raw HTML in the Markdown is shown as text, except <details> and <summary>, which
 * make the collapsible sections Telegram shows too; pictures become links, so nothing loads from
 * elsewhere by itself.
 */
import { Marked } from "./vendor/marked.esm.js";
import DOMPurify from "./vendor/purify.es.mjs";
import { escapeHtml, highlight } from "./highlight.js";

const ALLOWED_RAW = /^<\/?(details|summary)( open)?>$/i;
const RAW_TAG = /<\/?[a-z][^>]*>/gi;

const marked = new Marked({ gfm: true, breaks: true });
marked.use({
  renderer: {
    code({ text, lang }) {
      const language = String(lang || "").trim().split(/\s/)[0];
      const label = language ? `<span class="code-lang">${escapeHtml(language)}</span>` : "<span></span>";
      return `<div class="code">${`<div class="code-head">${label}<button type="button" class="code-copy">Copy</button></div>`}`
        + `<pre><code>${highlight(text.replace(/\n$/, ""), language)}</code></pre></div>`;
    },
    html({ text }) {
      return rawHtml(text);
    },
    image({ href, text }) {
      return `<a href="${escapeHtml(href || "")}">${escapeHtml(text || href || "picture")}</a>`;
    },
  },
});

/* Raw HTML from the Markdown as text, but for the tags of a collapsible section. */
export function rawHtml(text) {
  let out = "";
  let at = 0;
  for (const match of text.matchAll(RAW_TAG)) {
    out += escapeHtml(text.slice(at, match.index));
    out += ALLOWED_RAW.test(match[0]) ? match[0].toLowerCase() : escapeHtml(match[0]);
    at = match.index + match[0].length;
  }
  return out + escapeHtml(text.slice(at));
}

/* Safe HTML for markdown. */
export function render(markdown) {
  const html = marked.parse(String(markdown || ""));
  return DOMPurify.sanitize(html, {
    ADD_TAGS: ["details", "summary"],
    FORBID_TAGS: ["img", "style", "form", "input", "textarea", "select", "iframe", "video", "audio", "svg", "math"],
    FORBID_ATTR: ["style", "id", "name"],
  });
}
