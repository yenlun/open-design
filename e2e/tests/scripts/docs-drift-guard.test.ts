import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const repoRoot = resolve(import.meta.dirname, "../../..");
const script = resolve(repoRoot, ".github/scripts/docs_drift_guard.py");

describe("documentation drift guard", () => {
  it("replays the #7513 TypeScript-first contradiction without flagging aligned text", () => {
    const result = JSON.parse(execFileSync("python3", [script, "--self-check"], {
      cwd: repoRoot,
      encoding: "utf8",
    }));

    expect(result).toMatchObject({
      mode: "shadow",
      findings: [
        {
          rule: "typescript-first-daemon",
          document: { path: "CONTRIBUTING.md", line: 1 },
        },
      ],
    });
  });

  it("can observe the repository without assuming a stale claim remains forever", () => {
    const result = JSON.parse(execFileSync("python3", [script, "--root", repoRoot], {
      cwd: repoRoot,
      encoding: "utf8",
    }));
    const paths = result.findings.map((finding: { document: { path: string } }) => finding.document.path);

    expect(result).toMatchObject({ schema_version: 1, mode: "shadow" });
    expect(
      result.findings.every((finding: { rule: string }) => finding.rule === "typescript-first-daemon"),
    ).toBe(true);
    expect(paths).not.toContain("docs/i18n/CONTRIBUTING.fr.md");
  });
});
