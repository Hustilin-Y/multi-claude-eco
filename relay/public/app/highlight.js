/*
 * A small syntax highlighter for the chat's code blocks: comments, strings, numbers and keywords of
 * the languages assistants write most, as <span class="hl-…">. Anything else is shown plain. It
 * returns HTML with every character of the code escaped, so its output is safe to put in the page.
 */

const C_LIKE = "break case catch class const continue default do else enum export extends false final finally for "
  + "function if import in instanceof interface let new null private protected public return static super switch "
  + "this throw true try typeof var void while yield async await of struct impl fn mut pub use mod match loop "
  + "package go defer chan map range select type func nil self Self trait where as int char float double bool "
  + "string long short unsigned namespace template typename using virtual override sealed readonly undefined";
const PYTHON = "and as assert async await break class continue def del elif else except False finally for from global "
  + "if import in is lambda None nonlocal not or pass raise return True try while with yield self print match case";
const SHELL = "if then else elif fi for while until do done case esac in function return local export readonly set "
  + "unset echo printf cd exit source sudo true false";
const SQL = "select from where insert into values update set delete create table drop alter join left right inner "
  + "outer on group by order having limit as and or not null is in like distinct union all primary key index";
const RUBY = "def end class module if elsif else unless while until do return yield nil true false self require "
  + "begin rescue ensure then case when";

/* Language name (as a fence says it) -> {words, hash: # starts a comment, slash: // and /* do, dash: -- does}. */
const LANGUAGES = {};
for (const name of ["js", "javascript", "jsx", "ts", "typescript", "tsx", "mjs", "cjs", "java", "c", "h", "cpp", "c++",
  "cc", "cs", "csharp", "go", "rust", "rs", "kotlin", "kt", "swift", "scala", "dart", "php"]) {
  LANGUAGES[name] = { words: new Set(C_LIKE.split(" ")), slash: true, hash: name === "php" };
}
for (const name of ["py", "python", "python3"]) LANGUAGES[name] = { words: new Set(PYTHON.split(" ")), hash: true };
for (const name of ["sh", "bash", "shell", "zsh", "console", "shellscript"]) LANGUAGES[name] = { words: new Set(SHELL.split(" ")), hash: true };
for (const name of ["rb", "ruby"]) LANGUAGES[name] = { words: new Set(RUBY.split(" ")), hash: true };
for (const name of ["yaml", "yml", "toml", "ini", "dockerfile", "make", "makefile"]) LANGUAGES[name] = { words: new Set(), hash: true };
LANGUAGES.sql = { words: new Set(SQL.split(" ")), dash: true, caseless: true };
LANGUAGES.json = { words: new Set(["true", "false", "null"]) };

const cache = new Map();

/* The tokenizer for a language: one regex whose groups are comment, string, number and word. */
function tokenizer(language) {
  const key = JSON.stringify([language.hash, language.slash, language.dash]);
  if (!cache.has(key)) {
    const comments = [language.slash && "\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?(?:\\*\\/|$)", language.hash && "#[^\\n]*",
      language.dash && "--[^\\n]*"].filter(Boolean);
    const parts = [comments.length ? `(${comments.join("|")})` : "($^)",
      "(\"(?:\\\\.|[^\"\\\\\\n])*\"?|'(?:\\\\.|[^'\\\\\\n])*'?|`(?:\\\\.|[^`\\\\])*`?)",
      "(\\b\\d[\\d_]*(?:\\.\\d+)?(?:[eE][+-]?\\d+)?\\b|\\b0x[\\da-fA-F]+\\b)",
      "([A-Za-z_$][\\w$]*)"];
    cache.set(key, new RegExp(parts.join("|"), "g"));
  }
  return cache.get(key);
}

export function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/* code as HTML, highlighted when lang is a language it knows. */
export function highlight(code, lang) {
  const language = LANGUAGES[String(lang || "").trim().toLowerCase()];
  if (!language) return escapeHtml(code);
  const pattern = tokenizer(language);
  pattern.lastIndex = 0;
  let html = "";
  let at = 0;
  for (const match of code.matchAll(pattern)) {
    const [text, comment, string, number, word] = match;
    if (!text) continue;
    html += escapeHtml(code.slice(at, match.index));
    at = match.index + text.length;
    const kind = comment ? "c" : string ? "s" : number ? "n"
      : word && language.words.has(language.caseless ? word.toLowerCase() : word) ? "k" : "";
    html += kind ? `<span class="hl-${kind}">${escapeHtml(text)}</span>` : escapeHtml(text);
  }
  return html + escapeHtml(code.slice(at));
}
