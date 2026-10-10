import { randomBytes } from "node:crypto";
import { fileURLToPath } from "node:url";
import { createScheduler, insideWindow } from "../src/scheduler.js";

/** Entire evening, fake GitHub only. No credentials/files/network from the environment. */
export async function simulateEvening({ variableRuns = true } = {}) {
  const start = Date.parse("2026-10-08T15:00:00-03:00");
  let current = start;
  const totals = { ticks: 0, githubChecks: 0, dispatches: 0, skippedActive: 0, skippedRecent: 0, dedupeSkips: 0 };
  const scheduler = createScheduler({ now: () => current, log: () => {}, fetchImpl: async (_, request) => {
    if (request.method === "POST") {
      totals.dispatches++;
      return new Response(null, { status: 204 });
    }
    totals.githubChecks++;
    const slot = totals.ticks - 1;
    // Repeat three situations: no recent job, recently completed job, long active job.
    const mode = variableRuns ? slot % 3 : 0;
    const runs = mode === 0 ? [] : [{
      id: slot + 1, head_branch: "main",
      status: mode === 1 ? "completed" : "in_progress",
      created_at: new Date(current - (mode === 1 ? 4 : 20) * 60_000).toISOString(),
      run_started_at: null,
    }];
    return Response.json({ total_count: runs.length, workflow_runs: runs });
  } });
  const credentials = { GITHUB_TOKEN: randomBytes(32).toString("hex") };
  for (let slot = 0; slot < 60; slot++) {
    current = start + slot * 10 * 60_000;
    if (!insideWindow(current)) throw new Error("Simulation outside configured window");
    totals.ticks++;
    const { event } = await scheduler.tick(credentials, current);
    if (event === "SKIP_ACTIVE_RUN") totals.skippedActive++;
    if (event === "SKIP_RECENT_RUN") totals.skippedRecent++;
  }
  totals.dedupeSkips = totals.skippedActive + totals.skippedRecent;
  totals.githubRequests = totals.githubChecks + totals.dispatches;
  return totals;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  console.log(JSON.stringify({
    window: "15:00..00:50 America/Argentina/Buenos_Aires",
    variableRuns: await simulateEvening(),
    maximum: await simulateEvening({ variableRuns: false }),
  }, null, 2));
}
