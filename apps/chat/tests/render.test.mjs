// The page renders HTML by string concatenation, which is fine until a value
// arrives from somewhere other than this file. Source titles, URLs and
// excerpts come from a database column written by a retrieval service reading
// a 127GB archive, and message text is whatever someone typed.
//
// Both bugs this guards against were real, and neither showed up as a broken
// page: a title containing a double quote added an onmouseover handler to the
// citation link, and a javascript: URL in the sources column became a live
// link. Syntax checking the script would not have caught either.
//
// The script is evaluated as the page ships it rather than as a copy, so the
// test cannot drift from what is actually served.
import { readFileSync } from "node:fs";
import { strict as assert } from "node:assert";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.join(here, "..", "app", "static", "index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

// Enough DOM for the page to finish loading. The functions under test are
// pure; these stubs exist so the handler bindings at the bottom of the
// script do not throw before we reach them.
const stub = {
  addEventListener() {}, focus() {}, setAttribute() {}, querySelector: () => null,
  set innerHTML(_v) {}, value: "", checked: false,
};
const globals = {
  document: {
    getElementById: () => stub,
    querySelector: () => null,
    addEventListener() {},
    body: { classList: { toggle() {}, contains: () => false } },
  },
  fetch: async () => ({ ok: true, json: async () => [] }),
  confirm: () => false,
  setTimeout: () => 0,
  clearTimeout: () => {},
};

const exported = new Function(
  ...Object.keys(globals),
  script + "\nreturn { esc, render, panel, safeUrl };",
)(...Object.values(globals));

const { esc, render, panel, safeUrl } = exported;

const SOURCE = {
  n: 1,
  title: "Ada Lovelace",
  archive: "wikipedia_en_simple_all_nopic_2026-05",
  url: "https://wikipedia.home/content/wikipedia_en_simple_all_nopic_2026-05/Ada_Lovelace",
  excerpt: "She is known as the first computer programmer.",
};

// --- escaping --------------------------------------------------------------
assert.equal(esc('<b>'), "&lt;b&gt;", "tags must not survive");
assert.equal(
  esc('a" x'),
  "a&quot; x",
  "quotes must be escaped: these values go into attributes, not just text",
);

// --- citation markers ------------------------------------------------------
const cited = render("wrote it [1]", [SOURCE]);
assert.match(cited, /<a class="cite" href="https:\/\/wikipedia\.home\//, "[1] should link to the article");

assert.equal(
  render("a claim [1]", null),
  "a claim [1]",
  "with no sources the marker stays plain text",
);
assert.match(
  render("a claim [7]", [SOURCE]),
  /a claim \[7\]/,
  "a number with no matching source must not get an invented destination",
);

// --- markup cannot be injected --------------------------------------------
assert.ok(
  !render("<img src=x onerror=alert(1)>", [SOURCE]).includes("<img"),
  "message text must not become markup",
);
const hostileTitle = render("claim [1]", [{ ...SOURCE, title: 'Ada" onmouseover="alert(1)' }]);
assert.ok(
  !hostileTitle.includes('onmouseover="'),
  "a source title must not be able to add an attribute to the link",
);
// The escaped form still contains the characters "onmouseover=", so checking
// for that substring alone would pass on the safe output and the unsafe one
// alike. What matters is that the whole hostile string stayed inside the
// attribute it was written into.
assert.equal(
  hostileTitle.match(/title="([^"]*)"/)[1],
  "Ada&quot; onmouseover=&quot;alert(1)",
);

// --- only http(s) destinations --------------------------------------------
assert.equal(safeUrl("javascript:alert(1)"), "", "javascript: URLs are not links");
assert.equal(safeUrl(null), "", "a missing URL is not a link");
assert.equal(safeUrl(SOURCE.url), SOURCE.url);
assert.ok(
  !panel([{ ...SOURCE, url: "javascript:alert(1)" }]).includes("<a "),
  "a javascript: URL in the sources column must not become a live link",
);
assert.ok(
  panel([{ ...SOURCE, url: null }]).includes("Ada Lovelace"),
  "a source with no usable link is still listed - it informed the answer",
);

// --- the panel -------------------------------------------------------------
assert.equal(panel([]), "", "no sources means no panel at all");
assert.match(panel([SOURCE]), /1 source from the local Wikipedia/);
assert.match(panel([SOURCE, { ...SOURCE, n: 2 }]), /2 sources from the local Wikipedia/);
assert.match(panel([SOURCE]), /Simple English/, "the archive that answered is worth showing");
assert.match(
  panel([{ ...SOURCE, archive: "wikipedia_en_all_maxi_2026-08" }]),
  /Full English/,
);

console.log("render checks passed");
