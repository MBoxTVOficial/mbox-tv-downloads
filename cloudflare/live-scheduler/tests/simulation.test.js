import test from "node:test";
import assert from "node:assert/strict";
import { simulateEvening } from "../scripts/simulate.js";

test("complete variable evening: 60 ticks, 60 checks, 20 dispatches and 40 dedupe skips", async () => {
  assert.deepEqual(await simulateEvening(), {
    ticks: 60, githubChecks: 60, dispatches: 20,
    skippedActive: 20, skippedRecent: 20, dedupeSkips: 40, githubRequests: 80,
  });
});
test("maximum evening: 60 ticks and at most 120 GitHub requests", async () => {
  const result = await simulateEvening({ variableRuns: false });
  assert.equal(result.ticks, 60);
  assert.equal(result.githubChecks, 60);
  assert.equal(result.dispatches, 60);
  assert.equal(result.githubRequests, 120);
});
