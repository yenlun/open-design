# Documentation drift guard (shadow)

## Why this exists

Contributor-facing docs can silently fall out of sync with what the repository actually enforces. This already happened: [#7513](https://github.com/nexu-io/open-design/pull/7513) found that `CONTRIBUTING.md` told contributors the daemon (`apps/daemon/`) was "plain ESM JavaScript", while `AGENTS.md` and `scripts/guard.ts` both require project-owned source to be TypeScript-first — and the fix still had to go back for a second round before all seven localized copies were caught up.

This guard turns that class of bug into something CI can watch for, instead of relying on a reviewer noticing it again next time.

## How it works

- `.github/config/docs-drift.json` declares rules. Each rule has:
  - `truth_anchors` — files plus a regex pattern that represent the actual, enforced policy (e.g. `AGENTS.md`, `scripts/guard.ts`).
  - `documents` — a glob plus a "contradictory pattern" to scan for (e.g. `CONTRIBUTING.md` and every `docs/i18n/CONTRIBUTING.*.md`, checked for `daemon.*JavaScript`).
- `.github/scripts/docs_drift_guard.py` loads the rules, first confirms each truth anchor still matches the current source, and only then scans the matching documents for the contradictory pattern. If a truth anchor no longer matches (the policy itself changed), the rule is skipped instead of firing on a stale assumption.
- Findings are read-only: a markdown table (rule -> contradicting line -> the truth-anchor line it contradicts) is posted to the job summary. Nothing is written back to the PR or the repository.

## How correctness is checked

`--self-check` replays the exact #7513 contradiction against fixtures under `.github/fixtures/docs-drift/` and asserts the guard finds exactly that one contradiction, no more and no less. A second test runs the same rule against the real repository root instead of the fixture, and asserts the already-fixed French guide is *not* flagged — so correctness is checked against the live repository, not only against a canned example.

The historical baseline produced seven findings: the root `CONTRIBUTING.md` plus six localized guides still made the stale JavaScript claim. Running the same detector against #7513's corrected head produced zero findings. This `7 findings -> 0 findings` transition shows that the rule both identifies the known contradiction and clears after the documentation is synchronized.

The evidence is intentionally narrow. It validates one explicit TypeScript-first rule family; it does not claim general semantic understanding of documentation or automatic discovery of every possible source-to-doc mismatch.

## Why shadow mode

`docs_drift_shadow` in `ci.yml` runs only on `pull_request`, sets `continue-on-error: true`, and requests no write permissions. It observes and posts a summary; it cannot block a merge or touch PR state. The goal is to prove the signal is trustworthy against real history before ever proposing it become a required check.

This branch is a portfolio PoC maintained in a personal fork. It does not represent official OpenDesign adoption or a commitment by upstream maintainers to operate the guard.

## Adding a rule

1. Add an entry to `.github/config/docs-drift.json` with `truth_anchors` and `documents`.
2. Add a fixture pair under `.github/fixtures/docs-drift/<rule-id>/` — one truth file, one contradicting document.
3. Extend the expected set in `docs_drift_guard.py --self-check` and re-run it.
