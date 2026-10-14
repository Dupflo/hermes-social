// s30 (hermes-console ADR 064, D2-D4, A1, A2, A6, A8): the DM path helpers of app/tiktok_reply_selectors.py — the
// profile « Message » button (never the navigation link to the inbox), the DM editor of the conversation (never the
// comment field), the structural « DM refused » wording, the shape of an element for the `dm_probe` diagnostic, the
// text of the conversation column and the send button.
//
// The definitions are read from tiktok_reply_selectors.py as text (one line each, starting with " function <name>(";
// cssPath spans several lines, so a test cssPath returns the element's `path`). They run against a small fake DOM: a
// tree of elements with a minimal selector matcher (comma lists of simple compounds: tag, #id, .class, [attr],
// [attr="v"], [attr*="v"], [attr^="v"], optional ` i` flag). node:test and node:assert only, no dependency, no network.
// Every text, handle and class below is invented.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import vm from "node:vm";

const ROOT = fileURLToPath(new URL("../../", import.meta.url));
const SELECTORS = readFileSync(fileURLToPath(new URL("../../app/tiktok_reply_selectors.py", import.meta.url)), "utf8");

function definition(text, name) {
  const lines = text.split("\n").filter((l) => l.startsWith(` function ${name}(`));
  assert.equal(lines.length, 1, `one definition of ${name} expected`);
  return lines[0];
}

const TEST_CSS_PATH = "function cssPath(el){return el&&el.path?el.path:null}";
const NAMES = ["clean", "vis", "handle", "editors", "shapeOf", "messageButton", "dmBlock", "dmEditor", "convColumn",
  "convText", "sendButton"];
const CODE = [...NAMES.map((name) => definition(SELECTORS, name)), TEST_CSS_PATH].join("\n");

const WIDTH = 1920;
const HEIGHT = 987;
const SHOWN = { x: 600, y: 200, width: 120, height: 36 };
const HIDDEN = { x: 0, y: 0, width: 0, height: 0 };

// --- fake DOM -------------------------------------------------------------------------------------------------------

function parseCompound(compound) {
  const m = compound.match(/^([a-zA-Z][a-zA-Z0-9]*)?(#[\w-]+)?((?:\.[\w-]+)*)((?:\[[^\]]+\])*)$/);
  if (!m) throw new Error(`unsupported selector: ${compound}`);
  const attrs = [];
  for (const a of (m[4] || "").matchAll(/\[([\w-]+)(?:([*^]?=)"([^"]*)"(\s+i)?)?\]/g)) {
    attrs.push({ name: a[1], op: a[2] || null, value: a[3], ci: Boolean(a[4]) });
  }
  return {
    tag: m[1] ? m[1].toUpperCase() : null,
    id: m[2] ? m[2].slice(1) : null,
    classes: (m[3] || "").split(".").filter(Boolean),
    attrs,
  };
}

function matchesCompound(el, c) {
  if (c.tag && el.tagName !== c.tag) return false;
  if (c.id && el.id !== c.id) return false;
  const classes = String(el.className || "").split(/\s+/).filter(Boolean);
  if (c.classes.some((k) => !classes.includes(k))) return false;
  for (const a of c.attrs) {
    let v = el.getAttribute(a.name);
    if (v === null) return false;
    if (!a.op) continue;
    let want = a.value;
    if (a.ci) {
      v = v.toLowerCase();
      want = want.toLowerCase();
    }
    if (a.op === "=" && v !== want) return false;
    if (a.op === "*=" && !v.includes(want)) return false;
    if (a.op === "^=" && !v.startsWith(want)) return false;
  }
  return true;
}

function matches(el, selector) {
  return selector.split(",").map((s) => s.trim()).filter(Boolean).some((s) => matchesCompound(el, parseCompound(s)));
}

function descendants(el) {
  const out = [];
  for (const c of el.children) out.push(c, ...descendants(c));
  return out;
}

// An element: `text` is its own text node (first child), `attrs` its attributes (class = `cls`), `kids` its children.
function el(tag, { text = "", attrs = {}, cls = "", rect = SHOWN, kids = [], path = null, id = "", disabled = false } = {}) {
  const node = {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    id,
    className: cls,
    path,
    disabled,
    parentElement: null,
    children: kids,
    get childNodes() {
      return [...(text ? [{ nodeType: 3, nodeValue: text }] : []), ...this.children];
    },
    get innerText() {
      return [text, ...this.children.map((c) => c.innerText)].filter(Boolean).join(" ");
    },
    get textContent() {
      return this.innerText;
    },
    getAttribute(name) {
      if (name === "class") return cls || null;
      if (name === "id") return id || null;
      return Object.prototype.hasOwnProperty.call(attrs, name) ? String(attrs[name]) : null;
    },
    getBoundingClientRect: () => rect,
    contains(other) {
      for (let n = other; n; n = n.parentElement) if (n === node) return true;
      return false;
    },
    closest(selector) {
      for (let n = node; n; n = n.parentElement) if (n.nodeType === 1 && matches(n, selector)) return n;
      return null;
    },
    querySelectorAll(selector) {
      return descendants(node).filter((d) => matches(d, selector));
    },
    querySelector(selector) {
      return node.querySelectorAll(selector)[0] || null;
    },
  };
  for (const k of kids) k.parentElement = node;
  return node;
}

function page(...kids) {
  const body = el("body", { rect: { x: 0, y: 0, width: WIDTH, height: HEIGHT }, kids });
  const html = el("html", { rect: { x: 0, y: 0, width: WIDTH, height: HEIGHT }, kids: [body] });
  const document = {
    body,
    documentElement: html,
    querySelectorAll: (selector) => html.querySelectorAll(selector),
    querySelector: (selector) => html.querySelector(selector),
  };
  return { document, window: { innerWidth: WIDTH, innerHeight: HEIGHT } };
}

function call(dom, expression) {
  // eslint-disable-next-line no-new-func
  return new Function("document", "window", `${CODE}\nreturn (${expression});`)(dom.document, dom.window);
}

const NAV_LINK = () => el("a", { text: "Messages", attrs: { href: "/messages?lang=fr" }, path: "NAVLINK" });

// --- messageButton() (D2, A6) ---------------------------------------------------------------------------------------

test("messageButton: the profile button, never the navigation link that comes first in the DOM", () => {
  const button = el("button", { text: "Message", path: "PROFILE_MSG" });
  const dom = page(el("div", { id: "app-header", kids: [NAV_LINK()] }), el("div", { kids: [button] }));
  const r = call(dom, "messageButton()");
  assert.equal(r.el, button);
  assert.equal(r.candidates, 1);
  assert.equal(r.chosen, "text");
  assert.equal(r.disabled, false);
});

test("messageButton: the navigation link alone (and a « Messages » button in the nav) gives nothing", () => {
  const dom = page(el("nav", { kids: [NAV_LINK(), el("button", { text: "Messages" })] }));
  const r = call(dom, "messageButton()");
  assert.equal(r.el, null);
  assert.equal(r.candidates, 0);
  assert.equal(r.chosen, "none");
});

test("messageButton: a « Message » button inside the excluded header is counted, never chosen", () => {
  const dom = page(el("header", { kids: [el("button", { text: "Message" })] }));
  const r = call(dom, "messageButton()");
  assert.equal(r.el, null);
  assert.equal(r.excluded, 1);
  assert.equal(r.candidates, 0);
});

test("messageButton: a message-button wrapper holding a « Message » button is one candidate", () => {
  const inner = el("button", { text: "Message" });
  const wrap = el("div", { attrs: { "data-e2e": "message-button" }, kids: [inner] });
  const r = call(page(el("div", { kids: [wrap] })), "messageButton()");
  assert.equal(r.candidates, 1);
  assert.equal(r.el, wrap);
  assert.equal(r.chosen, "e2e");
});

test("messageButton: two distinct « Message » buttons out of the nav are ambiguous", () => {
  const dom = page(el("div", { kids: [el("button", { text: "Message" }), el("button", { text: "Message" })] }));
  const r = call(dom, "messageButton()");
  assert.equal(r.el, null);
  assert.equal(r.candidates, 2);
  assert.equal(r.chosen, "ambiguous");
});

test("messageButton: aria-label « Messages » is ignored, aria-label « Message » on a button is chosen", () => {
  const plural = page(el("div", { kids: [el("button", { attrs: { "aria-label": "Messages" } })] }));
  assert.equal(call(plural, "messageButton()").candidates, 0);
  const single = el("button", { attrs: { "aria-label": "Message" } });
  const r = call(page(el("div", { kids: [single] })), "messageButton()");
  assert.equal(r.el, single);
  assert.equal(r.chosen, "aria");
});

test("messageButton: an invisible button is ignored", () => {
  const dom = page(el("div", { kids: [el("button", { text: "Message", rect: HIDDEN })] }));
  assert.equal(call(dom, "messageButton()").candidates, 0);
});

test("messageButton: a unique aria-disabled button is returned disabled", () => {
  const button = el("button", { text: "Message", attrs: { "aria-disabled": "true" } });
  const r = call(page(el("div", { kids: [button] })), "messageButton()");
  assert.equal(r.el, button);
  assert.equal(r.disabled, true);
});

test("messageButton: a link with the text « Message » out of the nav is ignored", () => {
  const dom = page(el("div", { kids: [el("a", { text: "Message", attrs: { href: "/x" } })] }));
  assert.equal(call(dom, "messageButton()").candidates, 0);
});

// --- dmEditor() (D3) ------------------------------------------------------------------------------------------------

const DM_RECT = { x: 700, y: 900, width: 800, height: 40 };

test("dmEditor: the DM editor, never the comment field", () => {
  const comment = el("div", { attrs: { contenteditable: "true" } });
  const dm = el("div", { attrs: { contenteditable: "true", role: "textbox" }, rect: DM_RECT });
  const dom = page(el("div", { cls: "css-1ab2cd3-DivCommentInputWrapper", kids: [comment] }), el("div", { kids: [dm] }));
  const r = call(dom, "dmEditor()");
  assert.equal(r.el, dm);
  assert.equal(r.all.length, 1);
});

test("dmEditor: the comment field alone gives nothing", () => {
  const comment = el("div", { attrs: { contenteditable: "true" } });
  const dom = page(el("div", { cls: "css-1ab2cd3-DivCommentInputWrapper", kids: [comment] }));
  assert.equal(call(dom, "dmEditor()").el, null);
});

test("dmEditor: an editor in the header, the nav or the search is excluded", () => {
  const dom = page(
    el("div", { id: "app-header", kids: [el("input", {}), el("div", { attrs: { contenteditable: "true" } })] }),
    el("nav", { kids: [el("textarea", {})] }),
    el("div", { attrs: { "data-e2e": "search-box" }, kids: [el("div", { attrs: { role: "textbox" } })] }),
  );
  const r = call(dom, "dmEditor()");
  assert.equal(r.el, null);
  assert.equal(r.all.length, 0);
});

test("dmEditor: nested editors count once", () => {
  const inner = el("div", { attrs: { role: "textbox" } });
  const outer = el("div", { attrs: { contenteditable: "true" }, kids: [inner] });
  const r = call(page(el("div", { kids: [outer] })), "dmEditor()");
  assert.equal(r.all.length, 1);
  assert.equal(r.el, outer);
});

test("dmEditor: of two editors, the one whose placeholder, aria-label or Draft placeholder says message", () => {
  for (const hinted of [
    () => el("textarea", { attrs: { placeholder: "Send a message..." } }),
    () => el("div", { attrs: { contenteditable: "true", "aria-label": "Message" } }),
  ]) {
    const want = hinted();
    const r = call(page(el("div", { kids: [el("textarea", {}), want] })), "dmEditor()");
    assert.equal(r.el, want);
    assert.equal(r.all.length, 2);
  }
  const draft = el("div", { attrs: { contenteditable: "true" } });
  const root = el("div", { cls: "DraftEditor-root", kids: [
    el("div", { cls: "public-DraftEditorPlaceholder-root", text: "Send a message..." }), draft] });
  const r = call(page(el("div", { kids: [el("textarea", {}), root] })), "dmEditor()");
  assert.equal(r.el, draft);
});

test("dmEditor: two editors without any hint are ambiguous", () => {
  const r = call(page(el("div", { kids: [el("textarea", {}), el("div", { attrs: { contenteditable: "true" } })] })), "dmEditor()");
  assert.equal(r.el, null);
  assert.equal(r.all.length, 2);
});

// --- dmBlock() (D2, A7) ---------------------------------------------------------------------------------------------

test("dmBlock: a short visible leaf out of the nav and the bio saying DMs are refused", () => {
  const dom = page(el("div", { kids: [el("p", { text: "This account is private" })] }));
  assert.equal(call(dom, "dmBlock()"), true);
  const other = page(el("div", { kids: [el("p", { text: "You can't send messages to this account" })] }));
  assert.equal(call(other, "dmBlock()"), true);
});

test("dmBlock: the same wording in the bio, the title, a comment or a long text is not a refusal", () => {
  for (const zone of ["user-bio", "user-subtitle", "user-title"]) {
    const dom = page(el("div", { attrs: { "data-e2e": zone }, kids: [el("span", { text: "privacy first" })] }));
    assert.equal(call(dom, "dmBlock()"), false, zone);
  }
  const comment = page(el("div", { cls: "css-9zz-DivCommentItemWrapper", kids: [el("span", { text: "privacy" })] }));
  assert.equal(call(comment, "dmBlock()"), false);
  const long = page(el("p", { text: `privacy ${"x".repeat(130)}` }));
  assert.equal(call(long, "dmBlock()"), false);
  const hidden = page(el("p", { text: "privacy", rect: HIDDEN }));
  assert.equal(call(hidden, "dmBlock()"), false);
});

// --- shapeOf() (D1) -------------------------------------------------------------------------------------------------

test("shapeOf: tag, data-e2e, role, component name of the css class, rectangle, in_view — no text, no href", () => {
  const box = el("div", { cls: "css-1ab2cd3-DivChatBox e1x2y3z0", attrs: { "data-e2e": "chat-box", role: "dialog",
    href: "/@someone" }, text: "SENTINEL_TEXT", rect: { x: 10.4, y: 20.6, width: 30, height: 40 } });
  const s = call(page(box), "shapeOf(document.body.children[0])");
  assert.deepEqual(s, { tag: "DIV", e2e: "chat-box", role: "dialog", cls: "DivChatBox", x: 10, y: 21, w: 30, h: 40, in_view: true });
  assert.ok(!JSON.stringify(s).includes("SENTINEL_TEXT") && !JSON.stringify(s).includes("someone"));
});

test("shapeOf: no component class gives cls null, out of the viewport gives in_view false, null gives null", () => {
  const low = el("button", { cls: "plain e1x2y3z0", rect: { x: 10, y: HEIGHT - 5, width: 30, height: 40 } });
  const s = call(page(low), "shapeOf(document.body.children[0])");
  assert.equal(s.cls, null);
  assert.equal(s.e2e, null);
  assert.equal(s.in_view, false);
  assert.equal(call(page(), "shapeOf(null)"), null);
});

// --- convColumn() / convText() (A2) ---------------------------------------------------------------------------------

function inbox({ editorText = "" } = {}) {
  const editor = el("div", { attrs: { contenteditable: "true" }, text: editorText, rect: { x: 700, y: 900, width: 700, height: 40 } });
  const send = el("button", { attrs: { "data-e2e": "dm-new-send-btn" }, rect: { x: 1420, y: 902, width: 36, height: 36 } });
  const bubble = el("div", { cls: "css-1aa-DivChatItemWrapper", rect: { x: 1100, y: 500, width: 300, height: 40 },
    kids: [el("p", { text: "Salut ! " }), el("p", { kids: [el("span", { text: "Voici le lien" })] })] });
  const column = el("div", { rect: { x: 640, y: 60, width: 1280, height: 920 }, kids: [
    el("div", { rect: { x: 640, y: 60, width: 1280, height: 60 }, text: "Fan Name" }),
    el("div", { rect: { x: 640, y: 120, width: 1280, height: 760 }, kids: [bubble] }),
    el("div", { rect: { x: 680, y: 880, width: 1240, height: 80 }, kids: [editor, send] }),
  ] });
  const list = el("div", { rect: { x: 72, y: 60, width: 560, height: 920 }, kids: [
    el("p", { text: "Salut ! Voici le lien (preview of another conversation)", rect: { x: 72, y: 80, width: 560, height: 40 } }),
  ] });
  const main = el("div", { rect: { x: 72, y: 60, width: 1848, height: 920 }, kids: [list, column] });
  return { dom: page(main), editor, send, column, list };
}

test("convColumn: the highest ancestor of the editor that keeps its left edge near the editor", () => {
  const { dom, column } = inbox();
  assert.equal(call(dom, "convColumn(dmEditor().el)"), column);
});

test("convText: the column's text without the editor's text and without the conversation list on the left", () => {
  const { dom } = inbox({ editorText: "DRAFT_IN_EDITOR" });
  const text = call(dom, "convText(dmEditor().el)");
  assert.ok(text.includes("Salut ! Voici le lien"), text);
  assert.ok(!text.includes("DRAFT_IN_EDITOR"), text);
  assert.ok(!text.includes("preview of another conversation"), text);
  assert.ok(text.length <= 6000);
  assert.equal(call(dom, "convText(null)"), "");
});

// --- sendButton() (A8) ----------------------------------------------------------------------------------------------

test("sendButton: the existing selectors, bounded to the editor's column", () => {
  const { dom, send } = inbox();
  assert.equal(call(dom, "sendButton(dmEditor().el)"), send);
});

test("sendButton: else a button whose data-e2e or aria-label says send, on the editor's row, right of it", () => {
  const editor = el("div", { attrs: { contenteditable: "true" }, rect: { x: 700, y: 900, width: 600, height: 40 } });
  const far = el("button", { attrs: { "aria-label": "send gift" }, rect: { x: 1400, y: 300, width: 36, height: 36 } });
  const left = el("button", { attrs: { "data-e2e": "x-send" }, rect: { x: 100, y: 902, width: 36, height: 36 } });
  const row = el("div", { attrs: { role: "button", "data-e2e": "chat-send" }, rect: { x: 1320, y: 930, width: 36, height: 36 } });
  const dom = page(el("div", { rect: { x: 700, y: 880, width: 600, height: 80 }, kids: [editor] }),
    el("div", { rect: { x: 0, y: 0, width: WIDTH, height: HEIGHT }, kids: [far, left, row] }));
  assert.equal(call(dom, "sendButton(dmEditor().el)"), row);
});

test("sendButton: nothing gives null", () => {
  const editor = el("div", { attrs: { contenteditable: "true" }, rect: { x: 700, y: 900, width: 600, height: 40 } });
  const dom = page(el("div", { kids: [editor, el("button", { text: "Other" })] }));
  assert.equal(call(dom, "sendButton(dmEditor().el)"), null);
  assert.equal(call(dom, "sendButton(null)"), null);
});

// --- PROFILE_JS / THREAD_JS run whole under node:vm (T2) -----------------------------------------------------------

function script(name) {
  const python = process.env.HERMES_PYTHON || "python3";
  return execFileSync(
    python,
    ["-c", `import sys; from app.tiktok_reply_selectors import ${name} as js; sys.stdout.write(js)`],
    { cwd: ROOT, encoding: "utf8" },
  );
}

function runWhole(name, dom, pathname) {
  return vm.runInNewContext(script(name), { document: dom.document, window: dom.window, location: { pathname } });
}

test("the whole PROFILE_JS runs and returns its closed keys, the profile button and never the nav link", () => {
  const button = el("button", { text: "Message", cls: "css-7qq1x2-ButtonMessage e1x2y3z0" });
  const dom = page(el("div", { id: "app-header", kids: [NAV_LINK()] }), el("div", { kids: [button] }));
  const r = runWhole("PROFILE_JS", dom, "/@fan.one");
  assert.deepEqual(Object.keys(r).sort(), ["disabled", "dm_block", "login_gate", "message_button", "path_is_profile", "probe"]);
  assert.deepEqual(Object.keys(r.probe).sort(), ["btn", "candidates", "chosen", "excluded_btns", "nav_msg_links", "path_is_profile"]);
  assert.equal(r.login_gate, false);
  assert.equal(r.path_is_profile, true);
  assert.equal(r.disabled, false);
  assert.equal(r.dm_block, false);
  assert.ok(r.message_button.selector.endsWith("button:nth-of-type(1)"), r.message_button.selector);
  assert.equal(r.probe.chosen, "text");
  assert.equal(r.probe.candidates, 1);
  assert.equal(r.probe.nav_msg_links, 1);
  assert.equal(r.probe.btn.cls, "ButtonMessage");
  const empty = runWhole("PROFILE_JS", page(el("nav", { kids: [NAV_LINK()] })), "/messages");
  assert.equal(empty.message_button, null);
  assert.equal(empty.path_is_profile, false);
  assert.equal(empty.probe.chosen, "none");
  assert.equal(empty.probe.btn, null);
});

test("the whole THREAD_JS runs and returns its closed keys, the conversation and the probe", () => {
  const { dom } = inbox();
  const r = runWhole("THREAD_JS", dom, "/messages");
  assert.deepEqual(Object.keys(r).sort(), ["conversation", "editor", "error", "login_gate", "outgoing", "probe", "send_button"]);
  assert.deepEqual(Object.keys(r.probe).sort(),
    ["chat_items", "dm_block", "ed", "ed1", "ed2", "editors", "path_messages", "send"]);
  assert.equal(r.editor.text, "");
  assert.ok(r.send_button.selector);
  assert.ok(r.conversation.includes("Salut ! Voici le lien"));
  assert.equal(r.probe.editors, 1);
  assert.equal(r.probe.ed1, null);
  assert.equal(r.probe.path_messages, true);
  assert.equal(r.probe.chat_items, 1);
  assert.equal(r.probe.send.e2e, "dm-new-send-btn");
  const none = runWhole("THREAD_JS", page(el("nav", { kids: [NAV_LINK()] })), "/messages");
  assert.equal(none.editor, null);
  assert.equal(none.send_button, null);
  assert.equal(none.conversation, "");
  assert.equal(none.probe.editors, 0);
  const two = runWhole("THREAD_JS", page(el("div", { kids: [el("textarea", {}), el("textarea", {})] })), "/messages");
  assert.equal(two.editor, null);
  assert.equal(two.probe.editors, 2);
  assert.equal(two.probe.ed1.tag, "TEXTAREA");
  assert.equal(two.probe.ed2.tag, "TEXTAREA");
});
