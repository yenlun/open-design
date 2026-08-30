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

  it("detects issue-linked subset fixes and differently worded overlapping fixes", () => {
    const makePr = (number: number, title: string, body: string, files: Record<string, string>,
      labels: string[] = [], review?: "APPROVED" | "CHANGES_REQUESTED") => ({
      number,
      title,
      body,
      state: "open",
      draft: false,
      head_sha: `${number}`.padStart(40, "0"),
      labels,
      files,
      reviews: review ? [{ reviewer: `reviewer-${number}`, state: review,
        commit_id: `${number}`.padStart(40, "0"), submitted_at: "2026-08-30T00:00:00Z" }] : [],
    });
    const fixtureData = {
      schema_version: 1,
      pull_requests: [
        makePr(1001, "fix: preserve explicit radius", "Fixes #900", { "a.ts": "+ narrow" },
          ["needs-validation"], "APPROVED"),
        makePr(1002, "fix: preserve imported tokens", "Fixes #900", {
          "a.ts": "+ broad", "b.ts": "+ b", "c.ts": "+ c", "d.ts": "+ d", "e.ts": "+ e",
        }, ["needs-validation"], "CHANGES_REQUESTED"),
        makePr(1003, "fix: find the real body for preview bridge injection", "", {
          "route.ts": "+ first", "first.test.ts": "+ test",
        }),
        makePr(1004, "fix: locate preview bridge injection points structurally", "", {
          "route.ts": "+ second", "second.test.ts": "+ test",
        }),
      ],
      expected_findings: [
        { signal: "COMPETING_IMPLEMENTATIONS", prs: [1001, 1002] },
        { signal: "REVIEW_CONTRADICTION", prs: [1001, 1002] },
        { signal: "DUPLICATE_VALIDATION", prs: [1001, 1002] },
        { signal: "COMPETING_IMPLEMENTATIONS", prs: [1003, 1004] },
      ],
    };
    const regressionFixture = join(mkdtempSync(join(tmpdir(), "od-cross-pr-v2-")), "fixture.json");
    writeFileSync(regressionFixture, `${JSON.stringify(fixtureData)}\n`);

    const result = spawnSync("python3", [script, "--fixture", regressionFixture, "--strict"], {
      cwd: repoRoot,
      encoding: "utf8",
    });

    expect(result.status, result.stderr).toBe(0);
  });
});
