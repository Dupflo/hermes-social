// s25 (s24 m2, hermes-console ADR 059, P6): loginGate() of app/tiktok_reply_selectors.py, isolated.
//
// The definitions of clean, vis and loginGate are read from the Python module as text (each one sits on a single
// line starting with " function <name>(" in _HELPERS, pinned by tests/test_tiktok_reply_selectors_js.py) and run
// against a light fake DOM. node:test and node:assert only, no dependency, no network.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const SOURCE = readFileSync(
  fileURLToPath(new URL("../../app/tiktok_reply_selectors.py", import.meta.url)),
  "utf8",
);

function definition(name) {
  const line = SOURCE.split("\n").find((l) => l.startsWith(` function ${name}(`));
  assert.ok(line, `definition of ${name} not found`);
  return line;
}

const CODE = ["clean", "vis", "loginGate"].map(definition).join("\n");

const SHOWN = { x: 10, y: 10, width: 80, height: 24 };
const HIDDEN = { x: 0, y: 0, width: 0, height: 0 };

// A control (a, button, [role=button]); `inside` lists the container selectors it sits in (comment, DM bubble…).
function control(text, { inside = [], rect = SHOWN } = {}) {
  return {
    innerText: text,
    textContent: text,
    getBoundingClientRect: () => rect,
    closest(selector) {
      const parts = selector.split(",").map((s) => s.trim());
      return inside.some((c) => parts.includes(c)) ? {} : null;
    },
  };
}

function loginGateOn(controls, bodyText) {
  const document = {
    body: { innerText: bodyText ?? controls.map((c) => c.innerText).join("\n") },
    querySelectorAll(selector) {
      assert.match(selector, /\ba\b/);
      assert.match(selector, /button/);
      return controls;
    },
  };
  const getComputedStyle = () => ({ visibility: "visible", display: "block" });
  // eslint-disable-next-line no-new-func
  return new Function("document", "getComputedStyle", `${CODE}\nreturn loginGate();`)(document, getComputedStyle);
}

const COMMENT = '[class*="DivCommentItemWrapper"]';
const CHAT = '[data-e2e="chat-item"]';

test("header « Log in » button is a login gate", () => {
  assert.equal(loginGateOn([control("For You"), control("Log in")]), true);
});

test("« Sign up » button of a login modal is a login gate", () => {
  assert.equal(loginGateOn([control("  Sign   up ")]), true);
});

test("« Log in to comment » bar outside any comment is a login gate", () => {
  assert.equal(loginGateOn([control("Log in to comment")]), true);
});

test("a comment saying « Log in to see » or « Sign up here » is not a login gate", () => {
  const controls = [control("Reply", { inside: [COMMENT] }), control("Log in", { inside: [COMMENT] })];
  assert.equal(loginGateOn(controls, "Log in to see\nSign up here\nReply"), false);
});

test("a DM bubble « Sign up » is not a login gate", () => {
  assert.equal(loginGateOn([control("Sign up", { inside: [CHAT] })], "Sign up"), false);
});

test("a link whose text is not exact (« Log in here please ») is not a login gate", () => {
  assert.equal(loginGateOn([control("Log in here please")]), false);
});

test("an invisible control is not a login gate", () => {
  assert.equal(loginGateOn([control("Log in", { rect: HIDDEN })]), false);
});

test("a page without any control is not a login gate", () => {
  assert.equal(loginGateOn([], "Log in to comment"), false);
});
