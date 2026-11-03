#!/usr/bin/env node
/**
 * Fixture builder.
 *
 * Everything under apps/console/src/fixtures/ that is *derived* from real
 * repository artifacts is produced here, so the console never renders invented
 * shapes: it renders the same JSON the pipeline actually emits.
 *
 * Sources (read-only):
 *   frontend/apps/console/src/fixtures/report-supported.json   real `traceops.cli.demo` output
 *   evaluation/reports/latest.json          12 scenarios x 27 metrics
 *   evaluation/reports/ablation.json        E1..E5 arms
 *   frontend/apps/console/src/fixtures/topology.json
 *
 * The two evaluation reports are produced locally by the Python harness and are
 * gitignored, so they may be absent (CI, a fresh clone). Sources that are
 * missing are skipped rather than treated as an error: the committed fixture
 * stays as it is and the drift check only covers what it can actually rebuild.
 *
 * Hand-authored fixtures (incidents, progress, health) are committed directly
 * and intentionally not regenerated.
 *
 *   node scripts/build-fixtures.mjs           write
 *   node scripts/build-fixtures.mjs --check   exit 1 if out of date
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// This is a CLI: printing to stdout/stderr is the whole point.
const say = (line) => process.stdout.write(`${line}\n`);
const warn = (line) => process.stderr.write(`${line}\n`);

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, "../..");
const out = join(here, "../apps/console/src/fixtures");
const checkOnly = process.argv.includes("--check");

mkdirSync(out, { recursive: true });

const read = (p) => JSON.parse(readFileSync(join(repo, p), "utf8"));
const clone = (v) => JSON.parse(JSON.stringify(v));

/** Deterministic stringify: stable key order, 2-space indent, trailing newline. */
function stringify(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

const written = [];
const skipped = [];

function emit(name, value, volatile = []) {
  const path = join(out, name);
  const next = stringify(value);
  written.push([path, next]);
  if (checkOnly) {
    const prev = existsSync(path) ? readFileSync(path, "utf8") : "";
    if (prev !== next) {
      const prevValue = prev ? JSON.parse(prev) : {};
      const same =
        JSON.stringify(stripVolatile(prevValue, volatile)) ===
        JSON.stringify(stripVolatile(value, volatile));
      if (!same) {
        warn(`stale: ${name}`);
        process.exitCode = 1;
      }
    }
    return;
  }
  writeFileSync(path, next);
}

/** Drop the keys that change every time the harness runs. */
function stripVolatile(value, keys) {
  if (keys.length === 0) return value;
  const copy = { ...value };
  for (const key of keys) delete copy[key];
  return copy;
}

/**
 * Emit only when the source artifact is actually present.
 *
 * `volatile` lists top-level keys that legitimately differ between two runs of
 * the same harness (timestamps, run ids). The fixture keeps them — a reader
 * wants to know when the numbers were produced — but the drift check compares
 * without them, so re-running the harness locally is not reported as drift.
 */
function emitFromSource(name, repoPath, volatile = []) {
  if (!existsSync(join(repo, repoPath))) {
    skipped.push(repoPath);
    return;
  }
  const value = read(repoPath);
  emit(name, value, volatile);
}

// ─────────────────────────────────────────────────────────────────────────────
// Report: one real run, plus the three other verdict shapes the UI must handle.
// The variants keep identical evidence so the workbench layout is exercised
// with the same data volume under different conclusions.
// ─────────────────────────────────────────────────────────────────────────────

const report = read("frontend/apps/console/src/fixtures/report-supported.json");

/**
 * The variants keep identical evidence so the workbench layout is exercised
 * with the same data volume under different conclusions.
 *
 * `investigation_run_id` has to be rewritten too: otherwise every variant
 * still claims to be the original run, and the console shows the wrong run id
 * next to the wrong verdict.
 */
const withVerdict = (status, runId, mutate) => {
  const r = clone(report);
  r.diagnosis_status = status;
  r.investigation_run_id = runId;
  mutate(r);
  return r;
};

emit("report-supported.json", report);

emit(
  "report-refuted.json",
  withVerdict("REFUTED", "run-e5b118", (r) => {
    r.abstained = false;
    r.summary =
      "Refuted: payment-service / payment-gateway (upstream_timeout). " +
      "The candidate survived candidate generation but its predicted observations " +
      "did not hold in the window — redis pool saturation explains the same symptom " +
      "at an earlier timestamp, and the gateway's own error rate stayed flat.";
    r.hypotheses = (r.hypotheses ?? []).map((h, i) => ({
      ...h,
      status: i === 0 ? "refuted" : "supported",
      rank: i === 0 ? 2 : 1,
    }));
    r.root_cause_time = "2026-03-12T14:29:00Z";
    r.no_data = [
      ...(r.no_data ?? []),
      "query_service_logs[payment-gateway]: query succeeded but returned no data",
    ];
  }),
);

emit(
  "report-inconclusive.json",
  withVerdict("INCONCLUSIVE", "run-2b9e04", (r) => {
    r.abstained = false;
    r.summary =
      "Inconclusive: two candidates remain compatible with the observed evidence " +
      "(redis pool exhaustion, payment-service thread starvation). The discriminator " +
      "was not collectable in this window.";
    r.hypotheses = (r.hypotheses ?? []).map((h) => ({ ...h, status: "inconclusive" }));
    r.missing_data = [
      ...(r.missing_data ?? []),
      "redis INFO clients.highest_priority: metrics exporter not registered for redis",
      "payment-service dataclass GC pause histogram: not exported in this window",
    ];
    r.propagation_chain = [];
  }),
);

emit(
  "report-abstained.json",
  withVerdict("INCONCLUSIVE", "run-1c4a6e", (r) => {
    r.abstained = true;
    r.summary =
      "Abstained: evidence collected was insufficient to rank any candidate above " +
      "the abstention threshold. Returning a guess here would be a fabricated answer.";
    r.hypotheses = (r.hypotheses ?? []).map((h) => ({
      ...h,
      status: "inconclusive",
      rank: null,
    }));
    r.missing_data = [
      "trace backend unreachable for 4 of 5 windows (connection reset)",
      ...(r.missing_data ?? []),
    ];
    r.rejected_proposals = [
      {
        reason_code: "INSUFFICIENT_EVIDENCE",
        proposal: "redis / redis-connection-pool / pool_exhaustion",
        detail: "supported by 1 of 2 required independent evidence items",
      },
    ];
  }),
);

// ─────────────────────────────────────────────────────────────────────────────
// Evaluation: verbatim copies — the lab renders whatever the harness wrote.
// ─────────────────────────────────────────────────────────────────────────────

emitFromSource("evaluation-latest.json", "evaluation/reports/latest.json", ["generated_at"]);
emitFromSource("evaluation-ablation.json", "evaluation/reports/ablation.json", [
  "generated_at",
]);

// ─────────────────────────────────────────────────────────────────────────────
// Topology: the upstream OpenTelemetry demo graph.
// ─────────────────────────────────────────────────────────────────────────────

emitFromSource("topology.json", "frontend/apps/console/src/fixtures/topology.json");

if (!checkOnly) {
  for (const [path] of written) say(`wrote ${path.replace(`${repo}/`, "")}`);
  for (const path of skipped) say(`skipped ${path} (not present — fixture left untouched)`);
}
