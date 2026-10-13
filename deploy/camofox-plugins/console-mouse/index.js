// console-mouse — Camofox plugin (hermes-console s22, ADR 053).
//
// Trusted mouse gestures for the TikTok captcha solver of tiktok-backoffice: the core REST API only clicks
// by selector, and JS-dispatched events are untrusted. This plugin plays move/down/up/wait on the Playwright
// mouse of an existing tab, nothing else.
//
// Mounted as its own sub-directory (./camofox-plugins/console-mouse:/app/plugins/console-mouse:ro), never on
// /app/plugins, which would hide the persistence, vnc and youtube plugins of the image.
//
// Invariants (checked by tests): no module loading, no listener of any kind, no page reading, no navigation,
// no key presses. Logs carry counts only.

export const LIMITS = Object.freeze({
  maxActions: 1000,
  maxCoord: 10000,
  maxSteps: 100,
  maxWaitMs: 2000,
  maxTotalWaitMs: 15000,
  maxRuntimeMs: 20000,
  holdReleaseMs: 10000,
});

const BODY_KEYS = ["userId", "actions", "hold"];
const ACTION_KEYS = {
  move: ["type", "x", "y", "steps"],
  down: ["type"],
  up: ["type"],
  wait: ["type", "ms"],
};

function isPlainObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function onlyKeys(object, allowed) {
  return Object.keys(object).every((key) => allowed.indexOf(key) !== -1);
}

function isCoord(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= LIMITS.maxCoord;
}

function isIntIn(value, min, max) {
  return typeof value === "number" && Number.isInteger(value) && value >= min && value <= max;
}

function validateAction(action) {
  if (!isPlainObject(action) || typeof action.type !== "string") return null;
  const allowed = ACTION_KEYS[action.type];
  if (!Object.prototype.hasOwnProperty.call(ACTION_KEYS, action.type) || !onlyKeys(action, allowed)) return null;
  if (action.type === "move") {
    if (!isCoord(action.x) || !isCoord(action.y)) return null;
    if (action.steps !== undefined && !isIntIn(action.steps, 1, LIMITS.maxSteps)) return null;
    return action.steps === undefined
      ? { type: "move", x: action.x, y: action.y }
      : { type: "move", x: action.x, y: action.y, steps: action.steps };
  }
  if (action.type === "wait") {
    if (!isIntIn(action.ms, 0, LIMITS.maxWaitMs)) return null;
    return { type: "wait", ms: action.ms };
  }
  return { type: action.type };
}

/** Validate a POST /tabs/:tabId/mouse body. Never echoes a value back. */
export function validateBody(body) {
  const invalid = { ok: false };
  if (!isPlainObject(body) || !onlyKeys(body, BODY_KEYS)) return invalid;
  if (typeof body.userId !== "string" || body.userId.length === 0) return invalid;
  if (body.hold !== undefined && typeof body.hold !== "boolean") return invalid;
  if (!Array.isArray(body.actions) || body.actions.length < 1 || body.actions.length > LIMITS.maxActions) {
    return invalid;
  }
  const actions = [];
  let totalWait = 0;
  for (const raw of body.actions) {
    const action = validateAction(raw);
    if (!action) return invalid;
    if (action.type === "wait") totalWait += action.ms;
    actions.push(action);
  }
  if (totalWait > LIMITS.maxTotalWaitMs) return invalid;
  return { ok: true, userId: body.userId, actions, hold: body.hold === true };
}

/** Copy of the core findTab (not exposed to plugins): walks session.tabGroups. */
export function findTab(session, tabId) {
  if (!session || !session.tabGroups) return null;
  for (const group of session.tabGroups.values()) {
    if (group && group.has(tabId)) return group.get(tabId) || null;
  }
  return null;
}

class MouseTimeout extends Error {}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Races one step against what is left of the request budget.
function withinBudget(promise, remainingMs) {
  let timer;
  const deadline = new Promise((resolve, reject) => {
    timer = setTimeout(() => reject(new MouseTimeout()), Math.max(0, remainingMs));
  });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}

export function register(app, ctx) {
  const auth = ctx.auth();
  // Per-tab button state: { down, timer, busy }. Weak, so a closed tab leaves nothing behind.
  const states = new WeakMap();

  function stateOf(tabState) {
    let state = states.get(tabState);
    if (!state) {
      state = { down: false, timer: null, busy: false };
      states.set(tabState, state);
    }
    return state;
  }

  async function release(page, state) {
    if (!state.down) return;
    state.down = false;
    try {
      await page.mouse.up();
    } catch (_err) {
      // ignored: the button is considered released
    }
  }

  app.get("/console-mouse/health", auth, (req, res) => {
    res.json({ ok: true, plugin: "console-mouse", version: 1 });
  });

  app.post("/tabs/:tabId/mouse", auth, async (req, res) => {
    const parsed = validateBody(req.body);
    if (!parsed.ok) return res.status(400).json({ error: "invalid_body" });

    const tabId = req.params.tabId;
    const session = ctx.sessions.get(ctx.normalizeUserId(parsed.userId));
    if (!session) return res.status(404).json({ error: "tab_not_found" });
    if (session._closing) return res.status(409).json({ error: "session_closing" });
    const tabState = findTab(session, tabId);
    if (!tabState || !tabState.page) return res.status(404).json({ error: "tab_not_found" });

    const state = stateOf(tabState);
    if (state.busy) return res.status(409).json({ error: "busy" });
    state.busy = true;
    if (state.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }

    session.lastAccess = Date.now();
    tabState.toolCalls = (tabState.toolCalls || 0) + 1;

    const page = tabState.page;
    const started = Date.now();
    let performed = 0;
    try {
      for (const action of parsed.actions) {
        const remaining = LIMITS.maxRuntimeMs - (Date.now() - started);
        if (remaining <= 0) throw new MouseTimeout();
        if (action.type === "move") {
          const options = action.steps === undefined ? undefined : { steps: action.steps };
          await withinBudget(page.mouse.move(action.x, action.y, options), remaining);
        } else if (action.type === "down") {
          await withinBudget(page.mouse.down(), remaining);
          state.down = true;
        } else if (action.type === "up") {
          await withinBudget(page.mouse.up(), remaining);
          state.down = false;
        } else {
          await withinBudget(sleep(action.ms), remaining);
        }
        performed += 1;
      }
      if (state.down && parsed.hold) {
        state.timer = setTimeout(() => {
          state.timer = null;
          release(page, state);
        }, LIMITS.holdReleaseMs);
      } else {
        await release(page, state);
      }
      ctx.log("info", "console-mouse", { tabId, actions: performed, hold: parsed.hold });
      return res.json({ ok: true, performed });
    } catch (err) {
      await release(page, state);
      if (err instanceof MouseTimeout) return res.status(504).json({ error: "mouse_timeout" });
      return res.status(500).json({ error: ctx.safeError(err) });
    } finally {
      state.busy = false;
    }
  });
}
