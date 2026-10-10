import { createScheduler } from "./scheduler.js";

const scheduler = createScheduler();
export default {
  async scheduled(controller, env) {
    // No immediate retries: an ambiguous POST timeout may already have created a GitHub run.
    controller.noRetry();
    await scheduler.tick(env, controller.scheduledTime);
  },
  async fetch() {
    // Every path and method is informational. HTTP can never call tick()/GitHub.
    return Response.json({ service: "mbox-live-scheduler", status: "ok" }, {
      headers: { "Cache-Control": "no-store" },
    });
  },
};
