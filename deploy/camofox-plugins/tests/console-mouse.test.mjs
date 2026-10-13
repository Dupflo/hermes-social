// s22 (hermes-console ADR 053) — console-mouse plugin, tested on a fake Express app, a fake ctx and a fake page.
// No dependency: node:test + node:assert only. Run: `node --test deploy/camofox-plugins/tests/`.
import { test } from "node:test";
import assert from "node:assert/strict";

import { register, LIMITS, validateBody, findTab } from "../console-mouse/index.js";

const AUTH = function authMarker(req, res, next) {
  next();
};

function fakeApp() {
  const routes = [];
  return {
    routes,
    get(path, ...handlers) {
      routes.push({ method: "GET", path, handlers });
    },
    post(path, ...handlers) {
      routes.push({ method: "POST", path, handlers });
    },
    route(method, path) {
      return routes.find((r) => r.method === method && r.path === path);
    },
  };
}

function fakePage() {
  const calls = [];
  const page = {
    calls,
    mouse: {
      move: async (x, y, options) => {
        calls.push(options === undefined ? ["move", x, y] : ["move", x, y, options]);
      },
      down: async () => {
        calls.push(["down"]);
      },
      up: async () => {
        calls.push(["up"]);
      },
    },
  };
  return page;
}

function fakeCtx({ closing = false } = {}) {
  const page = fakePage();
  const tabState = { page, toolCalls: 0 };
  const group = new Map([["tab-1", tabState]]);
  const session = { tabGroups: new Map([["list-1", group]]), lastAccess: 0, _closing: closing };
  const logs = [];
  const eventsCalls = [];
  const ctx = {
    sessions: new Map([["user-1", session]]),
    normalizeUserId: (id) => String(id).trim(),
    auth: () => AUTH,
    log: (...args) => logs.push(args),
    safeError: (err) => `safe:${err && err.message}`,
    events: {
      on: () => {
        eventsCalls.push("on");
        throw new Error("events.on must never be called");
      },
      once: () => {
        eventsCalls.push("once");
        throw new Error("events.once must never be called");
      },
    },
  };
  return { ctx, page, tabState, session, logs, eventsCalls };
}

function fakeRes() {
  return {
    statusCode: 200,
    body: undefined,
    status(code) {
      this.statusCode = code;
      return this;
    },
    json(body) {
      this.body = body;
      return this;
    },
  };
}

async function call(app, method, path, { params = {}, body = {} } = {}) {
  const route = app.route(method, path);
  assert.ok(route, `${method} ${path} registered`);
  const res = fakeRes();
  const handler = route.handlers[route.handlers.length - 1];
  await handler({ params, body, headers: {} }, res);
  return res;
}

function setup(options) {
  const env = fakeCtx(options);
  const app = fakeApp();
  register(app, env.ctx);
  const post = (body, tabId = "tab-1") => call(app, "POST", "/tabs/:tabId/mouse", { params: { tabId }, body });
  return { ...env, app, post };
}

const valid = () => ({
  userId: "user-1",
  actions: [{ type: "move", x: 10, y: 20 }, { type: "down" }, { type: "move", x: 50, y: 20, steps: 5 }, { type: "up" }],
});

test("both routes sit behind the middleware returned by ctx.auth()", () => {
  const { app } = setup();
  const health = app.route("GET", "/console-mouse/health");
  const mouse = app.route("POST", "/tabs/:tabId/mouse");
  assert.ok(health && mouse);
  assert.equal(health.handlers[0], AUTH);
  assert.equal(mouse.handlers[0], AUTH);
  assert.equal(health.handlers.length, 2);
  assert.equal(mouse.handlers.length, 2);
});

test("health answers the probe body", async () => {
  const { app } = setup();
  const res = await call(app, "GET", "/console-mouse/health");
  assert.equal(res.statusCode, 200);
  assert.deepEqual(res.body, { ok: true, plugin: "console-mouse", version: 1 });
});

test("a valid body plays move/down/up in order, forwards steps, keeps the tab alive", async () => {
  const { post, page, tabState, session, logs } = setup();
  const before = Date.now();
  const res = await post(valid());
  assert.equal(res.statusCode, 200);
  assert.deepEqual(res.body, { ok: true, performed: 4 });
  assert.deepEqual(page.calls, [["move", 10, 20], ["down"], ["move", 50, 20, { steps: 5 }], ["up"]]);
  assert.equal(tabState.toolCalls, 1);
  assert.ok(session.lastAccess >= before);
  // counts only in the log
  assert.equal(logs.length, 1);
  assert.deepEqual(logs[0], ["info", "console-mouse", { tabId: "tab-1", actions: 4, hold: false }]);
});

test("a wait action waits without touching the page", async () => {
  const { post, page } = setup();
  const res = await post({ userId: "user-1", actions: [{ type: "wait", ms: 5 }, { type: "move", x: 1, y: 1 }] });
  assert.equal(res.statusCode, 200);
  assert.deepEqual(page.calls, [["move", 1, 1]]);
});

const many = (n) => Array.from({ length: n }, () => ({ type: "move", x: 1, y: 1 }));
const INVALID = [
  ["NaN x", { userId: "user-1", actions: [{ type: "move", x: NaN, y: 1 }] }],
  ["Infinity y", { userId: "user-1", actions: [{ type: "move", x: 1, y: Infinity }] }],
  ["negative x", { userId: "user-1", actions: [{ type: "move", x: -1, y: 1 }] }],
  ["x 10001", { userId: "user-1", actions: [{ type: "move", x: 10001, y: 1 }] }],
  ["string x", { userId: "user-1", actions: [{ type: "move", x: "1", y: 1 }] }],
  ["steps 0", { userId: "user-1", actions: [{ type: "move", x: 1, y: 1, steps: 0 }] }],
  ["steps 101", { userId: "user-1", actions: [{ type: "move", x: 1, y: 1, steps: 101 }] }],
  ["steps 1.5", { userId: "user-1", actions: [{ type: "move", x: 1, y: 1, steps: 1.5 }] }],
  ["wait 2001", { userId: "user-1", actions: [{ type: "wait", ms: 2001 }] }],
  ["wait 1.5", { userId: "user-1", actions: [{ type: "wait", ms: 1.5 }] }],
  [
    "wait sum 15001",
    { userId: "user-1", actions: [...Array.from({ length: 7 }, () => ({ type: "wait", ms: 2000 })), { type: "wait", ms: 1001 }] },
  ],
  ["1001 actions", { userId: "user-1", actions: many(1001) }],
  ["no action", { userId: "user-1", actions: [] }],
  ["actions not an array", { userId: "user-1", actions: { type: "move" } }],
  ["type click", { userId: "user-1", actions: [{ type: "click", x: 1, y: 1 }] }],
  ["unknown action key", { userId: "user-1", actions: [{ type: "down", button: "right" }] }],
  ["unknown body key", { userId: "user-1", actions: [{ type: "down" }], text: "x" }],
  ["userId missing", { actions: [{ type: "down" }] }],
  ["userId empty", { userId: "", actions: [{ type: "down" }] }],
  ["hold not a boolean", { userId: "user-1", actions: [{ type: "down" }], hold: "yes" }],
  ["action null", { userId: "user-1", actions: [null] }],
  ["body null", null],
];

for (const [name, body] of INVALID) {
  test(`invalid body (${name}) ⇒ 400 invalid_body, nothing played`, async () => {
    const { post, page, tabState } = setup();
    const res = await post(body);
    assert.equal(res.statusCode, 400);
    assert.deepEqual(res.body, { error: "invalid_body" });
    assert.deepEqual(page.calls, []);
    assert.equal(tabState.toolCalls, 0);
    assert.equal(validateBody(body).ok, false);
  });
}

test("validateBody accepts the bounds exactly", () => {
  const body = {
    userId: "u",
    hold: true,
    actions: [
      { type: "move", x: 0, y: LIMITS.maxCoord, steps: LIMITS.maxSteps },
      { type: "move", x: 0.5, y: 1, steps: 1 },
      ...Array.from({ length: 7 }, () => ({ type: "wait", ms: 2000 })),
      { type: "wait", ms: 1000 },
    ],
  };
  const result = validateBody(body);
  assert.equal(result.ok, true);
  assert.equal(result.hold, true);
  assert.equal(validateBody({ userId: "u", actions: many(LIMITS.maxActions) }).ok, true);
});

test("unknown tab ⇒ 404 tab_not_found; unknown session ⇒ 404", async () => {
  const { post, page } = setup();
  const res = await post(valid(), "tab-404");
  assert.equal(res.statusCode, 404);
  assert.deepEqual(res.body, { error: "tab_not_found" });
  const res2 = await post({ ...valid(), userId: "nobody" });
  assert.equal(res2.statusCode, 404);
  assert.deepEqual(page.calls, []);
});

test("closing session ⇒ 409", async () => {
  const { post, page, tabState } = setup({ closing: true });
  const res = await post(valid());
  assert.equal(res.statusCode, 409);
  assert.deepEqual(page.calls, []);
  assert.equal(tabState.toolCalls, 0);
});

test("findTab walks the tab groups", () => {
  const { session, tabState } = fakeCtx();
  assert.equal(findTab(session, "tab-1"), tabState);
  assert.equal(findTab(session, "nope"), null);
  assert.equal(findTab({ tabGroups: new Map() }, "tab-1"), null);
});

test("a second concurrent request on the same tab ⇒ 409 busy", async () => {
  const { post, page } = setup();
  let release;
  page.mouse.move = () =>
    new Promise((resolve) => {
      release = resolve;
    });
  const first = post({ userId: "user-1", actions: [{ type: "move", x: 1, y: 1 }] });
  await new Promise((r) => setImmediate(r));
  const second = await post({ userId: "user-1", actions: [{ type: "down" }] });
  assert.equal(second.statusCode, 409);
  assert.deepEqual(second.body, { error: "busy" });
  release();
  const res = await first;
  assert.equal(res.statusCode, 200);
  // free again afterwards
  page.mouse.move = async () => {};
  const third = await post({ userId: "user-1", actions: [{ type: "up" }] });
  assert.equal(third.statusCode, 200);
});

test("an error after a down releases the button, then 500 with safeError", async () => {
  const { post, page } = setup();
  let n = 0;
  page.mouse.move = async () => {
    n += 1;
    if (n === 2) throw new Error("boom");
  };
  const res = await post(valid());
  assert.equal(res.statusCode, 500);
  assert.deepEqual(res.body, { error: "safe:boom" });
  assert.deepEqual(page.calls, [["down"], ["up"]]);
});

test("without hold, a button left down is released at the end of the request", async () => {
  const { post, page } = setup();
  const res = await post({ userId: "user-1", actions: [{ type: "move", x: 1, y: 1 }, { type: "down" }] });
  assert.equal(res.statusCode, 200);
  assert.deepEqual(page.calls, [["move", 1, 1], ["down"], ["up"]]);
});

test("hold keeps the button down, a 10 s timer releases it", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  const { post, page } = setup();
  const res = await post({ userId: "user-1", actions: [{ type: "down" }], hold: true });
  assert.equal(res.statusCode, 200);
  assert.deepEqual(page.calls, [["down"]]);
  t.mock.timers.tick(LIMITS.holdReleaseMs - 1);
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(page.calls, [["down"]]);
  t.mock.timers.tick(1);
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(page.calls, [["down"], ["up"]]);
});

test("a following request cancels the hold timer", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  const { post, page } = setup();
  await post({ userId: "user-1", actions: [{ type: "down" }], hold: true });
  t.mock.timers.tick(5000);
  const res = await post({ userId: "user-1", actions: [{ type: "move", x: 3, y: 3 }], hold: true });
  assert.equal(res.statusCode, 200);
  t.mock.timers.tick(LIMITS.holdReleaseMs - 1);
  await new Promise((r) => setImmediate(r));
  // the first timer (due at 10 s) was cancelled: no up yet
  assert.deepEqual(page.calls, [["down"], ["move", 3, 3]]);
  const last = await post({ userId: "user-1", actions: [{ type: "up" }] });
  assert.equal(last.statusCode, 200);
  t.mock.timers.tick(LIMITS.holdReleaseMs * 2);
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(page.calls, [["down"], ["move", 3, 3], ["up"]]);
});

test("past 20 s the request stops, releases the button and answers 504", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  const { post, page } = setup();
  page.mouse.move = async (x, y) => {
    page.calls.push(["move", x, y]);
    t.mock.timers.tick(LIMITS.maxRuntimeMs + 1);
  };
  const res = await post({
    userId: "user-1",
    actions: [{ type: "down" }, { type: "move", x: 1, y: 1 }, { type: "move", x: 2, y: 2 }],
  });
  assert.equal(res.statusCode, 504);
  assert.deepEqual(res.body, { error: "mouse_timeout" });
  assert.deepEqual(page.calls, [["down"], ["move", 1, 1], ["up"]]);
});

test("an action that hangs is raced against the remaining budget ⇒ 504", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  const { post, page } = setup();
  page.mouse.move = () => new Promise(() => {});
  const pending = post({ userId: "user-1", actions: [{ type: "down" }, { type: "move", x: 1, y: 1 }] });
  await new Promise((r) => setImmediate(r));
  t.mock.timers.tick(LIMITS.maxRuntimeMs);
  const res = await pending;
  assert.equal(res.statusCode, 504);
  assert.deepEqual(page.calls, [["down"], ["up"]]);
});

test("the plugin never subscribes to any event", async () => {
  const { post, eventsCalls } = setup();
  await post(valid());
  await post({ ...valid(), hold: true });
  assert.deepEqual(eventsCalls, []);
});

test("limits are the ADR 053 contract", () => {
  assert.deepEqual(
    { ...LIMITS },
    {
      maxActions: 1000,
      maxCoord: 10000,
      maxSteps: 100,
      maxWaitMs: 2000,
      maxTotalWaitMs: 15000,
      maxRuntimeMs: 20000,
      holdReleaseMs: 10000,
    },
  );
});

