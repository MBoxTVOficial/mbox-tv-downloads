import { readFileSync, readdirSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { join } from "node:path";

const configuration = readFileSync("wrangler.toml", "utf8");
if (/GITHUB_TOKEN\s*=/.test(configuration)) throw new Error("Secret must not be in wrangler.toml");
if (!/crons\s*=\s*\["\*\/10 \* \* \* \*"\]/.test(configuration) ||
    !/workers_dev\s*=\s*false/.test(configuration) || !/preview_urls\s*=\s*false/.test(configuration)) {
  throw new Error("Unexpected scheduler configuration");
}
for (const directory of ["src", "scripts", "tests"]) {
  for (const file of readdirSync(directory).filter(name => name.endsWith(".js"))) {
    const text = readFileSync(join(directory, file), "utf8");
    if (/(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{20,}/.test(text)) {
      throw new Error(`Credential check failed: ${join(directory, file)}`);
    }
    // The checker contains its own forbidden strings; they are not dependencies of the Worker.
    if (file !== "check.js" && /api[-.]football|player_api\.php|API_FOOTBALL_KEY/i.test(text)) {
      throw new Error(`Unexpected service dependency: ${join(directory, file)}`);
    }
  }
}
for (const directory of ["src", "scripts", "tests"]) {
  for (const file of readdirSync(directory).filter(name => name.endsWith(".js"))) {
    execFileSync(process.execPath, ["--check", join(directory, file)], { stdio: "inherit" });
  }
}
console.log("Syntax, target configuration and credential checks OK.");
