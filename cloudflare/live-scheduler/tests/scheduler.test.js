import test from "node:test";
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import worker from "../src/index.js";
import {
  argentinaTime, insideWindow, checkRuns, createScheduler,
  REQUEST_TIMEOUT_MS, RECENT_RUN_MS, TARGET,
} from "../src/scheduler.js";

const art = value => Date.parse(`${value}-03:00`);
const NOW = art("2026-10-08T21:00:00");
const env = () => ({ GITHUB_TOKEN: randomBytes(32).toString("hex") });
const response = (runs = [], total = runs.length) => Response.json({ total_count: total, workflow_runs: runs });
const run = (ageMinutes, status = "completed", extra = {}) => ({
  id: 100 + ageMinutes, head_branch: "main", status,
  created_at: new Date(NOW - ageMinutes * 60_000).toISOString(), run_started_at: null, ...extra,
});

function harness({ timestamp = NOW, replies = [response(), new Response(null, { status: 204 })], timeoutMs = 8_000 } = {}) {
  const calls = [], logs = [];
  const scheduler = createScheduler({
    now: () => timestamp,
    log: (event, fields) => logs.push({ event, ...fields }),
    timeoutMs,
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      const next = replies.shift();
      if (next instanceof Error) throw next;
      if (typeof next === "function") return next(options);
      assert.ok(next, "Unexpected extra request");
      return next;
    },
  });
  return { scheduler, calls, logs };
}

for (const [time, eligible] of [
  ["14:50:00", false], ["15:00:00", true], ["23:50:00", true],
  ["00:50:00", true], ["01:00:00", false], ["00:59:59", true], ["14:59:59", false],
]) {
  test(`${time} ART: ${eligible ? "eligible" : "outside; zero GitHub requests"}`, async () => {
    const timestamp = art(`2026-10-08T${time}`);
    assert.equal(insideWindow(timestamp), eligible);
    const h = harness({ timestamp });
    const result = await h.scheduler.tick(env());
    assert.equal(result.event, eligible ? "DISPATCH_SUCCESS" : "SKIP_OUTSIDE_WINDOW");
    assert.equal(h.calls.length, eligible ? 2 : 0);
  });
}

test("explicit timezone converts UTC date crossing midnight, independent of machine locale", () => {
  assert.deepEqual(argentinaTime(Date.parse("2026-10-09T02:50:00Z")), { hour: 23, date: "2026-10-08", time: "23:50:00" });
  assert.deepEqual(argentinaTime(Date.parse("2026-10-09T03:00:00Z")), { hour: 0, date: "2026-10-09", time: "00:00:00" });
});

for (const status of ["queued", "in_progress", "waiting", "pending", "requested"]) {
  test(`active ${status}, including older than 8 minutes: no dispatch`, async () => {
    const h = harness({ replies: [response([run(40, status)])] });
    const result = await h.scheduler.tick(env());
    assert.equal(result.event, "SKIP_ACTIVE_RUN");
    assert.equal(result.runId, 140);
    assert.equal(h.calls.length, 1);
  });
}

test("completed run four minutes ago: no dispatch", async () => {
  const h = harness({ replies: [response([run(4)])] });
  assert.deepEqual(await h.scheduler.tick(env()), { event: "SKIP_RECENT_RUN", runId: 104, ageMinutes: 4 });
  assert.equal(h.calls.length, 1);
});

test("exactly eight minutes is still deduplicated", () => {
  assert.equal(checkRuns({ total_count: 1, workflow_runs: [run(8)] }, NOW).event, "SKIP_RECENT_RUN");
  assert.equal(RECENT_RUN_MS, 480_000);
});

test("nine-minute completed run allows dispatch", async () => {
  const h = harness({ replies: [response([run(9)]), new Response(null, { status: 204 })] });
  assert.equal((await h.scheduler.tick(env())).event, "DISPATCH_SUCCESS");
  assert.equal(h.calls.length, 2);
});

test("recent start takes precedence over old creation time", () => {
  const result = checkRuns({ total_count: 1, workflow_runs: [run(90, "completed", {
    run_started_at: new Date(NOW - 4 * 60_000).toISOString(),
  })] }, NOW);
  assert.equal(result.ageMinutes, 4);
  assert.equal(result.event, "SKIP_RECENT_RUN");
});

test("active run takes priority over recent completed run", () => {
  const result = checkRuns({ total_count: 2, workflow_runs: [run(1), run(90, "in_progress")] }, NOW);
  assert.equal(result.event, "SKIP_ACTIVE_RUN");
  assert.equal(result.runId, 190);
});

test("all events are considered; another branch does not prevent a main dispatch", async () => {
  const h = harness({ replies: [response([run(1, "queued", { head_branch: "other" })]), new Response(null, { status: 204 })] });
  assert.equal((await h.scheduler.tick(env())).event, "DISPATCH_SUCCESS");
  const url = new URL(h.calls[0].url);
  assert.equal(url.searchParams.get("event"), null);
  assert.equal(url.searchParams.get("branch"), "main");
  assert.equal(url.searchParams.get("per_page"), "100");
  assert.match(url.searchParams.get("created"), /^>=/);
});

for (const status of [401, 403, 404, 422, 429, 500, 502, 503]) {
  test(`GitHub check ${status}: fail closed, no POST`, async () => {
    const h = harness({ replies: [new Response("not logged", { status })] });
    assert.equal((await h.scheduler.tick(env())).event, "GITHUB_CHECK_FAILED");
    assert.equal(h.calls.length, 1);
    assert.equal(h.logs.at(-1).status, status);
  });
  test(`dispatch ${status}: safe failure, never retry`, async () => {
    const h = harness({ replies: [response(), new Response("not logged", { status })] });
    assert.equal((await h.scheduler.tick(env())).event, "DISPATCH_FAILED");
    assert.equal(h.calls.length, 2);
    assert.equal(h.logs.at(-1).status, status);
  });
}

test("check timeout cancels request and prevents dispatch", async () => {
  const h = harness({ timeoutMs: 10, replies: [() => new Promise(() => {})] });
  assert.equal((await h.scheduler.tick(env())).event, "GITHUB_CHECK_FAILED");
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].options.signal.aborted, true);
  assert.equal(h.logs.at(-1).reason, "timeout");
  assert.equal(REQUEST_TIMEOUT_MS, 8_000);
});

test("timeout also covers a stalled response body", async () => {
  const h = harness({ timeoutMs: 10, replies: [() => ({ status: 200, json: () => new Promise(() => {}) })] });
  assert.equal((await h.scheduler.tick(env())).event, "GITHUB_CHECK_FAILED");
  assert.equal(h.logs.at(-1).reason, "timeout");
});

test("ambiguous POST timeout is not immediately retried", async () => {
  const h = harness({ timeoutMs: 10, replies: [response(), () => new Promise(() => {})] });
  assert.equal((await h.scheduler.tick(env())).event, "DISPATCH_FAILED");
  assert.equal(h.calls.length, 2);
  assert.equal(h.logs.at(-1).reason, "timeout");
});

test("204 success uses exact target, ref main and no discovery inputs", async () => {
  const h = harness();
  const credentials = env();
  assert.equal((await h.scheduler.tick(credentials)).event, "DISPATCH_SUCCESS");
  const post = h.calls[1];
  assert.equal(post.url, "https://api.github.com/repos/MBoxTVOficial/mbox-tv-downloads/actions/workflows/update-sports-live.yml/dispatches");
  assert.equal(post.options.method, "POST");
  assert.deepEqual(JSON.parse(post.options.body), { ref: "main" });
  assert.equal(post.options.headers.Authorization, `Bearer ${credentials.GITHUB_TOKEN}`);
  assert.equal(post.options.redirect, "manual");
  assert.equal(TARGET.workflow, "update-sports-live.yml");
});

test("200 dispatch response is also supported", async () => {
  const h = harness({ replies: [response(), Response.json({ workflow_run_id: 1 })] });
  assert.equal((await h.scheduler.tick(env())).event, "DISPATCH_SUCCESS");
});

test("redirect is never followed with credentials", async () => {
  const h = harness({ replies: [new Response(null, { status: 302, headers: { Location: "https://example.invalid" } })] });
  assert.equal((await h.scheduler.tick(env())).event, "GITHUB_CHECK_FAILED");
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].options.redirect, "manual");
});

test("missing secret means zero requests", async () => {
  for (const credentials of [{}, { GITHUB_TOKEN: "" }, { GITHUB_TOKEN: "  " }]) {
    const h = harness();
    assert.equal((await h.scheduler.tick(credentials)).event, "GITHUB_CHECK_FAILED");
    assert.equal(h.calls.length, 0);
  }
});

for (const payload of [null, {}, { total_count: 1, workflow_runs: [] },
  { total_count: 101, workflow_runs: [run(9)] },
  { total_count: 1, workflow_runs: [run(9, "unexpected")] },
  { total_count: 1, workflow_runs: [run(9, "completed", { created_at: "invalid" })] },
  { total_count: 1, workflow_runs: [run(9, "completed", { id: true })] }]) {
  test(`malformed/incomplete history fails closed: ${JSON.stringify(payload)}`, async () => {
    const h = harness({ replies: [Response.json(payload)] });
    assert.equal((await h.scheduler.tick(env())).event, "GITHUB_CHECK_FAILED");
    assert.equal(h.calls.length, 1);
  });
}

test("invalid JSON body cannot trigger a blind dispatch", async () => {
  const h = harness({ replies: [new Response("not JSON", { status: 200 })] });
  assert.equal((await h.scheduler.tick(env())).event, "GITHUB_CHECK_FAILED");
  assert.equal(h.calls.length, 1);
});

test("network exceptions containing a generated secret never enter logs", async () => {
  const credentials = env();
  const h = harness({ replies: [new Error(`Authorization ${credentials.GITHUB_TOKEN} sensitive cookie`)] });
  assert.equal((await h.scheduler.tick(credentials)).event, "GITHUB_CHECK_FAILED");
  const output = JSON.stringify(h.logs);
  assert.equal(output.includes(credentials.GITHUB_TOKEN), false);
  assert.equal(output.includes("Authorization"), false);
  assert.equal(output.includes("cookie"), false);
});

test("dispatch response body containing sensitive text is never logged", async () => {
  const credentials = env();
  const h = harness({ replies: [response(), new Response(credentials.GITHUB_TOKEN, { status: 422 })] });
  assert.equal((await h.scheduler.tick(credentials)).event, "DISPATCH_FAILED");
  assert.equal(JSON.stringify(h.logs).includes(credentials.GITHUB_TOKEN), false);
});

test("concurrent ticks within one isolate cannot double-dispatch", async () => {
  let resolveCheck;
  const h = harness({ replies: [() => new Promise(resolve => { resolveCheck = resolve; }), new Response(null, { status: 204 })] });
  const first = h.scheduler.tick(env());
  assert.equal((await h.scheduler.tick(env())).event, "SKIP_ACTIVE_RUN");
  resolveCheck(response());
  assert.equal((await first).event, "DISPATCH_SUCCESS");
  assert.equal(h.calls.length, 2);
});

test("recent successful dispatch is deduped even before GitHub history catches up", async () => {
  const h = harness();
  assert.equal((await h.scheduler.tick(env())).event, "DISPATCH_SUCCESS");
  assert.equal((await h.scheduler.tick(env())).event, "SKIP_RECENT_RUN");
  assert.equal(h.calls.length, 2);
});

test("an error releases the isolate lock for the next cron", async () => {
  const h = harness({ replies: [new Response(null, { status: 403 }), response(), new Response(null, { status: 204 })] });
  assert.equal((await h.scheduler.tick(env())).event, "GITHUB_CHECK_FAILED");
  assert.equal((await h.scheduler.tick(env())).event, "DISPATCH_SUCCESS");
  assert.equal(h.calls.length, 3); // Across two ticks: 1 + 2, never >2 per tick.
});

test("crossing 01:00 during GET prevents a POST outside the window", async () => {
  let timestamp = art("2026-10-09T00:59:59");
  let calls = 0;
  const scheduler = createScheduler({ now: () => timestamp, log: () => {}, fetchImpl: async () => {
    calls++;
    timestamp = art("2026-10-09T01:00:01");
    return response();
  } });
  assert.equal((await scheduler.tick(env())).event, "SKIP_OUTSIDE_WINDOW");
  assert.equal(calls, 1);
});

test("a delayed invocation uses actual local time, not obsolete scheduledTime", async () => {
  const h = harness({ timestamp: art("2026-10-09T01:10:00") });
  assert.equal((await h.scheduler.tick(env(), NOW)).event, "SKIP_OUTSIDE_WINDOW");
  assert.equal(h.calls.length, 0);
});

test("all public HTTP paths and methods are informational only", async () => {
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => { calls++; return response(); };
  try {
    for (const method of ["GET", "POST", "PUT", "DELETE"]) {
      const result = await worker.fetch(new Request("https://example.invalid/dispatch", { method }), env());
      assert.equal(result.status, 200);
      assert.deepEqual(await result.json(), { service: "mbox-live-scheduler", status: "ok" });
    }
    assert.equal(calls, 0);
  } finally { globalThis.fetch = original; }
});

test("Cloudflare scheduled entry calls the same checked pipeline and disables implicit retries", async () => {
  const originalFetch = globalThis.fetch, originalNow = Date.now, originalLog = console.log;
  const credentials = env();
  const logs = [];
  let calls = 0, noRetries = 0;
  Date.now = () => NOW;
  console.log = (...args) => logs.push(args);
  globalThis.fetch = async () => ++calls === 1 ? response() : new Response(null, { status: 204 });
  try {
    await worker.scheduled({ scheduledTime: NOW, noRetry() { noRetries++; } }, credentials);
    assert.equal(calls, 2);
    assert.equal(noRetries, 1);
    assert.equal(JSON.stringify(logs).includes(credentials.GITHUB_TOKEN), false);
    assert.ok(JSON.stringify(logs).includes("DISPATCH_SUCCESS"));
  } finally {
    globalThis.fetch = originalFetch;
    Date.now = originalNow;
    console.log = originalLog;
  }
});
