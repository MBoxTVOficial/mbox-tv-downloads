export const TIMEZONE = "America/Argentina/Buenos_Aires";
export const REQUEST_TIMEOUT_MS = 8_000;
export const RECENT_RUN_MS = 8 * 60_000;
export const HISTORY_WINDOW_MS = 24 * 60 * 60_000;
export const TARGET = Object.freeze({
  owner: "MBoxTVOficial",
  repository: "mbox-tv-downloads",
  workflow: "update-sports-live.yml",
  branch: "main",
});

const WORKFLOW_URL = `https://api.github.com/repos/${TARGET.owner}/${TARGET.repository}/actions/workflows/${TARGET.workflow}`;
const ACTIVE_STATUSES = new Set(["queued", "in_progress", "waiting", "pending", "requested"]);
const formatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: TIMEZONE,
  year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});

export function argentinaTime(timestamp) {
  if (!Number.isFinite(timestamp)) throw new Error("invalid_clock");
  const parts = Object.fromEntries(formatter.formatToParts(new Date(timestamp))
    .filter(part => part.type !== "literal").map(part => [part.type, part.value]));
  return {
    hour: Number(parts.hour),
    date: `${parts.year}-${parts.month}-${parts.day}`,
    time: `${parts.hour}:${parts.minute}:${parts.second}`,
  };
}

export function insideWindow(timestamp) {
  const { hour } = argentinaTime(timestamp);
  return hour >= 15 || hour === 0;
}

function parseTimestamp(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value)) {
    throw new Error("invalid_history");
  }
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) throw new Error("invalid_history");
  return timestamp;
}

export function checkRuns(payload, now) {
  if (!payload || !Number.isSafeInteger(payload.total_count) || payload.total_count < 0 ||
      !Array.isArray(payload.workflow_runs) || payload.workflow_runs.length > 100) {
    throw new Error("invalid_history");
  }
  // No third request/pagination: an incomplete daily history prevents a blind dispatch.
  if (payload.total_count !== payload.workflow_runs.length) throw new Error("incomplete_history");
  const runs = [];
  for (const run of payload.workflow_runs) {
    if (!run || typeof run.head_branch !== "string") throw new Error("invalid_history");
    if (run.head_branch !== TARGET.branch) continue;
    if (!Number.isSafeInteger(run.id) || run.id <= 0 ||
        (!ACTIVE_STATUSES.has(run.status) && run.status !== "completed")) {
      throw new Error("invalid_history");
    }
    const created = parseTimestamp(run.created_at);
    const started = run.run_started_at == null ? created : parseTimestamp(run.run_started_at);
    const latest = Math.max(created, started);
    if (latest > now + 60_000) throw new Error("invalid_history");
    runs.push({ id: run.id, status: run.status, ageMs: Math.max(0, now - latest) });
  }
  // A long-running/queued job wins over a recent completed job, even if it is older than 8 min.
  const active = runs.find(run => ACTIVE_STATUSES.has(run.status));
  if (active) return { event: "SKIP_ACTIVE_RUN", runId: active.id };
  const recent = runs.filter(run => run.ageMs <= RECENT_RUN_MS)
    .sort((a, b) => a.ageMs - b.ageMs)[0];
  if (recent) return {
    event: "SKIP_RECENT_RUN", runId: recent.id,
    ageMinutes: Math.round(recent.ageMs / 600) / 100,
  };
  return null;
}

// Deliberately never log response bodies, exception messages, headers, env or request objects.
function logToConsole(event, fields) {
  console.log("MBOX_SCHEDULER", JSON.stringify({ event, ...fields }));
}

async function request(fetchImpl, url, init, parseJson, timeoutMs) {
  const controller = new AbortController();
  let timer;
  let timedOut = false;
  try {
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
        reject(new Error("timeout"));
      }, timeoutMs);
    });
    const operation = (async () => {
      const response = await fetchImpl(url, { ...init, redirect: "manual", signal: controller.signal });
      if (!Number.isInteger(response.status) || response.status < 100 || response.status > 599) {
        return { status: 0, reason: "invalid_response" };
      }
      if (parseJson && response.status === 200) {
        try {
          return { status: response.status, payload: await response.json() };
        } catch {
          return { status: response.status, reason: "invalid_response" };
        }
      }
      return { status: response.status };
    })();
    return await Promise.race([operation, timeout]);
  } catch {
    return { status: 0, reason: timedOut ? "timeout" : "network_error" };
  } finally {
    clearTimeout(timer);
  }
}

export function createScheduler({
  fetchImpl = (...args) => globalThis.fetch(...args),
  now = () => Date.now(),
  log = logToConsole,
  timeoutMs = REQUEST_TIMEOUT_MS,
} = {}) {
  let inFlight = false;
  let lastDispatchAt = null;
  return {
    async tick(env, scheduledTime = null) {
      const timestamp = now();
      log("TICK", { scheduledTime: Number.isFinite(scheduledTime) ? scheduledTime : null });
      let local;
      try { local = argentinaTime(timestamp); } catch {
        log("GITHUB_CHECK_FAILED", { status: 0, reason: "invalid_clock" });
        return { event: "GITHUB_CHECK_FAILED" };
      }
      log("LOCAL_TIME", { date: local.date, time: local.time, timezone: TIMEZONE });
      if (!insideWindow(timestamp)) {
        log("SKIP_OUTSIDE_WINDOW", {});
        return { event: "SKIP_OUTSIDE_WINDOW" };
      }
      if (inFlight) {
        log("SKIP_ACTIVE_RUN", { source: "worker_isolate" });
        return { event: "SKIP_ACTIVE_RUN" };
      }
      if (lastDispatchAt !== null && timestamp - lastDispatchAt >= 0 && timestamp - lastDispatchAt <= RECENT_RUN_MS) {
        const fields = { source: "worker_isolate", ageMinutes: Math.round((timestamp - lastDispatchAt) / 600) / 100 };
        log("SKIP_RECENT_RUN", fields);
        return { event: "SKIP_RECENT_RUN", ...fields };
      }
      if (typeof env?.GITHUB_TOKEN !== "string" || !env.GITHUB_TOKEN.trim()) {
        log("GITHUB_CHECK_FAILED", { status: 0, reason: "missing_secret" });
        return { event: "GITHUB_CHECK_FAILED" };
      }
      inFlight = true;
      try {
        const headers = {
          Accept: "application/vnd.github+json",
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          "User-Agent": "MBox-Live-Scheduler",
          "X-GitHub-Api-Version": "2026-03-10",
        };
        const url = new URL(`${WORKFLOW_URL}/runs`);
        url.searchParams.set("branch", TARGET.branch);
        url.searchParams.set("per_page", "100");
        url.searchParams.set("created", `>=${new Date(timestamp - HISTORY_WINDOW_MS).toISOString()}`);
        url.searchParams.set("exclude_pull_requests", "true");
        log("CHECK_RUNS", {});
        const check = await request(fetchImpl, url.toString(), { method: "GET", headers }, true, timeoutMs);
        if (check.status !== 200 || check.reason) {
          log("GITHUB_CHECK_FAILED", { status: check.status, reason: check.reason || "http_error" });
          return { event: "GITHUB_CHECK_FAILED" };
        }
        let skip;
        try { skip = checkRuns(check.payload, timestamp); } catch {
          log("GITHUB_CHECK_FAILED", { status: 200, reason: "invalid_or_incomplete_history" });
          return { event: "GITHUB_CHECK_FAILED" };
        }
        if (skip) {
          const { event, ...fields } = skip;
          log(event, fields);
          return skip;
        }
        // A request may straddle 01:00; eligibility must still hold immediately before dispatch.
        const dispatchTime = now();
        if (!insideWindow(dispatchTime)) {
          log("SKIP_OUTSIDE_WINDOW", {});
          return { event: "SKIP_OUTSIDE_WINDOW" };
        }
        log("DISPATCH_START", {});
        const dispatch = await request(fetchImpl, `${WORKFLOW_URL}/dispatches`, {
          method: "POST", headers: { ...headers, "Content-Type": "application/json" },
          body: JSON.stringify({ ref: TARGET.branch }),
        }, false, timeoutMs);
        if (dispatch.status === 204 || dispatch.status === 200) {
          lastDispatchAt = dispatchTime;
          log("DISPATCH_SUCCESS", { status: dispatch.status });
          return { event: "DISPATCH_SUCCESS" };
        }
        log("DISPATCH_FAILED", { status: dispatch.status, reason: dispatch.reason || "http_error" });
        return { event: "DISPATCH_FAILED" };
      } finally {
        inFlight = false;
      }
    },
  };
}
