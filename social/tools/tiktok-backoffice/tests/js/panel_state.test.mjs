// s28 (hermes-console ADR 062, D2): panelState() of app/tiktok_reply_selectors.py, the passive snapshot of the
// right panel of a TikTok video (comments list, « You may like », closed) read before any trusted click.
//
// The definitions of clean, vis, handle and panelState are read from tiktok_reply_selectors.py as text (each one sits
// on a single line starting with " function <name>("); cssPath spans several lines, so a test cssPath is given (it
// returns the element's `path`). They run against a light fake DOM whose querySelectorAll(sel) returns the elements
// declared for each selector of the (comma separated) list. node:test and node:assert only, no dependency, no
// network. Every text below is invented.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const SELECTORS = readFileSync(fileURLToPath(new URL("../../app/tiktok_reply_selectors.py", import.meta.url)), "utf8");

function definition(text, name) {
  const lines = text.split("\n").filter((l) => l.startsWith(` function ${name}(`));
  assert.equal(lines.length, 1, `one definition of ${name} expected`);
  return lines[0];
}

const TEST_CSS_PATH = "function cssPath(el){return el&&el.path?el.path:null}";
const CODE = [
  ...["clean", "vis", "handle", "panelState"].map((name) => definition(SELECTORS, name)),
  TEST_CSS_PATH,
].join("\n");

const WIDTH = 1920;
const HEIGHT = 987;
const HIDDEN = { x: 0, y: 0, width: 0, height: 0 };
const TAB_COMMENTS = { x: 1530, y: 60, width: 90, height: 32 };
const TAB_LIKE = { x: 1700, y: 60, width: 90, height: 32 };
const PANEL = { x: 1460, y: 120, width: 440, height: 760 };
const ICON = { x: 1090, y: 660, width: 48, height: 48 };

// An element: `path` is its (fake) unique CSS path, `inside` the container selectors it sits in, `parent` its parent.
function el(text = "", { rect = PANEL, inside = [], path = null, parent = null, children = {} } = {}) {
  return {
    innerText: text,
    textContent: text,
    path,
    parentElement: parent,
    getBoundingClientRect: () => rect,
    getAttribute: () => null,
    closest(selector) {
      const parts = selector.split(",").map((s) => s.trim());
      return inside.some((c) => parts.includes(c)) ? {} : null;
    },
    querySelector(selector) {
      return (children[selector] ?? [])[0] ?? null;
    },
  };
}

// An action block (parent) holding a comment icon and its comment count.
function iconWithCount(count, { rect = ICON, path = "ICON" } = {}) {
  const countEl = el(count, { rect: { ...rect, y: rect.y + 50, height: 20 } });
  const parent = el("", { rect, children: { '[data-e2e="comment-count"]': [countEl] } });
  return el("", { rect, path, parent });
}

function stateOn(bySelector) {
  const document = {
    body: { innerText: "" },
    querySelectorAll(selector) {
      const found = new Set();
      for (const part of selector.split(",").map((s) => s.trim())) {
        for (const element of bySelector[part] ?? []) found.add(element);
      }
      return Array.from(found);
    },
  };
  const window = { innerWidth: WIDTH, innerHeight: HEIGHT };
  // eslint-disable-next-line no-new-func
  return new Function("document", "window", `${CODE}\nreturn panelState();`)(document, window);
}

const TABBAR = ".tux-tabbar__item-container";
const LIST = '[class*="DivCommentListContainer"]';
const INPUT = '[class*="DivCommentInputWrapper"]';
const RELATED = '[class*="DivRelatedTabContainer"]';
const ICON_SEL = '[data-e2e="comment-icon"]';

function tabs() {
  return [
    el("Comments", { rect: TAB_COMMENTS, inside: [TABBAR], path: "TAB_COMMENTS" }),
    el("You may like", { rect: TAB_LIKE, inside: [TABBAR], path: "TAB_LIKE" }),
  ];
}

test("closed panel (icon in the viewport, no tabs) is closed with the icon", () => {
  const s = stateOn({ [ICON_SEL]: [iconWithCount("3")] });
  assert.equal(s.state, "closed");
  assert.equal(s.comments_tab, null);
  assert.deepEqual(s.comment_icon, { selector: "ICON", x: 1114, y: 684 });
  assert.equal(s.comment_count, 3);
});

test("« You may like » active (tabs + related container) is related with the Comments tab", () => {
  const s = stateOn({ button: tabs(), [RELATED]: [el("reco")], [ICON_SEL]: [iconWithCount("3")] });
  assert.equal(s.state, "related");
  assert.deepEqual(s.comments_tab, { selector: "TAB_COMMENTS", x: 1575, y: 76 });
});

test("a « You may like » tab without comment list nor related container is related", () => {
  const s = stateOn({ button: tabs() });
  assert.equal(s.state, "related");
  assert.equal(s.comments_tab.selector, "TAB_COMMENTS");
});

test("open panel (tabs + list + items) is comments", () => {
  const s = stateOn({
    button: tabs(),
    [LIST]: [el("Invented one Invented two")],
    '[class*="DivCommentItemWrapper"]': [el("Invented one"), el("Invented two")],
    [ICON_SEL]: [iconWithCount("2")],
  });
  assert.equal(s.state, "comments");
  assert.equal(s.comment_count, 2);
});

test("comments wins over a related container still in the DOM", () => {
  const s = stateOn({ button: tabs(), [LIST]: [el("x")], [RELATED]: [el("reco")] });
  assert.equal(s.state, "comments");
});

test("video without comment (tabs + empty list + input) is comments", () => {
  const s = stateOn({ button: tabs(), [LIST]: [el("")], [INPUT]: [el("Add comment...")] });
  assert.equal(s.state, "comments");
});

test("a Comments tab alone, without list nor reco, is unknown with the tab", () => {
  const s = stateOn({ button: [el("Comments", { rect: TAB_COMMENTS, inside: [TABBAR], path: "TAB_COMMENTS" })] });
  assert.equal(s.state, "unknown");
  assert.equal(s.comments_tab.selector, "TAB_COMMENTS");
});

test("a Comments button outside any tab bar is taken only in the right top area", () => {
  const left = el("Comments", { rect: { x: 200, y: 60, width: 90, height: 32 }, path: "LEFT" });
  assert.equal(stateOn({ button: [left] }).comments_tab, null);
  const right = el("Comments", { rect: TAB_COMMENTS, path: "RIGHT" });
  assert.equal(stateOn({ button: [left, right] }).comments_tab.selector, "RIGHT");
});

test("two icons: the one below the viewport (y=1672) is never chosen", () => {
  const below = iconWithCount("99", { rect: { ...ICON, y: 1672 }, path: "ICON_NEXT" });
  const current = iconWithCount("3");
  assert.equal(stateOn({ [ICON_SEL]: [below, current] }).comment_icon.selector, "ICON");
  const s = stateOn({ [ICON_SEL]: [below] });
  assert.equal(s.comment_icon, null);
  assert.equal(s.comment_count, null);
});

test("the count is read in the action block of the chosen icon, not another one", () => {
  const below = iconWithCount("99", { rect: { ...ICON, y: 1672 }, path: "ICON_NEXT" });
  const s = stateOn({ [ICON_SEL]: [below, iconWithCount("7")] });
  assert.equal(s.comment_count, 7);
});

test("comment_count reads the K and M suffixes", () => {
  assert.equal(stateOn({ [ICON_SEL]: [iconWithCount("1.2K")] }).comment_count, 1200);
  assert.equal(stateOn({ [ICON_SEL]: [iconWithCount("3M")] }).comment_count, 3000000);
  assert.equal(stateOn({ [ICON_SEL]: [iconWithCount("abc")] }).comment_count, null);
});

test("an invisible tab or icon is ignored", () => {
  const s = stateOn({
    button: [el("Comments", { rect: HIDDEN, inside: [TABBAR], path: "TAB_COMMENTS" })],
    [ICON_SEL]: [iconWithCount("3", { rect: HIDDEN })],
  });
  assert.equal(s.state, "closed");
  assert.equal(s.comments_tab, null);
  assert.equal(s.comment_icon, null);
});

test("the result carries no comment text, only the four keys", () => {
  const s = stateOn({
    button: tabs(),
    [LIST]: [el("Invented secret words")],
    '[class*="DivCommentItemWrapper"]': [el("Invented secret words")],
    [ICON_SEL]: [iconWithCount("1")],
  });
  assert.deepEqual(Object.keys(s).sort(), ["comment_count", "comment_icon", "comments_tab", "state"]);
  assert.ok(!JSON.stringify(s).includes("Invented"));
});
