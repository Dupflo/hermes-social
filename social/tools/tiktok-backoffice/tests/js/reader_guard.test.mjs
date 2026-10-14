// s26 (hermes-console ADR 060, D1/P1): readerGuard() of app/camofox_reader.py, the structural guard of the comment
// reader, on top of loginGate() of app/tiktok_reply_selectors.py.
//
// The definitions of clean, vis and loginGate are read from tiktok_reply_selectors.py, and readerGuard from
// camofox_reader.py, as text (each one sits on a single line starting with " function <name>(", pinned by
// tests/test_tiktok_reply_selectors_js.py) and run against a light fake DOM whose querySelectorAll(sel) returns the
// elements declared for each selector of the (comma separated) list. node:test and node:assert only, no dependency,
// no network.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

function source(path) {
  return readFileSync(fileURLToPath(new URL(path, import.meta.url)), "utf8");
}

const SELECTORS = source("../../app/tiktok_reply_selectors.py");
const READER = source("../../app/camofox_reader.py");

function definition(text, name) {
  const lines = text.split("\n").filter((l) => l.startsWith(` function ${name}(`));
  assert.equal(lines.length, 1, `one definition of ${name} expected`);
  return lines[0];
}

const CODE = [
  ...["clean", "vis", "loginGate"].map((name) => definition(SELECTORS, name)),
  definition(READER, "readerGuard"),
].join("\n");

const SHOWN = { x: 900, y: 300, width: 300, height: 40 };
const HIDDEN = { x: 0, y: 0, width: 0, height: 0 };

// An element; `inside` lists the container selectors it sits in (header, comment…), `aria` its aria-label.
function el(text = "", { inside = [], rect = SHOWN, aria = null } = {}) {
  return {
    innerText: text,
    textContent: text,
    getBoundingClientRect: () => rect,
    getAttribute: (name) => (name === "aria-label" ? aria : null),
    closest(selector) {
      const parts = selector.split(",").map((s) => s.trim());
      return inside.some((c) => parts.includes(c)) ? {} : null;
    },
  };
}

// `bySelector` maps one simple selector to its elements; a comma list returns the union, without duplicates.
function guardOn(bySelector) {
  const document = {
    body: { innerText: "Messages Log in Sign up" },
    querySelectorAll(selector) {
      const found = new Set();
      for (const part of selector.split(",").map((s) => s.trim())) {
        for (const element of bySelector[part] ?? []) found.add(element);
      }
      return Array.from(found);
    },
  };
  // eslint-disable-next-line no-new-func
  return new Function("document", `${CODE}\nreturn readerGuard();`)(document);
}

const ITEM = '[class*="DivCommentItemWrapper"]';
const LEVEL = '[data-e2e*="comment-level"]';
const PANEL = '[class*="DivCommentListContainer"]';
const ICON = '[data-e2e="comment-icon"]';

test("partially loaded page (nothing visible) is no_signal", () => {
  const g = guardOn({ [ITEM]: [el("hidden", { rect: HIDDEN })], [ICON]: [el("12", { rect: HIDDEN })] });
  assert.deepEqual(g, { state: "no_signal", login_gate: false, items: 0, panel: false, icon: false });
});

test("the comment icon alone is no_signal: it is not a logged-in signal", () => {
  const g = guardOn({ [ICON]: [el("12")] });
  assert.equal(g.state, "no_signal");
  assert.equal(g.icon, true);
});

test("complete logged-in page (icon, panel, 3 comments) is ok", () => {
  const g = guardOn({ [ICON]: [el("3")], [PANEL]: [el("3 comments")], [ITEM]: [el("a"), el("b"), el("c")] });
  assert.deepEqual(g, { state: "ok", login_gate: false, items: 3, panel: true, icon: true });
});

test("an open panel without comment (video without comment) is ok", () => {
  const g = guardOn({ [PANEL]: [el("0 comments")] });
  assert.equal(g.state, "ok");
  assert.equal(g.items, 0);
});

test("a comment-level-1 element alone is ok", () => {
  const g = guardOn({ [LEVEL]: [el("hello")] });
  assert.equal(g.state, "ok");
  assert.equal(g.items, 1);
});

test("an element under #app-header is not counted", () => {
  const g = guardOn({ [ITEM]: [el("notif", { inside: ["#app-header"] })] });
  assert.equal(g.items, 0);
  assert.equal(g.state, "no_signal");
});

test("real logged-out page (icon, panel, header « Log in » button) is login_gate", () => {
  const g = guardOn({ [ICON]: [el("3")], [PANEL]: [el("3 comments")], button: [el("Log in")] });
  assert.equal(g.state, "login_gate");
  assert.equal(g.login_gate, true);
});

test("a comment « Log in to see » with a « Log in » control inside it, plus comments, is ok", () => {
  const comment = el("Log in to see");
  const g = guardOn({
    [ITEM]: [comment, el("other")],
    button: [el("Log in", { inside: [ITEM] })],
  });
  assert.equal(g.state, "ok");
  assert.equal(g.login_gate, false);
  assert.equal(g.items, 2);
});

test("an invisible element is not counted", () => {
  const g = guardOn({ [ITEM]: [el("a", { rect: HIDDEN })], [PANEL]: [el("p", { rect: HIDDEN })] });
  assert.equal(g.items, 0);
  assert.equal(g.panel, false);
  assert.equal(g.state, "no_signal");
});

test("aria-label « Read or add comments » fallback sets icon", () => {
  const g = guardOn({ "[aria-label]": [el("", { aria: "Read or add comments" })] });
  assert.equal(g.icon, true);
  assert.equal(g.state, "no_signal");
});
