// s29 (hermes-console ADR 063, D1): videoAuthorHandles() of app/tiktok_reply_selectors.py, the handles of the video
// author read from the links of a BOUNDED author area (never the whole document: the logged-in account is the author
// of the target videos, so its navigation links are on every page, a derived video included).
//
// The definitions of clean, vis and videoAuthorHandles are read from tiktok_reply_selectors.py as text (one line each,
// starting with " function <name>("). They run against a light fake DOM: document.querySelectorAll(sel) returns the
// elements declared for each selector of the (comma separated) list, and `a[href*="/@"]` returns EVERY link of the
// page, so a whole-document query would pick the navigation, comments and recommendations up. The last test runs the
// whole _COMMENT_EXTRACTION_JS of app/camofox_reader.py (exported by a short Python command) under node:vm.
// node:test, node:assert, node:vm and node:child_process only, no dependency, no network. Every handle is invented.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const ROOT = fileURLToPath(new URL("../../", import.meta.url));
const SELECTORS = readFileSync(fileURLToPath(new URL("../../app/tiktok_reply_selectors.py", import.meta.url)), "utf8");

function definition(text, name) {
  const lines = text.split("\n").filter((l) => l.startsWith(` function ${name}(`));
  assert.equal(lines.length, 1, `one definition of ${name} expected`);
  return lines[0];
}

const CODE = ["clean", "vis", "videoAuthorHandles"].map((name) => definition(SELECTORS, name)).join("\n");

const LINK = 'a[href*="/@"]';
const SHOWN = { x: 1500, y: 140, width: 120, height: 24 };
const HIDDEN = { x: 0, y: 0, width: 0, height: 0 };

const USERNAME = '[data-e2e="browse-username"]';
const UNIQUE_ID = '[data-e2e="video-author-uniqueid"]';
const BROWSE_AVATAR = '[data-e2e="browse-user-avatar"]';
const VIDEO_AVATAR = '[data-e2e="video-author-avatar"]';
const AUTHOR_CONTAINER = '[class*="DivAuthorContainer"]';

const HEADER = "#app-header";
const NAV = "nav";
const COMMENT = '[class*="DivCommentItemWrapper"]';
const RECO = '[data-e2e="recommend-list-item-container"]';
const DESC = '[data-e2e="browse-video-desc"]';

// An element. `href` makes it a link (it is then its own closest('a[href*="/@"]')), `parentLink` is the link it sits
// in, `links` the links under it, `inside` the container selectors it sits in (answered by closest()).
function node({ href = null, visible = true, inside = [], parentLink = null, links = [], text = "" } = {}) {
  const self = {
    innerText: text,
    textContent: text,
    getBoundingClientRect: () => (visible ? SHOWN : HIDDEN),
    getAttribute: (name) => (name === "href" ? href : null),
    closest(selector) {
      for (const part of selector.split(",").map((s) => s.trim())) {
        if (part === LINK) {
          if (href && href.includes("/@")) return self;
          if (parentLink) return parentLink;
          continue;
        }
        if (inside.includes(part)) return {};
      }
      return null;
    },
    querySelectorAll(selector) {
      return selector === LINK ? links : [];
    },
  };
  return self;
}

function link(href, options = {}) {
  return node({ ...options, href });
}

function handlesOn(bySelector) {
  const allLinks = new Set(bySelector[LINK] ?? []);
  const document = {
    body: { innerText: "" },
    querySelectorAll(selector) {
      const found = new Set();
      for (const part of selector.split(",").map((s) => s.trim())) {
        for (const element of part === LINK ? Array.from(allLinks) : bySelector[part] ?? []) found.add(element);
      }
      return Array.from(found);
    },
  };
  // eslint-disable-next-line no-new-func
  return new Function("document", "window", `${CODE}\nreturn videoAuthorHandles();`)(document, {
    innerWidth: 1920,
    innerHeight: 987,
  });
}

// The navigation of the logged-in account (header avatar, side bar profile link): it points to the target author.
function navigationTo(handle) {
  const header = link(`/@${handle}`, { inside: [HEADER] });
  const side = link(`/@${handle}?lang=en`, { inside: [NAV] });
  return { header, side, avatar: node({ inside: [HEADER], parentLink: header }) };
}

test("good video: the bounded author link wins over nav, comments, recommendations and mentions", () => {
  const author = link("/@creator.one");
  const nav = navigationTo("creator.one");
  const fan = link("/@fan.one", { inside: [COMMENT] });
  const reco = link("/@reco.one", { inside: [RECO] });
  const mention = link("/@mention.one", { inside: [DESC] });
  const handles = handlesOn({
    [USERNAME]: [node({ text: "flodev", parentLink: author })],
    [AUTHOR_CONTAINER]: [node({ links: [author, mention] }), node({ inside: [COMMENT], links: [fan] })],
    [BROWSE_AVATAR]: [nav.avatar],
    [VIDEO_AVATAR]: [node({ inside: [RECO], parentLink: reco })],
    [LINK]: [nav.header, nav.side, author, fan, reco, mention],
  });
  assert.deepEqual(handles, ["creator.one"]);
});

test("derived video: the author area of another account is read, never the nav of the logged-in author", () => {
  const other = link("/@other.one");
  const nav = navigationTo("creator.one");
  const handles = handlesOn({
    [USERNAME]: [node({ text: "Other", parentLink: other })],
    [BROWSE_AVATAR]: [nav.avatar],
    [LINK]: [nav.header, nav.side, other],
  });
  assert.deepEqual(handles, ["other.one"]);
});

test("page without an author area (nav, comments, recommendations only) gives no handle", () => {
  const nav = navigationTo("creator.one");
  const fan = link("/@fan.one", { inside: [COMMENT] });
  const reco = link("/@reco.one", { inside: [RECO] });
  const handles = handlesOn({
    [BROWSE_AVATAR]: [nav.avatar],
    [AUTHOR_CONTAINER]: [node({ inside: [COMMENT], links: [fan] })],
    [VIDEO_AVATAR]: [node({ inside: [RECO], parentLink: reco })],
    [LINK]: [nav.header, nav.side, fan, reco],
  });
  assert.deepEqual(handles, []);
});

test("an invisible author link is ignored", () => {
  const hidden = link("/@other.one", { visible: false });
  const handles = handlesOn({ [UNIQUE_ID]: [node({ parentLink: hidden })], [LINK]: [hidden] });
  assert.deepEqual(handles, []);
});

test("handles are lowercase, without duplicates and at most five", () => {
  const links = ["/@Creator.One", "/@creator.one", "/@b.one", "/@c.one", "/@d.one", "/@e.one", "/@f.one"].map((h) =>
    link(h),
  );
  const handles = handlesOn({ [AUTHOR_CONTAINER]: [node({ links })], [LINK]: links });
  assert.deepEqual(handles, ["creator.one", "b.one", "c.one", "d.one", "e.one"]);
});

test("a suffixed href gives the bare handle", () => {
  const query = link("/@x.one?lang=fr");
  const video = link("https://www.tiktok.com/@y.one/video/1234567890");
  const handles = handlesOn({ [AUTHOR_CONTAINER]: [node({ links: [query, video] })], [LINK]: [query, video] });
  assert.deepEqual(handles, ["x.one", "y.one"]);
});

test("an href with an invalid encoding is ignored without exception", () => {
  const broken = link("/@x%ZZ");
  const good = link("/@z.one");
  const handles = handlesOn({ [AUTHOR_CONTAINER]: [node({ links: [broken, good] })], [LINK]: [broken, good] });
  assert.deepEqual(handles, ["z.one"]);
});

test("the whole _COMMENT_EXTRACTION_JS runs and returns video_author_handles (R1)", () => {
  const python = process.env.HERMES_PYTHON || "python3";
  const script = execFileSync(
    python,
    ["-c", "import sys; from app.camofox_reader import _COMMENT_EXTRACTION_JS as js; sys.stdout.write(js)"],
    { cwd: ROOT, encoding: "utf8" },
  );
  const author = link("/@creator.one");
  const empty = () => [];
  const document = {
    title: "",
    body: { innerText: "", innerHTML: "" },
    documentElement: {},
    querySelectorAll: (selector) => (selector.includes(USERNAME) ? [node({ parentLink: author })] : []),
    querySelector: () => null,
    getElementsByTagName: empty,
  };
  const window = { innerWidth: 1920, innerHeight: 987, getComputedStyle: () => ({}) };
  const location = { href: "https://www.tiktok.com/@creator.one/video/1234567890", pathname: "/@creator.one/video/1234567890" };
  const r = vm.runInNewContext(script, { document, window, location, getComputedStyle: window.getComputedStyle });
  assert.ok(Array.isArray(r.video_author_handles));
  assert.deepEqual(Array.from(r.video_author_handles), ["creator.one"]);
});
