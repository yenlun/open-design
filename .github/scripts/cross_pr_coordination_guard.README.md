# Cross-PR coordination guard (shadow)

## Why this exists

Two contributors can independently fix the same problem without either noticing, and reviewers can end up giving contradictory verdicts on essentially the same change because nothing surfaces the link between the PRs. That is not hypothetical in this repository right now: [#7446](https://github.com/nexu-io/open-design/pull/7446) and [#7521](https://github.com/nexu-io/open-design/pull/7521) both add the same `installStdioErrorGuard` fix for the same EPIPE crash (#6964) with near-identical code — `PerishCode` approved #7446, `nettee` requested changes on #7521. Same fix, opposite verdicts, both still open and both still consuming reviewer attention in the `needs-validation` queue.

## What it detects

For pairs of open PRs it computes:
- **Relation** — shared `Fixes #NNN`-style issue references, explicit "replaces / supersedes / reopening #NNN" language, and title similarity.
- **Implementation similarity** — file overlap and patch-text similarity (`difflib`) between the two diffs.

...and raises three signals when they line up:
- `COMPETING_IMPLEMENTATIONS` — same problem, different (or near-identical) code.
- `REVIEW_CONTRADICTION` — one reviewer approved, another requested changes, on what is effectively the same change (#7446/#7521 is the case that motivated this).
- `DUPLICATE_VALIDATION` — both PRs sitting in the same validation queue for the same underlying fix.

## The recall gap this was originally missing

The first version gated everything on `file_similarity >= 0.8` (files-in-common divided by files-in-union across both PRs). That misses two real patterns:

1. **Subset overlap** — a PR touching one file and a PR touching that file plus four others, both fixing the same referenced issue, computes to `file_similarity = 0.2` — well under the old 0.8 floor, so it was never even considered.
2. **Worded differently, same fix, no explicit cross-reference** — e.g. "find the real body for preview bridge injection" vs. "locate preview bridge injection points structurally": same underlying fix, no shared `Fixes #` reference, and the old candidate filter required `title_similarity >= 0.92` (near-identical titles) before a pair was even evaluated.

The latest commit (`ci: improve cross-PR guard recall`) adds two narrower paths in `is_competing()`: a strong relation (shared issue, or explicit replace/supersede/reopen language) plus any shared file at `file_similarity >= 0.15`; and, when there is no explicit reference, a lowered `CANDIDATE_TITLE_SIMILARITY = 0.45` combined with `file_similarity >= 0.3`. Both paths are covered by a new regression fixture (4 synthetic PRs, `e2e/tests/scripts/cross-pr-coordination-guard.test.ts`) run with `--strict`, so the recall improvement is asserted, not just described.

## Historical evaluation

The v1 detector rejected an initial 30-pair negative batch, but a separate 200-pair blind-to-prediction evaluation found four real competing pairs and v1 missed all four. Those revealed pairs became the v2 development set: v2 recovered all four positives while preserving all 196 negatives.

The detector was then frozen and tested on a fresh, non-overlapping holdout of 100 historical pairs (25 candidate-enriched and 75 strict controls, covering 107 PRs). Human labels were locked before identities, strata, and predictions were revealed. The holdout contained one new competing pair, #7454/#7419; v2 correctly emitted both `COMPETING_IMPLEMENTATIONS` and `DUPLICATE_VALIDATION`, with no false positives or false negatives across the 100 pairs.

That is useful out-of-sample evidence, not production-grade proof: the fresh holdout contained only one positive and no true review contradiction. The defensible result is **100/100 exact agreement on this stratified holdout**, while keeping the guard shadow-only until more independently labeled positives are available.

## Why shadow mode

`cross_pr_coordination_shadow` in `ci.yml` runs only on `pull_request`, with `continue-on-error: true` and the default read-scoped `github.token`. It observes and posts a summary; it does not comment on PRs, label them, or affect merge status. Same reasoning as the docs-drift guard: prove the signal holds up against real, already-open PRs in this repository before proposing it become a blocking check.

This branch is a portfolio PoC maintained in a personal fork. It does not represent official OpenDesign adoption or a commitment by upstream maintainers to operate the guard.
