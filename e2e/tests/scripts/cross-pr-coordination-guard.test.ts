import { execFileSync, spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const repoRoot = resolve(import.meta.dirname, "../../..");
const script = resolve(repoRoot, ".github/scripts/cross_pr_coordination_guard.py");
const fixture = resolve(repoRoot, ".github/fixtures/cross-pr-coordination-history.json");

describe("cross-PR coordination guard", () => {
  it("replays the documented positive and negative history", () => {
    const result = JSON.parse(execFileSync("python3", [script, "--self-check"], {
      cwd: repoRoot,
      encoding: "utf8",
    }));

    expect(result.mode).toBe("shadow");
    expect(result.findings.map((finding: { signal: string; prs: number[] }) => [
      finding.signal,
      finding.prs,
    ])).toEqual([
      ["COMPETING_IMPLEMENTATIONS", [7446, 7521]],
      ["REVIEW_CONTRADICTION", [7446, 7521]],
      ["DUPLICATE_VALIDATION", [7446, 7521]],
      ["COMPETING_IMPLEMENTATIONS", [7550, 7552]],
      ["DUPLICATE_VALIDATION", [7550, 7552]],
      ["COMPETING_IMPLEMENTATIONS", [7603, 7605]],
      ["DUPLICATE_VALIDATION", [7603, 7605]],
      ["COMPETING_IMPLEMENTATIONS", [7701, 7702]],
    ]);
  });

  it("fails fixture validation when an expected signal is missing", () => {
    const fixtureData = JSON.parse(readFileSync(fixture, "utf8"));
    fixtureData.expected_findings.push({
      signal: "REVIEW_CONTRADICTION",
      prs: [7603, 7605],
    });
    const invalidFixture = join(mkdtempSync(join(tmpdir(), "od-cross-pr-guard-")), "fixture.json");
    writeFileSync(invalidFixture, `${JSON.stringify(fixtureData)}\n`);
    const result = spawnSync("python3", [script, "--fixture", invalidFixture, "--strict"], {
      cwd: repoRoot,
      encoding: "utf8",
    });

    expect(result.status).toBe(1);
    expect(result.stderr).toContain("fixture findings differ");
  });
});
