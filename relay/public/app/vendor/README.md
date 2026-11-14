# Vendored libraries for the chat tab

Copied unchanged from npm (`npm pack`), except that the `//# sourceMappingURL=` line is left out, since
the source maps aren't served. The page loads them from here, never from a CDN at runtime.

| File | Package | License |
| --- | --- | --- |
| `marked.esm.js` | marked 15.0.12, `lib/marked.esm.js` | MIT (`marked-LICENSE.md`) |
| `purify.es.mjs` | dompurify 3.2.6, `dist/purify.es.mjs` | Apache 2.0 or MPL 2.0 (`dompurify-LICENSE`) |

To update, `npm pack marked@<version> dompurify@<version>`, copy the same files and drop the source map line.
